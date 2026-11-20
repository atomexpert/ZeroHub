"""
Isolated tests for Antigravity Hub v=87: personal Claude Code CLI (`@anthropic-ai/claude-code`) subscriptions.

Covers (Section 8 of the spec):
1. Fernet crypto + RBAC isolation (per-user keys, cross-user 404, guest 403 on CLI install, no token leakage).
2. All 3 auth paths: remote OAuth (`auth login` URL + code), `setup-token` import, `.credentials.json` import,
   plus back-sync of refreshed tokens from disk into SQLite.
3. `/api/oauth/usage` -> `primary_5h` / `secondary_7d` normalization and `get_claude_sidebar_summary`.
4. Security matrix (`--permission-mode`, `--disallowedTools`, strict MCP with `"type": "http"`) + 0% GPU env.
5. `stream-json` parsing (init -> thinking -> tool_use Write/Edit/Bash/MCP -> tool_result -> text -> result usage),
   File Traffic Light paths and automatic failover to the 2nd account on `429 rate_limit_error`.
6. Three-way session bridge columns and `/compact` resetting all three native session ids.

Runs 100% inside `tempfile.TemporaryDirectory()` — never touches live `data/hub.db` or the running server.
"""

from __future__ import annotations

import asyncio
import json
import os
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

from tests.test_chatgpt_codex_integration import _MiniAsgiClient, _unlink_test_junctions  # noqa: E402


MOCK_CLAUDE_CLI = textwrap.dedent(
    r'''
    import json, os, sys, time
    from pathlib import Path

    def log(entry):
        lf = os.environ.get("MOCK_CLAUDE_LOG_FILE", "")
        if lf:
            with open(lf, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def cfg_dir():
        return Path(os.environ.get("CLAUDE_CONFIG_DIR", "."))

    def access_token():
        tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        if tok:
            return tok
        p = cfg_dir() / ".credentials.json"
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"]
            except Exception:
                return ""
        return ""

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def main():
        args = sys.argv[1:]
        if "--version" in args:
            print("2.1.99 (Claude Code)")
            return 0
        if args[:2] == ["auth", "login"]:
            sys.stdout.write("Browser didn't open? Use the url below to sign in:\n\n"
                             "\x1b[1mhttps://claude.ai/oauth/authorize?code=true&client_id=abc&state=xyz\x1b[0m\n\n"
                             "Paste code here if prompted > ")
            sys.stdout.flush()
            code = sys.stdin.readline().strip()
            log({"cmd": "login", "code": code, "browser": os.environ.get("BROWSER", "")})
            if code != "GOODCODE#state":
                print("OAuth error: invalid code")
                return 1
            (cfg_dir() / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
                "accessToken": "at_oauth_user@example.com", "refreshToken": "rt_oauth",
                "expiresAt": 1999999999000, "scopes": ["user:inference"], "subscriptionType": "max"}}), encoding="utf-8")
            print("Login successful.")
            return 0
        if args[:2] == ["auth", "status"]:
            tok = access_token()
            p = cfg_dir() / ".credentials.json"
            if tok.startswith("expired_") and p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
                data["claudeAiOauth"]["accessToken"] = tok.replace("expired_", "fresh_")
                data["claudeAiOauth"]["expiresAt"] = 2999999999000
                p.write_text(json.dumps(data), encoding="utf-8")
                tok = data["claudeAiOauth"]["accessToken"]
            email = tok.split("_", 2)[-1] if tok.count("_") >= 2 else "token@example.com"
            print(json.dumps({"loggedIn": bool(tok), "email": email, "subscriptionType": "pro" if "sk-ant-oat" in tok else "max",
                              "authMethod": "claude.ai", "accountUuid": "uuid-" + email}))
            return 0 if tok else 1
        if "-p" in args:
            prompt = sys.stdin.read()
            tok = access_token()
            log({"cmd": "turn", "argv": args, "token": tok, "prompt_len": len(prompt), "prompt_head": prompt[:700],
                 "cuda": os.environ.get("CUDA_VISIBLE_DEVICES", "MISSING"), "ort": os.environ.get("ORT_DISABLE_ALL_GPU", ""),
                 "api_key": os.environ.get("ANTHROPIC_API_KEY", ""), "cfg": os.environ.get("CLAUDE_CONFIG_DIR", "")})
            if os.environ.get("MOCK_REJECT_EFFORT", "") == "1" and "--effort" in args:
                sys.stderr.write("error: unknown option '--effort'\n")
                sys.stderr.flush()
                return 1
            emit({"type": "system", "subtype": "init", "session_id": "sess-" + tok[-6:]})
            if tok == os.environ.get("MOCK_EXHAUSTED_TOKEN", "__none__"):
                emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
                      "result": "API Error: 429 {\"type\":\"error\",\"error\":{\"type\":\"rate_limit_error\"}} 5-hour limit reached ∙ resets 5pm"})
                return 1
            ev = lambda e: emit({"type": "stream_event", "event": e})
            ev({"type": "message_start"})
            ev({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Планирую правку."}})
            tools = [
                ("t1", "Write", {"file_path": os.path.join(os.getcwd(), "src", "new_mod.py"), "content": "x=1"}),
                ("t2", "Edit", {"file_path": os.path.join(os.getcwd(), "README.md"), "old_string": "a", "new_string": "b"}),
                ("t3", "Bash", {"command": "pytest -q", "description": "Run tests"}),
                ("t4", "mcp__hub_openrouter__openrouter_chat", {"model": "x", "prompt": "hi"}),
            ]
            for i, (tid, name, inp) in enumerate(tools, start=1):
                ev({"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": tid, "name": name, "input": {}}})
                ev({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": json.dumps(inp)}})
                ev({"type": "content_block_stop", "index": i})
                emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
                emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": name == "Bash"}]}})
            ev({"type": "message_start"})
            ev({"type": "content_block_start", "index": 9, "content_block": {"type": "text"}})
            ev({"type": "content_block_delta", "index": 9, "delta": {"type": "text_delta", "text": "Готово: "}})
            ev({"type": "content_block_delta", "index": 9, "delta": {"type": "text_delta", "text": "файлы обновлены."}})
            emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Готово: файлы обновлены."}]}})
            emit({"type": "result", "subtype": "success", "is_error": False, "result": "Готово: файлы обновлены.",
                  "usage": {"input_tokens": 100, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 50, "output_tokens": 70}})
            return 0
        print("unknown args", args, file=sys.stderr)
        return 2

    sys.exit(main())
    '''
)

USAGE_PAYLOAD = {
    "five_hour": {"utilization": 16.0, "resets_at": "2026-11-13T12:00:00+00:00"},
    "seven_day": {"utilization": 42.5, "resets_at": "2026-11-18T00:00:00+00:00"},
    "seven_day_opus": {"utilization": 10, "resets_at": ""},
}


def _fake_usage(access_token: str, cli_version: str) -> tuple[int, Any]:
    if access_token.startswith("expired_"):
        return 401, "unauthorized"
    if access_token.startswith("sk-ant-oat"):
        return 403, "scope"
    return 200, dict(USAGE_PAYLOAD)


def _creds(token: str, refresh: str = "rt_x") -> str:
    return json.dumps(
        {"claudeAiOauth": {"accessToken": token, "refreshToken": refresh, "expiresAt": 1999999999000, "scopes": ["user:inference"]}}
    )


class TestClaudeCodeIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.agy_engine as agy_engine
        import app.claude_engine as claude_engine
        import app.config as config
        import app.database as database

        self._live_db = ROOT_DIR / "data" / "hub.db"
        self._live_stat = (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size) if self._live_db.exists() else None
        self._tmp = tempfile.TemporaryDirectory(prefix="agy_test_claude_v87_", ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name).resolve()
        self._orig = {
            "cfg_db": config.DB_PATH, "db_db": database.DB_PATH, "cfg_cli": config.CLI_HOMES_DIR,
            "cfg_agy": config.AGY_CLI_DATA_DIR, "eng_agy": agy_engine.AGY_CLI_DATA_DIR,
            "cl_sbx": claude_engine.CHAT_SANDBOX_DIR,
            "cfg_up": config.UPLOADS_DIR, "eng_up": agy_engine.UPLOADS_DIR, "cl_up": claude_engine.UPLOADS_DIR,
            "env_bin": os.environ.get("HUB_CLAUDE_BIN"),
            "env_log": os.environ.get("MOCK_CLAUDE_LOG_FILE"), "env_exh": os.environ.get("MOCK_EXHAUSTED_TOKEN"),
        }
        for d in ("cli_homes", "agy", "sbx", "uploads"):
            (self.tmp / d).mkdir(parents=True, exist_ok=True)
        self.mock_py = self.tmp / "mock_claude.py"
        self.mock_py.write_text(MOCK_CLAUDE_CLI, encoding="utf-8")
        self.log_file = self.tmp / "mock_claude_log.jsonl"
        os.environ["HUB_CLAUDE_BIN"] = str(self.mock_py)
        os.environ["MOCK_CLAUDE_LOG_FILE"] = str(self.log_file)
        os.environ.pop("MOCK_EXHAUSTED_TOKEN", None)
        os.environ.pop("MOCK_REJECT_EFFORT", None)
        config.DB_PATH = self.tmp / "test_v87.db"
        database.DB_PATH = config.DB_PATH
        config.CLI_HOMES_DIR = self.tmp / "cli_homes"
        config.AGY_CLI_DATA_DIR = self.tmp / "agy"
        agy_engine.AGY_CLI_DATA_DIR = self.tmp / "agy"
        claude_engine.CHAT_SANDBOX_DIR = self.tmp / "sbx"
        config.UPLOADS_DIR = self.tmp / "uploads"
        agy_engine.UPLOADS_DIR = self.tmp / "uploads"
        claude_engine.UPLOADS_DIR = self.tmp / "uploads"
        config._CLAUDE_INFO_CACHE.clear()
        self._usage_patch = patch.object(claude_engine, "_http_get_usage_sync", side_effect=_fake_usage)
        self._usage_patch.start()
        await database.init_db()

    async def asyncTearDown(self) -> None:
        import app.agy_engine as agy_engine
        import app.claude_engine as claude_engine
        import app.config as config
        import app.database as database
        import app.main as main_mod

        main_mod.CHAT_TURN_QUEUES.clear()
        main_mod.CHAT_QUEUE_PAUSED.clear()
        main_mod.COMPACTING_CHATS.clear()
        main_mod.ACTIVE_TURN_TASKS.clear()
        os.environ.pop("MOCK_REJECT_EFFORT", None)
        self._usage_patch.stop()
        for fid, fl in list(claude_engine._OAUTH_FLOWS.items()):
            await claude_engine.cancel_claude_oauth_flow(fl.user_id, fid)
        config.DB_PATH = self._orig["cfg_db"]
        database.DB_PATH = self._orig["db_db"]
        config.CLI_HOMES_DIR = self._orig["cfg_cli"]
        config.AGY_CLI_DATA_DIR = self._orig["cfg_agy"]
        agy_engine.AGY_CLI_DATA_DIR = self._orig["eng_agy"]
        claude_engine.CHAT_SANDBOX_DIR = self._orig["cl_sbx"]
        config.UPLOADS_DIR = self._orig["cfg_up"]
        agy_engine.UPLOADS_DIR = self._orig["eng_up"]
        claude_engine.UPLOADS_DIR = self._orig["cl_up"]
        config._CLAUDE_INFO_CACHE.clear()
        for k, ok in (("HUB_CLAUDE_BIN", "env_bin"), ("MOCK_CLAUDE_LOG_FILE", "env_log"), ("MOCK_EXHAUSTED_TOKEN", "env_exh")):
            if self._orig[ok] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = self._orig[ok]
        _unlink_test_junctions(self.tmp)
        self._tmp.cleanup()
        if self._live_stat is not None:
            self.assertEqual(self._live_stat, (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size))

    def _headers(self, user_id: str) -> dict[str, str]:
        import app.auth as auth

        return {"Authorization": f"Bearer {auth.create_user_session_token(user_id=user_id, session_version=1)}"}

    def _log(self) -> list[dict[str, Any]]:
        if not self.log_file.exists():
            return []
        return [json.loads(x) for x in self.log_file.read_text(encoding="utf-8").splitlines() if x.strip()]

    # ------------------------------------------------------------------
    async def test_01_fernet_and_rbac_isolation(self) -> None:
        import app.database as database
        import app.main as main

        ua = await database.create_user(username="alice", password="pw_a", display_name="A", max_access_mode="write_files")
        ub = await database.create_user(username="bob", password="pw_b", display_name="B", max_access_mode="write_files")
        c = _MiniAsgiClient(main.app)
        ra = await c.request("POST", "/api/claude/accounts/import", headers=self._headers(ua["id"]),
                             json_body={"payload": _creds("at_SECRET_alice@example.com", "rt_SECRET_A"), "label": "Личный Max"})
        self.assertEqual(ra.status_code, 200, ra.text)
        acc_a = ra.json()["account"]
        self.assertTrue(acc_a["id"].startswith("cld_"))
        self.assertNotIn("SECRET", ra.text)
        self.assertEqual(acc_a["quotas"]["primary_5h"]["remaining_pct"], 84.0)

        rb = await c.request("POST", "/api/claude/accounts/import", headers=self._headers(ub["id"]),
                             json_body={"payload": "\x73k-ant-oat01-SECRETBOBTOKEN", "label": "Bob Pro"})
        self.assertEqual(rb.status_code, 200, rb.text)
        self.assertEqual(rb.json()["account"]["auth_method"], "setup_token")
        self.assertNotIn("SECRETBOB", rb.text)
        self.assertFalse(rb.json()["account"]["quotas"]["ok"])

        # Per-user Fernet keys differ
        tok_a = database.encrypt_claude_credentials_json(ua["id"], '{"x":1}')
        self.assertEqual(database.decrypt_claude_credentials_json(ub["id"], tok_a), "")
        self.assertEqual(database.decrypt_claude_credentials_json(ua["id"], tok_a), '{"x":1}')

        # Bob sees only his account / models
        sb = await c.request("GET", "/api/claude/state", headers=self._headers(ub["id"]))
        ids_b = [a["id"] for a in sb.json()["accounts"]]
        self.assertNotIn(acc_a["id"], ids_b)
        for method, path, body in (
            ("POST", f"/api/claude/accounts/{acc_a['id']}/activate", None),
            ("PATCH", f"/api/claude/accounts/{acc_a['id']}", {"label": "hack"}),
            ("DELETE", f"/api/claude/accounts/{acc_a['id']}", None),
        ):
            r = await c.request(method, path, headers=self._headers(ub["id"]), json_body=body)
            self.assertEqual(r.status_code, 404, (method, path, r.text))
        self.assertIsNotNone(await database.get_user_claude_account(ua["id"], acc_a["id"]))

        r_inst = await c.request("POST", "/api/admin/claude/install", headers=self._headers(ub["id"]))
        self.assertEqual(r_inst.status_code, 403)

        # Models: claude-code:* only for users with accounts; __no_opus__ hides Opus
        await database.update_user(ub["id"], allowed_models=["__no_opus__"])
        fresh_b = await database.get_user_by_id(ub["id"])
        models_b = await main._get_models_for_user([], fresh_b)
        ids = [m["id"] for m in models_b if m["id"].startswith("claude-code:")]
        self.assertIn("claude-code:sonnet@high", ids)
        self.assertFalse(any("opus" in i for i in ids))
        uc = await database.create_user(username="carl", password="pw_c", display_name="C", max_access_mode="write_files")
        self.assertFalse(any(m["id"].startswith("claude-code:") for m in await main._get_models_for_user([], uc)))

    async def test_02_auth_paths_and_token_backsync(self) -> None:
        import app.claude_engine as ce
        import app.config as config
        import app.database as database
        import app.main as main

        u = await database.create_user(username="dan", password="pw_1234", display_name="D", max_access_mode="write_files")
        c = _MiniAsgiClient(main.app)
        r = await c.request("POST", "/api/claude/auth/flow/start", headers=self._headers(u["id"]), json_body={"label": "Мобильный"})
        self.assertEqual(r.status_code, 200, r.text)
        flow = r.json()
        self.assertEqual(flow["status"], "waiting_code")
        self.assertTrue(flow["verification_url"].startswith("https://claude.ai/oauth/authorize?"))
        self.assertEqual(flow["expires_in"] > 500, True)
        r2 = await c.request("POST", f"/api/claude/auth/flow/{flow['flow_id']}/code", headers=self._headers(u["id"]),
                             json_body={"code": "  GOODCODE#state \n"})
        self.assertEqual(r2.status_code, 200, r2.text)
        body = r2.json()
        self.assertEqual(body["status"], "completed")
        acc = body["account"]
        self.assertEqual(acc["email"], "user@example.com")
        self.assertEqual(acc["plan_type"], "max")
        self.assertEqual(acc["label"], "Мобильный")
        acc_dir = config.ensure_user_claude_account_dir(u["id"], acc["id"])
        self.assertTrue((acc_dir / ".credentials.json").is_file())
        accounts_root = acc_dir.parent
        self.assertFalse(any(p.name.startswith("_pending_") for p in accounts_root.iterdir()))
        self.assertTrue(any(e.get("cmd") == "login" and e.get("browser") == "echo" for e in self._log()))

        # setup-token + .credentials.json imports
        a2 = await ce.import_claude_credentials(u["id"], "sk-ant-oat01-ABCDEF", "Token")
        self.assertEqual(a2["auth_method"], "setup_token")
        a3 = await ce.import_claude_credentials(u["id"], _creds("at_x_json@example.com"), "JSON")
        self.assertEqual(a3["auth_method"], "credentials_json")
        self.assertEqual(len(await database.list_user_claude_accounts(u["id"])), 3)
        with self.assertRaises(ValueError):
            await ce.import_claude_credentials(u["id"], "garbage", "")

        # Back-sync: CLI refreshed the token on disk -> re-encrypted in DB
        full = await database.get_user_claude_account(u["id"], acc["id"], include_decrypted_credentials=True)
        d, _env = ce.materialize_account_claude_home(u["id"], full)
        data = json.loads((d / ".credentials.json").read_text(encoding="utf-8"))
        data["claudeAiOauth"]["accessToken"] = "at_refreshed_user@example.com"
        (d / ".credentials.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertTrue(await ce.sync_refreshed_credentials_back(u["id"], acc["id"], d))
        full2 = await database.get_user_claude_account(u["id"], acc["id"], include_decrypted_credentials=True)
        self.assertIn("at_refreshed_user@example.com", full2["credentials_json"])

        # 401 -> CLI refresh via `auth status` -> retry quotas
        a4 = await database.upsert_user_claude_account(u["id"], raw_credentials_json=_creds("expired_tok_e@example.com", "rt_e"), email="e@example.com")
        q = await ce.refresh_claude_account_quotas(u["id"], a4["id"])
        self.assertTrue(q["ok"], q)
        full4 = await database.get_user_claude_account(u["id"], a4["id"], include_decrypted_credentials=True)
        self.assertIn("fresh_tok_e@example.com", full4["credentials_json"])

    async def test_03_quota_parsing_and_summary(self) -> None:
        import app.claude_engine as ce
        import app.database as database

        q = ce.parse_claude_usage_payload(USAGE_PAYLOAD, "max")
        self.assertEqual(q["primary_5h"], {"used_pct": 16.0, "remaining_pct": 84.0, "window_mins": 300, "reset_time": "2026-11-13T12:00:00+00:00"})
        self.assertEqual(q["secondary_7d"]["remaining_pct"], 57.5)
        self.assertEqual(q["secondary_7d"]["window_mins"], 10080)
        self.assertIn("seven_day_opus", q)
        u = await database.create_user(username="eve", password="pw_1234", display_name="E", max_access_mode="write_files")
        acc = await database.upsert_user_claude_account(u["id"], raw_credentials_json=_creds("at_q_eve@example.com"), email="eve@example.com",
                                                         plan_type="max", quotas=q)
        s = await database.get_claude_sidebar_summary(u["id"])
        self.assertTrue(s["has_account"])
        self.assertEqual(s["active_account_id"], acc["id"])
        self.assertEqual(s["quotas"]["primary_5h"]["remaining_pct"], 84.0)
        self.assertEqual(s["enabled_count"], 1)
        for k in ("tokens_5h", "tokens_24h", "tokens_7d", "tokens_total", "cli_installed", "cli_version", "auto_failover"):
            self.assertIn(k, s)

    async def test_04_security_matrix_mcp_and_gpu_env(self) -> None:
        import app.claude_engine as ce
        import app.config as config

        plan = ce.resolve_claude_security_matrix(kind="chat", access_mode="full_access", is_admin=True)
        self.assertEqual(plan["permission_mode"], "plan")
        self.assertEqual(plan["disallowed_tools"], ["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"])
        ro = ce.resolve_claude_security_matrix(kind="project", access_mode="read_only", is_admin=True)
        self.assertEqual(ro["permission_mode"], "plan")
        code = ce.resolve_claude_security_matrix(kind="project", access_mode="write_files", is_admin=False)
        self.assertEqual((code["permission_mode"], code["disallowed_tools"]), ("acceptEdits", ["Bash"]))
        dock = ce.resolve_claude_security_matrix(kind="project", access_mode="full_access", is_admin=False,
                                                 force_no_host_shell=True, allow_hub_sandbox=True)
        self.assertEqual((dock["permission_mode"], dock["disallowed_tools"]), ("acceptEdits", ["Bash"]))
        self.assertIn("mcp__hub_sandbox__*", dock["allowed_tools"])
        nodock = ce.resolve_claude_security_matrix(kind="project", access_mode="full_access", is_admin=False, force_no_host_shell=True)
        self.assertEqual(nodock["permission_mode"], "acceptEdits")
        self.assertNotIn("mcp__hub_sandbox__*", nodock["allowed_tools"])
        for adm in (True, False):  # admin host / trusted host_full user
            host = ce.resolve_claude_security_matrix(kind="project", access_mode="full_access", is_admin=adm, force_no_host_shell=False)
            self.assertEqual(host["permission_mode"], "bypassPermissions")
            self.assertEqual(host["disallowed_tools"], [])

        acc_dir = config.ensure_user_claude_account_dir("u_sec", "cld_abc123")
        mcp = ce.write_claude_mcp_config(acc_dir, openrouter_url="http://127.0.0.1:1/mcp/openrouter/t", sandbox_url="http://127.0.0.1:1/mcp/sandbox/s")
        conf = json.loads(mcp.read_text(encoding="utf-8"))
        self.assertEqual(conf["mcpServers"]["hub_sandbox"], {"type": "http", "url": "http://127.0.0.1:1/mcp/sandbox/s"})
        self.assertEqual(conf["mcpServers"]["hub_openrouter"]["type"], "http")
        args = ce.build_claude_turn_args(model_slug="opus", effort="high", sec=dock, mcp_config_path=mcp, resume_session_id="s1", system_prompt="R")
        self.assertEqual(args[:5], ["-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages"])
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(args[args.index("--mcp-config") + 1], str(mcp))
        self.assertEqual(args[args.index("--resume") + 1], "s1")
        self.assertEqual(args[args.index("--disallowedTools") + 1], "Bash")

        os.environ["ANTHROPIC_API_KEY"] = "sk-api-should-be-removed"
        try:
            env = ce.build_claude_env("u_sec", acc_dir, oauth_token="sk-ant-oat01-z")
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "")
        self.assertEqual(env["ORT_DISABLE_ALL_GPU"], "1")
        self.assertEqual(env["BROWSER"], "echo")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(acc_dir.resolve()))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-z")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertTrue(str(acc_dir).replace("\\", "/").endswith("u_sec/.claude/accounts/cld_abc123"))
        with self.assertRaises(ValueError):
            config.ensure_user_claude_account_dir("u_sec", "..")

        self.assertEqual(ce.parse_claude_code_model_spec("claude-code:opus@medium"), ("opus", "medium"))
        self.assertEqual(ce.parse_claude_code_model_spec("claude-code:claude-sonnet-4-6"), ("claude-sonnet-4-6", "high"))
        self.assertEqual(ce.parse_claude_code_model_spec("claude-code:x;rm -rf@ultra"), ("sonnet", "high"))

    async def test_05_streaming_tools_and_failover(self) -> None:
        import app.agy_engine as agy_engine
        import app.claude_engine as ce
        import app.database as database

        admin = await database.get_user_by_id("admin")
        proj = self.tmp / "proj"
        (proj / "src").mkdir(parents=True)
        a1 = await database.upsert_user_claude_account("admin", raw_credentials_json=_creds("at_one_a1@example.com"), email="a1@example.com", label="A1")
        a2 = await database.upsert_user_claude_account("admin", raw_credentials_json=_creds("at_two_a2@example.com"), email="a2@example.com", label="A2", make_active=False)
        await database.activate_user_claude_account("admin", a1["id"])
        os.environ["MOCK_EXHAUSTED_TOKEN"] = "at_one_a1@example.com"

        events: list[dict[str, Any]] = []
        active_files: set[str] = set()
        async for ev in ce.stream_claude_turn(
            chat_id="chat_cc_1", kind="project", project_name="proj", model="claude-code:sonnet@high",
            access_mode="write_files", prompt="Сделай правку " + "x" * 40000, attachments=[],
            claude_session_id="", claude_account_id="", history_messages=[], user=admin,
            resolved_project_dir=proj, execution_mode="host",
        ):
            events.append(ev)
            run = agy_engine.ACTIVE_RUNS.get("chat_cc_1")
            if run is not None:
                active_files |= set(run.active_files)

        types = [e["type"] for e in events]
        self.assertIn("claude_failover", types, types)
        fo = next(e for e in events if e["type"] == "claude_failover")
        self.assertEqual((fo["from_label"], fo["to_label"], fo["account_id"]), ("A1", "A2", a2["id"]))
        done = events[-1]
        self.assertEqual(done["type"], "done", done)
        self.assertEqual(done["content"], "Готово: файлы обновлены.")
        self.assertEqual(done["thinking"], "Планирую правку.")
        self.assertEqual(done["claude_account_id"], a2["id"])
        self.assertEqual(done["claude_session_id"], "sess-" + "at_two_a2@example.com"[-6:])
        self.assertEqual(done["usage"]["input_tokens"], 1050)
        self.assertEqual(done["usage"]["output_tokens"], 70)
        self.assertEqual(done["usage"]["cached_tokens"], 900)
        self.assertEqual(done["usage"]["provider"], "claude_code")
        names = [t["name"] for t in done["tool_calls"]]
        self.assertEqual(names, ["write_file", "edit_file", "run_command", "mcp_hub_openrouter_openrouter_chat"])
        states = {t["name"]: t["state"] for t in done["tool_calls"]}
        self.assertEqual(states["run_command"], "ERROR")
        self.assertEqual(states["write_file"], "DONE")
        bash = next(t for t in done["tool_calls"] if t["name"] == "run_command")
        self.assertEqual(bash["parameters"], {"CommandLine": "pytest -q", "toolSummary": "Run tests"})
        self.assertIn("src/new_mod.py", active_files)
        self.assertIn("README.md", active_files)
        self.assertEqual(
            agy_engine.extract_writing_tool_paths("edit_file", {"Path": str(proj / "README.md")}, proj), ["README.md"]
        )
        self.assertIn("claude_session", types)
        self.assertLess(types.index("thinking_update"), types.index("tool_step"))
        self.assertLess(types.index("tool_step"), types.index("text_delta"))

        turns = [e for e in self._log() if e.get("cmd") == "turn"]
        self.assertEqual(len(turns), 2)
        for t in turns:
            self.assertEqual(t["cuda"], "")
            self.assertEqual(t["ort"], "1")
            self.assertEqual(t["api_key"], "")
            self.assertGreater(t["prompt_len"], 40000)  # prompt via stdin, not argv
            self.assertEqual(t["argv"][t["argv"].index("--permission-mode") + 1], "acceptEdits")
            self.assertEqual(t["argv"][t["argv"].index("--disallowedTools") + 1], "Bash")
            self.assertIn("--strict-mcp-config", t["argv"])
        self.assertEqual((await database.get_active_claude_account("admin"))["id"], a2["id"])
        exhausted = await database.get_user_claude_account("admin", a1["id"])
        self.assertEqual(exhausted["quotas"]["primary_5h"]["remaining_pct"], 0.0)

    async def test_06_session_bridge_and_compact(self) -> None:
        import app.agy_engine as agy_engine
        import app.database as database

        chat = await database.create_chat(kind="chat", title="Мост", model="gemini-4-carbon-high")
        self.assertIsNotNone(chat)
        cid = chat["id"]
        await database.update_chat(cid, agy_conversation_id="agy-1", last_engine="agy")
        await database.update_chat(cid, codex_thread_id="thr-1", codex_account_id="cgpt_1", last_engine="codex")
        c2 = await database.update_chat(cid, claude_session_id="sess-1", claude_account_id="cld_1", last_engine="claude")
        self.assertEqual((c2["last_engine"], c2["claude_session_id"], c2["claude_account_id"]), ("claude", "sess-1", "cld_1"))
        c3 = await database.update_chat(cid, last_engine="agy")
        self.assertEqual(c3["last_engine"], "agy")
        self.assertEqual(c3["claude_session_id"], "sess-1")
        after = await database.save_chat_compaction(cid, compacted_summary="S", compacted_up_to_msg_id=5, compacted_count=1)
        self.assertEqual((after["agy_conversation_id"], after["codex_thread_id"], after["claude_session_id"]), ("", "", ""))
        self.assertEqual(after["compacted_summary"], "S")
        self.assertEqual(agy_engine.classify_model_family("claude-code:opus@high"), "claude_code")
        self.assertNotEqual(agy_engine.classify_model_family("claude-code:opus@high"), agy_engine.classify_model_family("claude-opus-5-5"))
        w = agy_engine.get_model_context_window("claude-code:sonnet@high")
        self.assertEqual((w["family"], w["max_tokens"]), ("claude_code", 200_000))

    async def test_07_audit_regressions_and_3way_turn_e2e(self) -> None:
        import app.agy_engine as agy_engine
        import app.claude_engine as ce
        import app.config as config
        import app.database as database
        import app.main as main
        import app.openrouter_service as openrouter_service
        import app.sandbox_service as sandbox_service

        # 1. Guest personal Claude Code usage is isolated from 24h Google token counter & owner quota_reserve_pct
        guest = await database.create_user(
            username="vera",
            password="pw_vera_1",
            display_name="Vera",
            max_access_mode="docker_full",
            allowed_models=["gemini-4-carbon-low"],
            daily_token_limit=1000,
            quota_reserve_pct=99,
        )
        g_chat = await database.create_chat(
            title="Vera Quota",
            kind="chat",
            model="gemini-4-carbon-low",
            owner_id=guest["id"],
        )
        await database.add_message(
            g_chat["id"],
            user_id=guest["id"],
            role="assistant",
            content="Ответ Google",
            model="gemini-4-carbon-low",
            usage={"input_tokens": 300, "output_tokens": 200, "total_tokens": 500},
            status="done",
        )
        self.assertEqual(await database.get_user_24h_tokens(guest["id"]), 500)
        with patch.object(
            main,
            "fetch_account_quotas",
            return_value={"ok": True, "gemini_5h_pct": 25.0, "claude_5h_pct": 25.0},
        ):
            # Google model is blocked by owner's 99% quota reserve (25% <= 99%)
            self.assertIsNotNone(await main._enforce_user_turn_quotas(guest, "gemini-4-carbon-low"))
            err_no_cld = await main._enforce_user_turn_quotas(guest, "claude-code:sonnet@high")
            self.assertIsNotNone(err_no_cld)
            self.assertIn("Claude", err_no_cld or "")

            acc_v1 = await ce.import_claude_credentials(guest["id"], _creds("at_v1_vera@example.com"), "Vera Max #1")
            # Personal Claude Code bypasses Google allowed_models & owner's 99% quota_reserve_pct!
            self.assertIsNone(await main._enforce_user_turn_quotas(guest, "claude-code:sonnet@high"))

        await database.add_message(
            g_chat["id"],
            user_id=guest["id"],
            role="assistant",
            content="Ответ личного Claude Code",
            model="claude-code:sonnet@high",
            usage={
                "provider": "claude_code",
                "claude_account_id": acc_v1["id"],
                "input_tokens": 25000,
                "context_input_tokens": 25000,
                "cached_tokens": 20000,
                "output_tokens": 5000,
                "total_tokens": 30000,
            },
            status="done",
        )
        # 30,000 personal Claude tokens MUST NOT inflate the 24h Google CLI token counter (stays 500 < 1000)!
        self.assertEqual(await database.get_user_24h_tokens(guest["id"]), 500)
        self.assertIsNone(await main._enforce_user_turn_quotas(guest, "claude-code:sonnet@high"))
        cld_stats = await database.get_claude_token_usage_stats(guest["id"])
        self.assertEqual(cld_stats["window_24h"]["total_tokens"], 30000)

        # 2. Context window telemetry (`compute_chat_context_stats`) must NOT double-count `cached_tokens`
        await database.update_chat(
            g_chat["id"],
            model="claude-code:sonnet@high",
            claude_session_id="sess-vera01",
            claude_account_id=acc_v1["id"],
            last_engine="claude",
        )
        g_chat_fresh = await database.get_chat(g_chat["id"])
        g_msgs = await database.get_messages(g_chat["id"])
        assert g_chat_fresh is not None
        ctx_stats = agy_engine.compute_chat_context_stats(g_chat_fresh, g_msgs)
        self.assertEqual(ctx_stats["source"], "telemetry")
        self.assertEqual(ctx_stats["max_tokens"], 200_000)
        # 25,000 context_input_tokens + 5,000 output_tokens = 30,000 (20,000 cached_tokens NOT double-counted -> != 50,000)
        self.assertEqual(ctx_stats["used_tokens"], 30000, f"Must not double-count 20k cached_tokens: {ctx_stats}")

        # 3. `CLAUDE.md` & `skills/*/SKILL.md` mirroring into per-account `CLAUDE_CONFIG_DIR`
        config.sync_user_cli_rules(guest, "Отвечай строго по делу.")
        await database.upsert_user_openrouter_config(guest["id"], new_api_key="sk-or-v1-verakey123456", is_enabled=True)
        await openrouter_service.sync_user_openrouter_cli_files(guest["id"])
        sandbox_service.sync_user_sandbox_cli_files(guest, sandbox_ready=True)
        full_v1 = await database.get_user_claude_account(guest["id"], acc_v1["id"], include_decrypted_credentials=True)
        assert full_v1 is not None
        acc_dir_v1, _ = ce.materialize_account_claude_home(guest["id"], full_v1)
        self.assertTrue((acc_dir_v1 / "CLAUDE.md").is_file())
        self.assertIn("Отвечай строго по делу.", (acc_dir_v1 / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertTrue((acc_dir_v1 / "skills" / "openrouter-bridge" / "SKILL.md").is_file())
        self.assertTrue((acc_dir_v1 / "skills" / "docker-sandbox" / "SKILL.md").is_file())

        # 4. Scoped `--add-dir` for uploaded attachments + transparent `--effort` fallback on older CLI
        up_chat_dir = (self.tmp / "uploads" / "chat_att_ok").resolve()
        up_chat_dir.mkdir(parents=True, exist_ok=True)
        att_path = up_chat_dir / "large_prompt_input.md"
        att_path.write_text("# Большое ТЗ", encoding="utf-8")
        os.environ["MOCK_REJECT_EFFORT"] = "1"
        try:
            att_events = [
                ev
                async for ev in ce.stream_claude_turn(
                    chat_id="chat_att_ok",
                    kind="chat",
                    project_name="",
                    model="claude-code:sonnet@high",
                    access_mode="read_only",
                    prompt="Прочитай вложение",
                    attachments=[{"name": "large_prompt_input.md", "disk_path": str(att_path)}],
                    claude_session_id="",
                    claude_account_id="",
                    history_messages=[],
                    user=guest,
                )
            ]
        finally:
            os.environ.pop("MOCK_REJECT_EFFORT", None)
        self.assertEqual(att_events[-1]["type"], "done", att_events[-1])
        turn_logs = [e for e in self._log() if e.get("cmd") == "turn"]
        self.assertGreaterEqual(len(turn_logs), 2)
        self.assertIn("--effort", turn_logs[-2]["argv"])
        self.assertNotIn("--effort", turn_logs[-1]["argv"])
        self.assertIn("--add-dir", turn_logs[-1]["argv"])
        add_dir_val = turn_logs[-1]["argv"][turn_logs[-1]["argv"].index("--add-dir") + 1]
        self.assertEqual(Path(add_dir_val).resolve(), up_chat_dir)

        # 5. `GET /api/claude/state?refresh=true` refreshes ALL enabled accounts of the user
        acc_v2 = await database.upsert_user_claude_account(
            guest["id"],
            raw_credentials_json=_creds("at_v2_vera2@example.com"),
            email="vera2@example.com",
            label="Vera Max #2",
            quotas={},
            make_active=False,
        )
        client = _MiniAsgiClient(main.app)
        st_resp = await client.request("GET", "/api/claude/state?refresh=true", headers=self._headers(guest["id"]))
        self.assertEqual(st_resp.status_code, 200, st_resp.text)
        st_json = st_resp.json()
        self.assertEqual(st_json["active_account_id"], acc_v1["id"])
        by_id = {a["id"]: a for a in st_json["accounts"]}
        self.assertEqual(by_id[acc_v1["id"]]["quotas"]["primary_5h"]["remaining_pct"], 84.0)
        self.assertEqual(by_id[acc_v2["id"]]["quotas"]["primary_5h"]["remaining_pct"], 84.0)

        # 6. Full 3-way `_run_turn_task_inner` E2E (`Gemini` -> `Claude Code` -> custom slug -> `Gemini` + `/compact`)
        admin_user = await database.get_user_by_id("admin")
        assert admin_user is not None
        adm_a1 = await ce.import_claude_credentials("admin", _creds("at_adm_one@example.com"), "Admin CC #1")
        adm_a2 = await ce.import_claude_credentials("admin", _creds("at_adm_two@example.com"), "Admin CC #2")
        await database.activate_user_claude_account("admin", adm_a1["id"])

        sw_chat = await database.create_chat(
            title="3-Way Engine Switch",
            kind="chat",
            model="gemini-4-carbon-high",
            owner_id="admin",
        )
        scid = sw_chat["id"]
        agy_calls: list[dict[str, Any]] = []

        async def _fake_agy_stream(**kwargs: Any):
            agy_calls.append(kwargs)
            yield {"type": "agy_session", "agy_conversation_id": "agy-conv-3way"}
            yield {"type": "text_delta", "delta": f"Ответ AGY ({kwargs['model']})", "seq": 1}
            yield {
                "type": "done",
                "content": f"Ответ AGY ({kwargs['model']})",
                "thinking": "",
                "tool_calls": [],
                "usage": {"input_tokens": 120, "output_tokens": 40, "total_tokens": 160},
            }

        async def _noop_refresh(*_a: Any, **_kw: Any) -> None:
            return None

        async def _turn(prompt_txt: str, mid: str) -> dict[str, Any]:
            await main._run_turn_task_inner(
                chat_id=scid,
                prompt=prompt_txt,
                attachments=[],
                override_model=mid,
                override_access_mode=None,
                trigger_user=admin_user,
                referenced_chat_ids=[],
            )
            c_row = await database.get_chat(scid)
            assert c_row is not None
            return c_row

        log_before_3way = len(self._log())
        with (
            patch.object(main, "stream_agy_turn", side_effect=_fake_agy_stream),
            patch.object(main, "_refresh_and_broadcast_quotas", side_effect=_noop_refresh),
        ):
            # Step A: Gemini turn
            s1 = await _turn("Привет от Gemini", "gemini-4-carbon-high")
            self.assertEqual((s1["last_engine"], s1["agy_conversation_id"]), ("agy", "agy-conv-3way"))

            # Step B: Switch to Claude Code -> injects history recovery with Gemini's reply, starts session
            s2 = await _turn("Продолжи мысль в Claude Code", "claude-code:sonnet@high")
            self.assertEqual((s2["last_engine"], s2["claude_account_id"]), ("claude", adm_a1["id"]))
            sess_1 = s2["claude_session_id"]
            self.assertTrue(sess_1.startswith("sess-"))

            # Step C: Custom `claude-code:*` slug preserved in DB + resumes `sess_1` without recovery
            s3 = await _turn("Уточни на кастомном слаге", "claude-code:claude-sonnet-4-6@medium")
            self.assertEqual(s3["model"], "claude-code:claude-sonnet-4-6@medium")
            self.assertEqual(s3["claude_session_id"], sess_1)

            # Step D: Switch back to Gemini -> `agy_conversation_id` forced to "" so AGY gets Claude's replies!
            s4 = await _turn("Финальный итог в Gemini", "gemini-4-carbon-high")
            self.assertEqual(s4["last_engine"], "agy")
            self.assertEqual(len(agy_calls), 2)
            self.assertEqual(agy_calls[1]["agy_conversation_id"], "")
            hist_joined = "\n".join(m.get("content", "") for m in agy_calls[1]["history_messages"])
            self.assertIn("Готово: файлы обновлены.", hist_joined)

        cc_turns = [e for e in self._log()[log_before_3way:] if e.get("cmd") == "turn"]
        self.assertEqual(len(cc_turns), 2)
        self.assertNotIn("--resume", cc_turns[0]["argv"])
        self.assertIn("Ответ AGY (gemini-4-carbon-high)", cc_turns[0]["prompt_head"])
        self.assertIn("--resume", cc_turns[1]["argv"])
        self.assertEqual(cc_turns[1]["argv"][cc_turns[1]["argv"].index("--resume") + 1], sess_1)
        self.assertEqual(cc_turns[1]["argv"][cc_turns[1]["argv"].index("--model") + 1], "claude-sonnet-4-6")
        self.assertEqual(cc_turns[1]["argv"][cc_turns[1]["argv"].index("--effort") + 1], "medium")

        # Step E: `/compact` via `claude-code:haiku@low` + reset of all 3 session ids
        msgs_sw = await database.get_messages(scid)
        sw_latest = await database.get_chat(scid)
        assert sw_latest is not None
        comp = await agy_engine.compact_chat_context(
            chat=sw_latest,
            messages=msgs_sw,
            model="claude-code:haiku@low",
            user_id="admin",
        )
        self.assertTrue(str(comp.get("summary") or "").strip())
        self.assertEqual((comp.get("usage") or {}).get("provider"), "claude_code")
        after_comp = await database.save_chat_compaction(
            scid,
            compacted_summary=comp["summary"],
            compacted_up_to_msg_id=int(msgs_sw[-1]["id"]),
            compacted_count=len(msgs_sw),
        )
        assert after_comp is not None
        self.assertEqual(
            (
                after_comp["agy_conversation_id"],
                after_comp["codex_thread_id"],
                after_comp["claude_session_id"],
                after_comp["claude_account_id"],
            ),
            ("", "", "", ""),
        )

        # Step F: Deleting a Claude account clears `claude_session_id` / `claude_account_id` on chats referencing it
        await database.update_chat(scid, claude_session_id="sess-dead", claude_account_id=adm_a2["id"], last_engine="claude")
        self.assertTrue(await database.delete_user_claude_account("admin", adm_a2["id"]))
        after_del = await database.get_chat(scid)
        assert after_del is not None
        self.assertEqual((after_del["claude_session_id"], after_del["claude_account_id"]), ("", ""))

    def test_08_frontend_utf8_integrity_and_advisor_claude_code_vm(self) -> None:
        import re
        import subprocess
        from app.config import apply_isolated_home_to_env, ensure_user_cli_home, subprocess_hidden_kwargs

        static_dir = ROOT_DIR / "app" / "static"
        for fname in ("index.html", "app.js", "styles.css", "sw.js"):
            fpath = static_dir / fname
            raw = fpath.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{fname} must not have UTF-8 BOM")
            txt = raw.decode("utf-8")
            for bad_seq in ("РџРѕ", "РќР°СЃ", "РњРѕРґ"):
                self.assertNotIn(bad_seq, txt, f"{fname} contains CP1251 mojibake ({bad_seq})")

        html_txt = (static_dir / "index.html").read_text(encoding="utf-8")
        for landmark in (
            'id="claudeModal"',
            'id="sidebarClaudeBtn"',
            'id="mobClaudePillBtn"',
            'id="settingsClaudeBlock"',
            "Подписки Claude Code",
        ):
            self.assertIn(landmark, html_txt)
        ver_matches = [int(v) for v in re.findall(r"(?:styles\.css|app\.js)\?v=(\d+)", html_txt)]
        self.assertTrue(ver_matches and all(v >= 87 for v in ver_matches), ver_matches)

        sw_txt = (static_dir / "sw.js").read_text(encoding="utf-8")
        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_txt)
        self.assertIsNotNone(m_sw)
        assert m_sw is not None
        self.assertGreaterEqual(int(m_sw.group(1)), 87)

        node_script = textwrap.dedent(
            r"""
            const assert = require("assert");
            const fs = require("fs");
            const path = require("path");
            const vm = require("vm");

            const rootDir = process.env.HUB_ROOT_DIR;
            const appCode = fs.readFileSync(path.join(rootDir, "app/static/app.js"), "utf8");
            const dummyClassList = { add() {}, remove() {}, toggle() {}, contains: () => false };
            const dummyEl = () => ({
              classList: dummyClassList,
              addEventListener() {},
              removeEventListener() {},
              querySelector: () => null,
              querySelectorAll: () => [],
              setAttribute() {},
              removeAttribute() {},
              getAttribute: () => null,
              appendChild() {},
              style: {},
              dataset: {},
              options: [],
              value: "",
              textContent: "",
              innerHTML: "",
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
              location: { protocol: "http:", host: "127.0.0.1:8080", pathname: "/", search: "", hash: "" },
              navigator: { userAgent: "NodeTest" },
              innerWidth: 1280,
              innerHeight: 800,
              setTimeout,
              clearTimeout,
              setInterval: () => 1,
              clearInterval: () => {},
              requestAnimationFrame: (cb) => setTimeout(cb, 0),
              cancelAnimationFrame: clearTimeout,
              matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
              addEventListener() {},
              CSS: { supports: () => true, escape: (s) => String(s) },
              URL,
              URLSearchParams,
            };
            win.window = win;
            win.globalThis = win;
            const ctx = vm.createContext(
              Object.assign({}, win, {
                window: win, globalThis: win, document: doc, console,
                Math, Date, JSON, Promise, Set, Map, WeakMap, AbortController,
                RegExp, Array, Object, String, Number, Boolean, Error, TypeError,
                encodeURIComponent, decodeURIComponent, parseInt, parseFloat, isNaN, isFinite, Intl,
              })
            );
            vm.runInContext(appCode, ctx, { filename: "app.js" });
            const api = win.__AGY_ADVISOR_API__ || ctx.__AGY_ADVISOR_API__;
            assert(api, "Advisor API exported");

            assert.strictEqual(api.classifyModelVendor({ id: "claude-code:opus@high" }), "claude_code");
            assert.strictEqual(api.classifyModelVendor({ id: "claude-opus-4-6-thinking" }), "claude");
            assert.strictEqual(api.getAdvisorShortVendorName("claude_code"), "Claude Code");

            const qPair = api.extractClaudeQuotasPair({
              primary_5h: { remaining_pct: 84 },
              secondary_7d: { remaining_pct: 57.5 },
            });
            assert.strictEqual(qPair.pct5h, 84);
            assert.strictEqual(qPair.pct7d, 57.5);

            const profOpus = api.buildModelAdvisorProfile({ id: "claude-code:opus@high", label: "Claude Opus (High)" }, 0);
            assert.strictEqual(profOpus.vendor, "claude_code");
            assert.strictEqual(profOpus.isClaudeCode, true);
            assert.strictEqual(profOpus.isOpus, true);
            assert(profOpus.power >= 95);

            // Strict quota independence: Google Claude at 2% vs Personal Claude Code at 92%
            const models = [
              { id: "claude-opus-4-6-thinking", label: "Claude Opus 4.6 (Thinking)" },
              { id: "claude-code:opus@high", label: "Claude Opus (High)" },
              { id: "claude-code:sonnet@high", label: "Claude Sonnet (High)" },
              { id: "claude-code:haiku@low", label: "Claude Haiku (Low)" },
            ];
            const opts = {
              models,
              strategy: "max_quality",
              quotas: { gemini_5h_pct: 20, claude_5h_pct: 2 },
              claude: {
                activeAccountId: "cld_1",
                accounts: [{ id: "cld_1", is_active: true, is_enabled: true, quotas: { primary_5h: { remaining_pct: 92 } } }],
              },
            };
            assert.strictEqual(api.getAdvisorVendorQuota5hPct("claude", opts), 2);
            assert.strictEqual(api.getAdvisorVendorQuota5hPct("claude_code", opts), 92);

            const heavySpec = "План.\n<TASK_PROFILE>{\"complexity\":\"critical\",\"type\":\"backend\",\"files\":5,\"risk\":\"high\",\"needs_terminal\":true}</TASK_PROFILE>";
            const rec = api.recommendModelsForTask(heavySpec, opts);
            assert.strictEqual(rec.top3[0].id, "claude-code:opus@high");
            """
        )

        env = apply_isolated_home_to_env(os.environ.copy(), ensure_user_cli_home("_system"))
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
            **subprocess_hidden_kwargs(),
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Node Claude Code Advisor VM failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
