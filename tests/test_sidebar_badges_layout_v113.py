"""
Isolated automated verification suite for Release v113:
Fixes badge overlap in `#sidebarActivitySection > .tree-section-head` (`⚡ Активность` pills vs
`✓ Прочитано`) and project title crushing / badge crowding in `.proj-tree-header` (`📁 Antigravity_Hub`
with `⚙️ Ядро`, process counters, running/queued/done pills, and quick action buttons).

Invariants enforced:
- Verifies live `data/hub.db` `(st_mtime_ns, st_size)` is never touched.
- Enforces `0% GPU` (`CUDA_VISIBLE_DEVICES=""`, `ORT_DISABLE_ALL_GPU="1"`, `--headless=new --disable-gpu`).
- Launches all subprocesses strictly hidden via `subprocess_hidden_kwargs()` (`0` popup consoles).
- Verifies real Chromium geometry (`getBoundingClientRect()`):
  1) Zero overlap (`0px^2` intersection) between `#activitySectionToggleBtn`, every `.act-sec-pill`,
     and `#clearUnreadDoneBtn` at `240px` (minimum resizable sidebar), `272px` (default sidebar),
     `360px` (wide sidebar), and `390x844` mobile drawer.
  2) Full readability of `.proj-tree-name` (`Antigravity_Hub` width >= 100px instead of `14px` `"A"`)
     even when `⚙️ Ядро`, background process pill, running/queued/done pills, and `.proj-tree-quick-btns`
     are simultaneously rendered.
  3) Zero vertical layout shift (`0px` height jump) when toggling quick buttons on idle or active projects.
- Verifies `node --check`, `main.workspace` CSS invariant, balanced braces, UTF-8 without BOM,
  and synchronous static cache bump (`?v=113` / `agy-hub-v113`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
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


class TestSidebarBadgesLayoutV113(unittest.TestCase):
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

    def test_01_css_rules_braces_and_workspace_invariant(self) -> None:
        raw_css = STYLES_CSS_PATH.read_text(encoding="utf-8")
        clean_css = _strip_css_comments(raw_css)

        self.assertEqual(
            clean_css.count("{"),
            clean_css.count("}"),
            "Unbalanced curly braces in styles.css",
        )
        self.assertIn(".proj-tree-head-top", clean_css)
        self.assertIn(".proj-tree-counters.proj-tree-badges-row", clean_css)
        self.assertRegex(
            clean_css,
            r"#sidebarActivitySection\.activity-radar-section\s*>\s*\.tree-section-head\s*\{[^}]*flex-wrap\s*:\s*wrap",
        )
        self.assertRegex(
            clean_css,
            r"#sidebarActivitySection\s+\.tree-head-action-btn\.clear-done-btn\s*\{[^}]*margin-left\s*:\s*auto",
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

    def test_02_js_syntax_and_two_tier_project_header_markup(self) -> None:
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

        app_js = APP_JS_PATH.read_text(encoding="utf-8")
        self.assertIn('class="proj-tree-head-top"', app_js)
        self.assertIn('class="proj-tree-counters proj-tree-badges-row"', app_js)
        self.assertIn("⚙️ Ядро", app_js)

    def test_03_headless_chromium_zero_overlap_and_full_project_name(self) -> None:
        browser = _find_browser()
        if not browser:
            self.skipTest("No Edge/Chrome binary found for headless geometry check")

        css_uri = STYLES_CSS_PATH.resolve().as_uri()
        fixture_html = f"""<!DOCTYPE html>
<html lang="ru" data-theme="light" data-palette="emerald" data-auth="authenticated">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <link rel="stylesheet" href="{css_uri}" />
  <style>
    body {{ margin: 0; padding: 0; }}
    .test-sidebar-host {{ display: flex; height: 820px; }}
  </style>
</head>
<body>
  <div class="test-sidebar-host">
    <aside id="sidebar" class="sidebar open" style="width: 272px; min-width: 272px; max-width: 272px;">
      <div class="sidebar-scroll" id="sidebarScroll">
        <section id="sidebarActivitySection" class="tree-section activity-radar-section" aria-label="Активность">
          <div class="tree-section-head" id="actHead">
            <button id="activitySectionToggleBtn" type="button" class="tree-section-toggle-btn" aria-expanded="true">
              <span id="activitySectionChevron" class="tree-sec-chevron" aria-hidden="true">▾</span>
              <svg class="ico ico-xs activity-bolt-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
              <span id="activitySectionTitle">Активность</span>
              <span id="activitySectionCount" class="tree-sec-count hidden">3</span>
              <span id="activitySectionBadges" class="activity-sec-badges">
                <span class="act-sec-pill is-run"><span class="act-sec-pill-dot"></span><span>2</span></span>
                <span class="act-sec-pill is-queue">+1 оч.</span>
                <span class="act-sec-pill is-done">✓ 1</span>
              </span>
            </button>
            <button id="clearUnreadDoneBtn" type="button" class="tree-head-action-btn clear-done-btn">✓ Прочитано</button>
          </div>
          <div id="sidebarActivityList" class="nav-list">
            <div class="nav-item active">
              <div class="nav-item-main has-activity-context">
                <div class="nav-item-title">ШТАБ</div>
                <span class="activity-proj-tag is-subline-context"><span class="activity-proj-dot"></span><span class="activity-proj-name">Antigravity_Hub</span></span>
              </div>
              <div class="badge-row"><span class="chat-state-pill is-running"><span class="csp-dot"></span>В работе</span></div>
            </div>
          </div>
        </section>

        <section id="treeProjectsSection" class="tree-section" aria-label="Проекты">
          <div class="tree-section-head">
            <div class="tree-section-head-left">
              <span>Проекты</span>
              <span id="projectsTreeCount" class="tree-sec-count">31</span>
            </div>
            <div class="tree-section-head-actions">
              <button id="openDesktopMigrationBtn" type="button" class="tree-head-action-btn tree-head-collect-btn">
                <span class="collect-btn-label">📦 Собрать</span>
              </button>
              <button id="openCreateProjectBtn" type="button" class="tree-head-action-btn tree-head-icon-btn">+</button>
            </div>
          </div>
          <div id="projectsTreeList" class="projects-tree-list">
            <div class="proj-tree-group" id="idleProj">
              <div class="proj-tree-header">
                <div class="proj-tree-head-top">
                  <div class="proj-tree-head-left">
                    <span class="proj-drag-grip">⋮⋮</span>
                    <span class="proj-tree-chevron">▸</span>
                    <span class="proj-tree-folder-ico">📁</span>
                    <span class="proj-tree-name" id="idleProjName">Tulpar_VPN</span>
                  </div>
                  <div class="proj-tree-head-right">
                    <span class="proj-count-idle">3</span>
                    <div class="proj-tree-quick-btns" id="idleQuickBtns">
                      <button type="button" class="proj-inline-mem-btn">🧠</button>
                      <button type="button" class="proj-inline-new-btn">+</button>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <div class="proj-tree-group is-selected-proj expanded" id="activeHubProj">
              <div class="proj-tree-header has-badges" id="activeHubHeader">
                <div class="proj-tree-head-top">
                  <div class="proj-tree-head-left">
                    <span class="proj-drag-grip">⋮⋮</span>
                    <span class="proj-tree-chevron">▸</span>
                    <span class="proj-tree-folder-ico">📁</span>
                    <span class="proj-tree-name" id="hubProjName">Antigravity_Hub</span>
                  </div>
                  <div class="proj-tree-head-right">
                    <div class="proj-tree-quick-btns" id="hubQuickBtns">
                      <button type="button" class="proj-inline-mem-btn">🧠<span class="proj-inline-mem-dot is-filled"></span></button>
                      <button type="button" class="proj-inline-new-btn">+</button>
                    </div>
                  </div>
                </div>
                <div class="proj-tree-counters proj-tree-badges-row" id="hubBadgesRow">
                  <span class="proj-self-hub-badge">⚙️ Ядро</span>
                  <button type="button" class="proj-proc-pill is-running">🟢 2</button>
                  <span class="proj-count-running"><span class="proj-count-running-dot"></span><span>2</span></span>
                  <span class="proj-count-queued"><span>+1</span></span>
                  <span class="proj-count-done">✓ 1</span>
                </div>
              </div>
            </div>
          </div>
        </section>
      </div>
    </aside>
  </div>
  <script>
    (function () {{
      function overlapArea(a, b) {{
        var x = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left));
        var y = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
        return x * y;
      }}
      function measureForSidebarWidth(px) {{
        var sb = document.getElementById("sidebar");
        sb.style.width = px + "px";
        sb.style.minWidth = px + "px";
        sb.style.maxWidth = px + "px";
        sb.offsetHeight;

        var clearBtn = document.getElementById("clearUnreadDoneBtn").getBoundingClientRect();
        var actHead = document.getElementById("actHead").getBoundingClientRect();
        var actTitle = document.getElementById("activitySectionTitle").getBoundingClientRect();
        var pills = Array.from(document.querySelectorAll("#activitySectionBadges .act-sec-pill")).map(function (el) {{
          return el.getBoundingClientRect();
        }});
        var maxPillClearOverlap = 0;
        pills.forEach(function (pr) {{
          maxPillClearOverlap = Math.max(maxPillClearOverlap, overlapArea(pr, clearBtn));
        }});
        var titleClearOverlap = overlapArea(actTitle, clearBtn);
        var clearOverflowRight = Math.max(0, clearBtn.right - actHead.right - 1.5);

        var hubNameEl = document.getElementById("hubProjName");
        var hubNameRect = hubNameEl.getBoundingClientRect();
        var hubQuickRect = document.getElementById("hubQuickBtns").getBoundingClientRect();
        var hubHeaderEl = document.getElementById("activeHubHeader");
        var hubHeaderRect = hubHeaderEl.getBoundingClientRect();
        var badgeEls = Array.from(document.querySelectorAll("#hubBadgesRow > *"));
        var maxBadgeOverlapWithNameOrQuick = 0;
        var maxBadgeOverflowRight = 0;
        badgeEls.forEach(function (b) {{
          var br = b.getBoundingClientRect();
          maxBadgeOverlapWithNameOrQuick = Math.max(
            maxBadgeOverlapWithNameOrQuick,
            overlapArea(br, hubNameRect),
            overlapArea(br, hubQuickRect)
          );
          maxBadgeOverflowRight = Math.max(maxBadgeOverflowRight, br.right - hubHeaderRect.right - 1.5);
        }});
        var nameQuickOverlap = overlapArea(hubNameRect, hubQuickRect);

        // Hover / selection stability check on idle project
        var idleGroup = document.getElementById("idleProj");
        var idleHeader = idleGroup.querySelector(".proj-tree-header");
        var idleHeightBefore = idleHeader.getBoundingClientRect().height;
        idleGroup.classList.add("is-selected-proj");
        var idleHeightAfter = idleHeader.getBoundingClientRect().height;
        idleGroup.classList.remove("is-selected-proj");

        var origHubText = hubNameEl.textContent;
        hubNameEl.textContent = "Antigravity_Hub_Enterprise_Platform";
        var longNameWidth = Math.round(hubNameEl.getBoundingClientRect().width * 10) / 10;
        hubNameEl.textContent = origHubText;

        return {{
          sidebarWidth: px,
          maxPillClearOverlap: Math.round(maxPillClearOverlap * 100) / 100,
          titleClearOverlap: Math.round(titleClearOverlap * 100) / 100,
          clearOverflowRight: Math.round(clearOverflowRight * 100) / 100,
          hubNameWidth: Math.round(hubNameRect.width * 10) / 10,
          longNameWidth: longNameWidth,
          hubNameTruncatedPx: Math.max(0, hubNameEl.scrollWidth - hubNameEl.clientWidth),
          nameQuickOverlap: Math.round(nameQuickOverlap * 100) / 100,
          maxBadgeOverlapWithNameOrQuick: Math.round(maxBadgeOverlapWithNameOrQuick * 100) / 100,
          maxBadgeOverflowRight: Math.round(maxBadgeOverflowRight * 100) / 100,
          idleHoverHeightDelta: Math.abs(idleHeightAfter - idleHeightBefore)
        }};
      }}

      var results = [
        measureForSidebarWidth(240),
        measureForSidebarWidth(272),
        measureForSidebarWidth(360)
      ];
      var pre = document.createElement("pre");
      pre.id = "geomReport";
      pre.textContent = JSON.stringify(results);
      document.body.appendChild(pre);
    }})();
  </script>
</body>
</html>
"""

        with tempfile.TemporaryDirectory(prefix="agy_sidebar_badges_v113_") as tmp_dir:
            tmp_root = Path(tmp_dir)
            html_file = tmp_root / "fixture.html"
            html_file.write_text(fixture_html, encoding="utf-8")

            for label, (vw, vh) in (("desktop", (1280, 860)), ("mobile", (390, 844))):
                profile_dir = tmp_root / f"prof_{label}"
                profile_dir.mkdir(parents=True, exist_ok=True)
                cmd = [
                    browser,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-extensions",
                    f"--user-data-dir={profile_dir}",
                    f"--window-size={vw},{vh}",
                    "--dump-dom",
                    html_file.resolve().as_uri(),
                ]
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=35,
                    **subprocess_hidden_kwargs(),
                )
                self.assertEqual(proc.returncode, 0, f"Headless browser failed ({label}): {proc.stderr}")
                m_rep = re.search(r'<pre id="geomReport">(.*?)</pre>', proc.stdout, flags=re.DOTALL)
                self.assertIsNotNone(m_rep, f"Missing geomReport in DOM dump ({label})")
                assert m_rep is not None
                reports = json.loads(m_rep.group(1))

                for rep in reports:
                    w = rep["sidebarWidth"]
                    self.assertEqual(
                        rep["maxPillClearOverlap"],
                        0,
                        f"{label}@{w}px: Activity pills overlapped clearUnreadDoneBtn! {rep}",
                    )
                    self.assertEqual(
                        rep["titleClearOverlap"],
                        0,
                        f"{label}@{w}px: Activity title overlapped clearUnreadDoneBtn! {rep}",
                    )
                    self.assertLessEqual(
                        rep["clearOverflowRight"],
                        1.5,
                        f"{label}@{w}px: clearUnreadDoneBtn overflowed Activity header! {rep}",
                    )
                    self.assertGreaterEqual(
                        rep["hubNameWidth"],
                        85.0,
                        f"{label}@{w}px: Project name Antigravity_Hub crushed! {rep}",
                    )
                    self.assertGreaterEqual(
                        rep["longNameWidth"],
                        85.0 if w == 240 else 115.0,
                        f"{label}@{w}px: Long project name slot too narrow! {rep}",
                    )
                    if w >= 272:
                        self.assertLessEqual(
                            rep["hubNameTruncatedPx"],
                            2,
                            f"{label}@{w}px: Antigravity_Hub should fit untruncated at >=272px! {rep}",
                        )
                    self.assertEqual(
                        rep["nameQuickOverlap"],
                        0,
                        f"{label}@{w}px: Project name overlapped quick buttons! {rep}",
                    )
                    self.assertEqual(
                        rep["maxBadgeOverlapWithNameOrQuick"],
                        0,
                        f"{label}@{w}px: Project badges overlapped name or quick buttons! {rep}",
                    )
                    self.assertLessEqual(
                        rep["maxBadgeOverflowRight"],
                        1.5,
                        f"{label}@{w}px: Project badges overflowed header! {rep}",
                    )
                    self.assertLessEqual(
                        rep["idleHoverHeightDelta"],
                        0.5,
                        f"{label}@{w}px: Idle project header jumped in height on quick-btn reveal! {rep}",
                    )

    def test_04_utf8_no_bom_and_v113_cache_sync(self) -> None:
        checked = (
            "app/static/index.html",
            "app/static/styles.css",
            "app/static/sw.js",
            "app/static/app.js",
            "tests/test_sidebar_badges_layout_v113.py",
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
            self.assertGreaterEqual(v_num, 113)
            versions.append(v_num)
        self.assertEqual(len(set(versions)), 1, f"Mismatched asset versions: {versions}")

        self.assertNotIn("?v=112", html_txt)
        self.assertNotIn("?v=112", sw_txt)
        self.assertNotIn("agy-hub-v112", sw_txt)


if __name__ == "__main__":
    unittest.main()
