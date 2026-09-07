<!-- 目标读者：后续实施者 + 面试陈述。所有"现状"均以当前代码为准（行号仅为定位提示，改代码后可能漂移）。 -->

# ResearchPilot HITL 升级实施计划：事件化干预 · 复核决策台 · 成本门槛暂停

> 一句话目标：把 HITL 从"流程里两个等人节点"升级为**一等公民状态（事件 + 可干预动作）**，把复核从"逐条点按钮的队列"升级为**按决策价值排序、影响可预见、产出可量化的决策台**，并给长任务加**成本门槛暂停（软性监督点，默认继续）**。

> 三句话串讲（面试用）：
> 1. "等人"不再散落在流程代码里，而是统一成 `hitl_event`（类型/触发原因/影响面/建议动作/可执行动作），任务以 `waiting_for_human` 停靠在工作流节点上——把已有的"重启续跑"能力泛化成"人审后从同一节点继续"。
> 2. 复核按"被多少结论引用 × 是否支撑双篇比较 × 自动存疑原因"排序入队，每个决定先回显影响（排除 → 几条结论降级），结束后给出"改动 N 条结论（M 降级/K 确认）"的量化摘要——放大人的单位时间价值。
> 3. 分析是多阶段长任务，单次运行累计 token / 视觉调用 / 耗时过阈值即在幂等断点处暂停，询问"剩余 N 篇分析是否继续？"，默认继续、可一次性"跳过剩余图表"或"中止"。

---

## 0. 现状盘点与差距分析（先对齐事实，再谈设计）

### 0.1 现状关键事实（代码定位）

| # | 能力 | 现状 | 位置 |
|---|---|---|---|
| 1 | 顶层状态机 | `projects.status ∈ created/running/waiting/completed/failed`；`wait()` 把项目置为 terminal `waiting` + `current_stage`；`reopen()` 置回 `waiting` 并换 stage | `app/db/repositories.py`（start_run/wait/reopen/_set_terminal）；`app/db/database.py:31-36` |
| 2 | "人等"点 1：选文 | 检索完成 → `projects.wait("paper_selection")`；人选定 1-2 篇 → `select_papers` action → `reopen("acquiring_selected")` + 入队 `document_analysis` | `app/services/workflow.py:267`；`app/services/workspace.py:462-492` |
| 3 | "人等"点 2：缺全文/坏 PDF | 逐篇自动获取失败 → `projects.wait("awaiting_documents")`；UI `UploadDocumentsAction` 逐篇上传 → 上传后入队续跑。坏 PDF"替换"也走这个通道（重新 upload 同一 paper） | `app/services/workflow.py:90-143`；`app/services/workspace.py:431-442,519-540`；`ui/app.py:203-222` |
| 4 | 第三个"人参与面"（不阻塞）：证据复核 | 研究资料卡片上逐条点 确认/存疑/排除，写 `evidence_reviews`（PK=evidence_id，**无来源/无会话字段**）；复核只影响后续重建，不阻塞主流程 | `ui/app.py:236-245`；`app/db/research_repository.py:51-94`；表结构 `app/db/database.py:357-363` |
| 5 | 自动复核通道 | 视觉验证器写 `note="自动复核：…"`；图文一致性写 `note="图文一致：确认/存疑"`；两者都"从不覆盖已有复核"（`reviewed_evidence_ids` 拦截）——因此**双通道分歧今天会被静默吞掉**（先到先得） | `app/evidence/visual_verifier.py`；`app/evidence/consistency.py:93-167`；`app/db/research_repository.py:68-84` |
| 6 | 双篇比较 | 仅当选中 2 篇时由 `EvidenceSynthesizer.run` 按**固定四维**（共同点/差异/互补性/适用条件）生成 `ComparisonDraft` | `app/agents/paper_analysis.py:542-580` |
| 7 | 续跑能力（两个机制） | 检索阶段：LangGraph `AsyncSqliteSaver`（thread_id=`{project}:search-{rev}:{track}`），失败/等待项目以 `resume=True` 重跑；分析阶段：DB 幂等断点——`work_items.input_hash` 同参不重算、`document_analyses` 按 (document,input_hash) 去重、`paper_analyses` 已完成即跳过 | `app/agents/literature.py`；`app/services/workflow.py:61-80,203-213`；`app/db/progress_repository.py`；`app/agents/paper_analysis.py:442-483` |
| 8 | 用量/成本 | **无 token 台账**。`OllamaProvider._request` 拿到完整响应 dict（含 `prompt_eval_count/eval_count/eval_duration` 等）但只消费文本、把其余丢弃；现有粒度只有 `traces.latency` 与 `document_analyses` 的图表计数 | `app/llm/ollama.py:163-240` |
| 9 | UI 动作入口 | 单一切面 `WorkspaceService.action()`（type 分发），`next_action` 派生 UI 渲染器，`ACTION_RENDERERS` 三种 | `app/services/workspace.py:447-513`；`ui/app.py:551-557` |

### 0.2 与三个目标的差距

| 目标 | 缺口 |
|---|---|
| ① 事件化 HITL | "等人"只被建模成 `waiting + stage 字符串`，**原因、影响面、可选动作、可执行动作、谁触发的都散落在各 action 分支与 UI 文案里**；没有"事件"实体，就无法回答"现在到底在等什么、有哪些出路、为什么会等"。第三个参与面（证据复核）甚至不阻塞，属于"隐形等人"。 |
| ② 复核决策台 | `evidence_reviews` 无来源/无理由结构化字段；复核入口按论文/证据分散，**无风险排序、无影响回显、无批量闭环摘要**；无法回答"我先审哪条最值钱""排除这条会发生什么""这次审完产出了什么"。 |
| ③ 成本门槛 | 长任务（每篇：解析→全文证据索引→每图表 1 次观察 + 一致性 + 自动复核多次视觉调用→4 组专家批量→概述）**无累计计量、无软性监督点**；成本叙事只有 latency 佐证，缺少"我能看见并控制跑量"的证据。 |

---

## 1. 方案一（主推）：HITL 事件化 —— "两个节点等人" → "显式事件 + 可干预动作"

### 1.1 统一模型：一个事件 = 一次"在某个节点等人"的完整描述

事件是**一等公民**：任务在事件上停靠（`waiting_for_human`），事件携带"等什么 / 为什么 / 影响谁 / 人可以做什么 / 每步后果 / 不做会怎样"，由**动作分发器**唯一地消费它并决定续跑路径。

**字段模型（新 schema：`HitlEvent`）**

| 字段 | 说明 | 示例 |
|---|---|---|
| `event_id` | 事件唯一 ID | `evt_…` |
| `project_id` / `run_id` | 归属项目与产生它的运行 | |
| `type` | 事件类型（目录见 1.2） | `paper_selection` |
| `title` / `reason` | 面向人的标题 + 触发原因（自动可含结构化 cause） | "需要选择要精读的论文" / "检索完成，命中 17 篇" |
| `scope` | 影响面：受影响的实体集合（papers/evidence/documents 的 id 列表 + 摘要） | `{papers:[…], count:2}` |
| `level` | `blocking`（必须人处理才能前进）/ `escalation`（风险升级，建议人看，可暂时忽略）/ `info` | |
| `options` | **建议动作**（人可选的出路，按推荐度排序；每项带 label + 影响提示） | `[选文并开始分析, 重新检索, 换一批检索词]` |
| `actions` | **可执行动作**列表（程序可执行的动作 id + 参数 schema，见 1.4） | `select_papers(paper_tokens,requirements)` … |
| `default_action` | 超时/忽略时的默认（决策台与成本门槛的关键字段） | `continue_analysis` |
| `created_by` | `system / auto_review / budget_gate / human` | |
| `status` | `open / resolved / cancelled / superseded / expired` | |
| `resolved_by` / `resolution` | 谁、用哪个动作 + 参数 + 备注解决 | |
| `created_at` / `resolved_at` | | |

JSON 示例（一次"坏 PDF"事件）：

```json
{
  "event_id": "evt_9f2c",
  "type": "document_bad_pdf",
  "title": "《XXX》PDF 解析失败，需要替换",
  "reason": "自动获取的 PDF 解析 0 页；尝试重试后仍失败（error: parse empty page set）",
  "scope": {"papers": ["paper:…"], "evidence_invalidated": [], "downstream": "该论文的分析将暂停，其余论文不受影响"},
  "level": "blocking",
  "options": [
    {"action_id": "upload_replacement_pdf", "label": "上传可用的 PDF 替换", "impact": "仅重跑该论文的解析与精读"},
    {"action_id": "retry_auto_fetch", "label": "再试一次自动获取", "impact": "免费；可能再花 1-2 分钟"},
    {"action_id": "exclude_paper_from_analysis", "label": "放弃该论文，只分析其余", "impact": "双篇比较自动降级为单篇分析"}
  ],
  "actions": ["upload_replacement_pdf", "retry_auto_fetch", "exclude_paper_from_analysis"],
  "default_action": null,
  "created_by": "system"
}
```

### 1.2 事件目录（v1，覆盖现有两个等待点 + 预留三类升级点）

| `type` | 触发方 / 触发规则 | 影响面 scope | 建议动作 options | 可执行动作 actions | 现状对应 |
|---|---|---|---|---|---|
| `paper_selection` | system：检索完成且当前无有效选择 | papers | 选 1-2 篇精读 | `select_papers`、`regenerate_search(instruction)`、`cancel_selection`（回到检索） | 现有等待点 1 |
| `document_unavailable` | system：OA 获取失败/无公开 PDF | papers×1..N | 上传 / 换源 / 放弃 | `upload_pdf`、`retry_auto_fetch`、`exclude_paper` | 现有等待点 2（awaiting_documents） |
| `document_bad_pdf` | system：上传/获取的 PDF 解析失败或解析 0 页 | paper + 失效证据 | 替换 PDF | `upload_replacement_pdf`、`retry_auto_fetch`、`exclude_paper` | 现有通道隐式支持（upload 覆盖），事件化后显式 |
| `analysis_budget_exhausted`（= 方案三的 cost_gate） | budget：token/视觉调用/耗时超阈值 | 剩余 papers/visuals | 继续 / 跳过剩余图表 / 中止 | `continue_analysis`(默认)、`skip_remaining_visuals`、`abort_analysis` | 新增 |
| `auto_review_disagreement` | auto_review：**双自动通道分歧**（视觉验证 confirmed vs 一致性存疑）或 doubted 命中"高影响证据" | evidence 集合 | 复核争议证据 | `open_review_desk`、`confirm_evidence`、`exclude_evidence`、`ignore` | 新增（今天分歧被先到先得静默吞掉，见 0.1#5） |
| `comparison_dimension_choice`（可选） | system（默认 `skip` 自动继续）：双篇进入合成前，允许选比较维度 | comparison | 用默认四维 / 自选维度 | `use_default_dimensions`(默认)、`select_dimensions(dims,instructions)` | 新增（合成目前固定四维） |

> 触发哲学（也是 1.4 的规则来源）：**能安全自动的绝不上人；只有"阻塞性事实缺失"或"自动判据弱 + 影响面大"才升级**。`blocking` 事件=确定性事实缺失；`escalation` 事件=风险规则命中（低置信度 / 跨模态冲突 / 成本超阈值）。

### 1.3 状态模型："等人"是显式状态，停靠在节点上

**推荐：不新增第六个顶层状态，把 `waiting + 一条 open hitl_event` 定义为 `waiting_for_human`（派生语义视图）。**
理由（写进面试口径）：
- `wait`/`reopen` 已是 terminal 语义，`start_run(resume=True)` 只认 `{failed, waiting}`，任何第三方都不会在 waiting 时误跑；
- 事件表独立演进，不需要动 `CHECK(status IN …)`、所有 `status in {...}` 分支与既有测试；
- API/UI 面向用户呈现 `waiting_for_human`（stage 派生时查 open event），数据库不引入第二套会打架的状态机。

实现要点：

1. **表 `hitl_events`（迁移 V13）**：见 §1.6。
2. **开事件**：`ProjectRepository.wait_for_human(project_id, stage, event)` = 一个事务里 `wait(stage)` + `INSERT hitl_events(status='open')`，保证"项目停在节点"与"事件存在"原子。
3. **Worker 守卫**：`WorkflowWorker`/`execute_job` 领取任务前检查该项目是否存在 `open` 事件；存在则**不启动**（记 trace `hitl_gate_skipped`）。这样人在考虑期间，任何自动唤醒都不会绕过人的决定——这正是"等人是显式状态"的运行时保证。
4. **解决事件 = 从该节点继续**：`resolve(event_id, action_id, payload)` 由动作分发器执行，动作描述三件事：
   - 数据变更（写 selection / 换 document / 设 run 参数）；
   - 项目迁移（`reopen(target_stage)`，回到对应等待点之前的节点）；
   - 续跑（`enqueue(job_type)` → worker 以既有 `resume` 路径拾起，靠 §0.1#7 的两个幂等机制从断点继续，**已完成单元绝不重算**）。
5. **续跑是"重启续跑"能力的泛化**——把这条写清楚：

| 阶段 | 断点机制 | 人审后如何续 |
|---|---|---|
| 检索（LangGraph） | checkpoint thread `{project}:search-{rev}:{track}` | 重入 `LiteratureResearcher` 带 `resume=True`，图从最后完成的节点继续 |
| 全文/图表分析 | `document_analyses` 按 (document,input_hash) 幂等 + `completed_visuals` 计数 | 已解析文档跳过；只跑新文档 |
| 专家精读 | `work_items.input_hash` 幂等（`replace_changed=True`） | 已完成 section 直接读缓存结果 |
| 逐篇粒度 | `selected_paper_analyses` 已完成即跳过 | `_analyze_selected` 循环只补缺失论文 |

### 1.4 介入时机规则（回答"哪些必须人工、介入时机怎么设计"）

事件产生于**两个正交来源**，配成一张规则表（全部可配置，settings 前缀 `hitl_*`）：

A. **确定性阻塞源**（无默认动作、必须人）——只覆盖机器无法安全自决的事实缺失：
- 选文（业务上强制"人确认要精读哪几篇"）
- 全文缺失 / PDF 损坏（法律与质量边界：不自动用不可信来源替代）
B. **风险升级源**（有默认动作、可忽略、但命中即提示）：
- 证据 `confidence < hitl_min_confidence` 且被 ≥2 条结论引用（低置信度高影响）
- 跨模态自动复核**双通道分歧**或 doubted 数量占该文被引用证据的比例 ≥ `hitl_disagreement_ratio`
- 单次运行用量超 `hitl_budget_*`（见方案三）
C. **默认自动**：其余一切照旧（自动复核写库、自动下载重试、全自动合成）。

> 设计准则一句话：**升级的代价是人时间，所以每条升级规则都要满足"影响面大（scope 高）且自动判据弱（不确定性高）"；只满足其一就默认自动并记 trace。**

### 1.5 动作分发器与接口

- 新服务 `HitlService`（或并入 `WorkspaceService.action` 的 `resolve_hitl` 分支）：`open()` / `list_open(project_id)` / `resolve(event_id, action_id, payload)` / `supersede(old,new)`（例如 `regenerate_search` 会把未决的 `paper_selection` 事件 supersede 掉，避免一个项目同时挂两个互斥事件）。
- 现有 `action()` 的每个分支**登记为对应事件类型的可执行动作**：`select_papers/regenerate_search/reselect_papers/reanalyze_selected/evidence_review/upload` 逐步迁到"先找 open 事件再执行"的路径；**过渡期两套 API 并存**（旧请求若无 open 事件则按旧语义执行，保兼容）。
- UI：`step_header` 之上渲染"待你决定"横幅；`next_action` 由"open 事件 → 事件卡（reason/scope/options/默认动作）+ 动作按钮"驱动；`ACTION_RENDERERS` 增加事件渲染器族。事件卡长这样（决策台同款样式复用，见方案二）：

```
┌ 需要你决定 ──────────────────────────────┐
│ ⚠ document_bad_pdf：《XXX》PDF 解析失败     │
│ 影响：仅该论文暂停；其余论文不受影响          │
│ [上传替换 PDF] [重试自动获取] [放弃该论文]    │
└──────────────────────────────────────────┘
```

### 1.6 数据库迁移 V13（草案）

```sql
CREATE TABLE IF NOT EXISTS hitl_events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id TEXT,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    reason TEXT,
    scope_json TEXT NOT NULL,          -- 影响面（papers/evidence/documents id + 摘要）
    options_json TEXT NOT NULL,        -- 建议动作（人可读，按推荐度排序）
    level TEXT NOT NULL CHECK(level IN ('blocking','escalation','info')),
    status TEXT NOT NULL CHECK(status IN ('open','resolved','cancelled','superseded','expired')),
    created_by TEXT NOT NULL,
    default_action TEXT,               -- 无默认=必须人处理
    resolved_by TEXT,
    resolution_json TEXT,              -- {action_id, payload, note}
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    superseded_by TEXT REFERENCES hitl_events(id)
);
CREATE INDEX idx_hitl_events_open ON hitl_events(project_id,status,created_at);
```

同时为事件"原因/来源"的可审计性，`traces` 保持现有追加式审计作为事件附属日志（不放进事件表，事件只存当前态）。

### 1.7 验收标准（方案一）

- [ ] 现有两个等待点在**不改 UI 习惯**的情况下，全部以事件形式出现并可被 `resolve` 续跑（回归：`tests/test_workspace_stage_consistency.py` 全家绿）。
- [ ] 人为制造"坏 PDF 替换"：替换后只重跑该论文，其余论文的分析结果与 work_items 均未重算（断点证明）。
- [ ] open 事件存在期间，任何 `run`/wake 都不会把项目推进（守卫测试）。
- [ ] `regenerate_search` 会把旧 `paper_selection` 事件 supersede。
- [ ] 全量测试 + `docs/P*_VALIDATION.md` 风格补一份 `P10_HITL_VALIDATION.md`（见 §5）。

---

## 2. 方案二（收益最直接、改动最小）：复核从"队列"变"决策台"

### 2.1 先补齐数据基础（当前缺的字段）

`evidence_reviews` 现状只有 `(evidence_id, status, note, updated_at)`，无法回答"这条是谁判的、为什么判、人审过没有"。V13 一并加列（对历史行用 note 前缀回填）：

```sql
ALTER TABLE evidence_reviews ADD COLUMN source TEXT;   -- human / auto_visual_verifier / auto_consistency
ALTER TABLE evidence_reviews ADD COLUMN review_session_id TEXT; -- 一次人审会话分组
-- 回填：note LIKE '自动复核：%' -> auto_visual_verifier；note LIKE '图文一致：%' -> auto_consistency；
--       其余有行但无 note 前缀 -> human（现状 human 动作可带可无 note，结合 traces 再核）。
```

证据的**影响面元数据**不落库硬算、**读取时由服务派生**（写时维护易与报告重生成失同步）：
- `cited_by`：解析 `selected_paper_analyses.payload_json` 与 `analysis_reports.payload_json` 中所有 `kind=supported` 结论的 `evidence_ids`，统计每条被引用次数；
- `supports_comparison`：引用它的结论是否属于 `analysis_report.comparison`（共同点/差异/互补性/适用条件）字段；
- 自动存疑原因：来自该证据最新一条 auto 复核的 `note`（结构化后保留 reason 摘要）。

### 2.2 风险分级入队（决策价值排序）

**队列不是"所有未审证据"，而是按"审它的预期价值"排序的工作列表**，分三段：

| 段 | 判据 | 目的 |
|---|---|---|
| ① 高危（默认只看这段） | `status='doubted'`（自动通道判存疑）**且** `cited_by≥1`；或双通道分歧命中 | 自动判据弱又影响结论的，人先看 |
| ② 高影响确认（可选展开） | `cited_by≥2` 且 `kind=supported` 的图/表证据尚未被人工确认 | 把支撑多结论的钉子钉死（QA） |
| ③ 常规 | 其余 unreviewed | 仅在"我要全量过一遍"时出现 |

段内排序打分（默认权重可配置）：

```
priority = 0.45 * min(cited_by,5)/5
         + 0.25 * (1 if supports_comparison else 0)
         + 0.20 * doubt_severity        # auto doubted=1；双通道分歧=1.3；其余 0
         + 0.10 * (1 - min(confidence,1))   # 自动置信越低越靠前
```

派生查询（示意）：join `evidence_reviews` ↔ 引用计数视图，`WHERE status IN ('doubted',…) ORDER BY priority DESC LIMIT 20`，每项携带"引用结论预览（前 3 条 value 摘要）+ 每条自动判定的 note + 图/表源图链接"。

### 2.3 影响面回显：让人在点"排除"之前先看见后果

**干跑（dry-run）算法**（纯读、事务外，毫秒级）：

```
preview_exclude(evidence e):
  claims_citing = 所有结论中 evidence_ids ∋ e.id 的集合
  for c in claims_citing:
     if len(c.evidence_ids) == 1 and c.kind == 'supported':
         c 将在排除后降级为 inference          # 失去唯一证据
     else:
         c 保留 supported（仅少一条来源）        # 仍有多条证据
  return {cited_total, will_downgrade: [...], still_supported: [...],
           supports_comparison: 是否含 comparison 结论}
```

UI 交互：点击"排除"先弹确认行（不是 dialog 打断，而是就地回显）：

> 这条证据被 **3 条结论** 引用；排除后 **1 条** 将自动降级为推断（列出该结论摘要），**2 条**仍保留证据支持（仅移除本条来源）。确认排除？

**确认同理**给一句话回显（"该条支撑的 3 条结论将保持证据支持"），让人每个动作都"可预见后果"。预览与提交之间用同一函数，杜绝"预览一套、落库另一套"。

### 2.4 复核闭环与量化摘要

- **会话分组**：进入决策台即开一个 `review_session`（UI 侧无感），每个提交动作带 `review_session_id`；
- **提交动作**：复用现有 `evidence_review` 写库语义（upsert，不覆盖旧 human 决定），但改为**决策台端点**批量/单条提交，每个提交返回该条的实际影响（与预览一致）；
- **会话结束摘要**（离开决策台 / 手动"完成本轮复核"时生成，写 `traces` + 展示）：

> 本次复核共 **8 条**：确认 4 · 转存疑 2 · 排除 2 —— 影响 **5 条结论**：降级 1 · 保持证据支持 4。

- **闭环到报告**：排除只影响"将来重建"，已生成报告仍是旧结论 → 决策台顶部常驻一个"报告需更新"提示 + 一键"基于现有证据重新生成分析"（复用 `reanalyze_selected` 动作；`EvidenceRepository.excluded_ids` 已在重建路径生效，见 `app/db/evidence_repositories.py:157`）。这保证"人审的产出真正改写了最终结论"，是决策台**闭环**而非又一个只读清单。

### 2.5 API 与 UI 改动

- `GET /projects/{id}/review-desk?segment=high_risk`：分段队列 + 每项元数据（cited_by / supports_comparison / auto notes / 结论预览）。
- `POST /projects/{id}/review-desk/preview`：`{evidence_token, status}` → 影响回显（不落库）。
- `POST /projects/{id}/review-desk/commit`：`{evidence_token, status, note?}` 落库并返回实际影响。
- `POST /projects/{id}/review-desk/session/summary` 或提交即累计、页面关闭时自动结算摘要。
- UI：报告阶段（`analysis_review`）新增"复核决策台"tab（`ui/app.py`），三段式队列、单条证据大卡（图/文 + 各自动通道判定卡片 + 引用结论列表）、影响回显行、段内"全部确认/忽略"批量操作（带二次确认与合并影响预览）、会话摘要条。现有"研究资料"卡片上的三个按钮保留（快路径），但操作同样经过 preview→commit 语义。

### 2.6 验收标准（方案二）

- [ ] doubted 且高引用的证据永远排在 unreviewed 低引用之前（排序单测 + 黄金样例）。
- [ ] `preview` 与 `commit` 对同一证据返回一致的影响（含降级集合文本一致）。
- [ ] "排除唯一来源 → 结论降级"的干跑在真实报告 payload 上验证通过（gold 断言）。
- [ ] 历史数据回填后，`source` 无 NULL（或只有明确未知标记）。
- [ ] 决策台会话结束后生成可展示摘要，且 `traces` 可还原"谁改了什么、为什么"。

---

## 3. 方案三：长任务"成本门槛暂停"

### 3.1 计量（补齐 token 台账）

- `OllamaProvider` 现在把响应 dict 里的 `prompt_eval_count / eval_count / eval_duration / total_duration` 全部丢弃（`app/llm/ollama.py:163-240`）。改动：provider 增加可选 `usage_sink: Callable[[LLMCallUsage], Awaitable|None]`，在 `_request` 成功返回处（单点，text/vision 全覆盖）发射一次计量；**重试只记最终成功那次**，另记 `attempts` 与累计时长，便于审计"重试成本"。
- 新表 `llm_usage`（V13）：`id / project_id / run_id / phase / item_key / kind(text|vision) / model / prompt_tokens / completion_tokens / latency_ms / created_at`。逐条追加、只进不改（审计面）。
- 计量接收方在**每次任务运行**注入：`ResearchWorkflowService.execute_job` 创建本次运行的 `AnalysisBudget`（阈值来自 settings/项目覆盖），worker 每个 agent 构造时把同一 sink 传下去。
- 成本口径：本地 Ollama 以"调用量/耗时"为主，`estimated_cost_usd` 按可配置单价（text/vision 每百万 token）**估算列**展示（不给本地模型强加会计，保留对外叙事）。

### 3.2 阈值与检查点（软性监督点）

**检查点位置必须全部落在幂等断点边界上**（保证暂停后可无损续跑，呼应"停在工作流节点上"）：

| 边界 | 位置（现状代码） | 暂停时已完成的量 | 语义 |
|---|---|---|---|
| 逐篇 | `_analyze_selected` 的 `for paper_id in selection["paper_ids"]`（`app/services/workflow.py:158`） | 已完成的 N-M 篇分析 | 询问"剩余 M 篇是否继续" |
| 单篇图表批次 | `DocumentAnalysisPipeline` 每张图表完成后 `advance_visual_progress` | `completed_visuals/total_visuals` | 触发"跳过剩余图表"的粒度 |
| 专家批量 | `PaperAnalyst` 4 组 specialists 之间（已有 `set_analysis_step` 边界） | 已完成的 section | 高 token 消耗点 |
| 合成前 | `EvidenceSynthesizer.run` 前（可选） | 全部单篇分析 | 比较/报告是最后一大笔调用 |

阈值（settings 新增，默认值按一次典型双篇任务给，全部可被项目级参数覆盖）：

```python
hitl_budget_enabled: bool = True
hitl_budget_tokens: int = 600_000            # 累计 token（含历史补跑则按 run 计）
hitl_budget_vision_calls: int = 60           # 视觉调用数（观察+一致性+自动复核）
hitl_budget_elapsed_minutes: int = 45        # 运行墙钟
hitl_budget_grace_minutes: int = 15          # 未应答宽限：到期默认继续一次
```

**语义（与需求一致）**：越过任一阈值 → 在当前检查点产出 `analysis_budget_exhausted` 事件（blocking 之外的风险级 escalation，见 §1.4-B），**默认动作 `continue_analysis`**；若在宽限窗口内无人应答，事件自动按默认继续并记 trace（"默认继续"，不把流程饿死）；人若在场可一次性选"跳过剩余图表"或"中止"。

### 3.3 三个可执行动作的落地

| 动作 | 效果 | 落地方式 |
|---|---|---|
| `continue_analysis`（默认） | 本次运行续跑，不再重复询问（一次性放行：记 `resolved_by=budget_grace|human`） | 事件 resolved → `reopen` + enqueue，`resume` 拾起 |
| `skip_remaining_visuals` | 剩余论文只做正文/表格精读，跳过图表视觉观察与图表证据自动复核 | 在 selection 会话/运行上下文写 `run_policy={skip_visuals:true, remaining_papers:[…]}`；`DocumentAnalysisPipeline` 与 `PaperAnalyst._auto_verify_visual_claims` 读取该策略跳过视觉段；报告 `warnings` 注明"图表证据被跳过"（诚实降质而非静默） |
| `abort_analysis` | 停止本次运行，保留全部中间产物（已解析文档、证据、已完成论文分析），项目停在 `partial`（展示用失败原因 `user_aborted`，可安全重试） | `cancel_project` + `projects.fail(stage=当前检查点, error={type:'user_aborted'})`；由于所有单元幂等，重试只会补缺 |

### 3.4 UI

- 分析阶段进度条旁常驻**用量计量条**：`token 62.4k / 600k · 视觉 18/60 · 43/45 min`（数据源 `llm_usage` 实时 SUM + `document_analyses` 计数，恰好复用现有 `WorkspaceProgress` 的刷新节奏 `@st.fragment(run_every=3)`）。
- 触发时在检查点渲染成本门槛卡（复用方案一事件卡 + 方案二回显风格）：给出"已用 / 阈值 / 剩余 N 篇预估用量"与三个动作按钮，倒计时提示默认继续。

### 3.5 验收标准（方案三）

- [ ] 人为把阈值调小 → 运行精确停在检查点、无半途 LLM 调用被切断；续跑后已完成单元零重算。
- [ ] "跳过剩余图表"：剩余论文不再产生 vision 调用（用 `llm_usage.kind='vision'` 断言），报告 warnings 含说明。
- [ ] "中止"：中间产物可查、可安全 resume。
- [ ] 宽限到期未应答 → 自动按默认继续且 trace 记录 `budget_grace`。
- [ ] 计量对 text/vision/重试的统计与真实 Ollama 响应一致（gold 对齐测试）。

---

## 4. 实施顺序与里程碑（两条路径都给出）

三块不是互斥堆叠，而是一套模型的三个入口；推荐按**依赖最少的先落地**：

**推荐路径 A（先做 2，再做 1，最后 3）——理由：②改动最小、收益立现，且它产出的"影响回显/会话摘要"恰好是①事件卡的 UI 组件与③的摘要复用件。**
1. **M1（方案二）**：V13 迁移（含 `source/review_session_id` 与 `hitl_events`/`llm_usage` 表一次建齐，避免三次升版本）+ 决策台 API/UI + 排序/干跑/摘要。交付 `docs/P10_DECISION_DESK_VALIDATION.md`。
2. **M2（方案一）**：`hitl_events` 全生命周期 + worker 守卫 + 两个等待点改造 + 事件卡 UI + 旧 action 兼容层。交付 `docs/P11_HITL_EVENTS_VALIDATION.md`。
3. **M3（方案三）**：usage sink + 计量表 + `AnalysisBudget` 检查点 + 成本门槛卡 + 三个动作。交付 `docs/P12_BUDGET_GATE_VALIDATION.md`。
4. **M4（串联）**：用 `auto_review_disagreement`（①）把决策台（②）的高危段自动送入队列，用 `analysis_budget_exhausted`（③）演示一次完整"长任务监督"走查；补 end-to-end 回归。

**路径 B（面试叙事优先）**：先 1 再 3 再 2——事件模型是 3 的地基（cost_gate 就是事件），2 收尾展示闭环。改动总量相同，风险略高（先动状态机）。

每阶段都要：新增/更新单测与集成测试（`tests/test_*.py` 风格）+ 一处黄金样例断言 + 一份 VALIDATION 文档；最终全量 `pytest` + 现有 `evals/` 回归绿。

---

## 5. 面试口径（FAQ 速答，3 句/问）

**Q：哪些操作必须人工确认？介入时机怎么设计？**
> 分三类：事实性阻塞必须人（选文、全文缺失/坏 PDF 这类机器无权代答的），风险升级按规则触发（低置信度且被多结论引用、跨模态自动复核分歧、成本超阈值），其余一律自动并留 trace。介入时机 = "影响面大 × 自动判据弱"才升级，且永远停在幂等断点上，人解决事件后从同一节点续跑，已完成单元零重算。

**Q：怎么避免把人拖进低价值劳动？**
> 复核是决策台不是队列：按决策价值排序先审高危段；每个动作先回显影响（排除 → 几条结论降级），让决定可预见；批量动作 + 会话结束给量化摘要（改动 N 条结论、M 降级、K 确认）。人审的产出通过"重新生成"真正改写报告，形成闭环。

**Q：重启续跑和人审怎么结合？**
> 续跑有两层：检索是 LangGraph checkpoint，分析是 DB 幂等断点（input_hash/进度计数）。人审解决事件 = 把项目从 waiting 恢复到原节点重新入队，两条续跑机制自然拾起——"重启续跑"只是"人审后续跑"的一个特例。

**Q：长任务成本失控怎么办？**
> 每次 LLM 调用都记 token/视觉/耗时台账，检查点（逐篇/图表批次/专家批量/合成前）累计超阈值即暂停并默认继续，可一次性跳过剩余图表或中止；中止保留中间产物可安全重试。成本叙事从"延迟佐证"变成"我能看见并控制跑量"。

---

## 6. 风险、兼容与"不做"清单

**兼容与迁移**
- `evidence_reviews` 加列用 note 前缀回填 `source`，旧行不丢。
- 旧 `action()` 分支在 UI 全量切换前保持可用；open 事件存在时旧动作若与事件冲突，走事件语义并记 trace。
- V13 一次建齐三张/列改动（hitl_events、evidence_reviews 加列、llm_usage），保持 `SCHEMA_VERSION` 递增与 `database.py initialize()` 的迁移链风格。

**主要风险与对策**
- 状态机耦合：事件状态与项目状态可能不一致 → `wait_for_human` 单事务写入 + 启动守卫 + 迁移自检（孤儿 open 事件无项目 waiting 时告警）。
- 事件风暴/互斥事件：同一项目同时多个 open 事件 → 目录内定义互斥规则（同型事件先 supersede 旧的），UI 只呈现最新有效事件。
- "跳过剩余图表"导致下游证据缺失 → 该策略写进 run 上下文与报告 warnings，绝不静默；重跑时策略可清除。
- 影响回显与报告不同步 → 派生计算统一走服务层函数，preview 与 commit 共用同一实现，杜绝两套逻辑漂移。

**不做（本期明确排除）**
- 不做多用户/审批流权限（单用户工具语境，事件表已留 `resolved_by` 扩展位）。
- 不做把复核状态实时改写已生成报告（用 stale 标记 + 一键重生成，保住"人审前后结论可 diff"的审计性）。
- 不做金额硬上限/强制停用（本地模型语境，成本口径是估算列 + 软性门槛）。

---

## 实施状态更新：M3「成本门槛暂停」已按本方案落地（2026-06）

见 `docs/P12_BUDGET_GATE_VALIDATION.md`。已落地内容与本文档的差异（诚实边界）：

**已实现（与方案三一致）**
- 计量：`OllamaProvider` 增 `usage_sink`，每次成功 HTTP 调用（text/vision，含结构化重试的真实消耗）写入 `llm_usage`（V15，append-only、随项目删除级联）；`execute_job` 为每次任务注入带 run 上下文的 sink。
- 阈值与检查点：settings 新增 `budget_gate_enabled/tokens/vision_calls/minutes`；检查点全部复用既有幂等边界——逐篇论文间、生成报告前（`services/workflow.py`）、每张图表边界（`documents/analysis.py`）、分析块之间（`PaperAnalyst._pause_or_raise`）。越阈值即在同一事务语义下打开 `budget_windows`（open+快照）并抛 `AnalysisPausedError(reason="budget_gate")` → worker 优雅置 `waiting/analysis_paused`，记 `projects.pause_reason` 与 trace，任务不算失败、已完成单元零重算。
- 人审续跑：暂停后 UI 显示成本门槛卡（累计 token / 视觉调用 / 运行时长 vs 阈值）；`resume_analysis`（及其他重新入队分析的动作）调用 `ack_budget_gate` 关闭门并**重新基线窗口**——同一 run 不再每步追问，每再花一个阈值块才再次暂停（软性监督点）。
- 手动暂停优先级不变：`pause_requested` 命中时返回 `reason="user"`，门禁用时手动暂停照常工作。

**与方案三的偏差（本期未做，勿在简历/面试中声称）**
- 未做事件化 HITL（`hitl_events` 表、`waiting_for_human` 派生状态、worker 启动守卫、动作分发器）——门用 `projects.pause_reason` + `budget_windows.open` 表达，未引入第六状态。
- 未做"宽限窗口到期默认继续"（grace timer）；当前必须人工点"继续"。
- 未做 `skip_remaining_visuals` / `abort_analysis` 两个可执行动作——中止等价于"不点继续，保持暂停"（中间产物全保留，随时可回来续跑）。

### M2（方案一）事件化最小版：已落地（2026-06，V16）

见 `docs/P11_HITL_EVENTS_VALIDATION.md`。选文/缺全文两个人等待点已事件化；`waiting_for_human` = `waiting + open hitl_event`（派生语义，未加第六顶层状态）；worker 守卫确保 open 事件存在时不执行任何任务（`hitl_gate_skipped`）；人工动作通过 `resolve_all` 消费事件后按既有 resume 机制续跑。与完整方案一的差距（事件卡 UI、动作分发器强迁移、superseded_by 链、证据复核/成本门槛事件化）未做。
