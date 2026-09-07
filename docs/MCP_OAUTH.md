# 用 GitHub OAuth 保护远程 MCP Server（OAuth 2.1 授权码 + PKCE）

本文说明如何把 `researchpilot-mcp` 以 HTTP 暴露的自研 MCP Server（`literature` / `document`）
升级为 **OAuth 2.1 授权码（Authorization Code + PKCE）** 保护：外部 MCP 客户端（Claude
Desktop / Claude Code / 任何符合规范的客户端）先完成标准 OAuth 舞蹈，拿到本服务签发的令牌
后才能调用工具。

## 原理（一句话）

- 本服务是 **OAuth 授权服务器（Authorization Server）**：挂载 `/.well-known/oauth-authorization-server`、
  `/authorize`、`/token` 等端点，并实现**动态客户端注册**（FastMCP 3.x 内置）。
- **GitHub OAuth App 是上游 IdP**：用户授权发生在 github.com；FastMCP 的
  `GitHubProvider`（`mcp_servers/oauth.py` 里的 `build_github_provider`）代理上游，向 MCP
  客户端签发短期 access token（过期可 refresh）。
- 每次请求服务端用 `GitHubTokenVerifier` 调 `https://api.github.com/user` 校验令牌
  （默认缓存 300 秒，见 `mcp_servers/oauth.py`），并要求 `user` scope。

```
MCP Client ── discovery / DCR / authorize / token ──▶ 本服务 (AS, FastMCP)
      ▲                                                   │ 代理上游
      │  access token（本服务签发）                        ▼
      └─────────────────────────────────── GitHub OAuth App（用户登录授权）
```

## 1. 创建 GitHub OAuth App

1. GitHub → Settings → Developer settings → **OAuth Apps** → **New OAuth App**。
2. `Application name` 随意（如 `ResearchPilot MCP (dev)`）。
3. `Homepage URL`：填服务首页地址即可，本地开发填 **`http://localhost:8102`**（不是回调地址）。
4. `Redirect URLs`（即回调）：填 **`http://localhost:8102/auth/callback`**（默认 redirect path，
   末尾不要加 `/`，且必须与 `--public-base-url` 的 host:port 完全一致）。
5. 创建后记下 `Client ID` 并生成 `Client secret`（只显示一次）。

> 关键：**三个地方必须用同一个 host:port**——GitHub App 的回调 URL、`--public-base-url`、
> 客户端连接的 MCP URL。统一用 `http://localhost:8102`（GitHub 对 loopback 的 `localhost`
> 兼容最好，`127.0.0.1` 有时会被拒；两者混用会导致 redirect_uri 不匹配）。
> 若要公网使用，base URL 必须是 `https://`（`mcp_servers/oauth.py` 会拒绝非回环的明文
> http，且代码内已校验）。

## 2. 配置凭据（推荐写进项目根目录 `.env`）

CLI 启动时会**自动加载项目根目录 `.env`**（与 FastAPI 应用一致；已存在的系统环境变量优先）。
把下面几行取消注释并填成真实值写入 `.env` 即可，无需每次 export：

```dotenv
MCP_GITHUB_CLIENT_ID=Ov23li...
MCP_GITHUB_CLIENT_SECRET=你的ClientSecret
# 本地开发可关闭内置授权确认页（生产保持开启）：
MCP_OAUTH_REQUIRE_CONSENT=0
# 也可把 base URL 放这里，从而免传 --public-base-url：
# MCP_OAUTH_BASE_URL=http://localhost:8102
```

OAuth 状态（动态客户端注册、refresh token、授权事务）会**加密持久化**在
`data/oauth-proxy/`（默认，`data/` 已在 .gitignore），可用 `MCP_OAUTH_DATA_DIR` 覆盖；
存储密钥由 client secret 确定性派生，服务重启后已授权客户端无需重新登录。

## 3. 启动受 OAuth 保护的 Server

```powershell
.\.venv\Scripts\Activate.ps1
researchpilot-mcp --server document --transport http `
  --host 127.0.0.1 --port 8102 `
  --auth github --public-base-url http://localhost:8102
```

`literature` 同理。启动日志会打印 `(auth: github OAuth 2.1 (client Ov23li… at …))`。

> 说明：`--auth static`（`MCP_AUTH_TOKEN` 预共享 Bearer）与 `--auth none`（仅回环）仍可用，
> 见 `mcp_servers/oauth.py`。`stdio` 传输不启用认证（进程内/本地消费不受影响）。

## 4. 验证

### 4.1 发现端点（无认证，直接可看）

```powershell
Invoke-RestMethod http://127.0.0.1:8102/.well-known/oauth-authorization-server |
  ConvertTo-Json -Depth 4
```

应返回 `issuer`、`authorization_endpoint`、`token_endpoint`、`registration_endpoint` 等
（本实现已冒烟验证：`/authorize`、`/token`、`/register` 动态注册端点在线，支持 PKCE `S256`、scope `user`）。

### 4.2 未授权访问应被拒绝

```powershell
try { Invoke-RestMethod http://127.0.0.1:8102/mcp -Method Post } catch { $_.Exception.Response.StatusCode }
```

应得到 401（无令牌的 MCP 调用被拒绝）。

### 4.3 用真实 OAuth 客户端走完整授权码流程

**方式 A：Claude Desktop / Claude Code**

1. Claude Desktop → Settings → Developer → 添加 MCP Server → 类型选 HTTP/URL：
   - URL：`http://127.0.0.1:8102/mcp`
   （Claude Code：`claude mcp add researchpilot-doc --transport http http://127.0.0.1:8102/mcp`）
2. 首次调用工具时客户端会打开授权页 → 跳转 GitHub 登录并授权 → 回调到本服务 → 自动换发令牌。
3. 成功后 `tools/list` 应返回 `parse_document` / `get_page` / `get_document_structure` /
   `extract_figures` / `get_figure`（`literature` 则是 `search_papers` / `get_paper_metadata`）。

**方式 B：FastMCP Python 客户端（适合脚本验证）**

```python
import asyncio
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

async def main():
    async with Client(
        StreamableHttpTransport(url="http://127.0.0.1:8102/mcp"),
        auth="oauth",  # 触发授权码 + PKCE（本地回调服务器收集授权码）
    ) as client:
        tools = await client.list_tools()
        print([tool.name for tool in tools])

asyncio.run(main())
```

首次运行会弹出浏览器完成 GitHub 登录；成功后打印工具清单即代表完整授权码流程跑通。

## 5. 常见问题

| 现象 | 排查 |
|---|---|
| 回调 404 / redirect_uri 不匹配 | GitHub App 的 `Authorization callback URL` 必须等于 `{public-base-url}/auth/callback` |
| 授权页一直转圈/无反应 | 确认 `--public-base-url` 与 GitHub App 注册的域名端口一致；本地开发用 `127.0.0.1` 而非 `localhost` 时两端保持一致 |
| 调用工具报 401 / token invalid | 客户端令牌过期或被吊销：重新走一次授权（清掉客户端保存的凭据） |
| 不想每次看到确认页 | 本地开发设 `$env:MCP_OAUTH_REQUIRE_CONSENT = "0"`（生产必须保持确认） |
| 非回环绑定 | base URL 必须是 `https://`，且 GitHub App 域名要与证书匹配 |

## 6. 边界（诚实清单）

- 面向**自托管、单用户/个人**场景：静态 Bearer 仍是更简单的回退；GitHub OAuth 用于需要
  标准授权码流程的真实 MCP 客户端。
- **未实现**：多租户、A2A、网关侧客户端 OAuth（`config/mcp_servers.json` 的 http transport
  仍使用 `auth_env` 静态头）、通用 OIDC 多 IdP。
- 应用**进程内**消费（in-process transport）与 `stdio` 不经过 OAuth，行为不变。
