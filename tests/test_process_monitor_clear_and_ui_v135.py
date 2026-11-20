# -*- coding: utf-8 -*-
"""v135: Fix syncSecurityModeUI ReferenceError, header counter sync, bulk inactive process clear API,
pinned modal log spoiler-dock, and compact #chatActiveProcessesBar capsule."""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_audit_fixes_v128 as isolated
import test_smart_hq_return_v115 as hq_iso

ROOT_DIR = Path(__file__).resolve().parents[1]


class ProcessMonitorClearAndUiV135Tests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(isolated.AuditFixesV128Tests.setUpClass.__func__)
    tearDownClass = classmethod(isolated.AuditFixesV128Tests.tearDownClass.__func__)
    asyncSetUp = isolated.AuditFixesV128Tests.asyncSetUp
    asyncTearDown = isolated.AuditFixesV128Tests.asyncTearDown
    _assert_live_db_untouched = isolated.AuditFixesV128Tests._assert_live_db_untouched

    def test_01_static_cache_css_and_dom_contracts_v135(self):
        static_dir = ROOT_DIR / "app" / "static"
        index_html = (static_dir / "index.html").read_text(encoding="utf-8")
        sw_js = (static_dir / "sw.js").read_text(encoding="utf-8")
        styles_css = (static_dir / "styles.css").read_text(encoding="utf-8")
        app_js = (static_dir / "app.js").read_text(encoding="utf-8")

        for fname in ("index.html", "sw.js", "styles.css", "app.js"):
            raw = (static_dir / fname).read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{fname} must not have UTF-8 BOM")

        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_js)
        self.assertIsNotNone(m_sw, "CACHE_NAME missing in sw.js")
        ver = int(m_sw.group(1))
        self.assertGreaterEqual(ver, 135)
        self.assertIn(f'/static/styles.css?v={ver}"', index_html)
        self.assertIn(f'/static/app.js?v={ver}"', index_html)
        self.assertIn(f'"/static/styles.css?v={ver}"', sw_js)
        self.assertIn(f'"/static/app.js?v={ver}"', sw_js)

        # Required new DOM elements in index.html
        self.assertIn('id="clearInactiveProcessesBtn"', index_html)
        self.assertIn('id="copyHostServiceLogsBtn"', index_html)
        self.assertIn('id="hostServiceLogsHead"', index_html)
        self.assertIn('class="chat-active-processes-wrap"', index_html)

        # Every call to syncSecurityModeUI() in app.js has a matching function definition
        self.assertIn("function syncSecurityModeUI()", app_js)
        calls = re.findall(r"\bsyncSecurityModeUI\s*\(", app_js)
        self.assertGreaterEqual(len(calls), 7)  # 1 definition + 6 call sites (+ test/new calls)

        # CSS braces balanced & v125 post-section hex-free invariant preserved
        css_no_comments = re.sub(r"/\*[\s\S]*?\*/", "", styles_css)
        self.assertEqual(css_no_comments.count("{"), css_no_comments.count("}"))
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", styles_css.split("/* v125:", 1)[1]))

        # All DOM classes rendered for capsule & modal logs exist in styles.css
        required_selectors = (
            ".chat-active-processes-wrap",
            ".chat-active-processes-bar",
            ".cap-bar-prefix",
            ".cap-prefix-dot",
            ".cap-chip",
            ".cap-chip-dot",
            ".cap-chip-title",
            ".cap-chip-meta",
            ".cap-chip-actions",
            ".cap-chip-btn",
            ".cap-more-btn",
            ".cap-clear-crashes-btn",
            ".host-services-modal-card",
            ".host-services-modal-body",
            ".proc-clear-inactive-btn",
            ".host-svc-card.is-log-open",
            ".hs-log-toggle-btn.is-active",
            ".host-service-logs-box",
            ".host-service-logs-head",
            ".host-service-logs-chevron",
            ".host-service-logs-actions",
            ".host-services-modal-body .host-service-logs-pre",
        )
        for sel in required_selectors:
            self.assertIn(sel, styles_css, f"Missing CSS selector in styles.css: {sel}")

        # Key layout properties from TZ #2
        self.assertIn("max-height: min(80vh, 700px)", styles_css)
        self.assertIn("min-height: 150px", styles_css)
        self.assertIn("max-height: 185px", styles_css)
        self.assertIn("width: fit-content", styles_css)

        chk = subprocess.run(
            ["node", "--check", str(static_dir / "app.js")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            **hq_iso._subprocess_hidden_kwargs(),
        )
        self.assertEqual(chk.returncode, 0, chk.stderr)

    def test_02_node_vm_syncSecurityModeUI_header_capsule_and_logs_spoiler(self):
        static_dir = ROOT_DIR / "app" / "static"
        tree = ast.parse(Path(hq_iso.__file__).read_text(encoding="utf-8"))
        harness = next(
            n.value.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "node_harness" for t in n.targets)
        )
        harness += r"""
function makeTrackableEl(initialHidden = false) {
  const classes = new Set(initialHidden ? ["hidden"] : []);
  const attrs = new Map();
  return {
    innerHTML: "",
    textContent: "",
    title: "",
    value: "read_only",
    options: [{ value: "read_only", textContent: "" }, { value: "full_access", textContent: "" }],
    disabled: false,
    scrollTop: 0,
    scrollHeight: 200,
    clientHeight: 185,
    dataset: {},
    style: { setProperty: () => {}, removeProperty: () => {} },
    classList: {
      add: (...cs) => cs.forEach((c) => c && classes.add(c)),
      remove: (...cs) => cs.forEach((c) => classes.delete(c)),
      toggle: (c, force) => {
        const shouldHave = force === undefined ? !classes.has(c) : Boolean(force);
        if (shouldHave) classes.add(c);
        else classes.delete(c);
        return shouldHave;
      },
      contains: (c) => classes.has(c),
    },
    setAttribute: (k, v) => attrs.set(k, String(v)),
    getAttribute: (k) => attrs.get(k) || "",
    querySelector: () => null,
    querySelectorAll: () => [],
  };
}

// Wire trackable elements into api.els
api.els.accessModeSelect = makeTrackableEl(false);
api.els.securityModeWrap = makeTrackableEl(false); // inProject = true
api.els.hostServicesBtn = makeTrackableEl(true);
api.els.hostServicesBtnDot = makeTrackableEl(false);
api.els.hostServicesBtnLabel = makeTrackableEl(false);
api.els.mobileHostServicesBtn = makeTrackableEl(true);
api.els.mobileHostServicesBtnDot = makeTrackableEl(false);
api.els.mobileHostServicesBtnLabel = makeTrackableEl(false);
api.els.chatActiveProcessesBar = makeTrackableEl(true);
api.els.hostServicesModal = makeTrackableEl(false);
api.els.hostServicesList = makeTrackableEl(false);
api.els.hostServiceLogsBox = makeTrackableEl(true);
api.els.hostServiceLogsHead = makeTrackableEl(false);
api.els.hostServiceLogsTitle = makeTrackableEl(false);
api.els.hostServiceLogsPre = makeTrackableEl(false);
api.els.hostServiceLiveTailBadge = makeTrackableEl(true);
api.els.clearInactiveProcessesBtn = makeTrackableEl(true);

api.state.currentUser = { id: "admin", role: "admin", max_access_mode: "host_full" };
api.state.selectedProject = "Alpha";
api.state.activeTab = "project";
api.state.activeChatId = "chat-1";
api.state.activeChat = { id: "chat-1", kind: "project", project_name: "Alpha" };
api.state.projects = [
  {
    name: "Alpha",
    execution_mode: "host",
    processes_summary: {
      total: 158,
      running_count: 0,
      crashed_count: 0,
      unseen_crash_count: 0,
      completed_count: 158,
      stopped_count: 0,
      ports: [],
    },
  },
];

// 1) syncSecurityModeUI() runs without ReferenceError and hides header pill when only completed scripts exist
assert.equal(typeof api.syncSecurityModeUI, "function");
api.syncSecurityModeUI();
assert.equal(
  api.els.hostServicesBtn.classList.contains("hidden"),
  true,
  "Header pill must hide when running === 0 and crashed === 0 (only completed scripts remain)"
);
assert.equal(api.els.mobileHostServicesBtn.classList.contains("hidden"), true);

// 2) Crashed state syncs .is-crashed onto BOTH desktop & mobile dots
api.state.projects[0].processes_summary = {
  total: 6,
  running_count: 0,
  crashed_count: 5,
  unseen_crash_count: 5,
  completed_count: 1,
  stopped_count: 0,
  ports: [],
};
api.syncSecurityModeUI();
assert.equal(api.els.hostServicesBtn.classList.contains("hidden"), false);
assert.equal(api.els.hostServicesBtn.classList.contains("is-crashed"), true);
assert.equal(
  api.els.hostServicesBtnDot.classList.contains("is-crashed"),
  true,
  "Desktop dot must receive .is-crashed class"
);
assert.equal(
  api.els.mobileHostServicesBtnDot.classList.contains("is-crashed"),
  true,
  "Mobile dot must receive .is-crashed class"
);
assert.equal(api.els.hostServicesBtnLabel.textContent, "5 сбой !");

// 3) Running state clears .is-crashed on dot and displays live count
api.state.projects[0].processes_summary = {
  total: 2,
  running_count: 1,
  crashed_count: 0,
  unseen_crash_count: 0,
  completed_count: 1,
  stopped_count: 0,
  ports: [8090],
};
api.syncSecurityModeUI();
assert.equal(api.els.hostServicesBtnDot.classList.contains("is-crashed"), false);
assert.equal(api.els.hostServicesBtnDot.classList.contains("is-stopped"), false);
assert.equal(api.els.hostServicesBtnLabel.textContent, "1 проц. · :8090");

// 4) #chatActiveProcessesBar capsule: max 2 chips + "+ ещё N" + "Скрыть все" for crashes
api.state.hostServices.loadedProjectsOnce.add("alpha");
api.state.hostServices.itemsByProject.set("alpha", [
  { name: "worker-1", status: "running", alive: true, ram_mb: 38.2, duration_sec: 12 },
  { name: "etl-job", status: "crashed", alive: false, exit_code: 1, crash_unseen: true },
  { name: "parser-2", status: "crashed", alive: false, exit_code: 2, crash_unseen: true },
  { name: "done-old", status: "completed", alive: false, exit_code: 0 },
]);
api.renderChatActiveProcessesBar();
assert.equal(api.els.chatActiveProcessesBar.classList.contains("hidden"), false);
const barHtml = api.els.chatActiveProcessesBar.innerHTML;
assert.ok(barHtml.includes("Сбой скрипта:"), "Must show crash context label");
assert.equal((barHtml.match(/class="cap-chip\s/g) || []).length, 2, "Must cap visible chips at 2");
assert.ok(barHtml.includes("+ ещё 1"), "Must render + ещё 1 overflow button");
assert.ok(barHtml.includes('data-cap-act="clear-crashes"'), "Must render Скрыть все action");
assert.ok(barHtml.includes("код 1"), "Must show readable exit code");

// 5) Logs spoiler toggle + clearInactiveProcessesFromModal in Node VM
(async () => {
  const fetchLog = [];
  sandbox.fetch = async (url, init) => {
    const u = String(url);
    const m = (init && init.method) || "GET";
    fetchLog.push(`${m} ${u}`);
    const hdrs = { get: (h) => (String(h).toLowerCase() === "content-type" ? "application/json" : null) };
    if (u.endsWith("/logs?tail_lines=120")) {
      return { ok: true, status: 200, headers: hdrs, json: async () => ({ ok: true, log_tail: "line 1\nline 2" }) };
    }
    if (u.endsWith("/processes/clear") && m === "POST") {
      return {
        ok: true,
        status: 200,
        headers: hdrs,
        json: async () => ({
          ok: true,
          cleared_count: 3,
          processes: [{ name: "worker-1", status: "running", alive: true, ram_mb: 40 }],
          summary: {
            total: 1,
            running_count: 1,
            crashed_count: 0,
            unseen_crash_count: 0,
            completed_count: 0,
            stopped_count: 0,
            ports: [],
          },
        }),
      };
    }
    return { ok: true, status: 200, headers: hdrs, json: async () => ({ ok: true }) };
  };

  api.state.hostServices.activeProject = "Alpha";
  api.state.hostServices.scope = "project";
  api.renderHostServicesModal();
  assert.equal(api.els.clearInactiveProcessesBtn.classList.contains("hidden"), false);
  assert.equal(api.els.clearInactiveProcessesBtn.textContent, "🧹 Очистить (3)");

  // Open log for completed script `done-old`
  await api.openHostServiceLogs("done-old", "Alpha", { toggleIfOpen: true });
  assert.equal(api.els.hostServiceLogsBox.classList.contains("hidden"), false);
  assert.equal(api.els.hostServiceLogsPre.textContent, "line 1\nline 2");

  // Spoiler header collapse / expand
  api.toggleHostServiceLogsCollapse();
  assert.equal(api.els.hostServiceLogsBox.classList.contains("is-collapsed"), true);
  // Clicking 📄 Логи while collapsed expands it first
  await api.openHostServiceLogs("done-old", "Alpha", { toggleIfOpen: true });
  assert.equal(api.els.hostServiceLogsBox.classList.contains("is-collapsed"), false);
  // Clicking 📄 Логи again while open hides it
  await api.openHostServiceLogs("done-old", "Alpha", { toggleIfOpen: true });
  assert.equal(api.els.hostServiceLogsBox.classList.contains("hidden"), true);

  // Re-open log on inactive service, then clear inactive -> log must auto-close & only running remains
  await api.openHostServiceLogs("done-old", "Alpha");
  assert.equal(api.els.hostServiceLogsBox.classList.contains("hidden"), false);
  await api.clearInactiveProcessesFromModal();
  assert.ok(fetchLog.includes("POST /api/projects/Alpha/processes/clear"));
  assert.equal(
    api.els.hostServiceLogsBox.classList.contains("hidden"),
    true,
    "Open log of cleared inactive service must close automatically"
  );
  assert.equal(api.state.hostServices.itemsByProject.get("alpha").length, 1);
  assert.equal(api.state.hostServices.itemsByProject.get("alpha")[0].name, "worker-1");
  assert.equal(api.els.clearInactiveProcessesBtn.classList.contains("hidden"), true);

  // Dismiss all crashes helper
  api.state.hostServices.itemsByProject.set("alpha", [
    { name: "err-a", status: "crashed", alive: false, exit_code: 1, crash_unseen: true },
    { name: "err-b", status: "crashed", alive: false, exit_code: 2, crash_unseen: true },
  ]);
  api.state.projects[0].processes_summary = {
    total: 2,
    running_count: 0,
    crashed_count: 2,
    unseen_crash_count: 2,
    completed_count: 0,
    stopped_count: 0,
    ports: [],
  };
  await api.dismissAllProjectCrashes("Alpha");
  assert.equal(api.els.chatActiveProcessesBar.classList.contains("hidden"), true);
  assert.equal(api.els.hostServicesBtn.classList.contains("hidden"), true);

  // 6) loadHostServicesForProject & loadAllProcessesGlobal complete end-to-end without syncSecurityModeUI error
  await api.loadHostServicesForProject("Alpha");
  await api.loadAllProcessesGlobal();

  console.log("V135_NODE_VM_OK");
  process.exit(0);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
        vm_script = self.tmp_root / "v135_vm.js"
        vm_script.write_text(harness, encoding="utf-8")
        vm_res = subprocess.run(
            ["node", str(vm_script), str(static_dir / "app.js")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=25,
            **hq_iso._subprocess_hidden_kwargs(),
        )
        self.assertEqual(vm_res.returncode, 0, f"STDOUT:\n{vm_res.stdout}\nSTDERR:\n{vm_res.stderr}")
        self.assertIn("V135_NODE_VM_OK", vm_res.stdout)

    async def test_03_clear_inactive_services_preserves_running_and_cleans_logs_api(self):
        from app import host_service_manager as hsm

        proj_alpha = self.root / "AlphaClear"
        proj_beta = self.root / "BetaClear"
        proj_alpha.mkdir(parents=True, exist_ok=True)
        proj_beta.mkdir(parents=True, exist_ok=True)
        (proj_alpha / "README.md").write_text("# Alpha\n", encoding="utf-8")
        (proj_beta / "README.md").write_text("# Beta\n", encoding="utf-8")

        mgr = hsm.HostServiceManager()
        scope_a = hsm._compute_scope_hash(proj_alpha.resolve())
        scope_b = hsm._compute_scope_hash(proj_beta.resolve())
        log_dir_a = mgr._root_dir() / f"AlphaClear_{scope_a}"
        log_dir_b = mgr._root_dir() / f"BetaClear_{scope_b}"
        log_dir_a.mkdir(parents=True, exist_ok=True)
        log_dir_b.mkdir(parents=True, exist_ok=True)

        live_pid = os.getpid()
        alive_now, live_ft, _ = hsm.get_process_fingerprint(live_pid)
        self.assertTrue(alive_now)

        live_log = log_dir_a / "live-srv.log"
        done_log = log_dir_a / "done-script.log"
        done_rot = log_dir_a / "done-script.log.1"
        crash_log = log_dir_a / "crash-job.log"
        stop_log = log_dir_b / "stopped-bot.log"
        for f in (live_log, done_log, done_rot, crash_log, stop_log):
            f.write_text("log data\n", encoding="utf-8")

        with mgr._lock:
            mgr._loaded_data_dir = self.config.DATA_DIR.resolve()
            mgr._services = {
                mgr._make_key("AlphaClear", "live-srv", proj_alpha): {
                    "project_name": "AlphaClear",
                    "project_dir": str(proj_alpha.resolve()),
                    "scope_hash": scope_a,
                    "name": "live-srv",
                    "command": "python server.py",
                    "workdir": str(proj_alpha.resolve()),
                    "pid": live_pid,
                    "create_time_ft": live_ft,
                    "status": "running",
                    "started_at": "2026-11-18T10:00:00+00:00",
                    "log_path": str(live_log),
                },
                mgr._make_key("AlphaClear", "done-script", proj_alpha): {
                    "project_name": "AlphaClear",
                    "project_dir": str(proj_alpha.resolve()),
                    "scope_hash": scope_a,
                    "name": "done-script",
                    "command": "python one_shot.py",
                    "workdir": str(proj_alpha.resolve()),
                    "pid": 0,
                    "status": "completed",
                    "exit_code": 0,
                    "started_at": "2026-11-18T09:00:00+00:00",
                    "finished_at": "2026-11-18T09:00:02+00:00",
                    "log_path": str(done_log),
                },
                mgr._make_key("AlphaClear", "crash-job", proj_alpha): {
                    "project_name": "AlphaClear",
                    "project_dir": str(proj_alpha.resolve()),
                    "scope_hash": scope_a,
                    "name": "crash-job",
                    "command": "python broken.py",
                    "workdir": str(proj_alpha.resolve()),
                    "pid": 0,
                    "status": "crashed",
                    "exit_code": 1,
                    "crash_unseen": True,
                    "started_at": "2026-11-18T09:05:00+00:00",
                    "finished_at": "2026-11-18T09:05:03+00:00",
                    "log_path": str(crash_log),
                },
                mgr._make_key("BetaClear", "stopped-bot", proj_beta): {
                    "project_name": "BetaClear",
                    "project_dir": str(proj_beta.resolve()),
                    "scope_hash": scope_b,
                    "name": "stopped-bot",
                    "command": "python bot.py",
                    "workdir": str(proj_beta.resolve()),
                    "pid": 0,
                    "status": "stopped",
                    "started_at": "2026-11-18T08:00:00+00:00",
                    "stopped_at": "2026-11-18T08:10:00+00:00",
                    "log_path": str(stop_log),
                },
            }
            mgr._save_locked()

        admin_user = {"id": "admin", "username": "admin", "role": "admin", "is_active": True}

        with (
            patch.object(hsm, "HOST_SERVICE_MANAGER", mgr),
            patch.object(
                self.main_mod, "broadcast_project_processes_update", new_callable=AsyncMock
            ) as mock_bcast,
            patch.object(
                self.main_mod, "check_docker_status", AsyncMock(return_value={"running": False})
            ),
        ):
            # 1) Project-scoped clear removes `done-script` + `crash-job`, keeps `live-srv` AND Beta's `stopped-bot`
            res_proj = await self.main_mod.api_clear_project_processes(
                "AlphaClear", current_user=admin_user
            )
            self.assertTrue(res_proj["ok"])
            self.assertEqual(res_proj["cleared_count"], 2)
            self.assertCountEqual(res_proj["cleared_names"], ["done-script", "crash-job"])
            self.assertEqual(res_proj["kept_running_count"], 1)
            self.assertEqual(len(res_proj["processes"]), 1)
            self.assertEqual(res_proj["processes"][0]["name"], "live-srv")
            self.assertTrue(res_proj["processes"][0]["alive"])
            self.assertEqual(res_proj["summary"]["running_count"], 1)
            self.assertEqual(res_proj["summary"]["crashed_count"], 0)
            self.assertTrue(live_log.exists(), "Live process log MUST NOT be deleted")
            self.assertFalse(done_log.exists(), "Completed process log must be deleted")
            self.assertFalse(done_rot.exists(), "Rotated .log.1 must be deleted")
            self.assertFalse(crash_log.exists(), "Crashed process log must be deleted")
            self.assertTrue(stop_log.exists(), "Other project's log must not be touched by single-project clear")
            mock_bcast.assert_awaited_once()

            # 2) Global clear (`POST /api/processes/clear`) cleans BetaClear's `stopped-bot` while still preserving `live-srv`
            mock_bcast.reset_mock()
            res_all = await self.main_mod.api_clear_all_processes(current_user=admin_user)
            self.assertTrue(res_all["ok"])
            self.assertEqual(res_all["cleared_count"], 1)
            self.assertEqual(res_all["cleared_by_project"], {"BetaClear": 1})
            self.assertEqual(len(res_all["processes"]), 1)
            self.assertEqual(res_all["processes"][0]["name"], "live-srv")
            self.assertEqual(res_all["global_summary"]["running_count"], 1)
            self.assertEqual(res_all["global_summary"]["total"], 1)
            self.assertFalse(stop_log.exists(), "Stopped process log in BetaClear must be deleted")
            self.assertTrue(live_log.exists(), "Live process log in AlphaClear must still exist")
            mock_bcast.assert_awaited_once()

        self._assert_live_db_untouched()

    async def test_04_sandbox_clear_inactive_services_preserves_running_in_warm_container(self):
        from app import sandbox_service as sbx

        sbx_proj = self.root / "DockerClearProj"
        sbx_proj.mkdir(parents=True, exist_ok=True)
        cname = sbx.build_sandbox_container_name("u1", sbx_proj)

        # Cold container: never executes docker exec
        cold_res = await sbx.clear_inactive_services(
            "u1", sbx_proj, "DockerClearProj", running_containers=set()
        )
        self.assertTrue(cold_res["ok"])
        self.assertEqual(cold_res["cleared_count"], 0)

        # Warm container: executes _DOCKER_CLEAR_INACTIVE_SERVICES_PY and reports cleared/kept
        exec_calls: list[list[str]] = []

        async def _fake_docker_short(args: list[str], timeout: float = 15.0):
            exec_calls.append(args)
            return (0, json.dumps({"cleared_names": ["etl-crashed", "job-done"], "kept_running_count": 1}), "")

        with patch.object(sbx, "_run_docker_short", side_effect=_fake_docker_short):
            warm_res = await sbx.clear_inactive_services(
                "u1", sbx_proj, "DockerClearProj", running_containers={cname}
            )
        self.assertTrue(warm_res["ok"])
        self.assertEqual(warm_res["cleared_count"], 2)
        self.assertEqual(warm_res["cleared_names"], ["etl-crashed", "job-done"])
        self.assertEqual(warm_res["kept_running_count"], 1)
        self.assertEqual(len(exec_calls), 1)
        self.assertIn(cname, exec_calls[0])


if __name__ == "__main__":
    unittest.main()
