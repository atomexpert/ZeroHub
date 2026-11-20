"""
Isolated automated test suite for Shared Projects + Docker Sandbox Permissions & Storage UX (v93):
- Test 1: `agy` `--settings` & `--add-dir` matrix in Docker shared projects for Admin and Guest
  (`edit_file`, recursive `/**`, safe CLI `mcp`/`skills` read-only dirs, `call_mcp_tool(*)`,
  strict host-shell & secret-file `deny`, and `docker` / `docker_unavailable` prompt instructions).
- Test 2: Idempotent isolated `.git` boundary creation (`ensure_isolated_project_git_boundary` &
  `resolve_user_project`) preventing upward Git/ripgrep traversal to `Antigravity_Hub/.git`.
- Test 3: Flexible Shared Project storage (`shared_dir` vs `projects_root`), masked non-admin paths,
  path switching (`chats.project_path` sync + `stop_project_sandboxes`), and Zero-Trust `BASE_DIR` / `..` denylist.
- Test 4: Auto-upgrade `upgrade_members_docker` (`write_files`/`read_only` -> `docker_full` without
  downgrading `host_full`, plus `docker-sandbox` skill sync) and `Codex` + `Claude` Docker parity.
- Test 5: Frontend syntax (`node --check app/static/app.js`), UTF-8 no-BOM, `v=93` cache-bust,
  `main.workspace` CSS safety invariant, and live `data/hub.db` immutability.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

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

    def json(self) -> dict[str, Any]:
        return json.loads(self.text)


class _MiniAsgiClient:
    def __init__(self, app: Any, *, client: tuple[str, int] = ("127.0.0.1", 52193)) -> None:
        self._app = app
        self._client = client

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _MiniAsgiResponse:
        parsed_url = urllib.parse.urlsplit(path)
        raw_path = parsed_url.path or "/"
        qs = parsed_url.query
        payload = json.dumps(json_body).encode("utf-8") if json_body is not None else b""
        hdr_list: list[tuple[bytes, bytes]] = [(b"host", b"127.0.0.1:8765")]
        if json_body is not None:
            hdr_list.append((b"content-type", b"application/json"))
            hdr_list.append((b"content-length", str(len(payload)).encode("ascii")))
        for k, v in (headers or {}).items():
            hdr_list.append((k.lower().encode("latin-1"), str(v).encode("latin-1")))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": urllib.parse.unquote(raw_path),
            "raw_path": raw_path.encode("ascii", errors="ignore"),
            "query_string": qs.encode("ascii"),
            "headers": hdr_list,
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


class _FakeReader:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if self._lines:
            await asyncio.sleep(0)
            return self._lines.pop(0)
        return b""

    async def read(self, _n: int = -1) -> bytes:
        out = b"".join(self._lines)
        self._lines.clear()
        return out


class _FakeAgyProc:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.pid = 62093
        self.returncode: int | None = None
        encoded = [(json.dumps(e, ensure_ascii=False) + "\n").encode("utf-8") for e in events]
        self.stdout = _FakeReader(encoded)
        self.stderr = _FakeReader([])

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _FakeCodexStdin:
    def __init__(self, proc: "_FakeCodexProc") -> None:
        self._proc = proc
        self._buf = b""

    def write(self, data: bytes) -> None:
        self._buf += data
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                self._proc.on_client_json(json.loads(line))

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        pass


class _FakeCodexStdout:
    def __init__(self) -> None:
        self._q: asyncio.Queue[bytes] = asyncio.Queue()

    def push(self, obj: dict[str, Any]) -> None:
        self._q.put_nowait((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    def push_eof(self) -> None:
        self._q.put_nowait(b"")

    async def readline(self) -> bytes:
        return await self._q.get()


class _FakeCodexProc:
    def __init__(self, capture: dict[str, Any]) -> None:
        self.pid = 62094
        self.returncode: int | None = None
        self._capture = capture
        self.stdout = _FakeCodexStdout()
        self.stderr = _FakeReader([])
        self.stdin = _FakeCodexStdin(self)

    def on_client_json(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            self.stdout.push({"jsonrpc": "2.0", "id": rid, "result": {"userAgent": "codex-mock"}})
        elif method == "thread/start":
            self._capture["thread_start"] = msg.get("params") or {}
            self.stdout.push({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": "th_v93"}}})
        elif method == "turn/start":
            self._capture["turn_start"] = msg.get("params") or {}
            self.stdout.push({"jsonrpc": "2.0", "id": rid, "result": {"turn": {"id": "tu_v93"}}})
            self.stdout.push(
                {
                    "jsonrpc": "2.0",
                    "method": "item/agentMessage/delta",
                    "params": {"itemId": "msg_1", "delta": "Codex ready."},
                }
            )
            self.stdout.push(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/completed",
                    "params": {"turn": {"id": "tu_v93", "status": "completed"}},
                }
            )
            self.stdout.push_eof()

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _FakeClaudeStdin:
    def __init__(self, capture: dict[str, Any]) -> None:
        self._capture = capture
        self._chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self._chunks.append(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self._capture["stdin_prompt"] = b"".join(self._chunks).decode("utf-8", errors="replace")


class _FakeClaudeProc:
    def __init__(self, capture: dict[str, Any]) -> None:
        self.pid = 62095
        self.returncode: int | None = 0
        self.stdin = _FakeClaudeStdin(capture)
        self.stdout = _FakeReader(
            [
                (json.dumps({"type": "system", "subtype": "init", "session_id": "cl_v93"}) + "\n").encode("utf-8"),
                (
                    json.dumps(
                        {
                            "type": "result",
                            "subtype": "success",
                            "session_id": "cl_v93",
                            "result": "Claude ready.",
                            "usage": {"input_tokens": 8, "output_tokens": 4},
                        }
                    )
                    + "\n"
                ).encode("utf-8"),
            ]
        )
        self.stderr = _FakeReader([])

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return 0


class TestSharedProjectsPermissionsV93(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.claude_engine as claude_engine
        import app.codex_engine as codex_engine
        import app.config as config
        import app.database as database

        self._live_db = ROOT_DIR / "data" / "hub.db"
        self._live_stat_before = (
            (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            if self._live_db.exists()
            else None
        )

        self._tmp = tempfile.TemporaryDirectory(prefix="agy_test_v93_", ignore_cleanup_errors=True)
        self.tmp_root = Path(self._tmp.name).resolve()

        self._saved = {
            "cfg_data": config.DATA_DIR,
            "br_data": brain_reader.DATA_DIR,
            "cfg_db": config.DB_PATH,
            "db_db": database.DB_PATH,
            "cfg_homes": config.CLI_HOMES_DIR,
            "cfg_agy_data": config.AGY_CLI_DATA_DIR,
            "eng_agy_data": agy_engine.AGY_CLI_DATA_DIR,
            "cfg_ws": config.USER_WORKSPACES_DIR,
            "db_ws": database.USER_WORKSPACES_DIR,
            "br_ws": brain_reader.USER_WORKSPACES_DIR,
            "cfg_sp": config.SHARED_PROJECTS_DIR,
            "db_sp": database.SHARED_PROJECTS_DIR,
            "br_sp": brain_reader.SHARED_PROJECTS_DIR,
            "cfg_proj": config.PROJECTS_ROOT,
            "db_proj": database.PROJECTS_ROOT,
            "br_proj": brain_reader.PROJECTS_ROOT,
            "cfg_up": config.UPLOADS_DIR,
            "eng_up": agy_engine.UPLOADS_DIR,
            "cdx_up": codex_engine.UPLOADS_DIR,
            "cl_up": claude_engine.UPLOADS_DIR,
            "cfg_csbx": config.CHAT_SANDBOX_DIR,
            "eng_csbx": agy_engine.CHAT_SANDBOX_DIR,
            "cdx_csbx": codex_engine.CHAT_SANDBOX_DIR,
            "cl_csbx": claude_engine.CHAT_SANDBOX_DIR,
        }

        tmp_data = self.tmp_root / "data"
        tmp_db = tmp_data / "hub_v93.db"
        tmp_homes = tmp_data / "cli_homes"
        tmp_agy_data = tmp_data / "agy_cli_data"
        tmp_ws = tmp_data / "workspaces"
        tmp_sp = tmp_data / "shared_projects"
        tmp_proj = self.tmp_root / "projects_root"
        tmp_up = tmp_data / "uploads"
        tmp_csbx = tmp_data / "chat_sandbox"
        for d in (tmp_data, tmp_homes, tmp_agy_data, tmp_ws, tmp_sp, tmp_proj, tmp_up, tmp_csbx):
            d.mkdir(parents=True, exist_ok=True)

        # Populate safe read-only builtin skill inside AGY_CLI_DATA_DIR so junctions also resolve cleanly
        (tmp_agy_data / "builtin" / "skills" / "antigravity_guide").mkdir(parents=True, exist_ok=True)
        (tmp_agy_data / "builtin" / "skills" / "antigravity_guide" / "SKILL.md").write_text(
            "# Guide\n", encoding="utf-8"
        )
        # Simulated secret inside AGY_CLI_DATA_DIR (must NEVER be exposed via root --add-dir or allow)
        (tmp_agy_data / "oauth-token").write_text("SECRET_OAUTH", encoding="utf-8")

        config.DATA_DIR = brain_reader.DATA_DIR = tmp_data
        config.DB_PATH = database.DB_PATH = tmp_db
        config.CLI_HOMES_DIR = tmp_homes
        config.AGY_CLI_DATA_DIR = agy_engine.AGY_CLI_DATA_DIR = tmp_agy_data
        config.USER_WORKSPACES_DIR = database.USER_WORKSPACES_DIR = brain_reader.USER_WORKSPACES_DIR = tmp_ws
        config.SHARED_PROJECTS_DIR = database.SHARED_PROJECTS_DIR = brain_reader.SHARED_PROJECTS_DIR = tmp_sp
        config.PROJECTS_ROOT = database.PROJECTS_ROOT = brain_reader.PROJECTS_ROOT = tmp_proj
        config.UPLOADS_DIR = agy_engine.UPLOADS_DIR = codex_engine.UPLOADS_DIR = claude_engine.UPLOADS_DIR = tmp_up
        config.CHAT_SANDBOX_DIR = (
            agy_engine.CHAT_SANDBOX_DIR
        ) = codex_engine.CHAT_SANDBOX_DIR = claude_engine.CHAT_SANDBOX_DIR = tmp_csbx

        await database.init_db()

    async def asyncTearDown(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.claude_engine as claude_engine
        import app.codex_engine as codex_engine
        import app.config as config
        import app.database as database
        import app.main as main_mod

        agy_engine.ACTIVE_RUNS.clear()
        main_mod.CHAT_TURN_QUEUES.clear()
        main_mod.CHAT_QUEUE_PAUSED.clear()
        main_mod.COMPACTING_CHATS.clear()
        main_mod.ACTIVE_TURN_TASKS.clear()

        s = self._saved
        config.DATA_DIR = s["cfg_data"]
        brain_reader.DATA_DIR = s["br_data"]
        config.DB_PATH = s["cfg_db"]
        database.DB_PATH = s["db_db"]
        config.CLI_HOMES_DIR = s["cfg_homes"]
        config.AGY_CLI_DATA_DIR = s["cfg_agy_data"]
        agy_engine.AGY_CLI_DATA_DIR = s["eng_agy_data"]
        config.USER_WORKSPACES_DIR = s["cfg_ws"]
        database.USER_WORKSPACES_DIR = s["db_ws"]
        brain_reader.USER_WORKSPACES_DIR = s["br_ws"]
        config.SHARED_PROJECTS_DIR = s["cfg_sp"]
        database.SHARED_PROJECTS_DIR = s["db_sp"]
        brain_reader.SHARED_PROJECTS_DIR = s["br_sp"]
        config.PROJECTS_ROOT = s["cfg_proj"]
        database.PROJECTS_ROOT = s["db_proj"]
        brain_reader.PROJECTS_ROOT = s["br_proj"]
        config.UPLOADS_DIR = s["cfg_up"]
        agy_engine.UPLOADS_DIR = s["eng_up"]
        codex_engine.UPLOADS_DIR = s["cdx_up"]
        claude_engine.UPLOADS_DIR = s["cl_up"]
        config.CHAT_SANDBOX_DIR = s["cfg_csbx"]
        agy_engine.CHAT_SANDBOX_DIR = s["eng_csbx"]
        codex_engine.CHAT_SANDBOX_DIR = s["cdx_csbx"]
        claude_engine.CHAT_SANDBOX_DIR = s["cl_csbx"]

        _unlink_test_junctions(self.tmp_root)
        self._tmp.cleanup()

        if self._live_stat_before is not None:
            live_after = (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            self.assertEqual(self._live_stat_before, live_after, "Live data/hub.db must NEVER be modified!")

    def _headers(self, user_id: str, session_version: int = 1) -> dict[str, str]:
        import app.auth as auth

        tok = auth.create_user_session_token(user_id=user_id, session_version=session_version)
        return {"Authorization": f"Bearer {tok}"}

    async def test_1_agy_docker_permissions_and_add_dir_matrix(self) -> None:
        import app.agy_engine as agy_engine
        import app.config as config
        import app.database as database
        import app.sandbox_service as sandbox_service

        admin = await database.get_user_by_id("admin")
        assert admin is not None

        guest = await database.create_user(
            username="tulpar_guest",
            display_name="Tulpar Guest",
            password="guest_password_123",
            max_access_mode="docker_full",
        )
        sp = await database.create_shared_project(
            name="Tulpar_VPN",
            execution_mode="docker",
            storage_location="shared_dir",
            member_ids=[guest["id"]],
        )
        proj_dir = Path(sp["path"]).resolve()
        (proj_dir / "main.py").write_text("print('tulpar')\n", encoding="utf-8")

        await sandbox_service.sync_all_users_sandbox_cli_files()

        # Verify unit-level _build_permission_args when OpenRouter is OFF while hub_sandbox is ON:
        # call_mcp_tool(*) + call_mcp_tool(*hub_sandbox*) must be allowed, while call_mcp_tool(*hub_openrouter*) stays denied!
        guest_home = config.ensure_user_cli_home(guest)
        (guest_home / ".gemini" / "antigravity-cli" / "mcp" / "hub_sandbox").mkdir(parents=True, exist_ok=True)
        (guest_home / ".gemini" / "antigravity-cli" / "mcp" / "hub_sandbox" / "sandbox_exec.json").write_text(
            "{}", encoding="utf-8"
        )
        upload_dir = (config.UPLOADS_DIR / "c_unit_v93").resolve()
        upload_dir.mkdir(parents=True, exist_ok=True)

        perm_args = agy_engine._build_permission_args(
            "project",
            "full_access",
            is_admin=False,
            cli_home_dir=guest_home,
            project_dir=proj_dir,
            allowed_upload_dir=upload_dir,
            allow_hub_openrouter=False,
            allow_hub_sandbox=True,
            force_no_host_shell=True,
            sandbox_network="internet",
        )
        self.assertNotIn("--dangerously-skip-permissions", perm_args)
        unit_settings = json.loads(perm_args[perm_args.index("--settings") + 1])
        self.assertFalse(unit_settings["allowNonWorkspaceAccess"])
        allow_set = set(unit_settings["permissions"]["allow"])
        deny_set = set(unit_settings["permissions"]["deny"])

        proj_posix = proj_dir.as_posix().rstrip("/")
        self.assertIn(f"edit_file({proj_posix}/*)", allow_set)
        self.assertIn(f"edit_file({proj_posix}/**)", allow_set)
        self.assertIn(f"write_file({proj_posix}/*)", allow_set)
        self.assertIn(f"write_file({proj_posix}/**)", allow_set)
        self.assertIn(f"read_file({proj_posix}/*)", allow_set)
        self.assertIn(f"read_file({proj_posix}/**)", allow_set)
        self.assertIn(f"list_dir({proj_posix}/**)", allow_set)
        self.assertIn(f"find_by_name({proj_posix}/**)", allow_set)
        self.assertIn(f"grep_search({proj_posix}/**)", allow_set)
        self.assertIn("find_by_name", allow_set)
        self.assertIn("grep_search", allow_set)
        self.assertIn("search_web(*)", allow_set)
        self.assertIn("call_mcp_tool", allow_set)
        self.assertIn("call_mcp_tool(*)", allow_set)
        self.assertIn("call_mcp_tool(*hub_sandbox*)", allow_set)
        self.assertIn("sandbox_exec", allow_set)
        self.assertNotIn("call_mcp_tool(*hub_openrouter*)", allow_set)
        self.assertIn("call_mcp_tool(*hub_openrouter*)", deny_set)

        # Host shell & secret profile files must be strictly denied
        self.assertIn("command(*)", deny_set)
        self.assertIn("run_command", deny_set)
        self.assertIn("run_command(*)", deny_set)
        self.assertIn("read_file(*oauth-token*)", deny_set)
        self.assertIn("read_file(*mcp_config.json*)", deny_set)
        self.assertIn("read_file(*.credentials.json*)", deny_set)

        up_posix = upload_dir.as_posix().rstrip("/")
        self.assertIn(f"read_file({up_posix}/**)", allow_set)
        self.assertIn(f"write_file({up_posix}/**)", deny_set)
        self.assertIn(f"edit_file({up_posix}/**)", deny_set)

        # When sandbox_network == "none", web tools must not be added to allow
        offline_args = agy_engine._build_permission_args(
            "project",
            "full_access",
            is_admin=False,
            cli_home_dir=guest_home,
            project_dir=proj_dir,
            allow_hub_sandbox=True,
            force_no_host_shell=True,
            sandbox_network="none",
        )
        offline_allow = set(json.loads(offline_args[offline_args.index("--settings") + 1])["permissions"]["allow"])
        self.assertNotIn("search_web(*)", offline_allow)
        self.assertNotIn("read_url(*)", offline_allow)

        # End-to-end stream_agy_turn for BOTH Admin and Guest in Docker shared project + docker_unavailable contour
        fake_agy_exe = self.tmp_root / "fake_agy.exe"
        fake_agy_exe.write_bytes(b"MZ")
        recorded: list[dict[str, Any]] = []

        async def _fake_subproc(*cmd: Any, **kwargs: Any) -> _FakeAgyProc:
            recorded.append({"cmd": [str(c) for c in cmd], "env": dict(kwargs.get("env") or {})})
            return _FakeAgyProc(
                [
                    {"event": "init", "conversation_id": "conv-v93"},
                    {"event": "message", "delta": "ok"},
                    {"event": "result", "status": "success", "usage": {"total_tokens": 12}},
                ]
            )

        for actor_label, actor in (("admin", admin), ("guest", guest)):
            cid = f"chat_dkr_{actor_label}"
            (config.UPLOADS_DIR / cid).mkdir(parents=True, exist_ok=True)
            with (
                patch("app.agy_engine.resolve_agy_bin", return_value=str(fake_agy_exe)),
                patch(
                    "app.agy_engine.check_docker_status",
                    return_value={"sandbox_mcp_ready": True, "daemon_running": True, "image_ready": True},
                ),
                patch("asyncio.create_subprocess_exec", side_effect=_fake_subproc),
            ):
                async for _ in agy_engine.stream_agy_turn(
                    chat_id=cid,
                    kind="project",
                    project_name="Tulpar_VPN",
                    model="gemini-4-carbon-high",
                    access_mode="full_access",
                    prompt="Исправь main.py и запусти pytest",
                    attachments=[],
                    agy_conversation_id="",
                    history_messages=[],
                    user=actor,
                    resolved_project_dir=proj_dir,
                    execution_mode="docker",
                ):
                    pass
                agy_engine.ACTIVE_RUNS.clear()

        # Also test docker_unavailable contour (Docker Desktop stopped)
        with (
            patch("app.agy_engine.resolve_agy_bin", return_value=str(fake_agy_exe)),
            patch(
                "app.agy_engine.check_docker_status",
                return_value={"installed": True, "sandbox_mcp_ready": False, "daemon_running": False, "image_ready": False},
            ),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_subproc),
        ):
            async for _ in agy_engine.stream_agy_turn(
                chat_id="chat_dkr_down",
                kind="project",
                project_name="Tulpar_VPN",
                model="gemini-4-carbon-high",
                access_mode="full_access",
                prompt="Проверь тесты",
                attachments=[],
                agy_conversation_id="",
                history_messages=[],
                user=guest,
                resolved_project_dir=proj_dir,
                execution_mode="docker",
            ):
                pass
            agy_engine.ACTIVE_RUNS.clear()

        self.assertEqual(len(recorded), 3)
        for idx, actor in ((0, admin), (1, guest)):
            cmd = recorded[idx]["cmd"]
            self.assertNotIn("--dangerously-skip-permissions", cmd)
            add_dirs = [Path(cmd[i + 1]).resolve() for i, tok in enumerate(cmd[:-1]) if tok == "--add-dir"]
            self.assertIn(proj_dir, add_dirs)
            actor_home = config.ensure_user_cli_home(actor).resolve()
            self.assertNotIn(actor_home, add_dirs)
            self.assertNotIn(config.AGY_CLI_DATA_DIR.resolve(), add_dirs)
            self.assertTrue(
                any(p.name in ("mcp", "skills") for p in add_dirs),
                f"Expected safe CLI mcp/skills dirs in --add-dir, got: {add_dirs}",
            )
            prompt_arg = cmd[cmd.index("-p") + 1]
            self.assertIn("hub_sandbox", prompt_arg)
            self.assertIn("/workspace", prompt_arg)
            self.assertIn("НИКОГДА не вызывай `run_command`", prompt_arg)
            self.assertNotIn("WaitMsBeforeAsync", prompt_arg)

        prompt_down = recorded[2]["cmd"][recorded[2]["cmd"].index("-p") + 1]
        self.assertIn("Docker Desktop (демон Docker) не запущен", prompt_down)
        self.assertIn("НИКОГДА не вызывай `run_command`", prompt_down)

    async def test_2_isolated_git_boundary_creation(self) -> None:
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database

        # Simulate parent repo `.git` + `.gitignore` ignoring `data/` above SHARED_PROJECTS_DIR
        parent_git = self.tmp_root / ".git"
        parent_git.mkdir(parents=True, exist_ok=True)
        (self.tmp_root / ".gitignore").write_text("data/\n", encoding="utf-8")

        legacy_proj = (config.SHARED_PROJECTS_DIR / "Tulpar_VPN_Legacy").resolve()
        legacy_proj.mkdir(parents=True, exist_ok=True)
        (legacy_proj / "server.py").write_text("# vpn\n", encoding="utf-8")
        self.assertFalse((legacy_proj / ".git").exists())

        guest = await database.create_user(
            username="git_boundary_guest",
            password="guest_password_123",
            max_access_mode="docker_full",
        )
        # Insert legacy row directly without `.git` to simulate pre-existing folder before v93
        import aiosqlite

        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO shared_projects (id, name, path, description, execution_mode, member_ids_json, created_by, created_at, updated_at)
                VALUES ('sp_legacy_git', 'Tulpar_VPN_Legacy', ?, '', 'docker', ?, 'admin', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """,
                (str(legacy_proj), json.dumps([guest["id"]])),
            )
            await db.commit()
        shared_rows = await database.list_shared_projects(for_user_id=guest["id"], is_admin=False)

        resolved_info = brain_reader.resolve_user_project(
            "Tulpar_VPN_Legacy",
            guest,
            shared_rows,
        )
        assert resolved_info is not None
        self.assertEqual(Path(resolved_info["path"]).resolve(), legacy_proj)
        self.assertTrue((legacy_proj / ".git").exists(), "Isolated .git boundary must be created automatically")
        self.assertTrue((legacy_proj / ".git" / "HEAD").exists())

        # Idempotency check: custom marker inside `.git/description` must remain untouched on subsequent calls
        marker_file = legacy_proj / ".git" / "description"
        marker_file.write_text("keep-existing-git-marker", encoding="utf-8")
        brain_reader.ensure_isolated_project_git_boundary(legacy_proj)
        self.assertEqual(marker_file.read_text(encoding="utf-8"), "keep-existing-git-marker")

    async def test_3_shared_project_storage_locations_and_zero_trust_denylist(self) -> None:
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database
        import app.main as main_mod

        guest = await database.create_user(
            username="root_collab_guest",
            password="guest_password_123",
            max_access_mode="docker_full",
        )
        client = _MiniAsgiClient(main_mod.app)

        # Pre-existing project folder inside PROJECTS_ROOT (e.g. D:\Internet\Antigravity\Tulpar_VPN)
        existing_proj = (config.PROJECTS_ROOT / "Tulpar_VPN").resolve()
        existing_proj.mkdir(parents=True, exist_ok=True)
        (existing_proj / "README.md").write_text("# Tulpar VPN\n", encoding="utf-8")

        resp_create = await client.request(
            "POST",
            "/api/projects/create",
            headers=self._headers("admin"),
            json_body={
                "name": "Tulpar_VPN",
                "is_shared": True,
                "storage_location": "projects_root",
                "execution_mode": "docker",
                "member_ids": [guest["id"]],
            },
        )
        self.assertEqual(resp_create.status_code, 200, resp_create.text)
        sp_created = resp_create.json()["project"]
        self.assertEqual(sp_created["storage_location"], "projects_root")
        self.assertEqual(Path(sp_created["path"]).resolve(), existing_proj)
        self.assertEqual((existing_proj / "README.md").read_text(encoding="utf-8"), "# Tulpar VPN\n")
        # Clean folder in PROJECTS_ROOT without a parent `.git` above it stays untouched until needed:
        self.assertFalse((existing_proj / ".git").exists())
        (self.tmp_root / ".git").mkdir(parents=True, exist_ok=True)
        brain_reader.ensure_isolated_project_git_boundary(existing_proj)
        self.assertTrue((existing_proj / ".git").exists())

        # Create a chat inside this shared project for the guest
        resp_chat = await client.request(
            "POST",
            "/api/chats",
            headers=self._headers(guest["id"]),
            json_body={"kind": "project", "project_name": "Tulpar_VPN", "title": "VPN Task"},
        )
        self.assertEqual(resp_chat.status_code, 200, resp_chat.text)
        chat_id = resp_chat.json()["chat"]["id"]

        # Non-admin MUST NEVER see the Windows host path in /api/projects or /api/chats
        resp_guest_projs = await client.request("GET", "/api/projects", headers=self._headers(guest["id"]))
        self.assertEqual(resp_guest_projs.status_code, 200)
        guest_projs = resp_guest_projs.json()["projects"]
        tp_guest = next(p for p in guest_projs if p["name"] == "Tulpar_VPN")
        self.assertEqual(tp_guest["path"], "shared/Tulpar_VPN")
        self.assertNotIn(str(existing_proj), resp_guest_projs.text)

        resp_guest_chats = await client.request("GET", "/api/chats", headers=self._headers(guest["id"]))
        self.assertEqual(resp_guest_chats.status_code, 200)
        self.assertNotIn(str(existing_proj), resp_guest_chats.text)

        # Admin DOES see real disk path & storage_location in /api/admin/shared-projects and /api/system/info
        resp_adm_sp = await client.request("GET", "/api/admin/shared-projects", headers=self._headers("admin"))
        self.assertEqual(resp_adm_sp.status_code, 200)
        adm_sp = next(s for s in resp_adm_sp.json()["shared_projects"] if s["id"] == sp_created["id"])
        self.assertEqual(adm_sp["storage_location"], "projects_root")
        self.assertEqual(Path(adm_sp["path"]).resolve(), existing_proj)

        resp_sys = await client.request("GET", "/api/system/info", headers=self._headers("admin"))
        self.assertEqual(resp_sys.status_code, 200)
        self.assertEqual(
            Path(resp_sys.json()["shared_projects_dir"]).resolve(),
            config.SHARED_PROJECTS_DIR.resolve(),
        )

        # Switching storage_location updates chats.project_path and stops old project sandboxes
        with patch("app.sandbox_service.stop_project_sandboxes", new_callable=AsyncMock) as mock_stop:
            resp_patch = await client.request(
                "PATCH",
                f"/api/admin/shared-projects/{sp_created['id']}",
                headers=self._headers("admin"),
                json_body={"storage_location": "shared_dir"},
            )
            self.assertEqual(resp_patch.status_code, 200, resp_patch.text)
            self.assertEqual(resp_patch.json()["shared_project"]["storage_location"], "shared_dir")
            mock_stop.assert_awaited_once()

        moved_chat = await database.get_chat(chat_id)
        assert moved_chat is not None
        self.assertEqual(
            Path(moved_chat["project_path"]).resolve(),
            (config.SHARED_PROJECTS_DIR / "Tulpar_VPN").resolve(),
        )

        # Zero-Trust Denylist: Antigravity_Hub (BASE_DIR), DATA_DIR, CLI_HOMES_DIR, and `..` must be rejected (400 / ValueError)
        fake_hub_base = (config.PROJECTS_ROOT / "Antigravity_Hub").resolve()
        fake_hub_base.mkdir(parents=True, exist_ok=True)
        with patch.object(config, "BASE_DIR", fake_hub_base):
            resp_bad_hub = await client.request(
                "POST",
                "/api/projects/create",
                headers=self._headers("admin"),
                json_body={"name": "Antigravity_Hub", "is_shared": True, "storage_location": "projects_root"},
            )
            self.assertEqual(resp_bad_hub.status_code, 400, resp_bad_hub.text)

        for bad_name in ("../escape", "..\\escape", "..", "CON"):
            resp_bad = await client.request(
                "POST",
                "/api/projects/create",
                headers=self._headers("admin"),
                json_body={"name": bad_name, "is_shared": True, "storage_location": "projects_root"},
            )
            self.assertEqual(resp_bad.status_code, 400, (bad_name, resp_bad.text))

        with self.assertRaises(ValueError):
            brain_reader.validate_shared_project_path(
                "data", storage_location="", existing_path=str(config.DATA_DIR)
            )
        with self.assertRaises(ValueError):
            brain_reader.validate_shared_project_path(
                "cli_homes", storage_location="", existing_path=str(config.CLI_HOMES_DIR)
            )

    async def test_4_auto_upgrade_members_docker_and_tri_engine_parity(self) -> None:
        import app.claude_engine as claude_engine
        import app.codex_engine as codex_engine
        import app.config as config
        import app.database as database
        import app.main as main_mod

        u_write = await database.create_user(
            username="friend_write_only",
            password="friend_password_123",
            max_access_mode="write_files",
        )
        u_read = await database.create_user(
            username="friend_read_only",
            password="friend_password_123",
            max_access_mode="read_only",
        )
        u_host = await database.create_user(
            username="friend_host_full",
            password="friend_password_123",
            max_access_mode="host_full",
        )
        client = _MiniAsgiClient(main_mod.app)

        # Create Docker shared project with upgrade_members_docker=True -> write_files upgrades to docker_full, host_full untouched!
        with patch(
            "app.agy_engine.check_docker_status",
            AsyncMock(return_value={"sandbox_mcp_ready": True, "daemon_running": True, "image_ready": True}),
        ):
            resp_cr = await client.request(
                "POST",
                "/api/projects/create",
                headers=self._headers("admin"),
                json_body={
                    "name": "AutoUpgrade_Lab",
                    "is_shared": True,
                    "storage_location": "shared_dir",
                    "execution_mode": "docker",
                    "member_ids": [u_write["id"], u_host["id"]],
                    "upgrade_members_docker": True,
                },
            )
        self.assertEqual(resp_cr.status_code, 200, resp_cr.text)
        sp = resp_cr.json()["project"]
        proj_dir = Path(sp["path"]).resolve()

        fresh_write = await database.get_user_by_id(u_write["id"])
        fresh_host = await database.get_user_by_id(u_host["id"])
        assert fresh_write is not None and fresh_host is not None
        self.assertEqual(fresh_write["max_access_mode"], "docker_full")
        self.assertEqual(fresh_host["max_access_mode"], "host_full")

        # Verify docker-sandbox SKILL.md was synced for the upgraded user
        skill_md = (
            config.ensure_user_cli_home(fresh_write)
            / ".gemini"
            / "config"
            / "skills"
            / "docker-sandbox"
            / "SKILL.md"
        )
        self.assertTrue(skill_md.is_file(), f"Expected docker-sandbox SKILL.md at {skill_md}")

        # PATCH adding u_read with upgrade_members_docker=True also upgrades u_read -> docker_full
        resp_up = await client.request(
            "PATCH",
            f"/api/admin/shared-projects/{sp['id']}",
            headers=self._headers("admin"),
            json_body={
                "member_ids": [u_write["id"], u_host["id"], u_read["id"]],
                "upgrade_members_docker": True,
            },
        )
        self.assertEqual(resp_up.status_code, 200, resp_up.text)
        fresh_read = await database.get_user_by_id(u_read["id"])
        assert fresh_read is not None
        self.assertEqual(fresh_read["max_access_mode"], "docker_full")

        # Codex parity: stream_codex_turn in Docker shared project passes hub_sandbox developerInstructions + docker prompt
        await database.upsert_user_chatgpt_account(
            fresh_write["id"],
            raw_auth_json=json.dumps({"tokens": {"access_token": "tok_cdx", "account_id": "acc_cdx"}}),
            chatgpt_account_id="acc_cdx",
            email="friend@example.com",
            label="Plus",
            plan_type="plus",
        )
        codex_capture: dict[str, Any] = {}
        fake_codex_bin = self.tmp_root / "fake_codex.exe"
        fake_codex_bin.write_bytes(b"MZ")

        async def _fake_codex_exec(*cmd: Any, **kwargs: Any) -> _FakeCodexProc:
            codex_capture["cmd"] = [str(c) for c in cmd]
            codex_capture["cwd"] = kwargs.get("cwd")
            return _FakeCodexProc(codex_capture)

        with (
            patch("app.codex_engine.resolve_codex_bin", return_value=str(fake_codex_bin)),
            patch(
                "app.codex_engine.check_docker_status",
                return_value={"sandbox_mcp_ready": True, "daemon_running": True, "image_ready": True},
            ),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_codex_exec),
        ):
            async for _ in codex_engine.stream_codex_turn(
                chat_id="c_codex_v93",
                kind="project",
                project_name="AutoUpgrade_Lab",
                model="codex:gpt-5.3-codex",
                access_mode="full_access",
                prompt="Запусти pytest",
                attachments=[],
                codex_thread_id="",
                codex_account_id="",
                history_messages=[],
                user=fresh_write,
                resolved_project_dir=proj_dir,
                execution_mode="docker",
            ):
                pass
            codex_engine.ACTIVE_RUNS.clear()

        ts_params = codex_capture["thread_start"]
        self.assertEqual(ts_params["sandbox"], "workspace-write")
        self.assertIn("hub_sandbox", ts_params["developerInstructions"])
        self.assertIn("/workspace", ts_params["developerInstructions"])
        codex_prompt_text = json.dumps(codex_capture["turn_start"]["input"], ensure_ascii=False)
        self.assertIn("hub_sandbox", codex_prompt_text)

        # Claude parity: stream_claude_turn passes --add-dir proj_dir, Read/Edit/Write/Glob/Grep + mcp__hub_sandbox__*, and Bash disallowed
        await database.upsert_user_claude_account(
            fresh_write["id"],
            raw_credentials_json=json.dumps(
                {"claudeAiOauth": {"accessToken": "sk-ant-oat01-test", "refreshToken": "rt_test"}}
            ),
            claude_account_id="cld_acc_v93",
            email="friend@example.com",
            label="Max",
            plan_type="max",
        )
        claude_capture: dict[str, Any] = {}
        fake_claude_bin = self.tmp_root / "fake_claude.exe"
        fake_claude_bin.write_bytes(b"MZ")

        async def _fake_claude_spawn(argv: list[str], env: dict[str, str], cwd: Path) -> _FakeClaudeProc:
            claude_capture["argv"] = list(argv)
            claude_capture["cwd"] = cwd
            return _FakeClaudeProc(claude_capture)

        with (
            patch("app.claude_engine.resolve_claude_bin", return_value=str(fake_claude_bin)),
            patch(
                "app.claude_engine.check_docker_status",
                return_value={"sandbox_mcp_ready": True, "daemon_running": True, "image_ready": True},
            ),
            patch("app.claude_engine._spawn", side_effect=_fake_claude_spawn),
        ):
            async for _ in claude_engine.stream_claude_turn(
                chat_id="c_claude_v93",
                kind="project",
                project_name="AutoUpgrade_Lab",
                model="claude-code:sonnet@high",
                access_mode="full_access",
                prompt="Запусти pytest",
                attachments=[],
                claude_session_id="",
                claude_account_id="",
                history_messages=[],
                user=fresh_write,
                resolved_project_dir=proj_dir,
                execution_mode="docker",
            ):
                pass
            claude_engine.ACTIVE_RUNS.clear()

        cl_argv = claude_capture["argv"]
        cl_add_dirs = [Path(cl_argv[i + 1]).resolve() for i, tok in enumerate(cl_argv[:-1]) if tok == "--add-dir"]
        self.assertIn(proj_dir, cl_add_dirs)
        self.assertEqual(cl_argv[cl_argv.index("--permission-mode") + 1], "acceptEdits")
        cl_allowed = set(cl_argv[cl_argv.index("--allowedTools") + 1].split(","))
        for expected_tool in ("Read", "Glob", "Grep", "LS", "Write", "Edit", "MultiEdit", "mcp__hub_sandbox__*"):
            self.assertIn(expected_tool, cl_allowed)
        cl_disallowed = set(cl_argv[cl_argv.index("--disallowedTools") + 1].split(","))
        self.assertIn("Bash", cl_disallowed)
        self.assertIn("hub_sandbox", cl_argv[cl_argv.index("--append-system-prompt") + 1])

    async def test_5_frontend_syntax_utf8_no_bom_and_live_db_untouched(self) -> None:
        import app.config as config

        # 1. JS syntax check via node --check
        js_path = ROOT_DIR / "app" / "static" / "app.js"
        res = subprocess.run(
            ["node", "--check", str(js_path)],
            capture_output=True,
            text=True,
            timeout=15,
            **config.subprocess_hidden_kwargs(),
        )
        self.assertEqual(res.returncode, 0, f"node --check failed: {res.stderr}")

        # 2. UTF-8 without BOM check across all touched files
        checked_files = [
            ROOT_DIR / "app" / "brain_reader.py",
            ROOT_DIR / "app" / "sandbox_service.py",
            ROOT_DIR / "app" / "database.py",
            ROOT_DIR / "app" / "main.py",
            ROOT_DIR / "app" / "agy_engine.py",
            ROOT_DIR / "app" / "codex_engine.py",
            ROOT_DIR / "app" / "claude_engine.py",
            ROOT_DIR / "app" / "static" / "index.html",
            ROOT_DIR / "app" / "static" / "app.js",
            ROOT_DIR / "app" / "static" / "styles.css",
            ROOT_DIR / "app" / "static" / "sw.js",
        ]
        for fp in checked_files:
            raw = fp.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"UTF-8 BOM detected in {fp}")
            raw.decode("utf-8")

        # 3. Cache-bust v>=93 synchronized in index.html & sw.js
        idx_html = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")
        sw_js = (ROOT_DIR / "app" / "static" / "sw.js").read_text(encoding="utf-8")
        m_css = re.search(r"/static/styles\.css\?v=(\d+)", idx_html)
        m_js = re.search(r"/static/app\.js\?v=(\d+)", idx_html)
        self.assertIsNotNone(m_css)
        self.assertIsNotNone(m_js)
        ver = int(m_css.group(1))
        self.assertGreaterEqual(ver, 93)
        self.assertEqual(int(m_js.group(1)), ver)
        self.assertIn(f"agy-hub-v{ver}", sw_js)
        self.assertIn(f"/static/styles.css?v={ver}", sw_js)
        self.assertIn(f"/static/app.js?v={ver}", sw_js)

        # 4. CSS safety invariant: main.workspace must NEVER define transform/filter/backdrop-filter/perspective/contain
        css_text = (ROOT_DIR / "app" / "static" / "styles.css").read_text(encoding="utf-8")
        for m in re.finditer(r"main\.workspace\s*\{([^}]*)\}", css_text):
            body = m.group(1)
            for forbidden_prop in ("transform", "filter", "backdrop-filter", "perspective", "contain"):
                self.assertNotRegex(body, rf"(?<![-\w]){forbidden_prop}\s*:")

        # 5. Live data/hub.db immutability check
        if self._live_stat_before is not None:
            live_now = (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            self.assertEqual(self._live_stat_before, live_now)


if __name__ == "__main__":
    unittest.main()
