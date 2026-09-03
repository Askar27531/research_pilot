name: method-mechanism-extraction
description: Extract a paper's method composition and why-it-works mechanism into evidence-grounded Chinese claims.
version: 2.0.0

# Method Mechanism Extraction（方法与作用机制抽取）

## 目的
在逐篇精读的“方法与作用机制”栏中，把论文的方法如何构成、如何运作（methods），以及机制为何有效（mechanisms），抽成可教学、可追溯的中文条目，供跨论文比较与用户复核。

## 适用条件
给定某篇论文的全文证据索引（文本块与图表证据，各带 evidence_id 与页码）与用户课题；用于生成方法与机制两栏内容。

## 不适用条件
不用于方法卡片、方法迁移评估、实验设计或文献检索筛选；不对方法的新颖性、首创性或超越论文范围的贡献作任何宣称。

## 输入
用户课题、分析要求、论文元数据，以及受限的证据集合（evidence_id、页码、文本/图表内容）。只能依据这些证据作答。

## 步骤
1. 用方法相关关键词（方法、算法、模型、框架、流程、公式、架构、处理步骤、输入、输出等）在证据中定位方法描述与机制解释片段。
2. methods：叙述方法如何构成与运作——输入形式、主要处理步骤与关键组件、输出。
3. mechanisms：叙述该方法有效的因果原理与关键假设——例如某一步骤为何有效、在什么条件下成立。
4. methods 与 mechanisms 两栏内容不得相同：前者回答“怎么做”，后者回答“为什么有效”。
5. 逐条标注依据：论文明确陈述的事实使用 supported 并引用至少一条所给 evidence_id；超出论文论述的推断或假设使用 inference 且不引用证据。
6. 某栏证据不足时宁缺毋滥：给出简洁的 inference 或省略该条，绝不虚构数值或捏造证据。

## 输出 schema
MethodAnalysisDraft 的 methods 与 mechanisms：每个元素为一条 AnalysisClaim（value、kind=supported|inference、evidence_ids）。

## 质量检查
引用只来自所给 evidence 集合；supported 必须带证据、inference 不得带证据；两栏内容不重复；每条是完整成句的具体论述而非栏目词；机制表述与图表/数值证据一致；不把摘要换一种说法充当机制。

## 失败处理
结构化输出校验失败（如 supported 缺证据、inference 带证据）由 Provider 受控重试；引用越界会被代码拒绝并报错。
