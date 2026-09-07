# analysis_graph 重构设计：第二个 LangGraph 图 + interrupt() 人机协同（HITL）

> 状态：评审通过。M1（行为等价图骨架）、M2（SelectGate interrupt#1）、M3（ReviewGate interrupt#2：V17 `analysis_review_decision` 真值 + `continue`/`regenerate_after_review` 回边）、M4（收尾）**均已完成**：145 用例全绿、ruff 通过。验证与偏差详见 `docs/P13_ANALYSIS_GRAPH_VALIDATION.md`（本文件 §5/§6 中 "Command(resume) 字面时序"以 P13 偏差为准）。
> 技术前提：langgraph==1.2.10（`interrupt()` / `Command(resume=...)` / `AsyncSqliteSaver` 均可用，已在本机验证）。

---

## 0. 结论先行（TL;DR）

- **与 MCP 不冲突**：MCP 是"工具面"，LangGraph 是"控制面"。本设计只把 `document_analysis` 这一个任务的自有编排循环**换成第二个 LangGraph 图**；节点函数内部照旧调用 capability client / MCP 网关（`document_capabilities.parse_document`、文献客户端等），网关的路由/契约校验/降级/观测一行不改。
- 图中放 **两个 `interrupt()` 人门**：选文门（SelectGate）与复核门（ReviewGate）。
- `hitl_events`（waiting_for_human）仍是**人的决策记录与 UI/审计入口**；`interrupt()` 只是**执行层的冻结点**。二者通过"事件 resolve → 入队续跑 job → 驱动层以 `Command(resume=…)` 唤醒同一 checkpoint 线程"协同。
- 成本门槛（budget gate）**不改为 interrupt**：保持"在节点内安全边界检查 → 停车 `analysis_paused`（现有语义）→ resume 从 checkpoint 重入、靠业务表幂等跳过已完成单元"的机制，与图完全兼容（前提：节点体可重入）。

---

## 1. 现状（改动前的事实基线）

| 层 | 现状 | 位置 |
|---|---|---|
| LangGraph 图① | 仅检索 8 节点直线图，图内无 interrupt，checkpoint 仅用于崩溃续跑 | `app/graph/workflow.py`、`app/agents/literature.py`（thread_id `{project}:search-{rev}:{track}`） |
| 自有编排 | `document_analysis` job 由 `_analyze_selected()` 用 Python `for`/并发循环驱动：获取全文 → 逐篇精读（图表观察/一致性 → 索引 → 4 专家分批 → 概述 → 自动复核）→ 综合报告 | `app/services/workflow.py:117-260`、`app/agents/paper_analysis.py`、`app/documents/analysis.py` |
| HITL（人等） | DB 事件：`waiting_for_human` = `waiting` + open `hitl_event`；选文/缺全文/暂停点用 `wait_for_human`/`reopen("analysis_paused")` | `app/db/repositories.py`（V16 `hitl_events`）、`app/services/workspace.py` |
| 成本门槛 | V15：`llm_usage` 计量 + `budget_windows`；`budget_pause_reason()` 在论文间/图表边界/分析块间检查，命中抛 `AnalysisPausedError` | `app/db/research_repository.py`、`app/services/workflow.py`、`app/documents/analysis.py` |
| MCP 工具面 | 能力网关（capability 路由/契约校验/降级/观测）+ capability client；业务不直连 Server | `app/mcp/*`、`app/literature/*`、`app/services/workflow.py`（`self.app.state.document_capabilities`） |

**本次不动**：检索图①、MCP 网关与 Server、`hitl_events`/`waiting_for_human` 事件模型、决策台、成本门槛计量与阈值、`work_items.input_hash`/`visual_regions`/`selected_paper_analyses` 等幂等业务表。**只动**：`document_analysis` 的驱动方式 + 两个等待点的实现载体 + worker 对 job 的驱动。

---

## 2. 目标与非目标

**目标**
1. 让"整条分析流水线"成为可 checkpoint、可 debug、可向面试官画出节点/边/中断点的 LangGraph 图（图② `analysis_graph`）。
2. 选文、复核两个"等人"点以 LangGraph 官方 `interrupt()` 方式冻结在节点上，同时与既有 `hitl_events` 事件/UI/审计无缝协同。
3. 复用全部幂等基础，保证 interrupt 唤醒/预算停车/崩溃重启三类续跑都不重复消耗 token。

**非目标（本期不做）**
- 不把检索图①并入本图（保持两图：search / analysis）。
- 不重写 MCP 网关或换用 LangChain/LangGraph 自带 MCP 适配器——节点内继续用自家 capability client。
- 不新增顶层状态/事件机制（`waiting_for_human` 派生语义维持不变）。
- 不把"证据复核分歧自动检测"做成新的 LLM 通道（仍用决策台高危段触发 ReviewGate，见 §5.2）。

---

## 3. 总览：控制面换芯，工具面不动

```
                        ┌──────────── 工具面（MCP，不动）────────────┐
   LangGraph 控制面      │  capability client  ──▶ CapabilityRouter ──▶ MCP Server（自研文献/文档 + arXiv）
 ┌───────────────────┐  │     契约校验/降级/观测/调用事件             └───────────────────────────────┘
 │ 图① search_graph   │  │
 │ 图② analysis_graph◀┼──┼── 节点函数照旧调 capability client        （无第二套工具系统）
 └─────────┬─────────┘  │
           │ checkpointer (AsyncSqliteSaver，同一实例、同一 SQLite 文件)
           ▼
   worker/服务层：execute_job → 驱动图② → 捕获 GraphInterrupt / AnalysisPausedError
        ──▶ hitl_events（人的决策记录）+ UI + budget_windows + 业务幂等表
```

---

## 4. 图② 定义：节点 / 边 / 状态 schema

### 4.1 状态 schema（State channels）

```python
class AnalysisState(TypedDict, total=False):
    project_id: str
    search_revision: int
    track: str                       # = job.run_id 片段；thread_id 的一部分
    goal: str                        # 只读上下文（不进 reducer）
    requirements: str | None
    # —— 轻量指针（重 payload 一律在业务表，state 只放 id/摘要/flag）——
    selection_paper_ids: list[str]
    selected_papers_done: bool       # 选文门通过
    acquisition_complete: bool       # 所有选中论文 PDF 已 parsed（缺则事件停车，重入后重试）
    analyses: list[str]              # 已完成的 paper_analysis id（业务表以它为幂等锚点）
    review_gate: dict | None         # 复核门载荷：{high_risk:int, doubted_cited:[…], options…}
    review_outcome: str | None       # 'proceed' | 'regenerate_after_review'（resume 带回）
    synthesis_done: bool
```

原则：**每个节点输出先落业务表（region/evidence/work_item/analysis/report），再把"完成了什么"写回 state**——这正是今天 `_analyze_selected` 的做法，搬到图上不变。state 只做编排账本，不做结果容器，天然满足 checkpoint 序列化。

### 4.2 节点与边

```
START
  └─▶ select_gate ──interrupt#1(未选文)──▶ (人工 select_papers resolve)
           │ resume{paper_ids, requirements}
           ▼
       acquire_documents ──(有缺)→ 缺 PDF：抛 DocumentMissing → worker 停车 + document_unavailable 事件
           │ resume（upload 后重入，跳过已 parsed）
           ▼
       analyze_papers（逐篇子图/节点内循环，粒度=论文）
           ├─ parse+observe（图表批量：每图 boundary 查 budget_pause_reason）
           ├─ index 全文证据
           ├─ specialists×4（批内并发，块间 boundary 检查）
           ├─ overview
           └─ auto_verify 图表证据
           │  （每论文完成 → save_paper_analysis → analyses += id）
           ▼
       review_gate ──interrupt#2(high_risk>0)──▶ (决策台先复核 / 直接继续)
           │ resume{action:'proceed' | 'regenerate_after_review'}
           ▼
       synthesize（EvidenceSynthesizer：双篇比较 + 综合报告）
           ▼
       finish（projects.complete("analysis_review")）
  └─▶ END
```

边界（保持现状语义）：
- 论文间、图表边界、分析块间、合成前：节点内调用 `research.budget_pause_reason()`；命中则 `raise AnalysisPausedError(reason=…)`（**不**走 interrupt）→ worker 按今天方式停车/续跑。
- `acquire_documents` 与选文事件、缺 PDF 事件的承接关系见表 §6。

### 4.3 线程与续跑标识

- thread_id：`f"{project_id}:analysis-{search_revision}:{track}"`，与检索线程前缀 `search-` 区分；`ProjectRepository.delete` 现有的 `{project_id}:%` 前缀清理会自动覆盖两类图线程（无需新代码）。
- checkpoint：复用 `app.state.checkpointer`（同一 `AsyncSqliteSaver`）。

---

## 5. 两个 interrupt() 点

LangGraph 用法（v1.x）：节点内 `payload = interrupt(snapshot_payload)`；首次执行到该行抛 `GraphInterrupt` 并持久化状态；续跑用 `ainvoke(Command(resume=user_payload), config=同一 thread)`，节点从同一行继续并把 `user_payload` 赋给 `payload`。

### 5.1 SelectGate（选文门，interrupt#1）

- 位置：图入口第一节点（`select_gate`）。
- 触发：进入 `document_analysis` job 时若无有效 `paper_selection`（即"检索已完成但人还没选"）。
- interrupt 载荷（给人看）：
  ```json
  {
    "type": "paper_selection",
    "title": "请选择要精读的论文",
    "reason": "检索完成，候选 N 篇；确认前不下载全文",
    "options": [{"action":"select_papers"},{"action":"regenerate_search"},{"action":"reselect_papers"}]
  }
  ```
- resume 载荷（人给出）：
  ```json
  {"action": "select_papers", "paper_ids": ["…"], "requirements": "…"}
  ```
- 与现状差异：今天选文门在检索 job 结尾 `wait_for_human("paper_selection")`（图外）。改造后该门**进入图内**成为 interrupt#1；检索 job 依旧跑图①到"候选入库"即结束（不再自己开事件，改由 analysis 驱动开）。效果不变、载体换为"图内冻结 + checkpoint"。

### 5.2 ReviewGate（复核门，interrupt#2）

- 位置：`review_gate`——全部论文分析完成、**合成之前**。
- 触发（软门，默认可继续）：`review_desk` 式统计里存在"被结论引用且自动判存疑"的高危证据（`doubted ∧ cited_by≥1`，复用决策台判定口径的轻量版：只扫 `selected_paper_analyses` 的 supported 结论与 `evidence_reviews`）。无高危 → 直接放行，不 interrupt。
- 为什么放在合成前：综合报告是"最后一笔大调用"，人在此确认/排除可能翻转结论的证据，避免为注定要改的结论付合成成本（与成本控制叙事一致）。
- interrupt 载荷：
  ```json
  {
    "type": "evidence_review_gate",
    "title": "有 N 条被引用的自动存疑证据，先复核？",
    "scope": {"high_risk_count": 3, "evidence": ["evt-…"]},
    "options": [{"action":"review_in_desk","label":"先复核争议证据"},{"action":"continue","label":"直接生成报告（复核仍可事后做）"}],
    "default_action": "continue"
  }
  ```
- resume 载荷：
  - `{"action":"continue"}` → 直接进 synthesize（决策台事后可用，改动需走现有 stale+重新生成）。
  - `{"action":"regenerate_after_review"}`（M3 可选）→ 人先在决策台完成复核（写 `evidence_reviews`），图回边重跑"受影响论文的精读"节点（索引层 `excluded_ids` 已生效，未改动部分靠 work_item 缓存零重算）后再 synthesize。
- 与决策台关系：决策台 UI/API 原样保留；ReviewGate 只是把"高危复核"从**事后可选**升级为**合成前的软性阻塞点**（可跳过、不强制）。

---

## 6. hitl_events ↔ Command(resume) 协同时序

两条原则：
1. **事件是唯一的"人在等"事实来源**（UI/守卫/审计读它）；interrupt 是执行层镜像（"冻在哪个 checkpoint"）。
2. **事件的 resolve 动作是唯一入口**：任何"人给了决定"都必须先 resolve 事件（写 `resolution_json`），由 worker 把 `resolution_json` 翻译成 `Command(resume=…)` 唤醒图。

| 阶段 | 时序（事件 = 决策面 / 图 = 执行面） |
|---|---|
| 选文门 | ① 检索图①结束、候选入库 → 驱动层 `wait_for_human(paper_selection)`（事件 open、项目 waiting）<br>② 人点"选择论文" → `select_papers` action：resolve 事件（记 `resolution_json={paper_ids,requirements}`）→ 入队 `document_analysis`<br>③ worker 起 job → `ainvoke(analysis_graph, Command(resume=resolution_json), thread=analysis-… )` → SelectGate 从 interrupt 行继续 → 下载/分析 |
| 复核门 | ① 图跑完所有论文、`review_gate` 发现高危 → 节点 `interrupt()` → 驱动层捕获 `GraphInterrupt` → 开 `evidence_review_gate` 事件（open、项目 waiting）<br>② 人在决策台复核（写 `evidence_reviews`）→ 点"继续生成/先复核再重生成" → action resolve 事件（`resolution_json={action:…}`）→ 入队 `document_analysis`<br>③ worker `ainvoke(Command(resume=resolution_json), 同一 thread)` → ReviewGate 继续 → synthesize |
| 缺 PDF（保留事件停车，非 interrupt） | acquire 节点发现缺 → `raise DocumentMissing`（业务异常）→ worker 停车 + `document_unavailable` 事件；upload action resolve → 入队 → 图从 checkpoint 重入 `acquire_documents`，`visual_keys`/`paper_analyses` 等跳过已完成 |

守卫（沿用 V16）：job 启动前查 `has_open`，有 open 事件则 `hitl_gate_skipped`——因此"先 resolve 再入队"的顺序是硬约束（action 层已如此）。

审计闭环：事件的 `resolved_by + resolution_json + resolved_at` + trace（`interrupt_fired`/`graph_resumed`/`hitl_gate_skipped`）足以还原"人给了什么决定、图从哪个 checkpoint 继续、花了多少"。

---

## 7. 与 budget gate（成本门槛）的兼容

**结论：两套暂停各司其职，不冲突。**

| 维度 | budget/user 暂停（现有） | interrupt 等人（新增） |
|---|---|---|
| 触发 | 用量越阈值（`budget_pause_reason`）或人点暂停 | 选文/复核等"需要人给决定" |
| 载体 | `raise AnalysisPausedError` → worker 停车 `analysis_paused` + `pause_reason` + `budget_windows.open` | 节点 `interrupt()` → `GraphInterrupt` → worker 停车 + open `hitl_event` |
| 续跑 | 人工 `resume_analysis` → ack gate（重基线窗口）→ 入队 → **图从最近 checkpoint 重入**，节点内幂等跳过 | 人工 resolve 事件 → 入队 → `Command(resume=…)` **从同一 interrupt 行继续** |
| 语义 | "软性监督点"：每花满一个阈值块问一次 | "阻塞/软性决策点"：等到人给决定 |

预算检查点全部落在节点内（论文间/图表边界/分析块间/合成前），与图节点边界对齐——改造后把"检查"从 `for` 循环搬进对应节点函数即可，位置不变。可重入性由既有业务表保证：中断发生在节点中途（如第 12/30 张图）时，已观察图表在 `visual_regions` 有键、`completed_visuals` 已计数，重入节点会跳过它们再继续，不会重付视觉调用。**实现纪律：节点函数内不得把"是否完成"只放内存——沿用"先落库再更新 state"约定。**

---

## 8. 驱动层改造（worker / execute_job）

```python
# 伪代码：execute_job(document_analysis) 分支
project = await projects.get(job.project_id)
if await hitl.has_open(project.id):        # V16 守卫
    trace("hitl_gate_skipped"); return
config = {"configurable": {"thread_id": f"{project.id}:analysis-{rev}:{track}"}}
graph = build_analysis_graph(provider=..., checkpointer=..., sinks…)   # 每 job 构建（与今天 provider/sink 生命周期一致）
resume_payload = read_event_resolution(project.id) if resume else None
try:
    if resume_payload is not None:
        await graph.ainvoke(Command(resume=resume_payload), config=config)
    else:
        await graph.ainvoke(AnalysisState(project_id=…), config=config)
    await projects.complete(project.id, "analysis_review")     # finish 节点内的 complete 可保留在节点内
except GraphInterrupt as exc:
    # 图内等人：park 项目 + 开对应 hitl_event（SelectGate→paper_selection；ReviewGate→evidence_review_gate）
    await wait_for_human(project.id, stage_from(exc), event=…)
except AnalysisPausedError as exc:          # 预算/手动暂停：原语义
    await park_analysis_paused(project.id, exc.reason)
```

- job 类型仍只有 `research` / `document_analysis`，不新增；`resume=True/False` 由"事件 resolution 是否存在/项目是否 waiting"推导，动作层不变。
- 检索 job（图①）与缺 PDF/上传/暂停的既有动作（`upload`/`resume_analysis`/`reanalyze_part` 等）继续 resolve 事件后入队，新图自动承接。

---

## 9. 实施步骤与验收

| 里程碑 | 内容 | 验收 |
|---|---|---|
| M1 | 图②骨架：AnalysisState + 节点封装现有模块（先**无 interrupt**，节点直接复用 `_analyze_selected` 内部函数） | 现有 document_analysis 全流程行为回归（含 resume/预算暂停），`pytest` 全绿；金标样例一份 |
| M2 | SelectGate interrupt#1：选文门进图；`hitl_events`↔`Command(resume)` 时序 | 选文→分析→（崩溃/重启）→续跑路径正确；守卫测试（open 期间 job 被 skip） |
| M3 | ReviewGate interrupt#2：合成前高危统计 + interrupt + proceed / regenerate_after_review 回边 | 高危触发→人跳过→正常合成；人排除→重生成只重跑受影响论文（work_item 零重算断言） |
| M4 | 收尾：trace 事件、README/`docs/P13_ANALYSIS_GRAPH_VALIDATION.md`、面试口径 | 全量回归 + 手工走查两中断点 |

**风险与缓解**：中断点状态与业务表不一致（→ interrupt 载荷只读派生，写仍走节点+action 单写方）；`for` 循环改图造成回归（→ M1 纯搬运、行为先等价再动门）；双线程（search/analysis）前缀混淆（→ thread 命名 + delete 前缀已覆盖 + 单测断言）。

---

## 10. 明确不做 / 面试口径

**不做**：检索图与 analysis 图合并；`interrupt()` 化 budget gate；用 LangGraph 自带 MCP adapter 取代自家网关；复核分歧双通道检测。

**面试口径一句话**：
> "工具面是我的 MCP 能力网关，控制面是两张 LangGraph 图：检索一张、分析一张。人机协同用 LangGraph 原生 interrupt 实现——选文门和合成前的高危复核门都冻结在节点上，同时与我的 hitl_event 事件表协同：事件负责'等什么/谁来解决'的 UI 与审计，interrupt 负责'冻在哪个 checkpoint'，人 resolve 事件后我用 Command(resume) 从同一节点唤醒，续跑复用 input_hash/visual_regions 等幂等断点，不重复消耗 token。"

---

## 11. 代码落点清单（供实施时对照）

- 新增 `app/graph/analysis_nodes.py`（select_gate / acquire_documents / analyze_papers / review_gate / synthesize / finish 节点函数，搬运 `services/workflow.py` 现有逻辑）
- 新增 `app/graph/analysis_state.py`（AnalysisState TypedDict + channel 注释）
- 新增 `app/graph/analysis_graph.py`（`build_analysis_graph(provider, checkpointer, …)` 拓扑 + 边）
- 改 `app/services/workflow.py`：`document_analysis` 分支改为图驱动（§8 伪代码），删除 `_analyze_selected` 中的门逻辑（迁入节点）
- 改 `app/agents/paper_analysis.py` / `app/documents/analysis.py`：仅把"暂停检查位置"保持原样（已在节点内）
- 改 `app/services/workspace.py`：`select_papers`/复核相关 action 的 resolution_json 与 Command 载荷映射
- 新增 `docs/P13_ANALYSIS_GRAPH_VALIDATION.md`；更新简历条目文档
