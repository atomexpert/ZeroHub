"""
Isolated automated verification suite for Release v102:
Subtle architectural visual separation between expanded project/chat folders and adjacent
folders in the sidebar (`#projectsTreeList` & `#foldersContainer`) without altering existing
backgrounds, active row highlights, or Drag & Drop drop-line indicators.

Invariants enforced:
- Verifies live `data/hub.db` `(st_mtime_ns, st_size)` is never touched.
- Enforces `0% GPU` (`CUDA_VISIBLE_DEVICES=""`, `ORT_DISABLE_ALL_GPU="1"`).
- Verifies calm matte divider contrast across all 16 themes (`8 palettes × dark/light`).
- Verifies `main.workspace` CSS invariant, balanced braces, UTF-8 without BOM, and `v=102` sync.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("ORT_DISABLE_ALL_GPU", "1")

ROOT_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for p in (str(ROOT_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

LIVE_HUB_DB = ROOT_DIR / "data" / "hub.db"


def _snapshot_live_db() -> tuple[int, int] | None:
    if not LIVE_HUB_DB.exists():
        return None
    st = LIVE_HUB_DB.stat()
    return (st.st_mtime_ns, st.st_size)


def _strip_css_comments(css: str) -> str:
    return re.sub(r"/\*[\s\S]*?\*/", "", css)


class TestSidebarProjectSeparationV102(unittest.TestCase):
    def setUp(self) -> None:
        self._db_before = _snapshot_live_db()

    def tearDown(self) -> None:
        self._db_after = _snapshot_live_db()
        self.assertEqual(
            self._db_before,
            self._db_after,
            "Live production data/hub.db (st_mtime_ns, st_size) MUST NOT be modified!",
        )

    def test_01_css_separation_rules_dnd_safety_and_untouched_highlights(self) -> None:
        raw_css = (ROOT_DIR / "app" / "static" / "styles.css").read_text(encoding="utf-8")
        clean_css = _strip_css_comments(raw_css)

        self.assertEqual(
            clean_css.count("{"),
            clean_css.count("}"),
            "Unbalanced curly braces in styles.css",
        )

        # 1. Expanded project separation rules & Drag-and-Drop guard (:not(.proj-drop-below) / :not(.proj-drop-above))
        self.assertRegex(
            clean_css,
            r"\.proj-tree-group\.expanded\s*\+\s*\.proj-tree-group\s*\{[^}]*margin-top\s*:\s*9\.5px",
        )
        self.assertRegex(
            clean_css,
            r"\.proj-tree-group:not\(\.expanded\)\s*\+\s*\.proj-tree-group\.expanded\s*\{[^}]*margin-top\s*:\s*4px",
        )
        self.assertRegex(
            clean_css,
            r"\.proj-tree-group\.expanded:not\(\.proj-drop-below\)\s*\+\s*\.proj-tree-group:not\(\.proj-drop-above\)::before\s*\{[^}]*height\s*:\s*1px",
        )

        # 2. Expanded custom folder separation in #foldersContainer
        self.assertRegex(
            clean_css,
            r"\.folder-group:not\(\.collapsed\)\s*\+\s*\.folder-group::before\s*\{[^}]*height\s*:\s*1px",
        )

        # 3. Existing expanded folder background tint & active row highlights remain intact
        self.assertIn(
            "background: color-mix(in srgb, var(--text-primary) 2.2%, transparent);",
            clean_css,
        )
        self.assertIn(
            "background: color-mix(in srgb, var(--text-primary) 2.6%, transparent);",
            clean_css,
        )
        self.assertIn(
            ".proj-tree-group.has-active-chat.expanded > .proj-tree-header",
            clean_css,
        )

        # 4. Strict CSS invariant on main.workspace
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

    def test_02_divider_contrast_sweet_spot_across_all_16_themes(self) -> None:
        from test_visual_scroll_polish import (
            MODES,
            PALETTES,
            mix,
            parse_color,
            resolve_var,
            theme_vars,
            wcag_contrast,
        )

        for palette in PALETTES:
            for mode in MODES:
                v = theme_vars(palette, mode)
                sidebar = parse_color(resolve_var(v["--bg-sidebar"], v))
                b_sub = parse_color(resolve_var(v["--border-subtle"], v))
                b_str = parse_color(resolve_var(v["--border-strong"], v))
                weight = 0.22 if mode == "light" else 0.18
                divider_rgb = mix(b_str, b_sub, weight)
                cr = wcag_contrast(divider_rgb, sidebar)
                # Visible enough to prevent folder merging (>= 1.22), soft enough to stay calm Studio matte (<= 1.65)
                self.assertGreaterEqual(
                    cr,
                    1.22,
                    f"{palette}/{mode}: divider too faint against sidebar (contrast={cr:.2f})",
                )
                self.assertLessEqual(
                    cr,
                    1.65,
                    f"{palette}/{mode}: divider too harsh against sidebar (contrast={cr:.2f})",
                )

    def test_03_utf8_no_bom_and_v102_cache_sync(self) -> None:
        checked = (
            "app/static/index.html",
            "app/static/styles.css",
            "app/static/sw.js",
            "app/static/app.js",
            "tests/test_sidebar_project_separation_v102.py",
        )
        for rel in checked:
            raw = (ROOT_DIR / rel).read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"UTF-8 BOM found in {rel}")
            raw.decode("utf-8")

        html_txt = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")
        sw_txt = (ROOT_DIR / "app" / "static" / "sw.js").read_text(encoding="utf-8")

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
            self.assertGreaterEqual(v_num, 102)
            versions.append(v_num)
        self.assertEqual(len(set(versions)), 1, f"Mismatched asset versions: {versions}")


if __name__ == "__main__":
    unittest.main()
