name: paper-screening
description: Screen and rank retrieved papers against explicit research concepts.
version: 1.0.0

# Paper Screening

## 目的
根据研究问题筛选、排序候选论文，并给出可审查的选择理由。

## 适用条件
已经获得去重后的候选论文，需要控制相关性和最终数量。

## 不适用条件
不负责生成检索式、获取 PDF 正文或构造 Evidence。

## 输入
ResearchRequest、概念约束和候选 PaperMetadata 摘要。

## 步骤
1. 用必须概念组做确定性过滤。
2. 计算词法分数并只把 Top-30 交给模型。
3. 分批执行结构化相关性判断。
4. 合并分数并选出 maximum_papers 篇。

## 输出 schema
RankedPaper 列表，每项包含分数分量、selection_reason 和 matched_aspects。

## 质量检查
稳定键唯一；分数在 0 至 1；每篇入选论文都有非空理由。

## 失败处理
单批模型排序失败时退回词法分数，并写入 warning。
