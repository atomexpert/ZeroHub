"""
Isolated automated verification suite for Release v120:
Eliminates the vertical expand/shrink glitch (+5.46px -> -5.46px) when hovering over
expanded or collapsed project folders in the sidebar (`Antigravity_Hub` with `.has-badges`
as well as all other project and custom chat folders).

Root causes verified and locked against regression:
1. `.proj-inline-more-btn` (`⋮`) previously had `26x26px` while `.proj-inline-mem-btn` (`🧠`)
   and `.proj-inline-new-btn` (`+`) were `22x22px` on fine pointers (`24x24px` on coarse),
   and `.proj-tree-head-top` had no `min-height`. Toggling `.proj-tree-quick-btns` from
   `display: none` to `display: inline-flex` on `:hover` inflated `.proj-tree-head-top` from
   `20.54px` to `26.00px` (`+5.46px`), expanding `.proj-tree-header.has-badges` and the
   entire `.proj-tree-group.expanded` card, then shrinking it back as soon as the pointer
   moved into `.proj-tree-body` or left the header.
2. `.proj-tree-head-top` and `.proj-tree-head-right` now enforce `min-height: 24px` (exact
   integer device pixels at 100%, 125%, 150%, 175%, and 200% Windows DPI), while all three
   quick-action buttons (`.proj-inline-more-btn`, `.proj-inline-mem-btn`, `.proj-inline-new-btn`)
   share unified `22x22px` desktop (`24x24px` coarse) geometry and Studio hover styling.
3. `.proj-tree-group.menu-open > .proj-tree-header` keeps quick buttons visible and hides
   `.proj-count-idle` while the project context menu is open.

Invariants enforced:
- Live `data/hub.db` `(st_mtime_ns, st_size)` is strictly untouched.
- `0% GPU` (`CUDA_VISIBLE_DEVICES=""`, `ORT_DISABLE_ALL_GPU="1"`, `--headless=new --disable-gpu`).
- `0` popup consoles (`subprocess_hidden_kwargs()`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["ORT_DISABLE_ALL_GPU"] = "1"

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

STATIC_DIR = ROOT_DIR / "app" / "static"
STYLES_CSS_PATH = STATIC_DIR / "styles.css"
APP_JS_PATH = STATIC_DIR / "app.js"
INDEX_HTML_PATH = STATIC_DIR / "index.html"
SW_JS_PATH = STATIC_DIR / "sw.js"
LIVE_HUB_DB = ROOT_DIR / "data" / "hub.db"

try:
    from app.config import subprocess_hidden_kwargs
except Exception:  # pragma: no cover

    def subprocess_hidden_kwargs() -> dict:
        if os.name != "nt":
            return {}
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        return {
            "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            "startupinfo": si,
        }


def _snapshot_live_db() -> tuple[int, int] | None:
    if not LIVE_HUB_DB.exists():
        return None
    st = LIVE_HUB_DB.stat()
    return (st.st_mtime_ns, st.st_size)


def _strip_css_comments(css: str) -> str:
    return re.sub(r"/\*[\s\S]*?\*/", "", css)


def _find_browser() -> str | None:
    env_path = os.environ.get("AGY_TEST_BROWSER")
    if env_path and Path(env_path).exists():
        return env_path
    for candidate in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if Path(candidate).exists():
            return candidate
    for name in ("msedge", "chrome", "chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


class TestSidebarFolderHoverStabilityV120(unittest.TestCase):
    def setUp(self) -> None:
        self._db_before = _snapshot_live_db()

    def tearDown(self) -> None:
        if os.environ.get("HUB_ALLOW_EXTERNAL_LIVE_DB_ACTIVITY") != "1":
            self._db_after = _snapshot_live_db()
            self.assertEqual(
                self._db_before,
                self._db_after,
                "Live production data/hub.db (st_mtime_ns, st_size) MUST NOT be modified!",
            )

    def test_01_css_hover_lock_braces_and_workspace_invariant(self) -> None:
        raw_css = STYLES_CSS_PATH.read_text(encoding="utf-8")
        clean_css = _strip_css_comments(raw_css)

        self.assertEqual(
            clean_css.count("{"),
            clean_css.count("}"),
            "Unbalanced curly braces in styles.css",
        )
        self.assertRegex(
            clean_css,
            r"\.proj-tree-head-top\s*\{[^}]*min-height\s*:\s*24px",
        )
        self.assertRegex(
            clean_css,
            r"\.proj-tree-head-right\s*\{[^}]*min-height\s*:\s*24px",
        )
        self.assertIn(
            ".proj-tree-group.menu-open > .proj-tree-header .proj-tree-quick-btns",
            clean_css,
        )
        self.assertIn(
            ".proj-tree-group.menu-open > .proj-tree-header .proj-count-idle",
            clean_css,
        )
        self.assertNotRegex(
            clean_css,
            r"\.proj-inline-more-btn\s*\{[^}]*height\s*:\s*26px",
        )

        forbidden_props = (
            "transform",
            "filter",
            "backdrop-filter",
            "perspective",
            "contain",
            "container-type",
        )
        for m in re.finditer(r"main\.workspace\s*\{([^}]*)\}", clean_css, flags=re.DOTALL):
            block = m.group(1)
            for prop in forbidden_props:
                self.assertIsNone(
                    re.search(rf"(?<![\w-]){re.escape(prop)}\s*:", block),
                    f"Forbidden CSS property `{prop}` found inside `main.workspace`",
                )

    def test_02_js_syntax_utf8_no_bom_and_v120_cache_sync(self) -> None:
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "node binary must be available for syntax verification")
        for js_path in (APP_JS_PATH, SW_JS_PATH):
            res = subprocess.run(
                [str(node_bin), "--check", str(js_path)],
                capture_output=True,
                text=True,
                timeout=20,
                **subprocess_hidden_kwargs(),
            )
            self.assertEqual(
                res.returncode,
                0,
                f"node --check failed for {js_path.name}: {res.stderr}",
            )

        checked = (
            "app/static/index.html",
            "app/static/styles.css",
            "app/static/sw.js",
            "app/static/app.js",
            "tests/test_sidebar_folder_hover_stability_v120.py",
        )
        for rel in checked:
            raw = (ROOT_DIR / rel).read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"UTF-8 BOM found in {rel}")
            raw.decode("utf-8")

        html_txt = INDEX_HTML_PATH.read_text(encoding="utf-8")
        sw_txt = SW_JS_PATH.read_text(encoding="utf-8")

        m_sw_cache = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_txt)
        m_sw_css = re.search(r"/static/styles\.css\?v=(\d+)", sw_txt)
        m_sw_js = re.search(r"/static/app\.js\?v=(\d+)", sw_txt)
        m_html_css = re.search(r"/static/styles\.css\?v=(\d+)", html_txt)
        m_html_js = re.search(r"/static/app\.js\?v=(\d+)", html_txt)

        versions: list[int] = []
        for m in (m_sw_cache, m_sw_css, m_sw_js, m_html_css, m_html_js):
            self.assertIsNotNone(m)
            assert m is not None
            v_num = int(m.group(1))
            self.assertGreaterEqual(v_num, 120)
            versions.append(v_num)
        self.assertEqual(len(set(versions)), 1, f"Mismatched asset versions: {versions}")

    def test_03_headless_chromium_real_mouse_hover_zero_shift_across_dpr_and_themes(self) -> None:
        browser = _find_browser()
        if not browser:
            self.skipTest("No Edge/Chrome binary found for headless hover check")
        import websockets.sync.client as ws_client

        chats = [
            {"id": "t1", "kind": "project", "project_name": "Tulpar_VPN", "title": "ШТАБ", "icon": "🏠", "pinned": True, "is_generating": False, "updated_at": "2026-11-16T04:00:00Z"},
            {"id": "t2", "kind": "project", "project_name": "Tulpar_VPN", "title": "Вопрос-Ответ", "icon": "❓", "pinned": False, "is_generating": False, "updated_at": "2026-11-16T03:00:00Z"},
            {"id": "h1", "kind": "project", "project_name": "Antigravity_Hub", "title": "ШТАБ", "icon": "🏠", "pinned": True, "is_generating": True, "updated_at": "2026-11-16T04:50:00Z"},
            {"id": "h2", "kind": "project", "project_name": "Antigravity_Hub", "title": "Ядро", "icon": "🚀", "pinned": True, "is_generating": False, "updated_at": "2026-11-16T04:49:00Z"},
            {"id": "h5", "kind": "project", "project_name": "Antigravity_Hub", "title": "Визуальное оформление", "icon": "🎨", "pinned": False, "is_generating": False, "updated_at": "2026-11-16T04:46:00Z"},
            {"id": "p1", "kind": "project", "project_name": "Photo-1", "title": "Проверь ComfyUI", "icon": "💬", "pinned": True, "is_generating": True, "updated_at": "2026-11-15T12:00:00Z"},
            {"id": "r1", "kind": "chat", "project_name": "", "folder": "Заметки", "title": "Тестовая заметка", "icon": "💬", "pinned": False, "is_generating": False, "updated_at": "2026-11-15T11:00:00Z"},
        ]
        projects = [
            {"name": "Tulpar_VPN", "is_shared": True, "execution_mode": "docker", "hub_chat_count": 2},
            {
                "name": "Antigravity_Hub",
                "is_self_hub": True,
                "execution_mode": "host",
                "hub_chat_count": 3,
                "has_passport": True,
                "processes_summary": {"running_count": 2, "crashed_count": 0},
            },
            {"name": "AvitoParser", "execution_mode": "host", "hub_chat_count": 3},
            {"name": "Photo-1", "is_shared": True, "execution_mode": "docker", "hub_chat_count": 1},
            {"name": "avito_sniper_lab", "execution_mode": "host", "hub_chat_count": 0},
        ]

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send_json(self, obj: dict) -> None:
                raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                p = self.path.split("?", 1)[0]
                if p == "/":
                    raw = INDEX_HTML_PATH.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if p.startswith("/static/"):
                    f = STATIC_DIR / p[len("/static/") :]
                    if f.exists() and f.is_file():
                        ct = (
                            "text/javascript"
                            if f.suffix == ".js"
                            else "text/css"
                            if f.suffix == ".css"
                            else "image/svg+xml"
                        )
                        raw = f.read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", f"{ct}; charset=utf-8")
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                        return
                if p == "/api/system/info":
                    return self._send_json({"version": "120", "is_admin": True, "auth_enabled": False})
                if p == "/api/auth/me":
                    return self._send_json(
                        {"authenticated": True, "user": {"id": 1, "username": "admin", "role": "admin"}}
                    )
                if p == "/api/projects":
                    return self._send_json({"projects": projects})
                if p == "/api/chats":
                    return self._send_json({"chats": chats})
                if p.startswith("/api/chats/"):
                    cid = p.split("/")[3]
                    c = next((x for x in chats if x["id"] == cid), chats[4])
                    return self._send_json({"chat": c, "messages": []})
                if p == "/api/models":
                    return self._send_json(
                        {"models": [{"id": "gemini-4-carbon-high", "label": "Gemini 4 Carbon (High)"}]}
                    )
                return self._send_json({})

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            for dpr in (1.0, 1.25, 1.5):
                with tempfile.TemporaryDirectory(prefix=f"agy_v120_dpr{dpr}_") as tmp_dir:
                    proc = subprocess.Popen(
                        [
                            browser,
                            "--headless=new",
                            "--disable-gpu",
                            "--no-first-run",
                            "--no-default-browser-check",
                            "--disable-extensions",
                            f"--force-device-scale-factor={dpr}",
                            f"--user-data-dir={tmp_dir}",
                            "--window-size=1280,1300",
                            "--remote-debugging-port=0",
                            f"http://127.0.0.1:{port}/",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        **subprocess_hidden_kwargs(),
                    )
                    try:
                        devtools_file = Path(tmp_dir) / "DevToolsActivePort"
                        for _ in range(80):
                            if devtools_file.exists():
                                break
                            time.sleep(0.1)
                        self.assertTrue(devtools_file.exists(), "DevToolsActivePort not created")
                        dbg_port = int(devtools_file.read_text(encoding="utf-8").splitlines()[0].strip())
                        targets = json.loads(
                            urllib.request.urlopen(f"http://127.0.0.1:{dbg_port}/json/list")
                            .read()
                            .decode("utf-8")
                        )
                        ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page")

                        with ws_client.connect(ws_url) as ws:
                            msg_id = 0

                            def cdp(method: str, params: dict | None = None) -> dict:
                                nonlocal msg_id
                                msg_id += 1
                                cur = msg_id
                                ws.send(json.dumps({"id": cur, "method": method, "params": params or {}}))
                                while True:
                                    resp = json.loads(ws.recv())
                                    if resp.get("id") == cur:
                                        return resp.get("result", {})

                            def eval_js(expr: str):
                                res = cdp(
                                    "Runtime.evaluate",
                                    {"expression": expr, "returnByValue": True, "awaitPromise": True},
                                )
                                return res.get("result", {}).get("value")

                            for _ in range(60):
                                if (eval_js("document.querySelectorAll('.proj-tree-group').length") or 0) >= 5:
                                    break
                                time.sleep(0.1)

                            # Expand Tulpar_VPN, Antigravity_Hub, avito_sniper_lab; keep Photo-1 (with running badge) & AvitoParser collapsed
                            eval_js(
                                """
                                (async () => {
                                  for (const name of ['Tulpar_VPN', 'Antigravity_Hub', 'avito_sniper_lab']) {
                                    const g = document.querySelector(`.proj-tree-group[data-proj-group="${name}"]`);
                                    if (g && !g.classList.contains('expanded')) {
                                      g.querySelector('.proj-tree-header').click();
                                    }
                                  }
                                  const row = Array.from(document.querySelectorAll('.proj-tree-group[data-proj-group="Antigravity_Hub"] .nav-item'))
                                    .find(el => el.textContent.includes('Визуальное'));
                                  if (row) row.click();
                                  await document.fonts.ready;
                                  await new Promise(r => setTimeout(r, 250));
                                })()
                                """
                            )

                            snap_js = """
                              (() => {
                                const out = {};
                                document.querySelectorAll('.proj-tree-group, .proj-tree-header, .proj-tree-head-top, .proj-tree-badges-row, .folder-group, .folder-header-btn').forEach((el, idx) => {
                                  const r = el.getBoundingClientRect();
                                  const key = idx + ':' + el.className;
                                  out[key] = {
                                    x: Math.round(r.x * 100) / 100,
                                    y: Math.round(r.y * 100) / 100,
                                    w: Math.round(r.width * 100) / 100,
                                    h: Math.round(r.height * 100) / 100
                                  };
                                });
                                return out;
                              })()
                            """

                            for theme in ("light", "dark"):
                                for sb_w in (240, 272, 360):
                                    eval_js(
                                        f"""
                                        (() => {{
                                          document.documentElement.setAttribute('data-theme', '{theme}');
                                          const sb = document.getElementById('sidebar');
                                          sb.style.width = '{sb_w}px';
                                          sb.style.minWidth = '{sb_w}px';
                                          sb.style.maxWidth = '{sb_w}px';
                                          sb.offsetHeight;
                                        }})()
                                        """
                                    )
                                    cdp("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 900, "y": 400})
                                    time.sleep(0.03)
                                    baseline = eval_js(snap_js)

                                    hover_points = eval_js(
                                        """
                                        (() => {
                                          const pts = [];
                                          document.querySelectorAll('.proj-tree-group').forEach(g => {
                                            const name = g.dataset.projGroup;
                                            const hr = g.querySelector('.proj-tree-header').getBoundingClientRect();
                                            pts.push({ label: name + ':header', x: hr.left + 60, y: hr.top + 12 });
                                            const nav = g.querySelector('.nav-item');
                                            if (nav) {
                                              const nr = nav.getBoundingClientRect();
                                              pts.push({ label: name + ':body_row', x: nr.left + 60, y: nr.top + 12 });
                                            }
                                          });
                                          document.querySelectorAll('.folder-header-btn').forEach(fb => {
                                            const r = fb.getBoundingClientRect();
                                            pts.push({ label: 'folder:header', x: r.left + 60, y: r.top + 12 });
                                          });
                                          return pts;
                                        })()
                                        """
                                    )

                                    for pt in hover_points:
                                        cdp(
                                            "Input.dispatchMouseEvent",
                                            {"type": "mouseMoved", "x": pt["x"], "y": pt["y"]},
                                        )
                                        time.sleep(0.025)
                                        cur = eval_js(snap_js)
                                        for k, v in cur.items():
                                            b = baseline[k]
                                            for axis in ("x", "y", "w", "h"):
                                                delta = abs(v[axis] - b[axis])
                                                self.assertLessEqual(
                                                    delta,
                                                    0.05,
                                                    f"Hover shift @ dpr={dpr}, theme={theme}, width={sb_w}px, "
                                                    f"hover={pt['label']} on {k} axis={axis}: {b} -> {v}",
                                                )

                                    # Verify all 3 quick buttons render cleanly at identical 22x22 size on Antigravity_Hub hover
                                    hub_pt = next(p for p in hover_points if p["label"] == "Antigravity_Hub:header")
                                    cdp(
                                        "Input.dispatchMouseEvent",
                                        {"type": "mouseMoved", "x": hub_pt["x"], "y": hub_pt["y"]},
                                    )
                                    time.sleep(0.03)
                                    btn_metrics = eval_js(
                                        """
                                        (() => {
                                          const g = document.querySelector('.proj-tree-group[data-proj-group="Antigravity_Hub"]');
                                          const btns = Array.from(g.querySelectorAll('.proj-tree-quick-btns > button')).map(b => {
                                            const r = b.getBoundingClientRect();
                                            return { cls: b.className, w: Math.round(r.width * 10) / 10, h: Math.round(r.height * 10) / 10 };
                                          });
                                          const nr = g.querySelector('.proj-tree-name').getBoundingClientRect();
                                          const qr = g.querySelector('.proj-tree-quick-btns').getBoundingClientRect();
                                          const overlapX = Math.max(0, Math.min(nr.right, qr.right) - Math.max(nr.left, qr.left));
                                          return { btns, overlapX };
                                        })()
                                        """
                                    )
                                    self.assertEqual(len(btn_metrics["btns"]), 3, f"Expected 3 quick buttons: {btn_metrics}")
                                    self.assertEqual(btn_metrics["overlapX"], 0, f"Name overlapped quick btns: {btn_metrics}")
                                    heights = {b["h"] for b in btn_metrics["btns"]}
                                    self.assertEqual(len(heights), 1, f"Mismatched quick button heights: {btn_metrics}")
                    finally:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            proc.kill()
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
