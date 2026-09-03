name: paper-screening
description: Screen and rank retrieved papers against explicit research concepts.
version: 1.1.0

# Paper Screening

## 目的
从去重后的候选论文中剔除与课题不相干或仅属相邻主题的论文，保留直接研究请求任务的论文，并给出可审查的打分与选择理由。

## 适用条件
已经获得去重后的候选论文列表，需要按研究问题控制相关性与最终数量（maximum_papers）。

## 不适用条件
不负责生成检索式、获取 PDF 正文或构造 Evidence。

## 输入
ResearchRequest（研究问题、关键词、约束）、已验证的检索策略，以及候选论文子集（每条含 stable_id、标题、年份、截取的摘要片段）。

## 步骤
1. 代码先按必须概念组做确定性过滤，再按词法分数初筛，只把 Top-N（默认 10，最多 30）条交给模型。
2. 对输入的每条论文做一次 include/relevance 判定，规则如下：
   - 每个 stable_id 恰好返回一次：不得遗漏输入中的条目，也不得新增输入之外的条目。
   - include=true 仅当论文直接研究请求的任务本身；与任务相邻但不直接研究它的论文（示例：课题是配准时，融合/检测/感知类工作）以及综述类论文一律 include=false。
   - relevance 用 0-100 表示直接相关程度；reason 用一两句简短、具体的理由说明判定依据；matched_required_concepts 引用请求中的概念或术语。
3. 把模型判定与词法分数按权重合并为最终分数（0-1），剔除 include=false 的论文，按分数排序后截取 maximum_papers 篇；第 3 步为代码执行的确定性步骤。

## 输出 schema
PaperScreeningBatch：decisions 列表，每项为 PaperScreeningDecision（stable_id、include、relevance 0-100、reason、matched_required_concepts）。最终 RankedPaper 列表由代码合成。

## 质量检查
输入条目不遗漏、不重复；排除理由具体；入选论文均有非空理由；合并后分数在 0 至 1。

## 失败处理
单批模型判定失败时退回词法分数并写入 warning，不中断整个筛选。
