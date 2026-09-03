# ResearchPilot 分阶段开发路线图

> 本文是 `ResearchPilot_开发计划.md` 的执行版。原计划描述产品目标和总体架构，本文负责回答：下一步具体做什么、做到什么程度、如何验证、何时才能进入下一阶段。
>
> 已观察到但尚未解决的问题统一记录在 [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md)。

## 1. 执行原则

1. 按阶段门禁推进。当前阶段未通过验收，不提前堆叠下一阶段功能。
2. 每个功能必须同时包含实现、自动化测试、错误处理和最小文档。
3. 先完成纵向闭环，再提高单点能力。第一条闭环是“研究问题 → 检索词 → OpenAlex → 论文列表”。
4. 所有外部能力必须位于 Provider 或 MCP 边界后，业务节点不直接依赖第三方协议。
5. Agent 输出优先使用 Pydantic 结构化模型，关键结论必须能够关联 Evidence。
6. 每阶段结束记录基线指标，禁止在简历或 README 中填写未实测的数据。

## 2. 阶段总览

| 阶段 | 目标 | 核心交付物 | 退出条件 | 预计用时 |
|---|---|---|---|---|
| P0 | 工程与模型基础 | API、OllamaProvider、Schema、最小 Graph | 本地模型可被 Graph 稳定调用 | 2～3 天 |
| P1 | 文献搜索闭环 | OpenAlex Client、Literature MCP、去重与排序 | 固定课题可返回并保存论文 | 4～6 天 |
| P2 | 项目持久化与单 Agent 工作流 | SQLite、Repository、Checkpoint、项目 API | 搜索任务可重启、可查询 | 3～5 天 |
| P3 | 多 Agent 与 Skills | Coordinator、Literature Agent、Skill Loader | Trace 可证明 handoff 与按需加载 | 4～6 天 |
| P4 | PDF 文档工作区 | Document MCP、页面/结构/图片提取 | PDF 可解析且产物可追溯 | 5～7 天 |
| P5 | Evidence 与跨论文比较 | Evidence Store、Paper Summary、Comparison | 重要结论均带证据定位 | 5～7 天 |
| P6 | 实验设计与人工审批 | Hypothesis、Experiment Plan、HITL | 接受/修改/拒绝后可恢复 | 4～6 天 |
| P7 | Artifact 与前端闭环 | CSV、Markdown、Mermaid、Streamlit | 固定 Demo 可端到端完成 | 4～6 天 |
| P8 | 稳定性、安全与可观测性 | Resume、Retry、Sandbox、Trace Dashboard | 故障注入后不重复已完成工作 | 4～5 天 |
| P9 | Eval 与发布工程 | 20 条评测、README、演示材料 | 结果可复现并可公开展示 | 5～7 天 |

预计总工期约 7～10 周。阶段编号表示依赖顺序，不要求严格对应自然周。

---

## 3. P0：工程与模型基础

### 3.1 阶段目标

建立所有后续功能依赖的稳定运行骨架，证明 FastAPI、Ollama、Pydantic 和 LangGraph 能协同工作。

### 3.2 当前状态

- [x] 创建 Python 3.12 虚拟环境
- [x] 建立 `pyproject.toml` 和开发依赖
- [x] 配置 `.env` 和 `Settings`
- [x] 定义 `LLMProvider` 与 `OllamaProvider`
- [x] 验证 Qwen3 14B 普通及结构化输出
- [x] 创建 `GET /health`
- [x] 创建 `POST /models/test`
- [x] 定义 `ResearchRequest`
- [x] 定义 `ResearchState` 和初始状态工厂
- [x] 建立 LangGraph Hello World
- [x] 添加统一异常模型和 request ID
- [x] 补充本地启动说明

### 3.3 下一批任务

#### P0-T1：最小 Graph

实现节点：

```text
initialize
→ understand_request
→ finish
```

具体要求：

- `initialize` 接收 `ResearchRequest` 并创建完整状态。
- `understand_request` 调用 `OllamaProvider.structured_output()`。
- 模型输出至少包含规范化目标、主题词和时间范围。
- 节点只返回状态增量，不原地修改输入状态。
- 提供内存 Checkpointer，为 P2 的 SQLite Checkpointer 预留替换点。

#### P0-T2：最小 Graph API

- 增加 `POST /research/test`，仅用于运行最小 Graph。
- 返回 `project_id`、规范化目标、当前阶段和生成的主题词。
- LLM 失败映射为 503；输入错误保持 FastAPI 的 422。

#### P0-T3：基础工程约束

- 统一 API 错误响应：`code`、`message`、`retryable`、`request_id`。
- 增加日志配置，禁止打印完整论文正文和敏感环境变量。
- 在 README 中写明安装、激活环境、启动 Ollama、启动 API 和运行测试。

### 3.4 测试

- Unit：状态工厂、Graph 节点、模型输出校验。
- Integration：用 Stub Provider 运行完整 Graph。
- Live smoke：用 `qwen3:14b` 运行一次真实 Graph。
- Quality gate：`pytest`、`ruff check`、`pip check` 全部通过。

### 3.5 验收与退出条件

给定“调研 2024-2026 年 RGB-LWIR image registration”，系统生成结构化主题词并完成 Graph；同一 `thread_id` 可读取对应 checkpoint。满足后才能进入 P1。

---

## 4. P1：文献搜索最小闭环

**阶段状态：DONE。真实验收：46 条原始结果 → 19 条唯一记录 → 5 条入选结果；无 warning；端到端 286.3 秒。**

### 4.1 阶段目标

跑通“用户问题 → 检索式 → Literature MCP → OpenAlex → 去重 → 初排”的第一条业务闭环。

### 4.2 数据契约

先定义再实现客户端：

- `SearchPapersInput`：query、year_from、year_to、limit。
- `PaperMetadata`：source_id、title、authors、year、abstract、doi、venue、citation_count、open_access_url、source。
- `SearchResult`：query、papers、total_available、warnings。
- DOI 统一转为小写裸 DOI；无 DOI 时用 OpenAlex ID 作为稳定键。

### 4.3 任务顺序

#### P1-T1：OpenAlex Client

- 使用共享 `httpx.AsyncClient`。
- 实现查询参数映射和分页边界。
- 设置明确的 User-Agent、超时和最大重试次数。
- 区分超时、限流、上游 5xx、无结果和响应格式异常。
- 保留原始 source ID，但不把原始 API JSON 泄漏给上层。

#### P1-T2：Literature MCP

- 创建独立 server 和 schemas。
- 第一批仅暴露 `search_papers`、`get_paper_metadata`。
- MCP tool 返回稳定业务模型，不返回 Python 异常堆栈。
- 直接客户端测试与 MCP transport 测试各一套。

#### P1-T3：Query Generator

- 输入 ResearchRequest。
- 输出 3～5 个互补查询：核心概念、同义词、任务/方法组合。
- 每个查询包含目的说明，避免生成只有词序差异的重复查询。
- 对模型输出执行长度、数量和重复校验。

#### P1-T4：Deduplicator

去重优先级：

```text
normalized DOI
→ OpenAlex ID
→ normalized title + year
```

- 保存合并来源，不能静默丢失较完整字段。
- 为中英文标点、大小写、连续空格编写固定样例。

#### P1-T5：Basic Ranker

- 第一层：标题/摘要/关键词 overlap，结果可解释且可重复。
- 第二层：只对第一层 Top-N 调用 LLM relevance score。
- LLM 输出 score、reason、matched_aspects。
- 最终分数公式写入配置并记录各分量，方便后续 Eval。

#### P1-T6：搜索 Graph

```text
understand_request
→ generate_queries
→ search_papers
→ deduplicate
→ rank_papers
→ finish
```

- 单个查询失败不应导致所有结果丢失。
- 所有查询失败时才将任务标记为失败。
- 每个节点输出可序列化的状态增量。

### 4.4 测试与固定样例

- Mock OpenAlex：正常、空结果、429、500、超时、字段缺失。
- 去重数据集：至少 20 条人工构造记录，覆盖 DOI 与标题重复。
- Live smoke：固定 RGB-LWIR 课题，限制 20 条，避免测试污染和长时间调用。
- 记录：返回数量、重复率、空摘要比例、端到端耗时。

### 4.5 阶段交付物

- `app/literature/` 业务模块。
- `mcp_servers/literature/` MCP Server。
- Query、Paper、Ranking schemas。
- 第一份可复现搜索结果 JSON。

### 4.6 验收与退出条件

固定课题生成至少 3 个非重复查询，成功调用 MCP，论文按稳定键去重并排序；上游局部失败会产生 warning 而不是丢失所有结果。

---

## 5. P2：项目持久化与可恢复的单 Agent 工作流

**阶段状态：DONE。验收：SQLite migration/Repository、项目 API、幂等执行和跨连接 checkpoint 恢复均有自动化测试覆盖。**

### 5.1 阶段目标

将 P1 的内存 Demo 变成可创建、查询、重启和继续执行的项目。

### 5.2 任务顺序

1. 建立 SQLite connection/session 管理和 migration 方案。
2. 创建 `projects`、`papers`、`traces` 表及唯一约束。
3. 实现 Repository 层，Graph 节点禁止直接拼 SQL。
4. 写入项目、候选论文、排名分量和选择原因。
5. 将内存 Checkpointer 换为 SQLite Checkpointer。
6. 实现 API：
   - `POST /projects`
   - `GET /projects/{id}`
   - `POST /projects/{id}/research`
   - `GET /projects/{id}/papers`
   - `GET /projects/{id}/trace`
7. 定义项目状态机：created、running、waiting、completed、failed。
8. 对重复启动、未知项目、运行中项目再次启动给出确定行为。

### 5.3 数据一致性要求

- Project 与 Paper 写入处于明确事务边界。
- `(project_id, stable_paper_key)` 唯一。
- API 返回路径不包含机器绝对路径。
- 时间统一以 UTC 存储，API 使用 ISO 8601。
- DB schema 变化必须有 migration，禁止手工修改已有数据库。

### 5.4 验收与退出条件

关闭并重启 API 后，项目、论文和 Graph checkpoint 仍存在；重复执行不会生成重复论文记录。

---

## 6. P3：多 Agent 编排与 Skills

**阶段状态：DONE。验收：结构化 Coordinator handoff、Literature Researcher、两个明确 unsupported 的后续 Specialist、受限按需 Skill Loader 与完整 handoff/tool trace 均有自动化测试覆盖。**

### 6.1 阶段目标

在已有单 Agent 闭环上引入职责隔离，而不是为了展示数量提前制造复杂度。

### 6.2 任务顺序

1. 定义统一 `AgentTask`、`AgentResult`、`Handoff` schema。
2. 实现 Coordinator，只负责规划、路由、状态和失败策略。
3. 将 P1 搜索能力迁入 Literature Researcher。
4. 创建 Multimodal Analyst 和 Research Builder 空能力边界，未实现任务必须明确返回 unsupported。
5. 实现 Skill Registry：启动只读取 name、description、path。
6. 实现 `load_skill(name)`：校验白名单并按需读取完整内容。
7. 编写 `systematic-search` 和 `paper-screening` 两个 Skill。
8. 增加 handoff、skill_load、tool_call Trace 事件。

### 6.3 上下文约束

- Coordinator 不读取完整论文正文。
- Literature Agent 只获得研究请求、搜索状态和相关 Skill。
- Handoff 传结构化摘要，不传全部消息历史。
- 每次 Skill 加载必须写入 Trace，便于比较 With/Without Skills。

### 6.4 验收与退出条件

Trace 能清楚显示 Coordinator → Literature Researcher → MCP → Coordinator；搜索流程确实按需加载 Skill，且未加载无关 Skills。

---

## 7. P4：PDF 文档工作区

**阶段状态：DONE。验收：5 类边界版式自动化测试与 5 篇真实开放论文 smoke 均通过；66/66 页文本和截图成功，零页面 warning。Raster Figure、caption、Table candidate 和 5 个 Document MCP tools 已覆盖；OCR/矢量独立提取限制见 `P4_VALIDATION.md`。**

### 7.1 阶段目标

先把 PDF 的文字、页面、结构和图片可靠落盘，不追求第一版解决所有学术排版问题。

### 7.2 任务顺序

1. Workspace Manager：创建项目目录并校验相对路径。
2. `resolve_safe_path()`：拒绝 `..`、绝对路径和 workspace 越界。
3. Document schema：Document、Page、Section、Figure、TableCandidate。
4. PyMuPDF Parser：元数据、逐页文本、页面尺寸和页截图。
5. 基础章节识别：标题模式、字号与文本规则组合。
6. Embedded image 提取：过滤极小装饰图片和重复资源。
7. Caption matching：根据页面位置和 Figure/Fig. 模式匹配。
8. Figure 分类：先用 caption keyword 分为 architecture、result、ablation、other。
9. Document MCP tools：`parse_document`、`get_page`、`get_document_structure`、`extract_figures`、`get_figure`。
10. DOCX/PPTX/XLSX 仅在 PDF 验收后作为子阶段加入。

### 7.3 测试语料

- 准备 5 篇可合法用于测试的开放论文。
- 覆盖双栏、跨页、无嵌入图、矢量图、扫描页等情况。
- 人工标注页数、Figure 数量和 caption，形成最小 gold set。

### 7.4 验收与退出条件

5 篇样例均能显示页面文本与截图；Figure 产物包含 paper、page、label、caption 和 source path；失败页面不会阻断整篇文档的其余页面。

---

## 8. P5：Evidence 与跨论文分析

**阶段状态：DONE。验收：migration v2、Evidence/Document/Summary Repository、text/figure/table Builder、source hash 回读、Context Compaction、跨论文比较与查询 API 均通过自动化测试；固定 5 篇、25 条 Gold claim 的 locator 与 support rate 为 100%。自动 LLM 抽取 precision 未在本阶段宣称，详见 `P5_VALIDATION.md`。**

### 8.1 阶段目标

让系统的主要结论从“模型说了什么”升级为“结论由哪些可定位证据支持”。

### 8.2 任务顺序

1. 将 `EvidenceNode` 定义为 Pydantic 模型并版本化。
2. 建立 `evidence` 表及 Evidence Repository。
3. 定义 Paper Summary：method、dataset、metrics、contribution、limitation、evidence_ids。
4. 实现 text evidence：页码、章节、原文片段哈希和 claim。
5. 实现 figure/table evidence：label、caption、page、artifact path。
6. Evidence Builder 校验 source 存在、定位合法、paper_id 一致。
7. 实现 Context Compaction：原文 → summary + evidence index。
8. 实现跨论文比较表：方法、数据集、损失、指标、局限。
9. 每个比较单元保留 supporting evidence IDs。
10. 实现基于 Evidence ID 回读原文或图片的核验路径。

### 8.3 质量规则

- 无 Evidence 的内容只能标为 inference 或 suggestion，不能伪装成论文事实。
- `confidence` 表示提取置信度，不表示论文结论正确性。
- 原文片段只保存必要长度；报告优先引用定位而不是复制长文本。
- Evidence 删除或重建时必须处理引用完整性。

### 8.4 验收与退出条件

固定 5 篇论文生成比较表；所有关键方法、数据集、指标和局限结论至少关联一个有效 Evidence，并可回到具体页/章节/图/表。

---

## 9. P6：实验设计与 Human-in-the-loop

**阶段状态：DONE。验收：Evidence-grounded proposal、experiment-design Skill、假设/实验/消融校验、SQLite 可恢复 interrupt、Accept/Modify/Reject、version 冲突与审批历史均通过自动化测试。真实 Qwen 严格 schema 在 238.5 秒成功，默认超时据此调整为 300 秒；详见 `P6_VALIDATION.md`。**

### 9.1 阶段目标

基于已验证 Evidence 生成可执行、可否证的实验计划，并在高价值节点等待用户决定。

### 9.2 任务顺序

1. 定义 Hypothesis、Experiment、Ablation、EvaluationPlan schemas。
2. 编写 `experiment-design` Skill。
3. Builder 只接收研究目标、候选 baseline、Evidence 索引和用户约束。
4. 每个 Hypothesis 必须包含 evidence_ids 和 confidence。
5. 每个 Experiment 必须包含 baseline、modification、controls、metrics、success criterion、failure criterion。
6. 生成重复项检测，避免把同一实验换名称重复输出。
7. 在 `human_approval` 节点 interrupt。
8. 实现 Accept、Modify、Reject 的 API 和状态转换。
9. Modify 必须保存用户修改内容和新旧版本。
10. Resume 后只继续审批后的节点。

### 9.3 验收与退出条件

接受、修改、拒绝三条路径均有自动化测试；中断后重启服务仍能继续；实验建议缺少 Evidence 或 failure criterion 时模型输出校验失败并触发受控重试。

---

## 10. P7：Artifact 与前端闭环

**阶段状态：DONE。验收：versioned Artifact Repository、Markdown/CSV/Mermaid 生成、Artifact MCP/API，以及仅调用 FastAPI 的五页签 Streamlit UI 均有自动化或启动级检查覆盖；详见 `P7_VALIDATION.md`。**

### 10.1 Artifact MCP

按以下顺序实现：

1. `create_markdown`
2. `export_csv`
3. `create_mermaid`
4. `save_artifact`
5. 后续再加入受控 `run_python` 和 chart

所有工具只能写入当前项目的 `artifacts/`，同名覆盖必须显式允许或自动版本化。

### 10.2 Streamlit 页面顺序

1. New Research：创建并启动项目。
2. Research Progress：轮询状态、节点和错误。
3. Evidence Workspace：论文、证据、图片三栏联动。
4. Experiment Designer：展示证据并执行审批。
5. Artifacts：下载 CSV、Markdown 和 Mermaid。

UI 不直接访问 SQLite 或 Ollama，只调用 FastAPI。

### 10.3 验收与退出条件

从 UI 提交固定课题后，可看到论文、Evidence、实验审批和最终 Artifact；刷新页面不会丢失项目状态。

---

## 11. P8：稳定性、安全与可观测性

**阶段状态：DONE。验收：item-level 恢复、确定性故障注入、安全矩阵、端到端 trace 关联、指标 API 与 Dashboard 全部通过；详见 `P8_VALIDATION.md`。**

### 11.1 故障恢复

- 为 OpenAlex 429、Ollama 超时、单篇 PDF 失败设计故障注入测试。
- 明确每个节点是否幂等、最大重试次数和退避策略。
- 论文处理记录 item-level 状态，从失败论文继续而非重跑全部。
- 提供 Retry Node 与 Resume API，保留原始错误。

### 11.2 安全

- 对所有文件入口执行 MIME、扩展名、大小和路径校验。
- 禁止 Agent 直接执行 shell。
- Artifact Python Runner 延后到有独立限制后再启用。
- 日志脱敏，不记录 `.env`、完整 prompt 或未裁剪文档内容。

### 11.3 Trace

统一事件字段：trace_id、project_id、agent、node、tool、input_summary、output_summary、latency_ms、success、error、created_at。

关键指标：

- Task Success Rate
- Tool Call Success Rate
- Invalid Tool Call Rate
- Average Tool Calls
- Duplicate Search Rate
- Node Latency
- Recovery Rate

### 11.4 验收与退出条件

故意让第 5 篇论文失败，修复条件后 Resume 必须从第 5 篇继续；Trace 可证明前 4 篇未重复处理。

---

## 12. P9：Eval、文档与发布

### 12.1 ResearchPilot-Eval

建立版本化数据集：

- Literature Search ×5
- Figure Extraction ×5
- Evidence Grounding ×5
- Experiment Planning ×5

每条任务包含输入、固定依赖版本、人工参考、评分方法和允许误差。

### 12.2 最少完成的对照实验

优先选择：

1. Without Skills vs With Skills
2. Full Context vs Compacted Context

如果时间允许再做 Single Agent vs 4-Agent。对照实验必须保持任务集、模型参数和外部数据快照一致。

### 12.3 发布材料

- README：定位、架构、Quick Start、Demo、限制和 Eval 结果。
- Architecture Diagram：Agent、MCP、State、Evidence、Artifact 边界。
- 固定 Demo 脚本与结果快照。
- 截图、GIF 或视频。
- 已知问题和 Roadmap。
- 锁定依赖版本，验证全新环境安装。

### 12.4 最终退出条件

新机器按 README 可启动；固定 Demo 可完成；20 条 Eval 可重复运行；README 中的所有数字均来自保存的评测结果。

---

## 13. 每个任务的 Definition of Done

一个任务只有同时满足以下条件才能勾选完成：

- 代码已实现，并遵守现有模块边界。
- 正常路径、关键错误路径均有测试。
- `pytest`、`ruff check`、`pip check` 通过。
- 外部 API 不依赖真实网络完成日常单元测试。
- 至少进行一次必要的真实 smoke test。
- 新配置已加入 `.env.example`，且没有提交秘密信息。
- 新接口已出现在 OpenAPI，并有请求/响应示例。
- 新状态可序列化、可 checkpoint，不含客户端或文件句柄。
- 相关 README 或阶段文档已更新。

## 14. 当前推荐执行顺序

从当前代码状态开始，最近的 8 个可执行任务是：

1. 实现最小 LangGraph 的 `initialize → understand_request → finish`。
2. 为 Graph 注入 `LLMProvider`，避免节点内部硬编码 Ollama。
3. 添加 Graph 单元测试与真实 Qwen smoke test。
4. 增加 `POST /research/test`。
5. 定义 `PaperMetadata`、`SearchPapersInput`、`SearchResult`。
6. 实现 OpenAlex Client 和 mock 测试。
7. 实现 Literature MCP 的 `search_papers`。
8. 把检索节点接入 Graph，形成第一条纵向闭环。

完成第 8 项后暂停扩功能，先按 P1 验收场景跑一次并记录结果，再决定是否进入持久化阶段。

---

## 15. 任务卡规范

从现在开始，每项开发工作按以下模板执行。任务编号同时用于 Issue、分支名、Trace 测试数据和阶段验收记录。

```text
任务编号：P{阶段}-T{序号}
目标：一句话说明可观察结果
依赖：必须已经完成的任务
输入：本任务消费的数据或接口
输出：本任务产生的数据、接口或文件
目标文件：预期新增或修改的模块
实现步骤：按编码先后排序
测试：正常、边界、错误和真实 smoke
完成证据：测试输出、示例产物或截图
工时：实现与测试总计
```

任务状态统一使用：

```text
BACKLOG → READY → IN_PROGRESS → REVIEW → DONE
                         ↘ BLOCKED
```

状态规则：

- `READY`：依赖已完成，输入和验收标准明确。
- `IN_PROGRESS`：同一时间只保留一个主要任务，避免多个半成品。
- `REVIEW`：代码已完成，等待质量门禁和真实 smoke。
- `DONE`：符合第 13 节 Definition of Done，并保存完成证据。
- `BLOCKED`：记录阻塞原因、已尝试方法和解除条件。

---

## 16. P0 详细任务卡：工程与模型基础

### P0-T1：最小 LangGraph

**状态：DONE；预计工时：4～6 小时。**

依赖：`ResearchRequest`、`ResearchState`、`LLMProvider` 已完成。

输入：

```json
{
  "research_question": "调研 2024-2026 年 RGB-LWIR image registration",
  "keywords": ["RGB-LWIR", "registration"],
  "year_from": 2024,
  "year_to": 2026,
  "maximum_papers": 15
}
```

新增结构化模型：

```python
class ResearchUnderstanding(BaseModel):
    normalized_goal: str
    core_concepts: list[str]
    domain: str
    year_from: int | None
    year_to: int | None
    ambiguities: list[str]
```

目标文件：

```text
app/graph/
├── nodes.py
├── workflow.py
└── state.py
app/schemas/
└── planning.py
tests/unit/graph/
├── test_nodes.py
└── test_workflow.py
```

实现步骤：

1. 扩展状态，加入 `request`、`understanding` 和 `current_stage`。
2. 定义 `GraphDependencies`，至少持有 `LLMProvider`，不允许节点自行实例化 Ollama。
3. 实现 `understand_request`，组装最小 system/user messages。
4. 调用 `structured_output(..., ResearchUnderstanding)`。
5. 校验模型不得改变用户明确给出的年份范围。
6. 节点返回 `{understanding, current_stage}` 增量。
7. 使用 `StateGraph(ResearchState)` 连接 START、节点和 END。
8. 提供 `build_research_graph(dependencies, checkpointer=None)` 工厂。

必须测试：

- 正常请求产生结构化 understanding。
- 没有年份时允许输出 `None`。
- 用户年份与模型年份冲突时以用户输入为准。
- Provider 抛出 `LLMError` 时节点不吞异常。
- 两次 Graph 调用的 list/dict 状态互不污染。

完成证据：`pytest tests/unit/graph -q` 输出和一次真实 Qwen JSON 结果。

### P0-T2：Graph API

**状态：DONE；依赖：P0-T1；预计工时：3～4 小时。**

接口：

```text
POST /research/test
Content-Type: application/json
```

成功响应：

```json
{
  "project_id": "uuid",
  "status": "completed",
  "current_stage": "request_understood",
  "understanding": {
    "normalized_goal": "...",
    "core_concepts": ["..."],
    "domain": "computer vision",
    "year_from": 2024,
    "year_to": 2026,
    "ambiguities": []
  }
}
```

错误响应：

- 422：请求字段校验失败。
- 503：Ollama 超时、连接失败或输出无法校验。
- 500：未分类内部异常，响应不得包含堆栈和绝对路径。

目标文件：`app/api/routes/research.py`、`app/api/schemas/errors.py`、`tests/api/test_research.py`。

必须测试：200、422、503；OpenAPI 中存在请求与响应 schema；Provider 使用 dependency override 替换。

### P0-T3：Request ID 与统一错误

**状态：DONE；依赖：P0-T2；预计工时：2～3 小时。**

统一响应：

```json
{
  "error": {
    "code": "MODEL_UNAVAILABLE",
    "message": "Ollama request timed out",
    "retryable": true,
    "request_id": "..."
  }
}
```

实现：HTTP middleware 接收或生成 `X-Request-ID`；响应头原样返回；日志、Trace、错误响应使用同一 ID。

测试：客户端提供 ID、服务端生成 ID、并发请求 ID 不串线、错误响应不泄漏 prompt。

### P0-T4：开发者快速启动文档

**状态：DONE；依赖：P0-T1～T3；预计工时：1～2 小时。**

README 必须包含：Python 版本、虚拟环境命令、依赖安装、`.env`、`ollama list`、API 启动、测试命令、curl 示例、常见故障。

P0 最终验收命令：

```powershell
python -m pytest -q
python -m ruff check .
python -m pip check
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

---

## 17. P1 详细任务卡：文献搜索闭环

### P1-T1：论文数据模型

**状态：DONE；依赖：P0 完成；预计工时：3 小时。**

目标文件：`app/schemas/literature.py`。

字段约束：

- `PaperAuthor`：name、orcid 可选。
- `PaperMetadata`：stable_id、source_id、title、authors、year、abstract、doi、venue、citation_count、open_access_url、source。
- `SearchPapersInput`：query 3～500 字符、limit 1～100、可选年份。
- `SearchResult`：query、papers、total_available、warnings、source_latency_ms。
- URL 使用 Pydantic URL 类型或输出时转字符串，保证 checkpoint 可序列化。

测试至少覆盖字段缺失、负 citation、反向年份、空 title、非法 URL、JSON round trip。

### P1-T2：OpenAlex Client

**状态：DONE；依赖：P1-T1；预计工时：6～8 小时。**

目标文件：

```text
app/literature/
├── __init__.py
├── base.py
├── errors.py
├── normalize.py
└── openalex.py
```

公开方法：

```python
async def search_papers(query, year_from=None, year_to=None, limit=20) -> SearchResult
async def get_paper_metadata(identifier: str) -> PaperMetadata
async def close() -> None
```

实现细节：

1. Base URL 默认 `https://api.openalex.org`，通过 Settings 覆盖。
2. 若配置 `OPENALEX_EMAIL`，加入 polite pool 参数。
3. 将年份转为 OpenAlex filter；limit 映射 `per-page`。
4. reconstruct inverted abstract index，并处理 `None`。
5. 作者只保留展示名和 ORCID，不保存完整机构对象。
6. OA URL 优先 `best_oa_location.pdf_url`，再用 landing page。
7. 429 读取 `Retry-After`；只重试 429、502、503、504 和连接错误。
8. 最大尝试次数默认 3；指数退避参数放入 Settings。
9. 错误映射为 `LiteratureRateLimitError`、`LiteratureUnavailableError`、`PaperNotFoundError`、`LiteratureResponseError`。

Mock 测试矩阵：

| 场景 | 预期 |
|---|---|
| 正常 2 条 | 映射为 2 个 PaperMetadata |
| abstract 为 null | abstract 为 None，不报错 |
| 缺 authorship | authors 为空列表 |
| 429 后成功 | 按上限重试并成功 |
| 连续 500 | 抛可重试 unavailable |
| 404 metadata | 抛 PaperNotFoundError |
| 非 JSON | 抛 LiteratureResponseError |
| 请求超时 | 抛 LiteratureUnavailableError |

### P1-T3：Literature MCP Server

**状态：DONE；依赖：P1-T2；预计工时：5～7 小时。**

目标文件：

```text
mcp_servers/literature/
├── __init__.py
├── server.py
└── schemas.py
tests/integration/mcp/
└── test_literature_server.py
```

工具契约：

```text
search_papers(query, year_from?, year_to?, limit=20)
get_paper_metadata(identifier)
```

约束：MCP 层只负责协议适配、输入校验和错误翻译；不得复制 OpenAlex 映射逻辑。服务启动和关闭时管理共享客户端。测试应通过 MCP Client 调用，而不只是直接调用 Python 函数。

### P1-T4：Query Generator

**状态：DONE；依赖：P1-T1；可与 P1-T2 并行；预计工时：5 小时。**

输出模型：

```python
class SearchQuery(BaseModel):
    query: str
    purpose: Literal["core", "synonym", "method", "application"]
    concepts: list[str]

class SearchQueryPlan(BaseModel):
    queries: list[SearchQuery]  # 3～5 条
```

确定性后处理：trim、casefold 去重、Jaccard 高相似去重、补入用户显式关键词、保留英文搜索表达。模型失败时允许基于关键词的 fallback 查询，但必须写 warning。

### P1-T5：论文去重

**状态：DONE；依赖：P1-T1；预计工时：4 小时。**

目标文件：`app/literature/deduplication.py`。

实现纯函数，输出 `DeduplicationResult(unique_papers, duplicate_groups, merge_log)`。Title normalization 只用于匹配，不覆盖展示 title。合并时选择较完整 abstract、非空 DOI、较大 citation count，并合并来源查询。

关键测试：DOI URL 与裸 DOI、尾部标点、Unicode 大小写、标题多空格、同标题不同年份、无 DOI 同 OpenAlex ID。

### P1-T6：两阶段排序

**状态：DONE；依赖：P1-T4、P1-T5；预计工时：6～8 小时。**

第一层分数建议先固定：

```text
lexical_score =
0.50 × title_overlap
+ 0.30 × abstract_overlap
+ 0.15 × keyword_coverage
+ 0.05 × recency_component
```

只把 lexical Top-30 交给 LLM，每批最多 10 篇。LLM 返回 0～100 relevance、reason 和 matched_aspects。最终分数第一版使用 `0.45 × lexical + 0.55 × llm`，权重进入配置。

测试：同输入排序稳定；LLM 单批失败时回退 lexical；模型分数越界被拒绝；无 abstract 仍可排序。

### P1-T7：搜索 Graph 节点

**状态：DONE；依赖：P1-T3～T6；预计工时：6 小时。**

每个节点的输入输出：

| 节点 | 读取 | 写入 |
|---|---|---|
| generate_queries | request、understanding | search_queries |
| search_papers | search_queries | candidate_papers、warnings |
| deduplicate_papers | candidate_papers | unique candidates、dedup stats |
| rank_papers | candidates、request | ranked candidates |
| select_papers | ranked、maximum_papers | selected_papers |

不得在节点间传 OpenAlex client 或 MCP session；只传可 checkpoint 的业务数据。

### P1-T8：P1 固定验收

**状态：DONE。验收快照：`evals/snapshots/p1_rgb_lwir_search.json`。**

验收输入固定为：

```text
调研 2024-2026 年 RGB-LWIR image registration 研究，最多返回 10 篇。
```

保存到 `evals/snapshots/p1_rgb_lwir_search.json`：请求、生成查询、调用时间、候选数、去重数、Top-10、每篇分数分量和 warning。退出门禁为 MCP 调用成功、零重复 stable ID、年份满足约束、每篇有 selection reason。

---

## 18. P2～P3 详细任务清单：持久化、Agent 与 Skills

### P2：持久化任务分解

| 编号 | 任务 | 关键输出 | 测试重点 | 工时 |
|---|---|---|---|---|
| P2-T1 | SQLite 配置与连接 | DB engine/session 生命周期 | 临时 DB、并发连接、关闭 | 3h |
| P2-T2 | Migration 基线 | projects/papers/traces 表 | 空库升级、重复升级 | 4h |
| P2-T3 | Project Repository | create/get/update_status | 未知 ID、状态转换 | 4h |
| P2-T4 | Paper Repository | upsert/list/rank update | 唯一键、事务回滚 | 5h |
| P2-T5 | Trace Repository | append/list/filter | 大字段裁剪、顺序 | 3h |
| P2-T6 | SQLite Checkpointer | thread_id/project_id 映射 | 重启恢复、并发 thread | 5h |
| P2-T7 | Projects API | 5 个项目/研究接口 | 404、409、分页 | 6h |
| P2-T8 | 幂等搜索执行 | run_id 和节点写入边界 | 重复 POST 不重复数据 | 5h |

数据库细化：

```text
projects
- id TEXT PK
- name TEXT NOT NULL
- goal TEXT NOT NULL
- request_json TEXT NOT NULL
- status TEXT NOT NULL
- current_stage TEXT NOT NULL
- created_at TEXT NOT NULL
- updated_at TEXT NOT NULL
- version INTEGER NOT NULL

papers
- id TEXT PK
- project_id TEXT FK
- stable_key TEXT NOT NULL
- metadata_json TEXT NOT NULL
- relevance_score REAL
- selection_reason TEXT
- created_at TEXT NOT NULL
- UNIQUE(project_id, stable_key)

traces
- id TEXT PK
- trace_id TEXT NOT NULL
- project_id TEXT NOT NULL
- event_type TEXT NOT NULL
- agent TEXT
- node TEXT
- tool TEXT
- success INTEGER NOT NULL
- latency_ms INTEGER
- summary_json TEXT
- error_json TEXT
- created_at TEXT NOT NULL
```

Repository 规则：API/Agent/Graph 只依赖 Repository protocol；测试使用临时 SQLite；禁止在 route 中提交事务。

### P3：Agent 与 Skills 任务分解

| 编号 | 任务 | 交付物 | 明确不做 | 工时 |
|---|---|---|---|---|
| P3-T1 | Agent/Handoff schemas | AgentTask、AgentResult、Handoff | 自由文本路由 | 3h |
| P3-T2 | Coordinator | plan、route、merge result | 搜索与 PDF 解析 | 6h |
| P3-T3 | Literature Researcher | 封装 P1 工作流 | Evidence 分析 | 5h |
| P3-T4 | Specialist stubs | 能力声明、unsupported | 假实现结果 | 2h |
| P3-T5 | Skill metadata schema | name、description、version、path | 启动读全文 | 3h |
| P3-T6 | Skill Registry/Loader | list/load/cache | 任意路径加载 | 5h |
| P3-T7 | 两个 SKILL.md | search、screening | 一次写完 9 个 | 5h |
| P3-T8 | Handoff Trace | agent/skill/tool 事件 | 保存完整 prompt | 4h |

Skill 文件最低结构：目的、适用条件、不适用条件、输入、步骤、输出 schema、质量检查、失败处理。Loader 必须限制在配置的 skills root，拒绝路径穿越、未知名称和过大文件。

P3 验收 Trace 示例：

```text
Coordinator.plan
Coordinator.handoff → LiteratureResearcher
SkillRegistry.load → systematic-search
LiteratureResearcher.tool → Literature.search_papers
SkillRegistry.load → paper-screening
LiteratureResearcher.return → Coordinator
```

---

## 19. P4～P5 详细任务清单：文档与 Evidence

### P4：文档处理任务分解

| 编号 | 任务 | 输出 | 验收要点 | 工时 |
|---|---|---|---|---|
| P4-T1 | Workspace Manager | 项目目录及 manifest | 路径不越界 | 5h |
| P4-T2 | Document schemas | Page/Section/Figure/TableCandidate | JSON round trip | 3h |
| P4-T3 | PDF metadata/pages | page text、size、rotation | 5 个样例不崩溃 | 6h |
| P4-T4 | Page rendering | PNG screenshot | DPI、旋转正确 | 4h |
| P4-T5 | Section heuristic | heading + page range | 输出可解释 confidence | 6h |
| P4-T6 | Embedded figures | image + bbox + page | 去除小图标和重复图 | 8h |
| P4-T7 | Caption matching | label/caption/distance | gold set 统计 recall | 8h |
| P4-T8 | Figure classification | 4 类 + reason | keyword 基线可复现 | 4h |
| P4-T9 | Document MCP | 5 个 PDF tools | MCP transport 测试 | 6h |
| P4-T10 | PDF 阶段报告 | 指标与失败样例 | 不隐瞒不支持布局 | 3h |

Workspace manifest 示例：

```json
{
  "project_id": "...",
  "documents": [{"document_id": "...", "sha256": "...", "relative_path": "papers/P001.pdf"}],
  "generated_at": "...",
  "schema_version": 1
}
```

文件导入流程必须是：验证大小 → 检查扩展名/MIME → 计算 SHA-256 → 分配 document_id → 复制到项目目录 → 写 manifest。解析器只读取 Workspace Manager 返回的安全路径。

### P5：Evidence 任务分解

| 编号 | 任务 | 输出 | 测试重点 | 工时 |
|---|---|---|---|---|
| P5-T1 | Evidence schema v1 | EvidenceNode | 类型、confidence、定位 | 4h |
| P5-T2 | Evidence DB/Repository | CRUD/list by paper | 引用完整性 | 5h |
| P5-T3 | Text Evidence Builder | claim ↔ page/section/span | span 核验 | 7h |
| P5-T4 | Figure Evidence Builder | claim ↔ figure | 文件/label 核验 | 5h |
| P5-T5 | Paper Summary | 6 类信息 + evidence IDs | 无证据字段拒绝 | 7h |
| P5-T6 | Context Compaction | summary + evidence index | token/字符统计 | 5h |
| P5-T7 | Comparison schema | rows/cells/evidence | 空值与冲突证据 | 4h |
| P5-T8 | Cross-paper synthesis | CSV/Markdown 数据对象 | 每格证据定位 | 8h |
| P5-T9 | Evidence lookup API | list/detail/source preview | 项目隔离、404 | 5h |
| P5-T10 | Gold set 评估 | precision/support rate | 人工复核 5 篇 | 6h |

Evidence 唯一性建议使用：`paper_id + evidence_type + page + label/span_hash + normalized_claim_hash`。同一原文可以支持多个 claim，但不得复制存储整页正文。

Evidence 校验器必须回答：

1. paper_id 是否属于当前项目？
2. source_path 是否存在且位于 workspace？
3. page 是否在文档范围内？
4. figure/table label 是否能在解析结果中找到？
5. text span hash 是否仍与原始解析结果一致？

---

## 20. P6～P9 详细任务清单：实验、产物、稳定性与发布

### P6：实验设计与审批

| 编号 | 任务 | 具体结果 | 工时 |
|---|---|---|---|
| P6-T1 | Hypothesis schema | statement、evidence、confidence、assumptions | 3h |
| P6-T2 | Experiment schema | baseline/modification/controls/metrics/criteria | 4h |
| P6-T3 | experiment-design Skill | 完整工作流和质量清单 | 5h |
| P6-T4 | Builder 节点 | Evidence-grounded proposal | 8h |
| P6-T5 | Ablation generator | 单变量消融矩阵 | 5h |
| P6-T6 | Proposal validator | 缺项/无证据/重复检测 | 5h |
| P6-T7 | LangGraph interrupt | pending_approval checkpoint | 5h |
| P6-T8 | Approval API | accept/modify/reject | 6h |
| P6-T9 | Resume branches | 三条路径与版本历史 | 6h |

审批 API 请求必须带 proposal version，防止用户基于旧版本覆盖新方案。Modify 不直接接受任意状态 JSON，只接受允许修改的实验字段。

### P7：Artifact 与 UI

| 编号 | 任务 | 具体结果 | 工时 |
|---|---|---|---|
| P7-T1 | Artifact schema/repository | type/path/hash/version | 4h |
| P7-T2 | Markdown tool | experiment_plan.md | 4h |
| P7-T3 | CSV tool | comparison/experiment_matrix.csv | 4h |
| P7-T4 | Mermaid tool | pipeline.mmd + 语法检查 | 5h |
| P7-T5 | Artifact MCP | 协议封装和路径安全 | 5h |
| P7-T6 | New Research page | 表单和创建任务 | 5h |
| P7-T7 | Progress page | 状态、Trace、错误 | 6h |
| P7-T8 | Evidence page | 三栏联动和定位 | 8h |
| P7-T9 | Experiment page | proposal 与审批 | 6h |
| P7-T10 | Artifacts page | 预览和下载 | 4h |

前端 API client 单独封装，统一处理超时、503、任务状态和重试；页面代码不得直接 import Repository。

### P8：恢复、安全与 Trace

**阶段状态：DONE。item-level progress、第 5 项故障恢复、安全矩阵、API/Graph/MCP trace 串联、过滤/指标与 Streamlit Dashboard 均通过验收；详见 `P8_RELIABILITY_AUDIT.md` 和 `P8_VALIDATION.md`。**

| 编号 | 任务 | 故障/约束 | 验收 |
|---|---|---|---|
| P8-T1 | 节点幂等审计 | 列出每个节点写操作 | 审计表完整 |
| P8-T2 | Retry policy | API/模型/解析分类 | 不重试输入错误 |
| P8-T3 | Item-level progress | 单篇状态 | 从失败项恢复 |
| P8-T4 | 故障注入 harness | 第 5 篇失败 | 前 4 篇不重复 |
| P8-T5 | 文件安全测试 | traversal/symlink/超大文件 | 全部拒绝 |
| P8-T6 | Trace middleware | API/Graph/MCP 关联 | trace_id 连通 |
| P8-T7 | 指标聚合 | latency/success/recovery | 可按项目查询 |
| P8-T8 | Dashboard | 时间线和失败过滤 | Demo 可读 |

### P9：评测与发布

| 编号 | 任务 | 产物 | 工时 |
|---|---|---|---|
| P9-T1 | Eval schema/runner | 可重复运行框架 | 6h |
| P9-T2 | Search 任务 ×5 | gold/relevance labels | 8h |
| P9-T3 | Figure 任务 ×5 | figure/type labels | 8h |
| P9-T4 | Evidence 任务 ×5 | supported claims | 8h |
| P9-T5 | Experiment 任务 ×5 | rubric 和双人复核 | 8h |
| P9-T6 | Skills 对照实验 | 原始结果与汇总 | 6h |
| P9-T7 | Context 对照实验 | token/质量/延迟 | 6h |
| P9-T8 | 全新环境安装测试 | 安装日志 | 4h |
| P9-T9 | README/架构图 | 发布文档 | 8h |
| P9-T10 | 固定 Demo | 脚本、截图、视频 | 8h |

Eval 每次运行保存：git commit、模型名、模型 digest、参数、数据集版本、开始时间、环境摘要、逐条结果和聚合指标。这样模型或代码升级后才能进行有效比较。

---

## 21. 阶段依赖、风险与变更控制

### 21.1 关键依赖链

```text
P0 Graph
  ↓
P1 Search Contract → OpenAlex → Literature MCP → Search Graph
  ↓
P2 Persistence/Checkpoint
  ↓
P3 Agent Handoff/Skills
  ↓
P4 Document Workspace
  ↓
P5 Evidence/Comparison
  ↓
P6 Experiment/HITL
  ↓
P7 Artifact/UI
  ↓
P8 Reliability
  ↓
P9 Eval/Release
```

允许并行但不得提前集成的工作：

- P1 OpenAlex Client 与 Query Generator。
- P4 Workspace Manager 与 Document schemas。
- P6 Schema/Skill 与审批 API schema 设计。
- P9 gold set 标注可在 P4/P5 开始后持续积累。

### 21.2 主要风险与触发条件

| 风险 | 早期信号 | 应对 | 禁止做法 |
|---|---|---|---|
| Qwen structured output 不稳定 | 校验重试率 >10% | 缩小 schema、明确 prompt、受控重试 | 静默接收坏 JSON |
| OpenAlex 限流 | 429 增多 | polite pool、缓存、退避 | 无限重试 |
| 搜索质量差 | Top-10 明显偏题 | 调整 query 与 ranker，保存分量 | 只提高 LLM 权重掩盖问题 |
| PDF 图片提取低召回 | gold set 漏图多 | 页面区域候选作为 fallback | 第一版投入重型 OCR |
| Evidence 幻觉 | source 无法定位 | 强校验、unsupported 标记 | 用 confidence 伪装证据 |
| State 过大 | checkpoint 明显变慢 | 只存 ID/summary/index | 存原始 PDF 或图片 bytes |
| 多 Agent 无收益 | tool call/延迟上升 | 用 Eval 决定是否保留拆分 | 为数量继续加 Agent |
| UI 拖慢核心 | 页面先于 API 定型 | 严格执行 P7 顺序 | UI 直连数据库 |

### 21.3 变更控制

出现新需求时先归类：

- 修复当前阶段验收缺口：立即进入当前阶段。
- 增强当前阶段但不影响闭环：加入该阶段 `BACKLOG`，验收后再评估。
- 属于后续阶段：记录到对应任务卡，不提前实现。
- V1.5/V2 能力：记录到原计划边界，不能挤占 MVP 关键链路。

任何 schema 变更必须检查四个影响面：API、checkpoint、SQLite、Eval snapshot。任何 MCP tool 变更必须同步更新 tool schema、客户端适配、集成测试和 Skill 说明。

### 21.4 每日开发节奏

建议每个开发日按以下节奏推进：

1. 选择一个 `READY` 任务，复核依赖和退出条件。
2. 先写数据契约与失败场景测试。
3. 完成最小实现，使单元测试通过。
4. 补边界和集成测试。
5. 执行质量门禁。
6. 必要时运行一次真实 smoke。
7. 更新任务状态、完成证据和下一任务。

每日结束至少留下一个可运行状态，不在主线上保留无法导入、无法迁移或无法启动的半成品。
> 历史路线图：其中列出的细分 REST 路由已由核心接口收敛版取代，不代表当前可用 API。
