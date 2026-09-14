# 架构说明

ResearchPilot 是一个本地优先、证据可追溯的多模态论文研究助手，由两个进程组成：

- **API 服务**：FastAPI + LangGraph + SQLite，承载检索、分析、后台任务与证据复核。
- **工作台**：Streamlit，面向用户的四步界面，只通过 REST API 与后端交互。

REST 接口清单见 [`README.md`](../README.md)。本文描述后端架构。

## 进程与数据流

```
Streamlit (ui/app.py)
   │  REST (HTTP :8000)
   ▼
FastAPI (app/main.py)
   ├─ /health、/mcp/status 路由
   ├─ 工作台路由 (app/api/routes/core.py)
   ├─ WorkspaceService (app/services/workspace.py)   ← 读写项目/工作台读模型
   └─ WorkflowWorker (app/workflow_worker.py)        ← 单机持久化任务队列
          │
          ▼
   ResearchWorkflowService (app/services/workflow.py)
          │
          ├─ Graph ① 检索图 (app/graph/workflow.py)       8 节点
          └─ Graph ② 分析图 (app/graph/analysis_graph.py) 5 节点
                 │
                 ├─ MCP 能力网关 (app/mcp)
                 ├─ 文献源 OpenAlex / Crossref / arXiv
                 ├─ 文档子系统 (app/documents)
                 ├─ Ollama 文本/视觉模型 (app/llm)
                 └─ 技能注册表 (app/skills → skills/)
```

## 启动装配（lifespan）

`app/main.py` 的 lifespan 按顺序装配依赖，停机按逆序收尾：

1. 业务库：建库 + 幂等迁移（schema 1..18，重启自动补缺）。
2. LangGraph 档位：同一 SQLite 文件建 checkpoints/writes + WAL（重启续跑的地基）。
3. 技能注册表：启动只建元数据索引，正文按需加载。
4. 文档子系统：工作区 + 解析器 + 门面一次组装（应用与 MCP 文档能力共享同一实例）。
5. MCP 能力网关：注册 server + 启动体检（文献/文档 inprocess + 外部 arXiv）。
6. 后台 worker：恢复中断任务并开始领取循环。

## 模块地图

| 目录 | 职责 |
|---|---|
| `app/api` | FastAPI 路由、依赖注入、异常处理、上传令牌 |
| `app/core` | 配置（pydantic-settings）、日志 |
| `app/db` | 仓储层：项目、论文、证据、会话、任务、追踪等 |
| `app/literature` | 文献源适配、检索词生成、去重、过滤、排序 |
| `app/documents` | PDF 获取、解析（PyMuPDF）、工作区管理 |
| `app/evidence` | 证据构建、一致性校验、复核台、视觉复核 |
| `app/llm` | Ollama 文本/视觉调用、结构化输出重试 |
| `app/mcp` | MCP 能力注册表与路由（文献/文档/外部 arXiv） |
| `app/graph` | 两个 LangGraph 流水线 |
| `app/services` | WorkspaceService / ResearchWorkflowService |
| `app/skills` | 技能绑定与注册表 |
| `app/schemas` | Pydantic 模型 |
| `app/agents` | 检索/分析 Agent 封装 |
| `app/reliability` | 故障与暂停异常定义 |
| `mcp_servers` | 可独立部署的 MCP Server（stdio/HTTP） |
| `ui` | Streamlit 工作台 |
| `skills` | 技能定义（SKILL.md） |
| `evals` | 一致性/解析/检索评估数据与脚本 |

## 两个 LangGraph 流水线

两条流水线共享同一个 `AsyncSqliteSaver`，用 thread 前缀区分：

- **Graph ① 检索图**（thread `{project}:search-{revision}:{track}`）：
  `understand_request → generate_queries → search_papers → deduplicate_papers →
  filter_papers → enrich_arxiv_abstracts → rank_papers → select_papers`。
- **Graph ② 分析图**（thread `{project}:analysis:{run}`）：
  `select_gate`（选文，interrupt）→ `acquire_documents` → `analyze_papers` →
  `review_gate`（证据复核门，interrupt）→ `synthesize`，带 regenerate 回边。

## 持久化与断点续跑

- 业务数据与 LangGraph checkpoint 存同一 SQLite（`DATABASE_PATH`）。
- 后台 worker 是单机持久化队列：原子认领任务 → execute_job → 双写终态；重启时
  `recover_interrupted()` 把遗留 running 任务放回队列。
- 人工等待用 `hitl_events`（waiting_for_human）表达：图节点冻结运行，人工裁决动作再入队续跑。
- 外部检索/解析调用按 work-item 参数指纹缓存，失败重试只补未完成部分。
- 预算门：LLM 调用计量到 usage 台账，越过阈值在安全边界暂停（`AnalysisPausedError`）。

## 存储

- `data/research_pilot.db`：业务库 + LangGraph checkpoint。
- `data/workspaces/{project}`：论文 PDF、解析产物、图表裁剪、manifest。
- `data/oauth-proxy/`：独立 MCP 的 OAuth 状态（加密存储，已忽略不提交）。
- `skills/`：技能定义（正文按需加载，受 `SKILL_MAX_BYTES` 限制）。
