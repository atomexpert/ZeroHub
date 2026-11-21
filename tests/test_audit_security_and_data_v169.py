"""Audit hardening regression suite (v169 Stage 1):
- Stored XSS & CSP protections on `/api/uploads/{chat_id}/{filename}` + GZip bypass
- Streaming upload & audio transcription 413 caps + atomic save
- Multi-account & per-user CLI `brain` media delivery via `/api/media` with denylist/RBAC preserved
- IP-independent per-username brute-force protection, empty-key cleanup, non-blocking PBKDF2, PIN change WS disconnect
- Host service log banner secret redaction, `hub_files.grep_search` > 2.1 MiB + > 8 MiB notice, `.env` per-path lock & SHA-256 409 conflict
- `storage_maintenance.run_maintenance` CAS protection against concurrent `tool_calls_json` updates & fresh upload folder protection
- `_USAGE_LEDGER_CACHE` targeted updates, cache preservation on `delete_chat`, `get_user_24h_tokens` parity, SQLite indexes, `gdrive_service` backup `dst_conn` cleanup

Runs strictly inside temporary directories and temporary SQLite fixtures — never touches live `data/hub.db`.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import gzip
import hashlib
import io
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import (
    agy_engine,
    auth,
    config,
    database,
    env_service,
    gdrive_service,
    host_service_manager,
    main,
    storage_maintenance,
    voice,
    workspace_mcp,
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def isolated_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "data" / "hub.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    users_dir = tmp_path / "users"
    uploads_dir = tmp_path / "data" / "uploads"
    shared_dir = tmp_path / "shared_projects"
    cli_homes_dir = tmp_path / "data" / "cli_homes"
    agy_cli_dir = tmp_path / "agy_cli"
    agy_app_dir = tmp_path / "agy_app"
    certs_dir = tmp_path / "data" / "certs"
    sessions_dir = tmp_path / "data" / "sessions"
    projects_root = tmp_path / "projects"
    sandbox_dir = tmp_path / "sandbox"

    for key, path in {
        "BASE_DIR": tmp_path,
        "DATA_DIR": tmp_path / "data",
        "DB_PATH": db_path,
        "UPLOADS_DIR": uploads_dir,
        "USERS_DIR": users_dir,
        "SHARED_PROJECTS_DIR": shared_dir,
        "CLI_HOMES_DIR": cli_homes_dir,
        "AGY_CLI_DATA_DIR": agy_cli_dir,
        "AGY_APP_DATA_DIR": agy_app_dir,
        "CERTS_DIR": certs_dir,
        "SESSIONS_DIR": sessions_dir,
        "PROJECTS_ROOT": projects_root,
        "CHAT_SANDBOX_DIR": sandbox_dir,
        "ENV_PATH": tmp_path / ".env",
        "SECRET_KEY_FILE": tmp_path / "data" / ".secret_key",
    }.items():
        monkeypatch.setattr(config, key, path)

    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "USERS_DIR", users_dir)
    monkeypatch.setattr(database, "SHARED_PROJECTS_DIR", shared_dir)
    monkeypatch.setattr(config, "get_effective_users_dir", lambda: users_dir)
    monkeypatch.setattr(config, "get_effective_legacy_uploads_dir", lambda: uploads_dir)
    monkeypatch.setattr(main, "ws_manager", main.ConnectionManager())
    monkeypatch.setattr(auth, "_FAILED_ATTEMPTS", {})
    monkeypatch.setenv("HUB_SECRET_KEY", "audit-v169-fixture-master-key")

    database._reset_usage_ledger_cache()
    database.invalidate_user_cache()
    database.invalidate_chat_list_cache()
    run(database.init_db())
    yield tmp_path
    run(database.close_db_pool())
    database._reset_usage_ledger_cache()
    database.invalidate_user_cache()
    database.invalidate_chat_list_cache()


# ---------------------------------------------------------------------------
# 1. Stored XSS / CSP on `/api/uploads/{chat_id}/{filename}` + GZip bypass
# ---------------------------------------------------------------------------


def test_uploads_stored_xss_csp_and_gzip_bypass(isolated_hub: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    admin_user = {"id": "admin", "username": "admin", "role": "admin", "is_active": True}
    chat = run(database.create_chat(owner_id="admin", kind="chat", title="Upload Security"))
    chat_id = chat["id"]

    upload_dir = config.resolve_chat_uploads_dir(chat, owner_username="admin", is_shared_project=False)
    upload_dir.mkdir(parents=True, exist_ok=True)
    html_file = upload_dir / "payload.html"
    html_file.write_text("<html><script>alert(1)</script></html>" + ("A" * 2048), encoding="utf-8")
    svg_file = upload_dir / "diagram.svg"
    svg_file.write_text("<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>" + (" " * 2048), encoding="utf-8")
    png_file = upload_dir / "preview.png"
    png_file.write_bytes(b"\x89PNG\r\n\x1a\n" + (b"\x00" * 2048))

    monkeypatch.setattr(auth, "resolve_token_user", AsyncMock(return_value=admin_user))
    client = TestClient(main.app)

    # .html must force attachment + sandbox CSP + nosniff and bypass GZip
    r_html = client.get(
        f"/api/uploads/{chat_id}/payload.html",
        params={"token": "tok"},
        headers={"Accept-Encoding": "gzip"},
    )
    assert r_html.status_code == 200
    assert r_html.headers["content-disposition"].startswith("attachment;")
    assert "payload.html" in r_html.headers["content-disposition"]
    assert r_html.headers["x-content-type-options"] == "nosniff"
    assert r_html.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert r_html.headers.get("content-encoding") != "gzip"

    # .svg inline preview must include sandbox CSP + immutable + nosniff
    r_svg = client.get(
        f"/api/uploads/{chat_id}/diagram.svg",
        params={"token": "tok"},
        headers={"Accept-Encoding": "gzip"},
    )
    assert r_svg.status_code == 200
    assert "attachment" not in r_svg.headers.get("content-disposition", "")
    assert r_svg.headers["x-content-type-options"] == "nosniff"
    assert r_svg.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert (
        r_svg.headers["content-security-policy"]
        == "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox"
    )
    assert r_svg.headers.get("content-encoding") != "gzip"

    # .png inline preview keeps immutable + nosniff without forced attachment
    r_png = client.get(
        f"/api/uploads/{chat_id}/preview.png",
        params={"token": "tok"},
        headers={"Accept-Encoding": "gzip"},
    )
    assert r_png.status_code == 200
    assert r_png.headers["content-type"] == "image/png"
    assert "attachment" not in r_png.headers.get("content-disposition", "")
    assert r_png.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert r_png.headers["x-content-type-options"] == "nosniff"
    assert r_png.headers.get("content-encoding") != "gzip"


# ---------------------------------------------------------------------------
# 2. Streaming 413 limits on upload & voice transcribe + atomic save
# ---------------------------------------------------------------------------


def test_streaming_upload_and_voice_413_caps_and_atomic_save(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_user = {"id": "admin", "username": "admin", "role": "admin", "is_active": True}
    chat = run(database.create_chat(owner_id="admin", kind="chat", title="Upload Cap"))
    chat_id = chat["id"]

    monkeypatch.setattr(auth, "resolve_token_user", AsyncMock(return_value=admin_user))
    transcribe_mock = AsyncMock(return_value={"text": "should not run"})
    monkeypatch.setattr(voice, "transcribe_audio", transcribe_mock)

    client = TestClient(main.app)

    # Valid upload saves atomically with exact attachment schema and no stray .tmp files
    payload = b"hello atomic upload " * 2048
    r_ok = client.post(
        f"/api/chats/{chat_id}/upload",
        headers={"Authorization": "Bearer tok"},
        files={"file": ("notes.txt", payload, "text/plain")},
    )
    assert r_ok.status_code == 200
    att = r_ok.json()["attachment"]
    assert att["name"] == "notes.txt"
    assert att["size"] == len(payload)
    assert att["url"] == f"/api/uploads/{chat_id}/{att['stored_name']}"
    saved_path = Path(att["disk_path"])
    assert saved_path.is_file() and saved_path.read_bytes() == payload
    dest_dir = saved_path.parent
    assert not list(dest_dir.glob("*.tmp"))

    # Oversized upload (> 35 MiB) -> 413 and leaves zero temp/extra files
    before_files = sorted(p.name for p in dest_dir.iterdir())
    oversized_file = b"x" * (35 * 1024 * 1024 + 1024)
    r_big = client.post(
        f"/api/chats/{chat_id}/upload",
        headers={"Authorization": "Bearer tok"},
        files={"file": ("huge.bin", oversized_file, "application/octet-stream")},
    )
    assert r_big.status_code == 413
    assert sorted(p.name for p in dest_dir.iterdir()) == before_files

    # Oversized audio (> 25 MiB) -> 413 without calling transcribe_audio
    oversized_audio = b"a" * (25 * 1024 * 1024 + 1024)
    r_audio = client.post(
        "/api/voice/transcribe",
        headers={"Authorization": "Bearer tok"},
        files={"audio": ("long.webm", oversized_audio, "audio/webm")},
    )
    assert r_audio.status_code == 413
    assert transcribe_mock.await_count == 0


# ---------------------------------------------------------------------------
# 3. `/api/media` access to CLI `brain` artifacts across accounts & homes
# ---------------------------------------------------------------------------


def test_api_media_cli_brain_roots_and_denylist(isolated_hub: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    admin_user = {"id": "admin", "username": "admin", "role": "admin", "is_active": True}
    monkeypatch.setattr(auth, "resolve_token_user", AsyncMock(return_value=admin_user))
    monkeypatch.setattr(database, "list_shared_projects", AsyncMock(return_value=[]))

    cid = "11111111-2222-3333-4444-555555555555"
    # 1) Multi-account CLI brain artifact
    acc_brain = config.CLI_HOMES_DIR / "_agy_accounts" / "acc_work" / "antigravity-cli" / "brain" / cid
    acc_brain.mkdir(parents=True)
    chart_png = acc_brain / "chart.png"
    chart_png.write_bytes(b"\x89PNG\r\n\x1a\nchart")

    # 2) Per-user `.agy_homes` CLI brain artifact
    agy_home_brain = (
        config.CLI_HOMES_DIR / "admin" / ".agy_homes" / "agy_1" / ".gemini" / "antigravity-cli" / "brain" / cid
    )
    agy_home_brain.mkdir(parents=True)
    doc_pdf = agy_home_brain / "doc.pdf"
    doc_pdf.write_bytes(b"%PDF-1.4\nfixture")

    # 3) Per-user `.gemini` CLI brain artifact
    user_brain = config.CLI_HOMES_DIR / "admin" / ".gemini" / "antigravity-cli" / "brain" / cid
    user_brain.mkdir(parents=True)
    report_md = user_brain / "report.md"
    report_md.write_text("# Report", encoding="utf-8")

    # 4) Forbidden paths inside CLI_HOMES_DIR & brain logs
    raw_log = acc_brain / "logs" / "transcript.jsonl"
    raw_log.parent.mkdir(parents=True)
    raw_log.write_text('{"turn":1}\n', encoding="utf-8")

    non_brain_file = config.CLI_HOMES_DIR / "admin" / ".agy_homes" / "agy_1" / "notes.txt"
    non_brain_file.write_text("internal cli home file", encoding="utf-8")

    oauth_file = config.CLI_HOMES_DIR / "_agy_accounts" / "acc_work" / "antigravity-cli" / "antigravity-oauth-token.json"
    oauth_file.write_text('{"token":"secret"}', encoding="utf-8")

    client = TestClient(main.app)

    def fetch(p: Path):
        return client.get(
            "/api/media",
            params={"path": str(p), "token": "tok"},
            headers={"Accept-Encoding": "gzip"},
        )

    r_chart = fetch(chart_png)
    assert r_chart.status_code == 200 and r_chart.content == b"\x89PNG\r\n\x1a\nchart"
    assert r_chart.headers["content-type"] == "image/png"
    assert r_chart.headers.get("content-encoding") != "gzip"

    r_pdf = fetch(doc_pdf)
    assert r_pdf.status_code == 200 and r_pdf.content == b"%PDF-1.4\nfixture"
    assert r_pdf.headers["content-type"] == "application/pdf"

    r_md = fetch(report_md)
    assert r_md.status_code == 200 and r_md.content == b"# Report"

    # Must remain strictly 403
    assert fetch(raw_log).status_code == 403
    assert fetch(non_brain_file).status_code == 403
    assert fetch(oauth_file).status_code == 403


# ---------------------------------------------------------------------------
# 4. Auth rate limiting across changing X-Real-IP, cleanup, WS disconnect on PIN change
# ---------------------------------------------------------------------------


def test_auth_username_rate_limit_across_ips_and_cleanup(isolated_hub: Path) -> None:
    # check_rate_limit must NOT allocate empty entries for clean keys
    auth.check_rate_limit("198.51.100.1", "alice")
    assert auth._FAILED_ATTEMPTS == {}

    # Expired timestamps are cleaned out instead of lingering
    old_ts = time.time() - 700.0
    auth._FAILED_ATTEMPTS["198.51.100.9"] = [old_ts]
    auth._FAILED_ATTEMPTS["198.51.100.9:alice"] = [old_ts]
    auth._FAILED_ATTEMPTS["user:alice"] = [old_ts]
    auth.check_rate_limit("198.51.100.9", "alice")
    assert auth._FAILED_ATTEMPTS == {}

    # 20 failed attempts for the same username from 20 distinct X-Real-IPs via trusted local tunnel
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    for idx in range(auth.MAX_USER_ATTEMPTS_PER_10M):
        fake_ip = f"198.51.100.{idx + 1}"
        resp = client.post(
            "/api/auth/login",
            json={"username": "admin", "pin": "wrong-pin-9999"},
            headers={"X-Real-IP": fake_ip},
        )
        assert resp.status_code == 401
    assert len(auth._FAILED_ATTEMPTS.get("user:admin", [])) == auth.MAX_USER_ATTEMPTS_PER_10M

    # 21st attempt from a brand-new IP must be blocked with 429 by the IP-independent user bucket
    blocked = client.post(
        "/api/auth/login",
        json={"username": "admin", "pin": "wrong-pin-9999"},
        headers={"X-Real-IP": "203.0.113.99"},
    )
    assert blocked.status_code == 429

    # clear_failed_attempts clears all three keys
    auth.clear_failed_attempts("198.51.100.1", "admin")
    assert "user:admin" not in auth._FAILED_ATTEMPTS
    assert "198.51.100.1" not in auth._FAILED_ATTEMPTS
    assert "198.51.100.1:admin" not in auth._FAILED_ATTEMPTS


def test_change_pin_atomic_env_and_ws_disconnect(isolated_hub: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config.ENV_PATH.write_text("HUB_PIN=1234\nOTHER_VAR=keep\n", encoding="utf-8")
    monkeypatch.setenv("HUB_PIN", "1234")
    admin_updated = run(database.update_user("admin", password="1234"))
    assert admin_updated is not None

    closed_codes: list[int] = []

    class _FakeWS:
        async def close(self, code: int = 1000) -> None:
            closed_codes.append(code)

    fake_ws = _FakeWS()
    main.ws_manager.active[fake_ws] = {"id": "admin", "role": "admin"}  # type: ignore[index]

    tok = auth.create_user_session_token("admin", admin_updated["session_version"])
    client = TestClient(main.app)
    resp = client.post(
        "/api/auth/change-pin",
        headers={"Authorization": f"Bearer {tok}"},
        json={"current_pin": "1234", "new_pin": "9876"},
    )
    assert resp.status_code == 200
    assert closed_codes == [4401]
    assert fake_ws not in main.ws_manager.active
    env_text = config.ENV_PATH.read_text(encoding="utf-8")
    assert "HUB_PIN=9876" in env_text and "OTHER_VAR=keep" in env_text


# ---------------------------------------------------------------------------
# 5. Banner redaction, `hub_files.grep_search` > 2.1 MiB, `.env` lock & 409
# ---------------------------------------------------------------------------


def test_host_service_banner_redaction_and_grep_large_file(tmp_path: Path) -> None:
    raw_cmd = (
        'OPENAI_API_KEY=sk-live-secret123 BOT_TOKEN="tok with spaces" '
        'python worker.py --token mytok99 --api-key=ak-888 --password "p@ss word" '
        "--secret s3cr3t -p mypass https://alice:hunter2@api.example.com/v1 --port 8080"
    )
    redacted = host_service_manager._redact_cmd_for_banner(raw_cmd)
    for leaked in ("sk-live-secret123", "tok with spaces", "mytok99", "ak-888", "p@ss word", "s3cr3t", "mypass", "hunter2"):
        assert leaked not in redacted, f"Secret leaked in banner: {leaked} -> {redacted}"
    assert "--port 8080" in redacted
    assert "python worker.py" in redacted
    assert "<redacted>" in redacted

    # hub_files.grep_search must search files > 2.1 MiB (up to 8 MiB)
    ws = tmp_path / "ws"
    ws.mkdir()
    big_js = ws / "app.js"
    filler = ("// padding line 0123456789abcdef\n") * 70_000  # ~2.31 MiB
    big_js.write_text(filler + "const UNIQUE_V169_NEEDLE = true;\n", encoding="utf-8")
    assert big_js.stat().st_size > 2_200_000

    run_ctx = workspace_mcp.ResolvedFilesRun(
        user={"id": "admin", "role": "admin"},
        chat_id="chat-grep",
        run_state=agy_engine.ActiveRun(chat_id="chat-grep"),
        workspace_dir=ws,
        project_dir=ws,
        uploads_dir=None,
        can_write=False,
    )
    found = workspace_mcp._sync_grep_search(run_ctx, query="UNIQUE_V169_NEEDLE")
    assert "app.js:" in found and "UNIQUE_V169_NEEDLE" in found

    # Files > 8 MiB are skipped and reported when no hits are found
    huge_file = ws / "huge.log"
    huge_file.write_bytes(b"a" * (8 * 1024 * 1024 + 64))
    missed = workspace_mcp._sync_grep_search(run_ctx, query="NON_EXISTENT_NEEDLE_XYZ")
    assert "пропущено файлов > 8 МиБ: 1" in missed


def test_env_service_expected_sha256_conflict_and_concurrent_lock(tmp_path: Path) -> None:
    proj = tmp_path / "demo_proj"
    proj.mkdir()
    env_file = proj / ".env"
    env_file.write_text("ALPHA=1\n", encoding="utf-8")
    initial_sha = hashlib.sha256(env_file.read_bytes()).hexdigest()

    # Matching expected_sha256 succeeds
    res = env_service.save_project_env_file(
        proj,
        mode="raw",
        raw_content="ALPHA=2\n",
        ensure_gitignore=False,
        expected_sha256=initial_sha,
    )
    assert res["ok"] is True
    assert env_file.read_text(encoding="utf-8") == "ALPHA=2\n"

    # Stale expected_sha256 raises 409 EnvConflictError
    with pytest.raises(env_service.EnvConflictError) as exc_info:
        env_service.save_project_env_file(
            proj,
            mode="raw",
            raw_content="ALPHA=3\n",
            ensure_gitignore=False,
            expected_sha256=initial_sha,
        )
    assert exc_info.value.status_code == 409
    assert env_file.read_text(encoding="utf-8") == "ALPHA=2\n"

    # Omitting expected_sha256 preserves full backward compatibility
    res2 = env_service.save_project_env_file(
        proj,
        mode="raw",
        raw_content="ALPHA=4\n",
        ensure_gitignore=False,
    )
    assert res2["ok"] is True

    # Concurrent writers to the same canonical path serialize cleanly without corruption
    def _write_idx(i: int) -> bool:
        out = env_service.save_project_env_file(
            proj,
            mode="raw",
            raw_content=f"ALPHA={i}\nBETA={i * 10}\n",
            ensure_gitignore=False,
        )
        return bool(out["ok"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_write_idx, range(12)))
    assert all(results)
    final_lines = env_file.read_text(encoding="utf-8").splitlines()
    assert len(final_lines) == 2 and final_lines[0].startswith("ALPHA=") and final_lines[1].startswith("BETA=")


# ---------------------------------------------------------------------------
# 6. `storage_maintenance.run_maintenance` CAS & fresh uploads guard
# ---------------------------------------------------------------------------


def test_storage_maintenance_cas_and_fresh_upload_guard(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = run(database.create_chat(owner_id="admin", kind="chat", title="Maint Chat"))
    big_blob = "X" * 8000
    raw_large_tc = json.dumps(
        [{"name": "run_command", "status": "done", "result": big_blob}],
        ensure_ascii=False,
    )
    concurrent_newer_tc = json.dumps(
        [{"name": "run_command", "status": "done", "result": "CONCURRENT_FRESH_VALUE"}],
        ensure_ascii=False,
    )

    msg_race = run(database.add_message(chat["id"], role="assistant", content="race", status="done"))
    msg_normal = run(database.add_message(chat["id"], role="assistant", content="normal", status="done"))
    msg_streaming = run(database.add_message(chat["id"], role="assistant", content="live", status="streaming"))

    # Inject raw oversized tool_calls_json directly to simulate legacy uncompacted rows
    with sqlite3.connect(database.DB_PATH) as conn:
        for mid in (msg_race["id"], msg_normal["id"], msg_streaming["id"]):
            conn.execute("UPDATE messages SET tool_calls_json = ? WHERE id = ?", (raw_large_tc, mid))
        conn.commit()

    # Create one fresh orphan upload folder (< 600s) and one stale orphan upload folder (> 600s)
    fresh_orphan = config.UPLOADS_DIR / "brand-new-chat-orphan"
    fresh_orphan.mkdir(parents=True)
    (fresh_orphan / "a.txt").write_text("keep", encoding="utf-8")

    stale_orphan = config.UPLOADS_DIR / "old-chat-orphan"
    stale_orphan.mkdir(parents=True)
    (stale_orphan / "b.txt").write_text("delete", encoding="utf-8")
    old_time = time.time() - 1200.0
    os.utime(stale_orphan, (old_time, old_time))

    # Simulate concurrent write to `msg_race` right when compact_tool_calls_payload runs during maintenance
    real_compact = database.compact_tool_calls_payload
    mutated_once = False

    def _racing_compact(payload: Any) -> Any:
        nonlocal mutated_once
        if not mutated_once:
            mutated_once = True
            with sqlite3.connect(database.DB_PATH, timeout=15.0) as conn:
                conn.execute(
                    "UPDATE messages SET tool_calls_json = ? WHERE id = ?",
                    (concurrent_newer_tc, msg_race["id"]),
                )
                conn.commit()
        return real_compact(payload)

    monkeypatch.setattr(database, "compact_tool_calls_payload", _racing_compact)

    report = run(storage_maintenance.run_maintenance(lambda: True))
    # Only `msg_normal` should be slimmed (`msg_race` skipped by CAS, `msg_streaming` skipped by status != 'streaming')
    assert report["slimmed_messages"] == 1
    assert report["removed_orphan_uploads"] == 1
    assert fresh_orphan.exists(), "Fresh orphan upload folder (< 600s) must be protected!"
    assert not stale_orphan.exists(), "Stale orphan upload folder (> 600s) must be removed!"

    with sqlite3.connect(database.DB_PATH) as conn:
        race_val = conn.execute("SELECT tool_calls_json FROM messages WHERE id = ?", (msg_race["id"],)).fetchone()[0]
        norm_val = conn.execute("SELECT tool_calls_json FROM messages WHERE id = ?", (msg_normal["id"],)).fetchone()[0]
        stream_val = conn.execute("SELECT tool_calls_json FROM messages WHERE id = ?", (msg_streaming["id"],)).fetchone()[0]
    assert race_val == concurrent_newer_tc
    assert len(norm_val) < len(raw_large_tc)
    assert stream_val == raw_large_tc


# ---------------------------------------------------------------------------
# 7. `_USAGE_LEDGER_CACHE`, `delete_chat` cache retention, `get_user_24h_tokens`,
#    SQLite indexes, `rename_project_records`, and `gdrive_service` `dst_conn` cleanup
# ---------------------------------------------------------------------------


def test_usage_ledger_targeted_update_delete_chat_and_24h_tokens(isolated_hub: Path) -> None:
    chat_a = run(database.create_chat(owner_id="admin", kind="chat", title="Chat A"))
    chat_b = run(database.create_chat(owner_id="admin", kind="chat", title="Chat B"))

    now_iso = datetime.now(timezone.utc).isoformat()
    old_iso = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()

    m1 = run(
        database.add_message(
            chat_a["id"],
            user_id="admin",
            role="assistant",
            content="a1",
            model="gemini-4-neon",
            usage={"total_tokens": 100},
            created_at=now_iso,
        )
    )
    m2 = run(
        database.add_message(
            chat_b["id"],
            user_id="admin",
            role="assistant",
            content="b1",
            model="gemini-4-neon",
            usage={"input_tokens": 150, "output_tokens": 50},
            created_at=now_iso,
        )
    )
    # Excluded by model prefix (`codex:`, `claude-code:`) and provider
    run(
        database.add_message(
            chat_b["id"],
            user_id="admin",
            role="assistant",
            content="codex turn",
            model="codex:gpt-5",
            usage={"total_tokens": 5000},
            created_at=now_iso,
        )
    )
    run(
        database.add_message(
            chat_b["id"],
            user_id="admin",
            role="assistant",
            content="claude turn",
            model="claude-code:sonnet",
            usage={"total_tokens": 7000},
            created_at=now_iso,
        )
    )
    run(
        database.add_message(
            chat_b["id"],
            user_id="admin",
            role="assistant",
            content="provider excluded",
            model="custom-model",
            usage={"total_tokens": 9000, "provider": "chatgpt_codex"},
            created_at=now_iso,
        )
    )
    # Excluded because older than 24h
    run(
        database.add_message(
            chat_b["id"],
            user_id="admin",
            role="assistant",
            content="old turn",
            model="gemini-4-neon",
            usage={"total_tokens": 3000},
            created_at=old_iso,
        )
    )

    assert run(database.get_user_24h_tokens("admin")) == 300
    last_id_before = database._USAGE_LEDGER_CACHE["last_id"]
    assert last_id_before >= m2["id"]

    # Targeted dirty update of older message `m1` updates ONLY `m1` without resetting cache
    run(database.update_message(m1["id"], usage={"total_tokens": 450}))
    assert run(database.get_user_24h_tokens("admin")) == 650
    assert database._USAGE_LEDGER_CACHE["last_id"] == last_id_before
    assert database._USAGE_LEDGER_CACHE["rows_by_id"][m1["id"]][5] == 450

    # `append_message_code_handoff` must NOT bump `_USAGE_LEDGER_VERSION`
    ver_before_handoff = database._USAGE_LEDGER_VERSION
    handoffs = run(
        database.append_message_code_handoff(
            chat_b["id"],
            m2["id"],
            {"target_chat_id": chat_a["id"], "target_title": "Chat A", "project_name": "Alpha"},
        )
    )
    assert handoffs and len(handoffs) == 1
    assert database._USAGE_LEDGER_VERSION == ver_before_handoff

    # `delete_chat` evicts only `chat_a` while keeping `chat_b` cached (`last_id` intact)
    assert run(database.delete_chat(chat_a["id"])) is True
    assert database._USAGE_LEDGER_CACHE["last_id"] == last_id_before
    assert run(database.get_user_24h_tokens("admin")) == 200
    assert m1["id"] not in database._USAGE_LEDGER_CACHE["rows_by_id"]
    assert m2["id"] in database._USAGE_LEDGER_CACHE["rows_by_id"]

    # Verify new SQLite indexes were created by `init_db()`
    with sqlite3.connect(database.DB_PATH) as conn:
        idx_names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
        }
    for expected_idx in ("idx_chats_kind_proj_nocase", "idx_chats_source_desktop", "idx_chats_agy_conv"):
        assert expected_idx in idx_names


def test_gdrive_backup_closes_dst_conn_on_backup_failure_and_streams_valid_gzip(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = run(database.create_chat(owner_id="admin", kind="chat", title="Backup Test"))
    run(database.add_message(chat["id"], role="user", content="hello backup"))

    # Normal run produces valid gzipped SQLite snapshot with expected stats
    gz_bytes, stats = gdrive_service.build_sanitized_sqlite_gz_with_stats(source_db_path=database.DB_PATH)
    raw_sqlite = gzip.decompress(gz_bytes)
    assert raw_sqlite[:16] == b"SQLite format 3\x00"
    assert stats["chats_count"] >= 1 and stats["messages_count"] >= 1

    # Simulated failure inside `backup()` must close BOTH src_conn and dst_conn cleanly (no WinError 32!)
    real_connect = sqlite3.connect
    created_conns: list[sqlite3.Connection] = []

    class _TrackedConn(sqlite3.Connection):
        closed_flag: bool = False

        def backup(self, target: sqlite3.Connection, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
            raise sqlite3.OperationalError("simulated backup failure")

        def close(self) -> None:
            self.closed_flag = True
            super().close()

    def _patched_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = _TrackedConn
        conn = real_connect(*args, **kwargs)
        created_conns.append(conn)
        return conn

    monkeypatch.setattr(gdrive_service.sqlite3, "connect", _patched_connect)
    with pytest.raises(sqlite3.OperationalError, match="simulated backup failure"):
        gdrive_service.build_sanitized_sqlite_gz_with_stats(source_db_path=database.DB_PATH)

    assert len(created_conns) == 2
    assert all(getattr(c, "closed_flag", False) for c in created_conns)
