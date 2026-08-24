# P5 Evidence 与跨论文分析验收报告

## 验收范围

P5 验收使用固定 5 篇合成论文夹具，每篇包含 method、dataset、metric、contribution、limitation 五类人工标注事实，共 25 条 Gold claim。该集合用于验证 Evidence 定位、引用完整性和比较表支持率，不用于宣称 LLM 自动抽取精度。

## 指标

| 指标 | 结果 |
|---|---:|
| 论文数 | 5 |
| Gold claims | 25 |
| 成功创建 Evidence | 25/25 |
| Text span 回读一致 | 25/25 |
| 关键比较单元带有效 Evidence | 25/25 |
| Evidence support rate | 100% |
| 项目隔离测试 | 通过 |
| 被引用 Evidence 删除保护 | 通过 |
| 原文篡改检测 | 通过 |

## 已实现完整性约束

- migration v2 建立 project → paper → document → evidence → summary reference 关系。
- Evidence Builder 同时验证 project、paper、document 的链接和 Workspace SHA-256。
- Text Evidence 保存页码、章节、必要 quote、span offsets 和 quote hash。
- Figure Evidence 保存 page、label、caption、bbox、artifact path 和文件 hash。
- Table Evidence 保存 page、label、caption、bbox、页面截图路径和 caption hash。
- Supported claim 没有 Evidence ID 时 schema 校验失败；inference/suggestion 不允许伪装成 cited fact。
- Paper Summary 保存时逐个验证 Evidence 必须属于同一 project 和 paper。
- 被 Paper Summary 引用的 Evidence 受外键保护，无法直接删除。
- Source preview 每次重新验证 text span/hash、figure file hash 或 table caption hash。
- Cross-paper comparison 的 supported 单元保留 Evidence ID；缺失值明确标记为 missing。

## 当前边界

- P5 尚未评估 LLM 自动 claim extraction precision；当前 Gold Set 只证明“给定人工确认 claim 后，引用不会断裂或串项目”。自动抽取质量应在 P9 的独立标注集上评估。
- OCR 和矢量 Figure 独立提取仍受 P4 边界限制，不能据页面截图自动声称存在结构化 Evidence。
- Context compaction 当前统计字符缩减并保留 summary + evidence index；尚未进行 token-level 模型质量对照，该实验属于 P9。
