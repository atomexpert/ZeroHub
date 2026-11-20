"""
Isolated automated verification suite for Release v104:
Fixes vertical Flexbox collapsing of OpenRouter call logs (`#orLogsList` / `.or-log-item`)
and table/section spacing in `#openrouterModal` (`#orPaneTelemetry`).

Invariants enforced:
- Verifies live `data/hub.db` `(st_mtime_ns, st_size)` is never touched.
- Enforces `0% GPU` (`CUDA_VISIBLE_DEVICES=""`, `ORT_DISABLE_ALL_GPU="1"`, `--headless=new --disable-gpu`).
- Launches all subprocesses strictly hidden via `subprocess_hidden_kwargs()` (`0` popup consoles).
- Verifies real Chromium Flexbox geometry (`getBoundingClientRect()`, `scrollHeight > clientHeight`,
  zero vertical squashing with 35 log entries + expanded accordion item) on Desktop (`1280x860`)
  and Mobile (`390x844`).
- Verifies `node --check`, `main.workspace` CSS invariant, balanced braces, UTF-8 without BOM,
  and synchronous static cache bump (`?v=104` / `agy-hub-v104`).
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


class TestOpenRouterLogsLayoutV104(unittest.TestCase):
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

    def test_01_css_anti_squashing_and_telemetry_pane_spacing(self) -> None:
        raw_css = STYLES_CSS_PATH.read_text(encoding="utf-8")
        clean_css = _strip_css_comments(raw_css)

        self.assertEqual(
            clean_css.count("{"),
            clean_css.count("}"),
            "Unbalanced curly braces in styles.css",
        )

        # 1. .or-logs-list scroll container & child flex-shrink: 0 protection
        m_list = re.search(r"\.or-logs-list\s*\{([^}]*)\}", clean_css)
        self.assertIsNotNone(m_list, ".or-logs-list rule missing")
        assert m_list is not None
        list_body = m_list.group(1)
        self.assertIn("overflow-y: auto", list_body)
        self.assertIn("flex-direction: column", list_body)

        self.assertRegex(
            clean_css,
            r"\.or-logs-list\s*>\s*\*[^{]*\{[^}]*flex-shrink\s*:\s*0",
            ".or-logs-list > * must enforce flex-shrink: 0",
        )

        # 2. .or-log-item must explicitly set flex-shrink: 0 and NOT set overflow: hidden
        m_item = re.search(r"(?<![\w-])\.or-log-item\s*\{([^}]*)\}", clean_css)
        self.assertIsNotNone(m_item, ".or-log-item rule missing")
        assert m_item is not None
        item_body = m_item.group(1)
        self.assertRegex(item_body, r"flex-shrink\s*:\s*0")
        self.assertNotRegex(
            item_body,
            r"overflow\s*:\s*hidden",
            ".or-log-item must not set overflow: hidden (which forces Flexbox min-height: 0)",
        )

        # 3. .or-log-summary min-height & .or-log-details > * flex-shrink: 0
        self.assertRegex(
            clean_css,
            r"\.or-log-summary\s*\{[^}]*min-height\s*:\s*38px",
        )
        self.assertRegex(
            clean_css,
            r"\.or-log-details\s*>\s*\*\s*\{[^}]*flex-shrink\s*:\s*0",
        )
        self.assertRegex(
            clean_css,
            r"\.or-log-item\.is-expanded\s+\.or-log-chevron\s*\{[^}]*rotate\(180deg\)",
        )

        # 4. Models table dead bottom strip eliminated (.or-models-table margin-bottom: 0, wrapper margin-bottom: 14px)
        self.assertRegex(
            clean_css,
            r"\.or-models-table\s*\{[^}]*margin-bottom\s*:\s*0\b",
        )
        self.assertRegex(
            clean_css,
            r"#orUsedModelsWrap\s*,\s*#orUsedModelsEmpty\s*\{[^}]*margin-bottom\s*:\s*14px",
        )
        self.assertRegex(
            clean_css,
            r"#orPaneTelemetry\s*>\s*\.qm-section-head\s*\{[^}]*margin-bottom\s*:\s*8px",
        )

        # 5. Strict CSS invariant on main.workspace
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

    def test_02_js_syntax_scroll_preservation_and_aria_contracts(self) -> None:
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
        self.assertIn("const prevLogsScrollTop = els.orLogsList.scrollTop || 0;", app_js)
        self.assertIn("els.orLogsList.scrollTop = prevLogsScrollTop;", app_js)
        self.assertIn('class="or-log-chevron"', app_js)
        self.assertIn('aria-expanded="${expanded ? "true" : "false"}"', app_js)

    def test_03_headless_chromium_real_geometry_desktop_and_mobile(self) -> None:
        browser = _find_browser()
        if not browser:
            self.skipTest("No Edge/Chrome binary found for headless geometry check")

        rows_html: list[str] = []
        for idx in range(1, 36):
            expanded = idx == 3
            st_cls = "is-danger" if idx % 7 == 0 else "is-ok"
            st_txt = "Ошибка" if idx % 7 == 0 else "OK"
            details_html = (
                """
                <div class="or-log-details">
                  <div class="or-log-meta-grid">
                    <div class="or-log-meta-cell"><span>Generation ID</span><code>gen-1763188000-xyz</code></div>
                    <div class="or-log-meta-cell"><span>Апстрим-провайдер</span><code>OpenAI</code></div>
                    <div class="or-log-meta-cell"><span>Токены</span><code>4 120 / 0 / 820 / 0</code></div>
                    <div class="or-log-meta-cell"><span>Точная стоимость</span><code>$0.014820</code></div>
                  </div>
                  <div>
                    <div class="or-log-preview-label">Ответ модели (превью):</div>
                    <div class="or-log-preview-box">Готово! Проверены инварианты Flexbox и скролл контейнера логов.</div>
                  </div>
                </div>
                """
                if expanded
                else ""
            )
            rows_html.append(
                f"""
                <div class="or-log-item{" is-expanded" if expanded else ""}" data-or-log-id="{idx}">
                  <button type="button" class="or-log-summary" data-or-toggle-log="{idx}" aria-expanded="{"true" if expanded else "false"}">
                    <span class="or-log-left">
                      <span class="or-mini-pill {st_cls}">{st_txt}</span>
                      <span class="or-log-purpose-badge">🏷 Заголовок</span>
                      <span class="or-log-model">anthropic/claude-sonnet-5.5-20261022</span>
                      <span class="or-log-chat-tag is-link">📁 Antigravity_Hub · Визуальное оформление</span>
                    </span>
                    <span class="or-log-right">
                      <span>14.8k tok</span>
                      <span>4.4с</span>
                      <span class="or-log-cost">$0.045</span>
                      <span class="or-log-time">15 нояб., 12:40</span>
                      <svg class="or-log-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
                    </span>
                  </button>
                  {details_html}
                </div>
                """
            )

        css_uri = STYLES_CSS_PATH.resolve().as_uri()
        fixture_html = f"""<!DOCTYPE html>
<html lang="ru" data-theme="light" data-palette="emerald" data-auth="authenticated">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <link rel="stylesheet" href="{css_uri}" />
</head>
<body>
  <div id="openrouterModal" class="modal-backdrop">
    <div class="modal-card modal-wide or-modal-card">
      <div class="modal-header">
        <div class="or-modal-title-wrap">
          <h3>Персональный OpenRouter</h3>
          <span class="or-mini-pill is-ok">MCP активен</span>
        </div>
      </div>
      <div class="modal-body">
        <div id="orPaneTelemetry" class="or-tab-pane">
          <div class="qm-section-head">
            <span class="settings-section-title">Использованные модели OpenRouter</span>
            <span class="qm-updated-time">Моделей: 2</span>
          </div>
          <div id="orUsedModelsWrap" class="modes-table-wrap">
            <table class="modes-cheat-table token-models-table or-models-table">
              <thead>
                <tr>
                  <th>Модель и провайдер</th>
                  <th class="col-center">Вызовов</th>
                  <th class="col-center">Стоимость ($)</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td data-label="Модель"><strong><code>openai/gpt-4.1</code></strong><span class="or-model-provider-sub">Провайдер: OpenAI</span></td>
                  <td class="col-center" data-label="Вызовов">5</td>
                  <td class="col-center" data-label="Стоимость"><strong>$0.045</strong></td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="qm-section-head or-logs-head">
            <div>
              <span class="settings-section-title">Последние вызовы (до 60 записей)</span>
              <span class="or-logs-subnote">нажмите на строку для просмотра провайдера, токенов и превью ответа</span>
            </div>
            <button type="button" id="orClearLogsBtn" class="btn-secondary btn-sm">Очистить логи</button>
          </div>
          <div id="orLogsList" class="or-logs-list">
            {"".join(rows_html)}
          </div>
        </div>
      </div>
    </div>
  </div>
  <script>
    (function () {{
      var list = document.getElementById("orLogsList");
      var items = Array.from(list.querySelectorAll(".or-log-item"));
      var collapsedHeights = items
        .filter(function (el) {{ return !el.classList.contains("is-expanded"); }})
        .map(function (el) {{ return el.getBoundingClientRect().height; }});
      var expandedEl = list.querySelector(".or-log-item.is-expanded");
      var wrap = document.getElementById("orUsedModelsWrap");
      var tbl = wrap.querySelector("table");
      var report = {{
        itemCount: items.length,
        minCollapsedHeight: Math.min.apply(null, collapsedHeights),
        maxCollapsedHeight: Math.max.apply(null, collapsedHeights),
        expandedHeight: expandedEl ? expandedEl.getBoundingClientRect().height : 0,
        listClientHeight: list.clientHeight,
        listScrollHeight: list.scrollHeight,
        listHorizOverflow: list.scrollWidth - list.clientWidth,
        tableBottomDeadGap: Math.round(wrap.getBoundingClientRect().bottom - tbl.getBoundingClientRect().bottom)
      }};
      var pre = document.createElement("pre");
      pre.id = "geomReport";
      pre.textContent = JSON.stringify(report);
      document.body.appendChild(pre);
    }})();
  </script>
</body>
</html>
"""

        with tempfile.TemporaryDirectory(prefix="agy_or_logs_v104_") as tmp_dir:
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
                rep = json.loads(m_rep.group(1))

                self.assertEqual(rep["itemCount"], 35)
                self.assertGreaterEqual(
                    rep["minCollapsedHeight"],
                    38.0,
                    f"{label}: log items squashed vertically! minCollapsedHeight={rep['minCollapsedHeight']}",
                )
                self.assertGreaterEqual(
                    rep["expandedHeight"],
                    140.0,
                    f"{label}: expanded log item collapsed! expandedHeight={rep['expandedHeight']}",
                )
                self.assertGreater(
                    rep["listScrollHeight"],
                    rep["listClientHeight"] + 400,
                    f"{label}: #orLogsList must scroll vertically instead of shrinking items! {rep}",
                )
                self.assertLessEqual(
                    rep["listHorizOverflow"],
                    2,
                    f"{label}: unexpected horizontal overflow inside #orLogsList: {rep['listHorizOverflow']}px",
                )
                if label == "desktop":
                    self.assertLessEqual(
                        rep["tableBottomDeadGap"],
                        3,
                        f"Unexpected dead gap inside #orUsedModelsWrap bottom border: {rep['tableBottomDeadGap']}px",
                    )

    def test_04_utf8_no_bom_and_v104_cache_sync(self) -> None:
        checked = (
            "app/static/index.html",
            "app/static/styles.css",
            "app/static/sw.js",
            "app/static/app.js",
            "tests/test_openrouter_logs_layout_v104.py",
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
            self.assertGreaterEqual(v_num, 104)
            versions.append(v_num)
        self.assertEqual(len(set(versions)), 1, f"Mismatched asset versions: {versions}")

        self.assertNotIn("?v=103", html_txt)
        self.assertNotIn("?v=103", sw_txt)
        self.assertNotIn("agy-hub-v103", sw_txt)


if __name__ == "__main__":
    unittest.main()
