name: systematic-search
description: Generate complementary scholarly search queries and retrieve candidate papers.
version: 1.1.0

# Systematic Search

## 目的
把研究主题转换为互补、可复现且高精度的学术检索策略：先确定必须同时出现的概念组与排除主题，再生成覆盖全部概念组的英文检索式。本技能只负责策略；检索执行、逐条容错与去重由后续工具和代码阶段完成。

## 适用条件
在文献检索阶段，给定结构化 ResearchRequest 与 ResearchUnderstanding，需要把课题转成检索式并召回候选论文。

## 不适用条件
不用于 PDF 正文解析、证据抽取、逐篇精读分析或实验设计；不接收完整对话历史。

## 输入
结构化 ResearchRequest（研究问题、关键词、年份与来源约束）与 ResearchUnderstanding，不接收完整对话历史。

## 步骤
1. 提取 2-5 个必须同时出现的概念组：每组给出同义词与变体，入选论文必须命中每个概念组中的至少一个词。
2. 列出与主题相邻、容易被误检的排除主题（示例：目标是“配准”时，排除“融合/检测/感知”类工作）。
3. 生成 3-5 条互补的英文检索式，每条必须覆盖每个必要概念组；检索式之间互补、不重复。
4. 高精度优先：不生成只讲应用场景或只讲评价指标、不约束核心方法或任务的宽泛检索式。
5. 遵守输入的年份与来源限制。检索式生成后交由代码逐条检索、局部失败容错，并按 DOI、OpenAlex ID 或规范化标题去重。

## 输出 schema
SearchQueryPlan：topic、required_concept_groups、excluded_topics、queries（每条含 query、concepts、purpose）。

## 质量检查
概念组完整且每组有同义词；每条检索式覆盖全部必要概念组；检索式不重复、无宽泛式；topic 凝练为研究问题的等价表述。

## 失败处理
若生成结果少于 3 条有效检索式、概念组缺失或调用失败，由确定性回退方案补足并写入 warnings，不中断流程。
