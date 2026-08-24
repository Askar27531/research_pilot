# P6 实验设计与 Human-in-the-loop 验收报告

## 已验证闭环

```text
Evidence index
→ experiment-design Skill
→ Qwen structured ExperimentProposalDraft
→ LangGraph human_approval interrupt
→ SQLite checkpoint
→ Accept / restricted Modify / Reject
→ Command(resume=decision)
→ versioned terminal proposal
```

## 自动化验收

- Accept、Modify、Reject 三条 API 路径全部通过。
- proposal 创建后项目进入 `waiting / human_approval`。
- SQLite checkpoint 连接关闭并重新打开后仍可恢复审批。
- proposal version 从 1 更新到 2；旧 version 再提交返回 409。
- Modify 只能更新 modification、controls、metrics、success/failure criterion，不能覆盖 baseline、Evidence 或任意状态字段。
- hypothesis 与 experiment 缺少 Evidence ID 时 schema 拒绝。
- experiment 缺少 failure criterion 时 schema 拒绝。
- baseline + modification 重复的实验被拒绝。
- ablation 引用未知 experiment 时被拒绝。
- 模型引用输入集合之外的 Evidence ID 时 Builder 拒绝。

## 真实 Qwen smoke

模型：当前 `.env` 配置的本地 Qwen/Ollama 模型。

| 尝试 | 超时 | 结果 |
|---|---:|---|
| 首次严格 proposal schema | 180 秒 | ReadTimeout，受控转换为 `LLMError` |
| 缩小为单实验并使用同一严格 schema | 300 秒 | 成功，实际 238.5 秒 |

成功输出包含 1 hypothesis、1 experiment、1 ablation；所有 experiment 均包含 failure criterion 和 Evidence ID。基于实测，默认 `OLLAMA_TIMEOUT_SECONDS` 已调整为 300。

## 当前边界

- 本地 14B 模型生成完整 proposal 延迟较高；异步任务队列与进度推送属于后续 UI/可靠性阶段。
- Modify 是字段白名单更新，不允许直接提交完整 LangGraph state。
- 当前一次项目只允许一个 proposal；多轮重新生成需要在后续版本定义显式 supersede 语义，不能静默覆盖审批历史。
