"""Regression suite v155: Project `.env` & secrets manager tab, AST non-destructive patching,
HUB_SECRET_KEY self-hub protection, RBAC, `.env.example` sync, `.secrets/` inventory,
guardrails update, Node VM UI interactions & Chromium responsive geometry.

Runs strictly inside temporary directories and temporary SQLite fixtures — never touches live
`data/hub.db`, `users/`, `shared_projects/`, real `.env` files, or network endpoints.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = _TESTS_DIR.parent
for _p in (str(ROOT_DIR), str(_TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_frontend_contracts import STATIC, ShellParser, run_node
from test_ui_workspace_minimal_v142 import run_workspace_chromium


async def _asgi_request(
    app: Any,
    method: str,
    url: str,
    *,
    cookie_header: str = "",
    json_body: Any = None,
) -> tuple[int, dict[str, Any]]:
    parsed = urlsplit(url)
    raw_body = json.dumps(json_body).encode("utf-8") if json_body is not None else b""
    headers: list[tuple[bytes, bytes]] = [(b"host", b"testserver")]
    if cookie_header:
        headers.append((b"cookie", cookie_header.encode("utf-8")))
    if json_body is not None:
        headers.append((b"content-type", b"application/json"))
        headers.append((b"content-length", str(len(raw_body)).encode("ascii")))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": parsed.path,
        "raw_path": parsed.path.encode("utf-8"),
        "query_string": parsed.query.encode("utf-8"),
        "headers": headers,
        "client": ("127.0.0.1", 51455),
        "server": ("testserver", 80),
    }

    sent_request = False
    status_code = 500
    body_chunks: list[bytes] = []

    async def receive() -> dict[str, Any]:
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": raw_body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = int(message["status"])
        elif message["type"] == "http.response.body":
            body_chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    raw_resp = b"".join(body_chunks).decode("utf-8", errors="replace")
    data = json.loads(raw_resp) if raw_resp.strip() else {}
    return status_code, data


# ---------------------------------------------------------------------------
# 1. Step 1: Careful `.env` Guardrails & Open AI Access to User `.env`
# ---------------------------------------------------------------------------


def test_guardrails_careful_env_rule_and_workspace_mcp_user_env_access(tmp_path: Path) -> None:
    from app import config, workspace_mcp

    text = config.DEFAULT_PROJECT_GUARDRAILS
    words = [w for w in text.split() if w.strip()]
    assert 150 <= len(words) <= 255, f"Word count out of [150, 255]: {len(words)}"
    for required in (
        ".env",
        ".env.example",
        ".secrets/",
        "не перезатирая",
        "не обнуляя",
        "открытым текстом в чат",
    ):
        assert required in text, f"Missing guardrail phrase: {required}"

    # AI in regular user project MUST keep free access to `.env`
    user_proj = tmp_path / "MyTradingBot"
    user_proj.mkdir()
    user_env = user_proj / ".env"
    user_env.write_text("BOT_TOKEN=abc\n", encoding="utf-8")
    assert workspace_mcp._is_forbidden_secret_path(user_env.resolve(), workspace_root=user_proj.resolve()) is False

    # Root Antigravity_Hub `.env` MUST stay blocked for AI
    hub_root = config.BASE_DIR.resolve(strict=False)
    hub_env = (hub_root / ".env").resolve(strict=False)
    assert workspace_mcp._is_forbidden_secret_path(hub_env, workspace_root=hub_root) is True


# ---------------------------------------------------------------------------
# 2. Step 2: AST Non-Destructive Patching, Safety Guards & Example Sync
# ---------------------------------------------------------------------------


def test_env_service_ast_non_destructive_entries_mode_and_path_guards(tmp_path: Path) -> None:
    from app import env_service

    proj = tmp_path / "AlphaProject"
    proj.mkdir()
    original = (
        "# === Main Application ===\n"
        "\n"
        "# Human-friendly service name\n"
        'APP_NAME="Alpha #1 Service"\n'
        "export DEBUG_MODE='true' # inline note\n"
        "# Secret API token\n"
        "API_TOKEN=old_secret_123\n"
        "OBSOLETE_FLAG=yes\n"
        "\n"
        "# Multiline quoted secret\n"
        'CERT_LINE="line1\\nline2 \\"quoted\\""\n'
    )
    (proj / ".env").write_text(original, encoding="utf-8")
    (proj / ".env.local").write_text("LOCAL_ONLY=1\n", encoding="utf-8")
    (proj / "backend").mkdir()
    (proj / "backend" / ".env").write_text("BACKEND_PORT=8000\n", encoding="utf-8")
    (proj / ".env.example").write_text("APP_NAME=\nNEW_FROM_EX=\n", encoding="utf-8")
    (proj / ".git").mkdir()
    (proj / ".git" / ".env").write_text("BAD=1\n", encoding="utf-8")
    (proj / "node_modules").mkdir()
    (proj / "node_modules" / ".env").write_text("BAD=2\n", encoding="utf-8")

    # Discovery finds root .env first, includes .env.local & backend/.env, excludes templates & blocked dirs
    discovered = env_service.discover_project_env_files(proj)
    paths = [d["path"] for d in discovered]
    assert paths[0] == ".env"
    assert ".env.local" in paths
    assert "backend/.env" in paths
    assert ".env.example" not in paths
    assert ".git/.env" not in paths
    assert "node_modules/.env" not in paths

    # Parse verifies comments, quotes, export, escapes, inline comments
    parsed = env_service.parse_dotenv_text(original)
    by_key = {e["key"]: e for e in parsed["entries"]}
    assert by_key["APP_NAME"]["value"] == "Alpha #1 Service"
    assert by_key["APP_NAME"]["comment"] == "Human-friendly service name"
    assert by_key["DEBUG_MODE"]["value"] == "true"
    assert by_key["DEBUG_MODE"]["has_export"] is True
    assert by_key["DEBUG_MODE"]["inline_comment"] == "# inline note"
    assert by_key["CERT_LINE"]["value"] == 'line1\nline2 "quoted"'

    # Patch in entries mode:
    # - keep APP_NAME, DEBUG_MODE, CERT_LINE untouched
    # - edit API_TOKEN with `#` and edge spaces (auto double-quoted!)
    # - delete OBSOLETE_FLAG
    # - add NEW_WEBHOOK with comment and quotes
    updated_entries = [
        {"key": "APP_NAME", "value": "Alpha #1 Service", "comment": "Human-friendly service name"},
        {"key": "DEBUG_MODE", "value": "true", "comment": ""},
        {"key": "API_TOKEN", "value": " tok #456 ", "comment": "Secret API token"},
        {"key": "CERT_LINE", "value": 'line1\nline2 "quoted"', "comment": "Multiline quoted secret"},
        {"key": "NEW_WEBHOOK", "value": 'https://ex.local/hook?a="1"', "comment": "Webhook endpoint"},
    ]
    res = env_service.save_project_env_file(
        proj,
        rel_file=".env",
        mode="entries",
        entries=updated_entries,
        ensure_gitignore=True,
    )
    assert res["ok"] is True
    assert res["gitignore_updated"] is True

    patched_text = (proj / ".env").read_text(encoding="utf-8")
    assert not patched_text.startswith("\ufeff")
    # 100% preservation of header comments, blank lines, quotes, export & inline comment!
    assert "# === Main Application ===\n\n# Human-friendly service name\n" in patched_text
    assert 'APP_NAME="Alpha #1 Service"' in patched_text
    assert "export DEBUG_MODE='true' # inline note" in patched_text
    assert "# Secret API token\n" in patched_text
    assert 'API_TOKEN=" tok #456 "' in patched_text
    assert "OBSOLETE_FLAG" not in patched_text
    assert 'CERT_LINE="line1\\nline2 \\"quoted\\""' in patched_text
    assert "# Webhook endpoint\n" in patched_text
    assert 'NEW_WEBHOOK="https://ex.local/hook?a=\\"1\\""' in patched_text

    # Re-parse verifies round-trip values match 100%
    reparsed = {e["key"]: e["value"] for e in env_service.parse_dotenv_text(patched_text)["entries"]}
    assert reparsed == {
        "APP_NAME": "Alpha #1 Service",
        "DEBUG_MODE": "true",
        "API_TOKEN": " tok #456 ",
        "CERT_LINE": 'line1\nline2 "quoted"',
        "NEW_WEBHOOK": 'https://ex.local/hook?a="1"',
    }

    # Path traversal, absolute path, blocked dirs, templates & oversize rejected
    with pytest.raises(PermissionError):
        env_service.resolve_safe_env_target(proj, "../.env")
    with pytest.raises(PermissionError):
        env_service.resolve_safe_env_target(proj, str(proj / ".env"))
    with pytest.raises(PermissionError):
        env_service.resolve_safe_env_target(proj, ".git/.env")
    with pytest.raises(PermissionError):
        env_service.resolve_safe_env_target(proj, ".venv/.env")
    with pytest.raises(ValueError):
        env_service.resolve_safe_env_target(proj, ".env.example")
    with pytest.raises(ValueError):
        env_service.save_project_env_file(proj, rel_file=".env", mode="raw", raw_content="A=1\x00B=2")
    with pytest.raises(ValueError):
        env_service.save_project_env_file(
            proj, rel_file=".env", mode="raw", raw_content="X=" + ("a" * (env_service.MAX_ENV_FILE_BYTES + 10))
        )


def test_env_service_example_sync_and_secrets_inventory(tmp_path: Path) -> None:
    from app import env_service

    proj = tmp_path / "BotProject"
    proj.mkdir()
    (proj / ".gitignore").write_text("*.pyc\n.secrets/\n", encoding="utf-8")
    (proj / ".env").write_text(
        "# Telegram section\n"
        "TG_TOKEN=super_live_secret_999\n"
        "\n"
        "# Proxy\n"
        'PROXY_URL="http://user:pass@127.0.0.1:10808"\n',
        encoding="utf-8",
    )
    (proj / ".env.example").write_text(
        "# Telegram section\n"
        "TG_TOKEN=\n"
        "# OpenAI key for assistant\n"
        "OPENAI_API_KEY=\n"
        "# Webhook secret\n"
        "WEBHOOK_SECRET=\n",
        encoding="utf-8",
    )

    # Create file secrets metadata fixtures
    (proj / ".secrets").mkdir()
    (proj / ".secrets" / "service_account.json").write_text('{"private_key": "DO_NOT_READ"}', encoding="utf-8")
    (proj / "client_secret_oauth.json").write_text('{"installed": {}}', encoding="utf-8")
    (proj / "google_credentials.json").write_text('{"token": "xyz"}', encoding="utf-8")
    (proj / "tls_cert.pem").write_text("-----BEGIN CERT-----", encoding="utf-8")
    (proj / "signing.key").write_text("\x2d----BEGIN PRIVATE KEY-----", encoding="utf-8")

    state0 = env_service.read_project_env_state(proj, ".env", gdrive_vault_active=True)
    assert state0["gdrive_vault_active"] is True
    assert state0["gitignore_protected"] is False
    missing_keys = [m["key"] for m in state0["example"]["missing_keys"]]
    assert missing_keys == ["OPENAI_API_KEY", "WEBHOOK_SECRET"]
    assert state0["example"]["missing_keys"][0]["comment"] == "OpenAI key for assistant"

    # Inventory lists all 5 file secrets with metadata only (no content key!)
    sf_by_path = {s["rel_path"]: s for s in state0["secret_files"]}
    assert set(sf_by_path.keys()) == {
        ".secrets/service_account.json",
        "client_secret_oauth.json",
        "google_credentials.json",
        "tls_cert.pem",
        "signing.key",
    }
    assert sf_by_path[".secrets/service_account.json"]["gitignore_protected"] is True
    assert sf_by_path["signing.key"]["gitignore_protected"] is False
    for sf in state0["secret_files"]:
        assert "content" not in sf and sf["size_bytes"] > 0 and sf["updated_at"]

    # 1. import_missing adds missing keys with empty values & comments WITHOUT overwriting TG_TOKEN
    imp = env_service.sync_env_example(proj, rel_file=".env", direction="import_missing", ensure_gitignore=True)
    assert imp["added_keys"] == ["OPENAI_API_KEY", "WEBHOOK_SECRET"]
    after_imp = (proj / ".env").read_text(encoding="utf-8")
    assert "TG_TOKEN=super_live_secret_999" in after_imp
    assert "# OpenAI key for assistant\nOPENAI_API_KEY=" in after_imp
    assert "# Webhook secret\nWEBHOOK_SECRET=" in after_imp
    assert env_service.is_env_gitignore_protected(proj, ".env") is True
    gi_text = (proj / ".gitignore").read_text(encoding="utf-8")
    assert "*.pyc" in gi_text and ".env" in gi_text and "!.env.example" in gi_text

    # 2. export_template writes `.env.example` with comments & ZERO secret values
    exp = env_service.sync_env_example(proj, rel_file=".env", direction="export_template")
    assert exp["exported_keys_count"] == 4
    example_after = (proj / ".env.example").read_text(encoding="utf-8")
    assert "# Telegram section" in example_after
    assert "TG_TOKEN=" in example_after
    assert "PROXY_URL=" in example_after
    assert "OPENAI_API_KEY=" in example_after
    assert "WEBHOOK_SECRET=" in example_after
    assert "super_live_secret_999" not in example_after
    assert "user:pass@127.0.0.1:10808" not in example_after


# ---------------------------------------------------------------------------
# 3. Step 3: API RBAC (403 guest), Raw Mode, GDrive Notify & Self-Hub Lock
# ---------------------------------------------------------------------------


def test_project_env_api_rbac_raw_gdrive_trigger_and_self_hub_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import auth, brain_reader, config, database, gdrive_service, main

    temp_db = tmp_path / "env_api_v155.db"
    monkeypatch.setattr(config, "DB_PATH", temp_db)
    monkeypatch.setattr(database, "DB_PATH", temp_db)

    admin_root = tmp_path / "admin_projects"
    admin_root.mkdir()
    proj_dir = admin_root / "DemoApp"
    proj_dir.mkdir()
    (proj_dir / ".env").write_text("# Main\nSERVICE_KEY=init_val\n", encoding="utf-8")
    (proj_dir / ".env.example").write_text("SERVICE_KEY=\nEXTRA_KEY=\n", encoding="utf-8")

    # Isolated fake Hub project directory (never touches real repo BASE_DIR!)
    fake_hub_dir = tmp_path / "FakeAntigravityHub"
    fake_hub_dir.mkdir()
    hub_env_original = (
        "# Hub secret master key\n"
        "HUB_SECRET_KEY=super_master_key_v155_do_not_break\n"
        "HUB_PORT=8765\n"
    )
    (fake_hub_dir / ".env").write_text(hub_env_original, encoding="utf-8")
    monkeypatch.setattr(config, "BASE_DIR", fake_hub_dir)

    def _fake_resolve_project(project_name: str, user: dict[str, Any], shared: Any) -> dict[str, Any] | None:
        low = project_name.lower()
        if low == "demoapp":
            return {"name": "DemoApp", "path": str(proj_dir), "is_shared": False, "is_self_hub": False}
        if low == "antigravity_hub":
            return {"name": "Antigravity_Hub", "path": str(fake_hub_dir), "is_shared": False, "is_self_hub": True}
        return None

    monkeypatch.setattr(brain_reader, "resolve_user_project", _fake_resolve_project)

    notify_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gdrive_service,
        "notify_gdrive_change",
        lambda uid, reason="": notify_calls.append((str(uid), str(reason))),
    )

    async def _run_api_checks() -> None:
        await database.close_db_pool()
        try:
            await database.init_db()
            guest_user = await database.create_user(
                username="guest_env",
                password="Password123!",
                display_name="Guest",
                max_access_mode="write_files",
            )
            admin_cookie = f"{auth.COOKIE_NAME}={auth.create_user_session_token('admin', 1)}"
            guest_cookie = (
                f"{auth.COOKIE_NAME}="
                f"{auth.create_user_session_token(guest_user['id'], int(guest_user.get('session_version') or 1))}"
            )

            # 1. Guest gets 403 Forbidden on ALL 3 routes
            s_get, _ = await _asgi_request(main.app, "GET", "/api/projects/DemoApp/env", cookie_header=guest_cookie)
            s_put, _ = await _asgi_request(
                main.app,
                "PUT",
                "/api/projects/DemoApp/env",
                cookie_header=guest_cookie,
                json_body={"file": ".env", "mode": "raw", "raw_content": "HACK=1\n"},
            )
            s_sync, _ = await _asgi_request(
                main.app,
                "POST",
                "/api/projects/DemoApp/env/sync-example",
                cookie_header=guest_cookie,
                json_body={"file": ".env", "direction": "import_missing"},
            )
            assert s_get == 403 and s_put == 403 and s_sync == 403

            # 2. Admin GET returns complete payload
            st, data = await _asgi_request(main.app, "GET", "/api/projects/DemoApp/env", cookie_header=admin_cookie)
            assert st == 200 and data["ok"] is True
            assert data["project_name"] == "DemoApp"
            assert data["is_self_hub"] is False
            assert data["entries"][0]["key"] == "SERVICE_KEY"
            assert data["entries"][0]["locked"] is False
            assert [m["key"] for m in data["example"]["missing_keys"]] == ["EXTRA_KEY"]

            # 3. Admin PUT in raw mode creates `.gitignore` and triggers gdrive_service.notify_gdrive_change
            raw_payload = "# Raw edited\nSERVICE_KEY=updated_raw\nNEW_RAW=ok\n"
            st2, data2 = await _asgi_request(
                main.app,
                "PUT",
                "/api/projects/DemoApp/env",
                cookie_header=admin_cookie,
                json_body={"file": ".env", "mode": "raw", "raw_content": raw_payload, "ensure_gitignore": True},
            )
            assert st2 == 200 and data2["gitignore_protected"] is True and data2["gitignore_updated"] is True
            assert (proj_dir / ".env").read_text(encoding="utf-8") == raw_payload
            assert (proj_dir / ".gitignore").is_file()
            assert any(uid == "admin" and "project_env:DemoApp" in r for uid, r in notify_calls)

            # 4. Admin POST sync-example (`import_missing` triggers gdrive notify, `export_template` does not leak secrets)
            notify_before_imp = len(notify_calls)
            st3, data3 = await _asgi_request(
                main.app,
                "POST",
                "/api/projects/DemoApp/env/sync-example",
                cookie_header=admin_cookie,
                json_body={"file": ".env", "direction": "import_missing"},
            )
            assert st3 == 200 and data3["sync_result"]["added_keys"] == ["EXTRA_KEY"]
            assert len(notify_calls) == notify_before_imp + 1

            st4, data4 = await _asgi_request(
                main.app,
                "POST",
                "/api/projects/DemoApp/env/sync-example",
                cookie_header=admin_cookie,
                json_body={"file": ".env", "direction": "export_template"},
            )
            assert st4 == 200
            ex_content = (proj_dir / ".env.example").read_text(encoding="utf-8")
            assert "SERVICE_KEY=" in ex_content and "updated_raw" not in ex_content

            # 5. Self-Hub (`Antigravity_Hub`) marks `HUB_SECRET_KEY` locked and rejects modify/delete with HTTP 400
            st_hub, hub_data = await _asgi_request(
                main.app, "GET", "/api/projects/Antigravity_Hub/env", cookie_header=admin_cookie
            )
            assert st_hub == 200 and hub_data["is_self_hub"] is True
            hub_entries = {e["key"]: e for e in hub_data["entries"]}
            assert hub_entries["HUB_SECRET_KEY"]["locked"] is True
            assert hub_entries["HUB_PORT"]["locked"] is False

            # Attempt to modify HUB_SECRET_KEY in entries mode -> 400
            st_mod, mod_err = await _asgi_request(
                main.app,
                "PUT",
                "/api/projects/Antigravity_Hub/env",
                cookie_header=admin_cookie,
                json_body={
                    "file": ".env",
                    "mode": "entries",
                    "entries": [
                        {"key": "HUB_SECRET_KEY", "value": "tampered_value"},
                        {"key": "HUB_PORT", "value": "8765"},
                    ],
                },
            )
            assert st_mod == 400 and "HUB_SECRET_KEY" in str(mod_err.get("detail", ""))
            assert (fake_hub_dir / ".env").read_text(encoding="utf-8") == hub_env_original

            # Attempt to delete HUB_SECRET_KEY in raw mode -> 400
            st_del, del_err = await _asgi_request(
                main.app,
                "PUT",
                "/api/projects/Antigravity_Hub/env",
                cookie_header=admin_cookie,
                json_body={"file": ".env", "mode": "raw", "raw_content": "HUB_PORT=9000\n"},
            )
            assert st_del == 400 and "HUB_SECRET_KEY" in str(del_err.get("detail", ""))
            assert (fake_hub_dir / ".env").read_text(encoding="utf-8") == hub_env_original

            # Modifying HUB_PORT while keeping HUB_SECRET_KEY intact succeeds -> 200
            st_ok, ok_data = await _asgi_request(
                main.app,
                "PUT",
                "/api/projects/Antigravity_Hub/env",
                cookie_header=admin_cookie,
                json_body={
                    "file": ".env",
                    "mode": "entries",
                    "entries": [
                        {"key": "HUB_SECRET_KEY", "value": "super_master_key_v155_do_not_break"},
                        {"key": "HUB_PORT", "value": "8766"},
                    ],
                },
            )
            assert st_ok == 200
            updated_hub = (fake_hub_dir / ".env").read_text(encoding="utf-8")
            assert "HUB_SECRET_KEY=super_master_key_v155_do_not_break" in updated_hub
            assert "HUB_PORT=8766" in updated_hub
        finally:
            await database.close_db_pool()

    asyncio.run(_run_api_checks())


# ---------------------------------------------------------------------------
# 4. Step 4/5: Node VM UI Behavior & Auto-Masking Contracts
# ---------------------------------------------------------------------------

NODE_ENV_UI_CHECKS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const noop = () => {};
const ids = JSON.parse(fs.readFileSync(0, "utf8"));

function createEmitter() {
  const map = new Map();
  return {
    addEventListener(type, fn) {
      if (!map.has(type)) map.set(type, []);
      map.get(type).push(fn);
    },
    removeEventListener(type, fn) {
      const arr = map.get(type);
      if (!arr) return;
      const i = arr.indexOf(fn);
      if (i >= 0) arr.splice(i, 1);
    },
    dispatch(type, event = {}) {
      const evt = {
        type,
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; },
        stopPropagation() {},
        ...event,
      };
      for (const fn of [...(map.get(type) || [])]) fn(evt);
      return evt;
    },
  };
}

function makeElement(id = "") {
  const hiddenDefault = [
    "projectMemoryModal", "projMemTabEnvBtn", "projMemTabSettingsBtn",
    "projMemPaneEnv", "projMemPaneSettings", "projMemPaneChangelog",
    "projEnvRawWrap", "agyConfirmModal", "ctxProjEnvBtn",
  ].includes(id);
  const classes = new Set(hiddenDefault ? ["hidden"] : []);
  const attrs = new Map();
  const emitter = createEmitter();
  const el = {
    id,
    value: "",
    type: "text",
    checked: false,
    disabled: false,
    textContent: "",
    innerHTML: "",
    dataset: {},
    children: [],
    options: [],
    style: { setProperty: noop, removeProperty: noop, getPropertyValue: () => "" },
    classList: {
      add: (...ns) => ns.forEach(n => classes.add(n)),
      remove: (...ns) => ns.forEach(n => classes.delete(n)),
      toggle: (n, force) => {
        const next = force === undefined ? !classes.has(n) : Boolean(force);
        if (next) classes.add(n); else classes.delete(n);
        return next;
      },
      contains: n => classes.has(n),
    },
    setAttribute: (k, v) => attrs.set(k, String(v)),
    getAttribute: k => attrs.get(k) ?? null,
    removeAttribute: k => attrs.delete(k),
    addEventListener: emitter.addEventListener,
    removeEventListener: emitter.removeEventListener,
    dispatchEvent: evt => emitter.dispatch(evt.type, { target: el, ...evt }),
    click: () => emitter.dispatch("click", { target: el }),
    focus: noop,
    appendChild(child) { el.children.push(child); return child; },
    remove: noop,
    replaceChildren: noop,
    closest: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  return el;
}

const nodes = new Map(ids.map(id => [id, makeElement(id)]));
const docEmitter = createEmitter();
const document = {
  documentElement: makeElement("html"),
  body: makeElement("body"),
  activeElement: null,
  getElementById: id => nodes.get(id) ?? null,
  querySelector: sel => sel.startsWith("#") ? (nodes.get(sel.slice(1)) ?? null) : null,
  querySelectorAll: () => [],
  createElement: tag => makeElement(tag),
  addEventListener: docEmitter.addEventListener,
  removeEventListener: docEmitter.removeEventListener,
};

const storage = () => {
  const m = new Map();
  return { getItem: k => m.get(k) ?? null, setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k) };
};

const apiCalls = [];
const copied = [];
const rawConfirmCalls = [];

let envStore = {
  ok: true,
  project_name: "AlphaBot",
  is_self_hub: false,
  active_file: ".env",
  available_files: [
    { path: ".env", exists: true, size_bytes: 88, updated_at: "2026-11-20T09:00:00+00:00", gitignore_protected: true },
    { path: ".env.local", exists: true, size_bytes: 20, updated_at: "2026-11-20T09:00:00+00:00", gitignore_protected: true },
  ],
  exists: true,
  gitignore_protected: true,
  gdrive_vault_active: true,
  entries: [
    { key: "BOT_TOKEN", value: "top_secret_bot_999", comment: "Telegram bot token", is_empty: false, locked: false },
    { key: "EMPTY_KEY", value: "", comment: "", is_empty: true, locked: false },
  ],
  raw_content: "# Telegram bot token\nBOT_TOKEN=top_secret_bot_999\nEMPTY_KEY=\n",
  empty_keys_count: 1,
  example: { exists: true, path: ".env.example", missing_keys: [{ key: "WEBHOOK_URL", comment: "Webhook" }] },
  secret_files: [
    { rel_path: ".secrets/cert.pem", size_bytes: 1200, updated_at: "2026-11-20T09:00:00+00:00", gitignore_protected: true },
  ],
};

async function mockFetch(url, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const body = options.body ? JSON.parse(options.body) : null;
  apiCalls.push({ method, url: String(url), body });
  const reply = (payload, status = 200) => ({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    text: async () => JSON.stringify(payload),
    json: async () => payload,
  });
  if (url.includes("/memory")) {
    return reply({ ok: true, memory: { project_name: "AlphaBot", changelog: [], has_passport: true }, parallel_runs: [] });
  }
  if (url.includes("/settings")) {
    return reply({ ok: true, project_name: "AlphaBot", extra_dirs: [], host_services: [] });
  }
  if (url.includes("/env/sync-example") && method === "POST") {
    envStore = {
      ...envStore,
      entries: [...envStore.entries, { key: "WEBHOOK_URL", value: "", comment: "Webhook", is_empty: true, locked: false }],
      example: { exists: true, path: ".env.example", missing_keys: [] },
      sync_result: { ok: true, added_count: 1, added_keys: ["WEBHOOK_URL"], example_path: ".env.example" },
    };
    return reply(envStore);
  }
  if (url.includes("/env") && method === "PUT") {
    envStore = {
      ...envStore,
      entries: (body.entries || []).map(e => ({ ...e, is_empty: !e.value, locked: false })),
      raw_content: body.raw_content || envStore.raw_content,
      gitignore_updated: false,
    };
    return reply(envStore);
  }
  if (url.includes("/env") && method === "GET") {
    return reply(envStore);
  }
  return reply({ ok: true });
}

const context = {
  __AGY_EXPORT_ADVISOR_FOR_TESTS__: true,
  __AGY_SKIP_AUTO_INIT__: true,
  document,
  fetch: mockFetch,
  confirm: msg => { rawConfirmCalls.push(String(msg)); return true; },
  localStorage: storage(),
  sessionStorage: storage(),
  location: new URL("https://hub.invalid/"),
  URL,
  URLSearchParams,
  navigator: {
    userAgent: "Mozilla/5.0",
    clipboard: { writeText: async txt => { copied.push(String(txt)); } },
  },
  innerWidth: 1440,
  innerHeight: 900,
  addEventListener: noop,
  removeEventListener: noop,
  setTimeout: fn => { fn(); return 1; },
  clearTimeout: noop,
  setInterval: noop,
  clearInterval: noop,
  requestAnimationFrame: fn => fn(),
  cancelAnimationFrame: noop,
  CSS: { supports: () => true, escape: String },
  matchMedia: () => ({ matches: false, addEventListener: noop, removeEventListener: noop }),
};
context.window = context;
const sandbox = vm.createContext(context);
vm.runInContext(fs.readFileSync("app/static/ui-shell.js", "utf8"), sandbox, { filename: "ui-shell.js" });
vm.runInContext(fs.readFileSync("app/static/app.js", "utf8"), sandbox, { filename: "app.js" });

(async () => {
  const api = context.__AGY_ADVISOR_API__;
  api.bindProjectEnvEvents();

  // 1. Non-admin cannot access `env` tab
  api.state.currentUser = { id: "g1", username: "guest", role: "user" };
  await api.openProjectMemoryModal("AlphaBot", "env");
  assert.equal(api.state.projectMemory.activeTab, "passport");
  assert.equal(nodes.get("projMemTabEnvBtn").classList.contains("hidden"), true);
  assert.equal(nodes.get("projMemPaneEnv").classList.contains("hidden"), true);

  // 2. Admin opens `env` tab directly
  api.state.currentUser = { id: "admin", username: "admin", role: "admin" };
  await api.openProjectMemoryModal("AlphaBot", "env");
  assert.equal(api.state.projectMemory.activeTab, "env");
  assert.equal(nodes.get("projMemTabEnvBtn").classList.contains("hidden"), false);
  assert.equal(nodes.get("projMemPaneEnv").classList.contains("hidden"), false);
  assert.equal(nodes.get("generateProjectMemoryBtn").classList.contains("hidden"), true);
  assert.equal(nodes.get("projMemoryEnvCount").textContent, "2");

  // Default masking: password inputs, raw overlay active, revealAll = false
  const rowsHtml1 = nodes.get("projEnvRowsList").innerHTML;
  assert.match(rowsHtml1, /type="password"/);
  assert.doesNotMatch(rowsHtml1, /type="text"/);
  assert.match(rowsHtml1, /proj-env-empty-badge">пусто</);
  assert.equal(api.state.projectEnv.revealAll, false);
  assert.equal(api.state.projectEnv.revealedKeys.size, 0);
  assert.equal(nodes.get("projEnvRawMaskOverlay").classList.contains("hidden"), false);
  assert.match(nodes.get("projSecretsFilesList").innerHTML, /\.secrets\/cert\.pem/);

  // 3. Reveal single field, then reveal all
  api.state.projectEnv.revealedKeys.add("BOT_TOKEN");
  api.renderProjectEnvTab();
  const rowsHtmlSingle = nodes.get("projEnvRowsList").innerHTML;
  assert.match(rowsHtmlSingle, /type="text"[\s\S]*data-env-val-key="BOT_TOKEN"/);
  assert.match(rowsHtmlSingle, /type="password"[\s\S]*data-env-val-key="EMPTY_KEY"/);

  nodes.get("projEnvRevealAllBtn").click();
  assert.equal(api.state.projectEnv.revealAll, true);
  assert.equal(nodes.get("projEnvRevealAllBtn").textContent, "Скрыть все");

  // 4. Add new row -> switch Table -> Raw -> Table preserves unsaved edit -> save via PUT
  nodes.get("projEnvNewKeyInput").value = "STRIPE_KEY";
  nodes.get("projEnvNewValInput").value = "sk_live_abc #1";
  nodes.get("projEnvNewCommentInput").value = "Billing key";
  assert.equal(api.addProjectEnvDraftRow(), true);
  assert.equal(api.state.projectEnv.dirty, true);
  assert.equal(api.state.projectEnv.draftEntries.length, 3);

  api.switchProjectEnvMode("raw");
  assert.match(api.state.projectEnv.draftRaw, /# Billing key\nSTRIPE_KEY="sk_live_abc #1"/);
  api.switchProjectEnvMode("table");
  assert.equal(api.state.projectEnv.draftEntries.find(e => e.key === "STRIPE_KEY")?.value, "sk_live_abc #1");

  const saved = await api.saveProjectEnvFromModal();
  assert.ok(saved && saved.ok);
  assert.equal(api.state.projectEnv.dirty, false);
  assert.ok(apiCalls.some(c => c.method === "PUT" && c.url.includes("/api/projects/AlphaBot/env")));

  // 5. Auto-masking reset on modal close
  api.state.projectEnv.revealAll = true;
  api.state.projectEnv.revealedKeys.add("STRIPE_KEY");
  api.state.projectEnv.rawRevealed = true;
  const closed = await api.closeProjectMemoryModal();
  assert.equal(closed, true);
  assert.equal(api.state.projectEnv.revealAll, false);
  assert.equal(api.state.projectEnv.revealedKeys.size, 0);
  assert.equal(api.state.projectEnv.rawRevealed, false);
  assert.equal(nodes.get("projectMemoryModal").classList.contains("hidden"), true);
  assert.equal(nodes.get("projEnvRawMaskOverlay").classList.contains("hidden"), false);

  // 6. Zero raw browser confirm() calls
  assert.deepEqual(rawConfirmCalls, []);
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""


def test_project_env_ui_node_vm_and_css_important_budget() -> None:
    shell = ShellParser()
    shell.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    shell.close()
    run_node("env-ui-v155", shell.ids, script=NODE_ENV_UI_CHECKS)

    css = (STATIC / "ui-system.css").read_text(encoding="utf-8")
    stripped = re.sub(r"/\*[\s\S]*?\*/", "", css)
    important_hits = re.findall(r"!\s*important\b", stripped, flags=re.IGNORECASE)
    assert len(important_hits) == 1, f"Expected strictly 1 !important in ui-system.css, got {len(important_hits)}"
    assert "#projMemPaneEnv" in stripped and ".proj-env-row" in stripped


# ---------------------------------------------------------------------------
# 5. Chromium Responsive Geometry & DOM Masking Verification (1440 / 390 / 320)
# ---------------------------------------------------------------------------

CHROMIUM_ENV_GEOMETRY_CHECKS = r"""
const viewports = [[1440, 900], [390, 844], [320, 568]];
function check(ok, msg) {
  if (!ok) throw new Error(msg);
}

function measureViewport(width, height) {
  return new Promise((resolve, reject) => {
    const frame = document.createElement("iframe");
    frame.style.cssText = "border:0;position:absolute;width:" + width + "px;height:" + height + "px";
    frame.onload = async () => {
      try {
        const w = frame.contentWindow;
        const d = frame.contentDocument;
        const api = w.__AGY_ADVISOR_API__;
        const get = id => d.getElementById(id);

        api.bindProjectEnvEvents();
        api.state.currentUser = { id: "admin", username: "admin", role: "admin", max_access_mode: "full_access" };
        api.state.projectMemory.activeProject = "Antigravity_Hub";
        api.state.projectEnv.activeProject = "Antigravity_Hub";
        api.state.projectEnv.activeFile = ".env";
        api.state.projectEnv.data = {
          ok: true,
          project_name: "Antigravity_Hub",
          is_self_hub: true,
          active_file: ".env",
          available_files: [
            { path: ".env", exists: true, size_bytes: 240, gitignore_protected: true },
            { path: ".env.local", exists: true, size_bytes: 64, gitignore_protected: true },
            { path: "services/worker/.env.production", exists: true, size_bytes: 120, gitignore_protected: true },
          ],
          exists: true,
          gitignore_protected: true,
          gdrive_vault_active: true,
          entries: [
            { key: "HUB_SECRET_KEY", value: "master_secret_locked_987654321", comment: "Мастер-ключ шифрования hub.db", is_empty: false, locked: true },
            { key: "TELEGRAM_BOT_LONG_ACCESS_TOKEN_PROD", value: "123456:ABC-DEF-very-long-token-value", comment: "Токен основного бота уведомлений", is_empty: false, locked: false },
            { key: "OPENAI_OPTIONAL_ENDPOINT", value: "", comment: "Опциональный эндпоинт", is_empty: true, locked: false },
          ],
          raw_content: "# Мастер-ключ\nHUB_SECRET_KEY=master_secret_locked_987654321\nTELEGRAM_BOT_LONG_ACCESS_TOKEN_PROD=123456:ABC\n",
          empty_keys_count: 1,
          example: {
            exists: true,
            path: ".env.example",
            missing_keys: [{ key: "SENTRY_DSN", comment: "Мониторинг ошибок" }],
          },
          secret_files: [
            { rel_path: ".secrets/very_long_google_oauth_client_secret_file.json", size_bytes: 2048, updated_at: "2026-11-20T09:00:00Z", gitignore_protected: true },
            { rel_path: "certs/server_private.key", size_bytes: 1680, updated_at: "2026-11-20T09:00:00Z", gitignore_protected: false },
          ],
        };
        api.state.projectEnv.draftEntries = api.state.projectEnv.data.entries.map(e => ({ ...e }));
        api.state.projectEnv.draftRaw = api.state.projectEnv.data.raw_content;
        api.state.projectEnv.mode = "table";
        api.state.projectEnv.dirty = false;
        api.resetProjectEnvSecretsView();

        const modal = get("projectMemoryModal");
        const card = modal.querySelector(".modal-card");
        card.style.animation = "none";
        modal.classList.remove("hidden");
        api.switchProjectMemoryTab("env");
        api.renderProjectEnvTab();

        // 1. Default masking in real DOM
        const inputs = [...get("projEnvRowsList").querySelectorAll("input[data-env-val-key]")];
        check(inputs.length === 3, "Expected 3 env inputs, got " + inputs.length);
        check(inputs.every(inp => inp.type === "password"), "All env inputs must default to type=password");
        const lockedInp = get("projEnvRowsList").querySelector('input[data-env-val-key="HUB_SECRET_KEY"]');
        check(lockedInp && lockedInp.readOnly === true, "HUB_SECRET_KEY input must be readonly");
        check(!get("projEnvRowsList").querySelector('[data-env-del-key="HUB_SECRET_KEY"]'), "HUB_SECRET_KEY must not have delete button");

        // 2. Single reveal & reveal all
        const singleBtn = get("projEnvRowsList").querySelector('[data-env-toggle-key="TELEGRAM_BOT_LONG_ACCESS_TOKEN_PROD"]');
        singleBtn.click();
        check(get("projEnvRowsList").querySelector('input[data-env-val-key="TELEGRAM_BOT_LONG_ACCESS_TOKEN_PROD"]').type === "text", "Single reveal failed");
        check(get("projEnvRowsList").querySelector('input[data-env-val-key="HUB_SECRET_KEY"]').type === "password", "Other input must stay masked");

        get("projEnvRevealAllBtn").click();
        check([...get("projEnvRowsList").querySelectorAll("input[data-env-val-key]")].every(i => i.type === "text"), "Reveal all failed");

        // 3. Add row in DOM
        get("projEnvNewKeyInput").value = "CUSTOM_TIMEOUT_SEC";
        get("projEnvNewValInput").value = "45";
        get("projEnvAddRowBtn").click();
        check(get("projEnvRowsList").querySelectorAll("input[data-env-val-key]").length === 4, "Adding env row failed");
        api.state.projectEnv.dirty = false;

        // 4. Zero horizontal overflow in Table mode
        check(d.documentElement.scrollWidth <= w.innerWidth, "Document horizontal overflow in table mode on " + width + "px: " + d.documentElement.scrollWidth + " > " + w.innerWidth);
        check(card.scrollWidth <= card.clientWidth + 1, "Modal card horizontal overflow in table mode on " + width + "px: " + card.scrollWidth + " > " + card.clientWidth);

        // 5. Switch to Raw mode -> verify zero overflow & overlay behavior
        api.resetProjectEnvSecretsView();
        get("projEnvModeRawBtn").click();
        check(!get("projEnvRawWrap").classList.contains("hidden"), "Raw wrap must be visible");
        check(!get("projEnvRawMaskOverlay").classList.contains("hidden"), "Raw mask overlay must hide text by default");
        check(d.documentElement.scrollWidth <= w.innerWidth, "Document overflow in raw mode on " + width + "px");
        check(card.scrollWidth <= card.clientWidth + 1, "Modal card overflow in raw mode on " + width + "px");

        get("projEnvRawMaskOverlay").querySelector("[data-env-raw-reveal]").click();
        check(get("projEnvRawMaskOverlay").classList.contains("hidden"), "Clicking reveal button must hide raw overlay");

        // 6. Close modal -> auto-reset mask
        await api.closeProjectMemoryModal({ force: true });
        check(modal.classList.contains("hidden"), "Modal must close");
        check(api.state.projectEnv.revealAll === false && api.state.projectEnv.revealedKeys.size === 0, "Mask state must auto-reset on close");
        check(!get("projEnvRawMaskOverlay").classList.contains("hidden"), "Raw overlay must reset on close");

        resolve({ width, height, viewport: width + "x" + height, cardScrollWidth: card.scrollWidth, cardClientWidth: card.clientWidth });
      } catch (err) {
        reject(new Error(width + "x" + height + ": " + err.message));
      } finally {
        frame.remove();
      }
    };
    frame.srcdoc = workspacePage;
    document.body.append(frame);
  });
}

(async () => {
  const result = [];
  for (const [width, height] of viewports) {
    result.push(await measureViewport(width, height));
  }
  document.getElementById("report").textContent = JSON.stringify({ result });
})().catch(error => {
  document.getElementById("report").textContent = JSON.stringify({ error: error.message });
});
"""


def test_project_env_modal_geometry_and_masking_in_chromium(tmp_path: Path) -> None:
    reports = run_workspace_chromium(tmp_path, CHROMIUM_ENV_GEOMETRY_CHECKS)
    assert [row["viewport"] for row in reports] == ["1440x900", "390x844", "320x568"]
