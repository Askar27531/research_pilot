# ResearchPilot

![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Inference](https://img.shields.io/badge/inference-local--first-orange)
![UI](https://img.shields.io/badge/UI-Streamlit-ff4b4b)

ResearchPilot 是一个**本地优先、证据可追溯**的多模态论文研究助手。

它先理解你的课题并生成可审阅的检索策略，由你选定一至两篇论文后，再阅读 PDF 正文与图表，产出**每条结论都能回溯到页码、区域与来源哈希**的逐篇分析，以及两篇论文的对比综合。所有模型推理都在本地 Ollama 上完成，论文全文、解析产物与证据链不出本机。

> **设计取舍**：宁可显式失败，也不静默降级。视觉模型不可用时系统拒绝创建项目，而不是退回纯文本、假装读懂了图表。

## 它解决什么问题

传统文献工具给你一张清单；通用 LLM 给你一段总结，但你无法核对它读的是哪一页、哪张图。

ResearchPilot 面向**需要核对证据**的研究场景：结论必须挂载可追溯的证据，图表证据以实际裁剪图呈现，存疑证据可由人标记为确认、存疑或排除。系统只呈现证据、差异、冲突与风险，把最终判断权留给人。

## 用户流程

Streamlit 工作台分为四步：

1. **描述课题** —— 研究问题、当前方案、主要困难、目标指标；可选限定年份范围、论文数量上限与文献源。
2. **检索文献** —— 大模型理解课题并生成检索词与检索策略，经 MCP 从 OpenAlex、Crossref、arXiv 并发检索、规范化、去重、过滤并排序。
3. **选择论文** —— 查看检索策略、摘要与相关性说明，选择一至两篇并补充分析要求。
4. **证据化分析** —— 自动获取所选全文（失败时可上传 PDF），查看逐篇分析、图表证据与双篇比较。

两个**人工闸门**保证流程不会替你自动往下跑：未确认论文前不下载、不分析任何全文；逐篇分析完成后、最终合成前，若存在机器存疑的证据，流程会停下等你裁决。

## 核心能力

- **多来源检索**：OpenAlex、Crossref、arXiv 并发检索、规范化、去重与概念过滤，单一来源故障不影响整轮检索。
- **文档解析**：PyMuPDF 原生文本抽取、按需 OCR、位图/矢量区域识别、表格定位与稳定裁剪。
- **视觉分析**：本地视觉模型解读架构图、结果图、消融图与扫描页；模型响应经 Pydantic Schema 强校验，不合法则重试。
- **跨模态一致性**：把正文对图表的陈述与实际图像内容比对，标记冲突。
- **证据可追溯**：文本、视觉与表格证据均保留页码、区域、原文或裁剪图，以及 SHA-256 来源哈希。
- **持久化执行**：SQLite 同时承载业务数据与 LangGraph checkpoint，后台任务跨服务重启可恢复。
- **成本闸门**：LLM 调用逐次计量入账，累计越过阈值时在安全边界暂停，由人决定是否继续。
- **增量呈现**：分析结果按「论文 × 分析部分」分块落地，不必等整篇跑完即可阅读。

## 架构概览

两个进程，一个本地模型服务：

```
Streamlit (ui/app.py)  ──REST──▶  FastAPI (app/main.py)  ──▶  Ollama（文本 + 视觉）
                                        │
                                        ├─ WorkspaceService   工作台读模型
                                        └─ WorkflowWorker     单机持久化任务队列
                                               │
                                               ├─ Graph ① 检索图（8 节点）
                                               └─ Graph ② 分析图（5 节点，含 2 个人工闸门）
```

- **两条 LangGraph 流水线**共享同一个 SQLite checkpointer，用 thread 前缀区分；每个节点完成即落一份状态快照。
- **人工等待事件化**为 `hitl_events`：图节点冻结运行，人工裁决后再入队续跑，自动重试无法绕过。
- **幂等重放**：外部检索与解析调用按 work-item 参数指纹缓存，失败重试只补未完成部分，不重复付费。

模块地图与启动装配顺序见 **[架构说明](docs/ARCHITECTURE.md)**。

## 证据模型

每条结论都可回溯：

| 证据类型 | 来源 | 追溯信息 |
|---|---|---|
| 文本 | 解析后的 PDF 正文 | 页码、区域坐标、原文片段、SHA-256 |
| 视觉 | 图表裁剪图 | 页码、区域、裁剪图本身、SHA-256 |
| 表格 | 表格区域 | 页码、区域、结构化内容 |

证据进入分析前会经过一层视觉验证；人工复核状态持久化在项目中，并影响复核门的走向。

## 评估

`evals/` 下有 5 个评估套件：检索相关性、PDF 解析、跨模态一致性、真实图表一致性，以及一个 20 任务的综合数据集（检索 F1、图表数量与类型、证据支撑率、报告完整度）。单项得分 ≥ 0.8 记为通过，每次运行记录 git commit、模型名与运行环境，便于追踪口径漂移。

其中 3 个套件的数据集内嵌预测结果，**完全离线**运行，不依赖模型与网络。

## 快速开始

需要 Python 3.12 与本地 Ollama，并准备一个文本模型和一个支持图像输入的视觉模型。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"     # 或 python -m pip install -r requirements.txt
Copy-Item .env.example .env           # 填写 OLLAMA_MODEL 与 OLLAMA_VISION_MODEL
ollama pull qwen3:latest
ollama pull qwen3-vl:8b
```

全部配置项及说明见 `.env.example`。启动 API 与工作台：

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
streamlit run ui/app.py
```

工作台 <http://localhost:8501>，交互式 API 文档 <http://127.0.0.1:8000/docs>，健康与模型预检 <http://127.0.0.1:8000/health>。

## REST API

12 个核心操作覆盖项目生命周期、统一动作入口与产物读取：

| 方法与路径 | 用途 |
|---|---|
| `GET /health` | 服务、数据库、文本模型、视觉模型与 OCR 预检 |
| `GET /mcp/status` | MCP 能力注册、发现与健康状态 |
| `GET /projects` | 项目列表 |
| `POST /projects` | 创建项目、保存研究资料并提交首次任务 |
| `PATCH /projects/{id}` | 更新课题信息 |
| `DELETE /projects/{id}` | 删除项目及其本地工作区文件 |
| `GET /projects/{id}/workspace` | 统一工作台读模型，可按需读取单篇论文详情 |
| `POST /projects/{id}/actions` | 统一动作入口 |
| `POST /projects/{id}/documents` | 以上传令牌提交 PDF 并自动继续分析 |
| `GET /projects/{id}/review-desk` | 证据复核台：列出待复核证据 |
| `POST /projects/{id}/review-desk/preview` | 证据复核预览 |
| `GET /projects/{id}/resources/{token}` | 读取证据、图表裁剪或下载产物 |

`POST /projects/{id}/actions` 按 `type` 区分动作：运行与重试、带补充要求重新检索、确认选文、重新分析（整篇或单个分析块）、暂停与继续、单条证据复核，以及复核门的继续或回退重跑。

## MCP Server 独立部署

自研 MCP Server（`literature` / `document`）可脱离 FastAPI 独立部署，供外部 MCP 客户端使用，支持三种 HTTP 鉴权模式：`none`（回环/可信网络）、`static`（预共享 Bearer Token）、`github`（**OAuth 2.1 授权码 + PKCE**，经 GitHub OAuth App 代理）。非回环绑定且无鉴权会被拒绝启动。

详见 **[MCP OAuth 说明](docs/MCP_OAUTH.md)**。

## 项目结构

| 目录 | 说明 |
|---|---|
| `app/` | FastAPI 后端：路由、服务、LangGraph 流水线、文献/文档/证据/LLM/MCP 等 |
| `ui/` | Streamlit 工作台 |
| `mcp_servers/` | 可独立部署的 MCP Server 与 OAuth 鉴权 |
| `skills/` | 技能定义（`SKILL.md`），共 6 个 |
| `config/` | MCP server 声明 |
| `evals/` | 评估数据集、评估器与结果快照 |
| `scripts/` | 演示与运维脚本 |
| `docs/` | 架构与 MCP OAuth 说明 |

## 开发

```powershell
python -m ruff check app mcp_servers ui
python -m pip check
```

代码规范：目标 Python 3.12、行宽 100；换行由 `.gitattributes` 归一化为 LF。

## 许可证

[MIT](LICENSE) © ResearchPilot contributors
