"""极简 MCP Streamable HTTP 客户端。

只依赖标准库：不装 SDK 也能跑，这也是本项目"clone 下来就能试"的前提。
协议细节见 https://github.com/M-China/mcd-mcp-server
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URL = "https://mcp.mcd.cn"
DEFAULT_PROTOCOL = "2025-06-18"


class McpError(RuntimeError):
    """连接 / 鉴权 / 协议层错误，消息直接面向使用者。"""


def _decode_body(raw: str, content_type: str) -> dict[str, Any] | None:
    """MCP 可能回 JSON，也可能回 SSE；两种都要吃。"""
    text = raw.strip()
    if not text:
        return None

    if "text/event-stream" in content_type or text.startswith(("event:", "data:", ":")):
        message = None
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk:
                continue
            try:
                parsed = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
                message = parsed
        return message

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpError(f"服务端返回的不是 JSON-RPC：{text[:200]}") from exc


class McpClient:
    def __init__(self, token: str, url: str = DEFAULT_URL, timeout: float = 30.0):
        if not token or token == "YOUR_MCP_TOKEN":
            raise McpError(
                "没有 MCP Token。去 https://open.mcd.cn/mcp 手机号登录 → 控制台 → 激活，"
                "然后 export MCD_MCP_TOKEN='...'"
            )
        self._token = token.strip()
        self._url = url.rstrip("/") or DEFAULT_URL
        self._timeout = timeout
        self._session_id: str | None = None
        self._next_id = 0
        self.tool_schemas: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ HTTP
    def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": DEFAULT_PROTOCOL,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        request = urllib.request.Request(self._url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self._session_id = session_id
                raw = response.read().decode("utf-8", "replace")
                ctype = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 401:
                raise McpError("401：Token 无效或已过期，回控制台重新复制一份。") from exc
            if exc.code == 429:
                raise McpError("429：超过 600 次/分钟限流，歇一分钟再跑。") from exc
            raise McpError(f"HTTP {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"连不上 {self._url}：{exc.reason}") from exc

        return _decode_body(raw, ctype)

    # ------------------------------------------------------------- JSON-RPC
    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        message = self._post(payload)
        if message is None:
            raise McpError(f"{method} 没有返回内容")
        if "error" in message:
            error = message["error"]
            raise McpError(f"{method} 失败：{error.get('message', error)}")
        return message.get("result")

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._post(payload)
        except McpError:
            # 通知没有响应体，个别实现会返回 202 空响应，忽略即可
            pass

    # ---------------------------------------------------------------- 公开
    def initialize(self) -> dict[str, Any] | None:
        result = self._request(
            "initialize",
            {
                "protocolVersion": DEFAULT_PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "mc-oncall", "version": "0.1.0"},
            },
        )
        self._notify("notifications/initialized")
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list") or {}
        tools = result.get("tools", []) or []
        for tool in tools:
            name = tool.get("name")
            if name:
                self.tool_schemas[name] = tool.get("inputSchema", {})
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}}) or {}
        if result.get("isError"):
            raise McpError(f"{name} 返回错误：{_content_text(result)[:300]}")
        return result


def _content_text(result: dict[str, Any]) -> str:
    parts = []
    for block in result.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def tool_payload(result: dict[str, Any]) -> Any:
    """把 tools/call 的 content 转成 Python 对象。

    麦当劳部分工具返回纯文本（营养表是自定义格式），所以解析失败时原文返回，
    由上层适配器决定怎么读。
    """
    text = _content_text(result).strip()
    if not text:
        return None

    if "structuredContent" in result and result["structuredContent"]:
        structured = result["structuredContent"]
        if isinstance(structured, dict) and len(structured) == 1:
            return next(iter(structured.values()))
        return structured

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text
