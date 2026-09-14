"""MCP 能力网关：注册表（registry）+ 能力路由（router）。

本文件是"应用如何调用外部工具能力"的接缝层，回答三个问题：

1. 有哪些 MCP server？（配置 JSON → pydantic 校验 → 登记在册）
2. 每台 server 活着吗、各自提供哪些工具？（启动时 discover() 体检）
3. 业务代码说"我要能力 X"，到底调谁？（CapabilityRouter 按健康+优先级路由，失败自动换下一台）

整体分工（和同目录其它文件的关系）：
- models.py      ：配置与状态的数据结构（MCPServerConfig / MCPServerStatus / 能力绑定）。
- registry.py    ：本文件。持有"配置 + 每台 server 的实现对象 + 每台的健康卡"，
                   提供 体检(discover) / 选路(candidates) / 调用(invoke) / 状态输出(public_status)。
- document.py / literature.py：给业务代码用的"类型化客户端"，
                   内部只是把"能力名 + 参数"交给本文件的 CapabilityRouter。
- main.py        ：启动时 `MCPRegistry.load(...)` + `await registry.discover()`，
                   并把 CapabilityRouter 注入到图节点上下文（ctx.capabilities / ctx.literature）。

一次调用的典型旅程（下文每个方法会对应到这一步）：
    业务代码 → 客户端.parse_document() → CapabilityRouter.call("paper.parse", 参数)
           → registry.candidates()  选出健康的 server（按优先级排序）
           → registry.invoke()      串行锁 + 连接 + call_tool
           → 失败则试下一个候选（fallback）；全失败抛 MCPTemporaryError（上层幂等可重试）
"""

import asyncio  # 每台 server 一把锁：防止并发时对同一 in-process server 开两个 lifespan
import json  # 解析配置文件 JSON
import os  # stdio 子进程 env 白名单 / http 鉴权 token 都从进程环境读
from importlib.resources import files  # 读"安装包内"的默认配置（不依赖工作目录）
from pathlib import Path
from typing import Any
from urllib.parse import urlparse  # 校验远端 http 端点是否允许（https/本机）

# FastMCP 客户端：discover/invoke 时用它连接 server 并列出/调用工具
from fastmcp import Client, FastMCP

# 两种"远程"传输：stdio = 起子进程；http = 走网络（StreamableHttpTransport）
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

# 本包自有的模型与异常
from app.mcp.models import (
    MCPContractError,  # 配置与 server 实际契约对不上（如配置声明的工具不存在）
    MCPGatewayConfig,  # 校验后的整份网关配置（version + servers 列表）
    MCPServerConfig,  # 单台 server 的配置（id/transport/能力绑定…）
    MCPServerStatus,  # 单台 server 的"运行期健康卡"（healthy/发现的工具/错误）
    MCPTemporaryError,  # 瞬时错误：可换一台 server 重试 / 由上层幂等兜底
)


class MCPRegistry:
    """MCP 网关注册表：一份"配置 + 台账"，本身不决定路由，只提供事实与操作。

    三个核心台账（都在 __init__ 里建好）：
      - self.config            ：校验后的配置（哪些 server、每种能力绑定到哪个工具、优先级）
      - self.inprocess_servers ：配置里 transport=inprocess 的 server 的"真实实现对象"
                                 （main.py 把 FastMCP 实例按 id 塞进来）
      - self.statuses          ：每台 server 一张健康卡（healthy / discovered_tools / last_error）
    """

    def __init__(
        self,
        config: MCPGatewayConfig,
        inprocess_servers: dict[str, FastMCP] | None = None,
    ) -> None:
        self.config = config
        self.inprocess_servers = inprocess_servers or {}
        # 给每台配置里声明的 server 建一张"待体检"的健康卡（id 即键；其余运行期字段先空）
        self.statuses = {item.id: MCPServerStatus.pending(item.id) for item in config.servers}
        # 每台 server 一把 asyncio 锁：
        # FastMCP 的 in-process server 自带 lifespan（连接期会启/关它自己的上下文），
        # 若两个协程同时对同一台 server 开两个 Client，会各启一次 lifespan 而打架，
        # 所以每次 invoke 都拿这把锁，保证同一台 server 的调用是串行的。
        self._server_locks = {item.id: asyncio.Lock() for item in config.servers}

    def _mark(self, server_id: str, **updates: Any) -> None:
        """更新某台 server 的健康卡并盖上"检查时间"。

        这是 discover/invoke 里唯一的"改状态"入口：
        discover 成功 → _mark(id, healthy=True, discovered_tools=..., last_error=None)
        discover 失败 → _mark(id, healthy=False, last_error=...)
        invoke 偶发失败 → _mark(id, last_error=...)（只记错误，不动 healthy）
        """
        self.statuses[server_id] = self.statuses[server_id].model_copy(update=updates).checked()

    @classmethod
    def load(
        cls, path: str | Path, inprocess_servers: dict[str, FastMCP] | None = None
    ) -> "MCPRegistry":
        """装配入口（main.py: ``MCPRegistry.load(settings.mcp_config_path, {...})``）。

        只做"声明与登记"，不建立任何连接——真正的建连在 discover()。
        """
        configured = Path(path)
        # ① 读配置源：优先读用户给的 JSON 文件；文件不存在则回落到安装包内的默认配置
        #   （default_servers.json：外部 arXiv + 两个内置 server），保证开箱即用。
        content = (
            configured.read_text(encoding="utf-8")
            if configured.is_file()
            else files("app.mcp").joinpath("default_servers.json").read_text(encoding="utf-8")
        )
        payload = json.loads(content)      # ② JSON 文本 → 普通 dict
        # ③ pydantic 校验（models.py 里的规则：id 格式、transport 一致性、id 唯一等），
        #    配置写错在此直接抛错 = 启动即失败（fail fast），而不是第一次调用才炸。
        return cls(MCPGatewayConfig.model_validate(payload), inprocess_servers)

    def transport(self, server: MCPServerConfig):
        """按 transport 类型造出"怎么连这台 server"的传输对象。三种形态：
          inprocess → 直接取 main.py 传入的 FastMCP 对象（同一进程内调用）
          stdio     → 起一个子进程（如 uvx arxiv-mcp-server），只透传白名单环境变量
          http      → 走网络，强制 https（除本机回环），可带 auth token
        """
        if server.transport == "inprocess":
            # 按 id 取实现对象；若配置声明了 inprocess 但 main.py 没注册，
            # KeyError 会在 discover 里被捕获并标成 unhealthy（降级，不崩启动）。
            return self.inprocess_servers[server.id]
        if server.transport == "stdio":
            # 安全：绝不把整个进程环境变量塞给第三方子进程，
            # 只透传配置里 env_allowlist 允许的名字（如 API key）。
            env = {name: os.environ[name] for name in server.env_allowlist if name in os.environ}
            assert server.command is not None  # models.py 的校验已保证 stdio 必有 command
            return StdioTransport(server.command, server.args, env=env or None)
        # http 分支：远端端点必须 https（本机回环 127.0.0.1/localhost/::1 除外），
        # 防止把明文密钥发到非加密链路；auth_env 指定从哪个环境变量读鉴权 token。
        url = str(server.url)
        parsed = urlparse(url)
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise MCPContractError("Remote HTTP MCP endpoints must use HTTPS")
        auth = os.getenv(server.auth_env) if server.auth_env else None
        return StreamableHttpTransport(url, auth=auth)

    async def discover(self) -> None:
        """启动体检（main.py 里 lifespan 阶段调用一次，worker 起来之前）。

        对每台启用的 server 真实连一次并核对契约：
          - 连上并 list_tools()，拿到它"实际提供哪些工具"；
          - 校验配置里每个"能力→工具"绑定都真实存在（配置/版本漂移在此暴露）；
          - 通过 → 健康卡标 healthy；任何一步失败 → 只标 unhealthy（降级），不阻塞启动。
        之后 CapabilityRouter 路由时只会选 healthy 的 server。
        """
        for server in self.config.servers:
            if not server.enabled:          # 配置里 enabled=false 的整台跳过
                continue
            try:
                # async with Client(...)：建立一次会话；进出一次只为"列工具"，用后即关
                async with Client(self.transport(server), timeout=server.timeout_seconds) as client:
                    tools = await client.list_tools()
                names = sorted(tool.name for tool in tools)
                # 对账：配置里声明的能力绑定（binding.tool）必须在这台 server 的工具里
                missing = sorted(
                    binding.tool for binding in server.capabilities.values()
                    if binding.tool not in names
                )
                if missing:
                    raise MCPContractError(f"Configured tools not discovered: {', '.join(missing)}")
                self._mark(server.id, healthy=True, discovered_tools=names, last_error=None)
            except Exception as exc:  # noqa: BLE001 - 发现失败只降级，不拖垮启动
                self._mark(
                    server.id,
                    healthy=False,
                    last_error=f"{type(exc).__name__}: {str(exc)[:500]}",  # 截断防日志爆炸
                )

    def candidates(self, capability: str) -> list[tuple[MCPServerConfig, str]]:
        """按能力名选出"现在能用的提供者"，按优先级排序。

        过滤条件：server 已启用 ∧ 健康卡 healthy ∧ 该 server 声明了此能力。
        排序键：(binding.priority, 配置顺序) —— priority 小者优先；
        因此能力 X 可以有主备多台（如 arxiv 检索：外部 server 优先、内置兜底）。
        """
        values = []
        for order, server in enumerate(self.config.servers):
            binding = server.capabilities.get(capability)
            status = self.statuses[server.id]
            if server.enabled and status.healthy and binding:
                values.append((binding.priority, order, server, binding.tool))
        return [(server, tool) for _, _, server, tool in sorted(values)]

    async def invoke(self, server: MCPServerConfig, tool: str, arguments: dict[str, Any]) -> Any:
        """对一台明确的 server 执行一次工具调用（参数已是业务准备好的 dict）。

        返回值规整成"纯 Python 数据"：优先 structured_content（结构化输出），
        否则把 content 块转成 dict（能 model_dump 的转 JSON 结构）或字符串。
        任何非契约类异常 → 记到健康卡 + 抛 MCPTemporaryError（让上层可换一台重试）。
        """
        try:
            # 同一时间只允许一个调用碰这台 server（in-process server 的 lifespan 串行化）
            async with (
                self._server_locks[server.id],
                Client(self.transport(server), timeout=server.timeout_seconds) as client,
            ):
                result = await client.call_tool(tool, arguments, timeout=server.timeout_seconds)
            # 规整返回值：能直接给 pydantic 解析的优先用 structured_content
            if result.structured_content is not None:
                return result.structured_content
            return [
                block.model_dump(mode="json") if hasattr(block, "model_dump") else str(block)
                for block in result.content
            ]
        except (MCPContractError, ValueError):
            raise            # 契约/参数类错误：不该 fallback，直接抛给调用方
        except Exception as exc:
            self._mark(server.id, last_error=f"{type(exc).__name__}: {str(exc)[:500]}")
            raise MCPTemporaryError(f"{server.id}.{tool} failed: {exc}") from exc

    def _server_view(self, server: MCPServerConfig) -> dict[str, Any]:
        """把"配置里的静态身份 + 运行期健康卡"拼成 /mcp/status 里的一台 server 条目。"""
        runtime = self.statuses[server.id]
        return {
            "id": server.id,
            "source": server.source,
            "transport": server.transport,
            "enabled": server.enabled,
            "healthy": runtime.healthy,
            "discovered_tools": runtime.discovered_tools,
            "last_error": runtime.last_error,
            "checked_at": runtime.checked_at,
        }

    def public_status(self) -> dict[str, Any]:
        """诊断快照（GET /mcp/status 直接返回它）：
          - status   ：所有启用 server 都 healthy → "ready"，否则 "degraded"
          - servers  ：每台的静态身份 + 健康卡（给运维看哪台挂了、发现到什么工具）
          - routes   ：每个能力当前有哪些候选 server（按优先级排序的 id 列表）
        """
        return {
            "status": "ready" if all(
                self.statuses[s.id].healthy for s in self.config.servers if s.enabled
            ) else "degraded",
            "servers": [self._server_view(s) for s in self.config.servers],
            "routes": {
                capability: [server.id for server, _ in self.candidates(capability)]
                for capability in sorted({
                    c for s in self.config.servers for c in s.capabilities
                })
            },
        }


class CapabilityRouter:
    """能力路由器：业务代码只认"能力名"，由它决定调哪台 server。

    文档客户端(document.py) / 文献客户端(literature.py) 拿到的就是这个对象；
    它们把「能力名 + 参数」传进来，路由负责选路、容灾与结果适配。
    """

    def __init__(self, registry: MCPRegistry) -> None:
        self.registry = registry

    async def call(
        self,
        capability: str,
        arguments: dict[str, Any],
        arguments_by_server: dict[str, dict[str, Any]] | None = None,
        result_adapter=None,
    ) -> Any:
        """调用一个能力：按优先级逐个试健康的提供者，失败自动换下一台。

        - candidates()         ：先拿到"该能力当前健康提供者"的有序列表
        - arguments_by_server  ：可选——不同 server 需要不同参数形状时，
                                 （如外部 arxiv server 要 max_results 而不是 sources）
        - result_adapter       ：可选——把原始返回适配成客户端期望的模型/结构
        - 语义：MCPTemporaryError（瞬时错误，含"适配结果失败"）→ 记录后换下一台；
                全部失败 → 抛带"试过哪些 server"汇总的 MCPTemporaryError。
        """
        candidates = self.registry.candidates(capability)
        if not candidates:
            raise MCPTemporaryError(f"No healthy MCP server provides {capability}")
        failures: list[str] = []                       # 记录试过谁、为什么失败（最终汇总用）
        for server, tool in candidates:
            try:
                # 允许按 server 覆盖参数：默认所有候选用同一份 arguments
                effective = (arguments_by_server or {}).get(server.id, arguments)
                result = await self.registry.invoke(server, tool, effective)
                if result_adapter is not None:
                    try:
                        result = result_adapter(result)   # 把 server 原始输出转成业务结构
                    except Exception as exc:  # 适配失败 = 这台输出不合规，视为可换台重试
                        raise MCPTemporaryError(
                            f"{server.id}.{tool} returned an invalid result: {exc}"
                        ) from exc
                return result                            # 成功即返回，不再试后面的候选
            except MCPTemporaryError as exc:
                failures.append(f"{server.id}: {exc}")   # 记一笔，继续试下一台
        raise MCPTemporaryError(
            f"No MCP route completed {capability} (tried: {', '.join(failures)})"
        )
