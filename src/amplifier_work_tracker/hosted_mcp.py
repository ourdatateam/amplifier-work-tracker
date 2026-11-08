"""Small authenticated, stateless Streamable HTTP MCP adapter for hosted work.

The MCP SDK is an optional runtime dependency of this module so the domain
manager and its tests remain usable in a development environment without it.
The deployed server installs ``mcp>=1.20,<2`` and runs this module directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapter import NAME_RE

_DEFAULT_TOKEN_FILE = "/etc/beads/mcp-tokens.json"
_DEFAULT_PORT = 8104
_DEFAULT_IDLE_TIMEOUT = 300
_MIN_IDLE_TIMEOUT = 60
_MAX_IDLE_TIMEOUT = 1800


def _idle_timeout(value: int | None = None) -> int:
    if value is None:
        raw = os.environ.get("AMPLIFIER_WORK_TRACKER_MCP_IDLE_SECONDS", str(_DEFAULT_IDLE_TIMEOUT))
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError("MCP idle timeout must be between 60 and 1800 seconds") from exc
    if not _MIN_IDLE_TIMEOUT <= value <= _MAX_IDLE_TIMEOUT:
        raise ValueError("MCP idle timeout must be between 60 and 1800 seconds")
    return value


def _load_clients(path: str | os.PathLike[str]) -> list[dict[str, str]]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        clients = raw["clients"]
        if not isinstance(clients, list):
            raise ValueError
        valid = []
        for item in clients:
            actor, digest, role = item["actor"], item["token_sha256"], item["role"]
            if (
                not isinstance(actor, str)
                or not actor
                or not isinstance(digest, str)
                or len(digest) != 64
                or role not in {"worker", "human", "reader"}
            ):
                raise ValueError
            int(digest, 16)
            valid.append({"actor": actor, "token_sha256": digest.lower(), "role": role})
        return valid
    except Exception as exc:
        raise ValueError("MCP token configuration is invalid or unavailable") from exc


def authenticate(
    authorization: str | None, clients: list[dict[str, str]]
) -> tuple[str, str] | None:
    """Match a bearer token by SHA-256 digest without revealing which check failed."""
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    if not token:
        return None
    candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
    match: tuple[str, str] | None = None
    for client in clients:
        equal = hmac.compare_digest(candidate, client["token_sha256"])
        if equal:
            match = (client["actor"], client["role"])
    return match


class BearerAuthASGI:
    """Authenticate every HTTP request before MCP can initialize or list tools."""

    def __init__(self, app: Any, token_file: str | os.PathLike[str]):
        self.app = app
        self.token_file = token_file

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            clients = _load_clients(self.token_file)
            headers = dict(scope.get("headers", []))
            identity = authenticate(headers.get(b"authorization", b"").decode("latin1"), clients)
        except Exception:
            identity = None
        if identity is None:
            body = b'{"error":"unauthorized"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        # FastMCP dispatches tools from its own task group. Keep verified
        # identity on this HTTP request's scope so the injected SDK Context
        # can retrieve the right principal even across task boundaries.
        request_scope = dict(scope)
        request_scope["state"] = {**scope.get("state", {}), "hosted_mcp_identity": identity}
        await self.app(request_scope, receive, send)


@dataclass
class _SessionRecord:
    actor: str
    session: Any
    expires_at: float
    last_activity: float
    held_item: str | None = None
    expired: bool = False


def _result_value(result: Any) -> dict[str, Any]:
    output = result.output
    # ToolResult outputs are domain JSON, but keep the transport robust if a
    # future upstream method returns an unexpected object.
    try:
        json.dumps(output)
    except (TypeError, ValueError):
        output = str(output)
    return {"success": bool(result.success), "output": output}


def _run_coroutine(coroutine: Any) -> Any:
    """Run an async WorkTrackerSession method from sync watchdog/shutdown code."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    result: list[Any] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, name="hosted-mcp-cleanup")
    thread.start()
    thread.join(timeout=5)
    if thread.is_alive():
        raise TimeoutError("work tracker cleanup timed out")
    if errors:
        raise errors[0]
    return result[0] if result else None


def require_role(identity: tuple[str, str] | None, allowed: tuple[str, ...]) -> str:
    if identity is None:
        raise PermissionError("authenticated MCP request context is missing")
    actor, role = identity
    if role not in allowed:
        raise PermissionError("role is not allowed to perform this operation")
    return actor


class HostedWorkManager:
    """Own actor-isolated WorkTrackerSession instances and bounded client leases."""

    def __init__(
        self,
        session_factory: Callable[[str], Any] | None = None,
        idle_timeout: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        start_watchdog: bool = True,
    ):
        self.idle_timeout = _idle_timeout(idle_timeout)
        self._clock = clock
        self._factory = session_factory or _make_upstream_session
        self._records: dict[str, _SessionRecord] = {}
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._watchdog: threading.Thread | None = None
        if start_watchdog:
            self._watchdog = threading.Thread(
                target=self._watch, name="hosted-mcp-watchdog", daemon=True
            )
            self._watchdog.start()

    def _new_session(self, actor: str) -> Any:
        return self._factory(actor)

    def _watch(self) -> None:
        while not self._stopping.wait(min(15, max(1, self.idle_timeout / 4))):
            self.sweep()

    async def _invoke(self, session: Any, method: str, *args: Any, **kwargs: Any) -> Any:
        return await getattr(session, method)(*args, **kwargs)

    def _close_record(self, record: _SessionRecord) -> None:
        if record.expired:
            return
        record.expired = True
        if record.held_item:
            try:
                _run_coroutine(self._invoke(record.session, "unclaim", record.held_item))
            except Exception:
                # Keep this private and generic; never leak backend output or credentials.
                pass
        # WorkTrackerSession intentionally has no public shutdown API. Its
        # held record owns the renewal thread; stop and join it even when the
        # best-effort domain release failed, so server shutdown cannot leak it.
        held = getattr(record.session, "_held", None)
        if held is not None:
            held.stop.set()
            thread = getattr(held, "thread", None)
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5)

    def sweep(self) -> None:
        now = self._clock()
        with self._lock:
            for session_id, record in list(self._records.items()):
                if now >= record.expires_at:
                    self._close_record(record)
                    del self._records[session_id]

    def _session(self, actor: str, session_id: str | None = None) -> tuple[Any, str | None]:
        if session_id is None:
            return self._new_session(actor), None
        with self._lock:
            record = self._records.get(session_id)
            if record is None or record.actor != actor or record.expired:
                raise ValueError("unknown or expired session")
            now = self._clock()
            if now >= record.expires_at:
                self._close_record(record)
                del self._records[session_id]
                raise ValueError("unknown or expired session")
            record.last_activity = now
            record.expires_at = now + self.idle_timeout
            return record.session, session_id

    async def work_claim(self, actor: str, project: str = "home") -> dict[str, Any]:
        if not _valid_project(project):
            return {"success": False, "output": "invalid project name"}
        session = self._new_session(actor)
        session_id = secrets.token_urlsafe(32)
        now = self._clock()
        record = _SessionRecord(actor, session, now + self.idle_timeout, now)
        with self._lock:
            self._records[session_id] = record
        try:
            result = await self._invoke(session, "claim", project)
        except Exception:
            held = getattr(session, "_held", None)
            if held is not None:
                record.held_item = getattr(held, "item_id", None)
            with self._lock:
                self._records.pop(session_id, None)
                self._close_record(record)
            return {"success": False, "output": "work claim failed"}
        value = _result_value(result)
        if result.success and isinstance(result.output, dict):
            record.held_item = result.output.get("claimed")
            if record.held_item is None:
                # Upstream explicitly reports the normal empty-queue outcome.
                # Do not create an apparently active lease for a claim that
                # never happened.
                with self._lock:
                    self._records.pop(session_id, None)
                value["output"] = result.output
                return value
        value["session_id"] = session_id
        return value

    async def work_heartbeat(self, actor: str, session_id: str) -> dict[str, Any]:
        try:
            self._session(actor, session_id)
            return {
                "success": True,
                "output": {"session_id": session_id, "expires_in_seconds": self.idle_timeout},
            }
        except ValueError as exc:
            return {"success": False, "output": str(exc)}

    async def work_declare(self, actor: str, session_id: str, state: str) -> dict[str, Any]:
        return await self._session_call(actor, session_id, "declare", state)

    async def work_release(self, actor: str, session_id: str, item_id: str) -> dict[str, Any]:
        value = await self._session_call(actor, session_id, "unclaim", item_id)
        if value.get("success"):
            with self._lock:
                record = self._records.get(session_id)
                if record and record.actor == actor:
                    record.held_item = None
        return value

    async def work_resolve(
        self, actor: str, session_id: str, item_id: str, reason: str
    ) -> dict[str, Any]:
        value = await self._session_call(actor, session_id, "resolve", item_id, reason)
        if value.get("success"):
            with self._lock:
                record = self._records.get(session_id)
                if record and record.actor == actor:
                    record.held_item = None
        return value

    async def work_file(
        self,
        actor: str,
        session_id: str,
        title: str,
        description: str | None = None,
        acceptance: str | None = None,
    ) -> dict[str, Any]:
        return await self._session_call(
            actor, session_id, "file", title, description=description, acceptance=acceptance
        )

    async def _session_call(
        self, actor: str, session_id: str, method: str, *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            session, _ = self._session(actor, session_id)
            return _result_value(await self._invoke(session, method, *args, **kwargs))
        except ValueError as exc:
            return {"success": False, "output": str(exc)}
        except Exception:
            return {"success": False, "output": "work operation failed"}

    async def work_query(
        self,
        actor: str,
        kind: str,
        project: str = "home",
        item_id: str | None = None,
        session_id: str | None = None,
        limit: int | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        if not _valid_project(project):
            return {"success": False, "output": "invalid project name"}
        if kind not in {"status", "stats", "list", "item"}:
            return {"success": False, "output": "kind must be status, stats, list, or item"}
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100
        ):
            return {"success": False, "output": "limit must be between 1 and 100"}
        if kind == "item" and not item_id:
            return {"success": False, "output": "item_id is required for kind=item"}
        try:
            session, _ = self._session(actor, session_id)
            if kind == "status":
                result = await self._invoke(session, "status")
            elif kind == "stats":
                result = await self._invoke(session, "stats", project)
            else:
                result = await self._invoke(
                    session,
                    "list_items",
                    project,
                    item_id=item_id if kind == "item" else None,
                    status=status,
                    limit=limit,
                )
            return _result_value(result)
        except ValueError as exc:
            return {"success": False, "output": str(exc)}
        except Exception:
            return {"success": False, "output": "work query failed"}

    async def work_add(
        self,
        actor: str,
        project: str,
        title: str,
        description: str | None = None,
        acceptance: str | None = None,
        priority: int | None = None,
    ) -> dict[str, Any]:
        if not _valid_project(project):
            return {"success": False, "output": "invalid project name"}
        if priority is not None:
            return {
                "success": False,
                "output": "priority is not supported by WorkTrackerSession.add",
            }
        session = self._new_session(actor)
        try:
            return _result_value(
                await self._invoke(
                    session, "add", project, title, description=description, acceptance=acceptance
                )
            )
        except Exception:
            return {"success": False, "output": "work add failed"}

    def close(self) -> None:
        self._stopping.set()
        if self._watchdog is not None and self._watchdog is not threading.current_thread():
            self._watchdog.join(timeout=5)
        with self._lock:
            records, self._records = list(self._records.values()), {}
            for record in records:
                self._close_record(record)


def _valid_project(project: str) -> bool:
    return isinstance(project, str) and NAME_RE.fullmatch(project) is not None


def _make_upstream_session(actor: str) -> Any:
    try:
        from amplifier_module_tool_work_tracker import WorkTrackerSession
    except ImportError as exc:
        raise RuntimeError(
            "amplifier-core and the editable work-tracker module are required"
        ) from exc
    return WorkTrackerSession({"actor": actor})


def _request_principal(ctx: Any) -> tuple[str, str]:
    request = ctx.request_context.request
    scope = getattr(request, "scope", {}) if request is not None else {}
    identity = scope.get("state", {}).get("hosted_mcp_identity")
    if identity is None:
        raise RuntimeError("authenticated MCP request context is missing")
    return identity


def create_asgi_app(
    token_file: str | os.PathLike[str] | None = None,
    idle_timeout: int | None = None,
    session_factory: Callable[[str], Any] | None = None,
) -> Any:
    """Create FastMCP's stateless Streamable HTTP app, protected before MCP dispatch."""
    try:
        from mcp.server.fastmcp import Context, FastMCP
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError as exc:
        raise RuntimeError("install the official Python mcp package: mcp>=1.20,<2") from exc
    # FastMCP evaluates postponed annotations against this module's globals.
    # Bind the optional SDK type only when the SDK-backed app is requested.
    globals()["Context"] = Context
    manager = HostedWorkManager(session_factory, idle_timeout)
    hosts = [
        "beads.ssiops.com",
        "beads.ssiops.com:80",
        "beads.ssiops.com:443",
        "beads.ssiops.com:8104",
        "localhost",
        "localhost:80",
        "localhost:443",
        "localhost:8104",
        "127.0.0.1",
        "127.0.0.1:80",
        "127.0.0.1:443",
        "127.0.0.1:8104",
    ]
    mcp = FastMCP(
        "amplifier-work-tracker",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts
        ),
    )

    def permitted(ctx: Any, *roles: str) -> tuple[str, str]:
        identity = _request_principal(ctx)
        actor = require_role(identity, roles)
        return actor, identity[1]

    @mcp.tool()
    async def work_query(
        kind: str,
        project: str = "home",
        item_id: str | None = None,
        session_id: str | None = None,
        limit: int | None = None,
        status: str | None = None,
        *,
        ctx: Context,
    ) -> dict[str, Any]:
        actor, _ = permitted(ctx, "worker", "human", "reader")
        return await manager.work_query(actor, kind, project, item_id, session_id, limit, status)

    @mcp.tool()
    async def work_add(
        project: str,
        title: str,
        description: str | None = None,
        acceptance: str | None = None,
        priority: int | None = None,
        *,
        ctx: Context,
    ) -> dict[str, Any]:
        actor, _ = permitted(ctx, "worker", "human")
        return await manager.work_add(actor, project, title, description, acceptance, priority)

    @mcp.tool()
    async def work_claim(project: str = "home", *, ctx: Context) -> dict[str, Any]:
        # Human review claims establish an actor-owned fenced session too.
        actor, _ = permitted(ctx, "worker", "human")
        return await manager.work_claim(actor, project)

    @mcp.tool()
    async def work_heartbeat(session_id: str, *, ctx: Context) -> dict[str, Any]:
        actor, _ = permitted(ctx, "worker", "human")
        return await manager.work_heartbeat(actor, session_id)

    @mcp.tool()
    async def work_declare(session_id: str, state: str, *, ctx: Context) -> dict[str, Any]:
        actor, _ = permitted(ctx, "worker")
        return await manager.work_declare(actor, session_id, state)

    @mcp.tool()
    async def work_release(session_id: str, item_id: str, *, ctx: Context) -> dict[str, Any]:
        actor, _ = permitted(ctx, "worker", "human")
        return await manager.work_release(actor, session_id, item_id)

    @mcp.tool()
    async def work_resolve(
        session_id: str, item_id: str, reason: str, *, ctx: Context
    ) -> dict[str, Any]:
        actor, _ = permitted(ctx, "worker", "human")
        return await manager.work_resolve(actor, session_id, item_id, reason)

    @mcp.tool()
    async def work_file(
        session_id: str,
        title: str,
        description: str | None = None,
        acceptance: str | None = None,
        *,
        ctx: Context,
    ) -> dict[str, Any]:
        actor, _ = permitted(ctx, "worker")
        return await manager.work_file(actor, session_id, title, description, acceptance)

    class LifespanCleanup:
        def __init__(self, app: Any):
            self.app = app

        async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] == "lifespan":

                async def wrapped_send(message: dict[str, Any]) -> None:
                    if message["type"] == "lifespan.shutdown.complete":
                        manager.close()
                    await send(message)

                await self.app(scope, receive, wrapped_send)
            else:
                await self.app(scope, receive, send)

    app = LifespanCleanup(mcp.streamable_http_app())
    path = token_file or os.environ.get(
        "BEADS_MCP_TOKEN_FILE",
        os.environ.get("AMPLIFIER_WORK_TRACKER_MCP_TOKEN_FILE", _DEFAULT_TOKEN_FILE),
    )
    return BearerAuthASGI(app, path)


def main() -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("install uvicorn to run the hosted MCP endpoint") from exc
    port = int(os.environ.get("BEADS_MCP_PORT", str(_DEFAULT_PORT)))
    uvicorn.run(create_asgi_app(), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
