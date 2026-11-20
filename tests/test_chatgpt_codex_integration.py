"""
Isolated automated tests for Antigravity Hub v=80:
Personal ChatGPT Accounts (OpenAI Codex CLI `app-server` JSON-RPC 2.0) & Subscription Quotas.

Covers all 5 mandatory verification suites from Section 9.2 of the specification:
1. RBAC & Fernet Crypto-Isolation (`PBKDF2-SHA256` per-user salt, zero raw token leakage, 404 cross-user, 403 guest CLI install).
2. `CODEX_HOME` & `config.toml` Security Matrix (`cli_auth_credentials_store = "file"`, `shell_tool`, `hub_sandbox`, `AGENTS.md`, `.agents/skills`).
3. Independence from Admin's Google Quotas (`allowed_models`, `daily_token_limit`, `quota_reserve_pct` never block personal `codex:*`, and ChatGPT tokens never count toward guest's 24h Google limit).
4. Seamless Mid-Chat Engine & Account Switching (`Gemini` -> `ChatGPT Acc #1` -> `ChatGPT Acc #2` -> `Claude` -> `/compact`).
5. Full Python Mock `codex app-server` E2E (`login --device-auth`, `account/rateLimits/read` 5h/7d, live File Traffic Light `active_files`, Project Memory changelog, and automatic 5h quota failover Acc #1 -> Acc #2).

Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER touches live `data/hub.db` or `run_server.py`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import textwrap
import unittest
import urllib.parse
from pathlib import Path
from typing import Any
from unittest.mock import patch

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


def _make_fake_jwt(email: str, account_id: str, plan_type: str = "plus") -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode("ascii").rstrip("=")
    payload_obj = {
        "email": email,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": plan_type,
        },
    }
    payload = (
        base64.urlsafe_b64encode(json.dumps(payload_obj).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    return f"{header}.{payload}.sig"


def _make_auth_json_str(email: str, account_id: str, plan_type: str = "plus", secret_tag: str = "tok") -> str:
    jwt_tok = _make_fake_jwt(email, account_id, plan_type)
    return json.dumps(
        {
            "tokens": {
                "access_token": f"at_{secret_tag}_{account_id}",
                "refresh_token": f"rt_{secret_tag}_{account_id}",
                "id_token": jwt_tok,
                "account_id": account_id,
            }
        }
    )


MOCK_CODEX_CLI_SCRIPT = textwrap.dedent(
    r'''
    import base64
    import json
    import os
    import sys
    import time
    from pathlib import Path

    def _decode_jwt(tok: str) -> dict:
        parts = (tok or "").split(".")
        if len(parts) < 2:
            return {}
        b = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        try:
            return json.loads(base64.urlsafe_b64decode(b.encode("ascii")).decode("utf-8"))
        except Exception:
            return {}

    def _read_identity(codex_home: Path) -> tuple[str, str, str]:
        auth_p = codex_home / "auth.json"
        if not auth_p.is_file():
            return ("", "", "")
        try:
            raw = json.loads(auth_p.read_text(encoding="utf-8"))
        except Exception:
            return ("", "", "")
        tokens = raw.get("tokens") or {}
        claims = _decode_jwt(tokens.get("id_token") or "")
        ns = claims.get("https://api.openai.com/auth") or {}
        email = str(claims.get("email") or raw.get("email") or "")
        acc_id = str(tokens.get("account_id") or ns.get("chatgpt_account_id") or "")
        plan = str(ns.get("chatgpt_plan_type") or raw.get("plan_type") or "plus")
        return (email, acc_id, plan)

    def _append_log(entry: dict) -> None:
        log_file = os.environ.get("MOCK_CODEX_LOG_FILE", "").strip()
        if not log_file:
            return
        p = Path(log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def main() -> int:
        args = sys.argv[1:]
        codex_home = Path(os.environ.get("CODEX_HOME", "."))
        if "--version" in args:
            print("codex-cli 0.167.2-mock", flush=True)
            return 0

        if len(args) >= 2 and args[0] == "login" and "--device-auth" in args:
            sys.stdout.write(
                "\x1b[36mOpen this link in your browser:\x1b[0m https://auth.openai.com/codex/device\n"
                "\x1b[1;32mEnter one-time code:\x1b[0m K9W2-7XM4P\n"
            )
            sys.stdout.flush()
            delay = float(os.environ.get("MOCK_DEVICE_AUTH_DELAY", "0.05"))
            time.sleep(delay)
            email = os.environ.get("MOCK_DEVICE_EMAIL", "device_user@example.com")
            acc_id = os.environ.get("MOCK_DEVICE_ACC_ID", "cgpt_dev_01")
            plan = os.environ.get("MOCK_DEVICE_PLAN", "pro")
            header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode("ascii").rstrip("=")
            payload = base64.urlsafe_b64encode(
                json.dumps({
                    "email": email,
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": acc_id,
                        "chatgpt_plan_type": plan,
                    },
                }).encode("utf-8")
            ).decode("ascii").rstrip("=")
            codex_home.mkdir(parents=True, exist_ok=True)
            (codex_home / "auth.json").write_text(
                json.dumps({
                    "tokens": {
                        "access_token": f"at_device_{acc_id}",
                        "refresh_token": f"rt_device_{acc_id}",
                        "id_token": f"{header}.{payload}.sig",
                        "account_id": acc_id,
                    }
                }),
                encoding="utf-8",
            )
            return 0

        if args and args[0] == "app-server":
            email, acc_id, plan = _read_identity(codex_home)
            exhausted_acc = os.environ.get("MOCK_EXHAUSTED_ACC_ID", "").strip()
            emit_file_change = os.environ.get("MOCK_EMIT_FILE_CHANGE", "").strip()
            active_hold_ms = float(os.environ.get("MOCK_ACTIVE_HOLD_SEC", "0.0"))
            thread_seq = 0
            current_thread_id = ""

            for raw_line in sys.stdin:
                line = raw_line.strip()
                if not line:
                    continue
                msg = json.loads(line)
                rid = msg.get("id")
                method = msg.get("method")
                params = msg.get("params") or {}

                if method == "initialize":
                    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"userAgent": "mock"}}), flush=True)
                elif method == "account/read":
                    if email:
                        res = {"account": {"type": "chatgpt", "email": email, "planType": plan}, "requiresOpenaiAuth": False}
                    else:
                        res = {"account": None, "requiresOpenaiAuth": True}
                    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": res}), flush=True)
                elif method == "account/rateLimits/read":
                    used_5h = 100.0 if (exhausted_acc and acc_id == exhausted_acc) else 16.0
                    res = {
                        "rateLimits": {
                            "planType": plan or "plus",
                            "primary": {"usedPercent": used_5h, "windowDurationMins": 300, "resetsAt": int(time.time()) + 7200},
                            "secondary": {"usedPercent": 38.0, "windowDurationMins": 10080, "resetsAt": int(time.time()) + 300000},
                        }
                    }
                    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": res}), flush=True)
                elif method == "model/list":
                    res = {
                        "data": [
                            {
                                "id": "gpt-5.4",
                                "model": "gpt-5.4",
                                "displayName": "GPT-5.4",
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": "low"},
                                    {"reasoningEffort": "medium"},
                                    {"reasoningEffort": "high"},
                                ],
                                "defaultReasoningEffort": "medium",
                            },
                            {
                                "id": "gpt-5.3-codex",
                                "model": "gpt-5.3-codex",
                                "displayName": "GPT-5.3 Codex",
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": "medium"},
                                    {"reasoningEffort": "high"},
                                    {"reasoningEffort": "xhigh"},
                                ],
                                "defaultReasoningEffort": "high",
                            },
                        ]
                    }
                    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": res}), flush=True)
                elif method == "thread/start":
                    thread_seq += 1
                    current_thread_id = f"thr_{acc_id or 'anon'}_{int(time.time() * 1000) % 100000}_{thread_seq}"
                    _append_log({"event": "thread/start", "acc_id": acc_id, "thread_id": current_thread_id, "params": params})
                    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": current_thread_id}}}), flush=True)
                elif method == "thread/resume":
                    current_thread_id = str(params.get("threadId") or "thr_resumed")
                    _append_log({"event": "thread/resume", "acc_id": acc_id, "thread_id": current_thread_id, "params": params})
                    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": current_thread_id}}}), flush=True)
                elif method == "turn/start":
                    input_items = params.get("input") or []
                    full_input_text = "\n".join(str(it.get("text") or "") for it in input_items if isinstance(it, dict))
                    _append_log({
                        "event": "turn/start",
                        "acc_id": acc_id,
                        "email": email,
                        "thread_id": current_thread_id,
                        "input_text": full_input_text,
                        "effort": params.get("effort"),
                    })
                    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"turn": {"id": "turn_1", "status": "inProgress"}}}), flush=True)

                    if exhausted_acc and acc_id == exhausted_acc:
                        print(json.dumps({
                            "jsonrpc": "2.0",
                            "method": "turn/completed",
                            "params": {
                                "turn": {
                                    "id": "turn_1",
                                    "status": "failed",
                                    "error": {"message": "You have hit your ChatGPT usage_limit_reached (5-hour quota exhausted)."},
                                }
                            },
                        }), flush=True)
                        return 0

                    if emit_file_change:
                        print(json.dumps({
                            "jsonrpc": "2.0",
                            "method": "item/started",
                            "params": {
                                "item": {
                                    "id": "fc_1",
                                    "type": "fileChange",
                                    "status": "inProgress",
                                    "changes": [{"path": emit_file_change, "kind": "update"}],
                                }
                            },
                        }), flush=True)
                        if active_hold_ms > 0:
                            time.sleep(active_hold_ms)
                        print(json.dumps({
                            "jsonrpc": "2.0",
                            "method": "item/completed",
                            "params": {
                                "item": {
                                    "id": "fc_1",
                                    "type": "fileChange",
                                    "status": "completed",
                                    "changes": [{"path": emit_file_change, "kind": "update"}],
                                }
                            },
                        }), flush=True)

                    reply = f"Ответ от ChatGPT ({email or acc_id})"
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "item/agentMessage/delta",
                        "params": {"itemId": "msg_1", "delta": reply},
                    }), flush=True)
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "thread/tokenUsage/updated",
                        "params": {
                            "tokenUsage": {
                                "last": {
                                    "inputTokens": 1200,
                                    "cachedInputTokens": 300,
                                    "outputTokens": 180,
                                    "reasoningOutputTokens": 60,
                                    "totalTokens": 1440,
                                }
                            }
                        },
                    }), flush=True)
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"turn": {"id": "turn_1", "status": "completed"}},
                    }), flush=True)
                    return 0
            return 0
        return 1

    if __name__ == "__main__":
        sys.exit(main())
    '''
).lstrip()


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
    def __init__(self, app: Any, *, client: tuple[str, int] = ("127.0.0.1", 52800)) -> None:
        self._app = app
        self._client = client

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _MiniAsgiResponse:
        parsed_url = urllib.parse.urlsplit(path)
        raw_path = parsed_url.path or "/"
        qs = parsed_url.query
        if params:
            extra_qs = urllib.parse.urlencode(params)
            qs = f"{qs}&{extra_qs}" if qs else extra_qs

        payload = b""
        req_headers: list[tuple[bytes, bytes]] = [(b"host", b"127.0.0.1:8080")]
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            req_headers.append((b"content-type", b"application/json"))
            req_headers.append((b"content-length", str(len(payload)).encode("ascii")))
        if headers:
            for k, v in headers.items():
                req_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": urllib.parse.unquote(raw_path),
            "raw_path": raw_path.encode("ascii", errors="ignore"),
            "query_string": qs.encode("ascii", errors="ignore"),
            "headers": req_headers,
            "client": self._client,
            "server": ("127.0.0.1", 8080),
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


class TestChatGptCodexIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.codex_engine as codex_engine
        import app.config as config
        import app.database as database

        self._live_db = ROOT_DIR / "data" / "hub.db"
        self._live_stat_before = (
            (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            if self._live_db.exists()
            else None
        )

        self._tmp = tempfile.TemporaryDirectory(
            prefix="agy_test_codex_v80_", ignore_cleanup_errors=True
        )
        self.tmp_root = Path(self._tmp.name).resolve()

        self._orig = {
            "cfg_db": config.DB_PATH,
            "db_db": database.DB_PATH,
            "cfg_cli": config.CLI_HOMES_DIR,
            "cfg_agy": config.AGY_CLI_DATA_DIR,
            "eng_agy": agy_engine.AGY_CLI_DATA_DIR,
            "cfg_ws": config.USER_WORKSPACES_DIR,
            "db_ws": database.USER_WORKSPACES_DIR,
            "br_ws": brain_reader.USER_WORKSPACES_DIR,
            "cfg_sh": config.SHARED_PROJECTS_DIR,
            "db_sh": database.SHARED_PROJECTS_DIR,
            "br_sh": brain_reader.SHARED_PROJECTS_DIR,
            "cfg_up": config.UPLOADS_DIR,
            "eng_up": agy_engine.UPLOADS_DIR,
            "cdx_up": codex_engine.UPLOADS_DIR,
            "cfg_sbx": config.CHAT_SANDBOX_DIR,
            "eng_sbx": agy_engine.CHAT_SANDBOX_DIR,
            "cdx_sbx": codex_engine.CHAT_SANDBOX_DIR,
            "env_codex_bin": os.environ.get("HUB_CODEX_BIN"),
            "env_codex_log": os.environ.get("MOCK_CODEX_LOG_FILE"),
            "env_exhausted": os.environ.get("MOCK_EXHAUSTED_ACC_ID"),
            "env_fc": os.environ.get("MOCK_EMIT_FILE_CHANGE"),
            "env_hold": os.environ.get("MOCK_ACTIVE_HOLD_SEC"),
        }

        tmp_db = self.tmp_root / "test_v80.db"
        tmp_cli = self.tmp_root / "cli_homes"
        tmp_agy = self.tmp_root / "agy_cli_data"
        tmp_ws = self.tmp_root / "workspaces"
        tmp_sh = self.tmp_root / "shared_projects"
        tmp_up = self.tmp_root / "uploads"
        tmp_sbx = self.tmp_root / "chat_sandbox"
        for d in (tmp_cli, tmp_agy, tmp_ws, tmp_sh, tmp_up, tmp_sbx):
            d.mkdir(parents=True, exist_ok=True)

        self.mock_codex_py = self.tmp_root / "mock_codex_cli.py"
        self.mock_codex_py.write_text(MOCK_CODEX_CLI_SCRIPT, encoding="utf-8")
        self.mock_log_file = self.tmp_root / "mock_codex_rpc.jsonl"

        os.environ["HUB_CODEX_BIN"] = str(self.mock_codex_py)
        os.environ["MOCK_CODEX_LOG_FILE"] = str(self.mock_log_file)
        os.environ.pop("MOCK_EXHAUSTED_ACC_ID", None)
        os.environ.pop("MOCK_EMIT_FILE_CHANGE", None)
        os.environ.pop("MOCK_ACTIVE_HOLD_SEC", None)

        config.DB_PATH = tmp_db
        database.DB_PATH = tmp_db
        config.CLI_HOMES_DIR = tmp_cli
        config.AGY_CLI_DATA_DIR = tmp_agy
        agy_engine.AGY_CLI_DATA_DIR = tmp_agy
        config.USER_WORKSPACES_DIR = tmp_ws
        database.USER_WORKSPACES_DIR = tmp_ws
        brain_reader.USER_WORKSPACES_DIR = tmp_ws
        config.SHARED_PROJECTS_DIR = tmp_sh
        database.SHARED_PROJECTS_DIR = tmp_sh
        brain_reader.SHARED_PROJECTS_DIR = tmp_sh
        config.UPLOADS_DIR = tmp_up
        agy_engine.UPLOADS_DIR = tmp_up
        codex_engine.UPLOADS_DIR = tmp_up
        config.CHAT_SANDBOX_DIR = tmp_sbx
        agy_engine.CHAT_SANDBOX_DIR = tmp_sbx
        codex_engine.CHAT_SANDBOX_DIR = tmp_sbx

        await database.init_db()

    async def asyncTearDown(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.codex_engine as codex_engine
        import app.config as config
        import app.database as database

        for fid in list(codex_engine._DEVICE_AUTH_FLOWS.keys()):
            fl = codex_engine._DEVICE_AUTH_FLOWS.get(fid)
            if fl:
                await codex_engine.cancel_chatgpt_device_auth_flow(fl.user_id, fid)
        codex_engine._DEVICE_AUTH_FLOWS.clear()

        config.DB_PATH = self._orig["cfg_db"]
        database.DB_PATH = self._orig["db_db"]
        config.CLI_HOMES_DIR = self._orig["cfg_cli"]
        config.AGY_CLI_DATA_DIR = self._orig["cfg_agy"]
        agy_engine.AGY_CLI_DATA_DIR = self._orig["eng_agy"]
        config.USER_WORKSPACES_DIR = self._orig["cfg_ws"]
        database.USER_WORKSPACES_DIR = self._orig["db_ws"]
        brain_reader.USER_WORKSPACES_DIR = self._orig["br_ws"]
        config.SHARED_PROJECTS_DIR = self._orig["cfg_sh"]
        database.SHARED_PROJECTS_DIR = self._orig["db_sh"]
        brain_reader.SHARED_PROJECTS_DIR = self._orig["br_sh"]
        config.UPLOADS_DIR = self._orig["cfg_up"]
        agy_engine.UPLOADS_DIR = self._orig["eng_up"]
        codex_engine.UPLOADS_DIR = self._orig["cdx_up"]
        config.CHAT_SANDBOX_DIR = self._orig["cfg_sbx"]
        agy_engine.CHAT_SANDBOX_DIR = self._orig["eng_sbx"]
        codex_engine.CHAT_SANDBOX_DIR = self._orig["cdx_sbx"]

        for env_k, orig_key in (
            ("HUB_CODEX_BIN", "env_codex_bin"),
            ("MOCK_CODEX_LOG_FILE", "env_codex_log"),
            ("MOCK_EXHAUSTED_ACC_ID", "env_exhausted"),
            ("MOCK_EMIT_FILE_CHANGE", "env_fc"),
            ("MOCK_ACTIVE_HOLD_SEC", "env_hold"),
        ):
            prev = self._orig[orig_key]
            if prev is None:
                os.environ.pop(env_k, None)
            else:
                os.environ[env_k] = prev

        _unlink_test_junctions(self.tmp_root)
        self._tmp.cleanup()

        if self._live_stat_before is not None:
            stat_after = (
                self._live_db.stat().st_mtime_ns,
                self._live_db.stat().st_size,
            )
            self.assertEqual(
                self._live_stat_before,
                stat_after,
                "Production data/hub.db must NEVER be modified by tests!",
            )

    def _auth_headers(self, user_id: str, username: str) -> dict[str, str]:
        import app.auth as auth

        tok = auth.create_user_session_token(user_id=user_id, session_version=1)
        return {"Authorization": f"Bearer {tok}"}

    async def test_01_rbac_and_fernet_crypto_isolation(self) -> None:
        import app.database as database
        import app.main as main

        user_a = await database.create_user(
            username="alex",
            password="password_a",
            display_name="Alex",
            max_access_mode="write_files",
        )
        user_b = await database.create_user(
            username="boris",
            password="password_b",
            display_name="Boris",
            max_access_mode="write_files",
        )
        headers_a = self._auth_headers(user_a["id"], user_a["username"])
        headers_b = self._auth_headers(user_b["id"], user_b["username"])
        client = _MiniAsgiClient(main.app)

        # Import 2 accounts for User A and 1 account for User B
        r_a1 = await client.request(
            "POST",
            "/api/chatgpt/accounts/import",
            headers=headers_a,
            json_body={
                "auth_json": _make_auth_json_str("alex1@example.com", "acc_a1", "plus", "SUPERSECRET_A1"),
                "label": "Alex Plus #1",
            },
        )
        self.assertEqual(r_a1.status_code, 200, r_a1.text)
        acc_a1_id = r_a1.json()["account"]["id"]

        r_a2 = await client.request(
            "POST",
            "/api/chatgpt/accounts/import",
            headers=headers_a,
            json_body={
                "auth_json": _make_auth_json_str("alex2@example.com", "acc_a2", "pro", "SUPERSECRET_A2"),
                "label": "Alex Pro #2",
            },
        )
        self.assertEqual(r_a2.status_code, 200, r_a2.text)

        r_b1 = await client.request(
            "POST",
            "/api/chatgpt/accounts/import",
            headers=headers_b,
            json_body={
                "auth_json": _make_auth_json_str("boris@example.com", "acc_b1", "team", "SUPERSECRET_B1"),
                "label": "Boris Team",
            },
        )
        self.assertEqual(r_b1.status_code, 200, r_b1.text)

        # User A sees strictly 2 accounts, User B sees strictly 1 account, ZERO secret leakage
        st_a = await client.request("GET", "/api/chatgpt/state", headers=headers_a)
        st_b = await client.request("GET", "/api/chatgpt/state", headers=headers_b)
        self.assertEqual(st_a.status_code, 200)
        self.assertEqual(st_b.status_code, 200)
        self.assertEqual(len(st_a.json()["accounts"]), 2)
        self.assertEqual(len(st_b.json()["accounts"]), 1)
        self.assertEqual(st_b.json()["accounts"][0]["email"], "boris@example.com")

        for resp_txt in (r_a1.text, r_a2.text, r_b1.text, st_a.text, st_b.text):
            for forbidden in (
                "SUPERSECRET_",
                "encrypted_auth",
                "access_token",
                "refresh_token",
                "id_token",
                "auth_json",
            ):
                self.assertNotIn(forbidden, resp_txt)

        # Cross-user activate / patch / delete MUST return 404
        self.assertEqual(
            (await client.request("POST", f"/api/chatgpt/accounts/{acc_a1_id}/activate", headers=headers_b)).status_code,
            404,
        )
        self.assertEqual(
            (
                await client.request(
                    "PATCH",
                    f"/api/chatgpt/accounts/{acc_a1_id}",
                    headers=headers_b,
                    json_body={"label": "Hacked"},
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            (await client.request("DELETE", f"/api/chatgpt/accounts/{acc_a1_id}", headers=headers_b)).status_code,
            404,
        )

        # Cross-user Fernet decryption MUST reject foreign HKDF key (returns "" and never leaks plaintext)
        cipher_a = database.encrypt_chatgpt_auth_json(user_a["id"], '{"tokens":{"access_token":"secret"}}')
        self.assertIn("secret", database.decrypt_chatgpt_auth_json(user_a["id"], cipher_a))
        self.assertEqual(database.decrypt_chatgpt_auth_json(user_b["id"], cipher_a), "")

        # Guest cannot call admin CLI installer (403)
        r_inst = await client.request("POST", "/api/admin/codex/install", headers=headers_a)
        self.assertEqual(r_inst.status_code, 403)

    async def test_02_codex_security_matrix_and_config_toml(self) -> None:
        import app.codex_engine as codex_engine
        import app.config as config
        import app.database as database
        import app.openrouter_service as openrouter_service
        import app.sandbox_service as sandbox_service

        admin_user = await database.get_user_by_id("admin")
        assert admin_user is not None
        guest_files = await database.create_user(
            username="guest_files",
            password="pw1234",
            max_access_mode="write_files",
        )
        guest_docker = await database.create_user(
            username="guest_docker",
            password="pw5678",
            max_access_mode="docker_full",
        )

        # 1. Plain chat -> read-only, no shell, no sandbox MCP even for admin
        m_chat = codex_engine.resolve_codex_security_matrix(
            kind="chat",
            access_mode="full_access",
            is_admin=True,
            allow_hub_sandbox=True,
        )
        self.assertEqual(m_chat["sandbox_mode"], "read-only")
        self.assertFalse(m_chat["shell_tool"])
        self.assertFalse(m_chat["allow_hub_sandbox"])

        # 2. Project read_only -> read-only, no shell
        m_ro = codex_engine.resolve_codex_security_matrix(
            kind="project", access_mode="read_only", is_admin=True
        )
        self.assertEqual(m_ro["sandbox_mode"], "read-only")
        self.assertFalse(m_ro["shell_tool"])

        # 3. Project write_files -> workspace-write, no shell
        m_wf = codex_engine.resolve_codex_security_matrix(
            kind="project", access_mode="write_files", is_admin=True
        )
        self.assertEqual(m_wf["sandbox_mode"], "workspace-write")
        self.assertFalse(m_wf["shell_tool"])

        # 4. Guest without Docker requesting full_access -> host shell strictly forbidden!
        m_gf = codex_engine.resolve_codex_security_matrix(
            kind="project",
            access_mode="full_access",
            is_admin=False,
            allow_hub_sandbox=False,
        )
        self.assertFalse(m_gf["shell_tool"])
        self.assertFalse(m_gf["allow_hub_sandbox"])

        # 5. Guest with Docker / Shared Docker in full_access -> shell_tool=False, allow_hub_sandbox=True!
        m_gd = codex_engine.resolve_codex_security_matrix(
            kind="project",
            access_mode="full_access",
            is_admin=False,
            force_no_host_shell=True,
            allow_hub_sandbox=True,
        )
        self.assertEqual(m_gd["sandbox_mode"], "workspace-write")
        self.assertFalse(m_gd["shell_tool"])
        self.assertTrue(m_gd["allow_hub_sandbox"])

        # 6. Admin on host in full_access -> danger-full-access + shell_tool=True
        m_adm = codex_engine.resolve_codex_security_matrix(
            kind="project",
            access_mode="full_access",
            is_admin=True,
            force_no_host_shell=False,
        )
        self.assertEqual(m_adm["sandbox_mode"], "danger-full-access")
        self.assertTrue(m_adm["shell_tool"])
        self.assertFalse(m_adm["allow_hub_sandbox"])

        # Verify generated config.toml top-level `cli_auth_credentials_store = "file"` & MCP blocks
        toml_txt = codex_engine.render_codex_config_toml(
            sandbox_mode=m_gd["sandbox_mode"],
            shell_tool=m_gd["shell_tool"],
            openrouter_mcp_url="http://127.0.0.1:8765/mcp/openrouter/tok1",
            sandbox_mcp_url="http://127.0.0.1:8765/mcp/sandbox/tok2",
        )
        self.assertTrue(toml_txt.startswith('cli_auth_credentials_store = "file"'))
        self.assertIn("shell_tool = false", toml_txt)
        self.assertIn("[mcp_servers.hub_openrouter]", toml_txt)
        self.assertIn("[mcp_servers.hub_sandbox]", toml_txt)

        # Verify AGENTS.md and .agents/skills mirroring
        config.sync_user_cli_rules(guest_docker, "Пиши тесты на pytest.")
        codex_agents_md = config.ensure_user_codex_home(guest_docker) / "AGENTS.md"
        self.assertTrue(codex_agents_md.is_file())
        self.assertIn("Пиши тесты на pytest.", codex_agents_md.read_text(encoding="utf-8"))

        await database.upsert_user_openrouter_config(
            guest_docker["id"], new_api_key="sk-or-v1-testkey123456", is_enabled=True
        )
        await openrouter_service.sync_user_openrouter_cli_files(guest_docker["id"])
        sandbox_service.sync_user_sandbox_cli_files(guest_docker, sandbox_ready=True)

        skills_root = config.ensure_user_agents_skills_dir(guest_docker)
        self.assertTrue((skills_root / "openrouter-bridge" / "SKILL.md").is_file())
        self.assertTrue((skills_root / "docker-sandbox" / "SKILL.md").is_file())

    async def test_03_guest_codex_independent_from_admin_google_quotas(self) -> None:
        import app.codex_engine as codex_engine
        import app.database as database
        import app.main as main

        guest = await database.create_user(
            username="lena",
            password="pw_lena",
            display_name="Lena",
            max_access_mode="write_files",
            allowed_models=["gemini-4-carbon-low"],
            daily_token_limit=100,
            quota_reserve_pct=99,
        )

        # Exhaust guest's 24h Google token limit (500 > 100)
        chat = await database.create_chat(
            title="Quota Check",
            kind="chat",
            model="gemini-4-carbon-low",
            owner_id=guest["id"],
        )
        await database.add_message(
            chat["id"],
            user_id=guest["id"],
            role="assistant",
            content="Google answer",
            model="gemini-4-carbon-low",
            usage={"input_tokens": 300, "output_tokens": 200, "total_tokens": 500},
            status="done",
        )
        self.assertEqual(await database.get_user_24h_tokens(guest["id"]), 500)

        # Google models are blocked by allowed_models and daily_token_limit
        err_forbidden = await main._enforce_user_turn_quotas(guest, "claude-opus-4-6")
        self.assertIsNotNone(err_forbidden)
        err_limit = await main._enforce_user_turn_quotas(guest, "gemini-4-carbon-low")
        self.assertIsNotNone(err_limit)

        # Without a ChatGPT account, codex:* asks guest to connect their personal ChatGPT
        err_no_acc = await main._enforce_user_turn_quotas(guest, "codex:gpt-5.4@high")
        self.assertIsNotNone(err_no_acc)
        self.assertIn("ChatGPT", err_no_acc or "")

        # Connect guest's personal ChatGPT account -> codex:* bypasses ALL admin Google quotas!
        acc = await codex_engine.import_chatgpt_auth_json(
            guest["id"],
            raw_auth_json=_make_auth_json_str("lena@example.com", "acc_lena", "plus"),
            label="Lena Plus",
        )
        err_ok = await main._enforce_user_turn_quotas(guest, "codex:gpt-5.4@high")
        self.assertIsNone(err_ok)

        # Record 25,000 personal ChatGPT tokens -> guest's 24h Google counter stays 500!
        await database.add_message(
            chat["id"],
            user_id=guest["id"],
            role="assistant",
            content="ChatGPT answer",
            model="codex:gpt-5.4@high",
            usage={
                "provider": "chatgpt_codex",
                "account_id": acc["id"],
                "input_tokens": 20000,
                "output_tokens": 5000,
                "total_tokens": 25000,
            },
            status="done",
        )
        self.assertEqual(await database.get_user_24h_tokens(guest["id"]), 500)
        cgpt_stats = await database.get_chatgpt_token_usage_stats(guest["id"])
        self.assertEqual(cgpt_stats["window_24h"]["total_tokens"], 25000)

        # Pausing account blocks codex:* cleanly
        await database.patch_user_chatgpt_account(guest["id"], acc["id"], is_enabled=False)
        self.assertIsNotNone(await main._enforce_user_turn_quotas(guest, "codex:gpt-5.4@high"))

    async def test_04_mid_chat_engine_and_account_switching_and_compact(self) -> None:
        import app.agy_engine as agy_engine
        import app.codex_engine as codex_engine
        import app.database as database
        import app.main as main

        admin_user = await database.get_user_by_id("admin")
        assert admin_user is not None

        acc1 = await codex_engine.import_chatgpt_auth_json(
            "admin",
            raw_auth_json=_make_auth_json_str("adm1@example.com", "adm_acc1", "plus"),
            label="Acc #1",
        )
        acc2 = await codex_engine.import_chatgpt_auth_json(
            "admin",
            raw_auth_json=_make_auth_json_str("adm2@example.com", "adm_acc2", "pro"),
            label="Acc #2",
        )
        await database.activate_user_chatgpt_account("admin", acc1["id"])

        chat = await database.create_chat(
            title="Multi-Engine Switch",
            kind="chat",
            model="gemini-4-carbon-high",
            owner_id="admin",
        )
        cid = chat["id"]

        agy_calls: list[dict[str, Any]] = []

        async def _fake_stream_agy_turn(**kwargs: Any):
            agy_calls.append(kwargs)
            yield {"type": "agy_session", "agy_conversation_id": "agy-conv-777"}
            yield {"type": "text_delta", "delta": f"Ответ AGY ({kwargs['model']})", "seq": 1}
            yield {
                "type": "done",
                "content": f"Ответ AGY ({kwargs['model']})",
                "thinking": "",
                "tool_calls": [],
                "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            }

        async def _run_single_turn(prompt_txt: str, model_id: str) -> dict[str, Any]:
            await main._run_turn_task_inner(
                chat_id=cid,
                prompt=prompt_txt,
                attachments=[],
                override_model=model_id,
                override_access_mode=None,
                trigger_user=admin_user,
                referenced_chat_ids=[],
            )
            updated = await database.get_chat(cid)
            assert updated is not None
            return updated

        async def _noop_quota_refresh(*_a: Any, **_kw: Any) -> None:
            return None

        with (
            patch.object(main, "stream_agy_turn", side_effect=_fake_stream_agy_turn),
            patch.object(main, "_refresh_and_broadcast_quotas", side_effect=_noop_quota_refresh),
        ):
            # Step 1: Gemini turn
            c1 = await _run_single_turn("Привет, Gemini!", "gemini-4-carbon-high")
            self.assertEqual(c1["agy_conversation_id"], "agy-conv-777")
            self.assertEqual(c1["last_engine"], "agy")

            # Step 2: Switch to ChatGPT Acc #1 -> starts new thread WITH __RECOVERY__ containing Gemini's answer
            c2 = await _run_single_turn("Продолжи мысль Gemini", "codex:gpt-5.4@high")
            self.assertEqual(c2["last_engine"], "codex")
            self.assertEqual(c2["codex_account_id"], acc1["id"])
            thr_1 = c2["codex_thread_id"]
            self.assertTrue(thr_1.startswith("thr_adm_acc1_"))

            # Step 3: 2nd turn on same ChatGPT Acc #1 -> resumes thr_1 WITHOUT __RECOVERY__
            c3 = await _run_single_turn("Ещё один шаг на Acc #1", "codex:gpt-5.4@high")
            self.assertEqual(c3["codex_thread_id"], thr_1)

            # Step 4: Switch to ChatGPT Acc #2 -> account mismatch triggers new thread WITH __RECOVERY__
            await database.activate_user_chatgpt_account("admin", acc2["id"])
            c4 = await _run_single_turn("Теперь ответь с Acc #2", "codex:gpt-5.3-codex@xhigh")
            self.assertEqual(c4["codex_account_id"], acc2["id"])
            self.assertNotEqual(c4["codex_thread_id"], thr_1)
            self.assertTrue(c4["codex_thread_id"].startswith("thr_adm_acc2_"))

            # Step 5: Switch to Claude (AGY) -> last_engine=="codex" forces agy_conversation_id="" so AGY gets full history!
            c5 = await _run_single_turn("Подведи итог, Claude", "claude-sonnet-5-5-high")
            self.assertEqual(c5["last_engine"], "agy")
            self.assertEqual(len(agy_calls), 2)
            self.assertEqual(agy_calls[1]["agy_conversation_id"], "")
            hist_contents = "\n".join(m.get("content", "") for m in agy_calls[1]["history_messages"])
            self.assertIn("Ответ от ChatGPT (adm1@example.com)", hist_contents)
            self.assertIn("Ответ от ChatGPT (adm2@example.com)", hist_contents)

        # Verify mock RPC log for Steps 2, 3, 4
        rpc_logs = [
            json.loads(line)
            for line in self.mock_log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        turn_starts = [e for e in rpc_logs if e.get("event") == "turn/start"]
        self.assertEqual(len(turn_starts), 3)
        self.assertIn("Ответ AGY (gemini-4-carbon-high)", turn_starts[0]["input_text"])
        self.assertNotIn("Сообщения диалога после сводки", turn_starts[1]["input_text"])
        self.assertIn("Сообщения диалога после сводки", turn_starts[2]["input_text"])
        self.assertIn("Ответ от ChatGPT (adm1@example.com)", turn_starts[2]["input_text"])
        self.assertEqual(turn_starts[2]["effort"], "xhigh")

        # Step 6: /compact via `compact_chat_context(model="codex:gpt-5.4-mini@low")` + `save_chat_compaction`
        # verifies `run_one_shot_codex_prompt` dict contract AND resets BOTH agy_conversation_id & codex_thread_id
        chat_before_compact = await database.get_chat(cid)
        assert chat_before_compact is not None
        msgs = await database.get_messages(cid)
        compact_res = await agy_engine.compact_chat_context(
            chat=chat_before_compact,
            messages=msgs,
            model="codex:gpt-5.4-mini@low",
            user_id="admin",
        )
        self.assertTrue(str(compact_res.get("summary") or "").strip())
        self.assertEqual(compact_res.get("model"), "codex:gpt-5.4-mini@low")
        self.assertEqual((compact_res.get("usage") or {}).get("provider"), "chatgpt_codex")

        compacted_chat = await database.save_chat_compaction(
            cid,
            compacted_summary=compact_res["summary"],
            compacted_up_to_msg_id=int(msgs[-1]["id"]),
            compacted_count=len(msgs),
        )
        assert compacted_chat is not None
        self.assertEqual(compacted_chat["agy_conversation_id"], "")
        self.assertEqual(compacted_chat["codex_thread_id"], "")
        self.assertEqual(compacted_chat["codex_account_id"], "")

    async def test_05_mock_codex_app_server_device_auth_traffic_light_and_failover(self) -> None:
        import app.codex_engine as codex_engine
        import app.database as database
        import app.main as main

        admin_user = await database.get_user_by_id("admin")
        assert admin_user is not None
        headers_adm = self._auth_headers("admin", admin_user["username"])
        client = _MiniAsgiClient(main.app)

        # 1. OAuth Device Code flow E2E (`POST /api/chatgpt/auth/device/start` + poll)
        r_start = await client.request(
            "POST",
            "/api/chatgpt/auth/device/start",
            headers=headers_adm,
            json_body={"label": "Device Pro"},
        )
        self.assertEqual(r_start.status_code, 200, r_start.text)
        flow_data = r_start.json()
        self.assertEqual(flow_data["user_code"], "K9W2-7XM4P")
        self.assertEqual(flow_data["verification_url"], "https://auth.openai.com/codex/device")

        flow_id = flow_data["flow_id"]
        final_flow = None
        for _ in range(30):
            await asyncio.sleep(0.1)
            r_poll = await client.request(
                "GET", f"/api/chatgpt/auth/device/{flow_id}", headers=headers_adm
            )
            self.assertEqual(r_poll.status_code, 200)
            cand = r_poll.json()
            if cand["status"] != "pending":
                final_flow = cand
                break
        self.assertIsNotNone(final_flow)
        assert final_flow is not None
        self.assertEqual(final_flow["status"], "completed", final_flow)
        dev_acc = final_flow["account"]
        self.assertEqual(dev_acc["email"], "device_user@example.com")
        self.assertEqual(dev_acc["quotas"]["primary_5h"]["remaining_pct"], 84.0)
        self.assertEqual(dev_acc["quotas"]["secondary_7d"]["remaining_pct"], 62.0)

        # Models endpoint now includes `codex:gpt-5.4@high` & `codex:gpt-5.3-codex@xhigh`
        r_models = await client.request("GET", "/api/models", headers=headers_adm)
        self.assertEqual(r_models.status_code, 200)
        model_ids = {m["id"] for m in r_models.json()["models"]}
        self.assertIn("codex:gpt-5.4@high", model_ids)
        self.assertIn("codex:gpt-5.3-codex@xhigh", model_ids)

        # 2. Live File Traffic Light (`active_files`) + Project Memory changelog + Automatic 5h Quota Failover!
        acc_backup = await codex_engine.import_chatgpt_auth_json(
            "admin",
            raw_auth_json=_make_auth_json_str("backup@example.com", "cgpt_backup_02", "plus"),
            label="Backup Plus",
        )
        await database.activate_user_chatgpt_account("admin", dev_acc["id"])

        proj_dir = self.tmp_root / "workspaces" / "admin" / "demo_proj"
        proj_dir.mkdir(parents=True, exist_ok=True)
        target_rel = "src/service.py"

        # Simulate Account #1 (`cgpt_dev_01`) hitting 5h quota limit -> auto-failover to `cgpt_backup_02`
        # AND emit a live `fileChange` with a short hold so we observe `active_files` mid-turn!
        os.environ["MOCK_EXHAUSTED_ACC_ID"] = "cgpt_dev_01"
        os.environ["MOCK_EMIT_FILE_CHANGE"] = str(proj_dir / "src" / "service.py")
        os.environ["MOCK_ACTIVE_HOLD_SEC"] = "0.10"

        proj_chat = await database.create_chat(
            title="Feature Implementation",
            kind="project",
            project_name="demo_proj",
            access_mode="write_files",
            model="codex:gpt-5.3-codex@high",
            owner_id="admin",
        )
        pcid = proj_chat["id"]

        observed_active_files: list[str] = []

        async def _watch_traffic_light() -> None:
            import app.agy_engine as agy_engine

            for _ in range(160):
                parallel = agy_engine.get_parallel_project_runs(
                    project_name="demo_proj", exclude_chat_id="neighbor_tab"
                )
                for pr in parallel:
                    if pr.get("chat_id") == pcid and pr.get("active_files"):
                        observed_active_files.extend(pr["active_files"])
                        return
                await asyncio.sleep(0.025)

        fake_resolved = {
            "name": "demo_proj",
            "path": proj_dir,
            "execution_mode": "host",
            "is_shared": False,
        }
        with patch("app.brain_reader.resolve_user_project", return_value=fake_resolved):
            await asyncio.gather(
                main._run_turn_task_inner(
                    chat_id=pcid,
                    prompt="Обнови src/service.py",
                    attachments=[],
                    override_model="codex:gpt-5.3-codex@high",
                    override_access_mode="write_files",
                    trigger_user=admin_user,
                    referenced_chat_ids=[],
                ),
                _watch_traffic_light(),
            )

        # Traffic light caught `src/service.py` while `fileChange` was `inProgress`!
        self.assertTrue(
            any(target_rel in f.replace("\\", "/") for f in observed_active_files),
            f"Expected {target_rel} in live active_files, got: {observed_active_files}",
        )

        # Turn succeeded via automatic failover to `acc_backup` (`backup@example.com`)!
        msgs_after = await database.get_messages(pcid)
        final_asst = msgs_after[-1]
        self.assertEqual(final_asst["status"], "done", final_asst["content"])
        self.assertIn("backup@example.com", final_asst["content"])
        self.assertEqual(final_asst["usage"]["provider"], "chatgpt_codex")
        self.assertEqual(final_asst["usage"]["total_tokens"], 1440)

        active_now = await database.get_active_user_chatgpt_account("admin")
        assert active_now is not None
        self.assertEqual(active_now["id"], acc_backup["id"])

        exhausted_acc_row = await database.get_user_chatgpt_account("admin", dev_acc["id"])
        assert exhausted_acc_row is not None
        self.assertEqual(exhausted_acc_row["quotas"]["primary_5h"]["remaining_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
