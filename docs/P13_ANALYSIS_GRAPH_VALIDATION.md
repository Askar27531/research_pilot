# P13 · analysis_graph（LangGraph 图② + interrupt 人门）验证记录

> 目标：`docs/重构设计_analysis_graph_LangGraph_HITL.md` 的四里程碑。M1 行为等价骨架 → M2 SelectGate interrupt#1 → M3 ReviewGate interrupt#2 → M4 收尾。状态：**全部完成，145 用例全绿、ruff 通过**。
> 一句话：**分析流水线成为第二张可 checkpoint 的 LangGraph 图；"等人"以图内 `interrupt()` 冻结 + `hitl_events` 落事件，人工决定写业务表后重入即续，预算/手动暂停保持原有 DB 停车语义。**

## M1：行为等价图骨架（无 interrupt）

- 新增：`app/graph/analysis_state.py`（`AnalysisState` 编排账本）、`app/graph/analysis_nodes.py`（节点工厂 + `AnalysisGraphContext` + `AnalysisJobStop`）、`app/graph/analysis_graph.py`（线性拓扑）。
- `services/workflow.py`：删除旧 `_analyze_selected()`，`document_analysis` 改为 `_run_analysis_graph()` 图驱动；运行认领、缺 PDF 事件停车（`AnalysisJobStop`）、预算/手动暂停（`AnalysisPausedError`）语义原样保留。
- 回归：139 用例全绿（原 135 + 图级 4）。

## M2：SelectGate interrupt#1

- `select_gate` 节点：无有效选文 → `wait_for_human("paper_selection")` 落事件 → `interrupt()` 冻结（LangGraph 以返回状态的 `__interrupt__` 呈现，driver 据此记 trace `graph_interrupt`）。
- 检索 job 尾部改为：结果入库 → `reopen("paper_selection")` → 自动入队 `document_analysis`（图②接管选文等人）。
- 人工 `select_papers`：resolve 事件 + 存选文 → 新 job 重入 → gate 读选文真值放行（等待期零 LLM）。
- 测试：真实图 + MemorySaver 验证"停车→中断→`Command(resume)` 同线程续跑放行"。

## M3：ReviewGate interrupt#2

- V17：`projects.analysis_review_decision`（NULL | 'continue' | 'regenerate_after_review'，DB 真值）。
- `review_gate` 节点（analyze_papers 之后、synthesize 之前）：轻量高危统计（被 supported 结论引用且 `evidence_reviews.status='doubted'`，纯 DB 读）→ 无高危直接合成；有高危且无决定 → 停车（stage `analysis_paused`、`pause_reason='review_gate'`、事件 `evidence_review_gate`）→ `interrupt()`；决定 `continue` → 清决定、合成；决定 `regenerate_after_review` → `reset_analysis` + 清决定、条件边回 `analyze_papers`（work_item 缓存零重算 + `_sanitize_evidence` 应用排除）→ 再合成。
- 动作：`review_gate_continue` / `review_gate_regenerate`（schema+service+守卫校验）；选文/重选/重检索/重析会清残留决定；Streamlit 暂停面板按 open 事件渲染"直接生成 / 先处理争议证据再重新生成"。
- 测试：DB 列往返；图级 4 例（无高危放行 / 高危停车+中断 / continue 放行 / regenerate 清分析回边）。

## 偏差记录（诚实边界，面试口径）

- **Command(resume) 精确同线程唤醒未用于 worker 主路径**：多 job/事件堆积下区分"待续线程 vs 已完成线程"易误配；采用 DB 真值重入（选文/复核决定先落业务表，重入图后 gate 直读）——语义等价、零重算、无线程生命周期管理。LangGraph 官方 interrupt 语义仍真实发生（图确实冻结在节点上、可同线程 `Command(resume)` 恢复，见测试）；worker 实际续跑走"新 job + 幂等断点"。
- 合成前"先复核"没有专用 UI 页：停在复核门时可在「查看研究资料」卡片逐条确认/存疑/排除（既有 `evidence_review`），或选"直接生成"后走决策台 + 重新生成（既有闭环）。
- `regenerate_after_review` 粒度 = 整轮重跑论文精读（复用 work_item 缓存），未做"仅受影响论文/证据"的最细粒度。

## 验证

- 全量 `pytest` → **145 passed**；`ruff check app ui tests` 通过。
- 图级：`tests/unit/services/test_analysis_graph.py`（拓扑、SelectGate、ReviewGate 全语义）。
- DB：`test_hitl_events.py`（V17 列 + 决定往返）、schema 断言升至 17。

## 手工走查（有本地 Ollama 时）

1. 新建课题 → 检索完成自动进入分析 job → 停在"请选择要精读的论文"（等待你决定横幅；traces `graph_interrupt`）。
2. 选 1-2 篇 → 分析进行；若出现被引用自动存疑证据，合成前停在"分析已暂停（证据复核）"，可选直接生成或先处理再重新生成。
3. 两种选择都观察：已完成图表/块不重算（进度计数与 traces 可证）；`regenerate` 路径清 paper 分析后回边重跑并最终合成。
