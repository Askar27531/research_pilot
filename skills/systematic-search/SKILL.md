name: systematic-search
description: Generate complementary scholarly search queries and retrieve candidate papers.
version: 1.0.0

# Systematic Search

## 目的
把研究主题转换为互补且可复现的学术检索式，并从 Literature MCP 获取候选论文。

## 适用条件
用户要求检索、综述或寻找特定主题的论文。

## 不适用条件
不用于 PDF 正文解析、证据抽取或实验设计。

## 输入
结构化 ResearchRequest 和 ResearchUnderstanding，不接收完整对话历史。

## 步骤
1. 提取必须同时出现的概念组、同义词和排除主题。
2. 生成 3 至 5 条目的互补的英文检索式。
3. 逐条调用 Literature.search_papers，并记录局部失败。
4. 按 DOI、OpenAlex ID 或规范化标题去重。

## 输出 schema
SearchQueryPlan、PaperMetadata 列表、DeduplicationResult 和 warnings。

## 质量检查
检索式不重复；覆盖核心概念；遵守年份；结果保留来源查询。

## 失败处理
单条查询失败写入 warning 并继续；全部失败时返回明确的可重试错误。
