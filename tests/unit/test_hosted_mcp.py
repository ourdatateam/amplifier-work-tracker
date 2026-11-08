from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from amplifier_work_tracker.hosted_mcp import (  # noqa: E402
    BearerAuthASGI,
    HostedWorkManager,
    authenticate,
    require_role,
)


class FakeSession:
    def __init__(self, actor: str):
        self.actor = actor
        self.item_id = f"item-{id(self)}"
        self.claim_calls = 0
        self.release_calls = 0
        self.renewal_stopped = threading.Event()
        self.held_item: str | None = None

    async def claim(self, project: str):
        self.claim_calls += 1
        self.held_item = self.item_id
        return SimpleNamespace(success=True, output={"claimed": self.held_item, "project": project})

    async def unclaim(self, item_id: str):
        if item_id != self.held_item:
            return SimpleNamespace(success=False, output="not held here")
        self.release_calls += 1
        self.held_item = None
        self.renewal_stopped.set()
        return SimpleNamespace(success=True, output={"released": item_id})

    async def resolve(self, item_id: str, reason: str):
        if item_id != self.held_item:
            return SimpleNamespace(success=False, output="not held here")
        self.held_item = None
        return SimpleNamespace(success=True, output={"resolved": item_id, "reason": reason})

    async def list_items(self, project: str, **kwargs):
        return SimpleNamespace(success=True, output={"project": project, "items": []})

    async def status(self):
        return SimpleNamespace(success=True, output={"projects": []})

    async def stats(self, project: str):
        return SimpleNamespace(success=True, output={"project": project})

    async def add(self, project: str, title: str, **kwargs):
        return SimpleNamespace(success=True, output={"added": "item-2", "project": project})


class HostedMCPTests(unittest.TestCase):
    def setUp(self):
        self.sessions: list[FakeSession] = []

        def factory(actor: str) -> FakeSession:
            session = FakeSession(actor)
            self.sessions.append(session)
            return session

        self.now = [0.0]
        self.manager = HostedWorkManager(
            factory, idle_timeout=60, clock=lambda: self.now[0], start_watchdog=False
        )

    def tearDown(self):
        self.manager.close()

    def test_bearer_auth_rejects_missing_and_bad_tokens(self):
        client = {
            "actor": "codex",
            "token_sha256": hashlib.sha256(b"secret").hexdigest(),
            "role": "worker",
        }
        self.assertIsNone(authenticate(None, [client]))
        self.assertIsNone(authenticate("Bearer wrong", [client]))
        self.assertEqual(authenticate("Bearer secret", [client]), ("codex", "worker"))

    def test_http_auth_blocks_initialize_and_tool_list_before_dispatch(self):
        token = "private-token"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tokens.json"
            path.write_text(
                json.dumps(
                    {
                        "clients": [
                            {
                                "actor": "codex",
                                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                                "role": "worker",
                            }
                        ]
                    }
                )
            )
            seen = []

            async def downstream(scope, receive, send):
                seen.append(scope["path"])
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})

            app = BearerAuthASGI(downstream, path)

            async def request(pathname, body, authorization=None):
                messages = []
                headers = (
                    [] if authorization is None else [(b"authorization", authorization.encode())]
                )
                received = False

                async def receive():
                    nonlocal received
                    if received:
                        return {"type": "http.request", "body": b"", "more_body": False}
                    received = True
                    return {"type": "http.request", "body": body, "more_body": False}

                async def send(message):
                    messages.append(message)

                await app(
                    {"type": "http", "method": "POST", "path": pathname, "headers": headers},
                    receive,
                    send,
                )
                return messages

            initialize = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
            tools_list = b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
            for body in (initialize, tools_list):
                rejected = asyncio.run(request("/mcp", body))
                self.assertEqual(rejected[0]["status"], 401)
                self.assertNotIn(b"private-token", rejected[-1]["body"])
                rejected_bad = asyncio.run(request("/mcp", body, "Bearer bad"))
                self.assertEqual(rejected_bad[0]["status"], 401)
            self.assertEqual(seen, [])
            accepted = asyncio.run(request("/mcp", initialize, "Bearer " + token))
            self.assertEqual(accepted[0]["status"], 200)
            self.assertEqual(seen, ["/mcp"])

    def test_reader_cannot_write(self):
        with self.assertRaises(PermissionError):
            require_role(("readbot", "reader"), ("worker", "human"))
        self.assertEqual(require_role(("readbot", "reader"), ("reader",)), "readbot")

    def test_sessions_are_actor_isolated_and_resolve_is_session_fenced(self):
        first = asyncio.run(self.manager.work_claim("alice"))
        second = asyncio.run(self.manager.work_claim("alice"))
        sid1, sid2 = first["session_id"], second["session_id"]
        item_id = first["output"]["claimed"]
        foreign = asyncio.run(self.manager.work_resolve("bob", sid1, item_id, "done"))
        self.assertFalse(foreign["success"])
        foreign_query = asyncio.run(self.manager.work_query("bob", "list", session_id=sid1))
        foreign_heartbeat = asyncio.run(self.manager.work_heartbeat("bob", sid1))
        self.assertFalse(foreign_query["success"])
        self.assertFalse(foreign_heartbeat["success"])
        wrong_session = asyncio.run(self.manager.work_resolve("alice", sid2, item_id, "done"))
        self.assertFalse(wrong_session["success"])
        right_session = asyncio.run(self.manager.work_resolve("alice", sid1, item_id, "done"))
        self.assertTrue(right_session["success"])

    def test_query_is_read_only_and_does_not_claim(self):
        result = asyncio.run(self.manager.work_query("reader", "list", project="home"))
        self.assertTrue(result["success"])
        self.assertEqual(sum(session.claim_calls for session in self.sessions), 0)

    def test_empty_claim_returns_upstream_outcome_without_a_session_lease(self):
        class EmptySession(FakeSession):
            async def claim(self, project: str):
                return SimpleNamespace(
                    success=True,
                    output={"claimed": None, "reason": "no ready work"},
                )

        manager = HostedWorkManager(
            lambda actor: EmptySession(actor), idle_timeout=60, start_watchdog=False
        )
        try:
            result = asyncio.run(manager.work_claim("alice"))
            self.assertEqual(result["output"], {"claimed": None, "reason": "no ready work"})
            self.assertNotIn("session_id", result)
            self.assertEqual(manager._records, {})
        finally:
            manager.close()

    def test_heartbeat_extends_lease_but_expiry_releases_hold(self):
        claimed = asyncio.run(self.manager.work_claim("worker"))
        record = self.manager._records[claimed["session_id"]]
        self.now[0] = 45
        heartbeat = asyncio.run(self.manager.work_heartbeat("worker", claimed["session_id"]))
        self.assertTrue(heartbeat["success"])
        self.now[0] = 61
        self.manager.sweep()
        self.assertEqual(record.session.release_calls, 0)
        self.now[0] = 106
        self.manager.sweep()
        self.assertEqual(record.session.release_calls, 1)
        self.assertTrue(record.session.renewal_stopped.is_set())
        expired = asyncio.run(self.manager.work_heartbeat("worker", claimed["session_id"]))
        self.assertFalse(expired["success"])

    def test_shutdown_releases_session_from_async_lifespan_context(self):
        claimed = asyncio.run(self.manager.work_claim("worker"))
        session = self.manager._records[claimed["session_id"]].session

        async def shutdown():
            self.manager.close()

        asyncio.run(shutdown())
        self.assertEqual(session.release_calls, 1)
        self.assertTrue(session.renewal_stopped.is_set())

    def test_project_paths_and_unbounded_limits_are_refused(self):
        path_result = asyncio.run(self.manager.work_query("reader", "list", project="/tmp/x"))
        limit_result = asyncio.run(self.manager.work_query("reader", "list", limit=101))
        self.assertFalse(path_result["success"])
        self.assertFalse(limit_result["success"])


if __name__ == "__main__":
    unittest.main()
