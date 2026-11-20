"""
Unit tests for Antigravity Hub v=73:
- `database.list_chats()` returns `last_assistant_status` accurately across empty chats,
  user-only chats, done/cancelled/error assistant turns, and multi-turn histories.
- Static cache version consistency (`v>=73`) across `app/static/sw.js` and `app/static/index.html`,
  plus presence of all new adaptive mobile header / live status / mobile telemetry DOM anchors.
- Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER touches live `data/hub.db` or `run_server.py`.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
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


class TestMobileHeaderStatusV73(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database

        self._live_db = config.DB_PATH
        self._live_stat_before = (
            (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            if self._live_db.exists()
            else None
        )

        self._tmp = tempfile.TemporaryDirectory(
            prefix="agy_test_v73_", ignore_cleanup_errors=True
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

        tmp_db = self.tmp_root / "test_v73.db"
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

    async def test_list_chats_returns_last_assistant_status(self) -> None:
        import app.database as database

        await database.init_db()
        user = await database.create_user(
            username="mob_user",
            password="secret",
            display_name="Mobile User",
            role="user",
            max_access_mode="write_files",
        )
        uid = user["id"]

        # 1) Empty chat -> None
        c_empty = await database.create_chat(
            kind="project", project_name="DemoProj", title="Empty", owner_id=uid
        )
        # 2) User-only chat -> None
        c_user_only = await database.create_chat(
            kind="project", project_name="DemoProj", title="UserOnly", owner_id=uid
        )
        await database.add_message(
            c_user_only["id"], user_id=uid, role="user", content="Hello"
        )

        # 3) Assistant done -> "done"
        c_done = await database.create_chat(
            kind="project", project_name="DemoProj", title="DoneChat", owner_id=uid
        )
        await database.add_message(
            c_done["id"], user_id=uid, role="user", content="Implement v73"
        )
        await database.add_message(
            c_done["id"],
            user_id=uid,
            role="assistant",
            content="Implemented!",
            status="done",
        )

        # 4) Assistant cancelled -> "cancelled"
        c_stopped = await database.create_chat(
            kind="project", project_name="DemoProj", title="StoppedChat", owner_id=uid
        )
        await database.add_message(
            c_stopped["id"], user_id=uid, role="user", content="Long task"
        )
        await database.add_message(
            c_stopped["id"],
            user_id=uid,
            role="assistant",
            content="Partial...",
            status="cancelled",
        )

        # 5) Assistant error -> "error"
        c_err = await database.create_chat(
            kind="project", project_name="DemoProj", title="ErrChat", owner_id=uid
        )
        await database.add_message(
            c_err["id"], user_id=uid, role="user", content="Broken task"
        )
        await database.add_message(
            c_err["id"],
            user_id=uid,
            role="assistant",
            content="",
            status="error",
        )

        # 6) Multi-turn chat where earlier turn was error, latest assistant turn is done,
        #    followed by a new queued/pending user message -> still returns latest assistant "done"
        c_multi = await database.create_chat(
            kind="chat", project_name="", title="MultiTurn", owner_id=uid
        )
        await database.add_message(
            c_multi["id"], user_id=uid, role="user", content="Turn 1"
        )
        await database.add_message(
            c_multi["id"], user_id=uid, role="assistant", content="Err", status="error"
        )
        await database.add_message(
            c_multi["id"], user_id=uid, role="user", content="Turn 2"
        )
        await database.add_message(
            c_multi["id"], user_id=uid, role="assistant", content="Fixed!", status="done"
        )
        await database.add_message(
            c_multi["id"], user_id=uid, role="user", content="Turn 3 user message"
        )

        chats = await database.list_chats(owner_id=uid)
        by_id = {c["id"]: c for c in chats}

        self.assertIsNone(by_id[c_empty["id"]]["last_assistant_status"])
        self.assertIsNone(by_id[c_user_only["id"]]["last_assistant_status"])
        self.assertEqual(by_id[c_done["id"]]["last_assistant_status"], "done")
        self.assertEqual(by_id[c_stopped["id"]]["last_assistant_status"], "cancelled")
        self.assertEqual(by_id[c_err["id"]]["last_assistant_status"], "error")
        self.assertEqual(by_id[c_multi["id"]]["last_assistant_status"], "done")

    def test_static_version_and_mobile_dom_anchors(self) -> None:
        index_html = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")
        sw_js = (ROOT_DIR / "app" / "static" / "sw.js").read_text(encoding="utf-8")

        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_js)
        self.assertIsNotNone(m_sw)
        sw_ver = int(m_sw.group(1))
        self.assertGreaterEqual(sw_ver, 73)

        self.assertIn(f"/static/styles.css?v={sw_ver}", index_html)
        self.assertIn(f"/static/app.js?v={sw_ver}", index_html)
        self.assertIn(f"/static/styles.css?v={sw_ver}", sw_js)
        self.assertIn(f"/static/app.js?v={sw_ver}", sw_js)

        for required_id in (
            'id="mobileTopKicker"',
            'id="activeChatStatePill"',
            'id="mobileRightToolsSlot"',
            'id="mobileRegularChatTag"',
            'id="mobileTelemetryBar"',
            'id="mobQuotaPillBtn"',
            'id="mobOrPillBtn"',
            'id="mobTelemetryExpandBtn"',
            'id="activitySectionToggleBtn"',
            'id="activitySectionRunDot"',
        ):
            self.assertIn(required_id, index_html)


if __name__ == "__main__":
    unittest.main()
