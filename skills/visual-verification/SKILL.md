name: visual-verification
description: Two-phase anti-bias audit that re-reads a cited figure or table crop to confirm, doubt or exclude an analysis claim, optionally grounding the verdict to a visible region.
version: 1.0.0

# Visual Verification（图表证据自动复核）

## 目的
当论文分析把某条 figure/table 证据作为 supported 引用时，复核该图/表是否**直接支撑**这条论断。复核结论写入证据复核状态，供用户核对与改判。

## 适用条件
输入为：被引用的图表裁剪图、分析论断（claim）、图注 caption、正文提及句（prose mentions）。仅在证据尚未被人工复核时运行。

## 不适用条件
不判定论文外部真实性（"图支持论文说法"≠"说法在现实为真"）；不评审论断写作质量；不比较多张图。

## 输入
- 图像：被引用证据的裁剪图（唯一事实来源）。
- claim：需要核实的分析论断。
- 上下文：caption 与 prose mentions（仅辅助，不作证）。
- 阶段 A 的输出（可见事实清单）。

## 步骤（防自证偏差：先盲读，后对题）

### 阶段 A：盲读（不得先看 claim）
1. 仅凭图像与 caption 列出**可见事实**（visible_facts）与不确定项（unknowns）。
2. 本阶段禁止接触 claim——用独立于论断的视角描述图，防止"结论先行"污染观察。

### 阶段 B：判定（带反证要求）
3. 现在才阅读 claim，并**只用阶段 A 的事实清单**去核对它。
4. 主动寻找**能反驳该 claim 的可见证据**（反证优先），再下结论：
   - confirmed：事实清单中存在直接、明确的支撑（数值/趋势/结构对得上）；
   - doubted：事实不足、含糊、或支撑依赖推测；
   - excluded：可见事实与 claim 明显矛盾（如数值方向相反、缺失声称的组件）。
5. 若能指认图中具体区域（曲线、柱、子图、单元格），输出归一化坐标 bbox（x0/y0/x1/y1 均在 0–1，相对整图）与一句 note；**指认不出具体区域时应判 doubted，绝不猜测性地给 confirmed 加区域**。
6. confidence 反映"判定所依据的可见事实的确定性"。

## 输出 schema
- 阶段 A：BlindVisualFacts（visible_facts / unknowns）。
- 阶段 B：VisualVerificationVerdict（status=confirmed|doubted|excluded / reason / confidence / regions）。

## 质量检查
- 判定只能锚定阶段 A 的可见事实；claim、caption、mentions 本身不算证据。
- 涉及数值的 claim，必须能在图中（刻度/数值/单元格）找到对应才能 confirmed。
- regions 的坐标必须是图中真实可指认区域，且须归一化到 0–1；无法指认时 regions 留空并考虑 doubted。
- 有反证可见证据却判 confirmed 属于错误。

## 失败处理
阶段 A 或 B 结构化校验失败由 Provider 受控重试；两阶段任一最终失败，或图像不可读，由调用方跳过该项并保留给人工复核，不自动写结论。
