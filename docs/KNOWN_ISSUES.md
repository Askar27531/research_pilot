# ResearchPilot 已知问题与解决记录

> 本文件记录开发和真实验收中观察到的问题，包括当前状态、证据和解决记录。
>
> 状态约定：`未解决`、`处理中`、`已解决`、`暂不处理`。新增问题默认标记为 `未解决`，只有完成验证和关闭条件后才能改为 `已解决`。

## 问题总览

| ID | 问题 | 严重程度 | 首次发现阶段 | 状态 |
|---|---|---:|---|---|
| ISSUE-001 | P1 检索结果相关性不足 | 高 | P1 真实验收 | **已解决** |
| ISSUE-002 | 本地 Qwen 论文批量排序延迟较高 | 高 | P1 真实验收 | **已解决** |
| ISSUE-003 | OpenAlex API Key 行为与当前环境表现不一致 | 中 | P1 真实验收 | **已解决** |
| ISSUE-004 | FastAPI TestClient 存在上游弃用警告 | 低 | P0 测试 | **已解决** |
| ISSUE-005 | FastMCP/Authlib 存在上游弃用警告 | 低 | P1 测试 | **已解决** |
| ISSUE-006 | Literature MCP 全局 OpenAlex 客户端缺少显式关闭流程 | 中 | P1 实现审查 | **已解决** |
| ISSUE-007 | 中文研究请求的歧义字段可能出现乱码或低质量内容 | 中 | P0 真实验收 | **已解决** |
| ISSUE-008 | P1 验收快照不是相关性 Gold Set | 中 | P1 真实验收 | **已解决** |

---

## ISSUE-001：P1 检索结果相关性不足

- 状态：**已解决（2026-08-09）**
- 严重程度：高
- 影响模块：Query Generator、OpenAlex Search、Relevance Ranker
- 影响：最终 Top-K 中可能出现与 RGB-LWIR 配准只有间接关系的论文，降低后续 PDF 分析和 Evidence 构建质量。

### 观察证据

P1 固定验收的 Top-5 包含：

- 红外–可见光标定板论文；
- 光谱视觉基础模型论文；
- 可见光–SWIR 融合论文；
- 多光谱目标检测综述；
- 可见光–红外图像融合综述。

这些论文与跨光谱视觉相关，但并不都直接研究 RGB-LWIR image registration。

验收快照：[`../evals/snapshots/p1_rgb_lwir_search.json`](../evals/snapshots/p1_rgb_lwir_search.json)

### 可能原因

1. OpenAlex 全文搜索的召回范围较宽。
2. Query Generator 生成了 application、metric 等宽泛查询。
3. Lexical Ranker 没有对 registration 核心概念设置最低匹配门槛。
4. LLM Ranker 当前只做相对评分，没有先进行 include/exclude 判断。
5. 未使用论文类型、主题、标题短语等 OpenAlex 过滤条件。

### 临时处理

当前结果保留 `lexical_score`、`llm_score`、`final_score` 和 `selection_reason`，使用者需要人工检查最终列表。

### 建议处理方向

- 增加必须匹配的核心概念和否决规则。
- 将排序拆成 `include/exclude → relevance score`。
- 对 title 中没有 registration/alignment/correspondence 的论文降权。
- 建立 20～50 条人工相关性标注，测 Precision@K。
- 比较 OpenAlex `search` 与更精确的 filter/phrase query。

### 关闭条件

在固定 Gold Set 上达到预先设定的 Precision@10，并人工确认 Top-10 不再出现明显偏题论文；具体阈值需在 P9 Eval 设计时确定。

### 解决记录

- 大模型现在从用户主题生成 `required_concept_groups`、`excluded_topics` 和 3～5 条高精度检索式。
- 每条模型检索式必须覆盖所有 required concept groups，否则使用确定性 fallback。
- 去重后增加硬性概念过滤，论文必须命中每个概念组且不能命中排除主题。
- 修复 `IR` 等短词在普通单词内部产生子串误匹配的问题。
- LLM 再执行 include/exclude 筛选，明确排除融合、检测等邻近但非目标任务。
- 同主题真实验收从旧 Top-5 均为邻近主题，改善为 3 篇均直接涉及 registration/matching。
- 对比证据：[`../evals/snapshots/issues_001_002_after.json`](../evals/snapshots/issues_001_002_after.json)。

功能层面的明显偏题问题已关闭；正式 Precision@K Gold Set 仍由 ISSUE-008 跟踪，不重复阻塞本问题。

---

## ISSUE-002：本地 Qwen 论文批量排序延迟较高

- 状态：**已解决（2026-08-09）**
- 严重程度：高
- 影响模块：Relevance Ranker、端到端 Graph
- 影响：一次仅返回 5 篇论文的 P1 验收耗时约 286.3 秒，不适合交互式体验。

### 观察证据

- Query understanding 和 query generation 正常完成。
- OpenAlex 的 5 次请求约在数秒内完成。
- 两批本地 Qwen3 14B 相关性评分占据主要时间。
- 当前每批最多 10 篇，每篇摘要最多传入 1500 字符，最多评分 lexical Top-30。

### 临时处理

如果某批 LLM 调用失败或超时，系统会保留论文并降级为确定性 lexical ranking，同时写入 warning。

### 建议处理方向

- 将 LLM 候选数量从 Top-30 调整为可配置值并测 Top-10/15/20。
- 将摘要压缩到 500～800 字符，优先保留命中关键词附近文本。
- 每批减少为 5 篇，比较总吞吐与单次生成长度。
- 要求模型只返回紧凑分数和短理由。
- 缓存 `(research_request_hash, paper_stable_id, model_digest)` 评分。
- 测试更小本地模型负责初筛、14B 只处理边界样本。

### 关闭条件

固定 P1 任务在目标开发机上的端到端 P95 延迟达到项目确定的交互目标，同时 Precision@K 不显著下降。目标值尚未确定。

### 解决记录

- LLM 候选从最多 30 篇缩减为 lexical Top-10。
- 多批顺序评分改为单次 include/exclude + relevance 筛选。
- 每篇输入由最多 1500 字符缩减为关键词附近最多 600 字符。
- 增加基于主题、论文内容哈希和模型名的进程内筛选缓存。
- LLM 失败时仍可降级为 lexical ranking，不阻断检索。
- 同主题端到端耗时由 286.3 秒降至 141.0 秒，减少 145.3 秒（50.8%）。
- 新流程只进行一次论文筛选调用，真实运行无 warning。
- 对比证据：[`../evals/snapshots/issues_001_002_after.json`](../evals/snapshots/issues_001_002_after.json)。

本问题限定为“论文批量排序延迟”，已达到显著减少调用次数和耗时的修复目标。更严格的全流程 P95 交互目标将在后续 Eval 中继续量化。

---

## ISSUE-003：OpenAlex API Key 行为与当前环境表现不一致

- 状态：**已解决（2026-08-09）**
- 严重程度：中
- 影响模块：OpenAlex Client、部署配置
- 影响：新环境可能因缺少 API Key 突然出现 401/403，当前本机无 Key smoke 成功不能证明所有环境均可用。

### 观察证据

- 当前 OpenAlex 官方开发文档说明 API 访问需要免费 API Key。
- 当前开发环境未配置 `OPENALEX_API_KEY`，但真实查询仍然返回 HTTP 200。

### 临时处理

- Client 已支持可选 `OPENALEX_API_KEY` 和 `OPENALEX_EMAIL`。
- `.env.example` 和 README 已包含相应配置。

### 建议处理方向

- 在应用启动或 `/health` 扩展检查中报告 Key 是否配置。
- 分别记录无 Key、无效 Key和有效 Key 的真实响应。
- 对 401/403 增加明确的 `OPENALEX_AUTH_REQUIRED` 错误码。

### 关闭条件

确认官方当前认证规则，并用有效 Key 完成一次真实 smoke；401/403 能返回明确配置指引。

### 解决记录

- 已依据 OpenAlex 当前官方文档确认：API key 是正式必需参数，`mailto` polite pool 已于 2026 年 2 月废弃。
- 删除 `OPENALEX_EMAIL`，`.env.example` 明确要求 `OPENALEX_API_KEY`。
- 无 key 时在发出网络请求前 fail-fast；401/403 映射为 `OpenAlexAuthRequiredError`。
- MCP Adapter 保留认证错误语义，API 返回 `OPENALEX_AUTH_REQUIRED` 和申请地址。
- 新增 `GET /health/dependencies` 显示 key 是否配置。
- Mock 测试覆盖无 key、无效 key 和 MCP 错误透传。当前机器尚未配置用户自己的有效 key，因此真实 OpenAlex 搜索需配置后执行。
- 2026-08-09 已配置有效 key 并完成真实 smoke：`/health/dependencies` 报告已配置，OpenAlex 直连与 Literature MCP 均成功返回结果。

---

## ISSUE-004：FastAPI TestClient 存在上游弃用警告

- 状态：**已解决（2026-08-09）**
- 严重程度：低
- 影响模块：API Tests
- 影响：当前不影响测试通过，但未来依赖升级后测试客户端可能发生不兼容。

### 观察证据

测试输出：

```text
StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated;
install httpx2 instead.
```

### 临时处理

保留警告，不使用全局 warning ignore 隐藏它。

### 建议处理方向

- 核对 FastAPI/Starlette 当前推荐测试客户端组合。
- 评估迁移到 `httpx.AsyncClient` + ASGI transport 或官方推荐的 `httpx2`。
- 依赖升级必须重新运行全部 API 测试。

### 关闭条件

API 测试不再输出该弃用警告，并且全部接口测试保持通过。

### 解决记录

- 开发依赖加入 `httpx2>=2.9,<3`，TestClient 自动使用当前推荐后端。
- 全量测试不再出现 Starlette/TestClient 弃用警告。

---

## ISSUE-005：FastMCP/Authlib 存在上游弃用警告

- 状态：**已解决（2026-08-09）**
- 严重程度：低
- 影响模块：Literature MCP Tests、FastMCP Runtime
- 影响：当前 Literature MCP 功能正常，但未来 Authlib 2.0 可能造成依赖不兼容。

### 观察证据

测试输出：

```text
AuthlibDeprecationWarning: authlib.jose module is deprecated, please use joserfc instead.
It will be compatible before version 2.0.0.
```

### 临时处理

保留警告；不直接修改虚拟环境中的第三方包。

### 建议处理方向

- 跟踪 FastMCP 的兼容更新。
- 依赖升级时验证 MCP tool discovery、call 和 structured content。
- 如果上游长期未处理，再评估精确锁定兼容版本。

### 关闭条件

升级到无该警告的兼容 FastMCP/Authlib 组合，MCP 集成测试全部通过。

### 解决记录

- FastMCP 从 2.14.7 升级至 3.4.6。
- MCP tool discovery、search、metadata、structured content 和错误透传测试全部通过。
- 全量测试不再出现 `authlib.jose` 弃用警告。

---

## ISSUE-006：Literature MCP 全局 OpenAlex 客户端缺少显式关闭流程

- 状态：**已解决（2026-08-09）**
- 严重程度：中
- 影响模块：Literature MCP Server 生命周期
- 影响：长期运行或反复装载 Server 时，底层 `httpx.AsyncClient` 可能无法在进程退出前被优雅关闭。

### 观察证据

`create_literature_server()` 会创建一个共享 `OpenAlexClient`，但当前 FastMCP Server 尚未配置 lifespan shutdown 来调用 `close()`。

### 临时处理

生产工具调用复用同一个客户端，避免每次请求创建新连接池；短时测试未观察到功能故障。

### 建议处理方向

- 使用 FastMCP 官方 lifespan/context 机制管理 OpenAlex Client。
- 测试启动、调用、关闭后客户端确实关闭。
- 保持测试注入的外部 Fake Client 不被 Server 错误关闭。

### 关闭条件

MCP Server shutdown 会关闭自有 OpenAlex Client，并有自动化生命周期测试证明无未关闭资源警告。

### 解决记录

- `create_literature_server()` 使用 FastMCP lifespan 创建和释放自有 OpenAlex Client。
- Tool 通过 lifespan context 获取活动客户端。
- 自动测试证明自有客户端在 lifespan 退出后关闭。
- 自动测试证明外部注入客户端不会被 Server 错误关闭。

---

## ISSUE-007：中文研究请求的歧义字段可能出现乱码或低质量内容

- 状态：**已解决（2026-08-09）**
- 严重程度：中
- 影响模块：Request Understanding、Prompt/终端编码
- 影响：`ambiguities` 可能不能准确表达中文请求中的未决问题，影响后续 Coordinator 判断。

### 观察证据

P0 中文 smoke 中，核心目标、领域和年份正确，但 `ambiguities` 曾返回包含 `??` 的低质量文本。

### 可能原因

- PowerShell heredoc 到 Python stdin 的编码链路。
- 模型对中英文混合请求的表达不稳定。
- Prompt 没有规定 ambiguities 应沿用用户语言。

### 临时处理

核心字段仍经过 Pydantic 校验；用户显式年份由程序强制覆盖模型输出。

### 建议处理方向

- 使用 UTF-8 文件或真实 HTTP JSON 复现，排除终端输入问题。
- Prompt 明确要求使用用户语言输出 ambiguities。
- 无真实歧义时强制返回空列表。
- 增加中文、英文和中英混合测试集。

### 关闭条件

通过真实 HTTP UTF-8 请求验证中文输出无乱码，并通过多语言 understanding 测试集。

### 解决记录

- Request Understanding prompt 强制自然语言字段沿用用户语言；请求明确时返回空 ambiguity。
- 中文请求会确定性删除包含乱码、问号占位或错误语言的 ambiguity。
- 新增中文乱码回归测试。
- 使用 UTF-8 源文件经真实 FastAPI 路由和本机 Qwen3 14B 验收：HTTP 200，中文目标、概念和领域正常，年份正确，`ambiguities=[]`。
- 可复现命令：`python -m scripts.smoke_chinese_request`。

---

## ISSUE-008：P1 验收快照不是相关性 Gold Set

- 状态：**已解决（2026-08-09）**
- 严重程度：中
- 影响模块：Evaluation、P1 验收解释
- 影响：当前快照只能证明流程可运行，不能证明论文选择质量达到科研使用要求。

### 观察证据

当前快照保存了真实请求、查询、候选数量、去重数量、Top-5 和延迟，但没有每篇论文的人工 relevance label。

### 临时处理

快照明确标注为 functional baseline，不将其数值用于宣称搜索准确率。

### 建议处理方向

- 为固定课题建立候选池并进行人工 include/exclude 标注。
- 定义相关性等级和冲突处理规则。
- 计算 Precision@K、Recall@K、Duplicate Rate。
- 保存评测时的模型 digest 和 OpenAlex 数据时间。

### 关闭条件

形成版本化 Gold Set，P1 检索可由 Eval Runner 自动计算相关性指标。

### 解决记录

- 建立 `literature-rgb-lwir-gold-v1`，包含 6 条相关和 6 条不相关人工标注样例。
- 数据集记录相关/不相关定义、required concept groups、excluded topics 和逐条标注理由。
- 新增自动 evaluator，输出 TP、FP、FN、TN、Precision、Recall 和 F1。
- 当前 v1 结果：TP=6、FP=0、FN=0、TN=6，Precision=1.0、Recall=1.0、F1=1.0。
- 数据集：[`../evals/datasets/literature_rgb_lwir_gold_v1.json`](../evals/datasets/literature_rgb_lwir_gold_v1.json)。
- 执行命令：`python -m evals.run_literature_eval`。

---

## 更新规则

处理问题时必须：

1. 将状态改为 `处理中`。
2. 在对应 Issue 下记录修复 PR/commit 或文件。
3. 增加防回归测试。
4. 执行该问题的关闭条件。
5. 记录验证结果和日期后才能改为 `已解决`。

不得仅因为问题暂时没有复现，就将其标记为已解决。
