"""A small asyncio HTTP/1.1 server, standard library only.

NetOrbit serves one page, a JSON API and a server-sent event stream to a handful of
browsers on the home network. That doesn't need a web framework, and leaving one out
keeps the install small on a Raspberry Pi.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.cookies import SimpleCookie
import json
import logging
import mimetypes
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

_LOGGER = logging.getLogger(__name__)

MAX_HEADER = 32 * 1024
MAX_BODY = 4 * 1024 * 1024
KEEPALIVE_TIMEOUT = 30


class HTTPError(Exception):
    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or HTTPStatus(status).phrase)
        self.status = status
        self.message = message or HTTPStatus(status).phrase


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes
    params: dict[str, str] = field(default_factory=dict)
    remote: str | None = None

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body)
        except ValueError as err:
            raise HTTPError(400, "Body is not valid JSON") from err

    def arg(self, name: str, default: str | None = None) -> str | None:
        vals = self.query.get(name)
        return vals[0] if vals else default

    @property
    def cookies(self) -> dict[str, str]:
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("cookie", ""))
        except Exception:  # noqa: BLE001
            return {}
        return {k: m.value for k, m in jar.items()}


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/plain; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, data: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
        return cls(status, json.dumps(data, default=str).encode(), "application/json", dict(headers or {}))

    @classmethod
    def file(cls, path: Path, cache: str = "no-cache") -> Response:
        try:
            body = path.read_bytes()
        except OSError as err:
            raise HTTPError(404) from err
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        return cls(200, body, ctype, {"Cache-Control": cache})


class EventStream:
    """Returned by a handler to keep the connection open for server-sent events."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=100)

    def send(self, event: str, data: Any = None) -> None:
        msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass  # a stuck browser just misses updates

    def close(self) -> None:
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


Handler = Callable[[Request], Awaitable[Response | EventStream]]


class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern[str], Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        regex = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$"
        self._routes.append((method, re.compile(regex), handler))

    def match(self, method: str, path: str) -> tuple[Handler, dict[str, str]]:
        allowed = False
        for m, regex, handler in self._routes:
            found = regex.match(path)
            if not found:
                continue
            if m == method or (method == "HEAD" and m == "GET"):
                return handler, {k: unquote(v) for k, v in found.groupdict().items()}
            allowed = True
        raise HTTPError(405 if allowed else 404)


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "same-origin",
}


class HTTPServer:
    def __init__(self, router: Router, middleware: Callable[[Request, Handler], Awaitable[Response | EventStream]] | None = None) -> None:
        self.router = router
        self.middleware = middleware
        self._server: asyncio.base_events.Server | None = None
        self.streams: set[EventStream] = set()

    async def start(self, host: str, port: int) -> None:
        self._server = await asyncio.start_server(self._client, host, port, limit=MAX_HEADER)

    @property
    def sockets(self):
        return self._server.sockets if self._server else []

    async def stop(self) -> None:
        for s in list(self.streams):
            s.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            while True:
                try:
                    req = await asyncio.wait_for(self._read_request(reader, peer), KEEPALIVE_TIMEOUT)
                except (TimeoutError, asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
                    return
                except HTTPError as err:
                    await self._write(writer, Response(err.status, err.message.encode()), keep=False)
                    return
                if req is None:
                    return
                keep = req.headers.get("connection", "").lower() != "close"
                result = await self._dispatch(req)
                if isinstance(result, EventStream):
                    await self._stream(writer, result)
                    return
                if req.method == "HEAD":
                    result.headers["Content-Length"] = str(len(result.body))
                    result.body = b""
                await self._write(writer, result, keep)
                if not keep:
                    return
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _read_request(self, reader: asyncio.StreamReader, peer) -> Request | None:  # noqa: ANN001
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.LimitOverrunError as err:
            raise HTTPError(431) from err
        except asyncio.IncompleteReadError as err:
            if not err.partial:
                return None
            raise
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, _version = lines[0].split(" ", 2)
        except ValueError as err:
            raise HTTPError(400) from err
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if "chunked" in headers.get("transfer-encoding", "").lower():
            raise HTTPError(411, "Send a Content-Length")
        try:
            length = int(headers.get("content-length") or 0)
        except ValueError as err:
            raise HTTPError(400) from err
        if length < 0 or length > MAX_BODY:
            raise HTTPError(413)
        body = await reader.readexactly(length) if length else b""
        url = urlsplit(target)
        return Request(
            method=method.upper(), path=unquote(url.path) or "/", query=parse_qs(url.query),
            headers=headers, body=body, remote=peer[0] if peer else None,
        )

    async def _dispatch(self, req: Request) -> Response | EventStream:
        try:
            handler, params = self.router.match(req.method, req.path)
            req.params = params
            if self.middleware:
                return await self.middleware(req, handler)
            return await handler(req)
        except HTTPError as err:
            if req.path.startswith("/api/"):
                return Response.json({"error": err.message}, err.status)
            return Response(err.status, err.message.encode())
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error handling %s %s", req.method, req.path)
            return Response.json({"error": "Internal error, see the log"}, 500)

    async def _write(self, writer: asyncio.StreamWriter, resp: Response, keep: bool) -> None:
        headers = {
            "Content-Type": resp.content_type,
            "Content-Length": str(len(resp.body)),
            "Connection": "keep-alive" if keep else "close",
            **SECURITY_HEADERS,
            **resp.headers,
        }
        if resp.body == b"" and "Content-Length" in resp.headers:
            headers["Content-Length"] = resp.headers["Content-Length"]
        reason = HTTPStatus(resp.status).phrase if resp.status in HTTPStatus._value2member_map_ else ""
        head = f"HTTP/1.1 {resp.status} {reason}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
        writer.write(head.encode("latin-1") + resp.body)
        await writer.drain()

    async def _stream(self, writer: asyncio.StreamWriter, stream: EventStream) -> None:
        self.streams.add(stream)
        try:
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\n"
                b"Connection: keep-alive\r\nX-Accel-Buffering: no\r\n\r\n: hello\n\n"
            )
            await writer.drain()
            while True:
                try:
                    msg = await asyncio.wait_for(stream.queue.get(), 25)
                except (TimeoutError, asyncio.TimeoutError):
                    msg = ": keepalive\n\n"
                if msg is None:
                    return
                writer.write(msg.encode())
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.streams.discard(stream)
