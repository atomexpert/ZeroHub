"""
Unit & ASGI API tests for Antigravity Hub v=66:
- Per-user project folder ordering (`database.get_user_projects_order` / `set_user_projects_order`)
- Self-healing SQLite column check (`_ensure_projects_order_column`) & idempotent `init_db()` migration
- `POST /api/projects/reorder` (with `projects_reordered` WebSocket broadcast) & `GET /api/projects`
- Strict user isolation (`user_a` vs `user_b`) and reset via empty list `[]`
- Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER touches live `data/hub.db` or `run_server.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _unlink_test_junctions(root: Path) -> None:
    if not root.exists():
        return
    for p in sorted(root.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        try:
            if hasattr(os.path, "isjunction") and os.path.isjunction(p):
                os.rmdir(p)
            elif p.is_symlink() and p.is_dir():
                os.rmdir(p)
        except Exception:
            pass


class _MiniAsgiResponse:
    def __init__(self, status_code: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self.content = body

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> dict:
        return json.loads(self.text)


class _MiniAsgiClient:
    def __init__(self, app, *, client: tuple[str, int] = ("127.0.0.1", 52660)) -> None:
        self._app = app
        self._client = client

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> _MiniAsgiResponse:
        parsed_url = urllib.parse.urlsplit(path)
        raw_path = parsed_url.path or "/"
        qs = parsed_url.query
        if params:
            extra_qs = urllib.parse.urlencode(params)
            qs = f"{qs}&{extra_qs}" if qs else extra_qs

        payload = b""
        req_headers: list[tuple[bytes, bytes]] = [(b"host", b"127.0.0.1:8080")]
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            req_headers.append((b"content-type", b"application/json"))
            req_headers.append((b"content-length", str(len(payload)).encode("ascii")))
        if headers:
            for k, v in headers.items():
                req_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": urllib.parse.unquote(raw_path),
            "raw_path": raw_path.encode("ascii", errors="ignore"),
            "query_string": qs.encode("ascii", errors="ignore"),
            "headers": req_headers,
            "client": self._client,
            "server": ("127.0.0.1", 8080),
        }

        sent_body = False
        resp_status = 500
        resp_headers: dict[str, str] = {}
        resp_chunks: list[bytes] = []

        async def _receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def _send(message):
            nonlocal resp_status, resp_headers
            mtype = message.get("type")
            if mtype == "http.response.start":
                resp_status = int(message.get("status", 200))
                resp_headers = {
                    k.decode("latin-1").lower(): v.decode("latin-1")
                    for k, v in (message.get("headers") or [])
                }
            elif mtype == "http.response.body":
                chunk = message.get("body") or b""
                if chunk:
                    resp_chunks.append(chunk)

        await self._app(scope, _receive, _send)
        return _MiniAsgiResponse(resp_status, resp_headers, b"".join(resp_chunks))


class TestProjectsOrderV66(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database

        self._live_db = ROOT_DIR / "data" / "hub.db"
        self._live_stat_before = (
            (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            if self._live_db.exists()
            else None
        )

        self._tmp = tempfile.TemporaryDirectory(
            prefix="agy_test_v66_", ignore_cleanup_errors=True
        )
        self.tmp_root = Path(self._tmp.name).resolve()

        self._orig = {
            "cfg_db": config.DB_PATH,
            "db_db": database.DB_PATH,
            "cfg_cli": config.CLI_HOMES_DIR,
            "cfg_agy": config.AGY_CLI_DATA_DIR,
            "eng_agy": agy_engine.AGY_CLI_DATA_DIR,
            "cfg_ws": config.USER_WORKSPACES_DIR,
            "db_ws": database.USER_WORKSPACES_DIR,
            "br_ws": brain_reader.USER_WORKSPACES_DIR,
            "cfg_sh": config.SHARED_PROJECTS_DIR,
            "db_sh": database.SHARED_PROJECTS_DIR,
            "br_sh": brain_reader.SHARED_PROJECTS_DIR,
            "cfg_up": config.UPLOADS_DIR,
            "eng_up": agy_engine.UPLOADS_DIR,
            "cfg_sbx": config.CHAT_SANDBOX_DIR,
            "eng_sbx": agy_engine.CHAT_SANDBOX_DIR,
        }

        tmp_db = self.tmp_root / "test_v66.db"
        tmp_cli = self.tmp_root / "cli_homes"
        tmp_agy = self.tmp_root / "agy_cli_data"
        tmp_ws = self.tmp_root / "workspaces"
        tmp_sh = self.tmp_root / "shared_projects"
        tmp_up = self.tmp_root / "uploads"
        tmp_sbx = self.tmp_root / "chat_sandbox"
        for d in (tmp_cli, tmp_agy, tmp_ws, tmp_sh, tmp_up, tmp_sbx):
            d.mkdir(parents=True, exist_ok=True)

        config.DB_PATH = tmp_db
        database.DB_PATH = tmp_db
        config.CLI_HOMES_DIR = tmp_cli
        config.AGY_CLI_DATA_DIR = tmp_agy
        agy_engine.AGY_CLI_DATA_DIR = tmp_agy
        config.USER_WORKSPACES_DIR = tmp_ws
        database.USER_WORKSPACES_DIR = tmp_ws
        brain_reader.USER_WORKSPACES_DIR = tmp_ws
        config.SHARED_PROJECTS_DIR = tmp_sh
        database.SHARED_PROJECTS_DIR = tmp_sh
        brain_reader.SHARED_PROJECTS_DIR = tmp_sh
        config.UPLOADS_DIR = tmp_up
        agy_engine.UPLOADS_DIR = tmp_up
        config.CHAT_SANDBOX_DIR = tmp_sbx
        agy_engine.CHAT_SANDBOX_DIR = tmp_sbx

    async def asyncTearDown(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database

        config.DB_PATH = self._orig["cfg_db"]
        database.DB_PATH = self._orig["db_db"]
        config.CLI_HOMES_DIR = self._orig["cfg_cli"]
        config.AGY_CLI_DATA_DIR = self._orig["cfg_agy"]
        agy_engine.AGY_CLI_DATA_DIR = self._orig["eng_agy"]
        config.USER_WORKSPACES_DIR = self._orig["cfg_ws"]
        database.USER_WORKSPACES_DIR = self._orig["db_ws"]
        brain_reader.USER_WORKSPACES_DIR = self._orig["br_ws"]
        config.SHARED_PROJECTS_DIR = self._orig["cfg_sh"]
        database.SHARED_PROJECTS_DIR = self._orig["db_sh"]
        brain_reader.SHARED_PROJECTS_DIR = self._orig["br_sh"]
        config.UPLOADS_DIR = self._orig["cfg_up"]
        agy_engine.UPLOADS_DIR = self._orig["eng_up"]
        config.CHAT_SANDBOX_DIR = self._orig["cfg_sbx"]
        agy_engine.CHAT_SANDBOX_DIR = self._orig["eng_sbx"]

        _unlink_test_junctions(self.tmp_root)
        self._tmp.cleanup()

        if self._live_stat_before is not None:
            live_after = (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            self.assertEqual(
                self._live_stat_before,
                live_after,
                "Live data/hub.db must NEVER be modified by tests!",
            )

    async def test_self_healing_column_and_db_helpers(self) -> None:
        import app.database as database

        # Simulate pre-v66 legacy `users` table without `projects_order_json` before cold restart
        with contextlib.closing(sqlite3.connect(str(database.DB_PATH))) as conn:
            conn.execute(
                """
                CREATE TABLE users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    workspace_dir TEXT NOT NULL DEFAULT '',
                    max_access_mode TEXT NOT NULL DEFAULT 'write_files',
                    sandbox_network TEXT NOT NULL DEFAULT 'internet',
                    allowed_models_json TEXT NOT NULL DEFAULT '[]',
                    daily_token_limit INTEGER NOT NULL DEFAULT 0,
                    quota_reserve_pct INTEGER NOT NULL DEFAULT 15,
                    session_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO users (id, username, password_hash, created_at, updated_at)
                VALUES ('u1', 'legacy_user', 'h', '2026-01-01', '2026-01-01')
                """
            )
            conn.commit()

        # Calling get/set BEFORE init_db() must self-heal the column cleanly
        self.assertEqual(await database.get_user_projects_order("u1"), [])
        saved = await database.set_user_projects_order(
            "u1",
            ["  PolymarketWalletCheck  ", "AlphaProj", "polymarketwalletcheck", "", "BetaProj"],
        )
        self.assertEqual(saved, ["PolymarketWalletCheck", "AlphaProj", "BetaProj"])
        self.assertEqual(
            await database.get_user_projects_order("u1"),
            ["PolymarketWalletCheck", "AlphaProj", "BetaProj"],
        )

        # Subsequent init_db() must remain idempotent and preserve saved order
        await database.init_db()
        self.assertEqual(
            await database.get_user_projects_order("u1"),
            ["PolymarketWalletCheck", "AlphaProj", "BetaProj"],
        )

    async def test_api_reorder_projects_isolation_and_reset(self) -> None:
        import app.auth as auth
        import app.database as database
        import app.main as main

        await database.init_db()
        user_a = await database.create_user(
            username="alice_order",
            display_name="Alice",
            password="pass1234",
            max_access_mode="write_files",
        )
        user_b = await database.create_user(
            username="bob_order",
            display_name="Bob",
            password="pass5678",
            max_access_mode="write_files",
        )

        # Create 4 project directories in Alice's workspace and 3 in Bob's workspace
        ws_a = Path(user_a["workspace_dir"])
        for pname in ("Alpha", "Beta", "Gamma", "PolymarketWalletCheck"):
            (ws_a / pname).mkdir(parents=True, exist_ok=True)

        ws_b = Path(user_b["workspace_dir"])
        for pname in ("FirstBob", "SecondBob", "ThirdBob"):
            (ws_b / pname).mkdir(parents=True, exist_ok=True)

        tok_a = auth.create_user_session_token(
            user_id=user_a["id"], session_version=user_a["session_version"]
        )
        tok_b = auth.create_user_session_token(
            user_id=user_b["id"], session_version=user_b["session_version"]
        )
        hdr_a = {"Authorization": f"Bearer {tok_a}"}
        hdr_b = {"Authorization": f"Bearer {tok_b}"}

        client = _MiniAsgiClient(main.app)

        # Capture broadcast_user WebSocket payloads
        ws_events: list[tuple[str, dict]] = []
        orig_broadcast = main.ws_manager.broadcast_user

        async def _spy_broadcast(uid: str, payload: dict) -> None:
            ws_events.append((uid, payload))
            await orig_broadcast(uid, payload)

        main.ws_manager.broadcast_user = _spy_broadcast
        try:
            # 1. Default natural order for Alice
            r_init = await client.request("GET", "/api/projects", headers=hdr_a)
            self.assertEqual(r_init.status_code, 200)
            init_data = r_init.json()
            self.assertEqual(init_data["projects_order"], [])
            natural_order_a = [p["name"] for p in init_data["projects"]]
            self.assertEqual(
                set(natural_order_a),
                {"Alpha", "Beta", "Gamma", "PolymarketWalletCheck"},
            )

            # Pick the last two in natural order to move to #1 and #2 (leaving the other two unlisted)
            pick_first = natural_order_a[-1]
            pick_second = natural_order_a[-2]
            remaining_natural = [
                n for n in natural_order_a if n not in {pick_first, pick_second}
            ]

            # 2. Alice reorders (including a case-insensitive duplicate to test normalization)
            r_reord = await client.request(
                "POST",
                "/api/projects/reorder",
                json_body={"project_names": [pick_first, pick_second, pick_second.lower()]},
                headers=hdr_a,
            )
            self.assertEqual(r_reord.status_code, 200)
            self.assertEqual(
                r_reord.json(),
                {"ok": True, "project_names": [pick_first, pick_second]},
            )
            self.assertTrue(
                any(
                    uid == user_a["id"]
                    and evt.get("type") == "projects_reordered"
                    and evt.get("project_names") == [pick_first, pick_second]
                    for uid, evt in ws_events
                )
            )

            # 3. GET /api/projects for Alice returns custom order first + remaining projects in natural order
            r_after = await client.request("GET", "/api/projects", headers=hdr_a)
            self.assertEqual(r_after.status_code, 200)
            after_data = r_after.json()
            self.assertEqual(after_data["projects_order"], [pick_first, pick_second])
            self.assertEqual(
                [p["name"] for p in after_data["projects"]],
                [pick_first, pick_second, *remaining_natural],
            )

            # 4. Bob's order is 100% isolated from Alice's order
            r_bob_init = await client.request("GET", "/api/projects", headers=hdr_b)
            self.assertEqual(r_bob_init.status_code, 200)
            self.assertEqual(r_bob_init.json()["projects_order"], [])
            self.assertEqual(
                set(p["name"] for p in r_bob_init.json()["projects"]),
                {"FirstBob", "SecondBob", "ThirdBob"},
            )

            await client.request(
                "POST",
                "/api/projects/reorder",
                json_body={"project_names": ["ThirdBob", "FirstBob", "SecondBob"]},
                headers=hdr_b,
            )

            # 5. Alice resets her order via [] -> returns to exact natural order while Bob keeps his custom order!
            r_reset = await client.request(
                "POST",
                "/api/projects/reorder",
                json_body={"project_names": []},
                headers=hdr_a,
            )
            self.assertEqual(r_reset.status_code, 200)
            self.assertEqual(r_reset.json(), {"ok": True, "project_names": []})

            r_alice_reset = await client.request("GET", "/api/projects", headers=hdr_a)
            self.assertEqual(r_alice_reset.json()["projects_order"], [])
            self.assertEqual(
                [p["name"] for p in r_alice_reset.json()["projects"]],
                natural_order_a,
            )

            r_bob_still = await client.request("GET", "/api/projects", headers=hdr_b)
            self.assertEqual(
                [p["name"] for p in r_bob_still.json()["projects"]],
                ["ThirdBob", "FirstBob", "SecondBob"],
            )

            # 6. Verify static cache-busted assets (>= v=66) & #projectContextMenu markup
            r_html = await client.request("GET", "/")
            self.assertEqual(r_html.status_code, 200)
            self.assertRegex(r_html.text, r"/static/styles\.css\?v=(?:6[6-9]|[7-9]\d|\d{3,})")
            self.assertRegex(r_html.text, r"/static/app\.js\?v=(?:6[6-9]|[7-9]\d|\d{3,})")
            self.assertIn('id="projectContextMenu"', r_html.text)
            self.assertIn('data-proj-act="move-top"', r_html.text)
            self.assertIn('data-proj-act="reset-order"', r_html.text)

            r_sw = await client.request("GET", "/sw.js")
            self.assertEqual(r_sw.status_code, 200)
            self.assertRegex(r_sw.text, r'"agy-hub-v(?:6[6-9]|[7-9]\d|\d{3,})"')
        finally:
            main.ws_manager.broadcast_user = orig_broadcast


if __name__ == "__main__":
    unittest.main()
