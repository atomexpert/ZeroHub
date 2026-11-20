"""v110/v111: SQLite pool, in-memory caches (list_chats / usage ledger / users / PBKDF2),
batch migrated-paths map, xray pipe drain and static fast-path invariants.

Strictly isolated: temporary data dir, redirected DB_PATH, pool closed before cleanup,
live `data/hub.db` must stay byte-for-byte untouched.
"""
import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE_HUB_DB = ROOT / "data" / "hub.db"
STATIC_DIR = ROOT / "app" / "static"


def _stat_snapshot(p: Path):
    if not p.exists():
        return (False, 0, 0)
    st = p.stat()
    return (True, st.st_mtime_ns, st.st_size)


class PerformanceOptimizationsV110Test(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._live_db_before = _stat_snapshot(LIVE_HUB_DB)

    async def asyncSetUp(self) -> None:
        from app import config, database

        self.config = config
        self.database = database
        self._tmp = tempfile.TemporaryDirectory(prefix="agy_hub_perf_v110_")
        self.tmp_path = Path(self._tmp.name)
        self._orig = {
            "DATA_DIR": config.DATA_DIR,
            "DB_PATH": config.DB_PATH,
            "USERS_DIR": config.USERS_DIR,
            "CLI_HOMES_DIR": config.CLI_HOMES_DIR,
            "DB_MOD_PATH": database.DB_PATH,
            "DB_MOD_DATA_DIR": getattr(database, "DATA_DIR", None),
        }
        config.DATA_DIR = self.tmp_path / "data"
        config.DB_PATH = config.DATA_DIR / "hub_isolated_v110.db"
        config.USERS_DIR = self.tmp_path / "users"
        config.CLI_HOMES_DIR = config.DATA_DIR / "cli_homes"
        database.DB_PATH = config.DB_PATH
        if self._orig["DB_MOD_DATA_DIR"] is not None:
            database.DATA_DIR = config.DATA_DIR
        for d in (config.DATA_DIR, config.USERS_DIR, config.CLI_HOMES_DIR):
            d.mkdir(parents=True, exist_ok=True)
        await database.close_db_pool()
        await database.init_db()

    async def asyncTearDown(self) -> None:
        try:
            await self.database.close_db_pool()
        finally:
            self.config.DATA_DIR = self._orig["DATA_DIR"]
            self.config.DB_PATH = self._orig["DB_PATH"]
            self.config.USERS_DIR = self._orig["USERS_DIR"]
            self.config.CLI_HOMES_DIR = self._orig["CLI_HOMES_DIR"]
            self.database.DB_PATH = self._orig["DB_MOD_PATH"]
            if self._orig["DB_MOD_DATA_DIR"] is not None:
                self.database.DATA_DIR = self._orig["DB_MOD_DATA_DIR"]
            self._tmp.cleanup()
        # The live Hub server (ports 8765/8766) legitimately writes hub.db while tests run;
        # same opt-out as tests/conftest.py. Isolation itself is asserted via DB_PATH checks.
        if os.environ.get("HUB_ALLOW_EXTERNAL_LIVE_DB_ACTIVITY") == "1":
            return
        self.assertEqual(
            _stat_snapshot(LIVE_HUB_DB),
            self._live_db_before,
            "Live data/hub.db must remain byte-for-byte untouched!",
        )

    # ------------------------------------------------------------------ pool
    async def test_01_pool_reuse_rollback_and_clean_close(self) -> None:
        db = self.database
        self.assertTrue(
            str(Path(db.DB_PATH).resolve()).startswith(str(self.tmp_path.resolve())),
            "DB_PATH must point into the temporary directory",
        )
        async with db.connect_db() as conn:
            await conn.execute("CREATE TABLE IF NOT EXISTS perf_t (id INTEGER PRIMARY KEY, v TEXT)")
            await conn.commit()
            cur = await conn.execute("PRAGMA journal_mode")
            self.assertEqual(str((await cur.fetchone())[0]).lower(), "wal")
            cur = await conn.execute("PRAGMA synchronous")
            self.assertEqual(int((await cur.fetchone())[0]), 1)  # NORMAL
            cur = await conn.execute("PRAGMA foreign_keys")
            self.assertEqual(int((await cur.fetchone())[0]), 1)
            cur = await conn.execute("PRAGMA temp_store")
            self.assertEqual(int((await cur.fetchone())[0]), 2)  # MEMORY

        async def worker(i: int) -> int:
            async with db.connect_db() as conn:
                await conn.execute("INSERT INTO perf_t (v) VALUES (?)", (f"w{i}",))
                await conn.commit()
                await asyncio.sleep(0.01)
                cur = await conn.execute("SELECT COUNT(*) FROM perf_t")
                return int((await cur.fetchone())[0])

        results = await asyncio.gather(*(worker(i) for i in range(10)))
        self.assertEqual(len(results), 10)
        self.assertLessEqual(len(db._all_conns), db._DB_POOL_MAX_SIZE)
        self.assertGreaterEqual(len(db._all_conns), 1)
        pooled_before = set(db._all_conns)

        # row_factory is reset between leases
        async with db.connect_db() as conn:
            self.assertIn(conn, pooled_before)
            self.assertIsNone(conn.row_factory)
            import aiosqlite

            conn.row_factory = aiosqlite.Row
        async with db.connect_db() as conn:
            self.assertIsNone(conn.row_factory)

        # rollback on exception inside connect_db()
        with self.assertRaises(RuntimeError):
            async with db.connect_db() as conn:
                await conn.execute("INSERT INTO perf_t (v) VALUES ('must_rollback')")
                raise RuntimeError("boom")
        async with db.connect_db() as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM perf_t WHERE v = 'must_rollback'")
            self.assertEqual(int((await cur.fetchone())[0]), 0)
            cur = await conn.execute("SELECT COUNT(*) FROM perf_t")
            self.assertEqual(int((await cur.fetchone())[0]), 10)

        # a half-read cursor must not leak a stale WAL snapshot into the next lease
        async with db.connect_db() as conn:
            cur = await conn.execute("SELECT id FROM perf_t ORDER BY id")
            await cur.fetchone()  # leave the statement un-exhausted on purpose
        raw = sqlite3.connect(str(db.DB_PATH), timeout=5.0)
        try:
            raw.execute("INSERT INTO perf_t (v) VALUES ('external')")
            raw.commit()
        finally:
            raw.close()
        for _ in range(db._DB_POOL_MAX_SIZE):
            async with db.connect_db() as conn:
                cur = await conn.execute("SELECT COUNT(*) FROM perf_t")
                self.assertEqual(int((await cur.fetchone())[0]), 11)

        # nested leases never deadlock (overflow connections)
        async def nested(depth: int) -> int:
            async with db.connect_db() as conn:
                if depth > 0:
                    return await nested(depth - 1)
                cur = await conn.execute("SELECT 1")
                return int((await cur.fetchone())[0])

        self.assertEqual(
            await asyncio.wait_for(asyncio.gather(*(nested(6) for _ in range(5))), timeout=20),
            [1] * 5,
        )
        self.assertEqual(len(db._overflow_conns), 0)
        self.assertLessEqual(len(db._all_conns), db._DB_POOL_MAX_SIZE)

        await db.close_db_pool()
        self.assertEqual(len(db._all_conns), 0)
        self.assertEqual(len(db._idle_conns), 0)
        # Windows: the file must be deletable right after the pool is closed
        probe = Path(str(db.DB_PATH) + ".copy")
        probe.write_bytes(Path(db.DB_PATH).read_bytes())
        probe.unlink()
        # pool transparently re-opens after close
        async with db.connect_db() as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM perf_t")
            self.assertEqual(int((await cur.fetchone())[0]), 11)

    async def test_02_new_indexes_exist(self) -> None:
        async with self.database.connect_db() as conn:
            cur = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
            names = {str(r[0]) for r in await cur.fetchall()}
        for idx in (
            "idx_chats_owner_kind_updated",
            "idx_messages_assistant_usage",
            "idx_messages_bookmarked",
        ):
            self.assertIn(idx, names)

    # --------------------------------------------------------- list_chats
    async def test_03_list_chats_cache_and_invalidation(self) -> None:
        db = self.database
        chat = await db.create_chat(title="Perf chat", model="gemini-4-carbon-high")
        cid = chat["id"]
        await db.add_message(cid, role="user", content="привет, мир")
        a_msg = await db.add_message(
            cid,
            role="assistant",
            content="ответ ассистента",
            model="gemini-4-carbon-high",
            usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        )

        first = {c["id"]: c for c in await db.list_chats()}[cid]
        self.assertEqual(first["message_count"], 2)
        self.assertEqual(first["bookmark_count"], 0)
        self.assertEqual(first["last_assistant_status"], "done")
        self.assertTrue(first["preview"].startswith("ответ ассистента"))
        self.assertIn("context_pct", first)
        path_key = (db._resolved_db_path(), cid)
        self.assertIn(path_key, db._CHAT_LIST_CACHE)

        # second call: served from cache — the subquery SQL must not run for this chat
        orig_sql = db._CHAT_LIST_METRICS_SQL
        db._CHAT_LIST_METRICS_SQL = "SELECT broken FROM no_such_table WHERE 1 IN ({ph})"
        try:
            second = {c["id"]: c for c in await db.list_chats()}[cid]
        finally:
            db._CHAT_LIST_METRICS_SQL = orig_sql
        for k in ("preview", "message_count", "bookmark_count", "last_assistant_status", "context_pct", "context_used_tokens"):
            self.assertEqual(second[k], first[k], k)

        # include_context_pct=False keeps the old shape (no context keys)
        lite = {c["id"]: c for c in await db.list_chats(include_context_pct=False)}[cid]
        self.assertNotIn("context_pct", lite)
        self.assertEqual(lite["message_count"], 2)

        # add_message invalidates
        await db.add_message(cid, role="user", content="второй вопрос")
        third = {c["id"]: c for c in await db.list_chats()}[cid]
        self.assertEqual(third["message_count"], 3)
        self.assertTrue(third["preview"].startswith("второй вопрос"))

        # set_message_bookmarked invalidates
        await db.set_message_bookmarked(cid, a_msg["id"], True)
        fourth = {c["id"]: c for c in await db.list_chats()}[cid]
        self.assertEqual(fourth["bookmark_count"], 1)

        # update_message invalidates (status + preview of the newest message)
        streaming = await db.add_message(cid, role="assistant", content="", status="streaming")
        fifth = {c["id"]: c for c in await db.list_chats()}[cid]
        self.assertEqual(fifth["last_assistant_status"], "streaming")
        await db.update_message(streaming["id"], content="финальный ответ", status="done")
        sixth = {c["id"]: c for c in await db.list_chats()}[cid]
        self.assertEqual(sixth["last_assistant_status"], "done")
        self.assertTrue(sixth["preview"].startswith("финальный ответ"))

        # search path keeps old behaviour
        found = await db.list_chats(query="второй вопрос")
        self.assertEqual([c["id"] for c in found], [cid])
        self.assertEqual(found[0]["message_count"], 4)

        # delete_chat invalidates
        self.assertTrue(await db.delete_chat(cid))
        self.assertNotIn(path_key, db._CHAT_LIST_CACHE)
        self.assertNotIn(cid, {c["id"] for c in await db.list_chats()})

    # ------------------------------------------------ usage ledger + users
    async def test_04_usage_ledger_user_cache_and_pbkdf2(self) -> None:
        db = self.database
        user = await db.create_user(username="perfuser", password="Secret-pass-123")
        uid = user["id"]
        chat = await db.create_chat(owner_id=uid, title="Usage chat")
        cid = chat["id"]
        await db.add_message(
            cid, user_id=uid, role="assistant", model="gemini-4-carbon-high",
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )
        await db.add_message(
            cid, user_id=uid, role="assistant", model="codex:gpt-5",
            usage={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30, "provider": "chatgpt_codex"},
        )
        await db.add_message(
            cid, user_id=uid, role="assistant", model="claude-code:sonnet",
            usage={"input_tokens": 40, "output_tokens": 20, "total_tokens": 60, "provider": "claude_code"},
        )
        await db.add_message(cid, user_id=uid, role="assistant", model="codex:empty", usage={})

        stats = await db.get_token_usage_stats(cid, filter_user_id=uid, include_by_user=True)
        self.assertEqual(stats["all_time"]["total_tokens"], 105)
        self.assertEqual(stats["all_time"]["turns"], 3)
        self.assertEqual(stats["window_5h"]["total_tokens"], 105)
        self.assertEqual(stats["active_chat"]["total_tokens"], 105)
        self.assertEqual({m["model"] for m in stats["by_model"]}, {"gemini-4-carbon-high", "codex:gpt-5", "claude-code:sonnet"})
        by_user = {u["user_id"]: u for u in stats["by_user"]}
        self.assertEqual(by_user[uid]["tokens_24h"], 105)
        self.assertEqual(by_user[uid]["shared_tokens_24h"], 15)
        self.assertIn("admin", by_user)
        self.assertIsNone((await db.get_token_usage_stats())["active_chat"])

        cg = await db.get_chatgpt_token_usage_stats(uid)
        self.assertEqual(cg["all_time"]["total_tokens"], 30)
        self.assertEqual([m["model"] for m in cg["by_model"]], ["codex:gpt-5"])
        cl = await db.get_claude_token_usage_stats(uid)
        self.assertEqual(cl["window_24h"]["total_tokens"], 60)
        self.assertEqual([m["model"] for m in cl["by_model"]], ["claude-code:sonnet"])

        # ledger is cached: a second call does not touch SQLite
        ver = db._USAGE_LEDGER_VERSION
        self.assertEqual(db._USAGE_LEDGER_CACHE["version"], ver)
        rows_obj = db._USAGE_LEDGER_CACHE["rows"]
        await db.get_chatgpt_token_usage_stats(uid)
        self.assertIs(db._USAGE_LEDGER_CACHE["rows"], rows_obj)

        # new assistant usage invalidates the ledger
        streaming = await db.add_message(cid, user_id=uid, role="assistant", model="codex:gpt-5", status="streaming")
        await db.update_message(streaming["id"], usage={"total_tokens": 70, "provider": "chatgpt_codex"}, status="done")
        self.assertEqual((await db.get_chatgpt_token_usage_stats(uid))["all_time"]["total_tokens"], 100)
        self.assertEqual((await db.get_token_usage_stats(filter_user_id=uid))["all_time"]["total_tokens"], 175)

        # delete_chat drops its usage from the ledger
        await db.delete_chat(cid)
        self.assertEqual((await db.get_token_usage_stats(filter_user_id=uid))["all_time"]["total_tokens"], 0)
        self.assertEqual((await db.get_claude_token_usage_stats(uid))["all_time"]["total_tokens"], 0)

        # user cache: hit returns an independent copy; update_user invalidates immediately
        u1 = await db.get_user_by_id(uid)
        u1["allowed_models"].append("mutated")
        u1["display_name"] = "mutated"
        u2 = await db.get_user_by_id(uid)
        self.assertNotIn("mutated", u2["allowed_models"])
        self.assertNotEqual(u2["display_name"], "mutated")
        sv_before = int(u2["session_version"])
        await db.update_user(uid, display_name="Perf Renamed", password="Another-pass-456")
        u3 = await db.get_user_by_id(uid)
        self.assertEqual(u3["display_name"], "Perf Renamed")
        self.assertEqual(int(u3["session_version"]), sv_before + 1)
        await db.update_user(uid, is_active=False)
        self.assertFalse((await db.get_user_by_id(uid))["is_active"])
        self.assertTrue(await db.delete_user(uid))
        self.assertIsNone(await db.get_user_by_id(uid))

        # PBKDF2 derivations are LRU-cached (round-trip still works)
        db._pbkdf2_fernet_cached.cache_clear()
        tok = db.encrypt_claude_credentials_json("u-perf", '{"a": 1}')
        t0 = time.perf_counter()
        for _ in range(20):
            self.assertEqual(db.decrypt_claude_credentials_json("u-perf", tok), '{"a": 1}')
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertGreaterEqual(db._pbkdf2_fernet_cached.cache_info().hits, 20)

    # --------------------------------------- migrated paths map + xray drain
    async def test_05_migrated_paths_map_and_pipe_drain(self) -> None:
        db = self.database
        now = db.utc_now_iso()
        async with db.connect_db() as conn:
            for scope, proj, paths in (
                ("admin::ProjA", "ProjA", '["D:/old/ProjA", "E:/older/ProjA"]'),
                ("admin::ProjB", "ProjB", "[]"),
                ("u2::ProjA", "proja", '["F:/third/ProjA"]'),
            ):
                await conn.execute(
                    "INSERT INTO project_memories (scope_key, owner_id, project_name, migrated_from_paths_json, updated_at) "
                    "VALUES (?, 'admin', ?, ?, ?)",
                    (scope, proj, paths, now),
                )
            await conn.commit()
        mapping = await asyncio.to_thread(db.get_all_projects_migrated_paths_sync)
        self.assertNotIn("projb", mapping)
        self.assertEqual(
            set(mapping["proja"]),
            set(await asyncio.to_thread(db.get_project_migrated_paths_sync, "ProjA")),
        )
        self.assertEqual(len(mapping["proja"]), 3)

        from app import brain_reader, proxy_router

        norms = brain_reader._get_migrated_norm_paths("ProjA", mapping)
        self.assertEqual(norms, brain_reader._get_migrated_norm_paths("ProjA"))

        script = (
            "import sys\n"
            "chunk = 'x' * 1023 + '\\n'\n"
            "for i in range(200):\n"
            "    sys.stdout.write(chunk)\n"
            "    sys.stderr.write(chunk)\n"
            "sys.stdout.flush(); sys.stderr.flush()\n"
            "sys.stderr.write('XRAY_FAKE_DONE\\n')\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **self.config.subprocess_hidden_kwargs(),
        )
        out_tail, err_tail, threads = proxy_router._start_pipe_drainers(proc)
        try:
            rc = await asyncio.to_thread(proc.wait, 15)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("child blocked on a full pipe: drain threads did not read stdout/stderr")
        self.assertEqual(rc, 0)
        for th in threads:
            th.join(timeout=5)
            self.assertFalse(th.is_alive())
            self.assertTrue(th.daemon)
        self.assertLessEqual(len(out_tail), proxy_router._PIPE_DRAIN_TAIL_LINES)
        self.assertIn(b"XRAY_FAKE_DONE", b"".join(err_tail))

    # ------------------------------------------------------ static assets
    async def test_06_static_fast_path_and_version_sync(self) -> None:
        import re

        index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        sw_js = (STATIC_DIR / "sw.js").read_text(encoding="utf-8")
        app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        css_v = re.search(r'styles\.css\?v=(\d+)', index_html).group(1)
        js_v = re.search(r'app\.js\?v=(\d+)', index_html).group(1)
        cache_v = re.search(r'CACHE_NAME = "agy-hub-v(\d+)"', sw_js).group(1)
        self.assertEqual(css_v, js_v)
        self.assertEqual(js_v, cache_v)
        self.assertGreaterEqual(int(js_v), 111)
        self.assertIn(f'"/static/app.js?v={js_v}"', sw_js)
        self.assertIn(f'"/static/styles.css?v={js_v}"', sw_js)
        self.assertIn('url.searchParams.has("v")', sw_js)
        self.assertIn('url.pathname.startsWith("/static/vendor/")', sw_js)
        self.assertIn("HLJS_CACHE_MAX = 256", app_js)
        self.assertIn("STREAM_RENDER_MIN_INTERVAL_MS = 75", app_js)
        self.assertIn("clearTimeout(streamThrottleTimer)", app_js)
        self.assertIn("chatAlreadyKnown", app_js)
        self.assertIn("isQuickResume", app_js)
        for raw in (index_html, sw_js, app_js):
            self.assertFalse(raw.startswith("\ufeff"), "UTF-8 without BOM")

        from fastapi.testclient import TestClient
        from app import main

        # No `with` block: lifespan (tunnel supervisor, watchdogs, model fetch) is NOT started.
        client = TestClient(main.app, raise_server_exceptions=False)
        if True:
            r1 = client.get(f"/static/app.js?v={js_v}")
            self.assertEqual(r1.status_code, 200)
            self.assertIn("immutable", r1.headers.get("cache-control", ""))
            r2 = client.get("/static/vendor/marked.min.js")
            if r2.status_code == 200:
                self.assertIn("immutable", r2.headers.get("cache-control", ""))
            r3 = client.get("/sw.js")
            self.assertEqual(r3.headers.get("cache-control"), "no-cache")
            r4 = client.get("/")
            self.assertEqual(r4.headers.get("cache-control"), "no-cache")
            r5 = client.get("/static/icon.svg")
            self.assertNotIn("immutable", r5.headers.get("cache-control", ""))


if __name__ == "__main__":
    unittest.main()
