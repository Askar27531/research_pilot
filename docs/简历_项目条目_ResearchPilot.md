# 简历条目：ResearchPilot（Agent 开发岗 · 企业需求对齐版）

> 依据企业 Agent 开发岗 JD 的 6 大技术面调整：编排框架/状态记忆、工具调用与 MCP、规划与多智能体协作、结构化输出与成本、安全护栏与可信、评测与工程化。
> 数字按当前代码核实：约 1.56 万行 Python（生产/UI/工具 ≈ 1.26 万行 / 97 个 .py + 测试 30 个文件 ≈ 3,017 行，145 个用例全绿）。时间与链接自行替换。

---

## 主条目（7 条 · 每条对齐一个 JD 考察面）

### ResearchPilot · 可恢复执行、证据可审计的多智能体研究系统（LLM Agent · 独立从 0 到 1 设计实现）

**Python / LangGraph / MCP(+OAuth 2.1) / FastAPI / Ollama / SQLite / PyMuPDF / Streamlit**

- **Durable Execution**：LangGraph **双图编排**——8 节点检索图 + 分析图（全文获取→逐篇精读→综合报告）；SQLite checkpoint + 幂等 work item，重启按原 run 续跑、失败重试不重复消耗 token。
- **MCP 工具层**：自研文献/文档 2 个 MCP Server + 网关纳管第三方 arXiv：capability 路由、契约校验、故障降级；HTTP 外放走 GitHub OAuth 2.1，真实客户端跑通授权。
- **多智能体协作**：Coordinator 将论文分析按研究问题/方法机制/实验与消融/局限适用性**正交分解**给 4 个专家 Agent，各专家以独立 Pydantic Schema、关键词路由与上下文/视觉预算做职责隔离，受限并发执行后在合并期对结论做**证据白名单二次校验**——既防角色越界，也防跨专家重复推断。
- **人机协同（HITL）**：关键决策点设为人在环门——检索策略先对用户可见，确认并选定论文前不触发全文下载与分析（成本、隐私与可解释性的共同边界）；分析流水线为第二张 LangGraph 图，**选文门与合成前高危复核门以 `interrupt()` 冻结在图节点**；选文/缺全文等人节点建模为显式 `hitl_event`（`waiting`+open 事件即 `waiting_for_human` 派生语义），worker 守卫保证等待期间任何唤醒都不绕过人工决定、人工动作 resolve 后按原断点续跑；分析运行中**每次 LLM 调用计量入库**，累计 token/视觉调用/时长越阈值即在幂等安全边界**自动暂停（成本门槛暂停，防长任务失控）**，人工决定继续（放行并重新计量）或保持暂停，续跑零重算；自动复核（含视觉两阶段判定）**永不覆盖人工裁决**，确认/存疑/排除以人工为最终准。
- **结构化输出与成本**：LLM 输出全经 Pydantic 强校验，带超时/预算上限与截断重试；幂等 + 本地推理控 token 成本。
- **防幻觉护栏**：结论 supported/inference 分级 + 证据白名单，无证据自动降级；视觉复核两阶段防偏。
- **工程与评测**：约 1.56 万行 Python（含测试）；30 个测试文件（约 3,000 行、145 个用例）+ 人工金标评测 + 故障注入。

---

## 超短版（3 条 · 一页简历）

### ResearchPilot · 可恢复执行、证据可审计的多智能体研究系统（个人项目）

**Python / LangGraph / MCP(+OAuth) / FastAPI / Ollama / SQLite / PyMuPDF**

- LangGraph + checkpoint + 幂等工作项实现 durable execution：重启续跑、重试不重复耗 token；4 专家分工，选文设 HITL 门。
- 自研 MCP 网关纳管 3 个 Server（文献/文档/第三方 arXiv）：capability 路由、契约校验、故障降级；HTTP 走 GitHub OAuth 2.1，已真实客户端验证。
- 防幻觉做成代码约束：结论分级 + 证据白名单，无证据自动降级；约 1.56 万行 Python（含测试）、30 个测试文件（145 个用例）+ 人工金标评测。

---

## JD 技术需求 ↔ 本项目证据（面试前自查用）

| 企业 JD 考察点 | 项目对应证据 | 面试怎么讲 |
|---|---|---|
| Agent 框架熟练度 | LangGraph **双图**：8 节点检索图 + 分析图（获取/精读/复核门/合成） | 能画出两张图的节点/边与两个 interrupt 门、说出每节点职责 |
| 状态/记忆设计 | checkpoint + 业务表双层持久化、input_hash 幂等 | 短期状态=图状态/run，长期=SQLite 项目库；跨重启恢复 |
| 工具调用/MCP | 3 个 MCP Server（自研文献/文档 + 第三方 arXiv）经能力网关统一纳管：capability 路由、契约校验、健康降级、调用观测 | 逐个报出 Server 的工具清单与降级关系；远程 HTTP 走 OAuth 2.1 |
| 远程服务鉴权/OAuth | GitHub OAuth 2.1 授权码 + PKCE（FastMCP 代理）：授权服务器发现、动态客户端注册、每请求 token 校验、加密持久化，真实客户端跑通 | 讲清"MCP 远程 Server 必须 OAuth"、授权码 + PKCE 流程、为何动态注册、状态如何落盘 |
| 任务分解/多智能体 | 4 专家 Agent 分工 + schema/预算隔离 + 合并 | 讲分工依据、为何受限并发、结果如何合并 |
| HITL 时机 | 选文门、缺全文门（均以 `hitl_event` 呈现 waiting_for_human）、成本门槛自动暂停、人工复核不覆盖 | 哪些操作必须人工确认、为什么放在选文点；open 事件期间 worker 守卫如何保证不被绕过 |
| 结构化输出/上下文 | Pydantic 强校验 + 截断 JSON 补全重试 | 输出治理 = schema + 预算 + 重试闭环 |
| 成本控制 | 幂等不重复耗 token、本地 Ollama、逐调用计量 + 成本门槛自动暂停（`llm_usage` + `budget_windows`） | token 成本是设计约束而非事后优化：先计量、超阈值自动暂停在安全边界等人裁决 |
| 安全 | 不透明令牌、路径沙箱、SHA-256 重校验 | 纵深防御：权限白名单思路 + 资源审计 |
| 防幻觉/护栏 | supported/inference + 证据白名单 + 视觉两阶段复核 | 护栏是确定性约束，可单测，非 prompt 祈使 |
| 评测/工程化 | 金标离线评测、故障注入、30 测试文件 | 用"评测集 + 确定性测试"替代主观验收 |

---

## 使用建议

- 投不同细分方向调序：Agent 平台/框架岗把第 1、2 条放最前；智能体应用/业务岗把 3、4 条提前；安全合规岗突出第 5 条。
- 标题行技术栈按 JD 增删（如企业用 LangSmith/LangFuse 可注明"有评测与可观测意识，项目内以 DB 任务状态 + trace 记录实现"）。
- 别写代码里没有的数字；若金标评测跑出指标，补在评测条最加分。

---

## OAuth 2.1：已落地内容与面试口径（写进简历前先看）

**已实现并真实验证（不是规划）：**
- `mcp_servers/oauth.py`：auth 工厂（`none`/`static`/`github`）+ `GitHubProvider`（FastMCP 内置 OAuth 代理，GitHub OAuth App 为 IdP，授权码 + PKCE，scope=`user`）；CLI `--auth github` + `--public-base-url`，凭据/参数从项目 `.env` 读取（`MCP_GITHUB_CLIENT_ID/SECRET`、`MCP_OAUTH_BASE_URL`、`MCP_OAUTH_REQUIRE_CONSENT`、`MCP_OAUTH_DATA_DIR`）。
- 验证记录：授权服务器发现端点（`/.well-known/oauth-authorization-server` → issuer、`/authorize`、`/token`、`/register` 动态注册、PKCE `S256`）在线；**无令牌调用返回 401**；**用真实 MCP 客户端（FastMCP Client `auth="oauth"`）走完授权码 + PKCE 全流程并 `tools/list` 成功**；`scripts/run_oauth_probe.py` 一键复验。
- 状态管理：客户端注册/refresh token 加密持久化在 `data/oauth-proxy/`（密钥由 client secret 确定性派生），服务重启后已授权客户端无需重登；token 每请求经 GitHub API 校验（300s 缓存）。
- 测试：`tests/unit/mcp/test_oauth.py` 11 个离线用例（模式选择、必填校验、非回环 https 强制、token 校验成功/401/scope 不足/网络故障）。

**面试口径（不吹版）：**
- 为什么做：MCP 规范要求经 HTTP 暴露的远程 Server 使用 OAuth 2.1；主流客户端（Claude Desktop / Cursor / Claude Code）对远程 MCP 只认标准 OAuth——这是"把自研工具外放给任意标准客户端"的前提，也是"本地 demo → 可对外服务"的第一道坎。
- 价值一句话："我的 MCP Server 不只本地 stdio 可用——HTTP 外放时走完整 OAuth 2.1（授权码 + PKCE + 动态客户端注册），并拿真实 MCP 客户端跑通过完整授权流程。"
- 诚实边界：单用户/自托管、GitHub 单一 IdP；**未做**：多租户、用户白名单、A2A、网关侧客户端 OAuth、通用 OIDC 多 IdP（被追问按 docs/MCP_OAUTH.md「常见问题/边界」回答）。

---

## MCP 专项：简历各位置怎么写（按空间取用）

**技术栈行（3 档粗细）**
- 最简：`MCP`
- 常用：`MCP（FastMCP Server + 统一 Client 网关）`
- 投 Agent 平台/工具链岗：`MCP 协议 / FastMCP（Server+Client 双角色）/ stdio+Streamable HTTP 传输`

**定位句/项目概述（一句）**
> 工具层基于自研 MCP 能力网关：Agent 只调用稳定的能力名（如 `literature.search.arxiv`），网关负责工具发现、Schema 契约校验、健康路由与故障降级——换 Server、换传输方式都不改动 Agent 代码。

**一行子弹（空间最省）**
> 自研 MCP 能力网关（Server+Client 双角色）：3 个学术源按 capability 路由接入，启动时工具发现 + JSON Schema 契约校验，故障自动降级备用服务器，调用级路由/延迟/trace 可观测。

**两到三行子弹 = 主条目第 2 条**（双角色、三传输、capability 路由、契约校验、降级、观测六要素全覆盖，见上文）。

**面试怎么讲（每个点对应真实实现）**
1. **角色定位**：MCP 是标准化"模型 ↔ 工具/数据"的协议；项目同时是 Server 提供方（自研文献/文档工具，可被外部宿主接入）与 Client 网关（统一消费自研 + 第三方 Server）。
2. **为什么不自接 3 个 API**：解耦 + 可插拔——第三方 arXiv Server（`uvx arxiv-mcp-server` 一行接入，Apache-2.0）与自研多源 Server 互为降级；面向能力名编程，换实现不改 Agent。
3. **契约校验**：启动 `discover()` 时 list_tools 并核对每个工具的 inputSchema 与配置声明的能力绑定，缺失/非法即标记不健康（契约不符 fail-fast）；调用前再按 Schema 校验必填与参数类型。
4. **降级语义边界**：只有连接失败/超时/服务端故障（临时错误）才切换候选；参数错误、契约不匹配直接失败不回退——避免用"降级"掩盖 bug。
5. **三种传输与安全**：in-process（同进程 FastMCP，用 per-server 锁避免并发开双 lifespan）、stdio（仅透传环境变量白名单）、Streamable HTTP（远程强制 HTTPS，回环除外；非回环独立 Server 默认要求 Bearer，可选 `--auth github` 走 OAuth 2.1）；文档工具做路径沙箱（拒绝路径穿越、越权 symlink）。
6. **可观测**：每次调用落一条事件（capability/server/tool/transport/latency/success/fallback/project_id/trace_id），`/mcp/status` 暴露当前路由（active/fallback/using_fallback/degraded）与最近 200 次调用。
7. **与 Function Calling / LangGraph 的关系**：Function Calling 是模型层能力，MCP 是工具协议层——网关把工具 Schema 显式化、可离线测试；LangGraph 节点经网关调能力并做结果适配，规范化后进入图状态。

**避坑（不要主动吹）**
- **已实现**：HTTP 暴露的自研 MCP Server 支持 GitHub OAuth 2.1（授权码 + PKCE，FastMCP `GitHubProvider` 代理，见 docs/MCP_OAUTH.md）；static Bearer 保留为自托管简化选项。**未实现、勿吹**：A2A、多租户/远程生产网关、网关侧客户端 OAuth（config 的 http transport 仍走 auth_env 静态头）、通用 OIDC 多 IdP。
- 别写"3 个数据源都是第三方 MCP Server"：第三方只接了 arXiv，OpenAlex/Crossref 是自研文献 Server 内的 provider；第三方 arXiv 仅作为高优先级实现、可整体降级到自研多源。
- docs/MCP_GATEWAY.md 里描述的 artifact / Paper Intelligence 工具集（submit_paper / start_paper_analysis 等）与当前 CLI/代码不一致（未落地），不要作为简历或面试素材。

---

## MCP 全景速览（把"不止一个 MCP"讲清楚，面试前背熟）

**一句话总结**：项目里 MCP 是"3 个 Server + 1 个统一能力网关"——自研 2 个（文献检索、PDF 文档），纳管 1 个第三方（arXiv）；Agent 与业务代码不直接碰任何一个 Server，只通过 capability 客户端走网关。

**Server ① 自研 Literature Server（`researchpilot-literature`）**
- 工具：`search_papers`（内部聚合 OpenAlex/Crossref/arXiv 三个 provider，返回归一化 SearchResult）、`get_paper_metadata`（OpenAlex ID / DOI）
- 形态：进程内注册 + CLI `researchpilot-mcp --server literature` 可 stdio/HTTP 独立部署
- 服务的 capability：`literature.search.multisource`、`literature.metadata`，并作为 arXiv 搜索的降级位

**Server ② 自研 Document Server（`researchpilot-document`）**
- 工具：`parse_document`、`get_page`、`get_document_structure`、`extract_figures`、`get_figure`（参数均带 project 作用域）
- 形态：进程内注册 + CLI 独立部署（同上）；HTTP 非回环默认要求 Bearer Token，也可 `--auth github` 走 OAuth 2.1（见 docs/MCP_OAUTH.md）；文档路径沙箱
- 服务的 capability：`paper.parse`

**Server ③ 第三方 arXiv Server（`external-arxiv`）**
- 来源：`uvx arxiv-mcp-server==0.7.2`（Apache-2.0），stdio 传输，配置文件声明接入，可整体插拔
- 工具：`search_papers`、`get_abstract`
- 服务 capability：`literature.search.arxiv`（priority 10 首选）、`literature.metadata.arxiv`——不可用时网关自动降级到自研 Literature Server（priority 100）

**统一能力网关 + capability 客户端**
- 业务只依赖 `LiteratureCapabilityClient` / `DocumentCapabilityClient`（稳定能力名），`CapabilityRouter` 按（启用, 健康, priority, 配置顺序）选路，临时故障自动降级；异构返回经 adapter 归一化后才进入 LangGraph 状态
- 自研 Server 双形态的意义：同一份 FastMCP 定义既能被本应用进程内消费，也能作为独立 MCP Server 暴露给任何外部 MCP 客户端（如 Claude Desktop / Cursor）——一个代码库、两种消费方式

**一句话叙事模板（面试开场用）**
> "这个项目的 MCP 不止一个：我自研了文献检索和 PDF 文档两个 MCP Server，还通过统一能力网关纳管了第三方 arXiv MCP Server。Agent 永远不直接调 Server——只调 capability 客户端，路由、契约校验、健康检查和降级全部收敛在网关一层。"

---

## 术语速览：A2A / OAuth 授权码 / 多租户远程网关（面试被问到能讲清）

**A2A（Agent2Agent Protocol，Agent 间通信协议）**
- 是什么：让"Agent ↔ Agent"互操作的开放协议（Google 提出，2025 年与 MCP 一同进入 Agentic AI Foundation）。MCP 是 Agent 的"手/工具"，A2A 是 Agent 的"同事/对话"。
- 关键概念：Agent Card（能力自述/发现）、Task（任务生命周期：submitted → working → input-required → completed/failed）、消息与工件流式传输；认证复用企业标准（OAuth 2.0/OpenID Connect）。
- 常见面试题"MCP 与 A2A 区别"：MCP 连接模型与工具（单 Agent 内部），A2A 连接 Agent 与 Agent（跨系统协作）；二者互补。

**OAuth 2.0 授权码模式（Authorization Code Flow）**
- 是什么：最常用的服务端授权流程——① 客户端重定向用户到授权服务器登录并授权；② 授权服务器回调返回一次性授权码（前端通道）；③ 客户端用授权码 + client secret 在后端换 access token / refresh token（秘密不暴露给浏览器）；④ 凭 token 调用资源。
- 为什么和 MCP 强相关：MCP 规范（2025-03-26 版）要求**经 HTTP(S) 暴露的远程 MCP Server 必须用 OAuth 2.1**（含授权码 + PKCE、动态客户端注册）——"对外提供远程 MCP"基本等于要接授权码流。
- 项目现状：**已实现**——`researchpilot-mcp --auth github` 让 HTTP 暴露的自研 Server 走 OAuth 2.1 授权码 + PKCE（FastMCP `GitHubProvider` 代理 GitHub OAuth App：发现端点、动态客户端注册、每请求 `GitHubTokenVerifier` 校验，见 docs/MCP_OAUTH.md）；static Bearer（`MCP_AUTH_TOKEN` + hmac 恒时比较）保留为自托管简化选项。**仍未做**：网关侧客户端 OAuth、多租户、通用 OIDC 多 IdP。

**多租户远程网关（Multi-tenant Remote Gateway）**
- 是什么：把 MCP 能力网关部署成公网/企业中心的远程 HTTP 服务，同时服务多个租户（公司/团队/用户），每个租户的工具、配置、数据、权限互相隔离。
- 与"本地单机单用户网关"的差距：每租户认证授权与 RBAC（SSO/OAuth/API Key + 作用域）、租户级工具白名单与数据隔离、限流与配额/计费、集中观测与审计合规、高可用与横向扩展、密钥与 secrets 管理、加密。
- 项目现状：本地优先、单机单用户、stdio/in-process/回环 HTTP——**不是多租户**。

**三者为何常被一起提**：一个"生产化 MCP 平台"通常 = 远程暴露（→OAuth 授权码）+ 多 Agent 协作（→A2A）+ 服务多用户/多团队（→多租户）。面试官用它们探测"你是在本地 demo 层面，还是想过企业级边界"。

**被追问时的标准答法（诚实 + 演进路径，不吹实现过）**
> "A2A 和多租户我没有落地，但 OAuth 授权码我已经接上了：自研 MCP Server 以 HTTP 暴露时走 OAuth 2.1 授权码 + PKCE（FastMCP GitHubProvider 代理 GitHub OAuth App），配了授权服务器发现、动态客户端注册和 GitHub token 每请求校验。和真正企业级网关的差距主要在：多租户隔离与 RBAC/配额、公网 TLS 部署与统一审计，跨系统协作再暴露 A2A。我的能力路由、契约校验、降级和调用观测这层可以直接复用。"
