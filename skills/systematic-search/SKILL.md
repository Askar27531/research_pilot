name: systematic-search
description: Generate complementary scholarly search queries and retrieve candidate papers.
version: 1.2.0

# Systematic Search

## 目的
把研究主题转换为互补、可复现、**先宽召回后收敛**的学术检索策略：概念组与检索式负责“尽量召回相关论文”，精确取舍交给后续的相关性筛选与 LLM 精筛。本技能只负责策略；检索执行、年份/来源过滤、去重由代码完成。

## 适用条件
在文献检索阶段，给定结构化 ResearchRequest 与 ResearchUnderstanding，需要把课题转成检索式并召回候选论文。

## 不适用条件
不用于 PDF 正文解析、证据抽取、逐篇精读分析或实验设计；不接收完整对话历史。

## 输入
结构化 ResearchRequest（研究问题、关键词、年份与来源约束）与 ResearchUnderstanding，不接收完整对话历史。

## 步骤
1. 提取 2-5 个概念组，每组尽可能扩展同义词、缩写与常见变体（例如 wildfire: forest fire、fire suppression、firefighting、wildfire control、wildfire response、containment）。组词面向召回，宁多勿漏；可标注哪个是核心组（任务/方法）。
2. 排除主题只列“明显不相干”的大类；不要因为一篇论文讨论同领域的相邻子任务（检测、预测、建模、监控）就把它们排除——相关性由后续 LLM 精筛判断。
3. 生成 3-5 条**互补且不同召回面**的英文检索式：至少一条“宽式”检索式只约束核心概念以最大化召回；其余可逐步收紧（多概念组组合）。禁止只是把同一条换几个词重排——那样召回面相同。
4. 检索式保持纯主题词与同义词组（可用引号与 AND/OR）：**不要写入年份、来源、数量等系统参数，也不要自创年份边界**（如 2020:2030）。这些由代码按输入统一处理。
5. 检索式生成后交由代码逐条检索、局部失败容错，并按 DOI、OpenAlex ID 或规范化标题去重。

## 输出 schema
SearchQueryPlan：topic、required_concept_groups、excluded_topics、queries（每条含 query、concepts、purpose）。

## 质量检查
每个概念组含至少 2 个同义词；检索式之间召回面不同（不是同义改写）；至少一条宽式检索式；查询文本中不含年份/来源/数量字段；topic 凝练为研究问题的等价表述。

## 失败处理
若生成结果少于 3 条有效检索式、概念组缺失或调用失败，由确定性回退方案补足并写入 warnings，不中断流程。
