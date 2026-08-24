# ResearchPilot 开发计划

> **执行路线图**：本文件保留产品目标与总体架构；具体任务顺序、阶段门禁、测试和验收标准见 [`research-pilot/docs/DEVELOPMENT_ROADMAP.md`](research-pilot/docs/DEVELOPMENT_ROADMAP.md)。

> **项目定位**：Skill-Driven Multimodal Deep Research Agent
>
> **目标用户**：硕士研究生 / 个人科研人员
>
> **核心目标**：围绕一个研究问题，完成“文献搜索 → 文献筛选 → 多模态论文分析 → 证据提取 → 跨论文比较 → 实验方案设计 → 科研图/科研产物生成”的完整 Agent 工作流。
>
> **开发原则**：4 个 Agent、MCP 真调用、Skills 动态加载、多模态证据可追溯、任务可恢复、全流程可评测；不追求大量 Agent，不做重型分布式基础设施。

---

## 1. 项目最终效果

用户输入：

```text
研究主题：UAV 场景下 RGB-LWIR 图像配准与融合
要求：
1. 搜索近三年相关论文；
2. 筛选最相关的 10~15 篇；
3. 比较方法、数据集、损失函数、指标和局限；
4. 提取其中的模型结构图、模块图和关键实验图；
5. 基于这些证据制定 baseline、改进模块、主实验和消融实验；
6. 生成一张实验流程图。
```

ResearchPilot 执行：

```text
Research Coordinator
        │
        ├── 规划任务与上下文
        │
        ▼
Literature Researcher
        │
        ├── 搜索 OpenAlex / Crossref / arXiv
        ├── 去重
        ├── 相关性筛选
        └── 保存论文元数据
        │
        ▼
Multimodal Evidence Analyst
        │
        ├── PDF / DOCX / PPTX / XLSX / Image 解析
        ├── 提取正文、表格、图片
        ├── 图片分类与理解
        └── 构建细粒度 Evidence
        │
        ▼
Research Builder
        │
        ├── 跨论文方法比较
        ├── Research Gap 分析
        ├── Hypothesis 设计
        ├── Experiment Plan
        └── 科研图生成
        │
        ▼
Research Package
```

最终产出：

```text
workspace/<project_id>/
├── project.json
├── research_state.json
├── sources/
├── papers/
├── documents/
├── figures/
├── tables/
├── evidence/
├── comparisons/
├── experiments/
├── artifacts/
└── traces/
```

---

# 2. 技术栈

## 2.1 第一版建议

| 层 | 技术 |
|---|---|
| Local LLM | Ollama + Qwen 14B |
| Agent Orchestration | LangGraph |
| Backend | FastAPI |
| MCP | FastMCP |
| UI | Streamlit |
| Database | SQLite |
| Agent State | LangGraph Checkpointer + SQLite |
| File Parsing | PyMuPDF、python-docx、python-pptx、openpyxl |
| Data Analysis | pandas、numpy、matplotlib |
| Literature API | OpenAlex、Crossref、arXiv |
| HTTP | httpx |
| Validation | Pydantic |
| Observability | 自研 Trace + LangSmith/Langfuse（二选一，后期） |
| Image Generation | 独立 Provider Adapter，先预留接口 |
| Container | Docker（Phase 2 后加入） |

第一版不要一开始就上 PostgreSQL、Redis、Celery、Kubernetes。

MVP 的目标是：

```text
单机
+ SQLite
+ Ollama
+ FastAPI
+ LangGraph
+ FastMCP
+ Streamlit
```

先把完整 Agent 闭环跑通。

---

# 3. 模型设计

## 3.1 当前本地模型

你已经部署：

```text
Ollama
└── Qwen 14B
```

统一通过环境变量配置模型名，不在代码中写死具体 tag：

```env
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=<你的Qwen14B模型名>
```

封装：

```python
class LLMProvider:
    async def chat(...): ...
    async def structured_output(...): ...
```

第一版：

```text
OllamaProvider
```

后续扩展：

```text
LLMProvider
├── OllamaProvider
├── OpenAIProvider
├── GeminiProvider
└── AnthropicProvider
```

这样简历可以体现 **model-provider abstraction**，而不是整个系统绑定单一模型。

---

## 3.2 Qwen 14B 主要负责

适合：

```text
Query 生成
摘要筛选
任务规划
结构化信息提取
论文之间的初步比较
工具选择
实验计划初稿
```

不要强行让本地 14B 承担：

```text
复杂科研图片理解
高难度跨 30 篇论文长上下文推理
高质量 publication-style 图片生成
```

这些能力设计成 Provider，可在后续接入 Vision / Strong Reasoning Model。

---

# 4. 四个 Agent 的职责

## 4.1 Research Coordinator Agent

### 职责

```text
理解研究目标
任务拆解
Agent 路由
Research State 管理
Context 构造
Handoff
失败重试策略
最终结果汇总
```

### 禁止

不直接：

```text
搜索论文
解析 PDF
提取图片
生成实验数据
```

它只负责 orchestration。

### 输入

```python
ResearchRequest
```

### 输出

```python
ResearchPlan
```

示例：

```json
{
  "goal": "研究 RGB-LWIR 配准方法",
  "tasks": [
    "literature_search",
    "paper_screening",
    "multimodal_analysis",
    "cross_paper_synthesis",
    "experiment_planning"
  ]
}
```

---

## 4.2 Literature Researcher Agent

### 职责

```text
生成检索式
调用 Literature MCP
去重
筛选
相关性排序
Related Work 扩展
保存 metadata
```

### 主要 Skill

```text
systematic-search
paper-screening
related-paper-discovery
```

### 主要 MCP

```text
Literature MCP
```

### 输出

```text
Selected Papers
+
Selection Reason
+
Metadata
```

---

## 4.3 Multimodal Evidence Analyst Agent

这是整个项目最重要的 Agent。

### 职责

```text
解析 PDF / DOCX / PPTX / XLSX / CSV / Image
识别章节
提取表格
提取图片
图片分类
图片描述
生成 Claim-Evidence 对应关系
```

### 主要 Skill

```text
figure-extraction
table-analysis
paper-structure-analysis
evidence-grounding
```

### 主要 MCP

```text
Document MCP
Artifact MCP
```

### 输出

```text
Evidence Nodes
Figures
Tables
Structured Paper Notes
```

---

## 4.4 Research Builder Agent

### 职责

```text
跨论文比较
方法聚类
Research Gap 整理
Hypothesis 生成
Experiment Plan
Ablation Plan
Evaluation Plan
科研流程图生成
```

### 主要 Skill

```text
method-comparison
experiment-design
ablation-design
scientific-diagram
```

### 强约束

任何实验建议必须包含：

```text
Hypothesis
Evidence
Control Variable
Metric
Expected Observation
Failure Criterion
```

---

# 5. Agent State 设计

不要只保存 `messages[]`。

定义统一状态：

```python
class ResearchState(TypedDict):
    project_id: str
    user_goal: str

    plan: dict
    current_stage: str

    search_queries: list[str]
    candidate_papers: list[dict]
    selected_papers: list[dict]

    evidence_ids: list[str]
    figure_ids: list[str]
    table_ids: list[str]

    hypotheses: list[dict]
    experiment_plan: dict | None

    pending_approval: dict | None
    errors: list[dict]
```

### State 原则

```text
Working Context != Persistent State != Evidence Store
```

三者分开。

---

# 6. Context Engineering

这是项目必须重点展示的部分。

## 6.1 Working Context

只包含当前 Agent 当前任务需要的内容。

例如 Figure 分析任务只给：

```text
Paper Metadata
Figure Image
Figure Caption
Relevant Paragraph
```

不要把整篇论文全部塞进去。

---

## 6.2 Research State

保存课题级信息：

```text
研究目标
已选论文
已接受 Hypothesis
已拒绝 Idea
Baseline
Dataset
Metrics
已完成工作
待完成工作
```

---

## 6.3 Context Builder

实现：

```python
build_context(agent_name, task, state)
```

例如 Experiment Designer：

```text
Research Goal
+
Selected Baseline
+
Top Relevant Evidence
+
Existing Experiments
+
User Constraints
```

而不是原始全部聊天历史。

---

## 6.4 Context Compaction

每处理完一篇论文：

```text
Raw Document
      ↓
Evidence Extraction
      ↓
Paper Summary Object
      ↓
Research Memory
```

之后跨论文分析优先读取：

```text
Paper Summary + Evidence Index
```

只有需要核实时再回读原始文件。

---

# 7. MCP 设计

第一版只做 3 个 MCP Server。

---

## 7.1 Literature MCP

建议自己实现。

目录：

```text
mcp_servers/literature/
├── server.py
├── openalex.py
├── crossref.py
├── arxiv.py
└── schemas.py
```

### Tools

#### search_papers

```python
search_papers(
    query: str,
    year_from: int | None,
    year_to: int | None,
    limit: int = 20
)
```

返回：

```text
paper_id
title
authors
year
abstract
doi
venue
citation_count
open_access_url
```

#### get_paper_metadata

```python
get_paper_metadata(identifier: str)
```

#### get_references

```python
get_references(paper_id: str)
```

#### get_citing_papers

```python
get_citing_papers(paper_id: str)
```

#### find_related_papers

```python
find_related_papers(paper_id: str, limit: int = 10)
```

#### download_open_access_pdf

```python
download_open_access_pdf(paper_id: str)
```

### MVP 要求

先实现：

```text
search_papers
get_paper_metadata
find_related_papers
```

下载 PDF 后做。

---

# 8. Document MCP

目录：

```text
mcp_servers/document/
├── server.py
├── pdf_parser.py
├── docx_parser.py
├── pptx_parser.py
├── xlsx_parser.py
├── image_parser.py
└── schemas.py
```

### Tools

```text
parse_document
get_document_structure
get_page
get_section
extract_figures
extract_tables
get_figure
get_table
```

---

## 8.1 PDF

使用：

```text
PyMuPDF
```

第一版完成：

```text
page text
embedded images
page screenshots
basic caption matching
```

不要第一版就追求复杂 scholarly PDF layout parser。

---

## 8.2 DOCX

使用：

```text
python-docx
```

支持：

```text
paragraph
heading
table
embedded images
```

---

## 8.3 PPTX

使用：

```text
python-pptx
```

支持：

```text
slide text
image
shape
notes（可选）
```

---

## 8.4 XLSX / CSV

使用：

```text
openpyxl
pandas
```

支持：

```text
sheet list
range preview
schema inference
summary statistics
```

---

## 8.5 Image

返回：

```text
path
size
metadata
vision_description（存在视觉模型时）
```

---

# 9. Artifact MCP

负责产生科研产物。

目录：

```text
mcp_servers/artifact/
├── server.py
├── python_runner.py
├── chart.py
├── diagram.py
└── export.py
```

### Tools

```text
run_python
create_chart
create_mermaid
create_markdown
export_csv
save_artifact
```

第一版不要让 Agent 任意 shell execution。

只允许：

```text
受控 Python
固定 workspace
限制运行时间
限制输出目录
```

---

# 10. Tool Namespace / Dynamic Tool Discovery

Agent 不要一开始看到全部 Tool Schema。

按 namespace：

```text
Literature
Document
Artifact
```

Coordinator 只知道：

```text
Literature Tools
Document Tools
Artifact Tools
```

任务确定后再加载具体 tools。

目标：

```text
减少 Tool Schema Token
减少错误工具选择
体现 Dynamic Tool Discovery
```

MVP 可以先手动 Router；V2 再实现真正 Tool Search。

---

# 11. Agent Skills

项目必须实现 Skills Layer。

目录：

```text
skills/
├── systematic-search/
│   └── SKILL.md
├── paper-screening/
│   └── SKILL.md
├── figure-extraction/
│   ├── SKILL.md
│   └── scripts/
├── table-analysis/
│   └── SKILL.md
├── evidence-grounding/
│   └── SKILL.md
├── method-comparison/
│   └── SKILL.md
├── experiment-design/
│   └── SKILL.md
├── ablation-design/
│   └── SKILL.md
└── scientific-diagram/
    └── SKILL.md
```

---

## 11.1 Skill Loader

第一阶段只实现：

```python
list_skills()
load_skill(name)
```

启动时仅加载：

```text
Skill Name
Skill Description
```

Agent 确定要使用时才读取完整 `SKILL.md`。

这就是 Progressive Disclosure。

---

## 11.2 MVP 优先实现 4 个 Skill

```text
systematic-search
paper-screening
figure-extraction
experiment-design
```

不要一开始把 9 个全部写完。

---

# 12. Evidence Graph

这是项目差异化核心。

不要只记录：

```text
答案来自 Paper A
```

而要记录：

```text
Claim
 ↓
Evidence
 ↓
Document Element
```

---

## 12.1 数据模型

```python
class EvidenceNode(BaseModel):
    evidence_id: str
    paper_id: str

    evidence_type: Literal[
        "text",
        "figure",
        "table"
    ]

    claim: str

    page: int | None
    section: str | None
    figure_label: str | None
    table_label: str | None

    source_path: str
    confidence: float
```

---

## 12.2 示例

```json
{
  "evidence_id": "EV_0012",
  "paper_id": "P_003",
  "evidence_type": "figure",
  "claim": "The model uses a cross-attention fusion module.",
  "page": 6,
  "section": "3.2",
  "figure_label": "Figure 3",
  "source_path": "papers/P_003.pdf",
  "confidence": 0.91
}
```

---

## 12.3 第一版存储

不需要 Neo4j。

直接：

```text
SQLite
+
JSON
```

即可。

后续如果需要真正 graph query，再升级。

---

# 13. Paper Figure Extraction

## 13.1 MVP 工作流

```text
PDF
 ↓
PyMuPDF
 ↓
Embedded Images / Page Regions
 ↓
Caption Matching
 ↓
Candidate Figures
 ↓
Classifier
 ↓
Architecture / Result / Ablation / Other
```

第一版可先通过：

```text
caption keywords
+
Qwen text reasoning
```

例如：

```text
framework
architecture
overview
pipeline
network
```

判断结构图。

后续有视觉模型后再真正 VLM 分类。

---

## 13.2 输出

```text
figures/
├── P001_Fig1_architecture.png
├── P003_Fig2_result.png
└── P008_Fig4_ablation.png
```

同时保存：

```json
{
  "paper_id": "P001",
  "figure": "Figure 1",
  "type": "architecture",
  "caption": "...",
  "page": 4
}
```

---

# 14. Experiment Planning

Experiment Designer 不允许直接输出“做几个实验试试”。

必须生成结构化对象。

## 14.1 Hypothesis

```json
{
  "id": "H1",
  "hypothesis": "Local deformation augmentation can improve robustness to spatial misalignment.",
  "evidence_ids": ["EV12", "EV19"],
  "confidence": 0.78
}
```

---

## 14.2 Experiment

```json
{
  "id": "E1",
  "hypothesis_id": "H1",
  "baseline": "Baseline-A",
  "modification": "Add local deformation augmentation",
  "control_variables": [
    "dataset",
    "optimizer",
    "learning rate",
    "training epochs"
  ],
  "metrics": ["mIoU", "F1"],
  "success_criterion": "mIoU improves without >1pp native degradation",
  "evidence_ids": ["EV12", "EV19"]
}
```

---

# 15. Human-in-the-loop

只在高价值节点加入。

第一版加入一个：

```text
Experiment Proposal Approval
```

流程：

```text
Agent 生成 Hypothesis
        ↓
Agent 生成 Experiment Plan
        ↓
暂停 Graph
        ↓
用户：
[Accept]
[Modify]
[Reject]
        ↓
Resume
```

后续可增加：

```text
下载大量论文前确认
生成外部图片前确认
覆盖已有 artifact 前确认
```

---

# 16. Durable Execution

Deep Research 不应该一次 `run()` 到底。

LangGraph 节点：

```text
understand_request
    ↓
plan_research
    ↓
search_papers
    ↓
screen_papers
    ↓
analyze_documents
    ↓
build_evidence
    ↓
synthesize
    ↓
propose_experiment
    ↓
human_approval
    ↓
generate_artifacts
```

每个节点后 checkpoint。

---

## 16.1 支持

```text
Pause
Resume
Retry Node
Resume after failure
```

例如：

```text
12 / 15 papers parsed
API error
```

重启后：

```text
从第 13 篇继续
```

而不是重新处理全部 15 篇。

---

# 17. Sandbox Workspace

每个项目独立目录：

```text
workspace/<uuid>/
```

所有工具强制：

```python
resolve_safe_path(project_id, relative_path)
```

禁止：

```text
..
绝对路径越界
访问 workspace 外目录
```

---

## 17.1 后期 Docker Sandbox

V2 将 Artifact MCP 的 Python 执行放到 Docker：

```text
read-only base image
workspace mount
CPU limit
memory limit
timeout
network disabled by default
```

这能成为很好的 Agent Safety 简历点。

---

# 18. Trace / Observability

第一版先自研简单 Trace，不强依赖外部平台。

记录：

```text
trace_id
project_id
agent
node
tool
input_summary
output_summary
latency
token_usage
success
error
```

UI 中展示：

```text
20:10:01 Coordinator → plan_research
20:10:03 Literature Agent → search_papers
20:10:05 Literature MCP.search_papers → 52 results
20:10:07 Literature Agent → screen_papers
20:10:14 Document MCP.parse_document → success
```

后续接：

```text
LangSmith
或
Langfuse
```

---

# 19. Evaluation

这是简历项目必须做的部分。

最终做一个：

```text
ResearchPilot-Eval
```

---

## 19.1 第一阶段 20 个任务

### Literature Search ×5

测：

```text
Precision@K
Recall@K
Duplicate Rate
```

### Figure Extraction ×5

测：

```text
Figure Recall
Figure Type Accuracy
```

### Evidence Grounding ×5

测：

```text
Evidence Precision
Citation Correctness
Claim Support Rate
```

### Experiment Planning ×5

人工 rubric：

```text
Feasibility
Groundedness
Control Variable Completeness
Hypothesis Consistency
```

---

## 19.2 Process Eval

同时记录：

```text
Task Success Rate
Tool Call Success Rate
Invalid Tool Call Rate
Average Tool Calls
Duplicate Search Rate
Average Latency
Token Usage
Recovery Rate
```

这会成为你的核心简历亮点之一。

---

# 20. Model Routing

不要第一版就实现复杂 Router。

先定义接口：

```python
class ModelRouter:
    def select(self, task_type): ...
```

默认：

```text
所有文本任务
→ Qwen 14B
```

后续：

```text
metadata extraction
→ cheap/local model

figure understanding
→ vision model

cross-paper synthesis
→ strong reasoning model

image generation
→ image model
```

然后做一组：

```text
Single Model
vs
Task-aware Routing
```

成本 / 延迟 / 成功率实验。

---

# 21. 前端页面

Streamlit 第一版只做 5 页。

---

## Page 1 — New Research

字段：

```text
Research Question
Keywords（可选）
Year Range
Maximum Papers
Existing Files
Research Constraints
```

按钮：

```text
Start Research
```

---

## Page 2 — Research Progress

显示：

```text
Plan
Current Agent
Current Node
Selected Papers
Processed Papers
Errors
Trace
```

---

## Page 3 — Evidence Workspace

三栏：

```text
Papers
Evidence
Figures / Tables
```

点击 Evidence：

```text
打开对应 page / figure / table
```

---

## Page 4 — Experiment Designer

显示：

```text
Hypotheses
Evidence
Experiment Matrix
Human Approval
```

---

## Page 5 — Artifacts

展示：

```text
Literature Comparison CSV
Extracted Figures
Experiment Plan Markdown
Mermaid Diagram
Generated Image
```

---

# 22. Repository 目录

```text
research-pilot/
│
├── README.md
├── .env.example
├── pyproject.toml
├── docker-compose.yml
│
├── app/
│   ├── api/
│   ├── core/
│   │   ├── config.py
│   │   ├── logging.py
│   │   └── security.py
│   │
│   ├── agents/
│   │   ├── coordinator.py
│   │   ├── literature.py
│   │   ├── multimodal.py
│   │   └── builder.py
│   │
│   ├── graph/
│   │   ├── state.py
│   │   ├── nodes.py
│   │   ├── routing.py
│   │   └── workflow.py
│   │
│   ├── llm/
│   │   ├── base.py
│   │   ├── ollama.py
│   │   └── router.py
│   │
│   ├── skills/
│   │   ├── loader.py
│   │   └── registry.py
│   │
│   ├── evidence/
│   │   ├── models.py
│   │   ├── store.py
│   │   └── builder.py
│   │
│   ├── workspace/
│   │   ├── manager.py
│   │   └── sandbox.py
│   │
│   ├── tracing/
│   │   ├── tracer.py
│   │   └── models.py
│   │
│   └── db/
│       ├── models.py
│       └── sqlite.py
│
├── mcp_servers/
│   ├── literature/
│   ├── document/
│   └── artifact/
│
├── skills/
│   ├── systematic-search/
│   ├── paper-screening/
│   ├── figure-extraction/
│   └── experiment-design/
│
├── ui/
│   └── streamlit_app.py
│
├── evals/
│   ├── datasets/
│   ├── evaluators/
│   └── run_eval.py
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── agent/
│
└── workspace/
```

---

# 23. 数据库表

第一版至少：

## projects

```text
id
name
goal
status
created_at
updated_at
```

## papers

```text
id
project_id
title
authors
year
doi
abstract
source
local_path
relevance_score
```

## evidence

```text
id
project_id
paper_id
type
claim
page
section
label
source_path
confidence
```

## artifacts

```text
id
project_id
type
name
path
created_by
created_at
```

## traces

```text
id
project_id
agent
node
tool
success
latency_ms
error
created_at
```

---

# 24. 开发阶段

下面按 **6 周**规划。

如果课业较忙，可以按 8~10 周拉长，但开发顺序不要改变。

---

# Week 1 — 单 Agent + Ollama + 文献搜索 MVP

## 目标

先证明：

```text
Qwen 14B
+
LangGraph
+
Literature MCP
```

能完整跑通。

## Day 1

完成：

```text
项目初始化
FastAPI
Ollama Provider
配置管理
Pydantic schema
```

测试：

```text
POST /chat
```

Qwen 正常返回。

---

## Day 2

实现 Literature MCP：

```text
search_papers
get_paper_metadata
```

优先 OpenAlex。

---

## Day 3

实现：

```text
query generation
paper deduplication
basic ranking
```

第一版 ranking：

```text
title
abstract
keyword overlap
+
LLM relevance score
```

---

## Day 4

建立第一个 LangGraph：

```text
understand_request
→ generate_queries
→ search_papers
→ rank_papers
→ final
```

---

## Day 5

建立 SQLite：

```text
projects
papers
```

UI：

```text
输入研究问题
显示搜索结果
```

### Week 1 验收

输入：

```text
搜索 2024-2026 RGB thermal image registration papers
```

系统能：

```text
生成多个 query
调用 MCP
返回论文
去重
排序
保存项目
```

---

# Week 2 — 4-Agent Orchestration + Skills

## 目标

从单 Agent 升级成真正的：

```text
Coordinator + 3 Specialists
```

## Day 1-2

实现 Agent：

```text
Coordinator
Literature Researcher
Multimodal Analyst
Research Builder
```

先不要求每个 Agent 都拥有完整能力。

---

## Day 3

实现 Handoff：

```text
Coordinator
→ Literature
→ Multimodal
→ Builder
```

---

## Day 4

实现 Skills Registry：

```text
skill metadata discovery
load_skill()
```

第一批：

```text
systematic-search
paper-screening
```

---

## Day 5

加入：

```text
Working Context Builder
Research State
```

### Week 2 验收

Trace 中能看到：

```text
Coordinator
→ Literature Researcher
→ MCP
→ Coordinator
```

且 Agent 使用 Skill，而不是把全部流程写死在一个 prompt 中。

---

# Week 3 — Multimodal Document Workspace

## 目标

支持：

```text
PDF
DOCX
PPTX
XLSX
CSV
Image
```

第一版重点 PDF。

---

## Day 1

Document MCP：

```text
parse_pdf
get_page
get_document_structure
```

---

## Day 2

PDF Figure：

```text
extract_figures
caption matching
```

---

## Day 3

PDF Table：

```text
extract basic tables
```

如果复杂表格精度不稳定，先保留：

```text
page screenshot + table region metadata
```

不要卡死在 OCR 上。

---

## Day 4

DOCX / PPTX / XLSX parser。

---

## Day 5

Research Workspace UI：

```text
Files
Figures
Tables
Document Structure
```

### Week 3 验收

上传论文 PDF 后能：

```text
显示正文结构
列出图片
保存图片
显示 Caption
查看对应页
```

---

# Week 4 — Evidence Graph + Deep Research

## 目标

这是项目从“论文助手”变成“科研 Agent”的关键周。

---

## Day 1

实现：

```text
EvidenceNode
EvidenceStore
```

---

## Day 2

Paper Analyst 对每篇论文生成：

```text
Method
Dataset
Metrics
Contribution
Limitation
Evidence IDs
```

---

## Day 3

实现跨论文 comparison：

```text
Method Comparison
Dataset Comparison
Metric Comparison
Limitation Comparison
```

输出 CSV / Markdown。

---

## Day 4

实现 Context Compaction：

```text
raw paper
→ paper summary
→ evidence index
```

---

## Day 5

加入 Research Deep-Dive：

用户可以：

```text
为什么你认为方法 A 比方法 B 更适合小样本？
```

系统必须返回：

```text
Claim
+
Evidence IDs
+
Paper/Page/Figure/Table
```

### Week 4 验收

所有重要论文比较结论至少有 1 个 evidence source。

---

# Week 5 — Experiment Designer + Artifact Generation

## 目标

完成“读论文 → 指导实验”的闭环。

---

## Day 1

实现 `experiment-design` Skill。

输出：

```text
Hypothesis
Baseline
Modification
Control
Metric
Success Criterion
Evidence
```

---

## Day 2

实现 Ablation Plan。

---

## Day 3

Human Approval：

```text
生成 Experiment Proposal
→ interrupt
→ Accept / Modify / Reject
```

---

## Day 4

Artifact MCP：

```text
create_markdown
create_csv
create_mermaid
```

---

## Day 5

图片生成接口：

```python
ImageGenerationProvider
```

第一版即使没有接实际图片 API，也要把接口设计完整。

Mermaid 科研流程图必须能直接生成。

### Week 5 验收

一个完整 Research Task 能最终得到：

```text
paper comparison.csv
important_figures/
evidence.json
experiment_plan.md
experiment_matrix.csv
pipeline.mmd
```

---

# Week 6 — Durable Execution + Evals + 简历工程化

## 目标

这一周决定项目是不是“普通 Demo”。

---

## Day 1

加入 Checkpoint：

```text
pause
resume
retry
```

测试故意让第 5 篇论文解析失败。

确保恢复后不重复处理前 4 篇。

---

## Day 2

Trace Dashboard。

指标：

```text
Agent
Tool
Latency
Success
Error
```

---

## Day 3

建立 ResearchPilot-Eval。

先做 20 条。

---

## Day 4

运行实验：

```text
Without Skills vs With Skills
Full Context vs Compacted Context
Single Agent vs 4-Agent
```

最少选其中 2 组完成。

---

## Day 5

完善：

```text
README
Architecture Diagram
Demo GIF / Video
Eval Results
Screenshots
Quick Start
```

### Week 6 验收

GitHub README 必须能回答：

```text
为什么需要 Multi-Agent？
为什么 MCP？
为什么 Skills？
如何控制 Context？
如何保证 Evidence？
失败如何恢复？
如何评测？
```

---

# 25. MVP 与增强版边界

## MVP 必须完成

```text
4 Agents
LangGraph State
Ollama Qwen 14B
Literature MCP
Document MCP
Artifact MCP
2~4 Skills
PDF Figure Extraction
Evidence Store
Experiment Plan
Human Approval
Checkpoint
Trace
20-task Eval
```

---

## V1.5 加分

```text
DOCX / PPTX / XLSX 完整支持
Dynamic Tool Loading
Research Memory
Docker Sandbox
Vision Model
Image Generation Provider
Langfuse / LangSmith
```

---

## V2 才考虑

```text
A2A Remote Literature Agent
Neo4j Evidence Graph
Browser Computer Use
Automatic Code Experiment Execution
Automatic Paper Writing
Automatic Submission
```

这些不要提前做。

---

# 26. 第一批具体开发任务清单

直接按下面顺序开发。

## P0

- [ ] 创建 GitHub 仓库
- [ ] 初始化 Python 项目
- [ ] `.env` / Config
- [ ] Ollama Qwen 14B Provider
- [ ] FastAPI `/health`
- [ ] FastAPI `/models/test`
- [ ] 定义 `ResearchRequest`
- [ ] 定义 `ResearchState`
- [ ] 建立 LangGraph Hello World

## P1

- [ ] OpenAlex client
- [ ] Literature MCP server
- [ ] `search_papers`
- [ ] `get_paper_metadata`
- [ ] Query generator
- [ ] Deduplicator
- [ ] Relevance ranker
- [ ] SQLite projects/papers

## P2

- [ ] Coordinator Agent
- [ ] Literature Agent
- [ ] Handoff
- [ ] Trace logging
- [ ] Streamlit New Research

## P3

- [ ] Document MCP
- [ ] PDF parser
- [ ] figure extraction
- [ ] caption matching
- [ ] workspace manager

## P4

- [ ] Multimodal Analyst
- [ ] EvidenceNode
- [ ] EvidenceStore
- [ ] paper summary schema
- [ ] cross-paper comparison

## P5

- [ ] Research Builder
- [ ] experiment-design Skill
- [ ] experiment matrix
- [ ] HITL interrupt
- [ ] Mermaid artifact

## P6

- [ ] Checkpointer
- [ ] Resume
- [ ] Eval dataset
- [ ] Eval runner
- [ ] README
- [ ] Demo

---

# 27. 第一批 API

```text
POST /projects
GET  /projects/{id}

POST /projects/{id}/research
POST /projects/{id}/resume

GET  /projects/{id}/papers
GET  /projects/{id}/evidence
GET  /projects/{id}/artifacts
GET  /projects/{id}/trace

POST /projects/{id}/approval
```

---

# 28. 测试策略

## Unit Test

```text
OpenAlex parsing
DOI normalization
file path sandbox
Skill loading
Evidence serialization
```

## Integration Test

```text
Literature MCP
Document MCP
Artifact MCP
Ollama structured output
```

## Agent Test

```text
是否调用正确 Agent
是否选择正确 Tool
是否产生 Evidence
是否保存 State
是否能 Resume
```

---

# 29. 简历最终应该重点强调的内容

项目开发过程中要刻意留下可量化指标。

最终可以写：

```text
ResearchPilot — Skill-Driven Multimodal Deep Research Agent
```

技术：

```text
LangGraph · MCP · Agent Skills · FastAPI · Ollama · Qwen
Multimodal Document Processing · SQLite · Docker · Agent Evals
```

简历 bullet 方向：

```text
1. 设计 4-Agent hierarchical orchestration，实现文献检索、多模态证据分析、跨论文综合与实验计划生成。

2. 自研 Literature / Document MCP Servers，统一科研搜索 API 与 PDF、DOCX、PPTX、XLSX 等文件处理工具。

3. 实现 Skill-driven Agent Runtime，通过 Progressive Disclosure 动态加载科研工作流，降低无关上下文与工具 schema 开销。

4. 构建细粒度 Evidence Store，将科研结论关联到论文 section / page / figure / table，实现可追溯的 evidence-grounded reasoning。

5. 基于 LangGraph 实现 checkpoint、resume、human-in-the-loop 与 task-aware context compaction，支持长任务失败恢复。

6. 建立 ResearchPilot-Eval，从 tool-call success、citation grounding、figure extraction、trajectory efficiency 等维度评估 Agent。
```

最终把真实实验数据补入：

```text
任务成功率
Citation Accuracy
Figure Recall
Tool Call Success Rate
Token Reduction
Latency
Recovery Rate
```

不要在简历中使用未经实际实验得到的数字。

---

# 30. 项目最重要的差异化表达

不要把 ResearchPilot 描述成：

```text
基于 LangGraph 的多智能体论文助手
```

推荐表述：

> **A skill-driven, MCP-native multimodal research agent that converts an open-ended research question into traceable scientific evidence, structured cross-paper analysis, and evidence-grounded experiment plans.**

核心链路：

```text
Search
  ↓
Read
  ↓
Multimodal Evidence
  ↓
Reason
  ↓
Plan
  ↓
Create
```

整个项目最值得打磨的不是“Agent 数量”，而是：

```text
Agent Orchestration
MCP Tooling
Agent Skills
Multimodal Evidence
Context Engineering
Durable Execution
Human Approval
Tracing
Evaluation
```

---

# 31. 开发时必须避免的坑

## 1. 不要先做 UI

优先：

```text
Graph
MCP
State
Evidence
```

UI 最后包装。

## 2. 不要为了 Multi-Agent 强行拆 Agent

只有职责、工具权限或上下文明显不同才拆。

## 3. 不要把整个 PDF 全塞进 Qwen

优先结构化提取 + evidence + context builder。

## 4. 不要第一版追求完美 PDF OCR

PyMuPDF + caption matching 足以支撑 MVP。

## 5. 不要让本地 14B 决定所有复杂科研结论

核心结论必须保留证据，模型只是辅助推理。

## 6. 不要一开始实现 A2A

4 Agent 单进程 LangGraph 足够。

## 7. 不要忽略 Eval

对简历项目来说，20 条认真设计的 Agent Eval，比再增加 5 个功能更有价值。

---

# 32. 第一阶段最终验收场景

开发完成后，用一个固定 Demo 完整录屏。

### Demo Prompt

```text
调研 2024-2026 年 RGB-LWIR image registration 研究。

要求：
1. 搜索并筛选 10 篇代表性论文；
2. 比较各论文的核心方法、数据集和评价指标；
3. 提取其中模型结构图；
4. 总结目前主要技术路线和局限；
5. 为一个新的 RGB-LWIR 配准研究设计 baseline、两个改进假设、主实验和消融实验；
6. 所有关键结论必须提供论文、页码、章节、图或表级证据；
7. 生成最终实验流程 Mermaid 图。
```

### Demo 必须展示

```text
Agent Handoff
MCP Tool Call
Skill Loading
Paper Search
PDF Parsing
Figure Extraction
Evidence Trace
Experiment HITL
Checkpoint
Final Artifacts
Eval Dashboard
```

如果这个 Demo 能流畅完成，ResearchPilot 就已经具备很强的简历展示价值。

---

# 33. 现在立即开始的第一步

今天只做以下 5 件事：

```text
1. 创建 research-pilot 仓库
2. 建立 OllamaProvider
3. 测试 Qwen 14B structured output
4. 创建 Literature MCP
5. 跑通 OpenAlex search_papers
```

第一天不要写任何多模态代码，也不要写完整四 Agent。

先验证最小闭环：

```text
User Research Question
      ↓
Qwen Query Generation
      ↓
Literature MCP
      ↓
OpenAlex
      ↓
Structured Papers
```

跑通后，再进入 Week 1 后续开发。
