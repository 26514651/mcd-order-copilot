"""McDonald's China MCP client -- standard library only, zero dependencies.

Implements just enough of the MCP "Streamable HTTP" transport to call tools
over JSON-RPC 2.0:

    POST https://mcp.mcd.cn
    Authorization: Bearer <token>
    Accept: application/json, text/event-stream

The server may answer either with a bare JSON body or with an SSE stream
(``event: message`` / ``data: {...}``). Both shapes are handled here, because
the transport picks the framing based on how the request is negotiated.

Why no official SDK: an agent skill should be runnable by ``python cli.py``
on a clean machine. Fewer moving parts means fewer ways for a reviewer's
first run to fail.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_ENDPOINT = "https://mcp.mcd.cn"
TOKEN_ENV = "MCD_MCP_TOKEN"
PROTOCOL_VERSION = "2025-03-26"


def extract_json_payload(text: str) -> Any:
    """Pull the response object out of a ``tools/call`` text block.

    The server does not return bare JSON. Each tool result is a *human-readable
    preamble* (field documentation, output-format rules) followed by the raw
    payload, e.g.::

        每个门店的基本信息按单行展示：
        - **门店名称**: data[].storeName
        ...
        {"success":true,"code":200,...,"data":[...]}

    So a plain ``json.loads`` always fails. We scan for every ``{`` that starts
    a JSON object and keep the largest one that parses -- the preamble contains
    braces too, but never a document bigger than the payload itself.
    """
    if not text:
        return None
    stripped = text.strip()

    try:
        direct = json.loads(stripped)
        if isinstance(direct, (dict, list)):
            return direct
    except json.JSONDecodeError:
        pass

    # ```json ... ``` fences do occur in some responses.
    fence = re.search(r"```(?:json)?\s*(.+?)```", stripped, re.S)
    if fence:
        try:
            fenced = json.loads(fence.group(1).strip())
            if isinstance(fenced, (dict, list)):
                return fenced
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    best: tuple[int, Any] | None = None
    for match in re.finditer(r'\{\s*"', stripped):
        try:
            obj, _end = decoder.raw_decode(stripped, match.start())
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, (dict, list)):
            continue
        score = len(json.dumps(obj, ensure_ascii=False))
        if best is None or score > best[0]:
            best = (score, obj)
    return best[1] if best else None


NUTRITION_COLUMNS = ("name", "desc", "kj", "kcal", "protein", "fat", "carb", "sodium", "calcium")


def parse_nutrition_rows(text: str) -> list[dict]:
    """Parse the CSV-ish payload returned by ``list-nutrition-foods``.

    The tool does not return a JSON array. Its ``data`` field is a single
    string shaped like::

        [160]{productName,nutritionDescription,energyKj,energyKcal,protein,...}
        猪柳麦满分,null,1288,308,16,16,24,781,213
        巨无霸,null,2146,513,27,26,42,961,171
        ...

    A header line in square brackets, then one comma-separated row per product.
    Newlines may be literal ``\\n`` escapes or real breaks depending on the
    transport, so both are handled.
    """
    if not text:
        return []
    body = text.replace("\\n", "\n")
    rows: list[dict] = []
    for line in body.split("\n"):
        line = line.strip().strip("`").strip()
        if not line or line.startswith("["):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < len(NUTRITION_COLUMNS):
            continue
        row = dict(zip(NUTRITION_COLUMNS, parts[: len(NUTRITION_COLUMNS)]))
        if not row.get("name") or not row.get("kcal"):
            continue
        if not row["kcal"].replace(".", "", 1).isdigit():
            continue
        rows.append(row)
    return rows


class MCPError(RuntimeError):
    """Raised when the endpoint answers with a JSON-RPC error object."""

    def __init__(self, code: int | None, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class MCPTransportError(RuntimeError):
    """Raised on network / HTTP failures so callers can tell them apart."""

    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class ToolResult:
    """Normalised result of a ``tools/call``."""

    name: str
    text: str
    is_error: bool = False
    data: Any = None
    raw: dict = field(default_factory=dict)


def _parse_body(raw: str, content_type: str) -> dict | None:
    """Return the JSON-RPC envelope from a bare JSON or an SSE body."""
    looks_like_sse = (
        "text/event-stream" in (content_type or "").lower()
        or raw.lstrip().startswith("event:")
        or raw.lstrip().startswith("data:")
    )
    if looks_like_sse:
        envelope = None
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                candidate = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and ("result" in candidate or "error" in candidate):
                envelope = candidate
        return envelope
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Last resort: an SSE body served with the wrong content type.
        for line in raw.splitlines():
            if line.strip().startswith("data:"):
                try:
                    return json.loads(line.strip()[5:].strip())
                except json.JSONDecodeError:
                    continue
    return None


class MCPClient:
    """Minimal synchronous MCP client for mcp.mcd.cn."""

    def __init__(
        self,
        token: str | None = None,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: int = 45,
        max_retries: int = 2,
        verbose: bool = False,
    ) -> None:
        token = token or os.environ.get(TOKEN_ENV, "")
        if not token:
            raise MCPTransportError(
                f"No token. Set the {TOKEN_ENV} environment variable "
                "(apply at https://open.mcd.cn/mcp) or run with --demo."
            )
        self._token = token
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_retries = max_retries
        self.verbose = verbose
        self.session_id: str | None = None
        self.server_info: dict = {}
        self._request_id = 0
        self.calls: list[dict] = []  # lightweight call log, used by evidence mode

    # ---------------------------------------------------------------- transport

    def _post(self, payload: dict, expect_reply: bool = True) -> tuple[dict | None, str, int | None]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "mcd-order-copilot/0.1",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8", "replace")
                    ctype = response.headers.get("Content-Type", "")
                    status = response.status
                    session = response.headers.get("Mcp-Session-Id") or response.headers.get("mcp-session-id")
                if session:
                    self.session_id = session
                return _parse_body(raw, ctype), raw, status
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                status = exc.code
                session = exc.headers.get("Mcp-Session-Id")
                if session:
                    self.session_id = session
                # 429 / 5xx are worth retrying; 4xx generally are not.
                if status in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    last_error = MCPTransportError(f"HTTP {status}", status, raw)
                    continue
                if status == 401:
                    raise MCPTransportError(
                        f"HTTP 401 -- token rejected. Re-issue it at https://open.mcd.cn/mcp",
                        status,
                        raw,
                    )
                raise MCPTransportError(f"HTTP {status}", status, raw)
            except Exception as exc:  # URLError, timeout, ...
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
        raise MCPTransportError(f"Request failed after {self.max_retries + 1} attempts: {last_error}")

    def _call(self, method: str, params: dict | None = None, notify: bool = False) -> dict | None:
        self._request_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notify:
            payload["id"] = self._request_id
        if params is not None:
            payload["params"] = params

        envelope, raw, status = self._post(payload, expect_reply=not notify)
        if self.verbose:
            print(f"  -> {method}  HTTP {status}  {len(raw)} B")
        if notify:
            return None
        if envelope is None:
            raise MCPTransportError(f"{method}: unparseable response", status, raw)
        if "error" in envelope:
            err = envelope["error"] or {}
            raise MCPError(err.get("code"), err.get("message", "unknown error"), err.get("data"))
        return envelope.get("result") or {}

    # ------------------------------------------------------------------- public

    def connect(self) -> dict:
        """Run the MCP handshake. Safe to call more than once."""
        result = self._call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcd-order-copilot", "version": "0.1.0"},
            },
        )
        self.server_info = (result or {}).get("serverInfo", {})
        self._call("notifications/initialized", None, notify=True)
        return self.server_info

    def list_tools(self) -> list[dict]:
        result = self._call("tools/list", {}) or {}
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> ToolResult:
        """Call a tool and return its first text content block, JSON-decoded when possible."""
        result = self._call("tools/call", {"name": name, "arguments": arguments or {}})
        result = result or {}
        text = ""
        for block in result.get("content", []) or []:
            if block.get("type") == "text":
                text += block.get("text", "")
        decoded = extract_json_payload(text)
        self.calls.append({"tool": name, "arguments": arguments or {}, "bytes": len(text)})
        return ToolResult(
            name=name,
            text=text,
            is_error=bool(result.get("isError")),
            data=decoded,
            raw=result,
        )

    # ------------------------------------------------------------ domain helpers

    def list_nutrition_foods(self) -> list[dict]:
        """All nutrition rows (no parameters required).

        The payload arrives as a delimited string nested in ``data``, so the
        JSON path and the string path are both handled here.
        """
        result = self.call_tool("list-nutrition-foods")
        payload = result.data
        if isinstance(payload, dict):
            inner = payload.get("data")
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
            if isinstance(inner, str):
                rows = parse_nutrition_rows(inner)
                if rows:
                    return rows
            for key in ("list", "foods", "items"):
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    return [r for r in candidate if isinstance(r, dict)]
        elif isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        # Fallback: the CSV body may be embedded directly in the text block.
        return parse_nutrition_rows(result.text)

    def find_store(self, keyword: str = "麦当劳", city: str = "") -> dict | None:
        args: dict[str, Any] = {"beType": 1, "searchType": 2, "keyword": keyword}
        if city:
            args["city"] = city
        result = self.call_tool("query-nearby-stores", args)
        stores = _dig(result.data, ("data",))
        if isinstance(stores, list) and stores:
            return stores[0]
        return None

    def query_meals(self, store_code: str, order_type: int = 1, be_type: int = 1) -> dict:
        result = self.call_tool(
            "query-meals",
            {"storeCode": store_code, "orderType": order_type, "beType": be_type},
        )
        return _dig(result.data, ("data",)) or {}

    def query_meal_detail(self, store_code: str, code: str, order_type: int = 1, be_type: int = 1) -> dict:
        result = self.call_tool(
            "query-meal-detail",
            {"storeCode": store_code, "orderType": order_type, "beType": be_type, "code": code},
        )
        return _dig(result.data, ("data",)) or {}


def _dig(obj: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return None
    return obj
