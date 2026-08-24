# ResearchPilot 接口请求流程与代码逐行导读

> 适用代码：当前仓库 `0.1.0`。本文按实际 HTTP 请求的执行顺序讲解代码；“逐行”采用连续行号区间说明，空行和纯导入行合并解释。行号以本文生成时的源码为准，后续修改代码后可能发生偏移。

## 1. 先建立整体认识

一个请求通常经过下面这些层：

```text
客户端 / Streamlit
  → FastAPI app 与 RequestIDMiddleware
  → Pydantic 请求校验
  → Depends 依赖注入
  → API route
  → Agent / LangGraph / Domain Service
  → MCP tool（文献检索时）
  → Repository / Workspace
  → SQLite / 项目文件
  → Pydantic response_model
  → JSON 或文件响应
```

当前代码不是一条覆盖所有能力的总流水线，而是三组接口：

1. `/research/*`：临时研究理解和检索，不创建持久化项目。
2. `/projects/*`：创建项目、执行并保存文献搜索、恢复、查询进度。
3. `/projects/{project_id}/documents|evidence|experiment-proposal|artifacts/*`：独立调用文档、证据、实验和产物能力。

尤其要注意：`POST /projects/{project_id}/research` 当前只运行 `literature_search`，不会自动继续 PDF、Evidence、实验和 Artifact。

## 2. 应用启动和所有请求共有的流程

### 2.1 `app/main.py`：组装应用

源码：[app/main.py](../app/main.py)

| 行号 | 代码作用 |
|---|---|
| 1–8 | 导入异步生命周期、SQLite、FastAPI，以及 LangGraph 的 SQLite Checkpointer。 |
| 10–21 | 导入所有统一异常处理器和请求 ID 中间件。 |
| 22–28 | 导入七组 API Router。 |
| 29–36 | 导入配置、日志、数据库、文档服务、异常类型和 Skill Registry。 |
| 39–40 | 用 `@asynccontextmanager` 定义 FastAPI 生命周期；应用启动前执行 `yield` 之前的代码，关闭时执行 `finally`。 |
| 41 | 读取 `.env` 和默认设置。 |
| 42–43 | 创建 `Database` 并执行 migration，确保 SQLite 表结构存在。 |
| 44–49 | 单独创建 checkpoint SQLite 连接和 `AsyncSqliteSaver`；Agent Graph 的状态由它持久化。 |
| 50–51 | 创建 Skill Registry；启动时只发现元数据，不加载完整 Skill 内容。 |
| 52–54 | 把数据库、checkpointer、skills 放到 `app.state`，供依赖函数复用。 |
| 55–60 | 建立受项目目录约束的 `WorkspaceManager`、`PDFParser` 和 `DocumentService`。 |
| 61–64 | `yield` 后进入关闭阶段，释放 checkpoint 连接。 |
| 67–74 | 创建 FastAPI 实例并绑定生命周期。 |
| 75 | 注册 `RequestIDMiddleware`，所以每个路由前后都会经过它。 |
| 76–84 | 把领域异常映射成稳定的 HTTP JSON 错误。注册顺序从具体异常到兜底 `Exception`。 |
| 85–91 | 注册所有路由；最终路径由这里和各 Router 的 `prefix` 共同组成。 |
| 92 | 返回组装完成的应用。 |
| 95 | 模块导入时执行 `create_app()`；Uvicorn 使用的就是这个 `app`。 |

### 2.2 `app/api/errors.py`：请求 ID 与统一错误

源码：[app/api/errors.py](../app/api/errors.py)

| 行号 | 代码作用 |
|---|---|
| 26–35 | 定义统一错误契约：`code/message/retryable/request_id/details`。 |
| 38–39 | 从 `request.state` 读取 request ID；极端情况下临时生成。 |
| 42–59 | `error_response()` 把任意领域错误包装成统一 JSON。 |
| 62–65 | 中间件优先接收客户端 `X-Request-ID`，没有则生成 UUID，并限制到 128 字符。 |
| 66–67 | 调用下游路由，随后把同一个 ID 写入响应头。 |
| 68–75 | 记录方法、路径、状态码和 request ID。 |
| 78–85 | LLM 错误转换为可重试的 HTTP 503 `MODEL_UNAVAILABLE`。 |
| 88–103 | OpenAlex 缺 key 单独返回明确错误；其他文献错误按可重试性映射为 503/502。 |
| 106–117 | Pydantic/FastAPI 参数校验失败时返回 422，并只暴露安全的字段位置和错误说明。 |
| 120–127 | 普通 `HTTPException` 保留状态码并尝试用标准 HTTP 状态名作为错误码。 |
| 130–138 | 未处理异常只在服务端记录堆栈，对客户端隐藏内部信息。 |
| 141–160 | Repository、并发冲突、Evidence 引用冲突和文档安全错误分别映射为 404、409、400/422。 |

### 2.3 `app/api/dependencies.py`：依赖注入

源码：[app/api/dependencies.py](../app/api/dependencies.py)

| 行号 | 代码作用 |
|---|---|
| 26–30 | 每个需要模型的 HTTP 请求创建一个 `OllamaProvider`；响应结束后自动关闭 HTTP client。 |
| 33–46 | 从 `app.state` 取出长期资源：checkpointer、skills、database、document service。 |
| 49–100 | 每次请求用共享 `Database` 轻量创建相应 Repository。Repository 自己不持有长期连接。 |
| 103–105 | 用 `lru_cache` 复用 Literature MCP Adapter；它连接的是进程内 FastMCP server。 |

FastAPI 会先解析路径、查询、表单或 JSON，再递归解析 `Depends(...)`，最后才调用路由函数。因此参数格式不合法时，业务代码尚未执行就会返回 422。

## 3. 接口总览

| 方法与路径 | 主要用途 | 最深调用层 |
|---|---|---|
| `GET /health` | 进程健康 | Route |
| `GET /health/dependencies` | 检查配置是否填写 | Route → Settings |
| `POST /models/test` | 测试 Ollama 结构化输出 | Route → Ollama |
| `POST /research/test` | 临时运行“理解请求”Graph | Route → LangGraph → Ollama |
| `POST /research/search` | 临时检索论文 | Route → LangGraph → MCP → OpenAlex |
| `POST /projects` | 创建持久化项目 | Route → ProjectRepository → SQLite |
| `GET /projects/{id}` | 查询项目 | Route → ProjectRepository |
| `POST /projects/{id}/research` | 持久化执行文献搜索 | Route → Coordinator → LiteratureResearcher → Graph |
| `POST /projects/{id}/resume` | 恢复文献搜索 | 同上，并复用 checkpoint/work item |
| `GET /projects/{id}/papers` | 查询入选论文 | PaperRepository |
| `GET /projects/{id}/trace[/metrics]` | 查询 Trace/指标 | TraceRepository |
| `GET /projects/{id}/progress[/metrics]` | 查询工作项/恢复指标 | WorkItemRepository |
| `POST /projects/{id}/documents/import` | 上传并解析 PDF | Workspace → PDFParser → DocumentRepository |
| `POST /projects/{id}/evidence/{text|figure|table}` | 创建可核验 Evidence | EvidenceBuilder → EvidenceRepository |
| `GET /projects/{id}/evidence...` | 查询和回读证据 | Repository → EvidenceVerifier |
| `PUT /projects/{id}/summaries/{paper_id}` | 保存论文摘要 | SummaryRepository |
| `GET /projects/{id}/comparison` | 构建跨论文比较 | SummaryRepository → Synthesizer |
| `POST /projects/{id}/experiment-proposal` | 生成方案并暂停审批 | ProposalBuilder → LangGraph interrupt |
| `POST .../experiment-proposal/decision` | 接受、修改或拒绝 | LangGraph resume → ProposalRepository |
| `POST /projects/{id}/artifacts/{markdown|csv|mermaid}` | 生成产物 | ArtifactService → Workspace/Repository |
| `GET /projects/{id}/artifacts...` | 列表或校验下载 | ArtifactRepository → hash verify |

## 4. 健康检查与模型测试

### 4.1 `GET /health`

源码：[app/api/routes/health.py](../app/api/routes/health.py)

1. 第 21 行把 `GET /health` 注册到 Router。
2. 第 22–23 行直接返回 `HealthResponse(status="ok", service="research-pilot")`。
3. FastAPI 根据 `response_model` 再校验一次并序列化为 JSON。

这个接口只证明 FastAPI 进程能响应，不测试 SQLite、Ollama 或 OpenAlex 的真实连接。

### 4.2 `GET /health/dependencies`

1. 第 26–27 行注册接口。
2. 第 28 行读取 Settings。
3. 第 29–32 行只检查模型名和 OpenAlex key 是否为非空字符串。

### 4.3 `POST /models/test`

源码：[app/api/routes/models.py](../app/api/routes/models.py)

| 行号 | 执行过程 |
|---|---|
| 13–18 | 请求体只有 `prompt`，并约束长度 1–1000。 |
| 21–30 | `ModelProbe` 是内部模型输出 Schema；`ModelTestResponse` 是 HTTP 响应 Schema。 |
| 33–37 | FastAPI 校验请求并通过 `get_llm_provider` 注入 Ollama Provider。 |
| 38–47 | 构造 system/user messages，要求 Ollama 严格输出 `ModelProbe`。 |
| 49–54 | 组合 provider、模型名和生成内容后返回。 |

Ollama 的底层实现位于 [app/llm/ollama.py](../app/llm/ollama.py)：

- 13–23 行：读取 base URL、模型名和超时，建立异步 HTTP client。
- 32–45 行：从 Pydantic 模型生成 JSON Schema，请求后再用 Pydantic 严格校验模型文本。
- 47–60 行：构造 Ollama `/api/chat` payload，关闭流式输出和 thinking。
- 61–77 行：请求接口，并把超时、HTTP 错误、网络错误和非法 JSON 转成 `LLMError`。
- 79–87 行：上下文退出时关闭当前请求拥有的 client。
- 90–124 行：删除 Ollama grammar 不需要的约束，但生成结果仍接受完整 Pydantic 校验。

## 5. 临时 Research 接口

### 5.1 `POST /research/test`

源码：[app/api/routes/research.py](../app/api/routes/research.py)

| 行号 | 执行过程 |
|---|---|
| 35–40 | 注册接口并注入 Ollama 与全局 SQLite checkpointer。 |
| 41 | `create_research_state()` 为请求生成 project UUID 和所有空状态字段。 |
| 42 | 取 UUID；这里只作为 Graph thread ID，不在 projects 表创建记录。 |
| 43 | `build_research_graph()` 创建只含 `understand_request` 的最小 Graph。 |
| 44 | LangGraph 要求 checkpoint 配置放在 `configurable.thread_id`。 |
| 45 | `ainvoke` 异步执行 Graph。 |
| 46 | 把 Graph 中的 dict 重新验证为 `ResearchUnderstanding`。 |
| 47–52 | 返回理解结果和阶段。 |

[app/graph/workflow.py](../app/graph/workflow.py) 19–29 行具体组图：创建 `StateGraph`，添加理解节点，连接 `START → understand_request → END`，最后绑定 checkpointer 编译。

[app/graph/nodes.py](../app/graph/nodes.py) 的理解节点：

- 10–13 行：闭包把 Provider 注入节点。
- 14–30 行：还原 `ResearchRequest`，构造提示词并要求 `ResearchUnderstanding` 结构化输出。
- 32–36 行：用户显式年份覆盖模型推断结果。
- 37–39 行：中文请求过滤乱码或错误语言的 ambiguity。
- 41–44 行：节点只返回状态增量，不原地修改 Graph state。

### 5.2 `POST /research/search`

同一文件 55–78 行：

1. 第 62 行初始化状态。
2. 第 64 行建立完整检索 Graph。
3. 第 65 行用 project UUID 隔离 checkpoint。
4. 第 66 行运行所有节点。
5. 第 67–78 行把查询、候选数量、入选论文、去重报告、搜索策略、概念过滤和 warning 投影成响应。

检索 Graph 位于 [app/graph/workflow.py](../app/graph/workflow.py) 32–55 行，严格顺序为：

```text
understand_request
  → generate_queries
  → search_papers
  → deduplicate_papers
  → filter_papers
  → rank_papers
  → select_papers
```

[app/graph/search_nodes.py](../app/graph/search_nodes.py) 逐节点说明：

| 行号 | 节点行为 |
|---|---|
| 26–44 | 恢复请求和理解对象，调用 query generator，把 required concepts、excluded topics 和 queries 写回状态。 |
| 47–78 | 对每条查询调用 Literature MCP；单条失败写 warning，全部失败才抛异常。每条最多取 `min(30, max(maximum_papers×2, 10))`。 |
| 81–95 | 按 DOI/OpenAlex ID/规范化标题去重，并把输入数、唯一数、合并日志写入 `plan`。 |
| 98–121 | 必须命中每组 required concept，且不能命中排除主题；全部被过滤时明确失败。 |
| 124–140 | 先词法后 LLM 进行 include/exclude 和相关性排序；LLM 降级信息进入 warnings。 |
| 143–150 | 截取前 `maximum_papers` 篇作为最终选择。 |

### 5.3 Literature MCP 与 OpenAlex 边界

[app/literature/mcp_client.py](../app/literature/mcp_client.py)：

- 14–33 行：为一次搜索创建 FastMCP Client，调用名为 `search_papers` 的工具，而不是直接调用 OpenAlex 类。
- 34–38 行：把 MCP tool error 转换为应用领域错误，并验证 structured content。
- 40–50 行：metadata 查询采用同样模式。

[mcp_servers/literature/server.py](../mcp_servers/literature/server.py)：

- 10–23 行：Server lifespan 创建 OpenAlex client，并只关闭自己创建的 client。
- 25–29 行：建立 Literature MCP server。
- 31–47 行：注册 `search_papers` tool，从 lifespan context 获取 client 后调用 OpenAlex。
- 49–58 行：注册 metadata tool。
- 63 行：创建供应用依赖复用的进程内 server。

[app/literature/openalex.py](../app/literature/openalex.py) 是真正的外网边界：36–67 行构造 `/works` 查询与年份 filter；89–137 行处理 key、HTTP 状态、限流和重试；返回值经 `normalize.py` 映射为统一 `PaperMetadata`。

## 6. 持久化项目与研究请求

源码：[app/api/routes/projects.py](../app/api/routes/projects.py)

### 6.1 `POST /projects`

1. 第 39 行声明返回 201 和 `ProjectRecord`。
2. 第 41 行请求 JSON 由 `ProjectCreateRequest` 验证。
3. 第 42 行注入 `ProjectRepository`。
4. 第 44 行调用 `projects.create(name, request)`。
5. Repository 生成 UUID，将请求 JSON 写入 SQLite，随后查询并返回完整记录。

### 6.2 查询项目、论文、Trace 和进度

- 47–52 行：`GET /projects/{id}` 直接读取项目，不存在时 Repository 抛 `RecordNotFoundError`，统一转 404。
- 55–64 行：先确认项目存在，再分页查询论文；`limit` 只能是 1–100。
- 67–84 行：查询 Trace，可按事件类型和成功状态过滤。
- 87–94 行：聚合 trace 成功率、平均延迟和恢复次数。
- 97–104 行：查询 item-level 工作项。
- 107–114 行：聚合完成、失败、重试等进度指标。

### 6.3 `POST /projects/{id}/research`

这是当前最完整的持久化检索入口：

| 行号 | 执行过程 |
|---|---|
| 117–130 | FastAPI 注入 LLM、MCP client、checkpointer、四类 Repository 和 Skill Registry。 |
| 131 | 客户端没给 `run_id` 时生成 UUID。 |
| 132–145 | 把全部资源交给 `_execute`，并明确 `resume=False`。 |
| 194 | `start_run` 原子认领任务，避免相同 run 重复执行。 |
| 195 | 读取已保存论文，用于幂等快速返回。 |
| 196–204 | 如果未认领到任务，直接返回当前状态，不重复搜索或插入。 |
| 206 | 开始延迟计时。 |
| 207–214 | 写 `research_started` Trace。 |
| 215 | 创建 `LiteratureResearcher`，注入 Graph 需要的所有能力。 |
| 216 | 创建只负责路由的 Coordinator。 |
| 217–223 | 构建 `AgentTask`；注意 `task_type` 被固定为 `literature_search`。 |
| 224–225 | 进入错误边界并交给 Coordinator。 |
| 226–227 | 读取 Agent 输出并重新校验每篇 `RankedPaper`。 |
| 228–229 | 逐篇 upsert，重复 stable ID 不产生重复记录。 |
| 230 | 项目状态改为 completed。 |
| 231–239 | 写成功 Trace 和延迟。 |
| 240–247 | 返回 run ID、状态、阶段、论文数和是否恢复。 |
| 248–264 | 任意异常都会把项目标记 failed、记录裁剪后的错误和失败 Trace，然后重新抛出交给统一 handler。 |

### 6.4 `POST /projects/{id}/resume`

148–176 行与首次执行使用相同依赖和 `_execute`，区别只有 `resume=True`。真正的恢复来自三处：

1. `ProjectRepository.start_run(..., resume=True)` 控制合法状态转换。
2. LangGraph SQLite checkpointer 用 project ID 继续 Graph state。
3. `WorkItemRepository` 让已完成的逐查询工作项不重复调用外部工具。

### 6.5 Coordinator 与 LiteratureResearcher

[app/agents/coordinator.py](../app/agents/coordinator.py)：

- 22–30 行：收到任务先写 `agent_plan`。
- 31–35 行：把任务类型映射到 Agent 名称。
- 36–46 行：生成结构化 Handoff，只携带必要上下文摘要。
- 47–54 行：记录 handoff trace。
- 55–60 行：实际分派任务。
- 61–70 行：记录返回状态后将结果交还路由。

当前 `MultimodalAnalyst` 和 `ResearchBuilder` 在 [app/agents/specialists.py](../app/agents/specialists.py) 中仍返回 `unsupported`；项目研究接口只会走 LiteratureResearcher。

[app/agents/literature.py](../app/agents/literature.py) 的核心职责：

- `TracedLiteratureClient` 包装 MCP client，为每条查询建立 work item、执行幂等 claim、记录 tool trace，并在故障时保存失败状态。
- `LiteratureResearcher.run()` 按需加载 `systematic-search` 和 `paper-screening`，记录 skill trace，构建检索 Graph，并用 project ID/run ID 作为 checkpoint 配置。
- 成功后把 Graph 结果包装为 `AgentResult`；异常由上层 `_execute` 统一落项目失败状态。

## 7. PDF 上传与解析

### 7.1 `POST /projects/{id}/documents/import`

源码：[app/api/routes/evidence.py](../app/api/routes/evidence.py) 34–47 行：

1. 路径给出 `project_id`，multipart form 给出 `paper_id` 和 PDF 文件。
2. 第 43 行先确认项目存在。
3. 第 44 行最多读取“允许大小 + 1”字节；多出的 1 字节用于可靠判断超限。
4. 第 45 行 `WorkspaceManager.import_pdf_bytes()` 验证扩展名、PDF signature、大小和安全路径，计算 hash，写入项目 workspace/manifest。
5. 第 46 行同步解析 PDF，生成页面文本、截图、section、raster figure、caption 和 table candidate。
6. 第 47 行在 SQLite 中登记 `paper_id ↔ document_id`，返回 `LinkedDocument`。

[app/documents/workspace.py](../app/documents/workspace.py)：

- 25–40 行：验证 project ID 和相对路径，解析后的路径必须仍位于项目根目录，防止 `..` 越界。
- 75–102 行：验证上传字节、生成 document ID、保存 PDF、更新 manifest；失败时不留下不完整记录。
- 104–131 行：安全读取 document entry 和 manifest。

[app/documents/parser.py](../app/documents/parser.py)：

- 32–101 行：打开 PDF，逐页抽取文本和页面尺寸，渲染截图，发现标题、表格和嵌入图片，最后持久化 `document.json`。
- 103–111 行：按配置 DPI 渲染整页 PNG。
- 113–156 行：提取足够大的 raster image，落盘并尝试匹配最近 caption。
- 158–174 行：按空间距离寻找 caption。
- 176–185 行：通过 caption 关键词分类 architecture/result/ablation/other。
- 187–224 行：用字号和文本特征识别 heading，并构造 section 页码范围。
- 226–244 行：通过 PyMuPDF 表格发现能力保存 table candidate。

[app/documents/service.py](../app/documents/service.py) 是薄服务层：12–13 行委托解析；15–20 行回读持久化 JSON；22–27 行按页码查找；32–39 行列出或查找 figure。

Document MCP 在 [mcp_servers/document/server.py](../mcp_servers/document/server.py) 注册了同一服务的五个 tool。HTTP 上传接口当前直接调用 `DocumentService`，并没有跨 Document MCP 边界。

## 8. Evidence、摘要与跨论文比较

### 8.1 创建 Evidence

[app/api/routes/evidence.py](../app/api/routes/evidence.py)：

- 50–55 行：辅助函数把 DocumentService 和两个 Repository 组合成 `EvidenceBuilder`。
- 58–66 行：文本证据入口。
- 69–77 行：图片证据入口。
- 80–88 行：表格证据入口。

[app/evidence/builders.py](../app/evidence/builders.py) 执行可信性约束：

- 26–63 行：确认论文和文档关联；读取指定页；要求截图存在；要求 quote 确实出现在页面文本中；记录字符 span、section、source hash 后保存。
- 64–87 行：确认 figure ID 存在，图片文件存在，并保存 figure label/caption/path 定位。
- 88–115 行：确认 table candidate 存在并保存页码、label 和定位。
- 116–123 行：阻止跨项目或错误 paper/document 组合。
- 125–131 行：按页码推导 section。

`EvidenceRepository.create()` 还会用 locator key 保证同一定位幂等，并在 SQLite 保存完整 Evidence JSON。

### 8.2 查询与来源回读

- Evidence route 91–99 行：按 project + paper 分页查询。
- 102–108 行：按 project + evidence ID 查询，保证项目隔离。
- 111–119 行：先读取 Evidence，再由 `EvidenceVerifier` 回读原始页、图或表。

[app/evidence/verifier.py](../app/evidence/verifier.py) 会重新检查文件存在性和 SHA-256；文本证据还会检查保存的字符 span 是否仍等于 quote。因此数据库记录不能在源文件改变后继续冒充有效证据。

### 8.3 摘要与比较

- Evidence route 122–130 行：用 URL 中的 project/paper 覆盖请求体对应字段，再 upsert `PaperSummary`，避免客户端越权写到其他项目。
- 133–139 行：读取项目全部摘要，交给 `CrossPaperSynthesizer`。
- [app/evidence/synthesis.py](../app/evidence/synthesis.py) 45–69 行：按 method、dataset、metric、contribution、limitation 等固定列构造行；每个 cell 保留 evidence IDs。
- 同文件 71–94 行：可将比较对象渲染为 CSV 或 Markdown，但当前 comparison HTTP 接口只返回结构化 JSON。

## 9. 实验方案与 Human-in-the-loop

### 9.1 `POST /projects/{id}/experiment-proposal`

源码：[app/api/routes/experiments.py](../app/api/routes/experiments.py)

| 行号 | 执行过程 |
|---|---|
| 30–41 | 注册创建接口并注入模型、Evidence、Skill、checkpointer、项目、方案和 Trace。 |
| 42 | 确认项目存在。 |
| 43 | 创建 `ProposalBuilder` 并编译实验 Graph。 |
| 44 | 使用稳定的 `experiment:{project_id}` 作为审批 thread ID。 |
| 45–53 | 传入目标和已验证 evidence IDs，运行到 interrupt。 |
| 54 | 校验 Graph 输出的 proposal。 |
| 55 | 将 pending proposal 写入 SQLite。 |
| 56 | 项目状态切换为 waiting/human_approval。 |
| 57–72 | 记录 Skill 加载和人工审批暂停 Trace。 |
| 73 | 返回方案并声明 `interrupted=True`。 |

[app/experiments/builder.py](../app/experiments/builder.py)：

- 21 行：逐个从当前项目读取 Evidence，未知或跨项目 ID 会失败。
- 22 行：此时才完整加载 `experiment-design` Skill。
- 23–38 行：把 Skill 放入 system prompt，把目标和 Evidence index 放入 user prompt，要求输出 `ExperimentProposalDraft`。
- 39–46 行：模型引用的所有 Evidence 必须属于调用者提供的白名单。
- 47–56 行：生成 proposal ID、版本 1、pending 状态和时间戳。

[app/experiments/workflow.py](../app/experiments/workflow.py) 57–82 行构建 `build_proposal → human_approval → END`。第 66 行 `interrupt()` 会把状态写入 SQLite checkpoint 并暂停，而不是阻塞 HTTP 连接等待用户。

### 9.2 `POST .../decision`

Experiments route 84–115 行：

1. 读取数据库中的当前方案。
2. 第 97–100 行先比较客户端 version，旧页面提交会得到 409。
3. 用同一个 experiment thread 编译 Graph。
4. 第 103 行通过 `Command(resume=...)` 从 interrupt 恢复。
5. Graph 的 `apply_decision()` 处理 accept/modify/reject；modify 只能修改已存在的 experiment ID。
6. Repository 在事务中保存新版本和审批历史。
7. 更新项目阶段、写恢复 Trace，返回 `interrupted=False`。

## 10. Artifact 生成、列表与下载

### 10.1 创建 Markdown、CSV、Mermaid

源码：[app/api/routes/artifacts.py](../app/api/routes/artifacts.py)

- 24–25 行：组合 Workspace 与 ArtifactRepository。
- 28–37 行：Markdown 请求转给 `ArtifactService.markdown()`。
- 40–49 行：CSV 请求转给 `ArtifactService.csv()`。
- 52–59 行：Mermaid 请求转给 `ArtifactService.mermaid()`。

[app/artifacts/service.py](../app/artifacts/service.py)：

- 11–19 行：限定安全文件名和允许的 Mermaid 声明。
- 27–35 行：用 title/sections 生成 Markdown。
- 37–48 行：验证每行列数，生成带 UTF-8 BOM 的 CSV。
- 50–59 行：拒绝不支持或含 script、javascript、init、click 的 Mermaid。
- 70–89 行：生成 artifact UUID，经安全 workspace path 写文件，再记录相对路径、hash、大小和递增版本。
- 90–92 行：数据库写入失败则删除刚写的文件，避免孤儿产物。

### 10.2 列表和下载

- Artifacts route 62–69 行：先验证项目存在，再查询项目产物列表。
- 72–80 行：用 project + artifact ID 取记录，再读文件。
- ArtifactService 61–68 行：重新计算 SHA-256；文件被篡改时拒绝下载。
- Route 81–88 行：按类型设置 media type 和下载文件名。

Artifact MCP 的三个工具位于 [mcp_servers/artifact/server.py](../mcp_servers/artifact/server.py)，只是 `ArtifactService` 的协议包装；当前 HTTP routes 直接调用 service。

## 11. SQLite Repository 如何参与请求

### 11.1 Database

[app/db/database.py](../app/db/database.py)：

- 170–183 行：保存数据库路径，初始化时创建父目录、启用 migration，并按 schema version 顺序升级。
- 186–198 行：一个 migration 与 schema version 更新在同一事务中，失败回滚。
- 201–209 行：每次 Repository 操作用异步上下文打开连接、启用 foreign keys、设置 row factory，结束时关闭。

### 11.2 主要 Repository

[app/db/repositories.py](../app/db/repositories.py)：

- `ProjectRepository`（33–163）：创建项目、原子认领 run、状态转换和记录错误。
- `PaperRepository`（165–243）：按稳定论文 ID upsert，保存 rank/score/reason，分页查询。
- `TraceRepository`（245–367）：裁剪并保存 trace payload，支持过滤和指标聚合。

其他文件：

- [evidence_repositories.py](../app/db/evidence_repositories.py)：Document、Evidence、Summary 的项目隔离 CRUD。
- [proposal_repository.py](../app/db/proposal_repository.py)：方案版本、决定和 history 的事务更新。
- [artifact_repository.py](../app/db/artifact_repository.py)：同名 artifact 的递增版本和 metadata。
- [progress_repository.py](../app/db/progress_repository.py)：item claim/complete/fail 和恢复指标。

## 12. Streamlit 发出的请求

源码：[ui/app.py](../ui/app.py)

| 行号 | UI 行为 |
|---|---|
| 6 | API 地址默认 `http://127.0.0.1:8000`，可用环境变量覆盖。 |
| 9–17 | 通用 request helper 调用 HTTP、检查状态码并在页面显示错误。 |
| 21–23 | 创建五个 tab。 |
| 25–36 | New Research 只调用 `POST /projects`，目前不会自动调用 `/{id}/research`。 |
| 38–66 | Progress 查询项目、Trace 指标、工作项指标、时间线和进度列表。 |
| 68–73 | Evidence 需要手工输入 project ID 和 paper ID 后查询。 |
| 75–90 | Experiment 查询方案并提交 accept/modify/reject。 |
| 92–105 | Artifacts 列表、预览和下载。 |

## 13. 用一个真实调用顺序串起代码

当前版本要完成模块级演示，需要客户端显式编排：

```text
1. POST /projects
   → ProjectRepository.create

2. POST /projects/{id}/research
   → Coordinator
   → LiteratureResearcher
   → Literature Graph
   → Literature MCP
   → OpenAlex
   → PaperRepository

3. POST /projects/{id}/documents/import（逐篇上传 PDF）
   → WorkspaceManager
   → PDFParser
   → DocumentRepository

4. POST /projects/{id}/evidence/text|figure|table
   → EvidenceBuilder
   → EvidenceRepository

5. PUT /projects/{id}/summaries/{paper_id}
   → SummaryRepository

6. GET /projects/{id}/comparison
   → CrossPaperSynthesizer

7. POST /projects/{id}/experiment-proposal
   → ProposalBuilder + Skill + Ollama
   → LangGraph interrupt

8. POST /projects/{id}/experiment-proposal/decision
   → LangGraph resume
   → ProposalRepository

9. POST /projects/{id}/artifacts/markdown|csv|mermaid
   → ArtifactService
   → Workspace + ArtifactRepository
```

步骤 2 之后的步骤目前不会由 Coordinator 自动触发，这是理解当前代码边界的关键。

## 14. 调试一条请求的建议断点

以 `POST /projects/{id}/research` 为例，可以依次在这些位置下断点：

1. `RequestIDMiddleware.dispatch()`：确认请求进入以及 request ID。
2. `run_project_research()`：确认 FastAPI 解析和依赖注入结果。
3. `_execute()` 第 194 行：观察幂等 claim。
4. `Coordinator.run()`：观察 handoff。
5. `LiteratureResearcher.run()`：观察 Skill 和 checkpoint config。
6. `generate_queries/search/rank` Graph nodes：观察每个状态增量。
7. `LiteratureMCPClient.search_papers()`：确认跨 MCP tool 边界。
8. `OpenAlexClient._get_json()`：检查真实 HTTP、认证和重试。
9. `PaperRepository.upsert_ranked()`：检查最终持久化。
10. `RequestIDMiddleware.dispatch()` 返回处：确认响应状态和 header。

调试时推荐用显式虚拟环境解释器：

```powershell
cd D:\Agent\research-pilot
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

API 启动后可访问 `http://127.0.0.1:8000/docs`，Swagger 中的请求 Schema 来自路由参数和 Pydantic 模型，响应 Schema 来自每个接口的 `response_model`。
