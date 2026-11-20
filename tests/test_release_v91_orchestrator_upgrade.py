"""
Isolated regression test suite for Antigravity Hub Release v=91:
1. OpenRouter MCP v2 (server-side caller_family tracking via ACTIVE_RUNS, cross-vendor guardrail + explicit_user_choice,
   :batch & deprecated model filtering, outbound DLP secret scrubbing, 162s deadline, LRU/TTL retry cache,
   and query_openrouter -> openrouter_chat normalization).
2. Three-CLI parity & RBAC (.cmd -> cli.js unwrap across all _resolve_claude_details branches, 26000-char rules cap,
   sync_user_cli_skills, live web_search in Codex & WebSearch/WebFetch in Claude, admin-only read-only PROJECTS_ROOT,
   and strip_transient_turn_wrappers + anti-hallucination compaction prompt).
3. Model Advisor v2 & «↩ В Штаб» loop (family profiles, min(5h, 7d) bottleneck, spec_handoff scoring,
   vendor-diversified Top-3, honest score badges, and deterministic tool_calls fact summary).

Runs strictly inside `tempfile.TemporaryDirectory()` — never touches live `data/hub.db` or running processes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


class TestReleaseV91OrchestratorUpgrade(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.agy_engine as agy_engine
        import app.claude_engine as claude_engine
        import app.codex_engine as codex_engine
        import app.config as config
        import app.database as database
        import app.openrouter_service as or_svc

        self._live_db = ROOT_DIR / "data" / "hub.db"
        self._live_stat = (
            (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            if self._live_db.exists()
            else None
        )

        self._tmp = tempfile.TemporaryDirectory(
            prefix="agy_test_v91_release_", ignore_cleanup_errors=True
        )
        self.tmp = Path(self._tmp.name).resolve()
        for sub in ("cli_homes", "agy", "uploads", "projects_root", "claude_tools", "bin"):
            (self.tmp / sub).mkdir(parents=True, exist_ok=True)

        self._orig = {
            "cfg_db": config.DB_PATH,
            "db_db": database.DB_PATH,
            "cfg_cli": config.CLI_HOMES_DIR,
            "cfg_agy": config.AGY_CLI_DATA_DIR,
            "eng_agy": agy_engine.AGY_CLI_DATA_DIR,
            "cfg_up": config.UPLOADS_DIR,
            "eng_up": agy_engine.UPLOADS_DIR,
            "cdx_up": codex_engine.UPLOADS_DIR,
            "cld_up": claude_engine.UPLOADS_DIR,
            "cfg_proj": config.PROJECTS_ROOT,
            "cfg_claude_tools": config.CLAUDE_TOOLS_DIR,
            "active_runs": dict(agy_engine.ACTIVE_RUNS),
            "or_models_items": list(or_svc._MODELS_CACHE.get("items") or []),
            "or_models_by_id": dict(or_svc._MODELS_CACHE.get("by_id") or {}),
            "or_models_ts": float(or_svc._MODELS_CACHE.get("fetched_at") or 0.0),
            "env_claude_bin": os.environ.get("HUB_CLAUDE_BIN"),
            "env_appdata": os.environ.get("APPDATA"),
            "env_real_home": os.environ.get("HUB_REAL_HOME"),
        }

        config.DB_PATH = self.tmp / "test_v91.db"
        database.DB_PATH = config.DB_PATH
        config.CLI_HOMES_DIR = self.tmp / "cli_homes"
        config.AGY_CLI_DATA_DIR = self.tmp / "agy"
        agy_engine.AGY_CLI_DATA_DIR = self.tmp / "agy"
        config.UPLOADS_DIR = self.tmp / "uploads"
        agy_engine.UPLOADS_DIR = self.tmp / "uploads"
        codex_engine.UPLOADS_DIR = self.tmp / "uploads"
        claude_engine.UPLOADS_DIR = self.tmp / "uploads"
        config.PROJECTS_ROOT = self.tmp / "projects_root"
        config.CLAUDE_TOOLS_DIR = self.tmp / "claude_tools"
        config._CLAUDE_INFO_CACHE.clear()
        agy_engine.ACTIVE_RUNS.clear()
        or_svc._RECENT_OR_RESPONSES.clear()

        await database.init_db()

    async def asyncTearDown(self) -> None:
        import app.agy_engine as agy_engine
        import app.claude_engine as claude_engine
        import app.codex_engine as codex_engine
        import app.config as config
        import app.database as database
        import app.openrouter_service as or_svc
        from tests.test_chatgpt_codex_integration import _unlink_test_junctions

        agy_engine.ACTIVE_RUNS.clear()
        agy_engine.ACTIVE_RUNS.update(self._orig["active_runs"])
        or_svc._RECENT_OR_RESPONSES.clear()
        or_svc._MODELS_CACHE["items"] = self._orig["or_models_items"]
        or_svc._MODELS_CACHE["by_id"] = self._orig["or_models_by_id"]
        or_svc._MODELS_CACHE["fetched_at"] = self._orig["or_models_ts"]

        config.DB_PATH = self._orig["cfg_db"]
        database.DB_PATH = self._orig["db_db"]
        config.CLI_HOMES_DIR = self._orig["cfg_cli"]
        config.AGY_CLI_DATA_DIR = self._orig["cfg_agy"]
        agy_engine.AGY_CLI_DATA_DIR = self._orig["eng_agy"]
        config.UPLOADS_DIR = self._orig["cfg_up"]
        agy_engine.UPLOADS_DIR = self._orig["eng_up"]
        codex_engine.UPLOADS_DIR = self._orig["cdx_up"]
        claude_engine.UPLOADS_DIR = self._orig["cld_up"]
        config.PROJECTS_ROOT = self._orig["cfg_proj"]
        config.CLAUDE_TOOLS_DIR = self._orig["cfg_claude_tools"]
        config._CLAUDE_INFO_CACHE.clear()

        for env_key, orig_key in (
            ("HUB_CLAUDE_BIN", "env_claude_bin"),
            ("APPDATA", "env_appdata"),
            ("HUB_REAL_HOME", "env_real_home"),
        ):
            if self._orig[orig_key] is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = self._orig[orig_key]

        _unlink_test_junctions(self.tmp)
        self._tmp.cleanup()

        if self._live_stat is not None:
            self.assertEqual(
                self._live_stat,
                (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size),
                "Live production data/hub.db must remain 100% untouched!",
            )

    # ------------------------------------------------------------------
    # 1. Phase 1: OpenRouter MCP v2
    # ------------------------------------------------------------------
    async def test_01_openrouter_v2_guardrails_dlp_cache_and_legacy_rules(self) -> None:
        import time
        import app.agy_engine as agy_engine
        import app.database as database
        import app.openrouter_service as or_svc

        # 1a. Legacy rules normalization (DB + SKILL.md)
        raw_legacy = "Всегда вызывай query_openrouter(model='deepseek/deepseek-r1') перед финалом, но не путай с my_query_openrouter_helper."
        norm_rules = database.normalize_legacy_openrouter_rules(raw_legacy)
        self.assertIn("openrouter_chat(model=", norm_rules)
        self.assertNotIn(" query_openrouter(", f" {norm_rules}")
        self.assertIn("my_query_openrouter_helper", norm_rules)

        u = await database.create_user(
            username="or_tester",
            password="pw_1234",
            display_name="OR Tester",
            max_access_mode="write_files",
        )
        uid = u["id"]
        cfg = await database.upsert_user_openrouter_config(
            uid,
            new_api_key="\x73k-or-v1-liveSecretKey9876543210abcdef",
            is_enabled=True,
            default_model="",
            max_price_per_m=15.0,
            daily_spend_limit_usd=5.0,
            custom_rules="Для второго мнения используй query_openrouter.",
        )
        self.assertEqual(cfg["custom_rules"], "Для второго мнения используй openrouter_chat.")
        await or_svc.sync_user_openrouter_cli_files(uid)
        skill_path = (
            self.tmp / "cli_homes" / uid / ".gemini" / "config" / "skills" / "openrouter-bridge" / "SKILL.md"
        )
        skill_txt = skill_path.read_text(encoding="utf-8")
        self.assertIn("openrouter_chat", skill_txt)
        self.assertIn("explicit_user_choice", skill_txt)
        self.assertIn("162 с", skill_txt)
        self.assertNotIn("query_openrouter", skill_txt)

        # 1b. Deprecated & :batch detection and catalog filtering
        raw_catalog = [
            {
                "id": "openai/gpt-4o-mini-2024-07-18",
                "name": "GPT-4o Mini (2024-07-18)",
                "created": 1720000000,
                "context_length": 128000,
                "pricing": {"prompt": "0.00000015", "completion": "0.0000006"},
            },
            {
                "id": "anthropic/claude-3.5-sonnet",
                "name": "Claude 3.5 Sonnet",
                "created": 1730000000,
                "context_length": 200000,
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            },
            {
                "id": "google/gemini-4-carbon:batch",
                "name": "Gemini 4 Carbon Batch",
                "created": 1765000000,
                "context_length": 1000000,
                "pricing": {"prompt": "0.000001", "completion": "0.000004"},
            },
            {
                "id": "openai/gpt-6.1-sol",
                "name": "GPT-6.1 Sol",
                "created": 1768000000,
                "context_length": 272000,
                "pricing": {"prompt": "0.000002", "completion": "0.000008"},
            },
            {
                "id": "google/gemini-4-carbon",
                "name": "Gemini 4 Carbon",
                "created": 1768000000,
                "context_length": 1000000,
                "pricing": {"prompt": "0.00000125", "completion": "0.000005"},
            },
            {
                "id": "anthropic/claude-5.5-sonnet",
                "name": "Claude 5.5 Sonnet",
                "created": 1768000000,
                "context_length": 200000,
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            },
            {
                "id": "deepseek/deepseek-r2",
                "name": "DeepSeek R2",
                "created": 1768500000,
                "context_length": 164000,
                "pricing": {"prompt": "0.0000005", "completion": "0.000002"},
            },
        ]
        norm_models = [
            m for m in (or_svc._normalize_catalog_model(r) for r in raw_catalog) if m is not None
        ]
        or_svc._MODELS_CACHE["items"] = norm_models
        or_svc._MODELS_CACHE["by_id"] = {m["id"].lower(): m for m in norm_models}
        or_svc._MODELS_CACHE["fetched_at"] = time.monotonic()

        visible_search = await or_svc.search_openrouter_models()
        visible_ids = [m["id"] for m in visible_search]
        self.assertNotIn("openai/gpt-4o-mini-2024-07-18", visible_ids)
        self.assertNotIn("anthropic/claude-3.5-sonnet", visible_ids)
        self.assertNotIn("google/gemini-4-carbon:batch", visible_ids)
        self.assertIn("openai/gpt-6.1-sol", visible_ids)
        self.assertIn("google/gemini-4-carbon", visible_ids)
        self.assertIn("deepseek/deepseek-r2", visible_ids)

        # 1c. Server-side caller_family resolution via ACTIVE_RUNS (chat_id match + freshest fallback)
        agy_engine.register_chat_turn(
            "chat-gpt-1",
            user_id=uid,
            model="codex:gpt-6.1-sol@high",
            engine="codex",
        )
        agy_engine.register_chat_turn(
            "chat-gem-2",
            user_id=uid,
            model="gemini-4-carbon-high",
            engine="agy",
        )
        info_gpt = agy_engine.find_active_turn_for_user(uid, chat_id="chat-gpt-1")
        self.assertIsNotNone(info_gpt)
        self.assertEqual(info_gpt["caller_family"], "gpt")
        info_gem = agy_engine.find_active_turn_for_user(uid, chat_id="chat-gem-2")
        self.assertIsNotNone(info_gem)
        self.assertEqual(info_gem["caller_family"], "gemini")

        bundle = await database.get_user_openrouter_secret_bundle(uid)
        self.assertIsNotNone(bundle)
        http_calls: list[dict[str, Any]] = []

        def _fake_or_http(method: str, path: str, *, api_key: str = "", payload: Any = None, timeout: float = 30.0, deadline_mono: Any = None) -> tuple[int, Any]:
            http_calls.append({"method": method, "path": path, "body": payload})
            return (
                200,
                {
                    "id": "gen-v91-1",
                    "model": (payload or {}).get("model", "google/gemini-4-carbon"),
                    "choices": [{"message": {"content": "Кросс-вендорный вердикт ОК."}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70, "cost": 0.0002},
                },
            )

        with patch.object(or_svc, "_openrouter_http_sync", side_effect=_fake_or_http):
            # Same-vendor self-repeat (gpt -> openai/*) with explicit_user_choice=False -> BLOCKED at $0 / 0 HTTP calls!
            blocked_gpt = await or_svc.execute_openrouter_chat(
                bundle,
                model="openai/gpt-6.1-sol",
                prompt="Проверь код",
                explicit_user_choice=False,
                chat_id="chat-gpt-1",
            )
            self.assertFalse(blocked_gpt["ok"])
            self.assertEqual(len(http_calls), 0)
            self.assertIn("кросс-вендорного консилиума", blocked_gpt["error"])
            self.assertIn("google/gemini-4-carbon", blocked_gpt["error"])

            # Same-vendor self-repeat (gemini -> google/*) with explicit_user_choice=False -> BLOCKED at $0!
            blocked_gem = await or_svc.execute_openrouter_chat(
                bundle,
                model="google/gemini-4-carbon",
                prompt="Проверь код",
                explicit_user_choice=False,
                chat_id="chat-gem-2",
            )
            self.assertFalse(blocked_gem["ok"])
            self.assertEqual(len(http_calls), 0)

            # :batch model -> BLOCKED even with explicit_user_choice=True!
            blocked_batch = await or_svc.execute_openrouter_chat(
                bundle,
                model="google/gemini-4-carbon:batch",
                prompt="Проверь код",
                explicit_user_choice=True,
                chat_id="chat-gpt-1",
            )
            self.assertFalse(blocked_batch["ok"])
            self.assertEqual(len(http_calls), 0)
            self.assertIn(":batch", blocked_batch["error"])

            # Same-vendor WITH explicit_user_choice=True -> ALLOWED, BUT outbound DLP MUST STILL SCRUB SECRETS!
            secret_prompt = (
                "Ревью конфига: \x73k-or-v1-abcdef0123456789abcdef0123456789 "
                "и \x73k-ant-api03-secretClaudeKey1234567890ABCD "
                "и \x41IzaSyDa1234567890abcdefghijklmnopqrstuvw "
                "и HUB_SECRET_KEY=super_secret_hub_master_value_999"
            )
            allowed_explicit = await or_svc.execute_openrouter_chat(
                bundle,
                model="openai/gpt-6.1-sol",
                prompt=secret_prompt,
                explicit_user_choice=True,
                chat_id="chat-gpt-1",
            )
            self.assertTrue(allowed_explicit["ok"], allowed_explicit)
            self.assertEqual(len(http_calls), 1)
            sent_messages = http_calls[0]["body"]["messages"]
            sent_user_content = sent_messages[-1]["content"]
            self.assertIn("[REDACTED_SECRET]", sent_user_content)
            self.assertNotIn("sk-or-v1-abcdef", sent_user_content)
            self.assertNotIn("sk-ant-api03", sent_user_content)
            self.assertNotIn("AIzaSyDa123", sent_user_content)
            self.assertNotIn("super_secret_hub_master_value_999", sent_user_content)

            # Identical retry within 10 minutes -> served from _RECENT_OR_RESPONSES in 0 additional HTTP calls & $0.00!
            cached_retry = await or_svc.execute_openrouter_chat(
                bundle,
                model="openai/gpt-6.1-sol",
                prompt=secret_prompt,
                explicit_user_choice=True,
                chat_id="chat-gpt-1",
            )
            self.assertTrue(cached_retry["ok"])
            self.assertTrue(cached_retry.get("cached_retry"))
            self.assertEqual(cached_retry["cost_usd"], 0.0)
            self.assertEqual(cached_retry["latency_ms"], 0)
            self.assertEqual(len(http_calls), 1, "Retry cache must make 0 extra HTTP requests!")

    # ------------------------------------------------------------------
    # 2. Phase 2: Three-CLI Parity, Skills Sync, RBAC & Context Hygiene
    # ------------------------------------------------------------------
    async def test_02_three_cli_parity_rbac_and_context_hygiene(self) -> None:
        import json
        import app.agy_engine as agy_engine
        import app.claude_engine as claude_engine
        import app.codex_engine as codex_engine
        import app.config as config

        fake_node = str(self.tmp / "bin" / ("node.exe" if config.IS_WINDOWS else "node"))
        Path(fake_node).write_text("node", encoding="utf-8")

        # 2a. Every .cmd branch in _resolve_claude_details() unwraps to cli.js
        def _make_npm_layout(root: Path, bin_in_dot_bin: bool = False) -> tuple[Path, Path]:
            if bin_in_dot_bin:
                nm = root / "node_modules"
                shim = nm / ".bin" / "claude.cmd"
            else:
                nm = root / "node_modules"
                shim = root / "claude.cmd"
            cli_js = nm / "@anthropic-ai" / "claude-code" / "cli.js"
            cli_js.parent.mkdir(parents=True, exist_ok=True)
            shim.parent.mkdir(parents=True, exist_ok=True)
            cli_js.write_text("// cli.js\n", encoding="utf-8")
            shim.write_text("@ECHO OFF\r\n", encoding="utf-8")
            return shim, cli_js.resolve()

        with patch.object(config, "_find_node_bin", return_value=fake_node):
            # Branch 1: HUB_CLAUDE_BIN pointing directly to .cmd
            env_shim, env_js = _make_npm_layout(self.tmp / "env_direct")
            os.environ["HUB_CLAUDE_BIN"] = str(env_shim)
            p, js, src = config._resolve_claude_details()
            self.assertEqual((Path(p).resolve(), Path(js).resolve(), src), (env_js, env_js, "env"))

            # Branch 2: HUB_CLAUDE_BIN resolved via shutil.which -> .cmd
            which_shim, which_js = _make_npm_layout(self.tmp / "env_which")
            os.environ["HUB_CLAUDE_BIN"] = "custom-claude-cmd"
            with patch.object(shutil, "which", return_value=str(which_shim)):
                p, js, src = config._resolve_claude_details()
                self.assertEqual((Path(p).resolve(), Path(js).resolve(), src), (which_js, which_js, "env"))

            os.environ.pop("HUB_CLAUDE_BIN", None)
            os.environ["HUB_REAL_HOME"] = str(self.tmp / "empty_real_home")

            # Branch 3: local_shim in CLAUDE_TOOLS_DIR/node_modules/.bin/claude.cmd
            local_shim = config.CLAUDE_TOOLS_DIR / "node_modules" / ".bin" / ("claude.cmd" if config.IS_WINDOWS else "claude")
            local_shim.parent.mkdir(parents=True, exist_ok=True)
            local_shim.write_text("@ECHO OFF\r\n", encoding="utf-8")
            # When cli.js exists next to .bin, local_js or _unwrap_npm_shim_to_claude_js resolves cli.js
            local_js_target = config.CLAUDE_TOOLS_DIR / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
            local_js_target.parent.mkdir(parents=True, exist_ok=True)
            local_js_target.write_text("// local cli.js\n", encoding="utf-8")
            p, js, src = config._resolve_claude_details()
            self.assertEqual((Path(p).resolve(), Path(js).resolve(), src), (local_js_target.resolve(), local_js_target.resolve(), "local"))
            shutil.rmtree(config.CLAUDE_TOOLS_DIR, ignore_errors=True)
            config.CLAUDE_TOOLS_DIR.mkdir(parents=True, exist_ok=True)

            # Branch 4: APPDATA/npm/claude.cmd unwrap via lib/node_modules layout
            if config.IS_WINDOWS:
                fake_appdata = self.tmp / "fake_appdata"
                npm_dir = fake_appdata / "npm"
                lib_js = npm_dir / "lib" / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
                lib_js.parent.mkdir(parents=True, exist_ok=True)
                lib_js.write_text("// global npm lib cli.js\n", encoding="utf-8")
                appdata_shim = npm_dir / "claude.cmd"
                appdata_shim.write_text("@ECHO OFF\r\n", encoding="utf-8")
                os.environ["APPDATA"] = str(fake_appdata)
                p, js, src = config._resolve_claude_details()
                self.assertEqual((Path(p).resolve(), Path(js).resolve()), (lib_js.resolve(), lib_js.resolve()))
                os.environ.pop("APPDATA", None)

            # Branch 5: system PATH shutil.which("claude") -> .cmd
            sys_shim, sys_js = _make_npm_layout(self.tmp / "sys_npm", bin_in_dot_bin=True)
            os.environ["APPDATA"] = str(self.tmp / "empty_appdata")
            with patch.object(shutil, "which", return_value=str(sys_shim)):
                p, js, src = config._resolve_claude_details()
                self.assertEqual((Path(p).resolve(), Path(js).resolve(), src), (sys_js, sys_js, "system"))

        # 2b. 26000-char project rules cap in both Codex and Claude
        proj_dir = self.tmp / "projects_root" / "alpha_proj"
        rules_file = proj_dir / ".agents" / "rules" / "hub_project.md"
        rules_file.parent.mkdir(parents=True, exist_ok=True)
        rules_file.write_text("R" * 30_000, encoding="utf-8")
        self.assertEqual(len(codex_engine._read_project_hub_rules(proj_dir)), 26_000)
        self.assertEqual(len(claude_engine._read_project_hub_rules(proj_dir)), 26_000)

        # 2c. sync_user_cli_skills mirrors user skills and skips builtin skills
        builtin_skill = config.AGY_CLI_DATA_DIR / "builtin" / "skills" / "antigravity_guide" / "SKILL.md"
        builtin_skill.parent.mkdir(parents=True, exist_ok=True)
        builtin_skill.write_text("# Builtin\n", encoding="utf-8")

        home_u = config.ensure_user_cli_home("skill_user")
        user_skill_1 = home_u / ".gemini" / "config" / "skills" / "custom-arch" / "SKILL.md"
        user_skill_1.parent.mkdir(parents=True, exist_ok=True)
        user_skill_1.write_text("# Custom Arch Skill\n", encoding="utf-8")

        user_skill_2 = home_u / ".agents" / "skills" / "sql-helper" / "SKILL.md"
        user_skill_2.parent.mkdir(parents=True, exist_ok=True)
        user_skill_2.write_text("# SQL Helper Skill\n", encoding="utf-8")

        # Attempt to place a colliding builtin name in user folder — must be skipped
        collide_builtin = home_u / ".agents" / "skills" / "antigravity_guide" / "SKILL.md"
        collide_builtin.parent.mkdir(parents=True, exist_ok=True)
        collide_builtin.write_text("# Should Not Copy\n", encoding="utf-8")

        cdx_acc = config.ensure_user_codex_account_dir("skill_user", "acc_1")
        cld_acc = config.ensure_user_claude_account_dir("skill_user", "cld_1")
        config.sync_user_cli_rules("skill_user", "Мои правила.")

        for target_base in (
            home_u / ".codex" / "skills",
            cdx_acc / "skills",
            home_u / ".claude" / "skills",
            cld_acc / "skills",
        ):
            self.assertTrue((target_base / "custom-arch" / "SKILL.md").is_file(), str(target_base))
            self.assertTrue((target_base / "sql-helper" / "SKILL.md").is_file(), str(target_base))
            self.assertFalse((target_base / "antigravity_guide").exists(), str(target_base))

        # 2d. Web search config in Codex (TOML root key before [mcp_servers.*]) & Claude security matrix
        toml_live = codex_engine.render_codex_config_toml(
            sandbox_mode="workspace-write",
            shell_tool=False,
            network_access=True,
            openrouter_mcp_url="http://127.0.0.1:8765/mcp/openrouter/tok",
        )
        self.assertIn('web_search = "live"', toml_live)
        self.assertLess(toml_live.index('web_search = "live"'), toml_live.index("[features]"))
        self.assertLess(toml_live.index('web_search = "live"'), toml_live.index("[mcp_servers.hub_openrouter]"))

        toml_off = codex_engine.render_codex_config_toml(
            sandbox_mode="workspace-write",
            shell_tool=False,
            network_access=False,
        )
        self.assertIn('web_search = "disabled"', toml_off)

        cld_ro = claude_engine.resolve_claude_security_matrix(
            kind="project", access_mode="read_only", is_admin=False, sandbox_network="internet"
        )
        self.assertIn("WebSearch", cld_ro["allowed_tools"])
        self.assertIn("WebFetch", cld_ro["allowed_tools"])

        cld_wf = claude_engine.resolve_claude_security_matrix(
            kind="project", access_mode="write_files", is_admin=False, sandbox_network="internet"
        )
        self.assertIn("WebSearch", cld_wf["allowed_tools"])
        self.assertIn("WebFetch", cld_wf["allowed_tools"])

        cld_nonet = claude_engine.resolve_claude_security_matrix(
            kind="project", access_mode="write_files", is_admin=False, sandbox_network="none"
        )
        self.assertNotIn("WebSearch", cld_nonet["allowed_tools"])
        self.assertNotIn("WebFetch", cld_nonet["allowed_tools"])

        # 2e. Strict RBAC on PROJECTS_ROOT: Admin gets read-only access; non-admin NEVER gets PROJECTS_ROOT!
        pr_posix = config.PROJECTS_ROOT.resolve().as_posix().rstrip("/")
        p_posix = proj_dir.resolve().as_posix().rstrip("/")

        agy_admin_dkr = agy_engine._build_permission_args(
            "project",
            "write_files",
            is_admin=True,
            project_dir=proj_dir,
            force_no_host_shell=True,
        )
        adm_settings = json.loads(agy_admin_dkr[agy_admin_dkr.index("--settings") + 1])
        adm_allow = adm_settings["permissions"]["allow"]
        self.assertIn(f"read_file({pr_posix}/*)", adm_allow)
        self.assertNotIn(f"write_file({pr_posix}/*)", adm_allow)
        self.assertIn(f"write_file({p_posix}/*)", adm_allow)

        agy_guest = agy_engine._build_permission_args(
            "project",
            "write_files",
            is_admin=False,
            project_dir=proj_dir,
            force_no_host_shell=True,
        )
        guest_settings = json.loads(agy_guest[agy_guest.index("--settings") + 1])
        guest_allow = guest_settings["permissions"]["allow"]
        self.assertNotIn(f"read_file({pr_posix}/*)", guest_allow)
        self.assertNotIn(f"write_file({pr_posix}/*)", guest_allow)

        cdx_admin = codex_engine.resolve_codex_security_matrix(
            kind="project",
            access_mode="write_files",
            is_admin=True,
            workspace_dir=proj_dir,
        )
        self.assertEqual(cdx_admin["turn_sandbox_policy"]["writableRoots"], [str(proj_dir.resolve())])
        self.assertEqual(cdx_admin["extra_readable_roots"], [str(config.PROJECTS_ROOT.resolve())])

        cdx_guest = codex_engine.resolve_codex_security_matrix(
            kind="project",
            access_mode="write_files",
            is_admin=False,
            workspace_dir=proj_dir,
        )
        self.assertEqual(cdx_guest["extra_readable_roots"], [])

        # 2f. strip_transient_turn_wrappers removes stale <PROJECT_PULSE> from history recovery
        noisy_msg = (
            "<PROJECT_PULSE project=\"alpha\">\n"
            "🚦 СВЕТОФОР ФАЙЛОВ: НЕ ТРОГАТЬ app/main.py\n"
            "</PROJECT_PULSE>\n\n\n"
            "Пожалуйста, обнови роут в app/main.py."
        )
        cleaned_msg = agy_engine.strip_transient_turn_wrappers(noisy_msg)
        self.assertEqual(cleaned_msg, "Пожалуйста, обнови роут в app/main.py.")

        recovered = agy_engine._build_history_recovery_prompt(
            [
                {"id": 1, "role": "user", "status": "done", "content": noisy_msg},
                {"id": 2, "role": "assistant", "status": "done", "content": "Готово."},
                {"id": 3, "role": "user", "status": "streaming", "content": "Следующий шаг"},
            ],
            "Следующий шаг",
        )
        self.assertNotIn("<PROJECT_PULSE", recovered)
        self.assertNotIn("СВЕТОФОР ФАЙЛОВ", recovered)
        self.assertIn("Пожалуйста, обнови роут в app/main.py.", recovered)

    # ------------------------------------------------------------------
    # 3. Phase 3 & 4: Model Advisor v2 + «↩ В Штаб» loop (Node VM)
    # ------------------------------------------------------------------
    def test_03_advisor_v2_spec_handoff_and_return_to_hq_js(self) -> None:
        import app.config as config

        # Verify v>=92 synchronized bump in index.html & sw.js
        html_txt = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")
        sw_txt = (ROOT_DIR / "app" / "static" / "sw.js").read_text(encoding="utf-8")
        m_ver = re.search(r"/static/app\.js\?v=(\d+)", html_txt)
        self.assertIsNotNone(m_ver)
        ver_num = int(m_ver.group(1))
        self.assertGreaterEqual(ver_num, 92)
        self.assertIn(f"/static/styles.css?v={ver_num}", html_txt)
        self.assertIn(f'CACHE_NAME = "agy-hub-v{ver_num}"', sw_txt)
        self.assertIn(f"/static/styles.css?v={ver_num}", sw_txt)
        self.assertIn(f"/static/app.js?v={ver_num}", sw_txt)

        node_script = textwrap.dedent(
            r"""
            const assert = require("assert");
            const fs = require("fs");
            const path = require("path");
            const vm = require("vm");

            const rootDir = process.env.HUB_ROOT_DIR;
            const appCode = fs.readFileSync(path.join(rootDir, "app", "static", "app.js"), "utf8");

            const dummyClassList = () => ({
              add() {}, remove() {}, toggle() {}, contains: () => false,
            });
            const dummyEl = () => ({
              classList: dummyClassList(),
              addEventListener() {}, removeEventListener() {},
              querySelector: () => null, querySelectorAll: () => [],
              setAttribute() {}, getAttribute: () => null, appendChild() {},
              style: {}, dataset: {}, options: [], value: "", textContent: "", innerHTML: "",
            });
            const storageMap = new Map();
            const storage = {
              getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
              setItem: (k, v) => storageMap.set(k, String(v)),
              removeItem: (k) => storageMap.delete(k),
              clear: () => storageMap.clear(),
            };
            const doc = {
              documentElement: { getAttribute: () => "dark", setAttribute() {}, style: { setProperty() {}, getPropertyValue: () => "" } },
              body: dummyEl(),
              getElementById: () => dummyEl(),
              querySelector: () => null,
              querySelectorAll: () => [],
              createElement: () => dummyEl(),
              addEventListener() {},
            };
            const win = {
              __AGY_EXPORT_ADVISOR_FOR_TESTS__: true,
              __AGY_SKIP_AUTO_INIT__: true,
              document: doc,
              localStorage: storage,
              sessionStorage: storage,
              location: { protocol: "http:", host: "127.0.0.1:8765", pathname: "/", search: "", hash: "" },
              navigator: { userAgent: "NodeTest" },
              innerWidth: 1280,
              innerHeight: 800,
              setTimeout, clearTimeout,
              setInterval: () => 1, clearInterval: () => {},
              requestAnimationFrame: (cb) => setTimeout(cb, 0),
              cancelAnimationFrame: clearTimeout,
              matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
              addEventListener() {},
              CSS: { supports: () => true, escape: (s) => String(s) },
              URL, URLSearchParams,
            };
            win.window = win;
            win.globalThis = win;

            const ctx = vm.createContext(Object.assign({}, win, {
              window: win, globalThis: win, document: doc, console,
              Math, Date, JSON, Promise, Set, Map, WeakMap, AbortController,
              RegExp, Array, Object, String, Number, Boolean, Error, TypeError,
              encodeURIComponent, decodeURIComponent, parseInt, parseFloat, isNaN, isFinite, Intl,
            }));
            vm.runInContext(appCode, ctx, { filename: "app.js" });
            const api = win.__AGY_ADVISOR_API__;
            assert(api, "Advisor v2 API must be exported");

            // 1. Family profiles check (no uniform @xhigh=96)
            const pAstraX = api.buildModelAdvisorProfile("codex:gpt-6-astra@xhigh", "ChatGPT 6 Astra (@xhigh)");
            const pSolH = api.buildModelAdvisorProfile("codex:gpt-6.1-sol@high", "ChatGPT 6.1 Sol (@high)");
            const pTerraX = api.buildModelAdvisorProfile("codex:gpt-6.1-terra@xhigh", "ChatGPT 6.1 Terra (@xhigh)");
            const pLegacyX = api.buildModelAdvisorProfile("codex:gpt-5.3-codex@xhigh", "GPT-5.3 Codex (@xhigh)");
            const pCarbonH = api.buildModelAdvisorProfile("gemini-4-carbon-high", "Gemini 4 Carbon (High)");
            assert.strictEqual(pAstraX.capability, 97);
            assert.strictEqual(pAstraX.burnClass, 5);
            assert.strictEqual(pSolH.capability, 93);
            assert.strictEqual(pSolH.burnClass, 2);
            assert.strictEqual(pSolH.baseSeries, "gpt-6.1-sol");
            assert.strictEqual(pTerraX.capability, 91);
            assert.strictEqual(pLegacyX.capability, 89);
            assert.strictEqual(pLegacyX.isLegacyCodex, true);
            assert.strictEqual(pCarbonH.capability, 92);
            assert.strictEqual(pCarbonH.baseSeries, "gemini-4-carbon");

            // 2. Bottleneck min(5h, 7d) check in getAdvisorVendorQuotaSnapshot
            const snapNarrow7d = api.getAdvisorVendorQuotaSnapshot("codex", {
              chatgpt: {
                activeAccountId: "acc1",
                accounts: [{
                  id: "acc1",
                  is_active: true,
                  is_enabled: true,
                  quotas: { primary_5h: { remaining_pct: 88 }, secondary_7d: { remaining_pct: 14 } },
                }],
              },
            });
            assert.strictEqual(snapNarrow7d.pct5h, 88);
            assert.strictEqual(snapNarrow7d.pct7d, 14);
            assert.strictEqual(snapNarrow7d.effectivePct, 14);
            assert.strictEqual(snapNarrow7d.bottleneck, "7d");

            // 3. Benchmark: Gemini 94%, ChatGPT 78%, Claude 3% + heavy + executionContext: "spec_handoff"
            const pool = [
              { id: "gemini-4-carbon-high", label: "Gemini 4 Carbon (High)" },
              { id: "gemini-4-carbon-low", label: "Gemini 4 Carbon (Low)" },
              { id: "gemini-4-argon-high", label: "Gemini 4 Argon (High)" },
              { id: "gemini-3.8-flash-high", label: "Gemini 3.8 Flash (High)" },
              { id: "claude-opus-5-5-high", label: "Claude Opus 5.5 (High)" },
              { id: "claude-sonnet-5-5-high", label: "Claude Sonnet 5.5 (High)" },
              { id: "codex:gpt-6-astra@xhigh", label: "ChatGPT 6 Astra (@xhigh)" },
              { id: "codex:gpt-6-astra@high", label: "ChatGPT 6 Astra (@high)" },
              { id: "codex:gpt-6.1-sol@xhigh", label: "ChatGPT 6.1 Sol (@xhigh)" },
              { id: "codex:gpt-6.1-sol@high", label: "ChatGPT 6.1 Sol (@high)" },
              { id: "codex:gpt-6.1-terra@xhigh", label: "ChatGPT 6.1 Terra (@xhigh)" },
              { id: "codex:gpt-6.1-terra@high", label: "ChatGPT 6.1 Terra (@high)" },
              { id: "codex:gpt-5.3-codex@xhigh", label: "GPT-5.3 Codex (@xhigh)" },
              { id: "codex:gpt-5.3-codex@high", label: "GPT-5.3 Codex (@high)" },
            ];
            const heavyTask = {
              complexity: "heavy",
              type: "fullstack",
              files: 5,
              risk: "high",
              needs_terminal: true,
              needsTerminal: true,
            };
            const rec = api.recommendModelsForTask(heavyTask, {
              models: pool,
              strategy: "balanced",
              executionContext: "spec_handoff",
              quotas: { gemini_5h_pct: 94, claude_5h_pct: 3 },
              chatgpt_5h_pct: 78,
            });
            assert.strictEqual(rec.top3.length, 3);
            const top1Id = rec.top3[0].id;
            assert(
              top1Id === "gemini-4-carbon-high" || top1Id === "codex:gpt-6.1-sol@high",
              `Expected workhorse Carbon High or Sol @high at #1, got ${top1Id}`
            );
            // Must NOT be 3 @xhigh models of one vendor, and Claude 3% must NOT enter Top-3
            assert(!rec.top3.every((x) => x.effort === "xhigh"), "Must not fill Top-3 with @xhigh");
            const vendorsInTop3 = new Set(rec.top3.map((x) => x.vendor));
            assert(vendorsInTop3.size >= 2, `Expected >=2 vendors in Top-3, got ${Array.from(vendorsInTop3)}`);
            assert(!vendorsInTop3.has("claude"), "Exhausted Claude (3%) must never enter Top-3");

            // 4. buildCodeHandoffPrompt cleanup (no forced full git diff) & buildExecutionFactSummaryFromMsg
            const handoffText = api.buildCodeHandoffPrompt("План работ", "Штаб", { orReview: true });
            assert(
              handoffText.includes("не запуская полный git diff без необходимости"),
              "buildCodeHandoffPrompt must contain updated anti-bloat review instruction"
            );

            const fakeExecChat = {
              id: "exec-chat-1",
              kind: "project",
              project_name: "Antigravity_Hub",
              title: "Фаза 1–5 Реализация",
              access_mode: "full_access",
              model: "codex:gpt-6.1-sol@high",
            };
            const fakeMsg = {
              id: 42,
              role: "assistant",
              model: "codex:gpt-6.1-sol@high",
              content: "Все 5 фаз успешно внедрены.\n<TASK_PROFILE>{\"complexity\":\"heavy\"}</TASK_PROFILE>",
              tool_calls: [
                { name: "edit_file", state: "DONE", parameters: { Path: "D:\\Internet\\Antigravity\\Antigravity_Hub\\app\\openrouter_service.py" } },
                { name: "Write", state: "DONE", parameters: { file_path: "tests/test_release_v91_orchestrator_upgrade.py" } },
                { name: "edit_file", state: "DONE", parameters: { Path: "D:/Internet/Antigravity/Antigravity_Hub/app/openrouter_service.py" } },
                { name: "run_command", state: "DONE", parameters: { CommandLine: "pytest tests/test_release_v91_orchestrator_upgrade.py -q" } },
                { name: "mcp_hub_sandbox_sandbox_exec", state: "ERROR", parameters: { command: "flake8 --max-line-length=100 app/" } },
              ],
            };
            const fact = api.buildExecutionFactSummaryFromMsg(fakeMsg, fakeExecChat);
            assert.strictEqual(fact.files.length, 2, "Duplicate file edits must be deduplicated");
            assert.strictEqual(fact.commands.length, 2);
            assert.strictEqual(fact.commands[0].status, "DONE");
            assert.strictEqual(fact.commands[1].status, "ERROR");
            assert.strictEqual(fact.toolStepsCount, 5);

            const returnPrompt = api.buildReturnToHqPrompt(fakeMsg, fakeExecChat);
            assert(returnPrompt.includes("[Отчёт о выполнении из рабочей вкладки «Фаза 1–5 Реализация»"));
            assert(!returnPrompt.includes("<TASK_PROFILE>"), "TASK_PROFILE must be stripped");
            assert(returnPrompt.includes("Изменённые файлы (2):"));
            assert(returnPrompt.includes("Шагов инструментов: 5"));

            // 5. v=92 polish: buildReturnToHqPrompt strips interrupt / network warning banners & service blockquotes
            const interruptedMsg1 = Object.assign({}, fakeMsg, {
              content:
                "Итоговый отчёт с нормальной цитатой:\n\n" +
                "> Архитектурное решение согласовано.\n\n" +
                "Готово.\n<TASK_PROFILE>{\"complexity\":\"light\"}</TASK_PROFILE>\n\n---\n" +
                "⚠️ **Ошибка:** There was a network issue connecting to the server, please try again.",
            });
            const rpClean1 = api.buildReturnToHqPrompt(interruptedMsg1, fakeExecChat);
            assert(rpClean1.includes("> Архитектурное решение согласовано."), "Normal body blockquote must stay");
            assert(!rpClean1.includes("⚠️ **Ошибка:**"), "Divider error banner must be stripped");
            assert(!rpClean1.includes("network issue"), "Network error detail must be stripped");
            assert(!rpClean1.includes("<TASK_PROFILE>"), "TASK_PROFILE before error banner must be stripped");

            const interruptedMsg2 = Object.assign({}, fakeMsg, {
              content:
                "Отчёт сформирован полностью.\n\n" +
                "> ⚠️ Сетевое предупреждение: поток оборвался на финализации.\n> Проверьте соединение.",
            });
            const rpClean2 = api.buildReturnToHqPrompt(interruptedMsg2, fakeExecChat);
            assert(rpClean2.includes("Отчёт сформирован полностью."));
            assert(!rpClean2.includes("> ⚠️"), "Trailing service warning blockquote must be stripped");
            assert(!rpClean2.includes("поток оборвался"), "Warning blockquote body must be stripped");
            """
        )

        env = config.apply_isolated_home_to_env(
            os.environ.copy(), config.ensure_user_cli_home("_system")
        )
        env["HUB_ROOT_DIR"] = str(ROOT_DIR)
        proc = subprocess.run(
            ["node", "-e", node_script],
            cwd=str(ROOT_DIR),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            **config.subprocess_hidden_kwargs(),
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Node Advisor v2 & Return-to-HQ test failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )

    # ------------------------------------------------------------------
    # 4. Polish v=92: Google CLI Network Disconnects & Free OpenRouter Hints
    # ------------------------------------------------------------------
    async def test_04_polish_v92_network_errors_and_free_openrouter_suggestions(self) -> None:
        import asyncio
        import time
        import app.agy_engine as agy_engine
        import app.database as database
        import app.openrouter_service as or_svc
        from tests.test_agy_subagent_guard import _FakeCliProcess

        # 4a. New Google CLI transient network markers
        net_msg = "There was a network issue connecting to the server, please try again."
        self.assertTrue(agy_engine.is_transient_stream_error(net_msg))
        for phrase in (
            "there was a network issue",
            "Network issue connecting to backend",
            "Upstream network error while reading frame",
            "Connection error during gRPC stream",
            "Failed to connect to the server",
            "Temporary network failure, retry shortly",
        ):
            self.assertTrue(agy_engine.is_transient_stream_error(phrase), phrase)

        # Non-transient auth/quota errors must still return False
        self.assertFalse(agy_engine.is_transient_stream_error("401 Unauthorized: connection error"))
        self.assertFalse(agy_engine.is_transient_stream_error("429 Quota rate limit network error"))

        # 4b. stream_agy_turn: trailing network error AFTER completed response is quietly logged & suppressed
        fake_agy = str(self.tmp / "bin" / "agy.exe")
        Path(fake_agy).write_text("agy", encoding="utf-8")

        done_then_net_err_events = [
            {"event": "init", "conversation_id": "conv-v92-done"},
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "step_index": 1,
                    "state": "DONE",
                    "text_delta": "Все изменения успешно внесены и проверены.",
                },
            },
            {
                "event": "result",
                "result": {
                    "status": "ERROR",
                    "error": net_msg,
                    "usage": {"input_tokens": 120, "output_tokens": 30},
                },
            },
        ]
        spawned_done: list[tuple[Any, ...]] = []

        async def _spawn_done_case(*args: Any, **kwargs: Any) -> _FakeCliProcess:
            spawned_done.append(args)
            return _FakeCliProcess(done_then_net_err_events, returncode=1)

        agy_engine.register_chat_turn(
            "chat-v92-done",
            user_id="u_v92",
            model="gemini-4-carbon-high",
            engine="agy",
        )
        with (
            patch.object(agy_engine, "resolve_agy_bin", return_value=fake_agy),
            patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn_done_case),
            self.assertLogs("agy_hub.agy_engine", level="INFO") as cm,
        ):
            evts_done = [
                ev
                async for ev in agy_engine.stream_agy_turn(
                    chat_id="chat-v92-done",
                    kind="chat",
                    project_name="",
                    model="gemini-4-carbon-high",
                    access_mode="read_only",
                    prompt="Проверь отчёт",
                    attachments=[],
                    agy_conversation_id="",
                    history_messages=[],
                    user={"id": "u_v92", "role": "user"},
                )
            ]
        agy_engine.ACTIVE_RUNS.pop("chat-v92-done", None)

        self.assertEqual(len(spawned_done), 1, "Completed turn must NOT spawn extra retry processes")
        self.assertFalse(any(e.get("type") == "error" for e in evts_done), evts_done)
        self.assertTrue(any(e.get("type") == "done" for e in evts_done), evts_done)
        self.assertTrue(
            any("Suppressed trailing transient CLI error" in line for line in cm.output),
            cm.output,
        )

        # 4c. stream_agy_turn: mid-stream network error on incomplete response triggers auto_continue
        mid_cut_events_1 = [
            {"event": "init", "conversation_id": "conv-v92-mid"},
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "step_index": 1,
                    "state": "ACTIVE",
                    "text_delta": "Начинаю вносить изменения в файл:",
                },
            },
            {
                "event": "result",
                "result": {
                    "status": "ERROR",
                    "error": net_msg,
                },
            },
        ]
        mid_cut_events_2 = [
            {"event": "init", "conversation_id": "conv-v92-mid"},
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "step_index": 2,
                    "state": "DONE",
                    "text_delta": "\nПравки успешно завершены.",
                },
            },
            {
                "event": "result",
                "result": {
                    "status": "DONE",
                    "usage": {"input_tokens": 150, "output_tokens": 45},
                },
            },
        ]
        mid_queue = [
            _FakeCliProcess(mid_cut_events_1, returncode=1),
            _FakeCliProcess(mid_cut_events_2, returncode=0),
        ]
        spawned_mid: list[tuple[Any, ...]] = []

        async def _spawn_mid_case(*args: Any, **kwargs: Any) -> _FakeCliProcess:
            spawned_mid.append(args)
            return mid_queue.pop(0)

        agy_engine.register_chat_turn(
            "chat-v92-mid",
            user_id="u_v92",
            model="gemini-4-carbon-high",
            engine="agy",
        )
        with (
            patch.object(agy_engine, "resolve_agy_bin", return_value=fake_agy),
            patch.object(agy_engine, "AUTO_CONTINUE_PAUSE_SEC", 0.01),
            patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn_mid_case),
        ):
            evts_mid = [
                ev
                async for ev in agy_engine.stream_agy_turn(
                    chat_id="chat-v92-mid",
                    kind="chat",
                    project_name="",
                    model="gemini-4-carbon-high",
                    access_mode="read_only",
                    prompt="Выполни правки",
                    attachments=[],
                    agy_conversation_id="",
                    history_messages=[],
                    user={"id": "u_v92", "role": "user"},
                )
            ]
        agy_engine.ACTIVE_RUNS.pop("chat-v92-mid", None)

        self.assertEqual(len(spawned_mid), 2, "Mid-stream network cut must trigger auto_continue")
        self.assertFalse(any(e.get("type") == "error" for e in evts_mid), evts_mid)
        done_evts = [e for e in evts_mid if e.get("type") == "done"]
        self.assertEqual(len(done_evts), 1)
        self.assertIn("Правки успешно завершены.", done_evts[0].get("content", ""))

        # 4d. OpenRouter require_free=(max_price_limit <= 0.0) when blocking same-vendor / deprecated model
        u_free = await database.create_user(
            username="or_free_user_v92",
            password="pw_1234",
            display_name="OR Free User",
            max_access_mode="write_files",
        )
        uid_free = u_free["id"]
        await database.upsert_user_openrouter_config(
            uid_free,
            new_api_key="\x73k-or-v1-freeBudgetTestKey0123456789abcdef",
            is_enabled=True,
            default_model="",
            max_price_per_m=0.0,
            daily_spend_limit_usd=0.0,
            custom_rules="",
        )
        mixed_catalog_raw = [
            {
                "id": "openai/gpt-6.1-sol",
                "name": "GPT-6.1 Sol",
                "created": 1768000000,
                "context_length": 272000,
                "pricing": {"prompt": "0.000002", "completion": "0.000008"},
            },
            {
                "id": "google/gemini-4-carbon",
                "name": "Gemini 4 Carbon (Paid)",
                "created": 1768500000,
                "context_length": 1000000,
                "pricing": {"prompt": "0.00000125", "completion": "0.000005"},
            },
            {
                "id": "anthropic/claude-5.5-sonnet",
                "name": "Claude 5.5 Sonnet (Paid)",
                "created": 1768500000,
                "context_length": 200000,
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            },
            {
                "id": "deepseek/deepseek-r2:free",
                "name": "DeepSeek R2 (Free)",
                "created": 1768600000,
                "context_length": 164000,
                "pricing": {"prompt": "0", "completion": "0"},
            },
            {
                "id": "qwen/qwen3-coder:free",
                "name": "Qwen3 Coder (Free)",
                "created": 1768600000,
                "context_length": 131072,
                "pricing": {"prompt": "0", "completion": "0"},
            },
            {
                "id": "google/gemini-4-flash:free",
                "name": "Gemini 4 Flash (Free)",
                "created": 1768600000,
                "context_length": 1000000,
                "pricing": {"prompt": "0", "completion": "0"},
            },
        ]
        norm_mixed = [
            m for m in (or_svc._normalize_catalog_model(r) for r in mixed_catalog_raw) if m is not None
        ]
        or_svc._MODELS_CACHE["items"] = norm_mixed
        or_svc._MODELS_CACHE["by_id"] = {m["id"].lower(): m for m in norm_mixed}
        or_svc._MODELS_CACHE["fetched_at"] = time.monotonic()

        agy_engine.register_chat_turn(
            "chat-free-gpt",
            user_id=uid_free,
            model="codex:gpt-6.1-sol@high",
            engine="codex",
        )
        free_bundle = await database.get_user_openrouter_secret_bundle(uid_free)
        self.assertIsNotNone(free_bundle)

        recorded_require_free: list[bool] = []
        orig_select = or_svc._select_fresh_cross_vendor_candidates

        def _spy_select(*s_args: Any, **s_kwargs: Any) -> list[dict[str, Any]]:
            recorded_require_free.append(bool(s_kwargs.get("require_free")))
            return orig_select(*s_args, **s_kwargs)

        with patch.object(or_svc, "_select_fresh_cross_vendor_candidates", side_effect=_spy_select):
            blocked_same_free = await or_svc.execute_openrouter_chat(
                free_bundle,
                model="openai/gpt-6.1-sol",
                prompt="Проверь архитектуру",
                explicit_user_choice=False,
                chat_id="chat-free-gpt",
            )
            blocked_dep_free = await or_svc.execute_openrouter_chat(
                free_bundle,
                model="anthropic/claude-3.5-sonnet",
                prompt="Проверь архитектуру",
                explicit_user_choice=False,
                chat_id="chat-free-gpt",
            )
        agy_engine.ACTIVE_RUNS.pop("chat-free-gpt", None)

        self.assertEqual(recorded_require_free, [True, True])
        for blocked_res in (blocked_same_free, blocked_dep_free):
            self.assertFalse(blocked_res["ok"])
            err_txt = blocked_res["error"]
            self.assertIn("FREE ($0/1M)", err_txt)
            self.assertIn("deepseek/deepseek-r2:free", err_txt)
            self.assertNotIn("google/gemini-4-carbon", err_txt)
            self.assertNotIn("anthropic/claude-5.5-sonnet", err_txt)


if __name__ == "__main__":
    unittest.main()
