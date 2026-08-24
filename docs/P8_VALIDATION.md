# P8 恢复、安全与 Trace 验收报告

## 恢复与故障注入

- migration v5 新增 `work_items`，以 `(project_id, run_scope, item_key)` 唯一。
- Literature query 的 item key 是完整输入规范化 JSON 的 SHA-256。
- 每项持久化 running/completed/failed、attempts、result/error、latency。
- 确定性 FailureInjector 在第 5 项抛错；恢复后前 4 项读取持久化结果，不再次访问上游。
- 第 5 项 attempts 从 1 增至 2，成功后记录 `item_recovered`；第 6 项正常继续。
- 验收中 6 项最终全部 completed，upstream 每个 query 只执行一次，recovered_items=1。

## 安全矩阵

| 场景 | 结果 |
|---|---|
| `../` traversal | 拒绝 |
| 嵌套 traversal | 拒绝 |
| Windows 绝对路径 | 拒绝 |
| symlink 指向 Workspace 外 | 拒绝 |
| 非 PDF 扩展名 | 拒绝 |
| PDF 扩展名但魔数错误 | 拒绝 |
| 超过配置大小 | 拒绝 |
| Artifact 不安全名称/指令 | 拒绝 |

所有安全拒绝均不重试。

## Trace 串联

`X-Request-ID` 作为唯一 trace ID 进入：

```text
FastAPI middleware
→ project route / Coordinator / agent trace
→ LangGraph metadata.trace_id
→ LiteratureMCPClient tool argument
→ Literature MCP server
→ OpenAlex boundary
```

端到端测试验证同一 ID 出现在全部三次 MCP 搜索调用和项目 Trace 中。

## 指标与 Dashboard

- Trace 支持 `event_type`、`success`、`limit`、`offset` 过滤。
- Trace metrics：total/success/failed、success rate、average latency、recovery count、event type counts。
- Progress metrics：total/completed/failed/running/recovered items。
- Streamlit Progress 页显示四个指标卡、过滤后的 Trace timeline 和 item progress 表。
- Streamlit AppTest：0 exception，5 个页签全部加载。
