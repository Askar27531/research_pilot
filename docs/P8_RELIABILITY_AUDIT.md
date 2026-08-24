# P8 恢复、安全与 Trace 审计

**阶段状态：DONE。实施与故障注入结果见 `P8_VALIDATION.md`。**

## 写操作与幂等边界

| 节点/操作 | 写入 | 当前幂等键 | 恢复行为 | 待办 |
|---|---|---|---|---|
| Literature search | LangGraph checkpoint + `work_items` | `thread_id=project_id` + query input hash | 从失败 query 恢复，已完成项读缓存 | 已完成 |
| Paper selection | `papers` | `(project_id, stable_key)` | upsert，不重复论文 | 已由 query/item 故障注入覆盖恢复边界 |
| Project run | `projects` | `run_id` + 原子状态转换 | failed 必须走 resume | 并发压力测试 |
| Evidence build | `evidence` | project/paper/locator key | 同定位 upsert | 批量 item progress |
| Summary save | summary + refs | `(project_id, paper_id)` | 单事务替换 refs | 更新失败回滚测试 |
| Proposal creation | proposal + checkpoint | 每项目一个 proposal | interrupt 后 SQLite 恢复 | supersede 暂不支持 |
| Proposal decision | proposal + history | proposal version | 旧版本 409 | 并发决策压力测试 |
| Artifact generation | file + artifact row | UUID path；name/version | DB 失败清理文件 | 故障注入写盘失败 |

## Retry 分类基线

| 错误 | 是否重试 | 原因 |
|---|---|---|
| OpenAlex 429/502/503/504、连接错误 | 是，有限退避 | 暂时性上游故障 |
| Ollama timeout/连接错误 | 可重试，有限次数 | 本地服务暂时不可用 |
| Pydantic/请求校验错误 | 否 | 相同输入不会自行恢复 |
| Evidence hash/span 不匹配 | 否 | 原始来源已变化，需要重建 |
| 路径越界、symlink 越界、超大文件 | 否 | 安全拒绝 |
| proposal version 冲突 | 否 | 客户端必须刷新版本 |

## P8 退出门禁

- 第 5 个 item 故障后，前 4 个 item 不重复执行。
- 重启后从失败 item 恢复，而不是重跑完整批次。
- traversal、绝对路径、symlink 越界、超大文件全部拒绝。
- API、Graph、MCP Trace 使用同一 trace ID，可按项目、成功状态、事件类型过滤。
- Dashboard 展示 success rate、latency 和 recovery count。
