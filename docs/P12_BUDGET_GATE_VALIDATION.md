# P12 · 成本门槛暂停（M3）验证记录

> 落地范围：方案三「成本门槛暂停」按 `docs/HITL升级计划_事件化_决策台_成本门槛.md` 的软性监督点语义实现。
> 一句话：**每次 LLM 调用计量入库，越阈值即自动停在幂等安全边界等人裁决，继续=放行并重新计量；防止长任务失控。**

## 目标（简历口径，全部为真）

- 累计调用量（token / 视觉调用 / 运行时长）跨过配置阈值 → 分析在**安全边界**自动暂停（`analysis_paused`，非失败）。
- 暂停原因可审计：`projects.pause_reason='budget_gate'`、trace `analysis_paused{pause_reason}`、`budget_windows.snapshot_json` 存触发时用量快照。
- 人工裁决后断点续跑：`resume_analysis` 关闭门并重新基线窗口，已完成单元零重算（沿用 input_hash / visual_regions / paper_analyses 幂等）。
- 手动暂停不受影响，且优先级高于自动门槛。

## 数据层（V15，`app/db/database.py`）

| 对象 | 说明 |
|---|---|
| `projects.pause_reason` | `user` / `budget_gate`（UI 区分两种暂停） |
| `llm_usage` | append-only 计量台账：id/project_id/phase/kind(text\|vision)/model/prompt_tokens/completion_tokens/latency_ms/created_at；随项目删除级联 |
| `budget_windows` | 每项目一行：baseline_tokens/baseline_vision（窗口起点）、since（窗口开始）、open（门是否开着）、opened_at、snapshot_json（开门瞬间用量） |

## 计量（`app/llm/base.py` + `ollama.py`）

- `LLMCallUsage` dataclass + `UsageSink` 类型；`OllamaProvider(usage_sink=…)`。
- `_request` 成功返回前调用 sink（`_meter`），text/vision 由请求是否走 vision 模型判定；结构化重试的每次真实 HTTP 消耗都入账（无效 JSON 也烧了 token，如实计量）。
- sink 失败仅记日志，绝不影响调用（计量是可观测性，不是门）。
- `execute_job` 每次任务注入带 `(project_id, job_type)` 上下文的 sink → `ResearchDataRepository.record_usage`。

## 门槛与检查点（全部为既有幂等边界）

- 配置：`budget_gate_enabled`(默认 True)、`budget_gate_tokens`(600k)、`budget_gate_vision_calls`(60)、`budget_gate_minutes`(45)。
- `ResearchDataRepository.budget_pause_reason(project_id, settings)`：手动暂停优先返回 `"user"`；门禁用返回 None；用量相对**窗口基线**越任一阈值且门未开 → 开 `budget_windows`（open=1+快照）返回 `"budget_gate"`；门已开则返回 None（同一 run 不重复问）。
- 接入点：`services/workflow.py`（两篇论文之间、生成最终报告前）、`documents/analysis.py`（每张图表边界，并发池"排空再停"保留原暂停异常的原因）、`PaperAnalyst._pause_or_raise`（分析块/概述/自动复核之间）。
- worker 捕获 `AnalysisPausedError` → park_running_stages + reopen(`analysis_paused`) + `set_pause_reason(reason)` + trace，任务正常结束（非 failed）。

## 人工裁决（`services/workspace.py` + `ui/app.py`）

- 暂停态 UI：`stage()` 对 `budget_gate` 显示"分析已暂停（成本门槛）"及说明；暂停面板展示"累计 token / 视觉调用 / 已运行 vs 阈值"。
- `resume_analysis`（及 select_papers / reanalyze_selected / reanalyze_part / run / 上传续跑）先 `ack_budget_gate`：关闭门 + 基线=当前用量 + since=now → 再花满一个阈值块才会再次询问（不逐步骤骚扰）。
- 中止：不点继续即保持暂停，中间产物全保留，随时可续（未做独立 abort 动作，见计划文档偏差表）。

## 验证

- 新增单测 17 个，全量 `pytest` → **135 passed**（schema 断言随 V16 事件化 HITL 升至 16，另有 `test_hitl_events.py` 5 例）；`ruff check app ui tests` 通过。
- `tests/unit/db/test_budget_gate.py`：V15 建表/加列；计量合计；低于阈值不触发；越阈值开门一次、重复询问被抑制；ack 重基线后再花一个阈值块才再触发；视觉调用独立计数；分钟阈值从首次用量起算；手动暂停优先且门禁用时仍生效；pause_reason 往返与级联删除。
- `tests/unit/test_llm_usage_sink.py`：text/vision 计量字段正确；重试的真实消耗全部入账；sink 故障不打断调用。
- 相关回归：schema 版本断言更新到 17（`test_schema_cleanup.py` / `test_review_provenance.py` / `test_analysis_supervision.py`；V16 为事件化 HITL、V17 为 analysis_graph ReviewGate 决定列，分别见 `docs/P11_HITL_EVENTS_VALIDATION.md` / `docs/P13_ANALYSIS_GRAPH_VALIDATION.md`）。

## 手工走查路径（有本地 Ollama 时）

1. `.env` 设 `BUDGET_GATE_TOKENS` 很小（如 2000）→ 启动分析。
2. 观察分析进行到安全边界自动停下，阶段=已暂停（成本门槛），暂停面板显示累计用量与阈值。
3. 点「继续分析」→ 从停点续跑、已完成图表/块不重算（traces `analysis_paused` 与进度计数可证）。
4. 再次累计满一个阈值块 → 再次暂停（软性重复监督）；不点继续 = 保持暂停，产物完好。
