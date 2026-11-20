"""
Isolated verification suite for Process & Background Script Subsystem (Host + Docker),
Global Process List, Watchdog, Live Chat Card & Sticky Bar (TZ v109 / static v>=109):

1. Host process lifecycle (`running` -> `completed` vs `crashed` vs manual `stopped`),
   Windows `get_process_working_set_mb(pid)` via `ctypes`, `poll_running_services_transitions()`,
   `ack` alert clearing, and `SKILL.md` rules.
2. Zero-Trust RBAC & `project_dir` scope isolation across same-named projects (`admin` vs `host_full` user),
   sanitization of host filesystem paths (`log_path`, `project_dir`, `workdir`) on `/api/processes`
   and `/api/projects/{name}/processes*`, and per-recipient WebSocket isolation.
3. Docker sandbox background process metadata (`list_project_sandbox_services` cold-container fast exit
   + warm-container unified statuses) and `reap_idle_sandboxes_once` protection for live `/tmp/agy_svc_*.pid`.
4. Frontend static invariants (`UTF-8` no BOM, CSS brace balance, `main.workspace` GPU/scroll-safety invariant,
   `sw.js` <-> `index.html` cache version sync, `.hidden` empty strips) and Node VM unit tests for
   `extractProcessControlsFromToolCalls`, `formatProcessDurationShort`, `buildProcessErrorPromptText`,
   and `.msg-process-cards-wrap` in `COPY_STRIP_SELECTOR`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("ORT_DISABLE_ALL_GPU", "1")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


class _MiniAsgiResponse:
    def __init__(self, status_code: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self.content = body
        self.text = body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


class _MiniAsgiClient:
    """Lightweight in-process ASGI test client without external test dependencies."""

    def __init__(self, asgi_app: Any) -> None:
        self._app = asgi_app

    async def request(
        self,
        method: str,
        raw_path: str,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        client_host: str = "127.0.0.1",
    ) -> _MiniAsgiResponse:
        if "?" in raw_path:
            path_part, query_part = raw_path.split("?", 1)
        else:
            path_part, query_part = raw_path, ""

        body_bytes = b""
        req_headers: list[tuple[bytes, bytes]] = [(b"host", b"127.0.0.1:8765")]
        if json_body is not None:
            body_bytes = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            req_headers.append((b"content-type", b"application/json"))
            req_headers.append((b"content-length", str(len(body_bytes)).encode("ascii")))
        for k, v in (headers or {}).items():
            req_headers.append((k.lower().encode("latin-1"), str(v).encode("latin-1")))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": path_part,
            "raw_path": path_part.encode("utf-8"),
            "query_string": query_part.encode("utf-8"),
            "headers": req_headers,
            "client": (client_host, 54321),
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
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def _send(message: dict[str, Any]) -> None:
            nonlocal resp_status
            mtype = message.get("type")
            if mtype == "http.response.start":
                resp_status = int(message.get("status", 200))
                for hk, hv in message.get("headers") or []:
                    resp_headers[hk.decode("latin-1").lower()] = hv.decode("latin-1")
            elif mtype == "http.response.body":
                chunk = message.get("body", b"")
                if chunk:
                    resp_chunks.append(chunk)

        await self._app(scope, _receive, _send)
        return _MiniAsgiResponse(resp_status, resp_headers, b"".join(resp_chunks))


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


class TestProcessMonitorSubsystemV109(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hub_proc_mon_v109_", ignore_cleanup_errors=True)
        self.tmp_root = Path(self._tmp.name).resolve()
        self.data_dir = self.tmp_root / "data"
        self.projects_root = self.tmp_root / "users" / "admin" / "projects"
        self.users_root = self.tmp_root / "users"
        self.shared_projects_dir = self.tmp_root / "shared_projects"
        self.workspaces_dir = self.tmp_root / "users"
        self.cli_homes_dir = self.data_dir / "cli_homes"
        self.uploads_dir = self.data_dir / "uploads"
        self.agy_cli_dir = self.tmp_root / "agy_cli_shared"
        self.agy_app_dir = self.tmp_root / "agy_app_desktop"
        self.db_path = self.data_dir / "hub.db"

        for d in (
            self.data_dir,
            self.projects_root,
            self.users_root,
            self.shared_projects_dir,
            self.cli_homes_dir,
            self.uploads_dir,
            self.agy_cli_dir,
            self.agy_app_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        import app.agy_engine as agy_engine
        import app.auth as auth
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database
        import app.host_service_manager as host_service_manager
        import app.main as main_mod
        import app.sandbox_service as sandbox_service

        self.agy_engine = agy_engine
        self.auth = auth
        self.brain_reader = brain_reader
        self.config = config
        self.database = database
        self.host_service_manager = host_service_manager
        self.main_mod = main_mod
        self.sandbox_service = sandbox_service

        self._isolated_hsm = host_service_manager.HostServiceManager()

        self._patches = []
        for mod, mapping in (
            (
                config,
                {
                    "DATA_DIR": self.data_dir,
                    "DB_PATH": self.db_path,
                    "PROJECTS_ROOT": self.projects_root,
                    "USERS_ROOT_DIR": self.users_root,
                    "SHARED_PROJECTS_DIR": self.shared_projects_dir,
                    "USER_WORKSPACES_DIR": self.workspaces_dir,
                    "CLI_HOMES_DIR": self.cli_homes_dir,
                    "UPLOADS_DIR": self.uploads_dir,
                    "AGY_CLI_DATA_DIR": self.agy_cli_dir,
                    "AGY_APP_DATA_DIR": self.agy_app_dir,
                },
            ),
            (database, {"DB_PATH": self.db_path}),
            (
                brain_reader,
                {
                    "PROJECTS_ROOT": self.projects_root,
                    "AGY_APP_DATA_DIR": self.agy_app_dir,
                    "SHARED_PROJECTS_DIR": self.shared_projects_dir,
                    "USER_WORKSPACES_DIR": self.workspaces_dir,
                    "USERS_ROOT_DIR": self.users_root,
                },
            ),
            (
                agy_engine,
                {
                    "PROJECTS_ROOT": self.projects_root,
                    "CLI_HOMES_DIR": self.cli_homes_dir,
                    "AGY_CLI_DATA_DIR": self.agy_cli_dir,
                },
            ),
            (host_service_manager, {"HOST_SERVICE_MANAGER": self._isolated_hsm}),
            (main_mod, {"PROJECTS_ROOT": self.projects_root}),
        ):
            for attr, val in mapping.items():
                if hasattr(mod, attr):
                    self._patches.append(patch.object(mod, attr, val))
        for p in self._patches:
            p.start()

        if hasattr(database, "close_db_pool"):
            await database.close_db_pool()
        await database.init_db()
        self.admin_user = await database.get_user_by_id("admin")
        assert self.admin_user is not None

        self.u1 = await database.create_user(
            username="u1",
            display_name="User One",
            password="pass1234",
            role="user",
            max_access_mode="host_full",
        )
        self.admin_token = auth.create_user_session_token(
            "admin", int(self.admin_user["session_version"])
        )
        self.u1_token = auth.create_user_session_token(
            self.u1["id"], int(self.u1["session_version"])
        )

    async def asyncTearDown(self) -> None:
        try:
            for key, svc in list(self._isolated_hsm._services.items()):
                pid = int(svc.get("pid") or 0)
                ft = int(svc.get("create_time_ft") or 0)
                if pid > 0:
                    self.host_service_manager._kill_verified_process_tree(pid, ft)
                self._isolated_hsm._reap_proc_handle_locked(key)
        except Exception:
            pass
        if hasattr(self.database, "close_db_pool"):
            await self.database.close_db_pool()
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    async def test_01_host_lifecycle_completed_crashed_stopped_ram_and_watchdog(self) -> None:
        proj_dir = self.projects_root / "LifecycleProj"
        proj_dir.mkdir(parents=True, exist_ok=True)
        py_exe = f'"{sys.executable}"'

        # Idle fast-path: O(1) empty return when no running services exist
        self.assertEqual(self._isolated_hsm.poll_running_services_transitions(), [])

        # 1) Fast script exiting with 0 -> transitions to `completed` (NOT `stopped`)
        res_ok = await asyncio.to_thread(
            self._isolated_hsm.control_service,
            project_name="LifecycleProj",
            project_dir=proj_dir,
            action="start",
            name="fast-ok",
            command=f"{py_exe} -c \"import time; print('ok', flush=True); time.sleep(0.35)\"",
            startup_wait_ms=150,
            user=self.admin_user,
        )
        self.assertTrue(res_ok["ok"], res_ok)

        deadline = time.monotonic() + 5.0
        transitions_ok: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            ev_batch = self._isolated_hsm.poll_running_services_transitions()
            if ev_batch:
                transitions_ok.extend(ev_batch)
                break
            await asyncio.sleep(0.12)

        svcs_after_ok = self._isolated_hsm.list_project_services("LifecycleProj", project_dir=proj_dir)
        fast_view = next(s for s in svcs_after_ok if s["name"] == "fast-ok")
        self.assertEqual(fast_view["status"], "completed")
        self.assertNotEqual(fast_view["status"], "stopped")
        self.assertEqual(fast_view["exit_code"], 0)
        self.assertTrue(bool(fast_view["finished_at"]))
        self.assertIsNotNone(fast_view["duration_sec"])
        self.assertGreaterEqual(float(fast_view["duration_sec"]), 0.0)
        self.assertFalse(fast_view["crash_unseen"])
        self.assertTrue(any(t["transition"] == "completed" for t in transitions_ok))

        # 2) Failing script exiting with code 3 -> `crashed`, `crash_unseen == True`, `error_tail` has "boom"
        await asyncio.to_thread(
            self._isolated_hsm.control_service,
            project_name="LifecycleProj",
            project_dir=proj_dir,
            action="start",
            name="failing-job",
            command=f"{py_exe} -c \"import sys, time; print('boom', flush=True); time.sleep(0.3); sys.exit(3)\"",
            startup_wait_ms=120,
            user=self.admin_user,
        )

        deadline = time.monotonic() + 5.0
        crash_events: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            ev_batch = self._isolated_hsm.poll_running_services_transitions()
            if ev_batch:
                crash_events.extend(ev_batch)
                break
            await asyncio.sleep(0.12)

        self.assertTrue(len(crash_events) >= 1, "Watchdog poll must emit crashed transition")
        crash_ev = next(e for e in crash_events if e["service"]["name"] == "failing-job")
        self.assertEqual(crash_ev["transition"], "crashed")
        self.assertIn("boom", crash_ev["error_tail"])

        crash_view = next(
            s
            for s in self._isolated_hsm.list_project_services("LifecycleProj", project_dir=proj_dir)
            if s["name"] == "failing-job"
        )
        self.assertEqual(crash_view["status"], "crashed")
        self.assertEqual(crash_view["exit_code"], 3)
        self.assertTrue(crash_view["crash_unseen"])

        # `ack` clears `crash_unseen` without deleting the service record
        ack_res = await asyncio.to_thread(
            self._isolated_hsm.control_service,
            project_name="LifecycleProj",
            project_dir=proj_dir,
            action="ack",
            name="failing-job",
            user=self.admin_user,
        )
        self.assertTrue(ack_res["ok"])
        self.assertFalse(ack_res["crash_unseen"])
        after_ack = next(
            s
            for s in self._isolated_hsm.list_project_services("LifecycleProj", project_dir=proj_dir)
            if s["name"] == "failing-job"
        )
        self.assertEqual(after_ack["status"], "crashed")
        self.assertFalse(after_ack["crash_unseen"])

        # 3) Long-running script -> `ram_mb > 0` on Windows, manual `stop` -> `status == "stopped"`
        long_res = await asyncio.to_thread(
            self._isolated_hsm.control_service,
            project_name="LifecycleProj",
            project_dir=proj_dir,
            action="start",
            name="long-worker",
            command=f"{py_exe} -c \"import time; print('working', flush=True); time.sleep(30)\"",
            startup_wait_ms=250,
            user=self.admin_user,
        )
        self.assertTrue(long_res["alive"])
        live_pid = int(long_res["pid"])
        self.assertGreater(live_pid, 0)
        if os.name == "nt":
            rss_mb = self.host_service_manager.get_process_working_set_mb(live_pid)
            self.assertIsNotNone(rss_mb)
            self.assertGreater(float(rss_mb or 0.0), 0.0)

        stop_res = await asyncio.to_thread(
            self._isolated_hsm.control_service,
            project_name="LifecycleProj",
            project_dir=proj_dir,
            action="stop",
            name="long-worker",
            user=self.admin_user,
        )
        self.assertTrue(stop_res["ok"])
        self.assertEqual(stop_res["status"], "stopped")
        self.assertFalse(stop_res["crash_unseen"])

        # Verify SKILL.md rules mention parsers/scripts/workers on both Host and Docker
        host_skill = self.host_service_manager._build_host_services_skill_markdown()
        docker_skill = self.sandbox_service._build_docker_sandbox_skill_markdown()
        self.assertIn("парсер", host_skill.lower())
        self.assertIn("host_service_ctl", host_skill)
        self.assertIn("парсер", docker_skill.lower())
        self.assertIn("sandbox_service_ctl", docker_skill)

    async def test_02_zero_trust_rbac_project_dir_isolation_and_path_sanitization(self) -> None:
        admin_demo = self.projects_root / "demo"
        u1_slug = self.config.sanitize_user_folder_slug(self.u1)
        u1_demo = self.users_root / u1_slug / "projects" / "demo"
        admin_demo.mkdir(parents=True, exist_ok=True)
        u1_demo.mkdir(parents=True, exist_ok=True)

        py_exe = f'"{sys.executable}"'
        client = _MiniAsgiClient(self.main_mod.app)

        # Admin starts a live service in admin's `demo`
        r_adm_start = await client.request(
            "POST",
            "/api/projects/demo/processes/admin-svc/start",
            json_body={
                "command": f"{py_exe} -c \"import time; print('ADMIN_SECRET_LOG_777', flush=True); time.sleep(30)\"",
                "startup_wait_ms": 250,
            },
            headers={"Authorization": f"Bearer {self.admin_token}"},
        )
        self.assertEqual(r_adm_start.status_code, 200, r_adm_start.text)
        self.assertTrue(r_adm_start.json()["alive"])

        # `u1` (`host_full`) has their own `users/u1/projects/demo`:
        # 1. `u1` sees 0 processes in `/api/projects/demo/processes` and `/api/processes`
        r_u1_list = await client.request(
            "GET",
            "/api/projects/demo/processes",
            headers={"Authorization": f"Bearer {self.u1_token}"},
        )
        self.assertEqual(r_u1_list.status_code, 200, r_u1_list.text)
        self.assertEqual(r_u1_list.json()["processes"], [])

        r_u1_global = await client.request(
            "GET",
            "/api/processes",
            headers={"Authorization": f"Bearer {self.u1_token}"},
        )
        self.assertEqual(r_u1_global.status_code, 200)
        self.assertEqual(r_u1_global.json()["processes"], [])

        # 2. `u1` cannot read admin's logs
        r_u1_logs = await client.request(
            "GET",
            "/api/projects/demo/processes/admin-svc/logs",
            headers={"Authorization": f"Bearer {self.u1_token}"},
        )
        self.assertEqual(r_u1_logs.status_code, 200)
        self.assertNotIn("ADMIN_SECRET_LOG_777", r_u1_logs.json().get("log_tail", ""))

        # 3. `u1` attempting `stop` on `admin-svc` does NOT stop admin's service!
        await client.request(
            "POST",
            "/api/projects/demo/processes/admin-svc/stop",
            headers={"Authorization": f"Bearer {self.u1_token}"},
        )
        r_adm_check = await client.request(
            "GET",
            "/api/projects/demo/processes",
            headers={"Authorization": f"Bearer {self.admin_token}"},
        )
        self.assertEqual(r_adm_check.json()["summary"]["running_count"], 1)

        # 4. WebSocket isolation: admin's `demo` broadcast never reaches `u1`'s WS
        ws_admin = _FakeWebSocket()
        ws_u1 = _FakeWebSocket()
        self.main_mod.ws_manager.active[ws_admin] = self.admin_user
        self.main_mod.ws_manager.active[ws_u1] = self.u1
        try:
            await self.main_mod.broadcast_project_processes_update("demo", admin_demo)
            self.assertTrue(len(ws_admin.sent) >= 1)
            self.assertEqual(ws_u1.sent, [])
        finally:
            self.main_mod.ws_manager.active.pop(ws_admin, None)
            self.main_mod.ws_manager.active.pop(ws_u1, None)

        # 5. `u1` starts a service in `u1`'s own `demo` -> responses must hide all host absolute paths
        sub_dir = u1_demo / "backend"
        sub_dir.mkdir(parents=True, exist_ok=True)
        r_u1_start = await client.request(
            "POST",
            "/api/projects/demo/processes/u1-worker/start",
            json_body={
                "command": "python -V",
                "workdir": "backend",
                "startup_wait_ms": 250,
            },
            headers={"Authorization": f"Bearer {self.u1_token}"},
        )
        self.assertEqual(r_u1_start.status_code, 200, r_u1_start.text)

        for endpoint in ("/api/projects/demo/processes", "/api/processes"):
            resp = await client.request(
                "GET",
                endpoint,
                headers={"Authorization": f"Bearer {self.u1_token}"},
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            procs = payload["processes"]
            self.assertEqual(len(procs), 1)
            self.assertEqual(procs[0]["name"], "u1-worker")
            self.assertEqual(procs[0].get("log_path", ""), "")
            self.assertEqual(procs[0].get("project_dir", ""), "")
            self.assertEqual(procs[0].get("workdir"), "backend")
            raw_json = json.dumps(payload, ensure_ascii=False)
            self.assertIsNone(
                re.search(r"[A-Za-z]:[\\/]", raw_json),
                f"Non-admin response leaked Windows host path: {raw_json}",
            )

        # Clean up admin's running service
        await client.request(
            "POST",
            "/api/projects/demo/processes/admin-svc/stop",
            headers={"Authorization": f"Bearer {self.admin_token}"},
        )

    async def test_03_docker_sandbox_services_and_idle_reaper_protection(self) -> None:
        sbx_proj = self.shared_projects_dir / "DockerDemo"
        sbx_proj.mkdir(parents=True, exist_ok=True)
        cname = self.sandbox_service.build_sandbox_container_name("u1", sbx_proj)

        docker_calls: list[list[str]] = []

        # 1. Cold container -> returns `[]` immediately WITHOUT starting container or running `exec`
        async def _mock_docker_cold(args: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
            docker_calls.append(list(args))
            if args and args[0] == "inspect":
                return (1, "", "No such object")
            return (0, "", "")

        with patch.object(self.sandbox_service, "_run_docker_short", side_effect=_mock_docker_cold):
            cold_list = await self.sandbox_service.list_project_sandbox_services(
                "u1", sbx_proj, "DockerDemo"
            )
            self.assertEqual(cold_list, [])
            self.assertFalse(any(c and c[0] in ("run", "exec") for c in docker_calls))

        # 2. Warm container -> parses running, completed, and crashed scripts with RAM & exit codes
        fake_container_items = [
            {
                "name": "scraper",
                "safe_name": "scraper",
                "command": "python3 scrape.py",
                "workdir": ".",
                "pid": 4321,
                "alive": True,
                "status": "running",
                "exit_code": None,
                "ram_mb": 31.5,
                "port": 8080,
                "port_listening": True,
                "started_at": "2026-11-15T12:00:00+00:00",
                "finished_at": "",
                "stopped_at": "",
                "crash_unseen": False,
                "log_path": "/tmp/agy_svc_scraper.log",
            },
            {
                "name": "etl-crash",
                "safe_name": "etl-crash",
                "command": "python3 etl.py",
                "workdir": "jobs",
                "pid": 0,
                "alive": False,
                "status": "crashed",
                "exit_code": 2,
                "ram_mb": None,
                "port": None,
                "port_listening": False,
                "started_at": "2026-11-15T12:00:00+00:00",
                "finished_at": "2026-11-15T12:00:15+00:00",
                "stopped_at": "",
                "crash_unseen": True,
                "log_path": "/tmp/agy_svc_etl-crash.log",
            },
        ]

        async def _mock_docker_warm(args: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
            if args and args[0] == "exec":
                return (0, json.dumps(fake_container_items), "")
            return (0, "", "")

        with patch.object(self.sandbox_service, "_run_docker_short", side_effect=_mock_docker_warm):
            warm_list = await self.sandbox_service.list_project_sandbox_services(
                "u1", sbx_proj, "DockerDemo", running_containers={cname}
            )
            self.assertEqual(len(warm_list), 2)
            self.assertEqual(warm_list[0]["runtime"], "docker")
            self.assertEqual(warm_list[0]["name"], "scraper")
            self.assertEqual(warm_list[0]["status"], "running")
            self.assertEqual(warm_list[0]["ram_mb"], 31.5)
            self.assertEqual(warm_list[1]["name"], "etl-crash")
            self.assertEqual(warm_list[1]["status"], "crashed")
            self.assertEqual(warm_list[1]["exit_code"], 2)
            self.assertTrue(warm_list[1]["crash_unseen"])
            self.assertEqual(warm_list[1]["duration_sec"], 15.0)

        # 3. Idle Reaper: preserves container with live `/tmp/agy_svc_*.pid`, reaps truly idle container
        live_cname = "agy-sbx-live-bg-001"
        idle_cname = "agy-sbx-empty-idle-002"
        old_mono = time.monotonic() - (self.sandbox_service.SANDBOX_IDLE_TTL_SECONDS + 300)
        self.sandbox_service._CONTAINER_LAST_USED[live_cname] = old_mono
        self.sandbox_service._CONTAINER_STARTED_MONO[live_cname] = time.monotonic() - 600
        self.sandbox_service._CONTAINER_LAST_USED[idle_cname] = old_mono
        self.sandbox_service._CONTAINER_STARTED_MONO[idle_cname] = time.monotonic() - 600

        removed_containers: list[str] = []

        async def _mock_docker_reaper(args: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
            if args[:2] == ["ps", "-a"]:
                return (0, f"{live_cname}|running\n{idle_cname}|running\n", "")
            if args and args[0] == "exec":
                target_c = args[3]
                if target_c == live_cname:
                    return (0, "LIVE\n", "")
                return (1, "", "")
            if args[:2] == ["rm", "-f"]:
                removed_containers.append(args[2])
                return (0, "", "")
            return (0, "", "")

        try:
            with patch.object(self.sandbox_service, "_run_docker_short", side_effect=_mock_docker_reaper):
                reaped_cnt = await self.sandbox_service.reap_idle_sandboxes_once()
                self.assertEqual(reaped_cnt, 1)
                self.assertEqual(removed_containers, [idle_cname])
                self.assertNotIn(live_cname, removed_containers)
                self.assertGreater(
                    self.sandbox_service._CONTAINER_LAST_USED[live_cname], old_mono
                )
        finally:
            self.sandbox_service._CONTAINER_LAST_USED.pop(live_cname, None)
            self.sandbox_service._CONTAINER_STARTED_MONO.pop(live_cname, None)
            self.sandbox_service._CONTAINER_LAST_USED.pop(idle_cname, None)
            self.sandbox_service._CONTAINER_STARTED_MONO.pop(idle_cname, None)

    def test_04_frontend_static_invariants_and_node_vm_helpers(self) -> None:
        index_path = ROOT_DIR / "app" / "static" / "index.html"
        sw_path = ROOT_DIR / "app" / "static" / "sw.js"
        css_path = ROOT_DIR / "app" / "static" / "styles.css"
        js_path = ROOT_DIR / "app" / "static" / "app.js"

        for p in (index_path, sw_path, css_path, js_path):
            raw = p.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{p.name} must be UTF-8 without BOM")

        index_html = index_path.read_text(encoding="utf-8")
        sw_js = sw_path.read_text(encoding="utf-8")
        styles_css = css_path.read_text(encoding="utf-8")

        # Cache version sync (`v >= 109`)
        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_js)
        self.assertIsNotNone(m_sw, "sw.js must define CACHE_NAME = \"agy-hub-v<N>\"")
        ver = int(m_sw.group(1))
        self.assertGreaterEqual(ver, 109)
        self.assertIn(f'/static/styles.css?v={ver}"', index_html)
        self.assertIn(f'/static/app.js?v={ver}"', index_html)
        self.assertIn(f'/static/styles.css?v={ver}"', sw_js)
        self.assertIn(f'/static/app.js?v={ver}"', sw_js)

        # Required DOM elements & default `.hidden` state
        self.assertRegex(
            index_html,
            r'id="sidebarProcessesStrip"[^>]*class="[^"]*\bhidden\b',
        )
        self.assertRegex(
            index_html,
            r'id="chatActiveProcessesBar"[^>]*class="[^"]*\bhidden\b',
        )
        self.assertIn('id="hostServicesScopeTabs"', index_html)
        self.assertIn('id="sendProcessLogsToAiBtn"', index_html)

        # `main.workspace` CSS invariant: no transform/filter/backdrop-filter/perspective/contain/container-type
        for m_ws in re.finditer(r"main\.workspace\s*\{([^}]*)\}", styles_css):
            block = m_ws.group(1)
            for forbidden in (
                "transform",
                "filter",
                "backdrop-filter",
                "perspective",
                "contain",
                "container-type",
            ):
                self.assertIsNone(
                    re.search(rf"(?<![\w-]){re.escape(forbidden)}\s*:", block),
                    f"Forbidden CSS property `{forbidden}` found on `main.workspace`!",
                )

        # CSS brace balance check (ignoring comments)
        css_no_comments = re.sub(r"/\*[\s\S]*?\*/", "", styles_css)
        self.assertEqual(
            css_no_comments.count("{"),
            css_no_comments.count("}"),
            "Unbalanced curly braces in app/static/styles.css",
        )

        # Node VM test of exported pure helpers & COPY_STRIP_SELECTOR
        node_script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const code = fs.readFileSync("app/static/app.js", "utf8");
const noop = () => {};
const dummyEl = () => ({
  style: {},
  classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
  addEventListener: noop,
  removeEventListener: noop,
  querySelector: () => null,
  querySelectorAll: () => [],
  setAttribute: noop,
  getAttribute: () => "",
  appendChild: noop,
  dataset: {},
});

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  URL,
  URLSearchParams,
  Blob: class Blob {},
  FormData: class FormData {},
  AbortController: class AbortController { abort() {} },
  CSS: { escape: (s) => String(s) },
  localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  sessionStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  navigator: { clipboard: { writeText: async () => {} }, userAgent: "node-test" },
  location: { protocol: "http:", host: "127.0.0.1:8765", origin: "http://127.0.0.1:8765", pathname: "/" },
  matchMedia: () => ({ matches: false, addEventListener: noop }),
  requestAnimationFrame: (cb) => setTimeout(cb, 0),
  cancelAnimationFrame: clearTimeout,
  document: {
    documentElement: { style: {}, dataset: {}, setAttribute: noop, getAttribute: () => "dark" },
    body: dummyEl(),
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => dummyEl(),
    addEventListener: noop,
    removeEventListener: noop,
  },
  __AGY_EXPORT_ADVISOR_FOR_TESTS__: true,
  __AGY_SKIP_AUTO_INIT__: true,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

vm.runInNewContext(code, sandbox, { filename: "app.js" });
const api = sandbox.__AGY_ADVISOR_API__;
assert(api, "__AGY_ADVISOR_API__ must be exported");

assert(
  String(api.COPY_STRIP_SELECTOR || "").includes(".msg-process-cards-wrap"),
  "COPY_STRIP_SELECTOR must include .msg-process-cards-wrap"
);

assert.strictEqual(typeof api.extractProcessControlsFromToolCalls, "function");
assert.strictEqual(typeof api.formatProcessDurationShort, "function");
assert.strictEqual(typeof api.buildProcessErrorPromptText, "function");

const extracted = api.extractProcessControlsFromToolCalls([
  {
    name: "mcp_hub_host_host_service_ctl",
    parameters: { action: "start", name: "parser", command: "python parse.py", port: 8091 },
  },
  {
    name: "call_mcp_tool",
    parameters: {
      ServerName: "hub_sandbox",
      ToolName: "sandbox_service_ctl",
      Arguments: { action: "restart", name: "api-srv", command: "uvicorn app:app --port 8000", port: 8000 },
    },
  },
  {
    name: "mcp_hub_host_host_service_ctl",
    parameters: { action: "logs", name: "ignored-logs-only" },
  },
  {
    name: "call_mcp_tool",
    parameters: {
      ToolName: "host_service_ctl",
      Arguments: { action: "restart", name: "parser", command: "python parse.py --fast", port: 8092 },
    },
  },
]);

assert.strictEqual(extracted.length, 2);
assert.strictEqual(extracted[0].name, "parser");
assert.strictEqual(extracted[0].runtime, "host");
assert.strictEqual(extracted[0].port, 8092);
assert.strictEqual(extracted[1].name, "api-srv");
assert.strictEqual(extracted[1].runtime, "docker");
assert.strictEqual(extracted[1].port, 8000);

assert.strictEqual(api.formatProcessDurationShort(18.2), "18с");
assert.strictEqual(api.formatProcessDurationShort(240), "4м");
assert.strictEqual(api.formatProcessDurationShort(3900), "1ч 5м");

const promptTxt = api.buildProcessErrorPromptText("parser", 3, "line1\nTraceback\nValueError: boom");
assert(promptTxt.includes("Процесс «parser» завершился с ошибкой"));
assert(promptTxt.includes("3"));
assert(promptTxt.includes("```text"));
assert(promptTxt.includes("ValueError: boom"));
console.log("NODE_VM_PROCESS_MONITOR_OK");
"""
        proc = subprocess.run(
            ["node", "-e", node_script],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=20,
            encoding="utf-8",
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Node VM check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        self.assertIn("NODE_VM_PROCESS_MONITOR_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
