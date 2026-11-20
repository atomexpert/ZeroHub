"""
Automated isolated verification suite for Release v113/v114:
1. Live `data/hub.db` byte-level immutability guard (`st_mtime_ns`, `st_size`).
2. SQLite schema & legacy migration (`is_archived`, `archived_at` on `chats`).
3. Archive / unarchive lifecycle & `_CHAT_LIST_CACHE` coherence:
   - Archiving forces `pinned=False`, sets `archived_at`, preserves `updated_at`, and invalidates `_CHAT_LIST_CACHE`.
   - `include_archived=False` excludes archived chats cleanly.
   - Moving an archived chat to another project/folder (`is_archived=None`) automatically unarchives it.
4. `/api/projects` active vs archived counters (`hub_chat_count`, `archived_chat_count`) + middleware cache invalidation,
   `get_recent_project_chats_for_passport` active-first ordering, working cross-links & `@` mentions for owner,
   and strict Zero-Trust RBAC isolation against foreign users.
5. `POST /api/projects/{name}/memory/pin-note` (`changelog` and `rules`) + `.agents/rules/hub_project.md` sync.
6. Frontend `node --check`, `py_compile`, static `?v=N` sync across `index.html` & `sw.js`, UTF-8 without BOM,
   Node VM unit tests for v113 UI helpers (`extractPinExcerptFromMessage`, `buildAssistantFooterInnerHtml`,
   `findTargetHqChatForReturn`, `getSortedHandoffSiblingChats`, `hasAnyHqReturnSentFromChat`,
   `computeSidebarChatVisualSig`, `computeMessageItemSig`, `COPY_STRIP_SELECTOR`), and `main.workspace` CSS invariant.

Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER touches live `data/hub.db` or live server ports.
"""

from __future__ import annotations

import asyncio
import json
import os
import py_compile
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Any
from unittest.mock import patch

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("ORT_DISABLE_ALL_GPU", "1")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

LIVE_HUB_DB = ROOT_DIR / "data" / "hub.db"


def _stat_snapshot(p: Path) -> tuple[bool, int, int]:
    if not p.exists():
        return (False, 0, 0)
    st = p.stat()
    return (True, st.st_mtime_ns, st.st_size)


def _subprocess_hidden_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


class _MiniAsgiResponse:
    def __init__(self, status_code: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self.content = body

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> dict[str, Any]:
        return json.loads(self.text)


class _MiniAsgiClient:
    def __init__(
        self,
        app: Any,
        *,
        cookies: dict[str, str] | None = None,
        client: tuple[str, int] = ("127.0.0.1", 52800),
    ) -> None:
        self._app = app
        self._cookies = dict(cookies or {})
        self._client = client

    async def __aenter__(self) -> "_MiniAsgiClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> _MiniAsgiResponse:
        parsed_url = urllib.parse.urlsplit(path)
        raw_path = parsed_url.path or "/"
        qs = parsed_url.query
        if params:
            extra_qs = urllib.parse.urlencode(params)
            qs = f"{qs}&{extra_qs}" if qs else extra_qs

        payload = b""
        req_headers: list[tuple[bytes, bytes]] = [(b"host", b"127.0.0.1:8765")]
        if self._cookies:
            cookie_hdr = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
            req_headers.append((b"cookie", cookie_hdr.encode("latin-1")))
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            req_headers.append((b"content-type", b"application/json"))
            req_headers.append((b"content-length", str(len(payload)).encode("ascii")))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": urllib.parse.unquote(raw_path),
            "raw_path": raw_path.encode("ascii", errors="ignore"),
            "query_string": qs.encode("utf-8"),
            "headers": req_headers,
            "client": self._client,
            "server": ("127.0.0.1", 8765),
        }

        sent_body = False
        resp_status = 500
        resp_headers: dict[str, str] = {}
        resp_chunks: list[bytes] = []

        async def _receive() -> dict[str, Any]:
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def _send(message: dict[str, Any]) -> None:
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

    async def get(self, path: str, *, params: dict[str, str] | None = None) -> _MiniAsgiResponse:
        return await self.request("GET", path, params=params)

    async def patch(self, path: str, *, json: dict[str, Any] | None = None) -> _MiniAsgiResponse:
        return await self.request("PATCH", path, json_body=json)

    async def post(self, path: str, *, json: dict[str, Any] | None = None) -> _MiniAsgiResponse:
        return await self.request("POST", path, json_body=json)


class BranchArchiveAndMemoryV113Tests(unittest.IsolatedAsyncioTestCase):
    """Isolated verification of v113/v114 branch archive, combo HQ return, and project memory pin."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._live_db_before = _stat_snapshot(LIVE_HUB_DB)

    def _assert_live_db_untouched(self) -> None:
        if os.environ.get("HUB_ALLOW_EXTERNAL_LIVE_DB_ACTIVITY") == "1":
            return
        self.assertEqual(
            _stat_snapshot(LIVE_HUB_DB),
            self._live_db_before,
            "Live data/hub.db must remain byte-for-byte untouched!",
        )

    async def asyncSetUp(self) -> None:
        from app import auth, brain_reader, config, database, host_service_manager, main as main_mod

        self.auth = auth
        self.brain_reader = brain_reader
        self.config = config
        self.database = database
        self.main_mod = main_mod

        self._tmp = tempfile.TemporaryDirectory(prefix="hub_v113_test_", ignore_cleanup_errors=True)
        self.tmp_root = Path(self._tmp.name).resolve()
        self.hub_base = self.tmp_root / "Antigravity_Hub"
        data_dir = self.hub_base / "data"
        users_dir = self.hub_base / "users"
        projects_root = users_dir / "admin" / "projects"
        shared_dir = self.hub_base / "shared_projects"
        shared_uploads = shared_dir / "_uploads"
        shared_ctx = shared_dir / "_context"
        cli_homes = data_dir / "cli_homes"
        uploads_dir = data_dir / "uploads"
        self.db_path = data_dir / "hub_isolated_v113.db"

        for d in (
            self.hub_base,
            self.hub_base / "app",
            self.hub_base / ".venv",
            self.hub_base / ".git",
            data_dir,
            users_dir,
            projects_root,
            shared_dir,
            shared_uploads,
            shared_ctx,
            cli_homes,
            uploads_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        (self.hub_base / "run_server.py").write_text("# test\n", encoding="utf-8")
        (self.hub_base / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

        patch_map = {
            "BASE_DIR": self.hub_base,
            "DATA_DIR": data_dir,
            "DB_PATH": self.db_path,
            "USERS_DIR": users_dir,
            "USER_WORKSPACES_DIR": users_dir,
            "PROJECTS_ROOT": projects_root,
            "SHARED_PROJECTS_DIR": shared_dir,
            "SHARED_UPLOADS_DIR": shared_uploads,
            "SHARED_CONTEXT_DIR": shared_ctx,
            "CLI_HOMES_DIR": cli_homes,
            "UPLOADS_DIR": uploads_dir,
        }
        self._patches = []
        for mod in (config, database, brain_reader, main_mod):
            for attr, val in patch_map.items():
                if hasattr(mod, attr):
                    p = patch.object(mod, attr, val)
                    p.start()
                    self._patches.append(p)

        hsm_inst = host_service_manager.HostServiceManager()
        for mod in (host_service_manager, database, main_mod):
            if hasattr(mod, "HOST_SERVICE_MANAGER"):
                p = patch.object(mod, "HOST_SERVICE_MANAGER", hsm_inst)
                p.start()
                self._patches.append(p)

        if hasattr(main_mod, "_PROJECTS_LIST_CACHE"):
            main_mod._PROJECTS_LIST_CACHE.clear()
        await database.close_db_pool()

    async def asyncTearDown(self) -> None:
        try:
            if hasattr(self.main_mod, "_PROJECTS_LIST_CACHE"):
                self.main_mod._PROJECTS_LIST_CACHE.clear()
            await self.database.close_db_pool()
        finally:
            for p in reversed(getattr(self, "_patches", [])):
                p.stop()
            self._tmp.cleanup()
        self._assert_live_db_untouched()

    def _make_client_for_user(self, user: dict[str, Any]) -> _MiniAsgiClient:
        token = self.auth.create_user_session_token(
            str(user["id"]), int(user.get("session_version") or 1)
        )
        return _MiniAsgiClient(
            self.main_mod.app,
            cookies={self.auth.COOKIE_NAME: token},
        )

    async def test_01_sqlite_schema_and_legacy_migration(self) -> None:
        """Verify `is_archived` and `archived_at` columns in fresh DB and `ALTER TABLE` legacy upgrade."""
        self.assertTrue(
            str(Path(self.database.DB_PATH).resolve()).startswith(str(self.tmp_root)),
            "DB_PATH must point strictly inside TemporaryDirectory",
        )

        # Simulate a pre-v113 legacy `chats` table WITHOUT `is_archived` / `archived_at`
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """
                CREATE TABLE chats (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL DEFAULT 'admin',
                    kind TEXT NOT NULL DEFAULT 'chat',
                    project_name TEXT NOT NULL DEFAULT '',
                    project_path TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT 'Новый диалог',
                    model TEXT NOT NULL DEFAULT 'gemini-4-carbon-high',
                    access_mode TEXT NOT NULL DEFAULT 'read_only',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    icon TEXT NOT NULL DEFAULT '',
                    folder TEXT NOT NULL DEFAULT '',
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    agy_conversation_id TEXT NOT NULL DEFAULT '',
                    codex_thread_id TEXT NOT NULL DEFAULT '',
                    codex_account_id TEXT NOT NULL DEFAULT '',
                    claude_session_id TEXT NOT NULL DEFAULT '',
                    claude_account_id TEXT NOT NULL DEFAULT '',
                    last_engine TEXT NOT NULL DEFAULT 'agy',
                    source_desktop_id TEXT NOT NULL DEFAULT '',
                    compacted_summary TEXT NOT NULL DEFAULT '',
                    compacted_up_to_msg_id INTEGER NOT NULL DEFAULT 0,
                    compacted_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO chats (id, title, kind, owner_id, created_at, updated_at)
                VALUES ('legacy_c1', 'Старая ветка', 'chat', 'admin', '2026-11-10T10:00:00Z', '2026-11-10T10:00:00Z')
                """
            )
            conn.commit()
        finally:
            conn.close()

        # Run init_db() to trigger seamless ALTER TABLE migrations
        await self.database.init_db()

        async with self.database.connect_db() as aconn:
            cur = await aconn.execute("PRAGMA table_info(chats)")
            cols = {str(r[1]): r for r in await cur.fetchall()}

        self.assertIn("is_archived", cols)
        self.assertIn("archived_at", cols)

        legacy_chat = await self.database.get_chat("legacy_c1")
        self.assertIsNotNone(legacy_chat)
        assert legacy_chat is not None
        self.assertIs(legacy_chat["is_archived"], False)
        self.assertEqual(legacy_chat["archived_at"], "")

        fresh_chat = await self.database.create_chat(
            title="Новая ветка v113",
            kind="project",
            project_name="DemoProj",
            owner_id="admin",
        )
        self.assertIs(fresh_chat["is_archived"], False)
        self.assertEqual(fresh_chat["archived_at"], "")

    async def test_02_archive_unarchive_cache_coherence_and_move_reset(self) -> None:
        """Verify PATCH archive/unarchive, pinned=False reset, preserved updated_at, cache invalidation, and move auto-unarchive."""
        await self.database.init_db()

        alice = await self.database.create_user(
            username="alice_v113",
            password="AlicePassword123!",
            display_name="Alice",
            role="user",
            max_access_mode="full_access",
        )
        proj_a = (self.hub_base / "users" / alice["username"] / "projects" / "ProjAlpha").resolve()
        proj_b = (self.hub_base / "users" / alice["username"] / "projects" / "ProjBeta").resolve()
        proj_a.mkdir(parents=True, exist_ok=True)
        proj_b.mkdir(parents=True, exist_ok=True)

        branch = await self.database.create_chat(
            title="Фикс бага авторизации",
            kind="project",
            project_name="ProjAlpha",
            project_path=str(proj_a),
            owner_id=alice["id"],
        )
        await self.database.update_chat(branch["id"], pinned=True)
        await self.database.add_message(
            branch["id"],
            user_id=alice["id"],
            role="user",
            content="Проверь токен сессии",
        )
        branch_before = await self.database.get_chat(branch["id"])
        assert branch_before is not None
        self.assertTrue(branch_before["pinned"])
        orig_updated_at = branch_before["updated_at"]

        await asyncio.sleep(0.02)

        async with self._make_client_for_user(alice) as client:
            # 1) Prime `_CHAT_LIST_CACHE` via GET /api/chats
            r_list0 = await client.get("/api/chats")
            self.assertEqual(r_list0.status_code, 200)
            items0 = {c["id"]: c for c in r_list0.json()["chats"]}
            self.assertIn(branch["id"], items0)
            self.assertIs(items0[branch["id"]]["is_archived"], False)
            self.assertIs(items0[branch["id"]]["pinned"], True)

            # 2) Archive via PATCH /api/chats/{id}
            r_arch = await client.patch(
                f"/api/chats/{branch['id']}",
                json={"is_archived": True},
            )
            self.assertEqual(r_arch.status_code, 200)
            arch_chat = r_arch.json()["chat"]
            self.assertIs(arch_chat["is_archived"], True)
            self.assertTrue(bool(arch_chat["archived_at"]))
            self.assertIs(arch_chat["pinned"], False, "Archiving must force pinned=False")
            self.assertEqual(
                arch_chat["updated_at"],
                orig_updated_at,
                "Archiving must NOT bump updated_at",
            )

            # 3) Immediate GET /api/chats must reflect invalidated `_CHAT_LIST_CACHE`
            r_list1 = await client.get("/api/chats")
            self.assertEqual(r_list1.status_code, 200)
            items1 = {c["id"]: c for c in r_list1.json()["chats"]}
            self.assertIs(items1[branch["id"]]["is_archived"], True)
            self.assertIs(items1[branch["id"]]["pinned"], False)
            self.assertTrue(bool(items1[branch["id"]]["archived_at"]))

            # 4) GET /api/chats?include_archived=false must exclude archived branch
            r_active_only = await client.get("/api/chats", params={"include_archived": "false"})
            self.assertEqual(r_active_only.status_code, 200)
            active_ids = {c["id"] for c in r_active_only.json()["chats"]}
            self.assertNotIn(branch["id"], active_ids)

            # 5) Unarchive via PATCH /api/chats/{id} -> is_archived=False, archived_at=""
            r_unarch = await client.patch(
                f"/api/chats/{branch['id']}",
                json={"is_archived": False},
            )
            self.assertEqual(r_unarch.status_code, 200)
            unarch_chat = r_unarch.json()["chat"]
            self.assertIs(unarch_chat["is_archived"], False)
            self.assertEqual(unarch_chat["archived_at"], "")
            self.assertEqual(unarch_chat["updated_at"], orig_updated_at)

            r_active_after = await client.get("/api/chats", params={"include_archived": "false"})
            self.assertIn(branch["id"], {c["id"] for c in r_active_after.json()["chats"]})

            # 6) Re-archive, then move to ProjBeta without explicit `is_archived` -> auto-unarchives!
            r_arch2 = await client.patch(
                f"/api/chats/{branch['id']}",
                json={"is_archived": True},
            )
            self.assertEqual(r_arch2.status_code, 200)
            self.assertIs(r_arch2.json()["chat"]["is_archived"], True)

            r_move = await client.patch(
                f"/api/chats/{branch['id']}",
                json={"kind": "project", "project_name": "ProjBeta"},
            )
            self.assertEqual(r_move.status_code, 200)
            moved_chat = r_move.json()["chat"]
            self.assertEqual(moved_chat["project_name"], "ProjBeta")
            self.assertIs(
                moved_chat["is_archived"],
                False,
                "Moving an archived chat to another project/folder must auto-unarchive it",
            )
            self.assertEqual(moved_chat["archived_at"], "")

    async def test_03_projects_counters_passport_order_crosslinks_and_rbac(self) -> None:
        """Verify `/api/projects` active/archived counters, middleware cache busting, cross-links/@mentions, and RBAC."""
        await self.database.init_db()

        alice = await self.database.create_user(
            username="alice_proj_v113",
            password="AlicePassword123!",
            display_name="Alice",
            role="user",
            max_access_mode="full_access",
        )
        bob = await self.database.create_user(
            username="bob_proj_v113",
            password="BobPassword123!",
            display_name="Bob",
            role="user",
            max_access_mode="full_access",
        )

        alpha_dir = (self.hub_base / "users" / alice["username"] / "projects" / "AlphaApp").resolve()
        alpha_dir.mkdir(parents=True, exist_ok=True)
        bob_dir = (self.hub_base / "users" / bob["username"] / "projects" / "BobApp").resolve()
        bob_dir.mkdir(parents=True, exist_ok=True)

        hq_chat = await self.database.create_chat(
            title="Штаб AlphaApp",
            kind="project",
            project_name="AlphaApp",
            project_path=str(alpha_dir),
            access_mode="read_only",
            owner_id=alice["id"],
        )
        worker_active = await self.database.create_chat(
            title="Ветка API v2",
            kind="project",
            project_name="AlphaApp",
            project_path=str(alpha_dir),
            access_mode="write_files",
            owner_id=alice["id"],
        )
        await asyncio.sleep(0.02)
        worker_to_archive = await self.database.create_chat(
            title="УникальнаяАрхивнаяВетка113",
            kind="project",
            project_name="AlphaApp",
            project_path=str(alpha_dir),
            access_mode="write_files",
            owner_id=alice["id"],
        )
        await self.database.add_message(
            worker_to_archive["id"],
            user_id=alice["id"],
            role="assistant",
            content="Исправлен race condition в пуле коннектов SQLite.",
        )

        bob_hq = await self.database.create_chat(
            title="Штаб BobApp",
            kind="project",
            project_name="BobApp",
            project_path=str(bob_dir),
            owner_id=bob["id"],
        )

        async with self._make_client_for_user(alice) as alice_client:
            # 1) Prime `/api/projects` 3-second TTL cache
            r_projs0 = await alice_client.get("/api/projects")
            self.assertEqual(r_projs0.status_code, 200)
            p_map0 = {p["name"]: p for p in r_projs0.json()["projects"]}
            self.assertIn("AlphaApp", p_map0)
            self.assertEqual(p_map0["AlphaApp"]["hub_chat_count"], 3)
            self.assertEqual(p_map0["AlphaApp"]["archived_chat_count"], 0)

            # 2) Archive `worker_to_archive` and immediately re-query `/api/projects`
            r_arch = await alice_client.patch(
                f"/api/chats/{worker_to_archive['id']}",
                json={"is_archived": True},
            )
            self.assertEqual(r_arch.status_code, 200)

            r_projs1 = await alice_client.get("/api/projects")
            self.assertEqual(r_projs1.status_code, 200)
            p_map1 = {p["name"]: p for p in r_projs1.json()["projects"]}
            self.assertEqual(
                p_map1["AlphaApp"]["hub_chat_count"],
                2,
                "hub_chat_count must exclude archived chats immediately after PATCH /api/chats/{id}",
            )
            self.assertEqual(p_map1["AlphaApp"]["archived_chat_count"], 1)

            # 3) Cross-links (`GET /api/chats/{id}`) and search (`GET /api/chats?query=...`) still work for owner
            r_direct = await alice_client.get(f"/api/chats/{worker_to_archive['id']}")
            self.assertEqual(r_direct.status_code, 200)
            self.assertIs(r_direct.json()["chat"]["is_archived"], True)
            self.assertEqual(len(r_direct.json()["messages"]), 1)

            r_search = await alice_client.get(
                "/api/chats", params={"query": "УникальнаяАрхивнаяВетка113"}
            )
            self.assertEqual(r_search.status_code, 200)
            self.assertEqual(
                [c["id"] for c in r_search.json()["chats"]],
                [worker_to_archive["id"]],
            )

        # 4) `@ИмяВкладки` (`_resolve_allowed_referenced_chats`) still resolves owner's archived sibling branch
        allowed_refs = await self.main_mod._resolve_allowed_referenced_chats(
            hq_chat,
            alice,
            [{"id": worker_to_archive["id"], "scope": "summary"}],
        )
        self.assertEqual(
            [{"id": x["id"], "scope": x["scope"]} for x in allowed_refs],
            [{"id": worker_to_archive["id"], "scope": "summary"}],
        )

        # 5) `get_recent_project_chats_for_passport` puts active chats strictly BEFORE archived chats
        passport_chats = await self.database.get_recent_project_chats_for_passport(
            "AlphaApp", owner_id=alice["id"], limit=8
        )
        passport_ids = [c["id"] for c in passport_chats]
        self.assertEqual(set(passport_ids), {hq_chat["id"], worker_active["id"], worker_to_archive["id"]})
        self.assertEqual(
            passport_ids[-1],
            worker_to_archive["id"],
            "Archived branch must come AFTER active branches even when its updated_at is newer",
        )

        # 6) Strict Zero-Trust RBAC: Bob cannot view, unarchive, or @-reference Alice's archived branch
        async with self._make_client_for_user(bob) as bob_client:
            r_bob_get = await bob_client.get(f"/api/chats/{worker_to_archive['id']}")
            self.assertIn(r_bob_get.status_code, (403, 404))

            r_bob_patch = await bob_client.patch(
                f"/api/chats/{worker_to_archive['id']}",
                json={"is_archived": False},
            )
            self.assertIn(r_bob_patch.status_code, (403, 404))

        bob_refs = await self.main_mod._resolve_allowed_referenced_chats(
            bob_hq,
            bob,
            [{"id": worker_to_archive["id"], "scope": "full"}],
        )
        self.assertEqual(bob_refs, [])

    async def test_04_pin_to_project_memory_and_rules_file_sync(self) -> None:
        """Verify `POST /api/projects/{name}/memory/pin-note` for both `changelog` and `rules` + `.agents/rules/hub_project.md`."""
        await self.database.init_db()

        alice = await self.database.create_user(
            username="alice_mem_v113",
            password="AlicePassword123!",
            display_name="Alice",
            role="user",
            max_access_mode="full_access",
        )
        bob = await self.database.create_user(
            username="bob_mem_v113",
            password="BobPassword123!",
            display_name="Bob",
            role="user",
            max_access_mode="full_access",
        )

        proj_dir = (self.hub_base / "users" / alice["username"] / "projects" / "MemVaultProj").resolve()
        proj_dir.mkdir(parents=True, exist_ok=True)

        worker_chat = await self.database.create_chat(
            title="Ветка кэширования",
            kind="project",
            project_name="MemVaultProj",
            project_path=str(proj_dir),
            owner_id=alice["id"],
        )

        async with self._make_client_for_user(alice) as alice_client:
            # 1) Pin to Pulse (`changelog`)
            r_pulse = await alice_client.post(
                "/api/projects/MemVaultProj/memory/pin-note",
                json={
                    "note": "Внедрён O(1) штамп инвалидации для _CHAT_LIST_CACHE.",
                    "target": "changelog",
                    "chat_id": worker_chat["id"],
                    "chat_title": worker_chat["title"],
                },
            )
            self.assertEqual(r_pulse.status_code, 200)
            mem_pulse = r_pulse.json()["memory"]
            self.assertEqual(r_pulse.json()["target"], "changelog")
            self.assertGreaterEqual(int(mem_pulse.get("changelog_count") or 0), 1)
            changelog_texts = " ".join(str(x.get("summary") or x) for x in mem_pulse.get("changelog", []))
            self.assertIn("_CHAT_LIST_CACHE", changelog_texts)

            # 2) Pin to Passport (`rules`) -> syncs `.agents/rules/hub_project.md` on disk
            rule_note = "ИНВАРИАНТ V113: Архивация ветки обязана снимать флаг pinned и сохранять updated_at."
            r_rules = await alice_client.post(
                "/api/projects/MemVaultProj/memory/pin-note",
                json={
                    "note": rule_note,
                    "target": "rules",
                    "chat_id": worker_chat["id"],
                    "chat_title": worker_chat["title"],
                },
            )
            self.assertEqual(r_rules.status_code, 200)
            mem_rules = r_rules.json()["memory"]
            self.assertEqual(r_rules.json()["target"], "rules")
            self.assertTrue(mem_rules.get("has_passport"))

            rules_md_path = proj_dir / ".agents" / "rules" / "hub_project.md"
            self.assertTrue(rules_md_path.exists(), ".agents/rules/hub_project.md must be synced on disk")
            rules_md_content = rules_md_path.read_text(encoding="utf-8")
            self.assertIn("ИНВАРИАНТ V113", rules_md_content)

        # 3) Foreign user Bob cannot pin notes into Alice's private project
        async with self._make_client_for_user(bob) as bob_client:
            r_bob = await bob_client.post(
                "/api/projects/MemVaultProj/memory/pin-note",
                json={
                    "note": "Попытка чужой записи",
                    "target": "rules",
                },
            )
            self.assertIn(r_bob.status_code, (403, 404))

    def test_05_frontend_syntax_static_v114_node_vm_and_workspace_css_invariant(self) -> None:
        """Verify `node --check`, `py_compile`, `?v=N` sync, UTF-8 without BOM, Node VM v113 logic, and `main.workspace` CSS."""
        touched_files = [
            "app/database.py",
            "app/main.py",
            "app/static/app.js",
            "app/static/styles.css",
            "app/static/index.html",
            "app/static/sw.js",
            "tests/test_branch_archive_and_memory_v113.py",
        ]
        for rel in touched_files:
            raw = (ROOT_DIR / rel).read_bytes()
            self.assertFalse(
                raw.startswith(b"\xef\xbb\xbf"),
                f"{rel} must be UTF-8 WITHOUT BOM!",
            )
            raw.decode("utf-8")

        # 1) Python bytecode compilation check
        for py_rel in ("app/database.py", "app/main.py", "tests/test_branch_archive_and_memory_v113.py"):
            py_compile.compile(str(ROOT_DIR / py_rel), doraise=True)

        # 2) `node --check` on app.js and sw.js
        env = {**os.environ, "CUDA_VISIBLE_DEVICES": ""}
        for js_rel in ("app/static/app.js", "app/static/sw.js"):
            proc = subprocess.run(
                ["node", "--check", str(ROOT_DIR / js_rel)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=25,
                env=env,
                **_subprocess_hidden_kwargs(),
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"node --check failed on {js_rel}:\n{proc.stderr}\n{proc.stdout}",
            )

        # 3) Static cache version sync across `index.html` and `sw.js` (>= 113)
        sw_text = (ROOT_DIR / "app" / "static" / "sw.js").read_text(encoding="utf-8")
        html_text = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")

        m_sw_cache = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_text)
        m_sw_css = re.search(r"/static/styles\.css\?v=(\d+)", sw_text)
        m_sw_js = re.search(r"/static/app\.js\?v=(\d+)", sw_text)
        m_html_css = re.search(r"/static/styles\.css\?v=(\d+)", html_text)
        m_html_js = re.search(r"/static/app\.js\?v=(\d+)", html_text)
        for m in (m_sw_cache, m_sw_css, m_sw_js, m_html_css, m_html_js):
            self.assertIsNotNone(m)
            assert m is not None
            self.assertGreaterEqual(int(m.group(1)), 113)
        assert m_sw_cache and m_sw_css and m_sw_js and m_html_css and m_html_js
        versions = {
            int(m_sw_cache.group(1)),
            int(m_sw_css.group(1)),
            int(m_sw_js.group(1)),
            int(m_html_css.group(1)),
            int(m_html_js.group(1)),
        }
        self.assertEqual(len(versions), 1, f"Cache version mismatch across index.html and sw.js: {versions}")

        # Required v113 DOM anchors in index.html
        for dom_id in (
            'id="regularChatsArchiveWrap"',
            'id="activeChatArchivedPill"',
            'id="unarchiveActiveChatBtn"',
            'id="deleteOrArchiveChatModal"',
            'id="confirmArchiveInsteadOfDeleteBtn"',
            'id="confirmPermanentDeleteChatBtn"',
            'id="selToolbarSepPin"',
            'id="selToolbarPinMemBtn"',
        ):
            self.assertIn(dom_id, html_text)

        # 4) `main.workspace` CSS invariant: never create a containing block for position:fixed modals
        css_text = (ROOT_DIR / "app" / "static" / "styles.css").read_text(encoding="utf-8")
        forbidden_props = (
            "transform",
            "filter",
            "backdrop-filter",
            "perspective",
            "contain",
            "container-type",
        )
        for m in re.finditer(r"(?<![-\w])(?:main)?\.workspace\s*\{([^}]*)\}", css_text):
            block = m.group(1)
            for prop in forbidden_props:
                self.assertIsNone(
                    re.search(rf"(?<![-\w]){re.escape(prop)}\s*:", block),
                    f"Forbidden property `{prop}` found inside `.workspace` rule!",
                )

        # 5) Node VM unit tests for v113 frontend logic
        node_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const appJsPath = process.argv[2];
const code = fs.readFileSync(appJsPath, "utf8");

const noop = () => {};
const makeNode = (tag = "div") => {
  const node = {
    tagName: String(tag).toUpperCase(),
    id: "",
    className: "",
    textContent: "",
    innerHTML: "",
    value: "",
    disabled: false,
    hidden: false,
    style: { setProperty: noop, removeProperty: noop },
    dataset: {},
    attributes: {},
    children: [],
    parentNode: null,
    classList: {
      add(...cls) {
        const set = new Set(String(node.className || "").split(/\s+/).filter(Boolean));
        cls.forEach((c) => set.add(c));
        node.className = Array.from(set).join(" ");
      },
      remove(...cls) {
        const set = new Set(String(node.className || "").split(/\s+/).filter(Boolean));
        cls.forEach((c) => set.delete(c));
        node.className = Array.from(set).join(" ");
      },
      toggle(c, force) {
        const has = this.contains(c);
        const next = force === undefined ? !has : Boolean(force);
        if (next) this.add(c);
        else this.remove(c);
        return next;
      },
      contains(c) {
        return String(node.className || "").split(/\s+/).filter(Boolean).includes(c);
      },
    },
    addEventListener: noop,
    removeEventListener: noop,
    setAttribute(k, v) {
      node.attributes[k] = String(v);
    },
    getAttribute(k) {
      return Object.prototype.hasOwnProperty.call(node.attributes, k) ? node.attributes[k] : "";
    },
    appendChild(ch) {
      node.children.push(ch);
      return ch;
    },
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  return node;
};

const storageMap = new Map();
const fakeStorage = {
  getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
  setItem: (k, v) => storageMap.set(k, String(v)),
  removeItem: (k) => storageMap.delete(k),
};

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  cancelAnimationFrame: clearTimeout,
  Node: { ELEMENT_NODE: 1 },
  Intl,
  URL,
  URLSearchParams,
  encodeURIComponent,
  decodeURIComponent,
  Math,
  Date,
  JSON,
  Map,
  Set,
  Promise,
  RegExp,
  Array,
  Object,
  String,
  Number,
  Boolean,
  Error,
  Infinity,
  NaN,
  parseInt,
  parseFloat,
  isNaN,
  isFinite,
  performance: { now: () => Date.now() },
  CSS: { escape: (s) => String(s) },
  localStorage: fakeStorage,
  sessionStorage: fakeStorage,
  navigator: { userAgent: "NodeTest", clipboard: { writeText: async () => {} } },
  location: {
    protocol: "http:",
    host: "127.0.0.1:8765",
    hostname: "127.0.0.1",
    port: "8765",
    pathname: "/",
    search: "",
    hash: "",
    origin: "http://127.0.0.1:8765",
    href: "http://127.0.0.1:8765/",
  },
  getSelection: () => null,
  matchMedia: () => ({ matches: false, addEventListener: noop, removeEventListener: noop }),
  document: {
    getElementById: () => makeNode(),
    querySelector: () => makeNode(),
    querySelectorAll: () => [],
    createElement: (tag) => makeNode(tag),
    createDocumentFragment: () => ({ appendChild: noop }),
    addEventListener: noop,
    documentElement: { setAttribute: noop, getAttribute: () => "", style: { setProperty: noop } },
    body: makeNode("body"),
    hidden: false,
  },
  __AGY_EXPORT_ADVISOR_FOR_TESTS__: true,
  __AGY_SKIP_AUTO_INIT__: true,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: "app.js" });

const api = sandbox.__AGY_ADVISOR_API__;
assert.ok(api, "__AGY_ADVISOR_API__ must be exported");

// A. extractPinExcerptFromMessage: explicit selectedText priority & 400-char cap + fenced code block stripping
const sampleMsg = {
  id: 42,
  role: "assistant",
  content: "## Итог архитектуры\n```python\nprint('huge code block')\n```\nГлавный инвариант: кэш сбрасывается атомарно.",
};
const selShort = api.extractPinExcerptFromMessage(sampleMsg, "  Выделенный   инвариант  пользователя ");
assert.strictEqual(selShort, "Выделенный инвариант пользователя");

const selLong = api.extractPinExcerptFromMessage(sampleMsg, "А".repeat(500));
assert.ok(selLong.length <= 400, `Expected <=400 chars for long selection, got ${selLong.length}`);
assert.ok(selLong.endsWith("…"));

const autoExcerpt = api.extractPinExcerptFromMessage(sampleMsg, "");
assert.ok(!autoExcerpt.includes("huge code block"), "Auto-excerpt must strip fenced code blocks");
assert.ok(autoExcerpt.includes("Главный инвариант: кэш сбрасывается атомарно."));
assert.ok(autoExcerpt.length <= 400);

// B. Active vs archived HQ lookup + sibling handoff filtering
const archivedHq = {
  id: "hq_arch",
  title: "Штаб (старый)",
  kind: "project",
  project_name: "Alpha",
  access_mode: "read_only",
  is_archived: true,
  updated_at: "2026-11-15T12:00:00Z",
};
const activeHq = {
  id: "hq_live",
  title: "Штаб Alpha",
  kind: "project",
  project_name: "Alpha",
  access_mode: "read_only",
  is_archived: false,
  updated_at: "2026-11-14T12:00:00Z",
};
const workerTab = {
  id: "worker_1",
  title: "Фикс бага #7",
  kind: "project",
  project_name: "Alpha",
  access_mode: "write_files",
  is_archived: false,
  updated_at: "2026-11-15T12:30:00Z",
};
const archivedWorker = {
  id: "worker_old",
  title: "Старый воркер",
  kind: "project",
  project_name: "Alpha",
  access_mode: "full_access",
  is_archived: true,
  updated_at: "2026-11-15T12:20:00Z",
};

api.state.chats = [archivedHq, activeHq, workerTab, archivedWorker];
api.state.viewingDesktopConv = null;
api.state.activeChat = activeHq;
api.state.activeChatId = activeHq.id;

const siblings = api.getSortedHandoffSiblingChats("Alpha");
assert.strictEqual(
  siblings.map((c) => c.id).join(","),
  "worker_1",
  "getSortedHandoffSiblingChats must exclude archived branches"
);

api.state.activeChat = workerTab;
api.state.activeChatId = workerTab.id;

const foundHq = api.findTargetHqChatForReturn(workerTab);
assert.ok(foundHq && foundHq.id === "hq_live", "findTargetHqChatForReturn must skip archived HQ tabs");

// C. Assistant footer HTML: split combo `↩ В Штаб` + `↩+🗄` and readable `📌 В память` -> `✓ В памяти`
const footerHtml1 = api.buildAssistantFooterInnerHtml(sampleMsg, sampleMsg.content, false);
assert.ok(footerHtml1.includes("msg-hq-return-group"), "Active worker must render split .msg-hq-return-group");
assert.ok(footerHtml1.includes('data-return-archive-source="1"'), "Must include combo ↩+🗄 button");
assert.ok(footerHtml1.includes("msg-pin-memory-btn") && !footerHtml1.includes("msg-pin-memory-btn icon-only"));
assert.ok(footerHtml1.includes(">В память<"));

sampleMsg._pinnedToProjMemory = true;
const footerHtmlPinned = api.buildAssistantFooterInnerHtml(sampleMsg, sampleMsg.content, false);
assert.ok(footerHtmlPinned.includes("is-pinned-mem"), "Pinned message must get .is-pinned-mem");
assert.ok(footerHtmlPinned.includes("✓ В памяти"));

// Archived worker tab should NOT offer `↩+🗄` archive-again combo
api.state.activeChat = archivedWorker;
api.state.activeChatId = archivedWorker.id;
const footerHtmlArchivedWorker = api.buildAssistantFooterInnerHtml(sampleMsg, sampleMsg.content, false);
assert.ok(!footerHtmlArchivedWorker.includes('data-return-archive-source="1"'));

// D. hasAnyHqReturnSentFromChat detection for smart delete warning
assert.strictEqual(api.hasAnyHqReturnSentFromChat("worker_1"), false);
assert.strictEqual(api.hasAnyHqReturnSentFromChat(workerTab), false);
api.state.msgHqReturnsMap.set("worker_1:42", [{ hq_chat_id: "hq_live", hq_title: "Штаб Alpha" }]);
assert.strictEqual(api.hasAnyHqReturnSentFromChat("worker_1"), true);
assert.strictEqual(api.hasAnyHqReturnSentFromChat(workerTab), true);

// E. Visual / incremental DOM signatures & COPY_STRIP_SELECTOR
const sigActive = api.computeSidebarChatVisualSig({ ...workerTab, is_archived: false });
const sigArch = api.computeSidebarChatVisualSig({ ...workerTab, is_archived: true });
assert.notStrictEqual(sigActive, sigArch, "computeSidebarChatVisualSig must reflect is_archived");

const mSig0 = api.computeMessageItemSig({ ...sampleMsg, _pinnedToProjMemory: false }, 0, 1);
const mSig1 = api.computeMessageItemSig({ ...sampleMsg, _pinnedToProjMemory: true }, 0, 1);
assert.notStrictEqual(mSig0, mSig1, "computeMessageItemSig must reflect _pinnedToProjMemory");

for (const sel of [".msg-hq-return-group", ".msg-hq-return-btn", ".msg-pin-memory-btn"]) {
  assert.ok(api.COPY_STRIP_SELECTOR.includes(sel), `COPY_STRIP_SELECTOR must include ${sel}`);
}

console.log("NODE_VM_V113_OK");
"""
        harness_file = self.tmp_root / "v113_node_harness.js"
        harness_file.write_text(node_harness, encoding="utf-8")
        try:
            vm_proc = subprocess.run(
                ["node", str(harness_file), str(ROOT_DIR / "app" / "static" / "app.js")],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=25,
                env=env,
                **_subprocess_hidden_kwargs(),
            )
            self.assertEqual(
                vm_proc.returncode,
                0,
                f"Node VM v113 test failed:\n{vm_proc.stderr}\n{vm_proc.stdout}",
            )
            self.assertIn("NODE_VM_V113_OK", vm_proc.stdout)
        finally:
            harness_file.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
