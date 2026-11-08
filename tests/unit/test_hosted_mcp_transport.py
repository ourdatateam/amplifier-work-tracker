"""Exercise the installed MCP SDK's real Streamable HTTP ASGI transport."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("mcp.server.fastmcp")
from amplifier_work_tracker.hosted_mcp import create_asgi_app  # noqa: E402


class RecordingSession:
    def __init__(self, actor: str):
        self.actor = actor
        self.held = None

    async def list_items(self, project: str, **kwargs):
        return SimpleNamespace(success=True, output={"project": project, "actor": self.actor})

    async def status(self):
        return SimpleNamespace(success=True, output={"actor": self.actor})

    async def claim(self, project: str):
        self.held = f"item-{self.actor}"
        return SimpleNamespace(success=True, output={"claimed": self.held, "project": project})

    async def unclaim(self, item_id: str):
        if item_id != self.held:
            return SimpleNamespace(success=False, output="not held here")
        self.held = None
        return SimpleNamespace(success=True, output={"released": item_id})


def _token_file(path: Path) -> dict[str, str]:
    tokens = {"alice": "alice-token", "bob": "bob-token", "reader": "reader-token"}
    path.write_text(
        json.dumps(
            {
                "clients": [
                    {
                        "actor": actor,
                        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                        "role": "reader" if actor == "reader" else "worker",
                    }
                    for actor, token in tokens.items()
                ]
            }
        ),
        encoding="utf-8",
    )
    return tokens


async def _asgi_request(
    app,
    token: str | None,
    request_id: int,
    method: str,
    params: dict | None = None,
    *,
    host: str = "beads.ssiops.com",
) -> tuple[int, Any]:
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            **({"params": params} if params is not None else {}),
        }
    ).encode()
    headers = [
        (b"host", host.encode()),
        (b"content-type", b"application/json"),
        (b"accept", b"application/json, text/event-stream"),
        (b"content-length", str(len(payload)).encode()),
    ]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    received = False
    messages = []

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": payload, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "server": (host, 80),
            "client": ("127.0.0.1", 12345),
            "state": {},
        },
        receive,
        send,
    )
    status = next(
        message["status"] for message in messages if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    try:
        decoded = json.loads(body) if body else {}
    except json.JSONDecodeError:
        decoded = body.decode("utf-8", errors="replace")
    return status, decoded


async def _lifespan(app):
    incoming = asyncio.Queue()
    outgoing = asyncio.Queue()

    async def receive():
        return await incoming.get()

    async def send(message):
        await outgoing.put(message)

    task = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send))
    await incoming.put({"type": "lifespan.startup"})
    startup = await outgoing.get()
    assert startup["type"] == "lifespan.startup.complete"
    return incoming, outgoing, task


def test_sdk_lifespan_initialize_list_call_auth_and_host_validation(tmp_path):
    token_file = tmp_path / "tokens.json"
    tokens = _token_file(token_file)
    actors: list[str] = []
    actor_lock = threading.Lock()

    def factory(actor: str):
        with actor_lock:
            actors.append(actor)
        return RecordingSession(actor)

    app = create_asgi_app(token_file=token_file, idle_timeout=60, session_factory=factory)

    async def exercise():
        incoming, outgoing, task = await _lifespan(app)
        for token in (None, "bad-token"):
            for request_id, method in ((1, "initialize"), (2, "tools/list")):
                status, _ = await _asgi_request(
                    app,
                    token,
                    request_id,
                    method,
                    {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                )
                assert status == 401

        status, initialized = await _asgi_request(
            app,
            tokens["alice"],
            3,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        assert status == 200 and "result" in initialized
        status, listed = await _asgi_request(app, tokens["alice"], 4, "tools/list")
        assert status == 200
        tools = listed["result"]["tools"]
        query = next(tool for tool in tools if tool["name"] == "work_query")
        optional_session_id = query["inputSchema"]["properties"]["session_id"]
        assert (
            optional_session_id.get("type") == "string"
            or optional_session_id.get("anyOf", [])[0].get("type") == "string"
        )
        assert "session_id" not in query["inputSchema"].get("required", [])

        status, result = await _asgi_request(
            app,
            tokens["reader"],
            5,
            "tools/call",
            {
                "name": "work_query",
                "arguments": {"kind": "list", "project": "home"},
            },
        )
        assert status == 200
        assert result["result"]["structuredContent"]["output"]["actor"] == "reader"
        assert "reader" in actors

        status, status_result = await _asgi_request(
            app,
            tokens["reader"],
            6,
            "tools/call",
            {"name": "work_query", "arguments": {"kind": "status"}},
        )
        assert status == 200
        assert status_result["result"]["structuredContent"]["output"]["actor"] == "reader"

        status, claim = await _asgi_request(
            app,
            tokens["alice"],
            7,
            "tools/call",
            {"name": "work_claim", "arguments": {"project": "home"}},
        )
        assert status == 200
        claim_output = claim["result"]["structuredContent"]
        session_id = claim_output["session_id"]
        item_id = claim_output["output"]["claimed"]
        status, heartbeat = await _asgi_request(
            app,
            tokens["alice"],
            8,
            "tools/call",
            {"name": "work_heartbeat", "arguments": {"session_id": session_id}},
        )
        assert status == 200 and heartbeat["result"]["structuredContent"]["success"]
        status, release = await _asgi_request(
            app,
            tokens["alice"],
            9,
            "tools/call",
            {
                "name": "work_release",
                "arguments": {"session_id": session_id, "item_id": item_id},
            },
        )
        assert status == 200 and release["result"]["structuredContent"]["success"]

        rejected_host, _ = await _asgi_request(
            app,
            tokens["alice"],
            10,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
            host="attacker.invalid",
        )
        assert rejected_host == 421
        await incoming.put({"type": "lifespan.shutdown"})
        shutdown = await outgoing.get()
        assert shutdown["type"] == "lifespan.shutdown.complete"
        await task

    asyncio.run(exercise())


def test_concurrent_sdk_calls_keep_each_token_actor_isolated(tmp_path):
    token_file = tmp_path / "tokens.json"
    tokens = _token_file(token_file)
    seen: list[str] = []
    lock = threading.Lock()

    def factory(actor: str):
        with lock:
            seen.append(actor)
        return RecordingSession(actor)

    app = create_asgi_app(token_file=token_file, idle_timeout=60, session_factory=factory)
    outputs: dict[str, dict] = {}

    async def exercise():
        incoming, outgoing, task = await _lifespan(app)

        async def invoke(actor: str):
            init_status, _ = await _asgi_request(
                app,
                tokens[actor],
                10,
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": actor, "version": "1"},
                },
            )
            assert init_status == 200
            call_status, call = await _asgi_request(
                app,
                tokens[actor],
                11,
                "tools/call",
                {
                    "name": "work_claim",
                    "arguments": {"project": "home"},
                },
            )
            assert call_status == 200
            outputs[actor] = call["result"]["structuredContent"]

        await asyncio.wait_for(asyncio.gather(invoke("alice"), invoke("bob")), timeout=10)
        await incoming.put({"type": "lifespan.shutdown"})
        shutdown = await outgoing.get()
        assert shutdown["type"] == "lifespan.shutdown.complete"
        await task

    asyncio.run(exercise())

    assert outputs["alice"]["output"]["claimed"] == "item-alice"
    assert outputs["bob"]["output"]["claimed"] == "item-bob"
    assert outputs["alice"]["session_id"] != outputs["bob"]["session_id"]
    assert set(seen) == {"alice", "bob"}
