"""
Isolated automated verification suite for Release v99:
Fix vertical Flexbox collapse & column misalignment of `#tokenUsersWrap` and `#tokenModelsWrap`
in `#quotaModal`, enrich `get_token_usage_stats(include_by_user=True)` metadata (`is_active`,
`quota_reserve_pct`, deterministic sorting of all registered users), and wire one-click user
daily limit editing (`openUserLimitEditorFromQuota` -> `#usersAdminModal` -> `#ueDailyLimitInput`).

Invariants enforced:
- Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER modifies live `data/hub.db` or live folders.
- Verifies `(st_mtime_ns, st_size)` of live `data/hub.db` before and after every test.
- Enforces `0% GPU` (`CUDA_VISIBLE_DEVICES=""`, `ORT_DISABLE_ALL_GPU="1"`) and `CREATE_NO_WINDOW`.
- Verifies `main.workspace` CSS invariant and UTF-8 without BOM across all modified files.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("ORT_DISABLE_ALL_GPU", "1")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

LIVE_HUB_DB = ROOT_DIR / "data" / "hub.db"


def _snapshot_live_db() -> tuple[int, int] | None:
    if not LIVE_HUB_DB.exists():
        return None
    st = LIVE_HUB_DB.stat()
    return (st.st_mtime_ns, st.st_size)


def _strip_css_comments(css: str) -> str:
    return re.sub(r"/\*[\s\S]*?\*/", "", css)


def _extract_top_level_css_blocks(clean_css: str) -> list[tuple[str, str]]:
    """Return `(selector_header, declarations)` for top-level rules outside `@media` blocks."""
    blocks: list[tuple[str, str]] = []
    depth = 0
    buf: list[str] = []
    cur_header = ""
    in_at_rule = False

    i = 0
    n = len(clean_css)
    while i < n:
        ch = clean_css[i]
        if ch == "{":
            if depth == 0:
                cur_header = "".join(buf).strip()
                buf = []
                in_at_rule = cur_header.startswith("@")
            else:
                buf.append(ch)
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body = "".join(buf)
                buf = []
                if not in_at_rule:
                    blocks.append((cur_header, body))
                elif cur_header.rsplit(";", 1)[-1].strip() == "@layer legacy":
                    blocks.extend(_extract_top_level_css_blocks(body))
                cur_header = ""
                in_at_rule = False
            elif depth > 0:
                buf.append(ch)
        else:
            buf.append(ch)
        i += 1
    return blocks


def _hidden_subprocess_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return kwargs


class TestQuotaUsersTableV99(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._db_before = _snapshot_live_db()

    def tearDown(self) -> None:
        self._db_after = _snapshot_live_db()
        self.assertEqual(
            self._db_before,
            self._db_after,
            "Live production data/hub.db (st_mtime_ns, st_size) MUST NOT be modified by tests!",
        )

    async def test_by_user_includes_all_users_and_limit_metadata(self) -> None:
        from app import brain_reader, config, database

        with tempfile.TemporaryDirectory(prefix="agy_v99_quota_users_") as tmp_str:
            tmp_root = Path(tmp_str).resolve()
            hub_base = tmp_root / "Antigravity_Hub"
            data_dir = hub_base / "data"
            users_dir = hub_base / "users"
            projects_root = users_dir / "admin" / "projects"
            shared_dir = hub_base / "shared_projects"
            shared_uploads = shared_dir / "_uploads"
            shared_ctx = shared_dir / "_context"
            cli_homes = data_dir / "cli_homes"
            uploads_dir = data_dir / "uploads"
            db_path = data_dir / "hub.db"
            agy_cli_dir = tmp_root / "agy_cli"
            agy_app_dir = tmp_root / "agy_app"

            for d in (
                hub_base,
                hub_base / "app",
                data_dir,
                users_dir,
                projects_root,
                shared_dir,
                shared_uploads,
                shared_ctx,
                cli_homes,
                uploads_dir,
                agy_cli_dir,
                agy_app_dir,
            ):
                d.mkdir(parents=True, exist_ok=True)

            patch_specs = [
                (
                    config,
                    {
                        "BASE_DIR": hub_base,
                        "ENV_PATH": hub_base / ".env",
                        "DATA_DIR": data_dir,
                        "DB_PATH": db_path,
                        "USERS_DIR": users_dir,
                        "USER_WORKSPACES_DIR": users_dir,
                        "PROJECTS_ROOT": projects_root,
                        "SHARED_PROJECTS_DIR": shared_dir,
                        "SHARED_UPLOADS_DIR": shared_uploads,
                        "SHARED_CONTEXT_DIR": shared_ctx,
                        "CLI_HOMES_DIR": cli_homes,
                        "UPLOADS_DIR": uploads_dir,
                        "LEGACY_DATA_SHARED_PROJECTS_DIR": data_dir / "shared_projects",
                        "LEGACY_DATA_WORKSPACES_DIR": data_dir / "workspaces",
                        "LEGACY_DATA_UPLOADS_DIR": uploads_dir,
                        "AGY_CLI_DATA_DIR": agy_cli_dir,
                        "AGY_APP_DATA_DIR": agy_app_dir,
                    },
                ),
                (
                    database,
                    {
                        "DB_PATH": db_path,
                        "USERS_DIR": users_dir,
                        "USER_WORKSPACES_DIR": users_dir,
                        "PROJECTS_ROOT": projects_root,
                        "SHARED_PROJECTS_DIR": shared_dir,
                    },
                ),
                (
                    brain_reader,
                    {
                        "BASE_DIR": hub_base,
                        "USERS_DIR": users_dir,
                        "USER_WORKSPACES_DIR": users_dir,
                        "PROJECTS_ROOT": projects_root,
                        "SHARED_PROJECTS_DIR": shared_dir,
                    },
                ),
            ]

            patchers = [
                patch.object(mod, attr, val)
                for mod, mapping in patch_specs
                for attr, val in mapping.items()
                if hasattr(mod, attr)
            ]
            for p in patchers:
                p.start()
            try:
                await database.init_db()

                # 1. Active guest with shared Hub tokens + personal `codex:` tokens in 24h
                active_guest = await database.create_user(
                    username="alex_dev",
                    display_name="Алексей (Rust)",
                    password="dev_password_1",
                    daily_token_limit=300_000,
                    quota_reserve_pct=15,
                )
                active_chat = await database.create_chat(
                    owner_id=active_guest["id"],
                    title="Active Guest Chat",
                    model="gemini-4-carbon-high",
                )
                await database.add_message(
                    active_chat["id"],
                    user_id=active_guest["id"],
                    role="assistant",
                    content="Ответ общей модели Hub",
                    model="gemini-4-carbon-high",
                    usage={
                        "input_tokens": 200_000,
                        "output_tokens": 30_000,
                        "thinking_tokens": 20_000,
                        "cache_read_tokens": 0,
                        "total_tokens": 250_000,
                    },
                )
                await database.add_message(
                    active_chat["id"],
                    user_id=active_guest["id"],
                    role="assistant",
                    content="Ответ личной подписки Codex",
                    model="codex:gpt-6.1-sol-high",
                    usage={
                        "input_tokens": 100_000,
                        "output_tokens": 20_000,
                        "thinking_tokens": 0,
                        "cache_read_tokens": 0,
                        "total_tokens": 120_000,
                        "provider": "chatgpt_codex",
                    },
                )

                # 2. Brand-new guest with 0 messages and custom daily limit + reserve
                zero_guest = await database.create_user(
                    username="new_guest",
                    display_name="Мария (Новый гость)",
                    password="dev_password_2",
                    daily_token_limit=500_000,
                    quota_reserve_pct=20,
                )

                # 3. Blocked guest (is_active=0) with custom daily limit + reserve (even with prior tokens)
                blocked_guest = await database.create_user(
                    username="blocked_guest",
                    display_name="Иван (Заблокирован)",
                    password="dev_password_3",
                    daily_token_limit=150_000,
                    quota_reserve_pct=30,
                )
                blocked_chat = await database.create_chat(
                    owner_id=blocked_guest["id"],
                    title="Blocked Guest Chat",
                    model="gemini-3.8-flash-low",
                )
                await database.add_message(
                    blocked_chat["id"],
                    user_id=blocked_guest["id"],
                    role="assistant",
                    content="Старый ответ до блокировки",
                    model="gemini-3.8-flash-low",
                    usage={
                        "input_tokens": 40_000,
                        "output_tokens": 10_000,
                        "total_tokens": 50_000,
                    },
                )
                updated_blocked = await database.update_user(
                    blocked_guest["id"],
                    is_active=False,
                )
                self.assertIsNotNone(updated_blocked)
                assert updated_blocked is not None
                self.assertFalse(updated_blocked["is_active"])

                stats = await database.get_token_usage_stats(include_by_user=True)
                by_user = stats.get("by_user")
                self.assertIsInstance(by_user, list)
                assert isinstance(by_user, list)
                self.assertEqual(len(by_user), 4)

                # Deterministic order: admin first, active guests next (by 24h desc), blocked guest at bottom
                self.assertEqual(
                    [u["username"] for u in by_user],
                    ["admin", "alex_dev", "new_guest", "blocked_guest"],
                )

                by_uname = {u["username"]: u for u in by_user}

                adm = by_uname["admin"]
                self.assertEqual(adm["role"], "admin")
                self.assertTrue(adm["is_active"])
                self.assertFalse(adm["is_orphan"])
                self.assertEqual(adm["daily_token_limit"], 0)

                act = by_uname["alex_dev"]
                self.assertEqual(act["user_id"], active_guest["id"])
                self.assertEqual(act["display_name"], "Алексей (Rust)")
                self.assertEqual(act["role"], "user")
                self.assertTrue(act["is_active"])
                self.assertFalse(act["is_orphan"])
                self.assertEqual(act["daily_token_limit"], 300_000)
                self.assertEqual(act["quota_reserve_pct"], 15)
                self.assertEqual(act["shared_tokens_24h"], 250_000)
                self.assertEqual(act["tokens_24h"], 370_000)
                self.assertEqual(act["total_tokens"], 370_000)
                self.assertEqual(act["turns"], 2)

                zgu = by_uname["new_guest"]
                self.assertEqual(zgu["user_id"], zero_guest["id"])
                self.assertTrue(zgu["is_active"])
                self.assertFalse(zgu["is_orphan"])
                self.assertEqual(zgu["daily_token_limit"], 500_000)
                self.assertEqual(zgu["quota_reserve_pct"], 20)
                self.assertEqual(zgu["shared_tokens_24h"], 0)
                self.assertEqual(zgu["tokens_24h"], 0)
                self.assertEqual(zgu["total_tokens"], 0)
                self.assertEqual(zgu["turns"], 0)

                blk = by_uname["blocked_guest"]
                self.assertEqual(blk["user_id"], blocked_guest["id"])
                self.assertFalse(blk["is_active"])
                self.assertFalse(blk["is_orphan"])
                self.assertEqual(blk["daily_token_limit"], 150_000)
                self.assertEqual(blk["quota_reserve_pct"], 30)
                self.assertEqual(blk["shared_tokens_24h"], 50_000)
                self.assertEqual(blk["tokens_24h"], 50_000)
            finally:
                for p in reversed(patchers):
                    p.stop()

    def test_css_no_flex_shrink_collapse_and_workspace_invariants(self) -> None:
        raw_css = (ROOT_DIR / "app" / "static" / "styles.css").read_text(encoding="utf-8")
        clean_css = _strip_css_comments(raw_css)

        # Balanced braces guard
        self.assertEqual(
            clean_css.count("{"),
            clean_css.count("}"),
            "Unbalanced curly braces in styles.css",
        )

        top_blocks = _extract_top_level_css_blocks(clean_css)

        # 1. `.modal-body > *` must forbid vertical flex-shrink collapse
        modal_child_decls = [
            body
            for sel, body in top_blocks
            if ".modal-body > *" in [s.strip() for s in sel.split(",")]
        ]
        self.assertTrue(modal_child_decls, "Missing `.modal-body > *` rule in styles.css")
        self.assertRegex(
            "\n".join(modal_child_decls),
            r"flex-shrink\s*:\s*0\b",
            "`.modal-body > *` must include `flex-shrink: 0`",
        )

        # 2. Base `.modes-table-wrap` must have `flex-shrink: 0` and `overflow-x: auto`
        modes_wrap_decls = [
            body
            for sel, body in top_blocks
            if ".modes-table-wrap" in [s.strip() for s in sel.split(",")]
        ]
        self.assertTrue(modes_wrap_decls, "Missing top-level `.modes-table-wrap` rule")
        joined_modes_wrap = "\n".join(modes_wrap_decls)
        self.assertRegex(joined_modes_wrap, r"flex-shrink\s*:\s*0\b")
        self.assertRegex(joined_modes_wrap, r"overflow-x\s*:\s*auto\b")
        self.assertRegex(joined_modes_wrap, r"overflow-y\s*:\s*hidden\b")

        # 3. Table header nowrap, tabular-nums, and v99 user table classes
        self.assertRegex(
            clean_css,
            r"\.token-models-table\s+th\s*\{[^}]*white-space\s*:\s*nowrap",
        )
        self.assertRegex(
            clean_css,
            r"\.token-models-table\s+td\.col-center\s*\{[^}]*font-variant-numeric\s*:\s*tabular-nums",
        )
        for required_cls in (
            ".tu-user-cell",
            ".tu-user-login",
            ".tu-status-pill",
            ".tu-sub-note",
            ".tu-usage-warn",
            ".tu-usage-over",
            ".tu-limit-btn",
            ".tu-limit-admin",
        ):
            self.assertIn(required_cls, clean_css, f"Missing {required_cls} in styles.css")

        # 4. Strict CSS invariant: `main.workspace` must NEVER contain stacking/containing-block traps
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

    def test_frontend_markup_syntax_and_v99_sync(self) -> None:
        checked_files = (
            "app/database.py",
            "app/static/index.html",
            "app/static/styles.css",
            "app/static/app.js",
            "app/static/sw.js",
            "tests/test_quota_users_table_v99.py",
        )
        for rel in checked_files:
            p = ROOT_DIR / rel
            raw = p.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"UTF-8 BOM found in {rel}")
            raw.decode("utf-8")

        html_txt = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")
        sw_txt = (ROOT_DIR / "app" / "static" / "sw.js").read_text(encoding="utf-8")
        app_js = (ROOT_DIR / "app" / "static" / "app.js").read_text(encoding="utf-8")

        # Markup presence & ordering inside #quotaModal
        for needle in (
            'id="tokenUsersHead"',
            'id="tokenUsersCountBadge"',
            'id="openUsersLimitsFromQuotaBtn"',
            'id="tokenUsersWrap"',
            "token-users-table",
            'id="tokenModelsHead"',
            'id="tokenModelsWrap"',
        ):
            self.assertIn(needle, html_txt, f"Missing `{needle}` in index.html")

        self.assertLess(html_txt.index('id="tokenUsersHead"'), html_txt.index('id="tokenUsersWrap"'))
        self.assertLess(html_txt.index('id="tokenUsersWrap"'), html_txt.index('id="tokenModelsHead"'))
        self.assertLess(html_txt.index('id="tokenModelsHead"'), html_txt.index('id="tokenModelsWrap"'))

        # JS syntax check (`node --check`) with hidden window & 0% GPU
        env = os.environ.copy()
        env["CUDA_VISIBLE_DEVICES"] = ""
        env["ORT_DISABLE_ALL_GPU"] = "1"
        for js_rel in ("app/static/app.js", "app/static/sw.js"):
            proc = subprocess.run(
                ["node", "--check", str(ROOT_DIR / js_rel)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=20,
                **_hidden_subprocess_kwargs(),
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"node --check failed for {js_rel}:\n{proc.stderr}",
            )

        # Verify app.js v99 logic hooks
        self.assertIn("function openUserLimitEditorFromQuota(", app_js)
        self.assertIn("data-edit-user-limit", app_js)
        self.assertIn("tu-status-pill is-blocked", app_js)
        self.assertIn("tu-limit-btn", app_js)
        self.assertIn("tu-limit-admin", app_js)
        self.assertIn("ueDailyLimitInput.select()", app_js)

        # Synchronized version >= 99 across index.html and sw.js, and exact match between them
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
            self.assertGreaterEqual(v_num, 99)
            versions.append(v_num)
        self.assertEqual(len(set(versions)), 1, f"Mismatched asset versions: {versions}")
        self.assertNotIn("?v=98", html_txt)
        self.assertNotIn("?v=98", sw_txt)
        self.assertNotIn("agy-hub-v98", sw_txt)

    def test_dom_render_quota_modal_and_limit_editor_handoff_in_node(self) -> None:
        """Execute real `renderQuotaModal`, `openUserEditor`, and `openUserLimitEditorFromQuota` on 16 users in Node."""
        app_js = (ROOT_DIR / "app" / "static" / "app.js").read_text(encoding="utf-8")

        start_rqm = app_js.index("  function renderQuotaModal() {")
        end_rqm = app_js.index("  // ===================== PERSONAL OPENROUTER BRIDGE")
        rqm_slice = app_js[start_rqm:end_rqm]

        start_oue = app_js.index("  function openUserEditor(usr = null, opts = {}) {")
        end_oue = app_js.index("  async function saveUserFromEditor() {")
        oue_slice = app_js[start_oue:end_oue]

        with tempfile.TemporaryDirectory(prefix="agy_v99_node_dom_") as tmp_dir:
            harness = Path(tmp_dir) / "v99_dom_harness.js"
            harness.write_text(
                f"""
const assert = require("assert");

function makeClassList(initial = []) {{
  const s = new Set(initial);
  return {{
    add: (...c) => c.forEach((x) => s.add(x)),
    remove: (...c) => c.forEach((x) => s.delete(x)),
    toggle: (c, force) => {{
      if (force === undefined) {{
        if (s.has(c)) {{ s.delete(c); return false; }}
        s.add(c);
        return true;
      }}
      if (force) s.add(c); else s.delete(c);
      return Boolean(force);
    }},
    contains: (c) => s.has(c),
  }};
}}

let activeFocusedId = null;
let selectedId = null;
let scrolledIds = [];

function makeEl(id, hidden = false) {{
  return {{
    id,
    textContent: "",
    innerHTML: "",
    value: "",
    disabled: false,
    placeholder: "",
    dataset: {{}},
    classList: makeClassList(hidden ? ["hidden"] : []),
    focus() {{ activeFocusedId = id; }},
    select() {{ selectedId = id; }},
    scrollIntoView() {{ scrolledIds.push(id); }},
  }};
}}

const els = {{
  quotaModal: makeEl("quotaModal", false),
  qmAdminAccountCard: makeEl("qmAdminAccountCard", true),
  qmAvatar: makeEl("qmAvatar"),
  qmAccountEmail: makeEl("qmAccountEmail"),
  qmAccountPlanTag: makeEl("qmAccountPlanTag", true),
  qmTierSub: makeEl("qmTierSub"),
  toggleAccountEditBtn: makeEl("toggleAccountEditBtn"),
  savedProfilesBar: makeEl("savedProfilesBar", true),
  qmUpdatedTime: makeEl("qmUpdatedTime"),
  quotaGroupsGrid: makeEl("quotaGroupsGrid"),
  qmActiveChatTokens: makeEl("qmActiveChatTokens", true),
  tokenSummaryGrid: makeEl("tokenSummaryGrid"),
  tokenUsersHead: makeEl("tokenUsersHead", true),
  tokenUsersCountBadge: makeEl("tokenUsersCountBadge"),
  openUsersLimitsFromQuotaBtn: makeEl("openUsersLimitsFromQuotaBtn"),
  tokenUsersWrap: makeEl("tokenUsersWrap", true),
  tokenUsersBody: makeEl("tokenUsersBody"),
  tokenModelsHead: makeEl("tokenModelsHead", true),
  tokenModelsWrap: makeEl("tokenModelsWrap", true),
  tokenModelsBody: makeEl("tokenModelsBody"),
  usersAdminModal: makeEl("usersAdminModal", true),
  userEditorBox: makeEl("userEditorBox", true),
  ueUserIdInput: makeEl("ueUserIdInput"),
  userEditorHeading: makeEl("userEditorHeading"),
  ueUsernameInput: makeEl("ueUsernameInput"),
  ueDisplayNameInput: makeEl("ueDisplayNameInput"),
  uePasswordInput: makeEl("uePasswordInput"),
  uePasswordLabel: makeEl("uePasswordLabel"),
  ueMaxModeSelect: makeEl("ueMaxModeSelect"),
  ueSandboxNetSelect: makeEl("ueSandboxNetSelect"),
  ueDailyLimitInput: makeEl("ueDailyLimitInput"),
  ueReservePctInput: makeEl("ueReservePctInput"),
}};

const byUser = [
  {{ user_id: "admin", username: "admin", display_name: "Администратор", role: "admin", is_active: true, daily_token_limit: 0, quota_reserve_pct: 0, turns: 12, shared_tokens_24h: 420000, tokens_24h: 420000, total_tokens: 2500000 }},
  {{ user_id: "u-over", username: "over_guest", display_name: "Over Guest", role: "user", is_active: true, daily_token_limit: 200000, quota_reserve_pct: 25, turns: 8, shared_tokens_24h: 210000, tokens_24h: 738000, total_tokens: 900000 }},
  {{ user_id: "u-warn", username: "warn_guest", display_name: "Warn Guest", role: "user", is_active: true, daily_token_limit: 500000, quota_reserve_pct: 20, turns: 5, shared_tokens_24h: 420000, tokens_24h: 420000, total_tokens: 420000 }},
];
for (let i = 1; i <= 11; i++) {{
  byUser.push({{
    user_id: `u-zero-${{i}}`,
    username: `guest_${{i}}`,
    display_name: `Гость ${{i}}`,
    role: "user",
    is_active: true,
    daily_token_limit: i % 2 === 0 ? 300000 : 0,
    quota_reserve_pct: 15,
    turns: 0,
    shared_tokens_24h: 0,
    tokens_24h: 0,
    total_tokens: 0,
  }});
}}
byUser.push({{
  user_id: "u-blocked",
  username: "blocked_dev",
  display_name: "Заблокированный",
  role: "user",
  is_active: false,
  daily_token_limit: 150000,
  quota_reserve_pct: 30,
  turns: 2,
  shared_tokens_24h: 10000,
  tokens_24h: 10000,
  total_tokens: 50000,
}});
byUser.push({{
  user_id: "u-orphan",
  username: "deleted_old",
  display_name: "deleted_old",
  role: "user",
  is_active: true,
  is_orphan: true,
  daily_token_limit: 0,
  quota_reserve_pct: 15,
  turns: 1,
  shared_tokens_24h: 0,
  tokens_24h: 0,
  total_tokens: 12000,
}});

const state = {{
  accountProfile: null,
  accountProfiles: [],
  quotas: {{ groups: [] }},
  activeChatId: null,
  tokenStats: {{
    by_user: byUser,
    by_model: [
      {{ model: "gemini-4-carbon-high", turns: 10, input_tokens: 300000, cache_read_tokens: 120000, output_tokens: 45000, thinking_tokens: 18000, tokens_24h: 483000, total_tokens: 483000 }},
    ],
  }},
  adminOverview: {{
    users: byUser.filter((u) => !u.is_orphan).map((u) => ({{
      id: u.user_id,
      username: u.username,
      display_name: u.display_name,
      role: u.role,
      is_active: u.is_active,
      daily_token_limit: u.daily_token_limit,
      quota_reserve_pct: u.quota_reserve_pct,
      max_access_mode: "docker_full",
      sandbox_network: "internet",
      allowed_models: [],
    }})),
  }},
}};

function isAdminUser() {{ return true; }}
function escapeHtml(s) {{ return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }}
function formatShortDate() {{ return "08:30"; }}
function formatResetCountdown() {{ return ""; }}
function formatCompactTokens(n) {{
  const v = Number(n || 0);
  return v >= 1000 ? `${{(v / 1000).toFixed(0)}}k` : String(v);
}}
function formatFullTokens(n) {{ return String(Number(n || 0)); }}
function resolveModelDisplayName(m) {{ return m; }}
function renderOpenRouterEntryBadges() {{}}
function renderAgyPool() {{}}
function renderAgyOAuthFlowUI() {{}}
function syncProviderHubNavBars() {{}}
function syncUserEditorSandboxNetUI() {{}}
function renderUserEditorModelsChecklist() {{}}
async function openUsersAdminModal() {{
  els.usersAdminModal.classList.remove("hidden");
  els.userEditorBox.classList.add("hidden");
}}

{rqm_slice}
{oue_slice}

(async () => {{
  renderQuotaModal();
  assert.strictEqual(els.tokenUsersHead.classList.contains("hidden"), false);
  assert.strictEqual(els.tokenUsersWrap.classList.contains("hidden"), false);
  assert.strictEqual(els.tokenUsersCountBadge.textContent, "Всего: 16");
  const trCount = (els.tokenUsersBody.innerHTML.match(/<tr\\b/g) || []).length;
  assert.strictEqual(trCount, 16, `Expected all 16 user rows, got ${{trCount}}`);
  assert.ok(!els.tokenUsersBody.innerHTML.includes("nav-item-sub"), "Must not reuse sidebar .nav-item-sub inside table");
  assert.ok(els.tokenUsersBody.innerHTML.includes("tu-usage-over"));
  assert.ok(els.tokenUsersBody.innerHTML.includes("tu-usage-warn"));
  assert.ok(els.tokenUsersBody.innerHTML.includes("⛔ Заблокирован"));
  assert.ok(els.tokenUsersBody.innerHTML.includes("is-blocked-row"));
  assert.ok(els.tokenUsersBody.innerHTML.includes('data-edit-user-limit="u-warn"'));
  assert.ok(els.tokenUsersBody.innerHTML.includes("резерв 20%"));
  assert.strictEqual(els.tokenModelsHead.classList.contains("hidden"), false);
  assert.ok(els.tokenModelsBody.innerHTML.includes("+120k кэш"));
  assert.ok(els.tokenModelsBody.innerHTML.includes("+18k мысли"));

  await openUserLimitEditorFromQuota("u-warn");
  await new Promise((r) => setTimeout(r, 65));
  assert.strictEqual(els.quotaModal.classList.contains("hidden"), true);
  assert.strictEqual(els.usersAdminModal.classList.contains("hidden"), false);
  assert.strictEqual(els.userEditorBox.classList.contains("hidden"), false);
  assert.strictEqual(Number(els.ueDailyLimitInput.value), 500000);
  assert.strictEqual(Number(els.ueReservePctInput.value), 20);
  assert.ok(scrolledIds.includes("userEditorBox"));
  assert.strictEqual(activeFocusedId, "ueDailyLimitInput");
  assert.strictEqual(selectedId, "ueDailyLimitInput");
  console.log("NODE_V99_DOM_OK");
}})().catch((err) => {{
  console.error(err);
  process.exit(1);
}});
""",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["CUDA_VISIBLE_DEVICES"] = ""
            env["ORT_DISABLE_ALL_GPU"] = "1"
            proc = subprocess.run(
                ["node", str(harness)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=15,
                **_hidden_subprocess_kwargs(),
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"Node v99 DOM test failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
            )
            self.assertIn("NODE_V99_DOM_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
