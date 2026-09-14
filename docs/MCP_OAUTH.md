# 独立 MCP Server 的 HTTP 鉴权（OAuth 2.1 + PKCE）

`mcp_servers` 里的自研 MCP Server（`literature` / `document`）可脱离 FastAPI 独立部署，
通过 HTTP 暴露给外部 MCP 客户端（Claude Desktop / Claude Code / 任意符合规范的客户端）。

只有 HTTP transport 涉及鉴权；`stdio` transport 不鉴权，以保持进程内消费不变。

## 三种鉴权模式

| 模式 | 说明 | 需要的环境变量 |
|---|---|---|
| `none` | 不鉴权，仅回环/可信网络 | 无 |
| `static` | 预共享 Bearer Token（非回环绑定默认要求） | `MCP_AUTH_TOKEN` |
| `github` | OAuth 2.1 授权码 + PKCE，经 GitHub OAuth App 代理 | `MCP_GITHUB_CLIENT_ID`、`MCP_GITHUB_CLIENT_SECRET`、`MCP_OAUTH_BASE_URL` |

默认（`--auth` 省略）：回环绑定为 `none`，非回环绑定为 `static`。非回环且无鉴权会被拒绝启动，
防止意外暴露端点。

## 启动

```powershell
# stdio（不鉴权）
researchpilot-mcp --server literature --transport stdio

# HTTP + 静态 Bearer Token
researchpilot-mcp --server literature --transport http --host 0.0.0.0 --port 8100 --auth static

# HTTP + GitHub OAuth 2.1
researchpilot-mcp --server literature --transport http --host 0.0.0.0 --port 8100 `
  --auth github --public-base-url https://mcp.example.com
```

CLI 启动时会自动加载项目根目录 `.env`（已存在的进程环境变量优先）。

## GitHub OAuth App 配置

1. 在 GitHub 创建 OAuth App，回调地址设为 `{public-base-url}/auth/callback`。
2. 在 `.env` 中填写：

   ```ini
   MCP_GITHUB_CLIENT_ID=<client-id>
   MCP_GITHUB_CLIENT_SECRET=<client-secret>
   MCP_OAUTH_BASE_URL=https://mcp.example.com
   ```

3. 生产环境必须使用 `https://`；`http://` 仅允许回环开发。
4. 可选：
   - `MCP_OAUTH_REQUIRE_CONSENT=0` 关闭授权确认页（生产保持 1）。
   - `MCP_OAUTH_JWT_SIGNING_KEY=<key>` 指定 JWT 签名密钥（缺省由 client secret 派生）。

## 授权码 + PKCE 流程

1. 客户端读取授权服务器元数据并注册为客户端。
2. 打开 `/authorize`，用户在 GitHub 批准。
3. 服务端签发短时 token，之后每次请求都向 `https://api.github.com` 校验。

请求范围固定为 `user`。

## 状态存储

OAuth 状态（客户端注册、refresh token）加密存储在 `MCP_OAUTH_DATA_DIR`（默认
`data/oauth-proxy`），按签名密钥指纹隔离，服务重启后仍可用。该目录已在 `.gitignore`
中忽略，切勿提交。
