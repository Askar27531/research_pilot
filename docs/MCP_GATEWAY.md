# ResearchPilot MCP Capability Gateway

ResearchPilot 的 Agent 只调用稳定能力名，不依赖某个 MCP Server 的工具名称或传输方式。默认配置位于
`config/mcp_servers.json`，服务启动时完成 Tool Discovery、输入 Schema 检查和健康检查；配置修改在重启后生效。

## 路由与状态

路由顺序固定为：启用、健康、priority 数值、配置顺序。只有连接失败、超时或服务端故障会触发下一个
候选；缺少参数、参数类型或 Tool Schema 不匹配会直接失败。未映射的第三方 Tool 仅出现在发现结果中，
不会暴露给 Agent。

查看脱敏状态：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/mcp/status | ConvertTo-Json -Depth 8
```

状态包含当前路由、是否使用 fallback、最近错误及最近 200 次调用的 capability、server、tool、transport、
latency、success、fallback、project_id 和 trace_id；不会返回 Token、完整环境变量、PDF、Prompt 或响应正文。

默认通过 `uvx arxiv-mcp-server==0.7.2` 启动第三方 arXiv Server。未安装 `uvx` 或外部服务不可用时，
API 仍可启动，状态为 `degraded`，`literature.search.arxiv` 自动回退到 ResearchPilot Literature MCP。
该可选组件来自 `blazickjp/arxiv-mcp-server`，采用 Apache-2.0 License；升级时必须重新验证 Tool Discovery、
`search_papers` Schema、归一化结果及 fallback 测试。

## 运行可独立使用的自研 Server

安装 wheel 或 `pip install -e .` 后：

```powershell
researchpilot-mcp --server literature --transport stdio
researchpilot-mcp --server document --transport stdio
researchpilot-mcp --server artifact --transport stdio
researchpilot-mcp --server document --transport http --host 127.0.0.1 --port 8102
```

通用 MCP 客户端配置示例：

```json
{
  "mcpServers": {
    "researchpilot-paper-intelligence": {
      "command": "researchpilot-mcp",
      "args": ["--server", "document", "--transport", "stdio"],
      "env": {
        "OLLAMA_MODEL": "qwen3:14b",
        "OLLAMA_VISION_MODEL": "qwen3-vl:8b",
        "MCP_ALLOWED_ROOTS": "[\"D:/papers\"]"
      }
    }
  }
}
```

HTTP 默认仅绑定 `127.0.0.1`。绑定非回环地址时必须提供至少 24 字符的 `MCP_AUTH_TOKEN`，Server 将验证
Bearer Token。也可以改用 GitHub OAuth 2.1 授权码 + PKCE 保护 HTTP 端点（`researchpilot-mcp --auth github`，
无需静态令牌），完整配置与真实客户端联调步骤见 [MCP_OAUTH.md](MCP_OAUTH.md)。HTTP Document MCP 只接受公共 `https://` PDF；STDIO 额外接受 `MCP_ALLOWED_ROOTS` 内的
`file://` 路径。路径穿越、根目录外 symlink、私网 URL、非 PDF 和超限文件都会被拒绝。

## Paper Intelligence 工具

Document MCP 保留 `parse_document`、`get_page`、`get_document_structure`、`extract_figures` 和
`get_figure`，并公开以下领域流程：

1. `submit_paper(source_uri)` 返回不透明 `paper_handle`，并将输入复制到 Server Workspace。
2. `start_paper_analysis(paper_handle, objectives)` 返回持久化 `analysis_job_id`。
3. `get_analysis_status(analysis_job_id)` 查询进度或脱敏错误。
4. `get_paper_structure`、`get_paper_evidence`、`get_method_card`、`get_paper_summary` 读取结果。

文本、Figure 和 Table Evidence 保存页码、Bounding Box（如适用）、来源路径及 SHA-256。存在视觉证据时，
方法卡和摘要都必须引用至少一项视觉 Evidence；无证据内容只能标记为 inference。
