name: cross-modal-consistency
description: Compare what a paper's prose claims about a figure or table against what is actually visible in that figure/table, and report consistent, inconsistent or unverifiable checks with optional region grounding.
version: 1.0.0

# Cross-Modal Consistency（正文 ↔ 图表一致性扫描）

## 目的
对论文中"正文提及过"的每个图/表，系统性核对：**论文在正文里怎么说，图中实际显示什么**。这一步不依赖分析环节是否引用了该图表——只要正文提到它，就应当被核对，把"抽查式互证"升级为"系统化互证"。

## 适用条件
输入为单个图表裁剪图、图注 caption，以及正文中提及该图表的若干句子（prose mentions，含页码）。仅当该图表确实存在 prose mentions 时运行。

## 不适用条件
不判定论文的外部真实性（"图支持论文说法"≠"说法在现实为真"）；不评审图表质量；不对分析论断本身表态（那是 visual-verification 的职责）；不比较多张图表。

## 输入
- 图像：该图表裁剪图（唯一事实来源）。
- 上下文：caption 与编号后的 prose mentions（每句一个编号，含页码）。
- 没有分析 claim——本任务对象是"正文的陈述 vs 图中内容"。

## 步骤
1. 先仅凭图像列一遍可见事实（轴标签、数值与趋势、结构组件、图例、表头与单元格），用作对照基准；不要把这步的结果写进输出。
2. 逐条编号核对每个 mention 句子的**陈述**：
   - `consistent`：图中存在直接、可指认的可见内容支撑该陈述；
   - `inconsistent`：可见内容与该陈述**明显矛盾**（如数值方向相反、声称的组件/结果不存在、图注与图不符）；
   - `unverifiable`：图中信息不足、被截断或看不清，无法确认也无法否定。
3. 对能指认的区域给出归一化 bbox（0–1，相对裁剪图）与一句 note；指认不出就留空，**不得猜测性给区域**。
4. mention_index 必须对应输入中的编号；visible_evidence 必须引用图中可见事实，禁止把 caption 或论文其他部分当作证据。

## 输出 schema
CrossModalConsistencyReport：checks 列表，每项 =
ProseConsistencyCheck（mention_index / status=consistent|inconsistent|unverifiable /
visible_evidence / region? / note）。

## 质量检查
- **缺失信息不是矛盾**：图里看不到某硬件/超参/场景设定 ≠ 图否定了它。只有当图中存在**直接相反、可明确指认**的可见证据（数值方向相反、声称的组件/结果不存在且图上显示了替代物、轴标签直接冲突）时才判 inconsistent；其余"图里没有相关信息"的情形一律判 unverifiable。
- 数值类陈述必须能在图中（刻度/数值/单元格）核对，对不上即 inconsistent 或 unverifiable，不得含糊判 consistent；
- 只判"可见内容 vs 该句陈述"，不因论文可信而放行，也不因个人预期而否定；
- 一图多条 mention 时逐条独立判定，禁止用一条的结论带过其余。

## 失败处理
结构化校验失败由 Provider 受控重试；图像不可读或多次失败由调用方跳过该项并保留给人工复核，不自动写结论。
