<!-- 精简推荐方案：面向当前代码库现状，只做性价比最高的一件核心改动 + 两个轻提示。 -->

> **实施状态：已完成（本次落地）**。见文末「实施记录与验证」一节。

# HITL 升级 · 精简推荐方案：复核决策台（+ 两个零成本轻提示）

> **结论先行**：三点里性价比最高、改动最小、收益最直接的是 **方案二「复核决策台」**。方案一（事件表/状态机）与方案三（用量台账/暂停）本期**不做**，各以"搭便车的轻提示"保留叙事价值。
>
> 总改动量 ≈ 后端 1 天 + 前端 1 天 + 测试半天；**只动 1 处 DB 迁移、2 个新只读端点、1 个新 UI 页签**；不新增顶层状态、不动 worker/续跑逻辑、不加事件表、不加 token 台账。

---

## 0. 为什么是这个（取舍矩阵）

| 方案 | 改动量 | 风险 | 可演示收益 | 决定 |
|---|---|---|---|---|
| ① 事件化 HITL（新表+守卫+分发器+改两个等待点） | 大 | 中（动状态机/调度） | 高但偏"架构叙事" | ✂ 本期不做 |
| ② 复核决策台（排序+影响回显+摘要+闭环） | **小** | 低（纯增量） | 高且可量化 | ✅ **做这个** |
| ③ 成本门槛暂停（计量表+检查点+三动作） | 中 | 中（要动 provider/任务循环） | 中 | ✂ 本期不做，降级为"预算提示行" |

**为什么决策台最值**：现在"自动复核"（视觉验证器写 `自动复核：…`、一致性写 `图文一致：…`）把结果写进 `evidence_reviews` 后**没有人消费**——复核按钮散落在研究资料卡片里、没有排序、没有后果预览、没有产出统计。只要把"写库→被看到→被决策→影响报告"这条链路补上，就把现有全部自动复核能力变成产品价值；而它完全不碰项目状态机、续跑、worker，风险最低。

---

## 1. 决策台最小实现

### 1.1 数据层（1 次迁移 V13）

`evidence_reviews` 现状：`(evidence_id, status, note, updated_at)`，无来源、无会话；历史可区分性：自动复核必带 note 前缀（`自动复核：` / `图文一致：`），人审通常无 note——据此回填。

```sql
ALTER TABLE evidence_reviews ADD COLUMN source TEXT;              -- human / auto_visual_verifier / auto_consistency
ALTER TABLE evidence_reviews ADD COLUMN review_session_id TEXT;   -- 一次人审会话（可选，先留空也无碍）
-- 回填：note LIKE '自动复核：%' -> auto_visual_verifier
--       note LIKE '图文一致：%' -> auto_consistency
--       其余 -> human
```

`SCHEMA_VERSION` 12→13，在 `app/db/database.py` 的 `initialize()` 迁移链尾追加 `MIGRATION_V13`（沿用现有模式）。

### 1.2 派生计算（新增纯函数模块，如 `app/evidence/review_desk.py`）

**不落库、读时算**（报告每次重生成都可能变，写时维护必失同步；每个项目只有 1-2 篇论文，量小）：

- `citation_index(project_id)`：解析 `selected_paper_analyses.payload_json` 与 `analysis_reports.payload_json` 里所有 `kind=supported` 结论的 `evidence_ids`，产出每条证据的 `{cited_by, supports_comparison, claim_previews(前3条结论摘要)}`。`supports_comparison` = 引用它的结论出现在报告的 `comparison` 段（共同点/差异/互补性/适用条件）。
- `doubt_reason(evidence)`：该证据最新一条自动复核的 note（`图文一致：存疑：…` / `自动复核：…`），展示给复核者看"机器为什么存疑"。
- 排序分（决定台内顺序）：`priority = 0.45*cited_norm + 0.25*supports_comparison + 0.20*doubted + 0.10*(1-confidence)`。
- 队列语义（关键事实）：`evidence_reviews` 只在有人/自动判定后才插行，**没有行 = unreviewed**（`review_counts` 里的 unreviewed 是虚拟计数），因此：
  - 高危段 = `evidence_reviews.status='doubted'` 且 `cited_by≥1`；
  - 高影响确认段 = `evidence` LEFT JOIN 无 review 行 且 `cited_by≥2` 的图/表证据；
  - 默认只展示前两段（按 priority 排序），"全部"段可展开。

### 1.3 影响回显（预览与提交共用同一函数）

```python
def exclude_impact(citation_index, evidence_id):
    """纯函数：排除某条证据对现有结论的影响（预览与提交同源）。"""
    citing = citation_index[evidence_id]["claims"]           # 引用该证据的所有结论
    downgrade = [c for c in citing if len(c.evidence_ids) == 1]   # 唯一来源 -> 将降级为推断
    retained  = [c for c in citing if len(c.evidence_ids) > 1]    # 仍有多条来源 -> 保留证据支持
    return {"cited_total": len(citing), "downgrade": downgrade, "retained": retained,
            "supports_comparison": any(c.section == "comparison" for c in citing)}
```

> 文案示例：*这条证据被 3 条结论引用；排除后 1 条将自动降级为推断（附结论摘要），2 条仍保留证据支持。*

### 1.4 复核闭环与量化摘要

- 每次提交仍走**现有** `evidence_review` action 写库路径（upsert、不覆盖既有 human 决定）——后端写路径零改动，只在其返回里附一条本次影响摘要。
- Streamlit 侧在会话内累计，页签顶部显示：*本轮已复核 N 条：确认 A · 存疑 B · 排除 C —— 影响结论 M 条（降级 D / 保持 E）*。
- **闭环到报告**：只要本轮出现"排除"，页签顶部亮"报告基于旧证据，需重新生成"提示 + 一键 `reanalyze_selected`（已存在；重建路径会用 `EvidenceRepository.excluded_ids` 过滤被排除证据）。这样人审产出真正改写最终结论，不是只读清单。

### 1.5 接口与 UI

- `GET /projects/{id}/review-desk?segment=high_risk`：排序后的队列，每项含 evidence_token（复用现有 `issue_token`）、类型/页码/置信度、审查状态/来源/机器判定 note、cited_by/supports_comparison、引用结论预览。
- `POST /projects/{id}/review-desk/preview`：`{evidence_token, status}` → 只读影响回显（不落库）。
- 提交：沿用 `POST /projects/{id}/actions {type:"evidence_review"}`（UI 已会调）。
- UI（`ui/app.py`）：`analysis_review` 阶段新增"复核"页签 = 决策台：单条大卡（图/文 + 机器判定卡片 + 引用结论列表）→ 影响回显行 → 确认/存疑/排除；高危段顶部横幅"N 条自动存疑证据被结论引用，建议先处理"。现有研究资料卡片的三按钮保留为快路径。

### 1.6 验收

- [ ] doubted 且被引用 > unreviewed 未被引用（排序正确性单测）。
- [ ] preview 与提交对同一证据的影响一致（含降级集合）。
- [ ] "排除唯一来源 → 结论降级"在真实报告 payload 上验证。
- [ ] 迁移后 source 无 NULL；全量 pytest + 现有 evals 回归绿。

---

## 2. 两个"搭便车"轻提示（保留方案一/三的叙事，成本≈0）

### 2.1 高危存疑角标（方案一的极轻版）

不加事件表、不改状态机。只把**已经存在的数据**变成入口：
- `workspace` 已返回 `evidence_review` 计数；决策台加载时用 1.2 的 index 补一个派生值 `high_risk_count`（doubted ∩ cited_by≥1）。
- 报告页/材料页顶部：*"N 条自动存疑证据被结论引用，建议先复核"*，点击即跳决策台高危段。
- 这就是"介入时机规则"的最小形态：**自动判据弱（doubted）× 影响面大（被引用）→ 升级给人看**；其余全自动，不动流程。

### 2.2 分析预算提示行（方案三的极轻版）

不加 token 台账、不动 provider、不自动暂停：
- 用现有 `document_analyses.total_visuals/completed_visuals` 与 `traces` 已耗时长，在分析进度条下加一行：*"累计视觉分析 X/Y · 预计还需 ~Z 次视觉调用 · 已运行 M 分钟"*。
- 阈值只做**显示级**（config 加 1-2 个字段：`budget_warning_vision_calls` / `budget_warning_minutes`，默认如 40 次 / 30 分钟），超了换黄色提示。
- 若日后想要"暂停/跳过剩余图表"，升级点很明确：逐篇 for 循环本就在幂等断点边界，把提示行换成一次确认即可——但现在不做。

---

## 3. 明确不做（防复杂化）

| 不做 | 原因 |
|---|---|
| `hitl_events` 表、waiting_for_human 新状态、worker 守卫 | 状态机/调度风险与收益不成比例；现有两个等待点 UI 已能完成交互 |
| 双通道分歧检测与自动升级 | 需要记录每次自动判定的"通道"，本期只做"机器存疑+被引用"即够 |
| `llm_usage` 台账与 usage sink | 需动 provider 与全部任务循环；先用现成进度计数给预算提示 |
| 复核自动改写已生成报告 | 用 stale 提示 + 一键重生成，保住"人审前后结论可 diff"的审计性 |

---

## 4. 改动清单（按文件）

| 文件 | 改动 |
|---|---|
| `app/db/database.py` | SCHEMA_VERSION→13，加 `MIGRATION_V13`（evidence_reviews 两列 + 回填）并注册 |
| `app/db/research_repository.py` | `review_evidence()` 写库时带 source/review_session_id；加 `list_reviews()` 查询 |
| `app/evidence/review_desk.py`（新） | `citation_index()`、`exclude_impact()`、排序分、队列组装（纯函数，便于单测） |
| `app/services/workspace.py` | desk/preview 两个只读方法；`evidence_review` action 返回附影响摘要；workspace 加 `high_risk_count` |
| `app/api/routes/core.py` | 注册 `review-desk`、`review-desk/preview` 两个 GET/POST 端点 |
| `ui/app.py` | analysis_review 阶段加"复核"页签（决策台 UI + 影响回显 + 会话摘要行 + stale 提示） |
| `config`/`app/core/config.py` | 预算提示阈值 2 个字段 + 决策台权重常量（可选） |
| `tests/` | `test_review_desk.py`：排序、impact 预览=提交、迁移回填 |

---

## 5. 面试口径（3 句）

> **哪些证据需要人核、何时介入？** 一条自动"存疑"且被 ≥1 条结论引用的证据才值得人看——影响面大 × 机器判据弱才升级，其余自动通过并留痕；复核入口收敛成一个按决策价值排序的台子。
> **人的价值怎么放大？** 决策台只推高危与高影响段，点任何按钮前先回显后果（排除 → N 条降级/仍保留），一轮复核结束给量化摘要；排除结果通过"重新生成报告"真正生效，复核是可量化的质量产出而非点击劳动。
> **为什么不做更大的一等公民事件系统？** 现有两个等待点与自动复核已经覆盖全部真实"等人"场景，先让它们被看见、被决策、可统计；等出现真正的"多事件并发/自动暂停"需求时，再在同一个 decision 入口上升级，不必现在引入状态机复杂度。

---

## 6. 实施记录与验证（本次落地）

| 层 | 改动 |
|---|---|
| DB | `app/db/database.py`：SCHEMA_VERSION 12→13，`MIGRATION_V13` 为 `evidence_reviews` 加 `source`/`review_session_id` 并按 note 前缀回填（自动复核=/图文一致=/其余→human） |
| 仓储 | `review_evidence()` 支持 `source`/`review_session_id`；新增 `list_reviews()`；视觉验证器与一致性检查写入 `source`；人工 `evidence_review` action 写 `source="human"` |
| 纯逻辑 | 新增 `app/evidence/review_desk.py`：引用索引（论文+双篇比较）、影响回显（唯一来源→降级）、决策价值打分、队列分段（high_risk / high_impact / normal）、已排除/人工已确认不重复入队 |
| Schema/API | 新增 `app/schemas/review.py`（ReviewDesk/Item/Stats、Preview 请求/响应）；路由 `GET /projects/{id}/review-desk`、`POST …/review-desk/preview` |
| 服务 | `WorkspaceService.review_desk/review_preview`；workspace 载荷新增 `high_risk_review_count`（高危横幅）与 `budget_hint`（预算提示行，数据来自现有进度计数与 job 时长，阈值 `budget_warning_vision_calls/minutes`） |
| 闭环 | `PaperAnalyst.run` 用 `EvidenceRepository.excluded_ids` 过滤被排除证据 → 重新生成时被排除证据不再可引用，靠它的结论自动降级为推断 |
| UI | `ui/app.py`：分析报告页新增「综合分析 / 证据复核」视图切换；决策台（风险/高影响优先、单条证据卡、机器判定理由、引用结论列表、排除先回显影响再确认、本轮量化摘要与重置）；分析中预算提示行；高危存疑横幅 |

验证：`pytest tests -q` → **113 passed**（新增 `test_review_desk.py`、`test_review_provenance.py`，更新 schema 版本与 OpenAPI 路径断言）；`ruff check app ui tests` 通过。

手工走查：完成一次双篇分析后进「证据复核」→ 优先队列应把"自动存疑且被引用"的证据排在最前 → 点「排除」应先出现影响回显 → 确认后摘要计数 +1 → 切回「综合分析」点「重新生成」即可看到被排除证据不再支撑结论。回滚：删除 `MIGRATION_V13` 并改回 12（不涉及已提交数据的破坏性改写）。
