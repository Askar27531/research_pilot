name: experiment-design
description: Design falsifiable evidence-grounded experiments with controls and explicit criteria.
version: 1.0.0

# Experiment Design

## 目的
把已验证 Evidence 转换为可执行、可证伪且可人工审批的实验方案。

## 适用条件
项目已有有效 Evidence ID，用户要求形成假设、实验或消融计划。

## 不适用条件
不用于论文检索、PDF 解析或无证据的自由创意生成。

## 输入
研究目标、Evidence 索引及用户约束；不接收整篇 PDF 正文。

## 步骤
1. 每条假设关联至少一条 Evidence，并显式列出 assumptions。
2. 为每个实验定义 baseline、单一 modification、controls 和 metrics。
3. 同时定义 success criterion 与 failure criterion。
4. 对可拆分组件生成单变量 ablation，其他变量保持不变。
5. 删除 baseline + modification 重复的实验。

## 输出 schema
ExperimentProposalDraft，包括 hypotheses、experiments、ablations、evaluation。

## 质量检查
Evidence ID 有效；指标可测；成功/失败标准互补；消融只改变一个因素。

## 失败处理
缺失 Evidence 或失败标准时拒绝方案；结构化输出校验失败时由 Provider 受控重试。
