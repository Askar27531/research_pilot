name: visual-observation
description: Read a cropped academic figure or table with its caption and prose mentions and report only image-visible facts as a structured observation.
version: 1.0.0

# Visual Observation（图表可见事实观察）

## 目的
对论文中的一个图或表裁剪图进行"所见即所得"的观察：先列图中可直接看见的事实，再给出一句话摘要，供后续证据化分析与图文互证使用。

## 适用条件
输入为单个图表裁剪图（图像字节）以及该图表在解析时附带的上下文：区域类型（figure/table）、图注 caption、以及论文正文中提及该图表的句子（prose mentions，含页码）。

## 不适用条件
不用于判定论文结论真假，不用于跨图表比较，不用于生成分析论断；不把观察结果当作独立于论文的"事实证据"，只作为图中可见内容的记录。

## 输入
- 图像：图表裁剪图本身（唯一的事实来源）。
- 上下文（仅辅助理解，不是图中事实）：region_type、label、caption、mentions（正文提及句）。

## 步骤
1. 先看图，逐项列出**直接可见**的事实：坐标轴含义、数值与趋势、结构组件与连接、图例项、表头与单元格数值等。逐条写入 observations。
2. 对架构图/结果图/消融图等，识别其类型并概括 variables_or_components 与 main_results。
3. 凡无法从图中确认的内容（模糊、被截断、与上下文冲突但不可判定）一律写入 unknowns，不得猜测填进 observations。
4. 最后用一句话把图中最核心的信息压缩进 summary。
5. 仅当图像整体清晰且观察置信度高时 confidence 才可接近 1；否则如实降低。

## 输出 schema
VisualObservation：figure_type / summary / observations / variables_or_components / main_results / unknowns / confidence。

## 质量检查
- 规则一（事实第一）：observations 与 main_results 中的每一条都必须在图中直接可指认，禁止把 caption 或 prose mentions 的说法当作"图中可见"复述。
- 规则二（上下文不作证）：caption/mentions 只用于解释图中符号，不能成为断言来源；图与上下文矛盾时，把矛盾写入 unknowns 而不是顺从上下文。
- 规则三（不越界）：不得声称新颖性（novelty），不得推断论文作者意图。
- 规则四（诚实）：置信度低或信息不足时宁缺毋滥，禁止编造数值。

## 失败处理
图像无法读取或结构化校验失败由调用方（Provider 受控重试）处理；多次失败时该项观察视为失败，由上层决定跳过或重跑，不静默降级为纯文本描述。
