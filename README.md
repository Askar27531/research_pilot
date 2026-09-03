# ResearchPilot

ResearchPilot 是一个本地优先、证据可追溯的多模态论文研究助手。它先生成并展示检索策略，由用户选择一至两篇论文后，再阅读 PDF 正文与图表并生成证据化综合分析。

## 用户流程

Streamlit 工作台分为四步：

1. 描述课题：填写研究问题、当前方案、主要困难和目标指标。
2. 检索文献：大模型理解课题并生成检索词和策略，MCP 从 OpenAlex、Crossref 和 arXiv 检索。
3. 选择论文：查看检索策略、摘要和相关性说明，选择一至两篇并补充分析要求。
4. 证据化分析：自动获取所选全文，失败时上传 PDF；查看逐篇分析、图表证据和双篇比较。证据复核后，可继续审阅证据支持的研究方向建议与实验方案（只生成方案与材料，不执行实验）。

未确认论文前不会下载或分析全文。Project ID、Paper ID、revision、resume、Trace 和 JSON 均不会出现在普通用户流程中。

## 技术能力

- OpenAlex、Crossref、arXiv 多来源并发检索、规范化、去重和部分失败容错。
- PyMuPDF 原生文本、按需 OCR、位图/矢量区域、表格和稳定裁剪提取。
- Ollama 本地视觉模型分析架构图、结果图、消融图和扫描页面，响应经 Pydantic Schema 校验。
- 文本、视觉和表格证据保留页码、区域、原文/裁剪与 SHA-256 来源哈希。
- SQLite 持久化后台任务、LangGraph checkpoint 和项目级并发控制，服务重启后可恢复。
- 最终生成逐篇证据化分析，以及选择两篇时的共同点、差异、互补性和适用条件；证据复核后生成研究方向建议与实验方案，经用户审阅确认后产出 Markdown/CSV/Mermaid 研究材料。

## 安装与配置

要求 Python 3.12、Ollama，以及一个文本模型和支持视觉输入的本地模型。

```powershell
cd D:\Agent\research-pilot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

在 `.env` 中填写 `OLLAMA_MODEL` 和 `OLLAMA_VISION_MODEL`，模型名必须与 `ollama list` 完全一致。ResearchPilot 不会自动下载模型，也不会在视觉模型不可用时静默降级。

```powershell
ollama list
Invoke-RestMethod http://localhost:11434/api/tags
```

## 启动

先启动 API：

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

再启动工作台：

```powershell
streamlit run ui/app.py
```

浏览器打开 <http://localhost:8501>。API 文档位于 <http://127.0.0.1:8000/docs>，健康与模型预检可通过以下命令查看：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

## 核心 REST API

核心接口共 8 种路径、10 个操作。工作台只依赖其中 7 种路径、9 个操作（`GET /mcp/status` 仅供诊断与能力注册检查，由运维/自动化使用）：

| 方法与路径 | 用途 |
|---|---|
| `GET /health` | 服务、数据库、文本模型、视觉模型和 OCR 预检 |
| `GET /mcp/status` | MCP 能力注册、发现与健康状态（诊断用） |
| `GET /projects` | 项目列表 |
| `POST /projects` | 创建项目、保存研究资料并提交首次任务 |
| `PATCH /projects/{id}` | 更新课题信息；取消当前后台任务，保存后需重新提交检索 |
| `DELETE /projects/{id}` | 删除项目及其本地工作区文件 |
| `GET /projects/{id}/workspace` | 获取统一工作台读模型，可用 `paper` 参数按需读取论文详情 |
| `POST /projects/{id}/actions` | 运行、重试、审批、调整预览/应用和证据复核 |
| `POST /projects/{id}/documents` | 使用工作台提供的上传令牌提交 PDF，并自动继续分析 |
| `GET /projects/{id}/resources/{token}` | 读取证据、图表裁剪或下载产物 |

旧的细分 REST 路由已删除，不提供兼容代理。内部 Repository、Service、Worker 和 MCP 工具仍按职责拆分。

创建项目示例：

```powershell
$body = @{
  research_question = "多模态模型如何提高学术图表理解的可靠性？"
  current_approach = "基于文本抽取的文献综述"
  difficulties = @("图表证据难以追溯")
  target_metrics = @("证据定位准确率")
  advanced = @{
    year_from = 2022
    year_to = 2026
    max_papers = 20
    sources = @("openalex", "crossref", "arxiv")
  }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/projects `
  -ContentType application/json `
  -Body $body
```

## 质量检查

```powershell
python -m pytest
python -m ruff check app mcp_servers ui tests
python -m pip check
```

运行在线演示前需启动 API、Ollama，并配置文献服务：

```powershell
python -m scripts.run_fixed_demo
```

详细边界见 [架构说明](docs/ARCHITECTURE.md)。历史阶段验证文档保留用于追踪演进，但其中旧路由示例不再是当前接口。

## 当前边界

仅深入支持学术 PDF；不支持 DOCX、PPTX、XLSX 或独立图片。本轮不实现引用网络、PRISMA、系统综述协议、实验执行、训练代码生成、GPU 管理或 MLOps。系统只会基于证据生成研究方向建议与实验方案并等待用户确认，不自动宣称研究方向具有创新性，只呈现证据、差异、冲突和风险。
