# P11 · 事件化 HITL（waiting_for_human）最小版验证记录

> 落地范围：`docs/HITL升级计划_事件化_决策台_成本门槛.md` 方案一（M2）的**最小事件化**（M2-lite）。
> 一句话：**等人 = `waiting + 一条 open hitl_event`（派生语义，不新增第六个顶层状态）；worker 守卫保证等待期间任何唤醒都不会绕过人的决定。**

## 语义定义

- 顶层状态机保持不变（`created/running/waiting/completed/failed`，未动 `CHECK(status IN …)` 与 `status in {...}` 分支）。
- **`waiting_for_human` = `projects.status='waiting'` 且存在 `hitl_events.status='open'`**（API/UI 以 `workspace.waiting_for_human` + `hitl_events[]` 呈现）。
- 事件带：`type / title / reason / scope / options（人可选的出路）/ level / created_by / default_action`；解决记录 `resolved_by + resolution_json + resolved_at`（可审计"谁用哪个动作解决了什么等待"）。

## 数据层（V16，`app/db/database.py`）

`hitl_events`：`id/project_id(run_id)/type/title/reason/scope_json/options_json/level/status(open|resolved|cancelled|superseded)/created_by/default_action/resolved_by/resolution_json/created_at/resolved_at`，`(project_id,status,created_at)` 索引，随项目删除级联。

## 运行时

| 环节 | 实现 |
|---|---|
| 产生等待 | `ProjectRepository.wait_for_human(project, stage, event_type=…)`：**单事务** `UPDATE projects→waiting+stage` + `INSERT open 事件`，杜绝"在等但没有事件/有事件却没在等" |
| 消费等待 | `HitlEventRepository.resolve_all(project, …)`：人工动作执行前关闭 open 事件（`resolved`/`superseded`）→ 之后才 `reopen + enqueue` 续跑 |
| 守卫 | `ResearchWorkflowService.execute_job` 领取任务前查 `has_open`；有 open 事件即跳过并记 trace `hitl_gate_skipped`——自动重试/唤醒永远无法绕过人的决定 |
| UI | `render_workspace` 在等待时显示"**待你决定**：{title}——{reason}"横幅；`ProjectWorkspace` 新增 `waiting_for_human`/`hitl_events` 字段 |

## 已事件化的等待点

- **选文**（`event_type=paper_selection`，blocking）：检索完成时（`services/workflow.py`）与 `reselect_papers` 重开选文时产生；由 `select_papers` resolve，`regenerate_search`/`reselect_papers` 将其 supersede。
- **缺全文**（`event_type=document_unavailable`，blocking）：分析任务发现部分论文无法自动获取 PDF 时产生（scope 列缺失论文）；由 `upload` resolve（仍缺则下轮运行重新产生）。
- 其余入队动作（`run` / `reanalyze_selected` / `resume_analysis` / `reanalyze_part`）统一先 resolve_all 再入队，保证守卫不误拦。

## 与方案的偏差（本期未做，面试勿声称）

- 未做事件卡 UI（options 按钮渲染仍走既有各 stage 交互），只加"待你决定"横幅。
- 未做 open 期间"禁止旧 action 语义"的强迁移层（旧 action 兼容层：无 open 事件时按旧语义执行，open 事件存在时对应 action 会 resolve 它）。
- 未做 `superseded_by` 外键链与事件风暴/互斥规则表；现在用 `resolve_all(superseded)` 表达"旧等待作废"。
- 未做证据复核、成本门槛的事件化（成本门槛用 V15 `budget_windows` 表达，见 `docs/P12_BUDGET_GATE_VALIDATION.md`）。

## 验证

- `tests/unit/db/test_hitl_events.py`：V16 建表；`wait_for_human` 原子落定 waiting+open 事件；`resolve_all` 全量/按 type 过滤/记录 resolution；`superseded` 作废语义；级联删除。
- 全量 `pytest` → **135 passed**（schema 版本断言已升至 16）；`ruff check app ui tests` 通过。
