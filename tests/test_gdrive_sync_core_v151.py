"""Regression suite v151: `.env`/`.secrets/` guardrails, 4-layer project filter, encrypted vault,
sanitized SQLite backup, OAuth PKCE flow, incremental Google Drive mirror & admin-only REST API.

Runs strictly inside temporary directories and temporary SQLite fixtures with an in-memory fake
Google Drive HTTP transport (`0` real network calls) — never touches live `data/hub.db`, `users/`,
`shared_projects/`, or running services.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import re
import sqlite3
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = _TESTS_DIR.parent
for _p in (str(ROOT_DIR), str(_TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


async def _asgi_request(
    app: Any,
    method: str,
    url: str,
    *,
    cookie_header: str = "",
    json_body: Any = None,
) -> tuple[int, Any, str]:
    """Invoke FastAPI ASGI app in-memory without sockets; returns `(status, parsed_json_or_text, content_type)`."""
    parsed = urllib.parse.urlsplit(url)
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
        "client": ("127.0.0.1", 51235),
        "server": ("testserver", 80),
    }

    sent_request = False
    status_code = 500
    resp_content_type = ""
    body_chunks: list[bytes] = []

    async def receive() -> dict[str, Any]:
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": raw_body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code, resp_content_type
        if message["type"] == "http.response.start":
            status_code = int(message["status"])
            for hk, hv in message.get("headers") or []:
                if hk.lower() == b"content-type":
                    resp_content_type = hv.decode("latin1")
        elif message["type"] == "http.response.body":
            body_chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    raw_text = b"".join(body_chunks).decode("utf-8", errors="replace")
    if "application/json" in resp_content_type.lower():
        return status_code, (json.loads(raw_text) if raw_text.strip() else {}), resp_content_type
    return status_code, raw_text, resp_content_type


class FakeGoogleDriveServer:
    """In-memory fake Google OAuth + Drive REST v3 transport for deterministic offline tests."""

    def __init__(self, *, email: str = "archive.owner@example.com") -> None:
        self.email = email
        self.calls: list[dict[str, Any]] = []
        self.files: dict[str, dict[str, Any]] = {}
        self._id_seq = 0
        self.token_counter = 0
        self.sync_delay_sec = 0.0
        self.require_token: Optional[str] = None

    def _next_id(self, prefix: str) -> str:
        self._id_seq += 1
        return f"{prefix}_{self._id_seq:04d}"

    def resolve_logical_path(self, file_id: str) -> str:
        """Walk parent chain back to root folder name and return relative logical path."""
        parts: list[str] = []
        cur_id: Optional[str] = file_id
        seen: set[str] = set()
        while cur_id and cur_id in self.files and cur_id not in seen:
            seen.add(cur_id)
            rec = self.files[cur_id]
            parents = rec.get("parents") or []
            if not parents or parents == ["root"]:
                break
            parts.append(str(rec["name"]))
            cur_id = str(parents[0])
        return "/".join(reversed(parts))

    def active_files_by_path(self) -> dict[str, dict[str, Any]]:
        from app import gdrive_service

        out: dict[str, dict[str, Any]] = {}
        for fid, rec in self.files.items():
            if rec.get("mimeType") == gdrive_service.GOOGLE_DRIVE_FOLDER_MIME:
                continue
            if rec.get("trashed"):
                continue
            lp = self.resolve_logical_path(fid)
            out[lp] = rec
        return out

    def trashed_files_by_path(self) -> dict[str, dict[str, Any]]:
        from app import gdrive_service

        out: dict[str, dict[str, Any]] = {}
        for fid, rec in self.files.items():
            if rec.get("mimeType") == gdrive_service.GOOGLE_DRIVE_FOLDER_MIME:
                continue
            if rec.get("trashed"):
                lp = self.resolve_logical_path(fid)
                out[lp] = rec
        return out

    def count_upload_calls(self) -> int:
        return sum(
            1
            for c in self.calls
            if "/upload/drive/v3/files" in c["url"] and c["method"] in ("POST", "PATCH")
        )

    def count_delete_calls(self) -> int:
        return sum(1 for c in self.calls if c["method"] == "DELETE")

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> Any:
        import time
        from app.gdrive_service import GOOGLE_DRIVE_FOLDER_MIME, HttpResponse

        if self.sync_delay_sec > 0 and "/drive/v3/" in url:
            time.sleep(self.sync_delay_sec)

        m = method.upper()
        self.calls.append({"method": m, "url": url, "headers": dict(headers), "body_len": len(body or b"")})

        # 1. OAuth token exchange & refresh
        if url.startswith("https://oauth2.googleapis.com/token"):
            form = urllib.parse.parse_qs((body or b"").decode("utf-8"))
            grant = (form.get("grant_type") or [""])[0]
            self.token_counter += 1
            if grant == "authorization_code":
                tok = f"access_tok_{self.token_counter}"
                self.require_token = tok
                payload = {
                    "access_token": tok,
                    "refresh_token": f"refresh_tok_{self.token_counter}",
                    "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/drive.file openid email",
                    "token_type": "Bearer",
                }
                return HttpResponse(200, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8"))
            if grant == "refresh_token":
                tok = f"access_tok_refreshed_{self.token_counter}"
                self.require_token = tok
                payload = {
                    "access_token": tok,
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
                return HttpResponse(200, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8"))
            return HttpResponse(400, {}, b'{"error":"unsupported_grant_type"}')

        # Auth header enforcement for API endpoints
        auth_hdr = headers.get("Authorization", "")
        if self.require_token and auth_hdr != f"Bearer {self.require_token}":
            return HttpResponse(401, {"content-type": "application/json"}, b'{"error":{"message":"Invalid Credentials"}}')

        # 2. Userinfo
        if url.startswith("https://www.googleapis.com/oauth2/v2/userinfo"):
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"email": self.email, "verified_email": True}).encode("utf-8"),
            )

        parsed = urllib.parse.urlsplit(url)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # 3. Multipart upload new file: POST /upload/drive/v3/files?uploadType=multipart
        if m == "POST" and path == "/upload/drive/v3/files":
            ct = headers.get("Content-Type", "")
            bm = re.search(r"boundary=([^\s;]+)", ct)
            assert bm, f"Missing multipart boundary in {ct}"
            boundary = bm.group(1).encode("ascii")
            parts = (body or b"").split(b"--" + boundary)
            meta_part = parts[1].split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n")
            media_raw = parts[2].split(b"\r\n\r\n", 1)[1]
            if media_raw.endswith(b"\r\n"):
                media_raw = media_raw[:-2]
            meta = json.loads(meta_part.decode("utf-8"))
            fid = self._next_id("file")
            self.files[fid] = {
                "id": fid,
                "name": meta["name"],
                "parents": list(meta.get("parents") or ["root"]),
                "mimeType": "application/octet-stream",
                "trashed": False,
                "content": bytes(media_raw),
                "update_count": 0,
            }
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"id": fid, "parents": self.files[fid]["parents"]}).encode("utf-8"),
            )

        # 4. Media update existing file: PATCH /upload/drive/v3/files/{id}?uploadType=media
        if m == "PATCH" and path.startswith("/upload/drive/v3/files/"):
            fid = urllib.parse.unquote(path.split("/upload/drive/v3/files/", 1)[1])
            rec = self.files.get(fid)
            if not rec or rec.get("trashed"):
                return HttpResponse(404, {"content-type": "application/json"}, b'{"error":{"message":"Not Found"}}')
            rec["content"] = bytes(body or b"")
            rec["update_count"] = int(rec.get("update_count", 0)) + 1
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"id": fid}).encode("utf-8"),
            )

        # 5. Drive metadata endpoints: /drive/v3/files...
        if path == "/drive/v3/files" and m == "GET":
            q = (qs.get("q") or [""])[0]
            name_m = re.search(r"name = '((?:\\'|\\\\|[^'])*)'", q)
            parent_m = re.search(r"'([^']+)' in parents", q)
            want_name = name_m.group(1).replace("\\'", "'").replace("\\\\", "\\") if name_m else None
            want_parent = parent_m.group(1) if parent_m else None
            matched = []
            for rec in self.files.values():
                if rec.get("trashed"):
                    continue
                if GOOGLE_DRIVE_FOLDER_MIME in q and rec.get("mimeType") != GOOGLE_DRIVE_FOLDER_MIME:
                    continue
                if want_name is not None and rec.get("name") != want_name:
                    continue
                if want_parent is not None and want_parent not in (rec.get("parents") or []):
                    continue
                matched.append({"id": rec["id"], "name": rec["name"], "trashed": bool(rec.get("trashed"))})
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"files": matched}).encode("utf-8"),
            )

        if path == "/drive/v3/files" and m == "POST":
            meta = json.loads((body or b"{}").decode("utf-8"))
            fid = self._next_id("dir")
            self.files[fid] = {
                "id": fid,
                "name": meta["name"],
                "parents": list(meta.get("parents") or ["root"]),
                "mimeType": meta.get("mimeType") or GOOGLE_DRIVE_FOLDER_MIME,
                "trashed": False,
                "content": b"",
            }
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"id": fid, "name": meta["name"], "parents": self.files[fid]["parents"]}).encode("utf-8"),
            )

        if path.startswith("/drive/v3/files/"):
            fid = urllib.parse.unquote(path.split("/drive/v3/files/", 1)[1])
            rec = self.files.get(fid)
            if not rec:
                return HttpResponse(404, {"content-type": "application/json"}, b'{"error":{"message":"Not Found"}}')
            if m == "GET":
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps(
                        {
                            "id": rec["id"],
                            "name": rec["name"],
                            "mimeType": rec["mimeType"],
                            "trashed": bool(rec.get("trashed")),
                        }
                    ).encode("utf-8"),
                )
            if m == "PATCH":
                patch = json.loads((body or b"{}").decode("utf-8"))
                if "trashed" in patch:
                    rec["trashed"] = bool(patch["trashed"])
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps({"id": rec["id"], "trashed": bool(rec["trashed"])}).encode("utf-8"),
                )

        raise AssertionError(f"Unhandled FakeGoogleDriveServer request: {m} {url}")


# ---------------------------------------------------------------------------
# 1. Guardrails & Git Shield Hygiene Tests
# ---------------------------------------------------------------------------


def test_default_guardrails_env_secrets_hygiene_and_git_blocking() -> None:
    from app import config, git_service

    text = config.DEFAULT_PROJECT_GUARDRAILS
    words = [w for w in text.split() if w.strip()]
    assert 150 <= len(words) <= 255, f"Word count out of [150, 255]: {len(words)}"

    # Existing v148 contract markers must remain intact
    for v148_marker in (
        "grep_search",
        "py_compile",
        "node --check",
        "verify_project.py",
        "*.session",
        "git add .",
        "!important",
    ):
        assert v148_marker in text

    # New v151 `.env` / `.secrets/` / outputs hygiene markers
    for hygiene_marker in (".env", ".env.example", ".secrets/", "data/", "output/", "dist/", "target/"):
        assert hygiene_marker in text

    # Git service blocks `.secrets/`, `private-profiles/`, `*secret*.json`, `*credentials*.json`
    assert ".secrets" in git_service.BLOCKED_ANY_LEVEL_DIRS
    assert "private-profiles" in git_service.BLOCKED_ANY_LEVEL_DIRS
    assert ".secrets/" in git_service.DEFAULT_SAFE_GITIGNORE
    assert "private-profiles/" in git_service.DEFAULT_SAFE_GITIGNORE
    assert git_service.is_blocked_git_path(".secrets/id_rsa") is True
    assert git_service.is_blocked_git_path("nested/private-profiles/cookie.txt") is True
    assert git_service.is_blocked_git_path("client_secret_12345.json") is True
    assert git_service.is_blocked_git_path("config/google_credentials.json") is True
    assert git_service.is_blocked_git_path(".env.example") is False


# ---------------------------------------------------------------------------
# 2. Four-Layer Project Filter Tests
# ---------------------------------------------------------------------------


def test_four_layer_project_filter_realistic_tree(tmp_path: Path) -> None:
    from app import gdrive_service

    proj = tmp_path / "HybridApp"
    proj.mkdir()

    # Layer 1: `.gitignore` with dir rule, wildcard, and `!` negation for `.env.example`
    (proj / ".gitignore").write_text(
        "# Project ignores\n"
        ".env*\n"
        "!.env.example\n"
        "generated_cache/\n"
        "*.generated.txt\n",
        encoding="utf-8",
    )

    # Clean source code files that MUST pass
    (proj / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (proj / "src").mkdir()
    (proj / "src" / "widget.js").write_text("export const ok = true;\n", encoding="utf-8")
    (proj / "Cargo.toml").write_text('[package]\nname = "hybrid"\nversion = "0.1.0"\n', encoding="utf-8")
    (proj / ".env.example").write_text("API_KEY=replace_me\n", encoding="utf-8")

    # Layer 1 ignored items
    (proj / "generated_cache").mkdir()
    (proj / "generated_cache" / "artifact.py").write_text("SKIP = True\n", encoding="utf-8")
    (proj / "notes.generated.txt").write_text("generated\n", encoding="utf-8")

    # Layer 2 blocked runtime/build/output/profile dirs
    (proj / "target" / "release").mkdir(parents=True)
    (proj / "target" / "release" / "app.rs").write_text("fn main() {}\n", encoding="utf-8")
    (proj / "data").mkdir()
    (proj / "data" / "items.db").write_bytes(b"SQLite format 3\x00dummy")
    (proj / "chrome_profile_user1").mkdir()
    (proj / "chrome_profile_user1" / "prefs.json").write_text("{}", encoding="utf-8")

    # Layer 3 secrets (must NEVER enter `code/`, compact non-session secrets enter `vault_candidates`)
    (proj / ".env").write_text("DB_PASS=super_secret_env_pass\n", encoding="utf-8")
    (proj / ".secrets").mkdir()
    (proj / ".secrets" / "id_rsa").write_bytes(b"\x2d----BEGIN PRIVATE KEY-----\nTOPSECRET\n-----END PRIVATE KEY-----\n")
    (proj / "client_secret_test.json").write_text('{"installed":{"client_secret":"gsec_123"}}', encoding="utf-8")
    (proj / "bot.session").write_bytes(b"SQLite telethon session binary")

    # Layer 4 binaries, >1 MiB file, and Anti-Dump Guard (>80 data files & 0 code files)
    (proj / "ZeroNote.apk").write_bytes(b"PK\x03\x04apkpayload")
    (proj / "radmin_proxy.exe").write_bytes(b"MZexecutable")
    (proj / "dump.json").write_bytes(b"{" + b"a" * (1_500_000) + b"}")

    avito_dir = proj / "avito_cards"
    avito_dir.mkdir()
    for i in range(100):
        (avito_dir / f"card_{i:03d}.json").write_text(f'{{"id": {i}}}\n', encoding="utf-8")

    # Mixed subfolder with 85 JSON fixtures PLUS 1 `.py` code file must NOT be treated as a parser dump
    mixed_dir = proj / "fixtures_with_code"
    mixed_dir.mkdir()
    (mixed_dir / "loader.py").write_text("def load(): return []\n", encoding="utf-8")
    for i in range(85):
        (mixed_dir / f"fx_{i:03d}.json").write_text("{}", encoding="utf-8")

    scan = gdrive_service.scan_project_for_gdrive(proj, project_name="HybridApp", is_self_hub=False)
    code_paths = {f.rel_path for f in scan.code_files}
    vault_paths = {f.rel_path for f in scan.vault_candidates}

    # Allowed code files passed
    for expected in (".gitignore", ".env.example", "app.py", "src/widget.js", "Cargo.toml", "fixtures_with_code/loader.py"):
        assert expected in code_paths, f"Expected code file missing: {expected}"

    # Blocked items NEVER in `code/`
    for forbidden in (
        "generated_cache/artifact.py",
        "notes.generated.txt",
        "target/release/app.rs",
        "data/items.db",
        "chrome_profile_user1/prefs.json",
        "ZeroNote.apk",
        "radmin_proxy.exe",
        "dump.json",
        "avito_cards/card_000.json",
        ".env",
        ".secrets/id_rsa",
        "client_secret_test.json",
        "bot.session",
    ):
        assert forbidden not in code_paths, f"Forbidden file leaked into code/: {forbidden}"

    # Compact non-session secrets collected strictly in `vault_candidates`
    assert vault_paths == {".env", ".secrets/id_rsa", "client_secret_test.json"}
    assert "bot.session" not in vault_paths

    # Large file & Anti-Dump Guard diagnostics reported
    assert any(item["path"] == "dump.json" and item["size_bytes"] > 1_048_576 for item in scan.skipped_large_files)
    assert any(item["path"] == "avito_cards" and item["data_files_count"] == 100 for item in scan.skipped_dump_dirs)

    # Self-Hub mode additionally blocks top-level `users/` and `shared_projects/`
    hub_dir = tmp_path / "Antigravity_Hub"
    (hub_dir / "app").mkdir(parents=True)
    (hub_dir / "app" / "core.py").write_text("X = 1\n", encoding="utf-8")
    (hub_dir / "users" / "admin" / "projects" / "P1").mkdir(parents=True)
    (hub_dir / "users" / "admin" / "projects" / "P1" / "x.py").write_text("Y = 2\n", encoding="utf-8")
    (hub_dir / "shared_projects" / "S1").mkdir(parents=True)
    (hub_dir / "shared_projects" / "S1" / "z.py").write_text("Z = 3\n", encoding="utf-8")
    hub_scan = gdrive_service.scan_project_for_gdrive(hub_dir, project_name="Antigravity_Hub", is_self_hub=True)
    hub_code = {f.rel_path for f in hub_scan.code_files}
    assert hub_code == {"app/core.py"}


# ---------------------------------------------------------------------------
# 3. Encrypted Secrets Vault (`AGY_VAULT_V1`) Tests
# ---------------------------------------------------------------------------


def test_secrets_vault_encrypt_decrypt_roundtrip_and_wrong_passphrase(tmp_path: Path) -> None:
    from app import gdrive_service

    proj = tmp_path / "VaultProj"
    (proj / ".secrets").mkdir(parents=True)
    env_raw = b"TELEGRAM_BOT_TOKEN=123456:ABCDEF_secret_value\nPROXY_PASS=qwerty99\n"
    bin_raw = bytes(range(256)) + b"\x00\xffPRIVATE_CERT_PAYLOAD"
    (proj / ".env").write_bytes(env_raw)
    (proj / ".secrets" / "cert.p12").write_bytes(bin_raw)

    scan = gdrive_service.scan_project_for_gdrive(proj, project_name="VaultProj")
    payload = gdrive_service.build_secrets_vault_payload(scan.vault_candidates)
    passphrase = "Correct-Horse-Battery-Staple!2026"
    raw_vault = gdrive_service.encrypt_secrets_vault(payload, passphrase)

    # Envelope schema check & zero plaintext leakage
    envelope = json.loads(raw_vault.decode("utf-8"))
    assert envelope["magic"] == "AGY_VAULT_V1"
    assert envelope["kdf"] == "pbkdf2-sha256"
    assert envelope["iter"] == 600_000
    assert envelope["salt"] and envelope["ciphertext"]
    assert b"ABCDEF_secret_value" not in raw_vault
    assert b"PRIVATE_CERT_PAYLOAD" not in raw_vault

    # Correct passphrase restores every secret byte-for-byte
    decrypted = gdrive_service.decrypt_secrets_vault(raw_vault, passphrase)
    assert decrypted["format"] == "AGY_VAULT_PAYLOAD_V1"
    files = decrypted["files"]
    assert set(files.keys()) == {"VaultProj/.env", "VaultProj/.secrets/cert.p12"}
    assert gdrive_service.decode_vault_entry_bytes(files["VaultProj/.env"]) == env_raw
    assert gdrive_service.decode_vault_entry_bytes(files["VaultProj/.secrets/cert.p12"]) == bin_raw

    # Wrong passphrase is rejected cleanly
    with pytest.raises(gdrive_service.GDriveVaultError):
        gdrive_service.decrypt_secrets_vault(raw_vault, "Wrong-Passphrase-123")


def test_urllib_proxy_transport_uses_hub_proxy_router(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import config, gdrive_service, proxy_router

    captured: dict[str, Any] = {}

    class _FakeResp:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self) -> bytes:
            return b'{"ok":true}'

        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    class _FakeOpener:
        def open(self, req: Any, timeout: float = 0.0) -> _FakeResp:
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["timeout"] = timeout
            return _FakeResp()

    monkeypatch.setattr(config, "resolve_outbound_proxy", lambda: "http://127.0.0.1:10808")

    def _fake_builder(proxy_url: Optional[str]) -> _FakeOpener:
        captured["proxy_url"] = proxy_url
        return _FakeOpener()

    monkeypatch.setattr(proxy_router, "build_urllib_proxy_opener", _fake_builder)
    resp = gdrive_service.urllib_proxy_transport(
        "GET",
        "https://www.googleapis.com/drive/v3/files",
        {"Authorization": "Bearer test"},
        None,
        12.0,
    )
    assert resp.status == 200
    assert resp.json() == {"ok": True}
    assert captured["proxy_url"] == "http://127.0.0.1:10808"
    assert captured["method"] == "GET"
    assert captured["timeout"] == 12.0


# ---------------------------------------------------------------------------
# 4. Full End-to-End Sync Lifecycle, Sanitized SQLite, OAuth & REST API Tests
# ---------------------------------------------------------------------------


class GDriveSyncCoreAndApiV151Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app import auth, brain_reader, config, database, gdrive_service, main as main_mod

        self.auth = auth
        self.brain_reader = brain_reader
        self.config = config
        self.database = database
        self.gdrive_service = gdrive_service
        self.main_mod = main_mod

        self._tmp = tempfile.TemporaryDirectory(prefix="hub_v151_gdrive_", ignore_cleanup_errors=True)
        iso_root = Path(self._tmp.name).resolve() / "iso_hub"
        data_dir = iso_root / "data"
        users_dir = iso_root / "users"
        projects_root = users_dir / "admin" / "projects"
        shared_dir = iso_root / "shared_projects"
        shared_uploads = shared_dir / "_uploads"
        shared_ctx = shared_dir / "_context"
        cli_homes = data_dir / "cli_homes"
        uploads_dir = data_dir / "uploads"
        db_path = data_dir / "test_v151.db"
        for d in (
            iso_root,
            iso_root / "app",
            data_dir,
            users_dir,
            projects_root,
            shared_dir,
            shared_uploads,
            shared_ctx,
            cli_homes,
            uploads_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        (iso_root / "app" / "hub_stub.py").write_text("HUB_STUB = True\n", encoding="utf-8")

        await database.close_db_pool()
        patch_map = {
            "BASE_DIR": iso_root,
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
        }
        self._saved_attrs: list[tuple[Any, str, Any]] = []
        for mod in (config, database, brain_reader, main_mod):
            for attr, val in patch_map.items():
                if hasattr(mod, attr):
                    self._saved_attrs.append((mod, attr, getattr(mod, attr)))
                    setattr(mod, attr, val)

        self.projects_root = projects_root
        self.db_path = db_path
        self.fake_drive = FakeGoogleDriveServer(email="vault.admin@example.com")
        gdrive_service.set_default_transport(self.fake_drive)
        brain_reader.invalidate_projects_list_cache()
        await database.init_db()

    async def asyncTearDown(self) -> None:
        self.gdrive_service.set_default_transport(None)
        await self.database.close_db_pool()
        for mod, attr, orig in reversed(self._saved_attrs):
            setattr(mod, attr, orig)
        self.brain_reader.invalidate_projects_list_cache()
        self._tmp.cleanup()

    async def test_full_http_api_rbac_oauth_preview_incremental_sync_and_trash(self) -> None:
        app = self.main_mod.app
        cname = self.auth.COOKIE_NAME
        admin_cookie = f"{cname}={self.auth.create_user_session_token('admin', 1)}"

        regular_user = await self.database.create_user(
            username="regular_dev",
            password="Password123!",
            display_name="Regular Dev",
            max_access_mode="write_files",
        )
        user_cookie = f"{cname}={self.auth.create_user_session_token(regular_user['id'], int(regular_user.get('session_version') or 1))}"

        # 1. Strict RBAC: regular user receives 403 on every `/api/gdrive/*` route
        for method, path, body in (
            ("GET", "/api/gdrive/status", None),
            ("POST", "/api/gdrive/config", {"is_enabled": True}),
            ("POST", "/api/gdrive/oauth/start", {}),
            ("GET", "/api/gdrive/oauth/callback?code=abc&state=xyz", None),
            ("POST", "/api/gdrive/oauth/exchange", {"code": "abc", "state": "xyz"}),
            ("POST", "/api/gdrive/disconnect", {}),
            ("GET", "/api/gdrive/preview", None),
            ("POST", "/api/gdrive/sync", None),
        ):
            st_code, _, _ = await _asgi_request(app, method, path, cookie_header=user_cookie, json_body=body)
            self.assertEqual(st_code, 403, f"Expected 403 for non-admin on {method} {path}, got {st_code}")

        # Seed realistic local projects, chats, bookmarks, passport, and secret tables in SQLite
        proj_alpha = self.projects_root / "AlphaBot"
        (proj_alpha / ".secrets").mkdir(parents=True)
        (proj_alpha / "main.py").write_bytes(b"VERSION = 1\n")
        (proj_alpha / "old_util.js").write_bytes(b"export const legacy = 1;\n")
        (proj_alpha / ".env.example").write_bytes(b"BOT_TOKEN=\n")
        (proj_alpha / ".env").write_bytes(b"BOT_TOKEN=alpha_live_token_999\n")
        (proj_alpha / ".secrets" / "id_rsa").write_bytes(b"PRIVATE_KEY_ALPHA_777\n")
        (proj_alpha / "ZeroNote.apk").write_bytes(b"PK\x03\x04apk")
        (proj_alpha / "big_dump.json").write_bytes(b"[" + b"0" * 1_200_000 + b"]")
        dump_dir = proj_alpha / "avito_cards"
        dump_dir.mkdir()
        for idx in range(85):
            (dump_dir / f"item_{idx}.json").write_text('{"n":1}', encoding="utf-8")

        self.brain_reader.invalidate_projects_list_cache()

        # Regular active chat with bookmark, archived chat, and project chat + passport
        reg_chat = await self.database.create_chat(
            title="Архитектура зеркала",
            kind="chat",
            owner_id="admin",
            folder="Идеи",
        )
        m1 = await self.database.add_message(
            reg_chat["id"], role="user", content="Сделай облачное зеркало", user_id="admin"
        )
        m2 = await self.database.add_message(
            reg_chat["id"], role="assistant", content="Готово: 4 слоя фильтра и сейф.", user_id="admin"
        )
        await self.database.set_message_bookmarked(reg_chat["id"], int(m2["id"]), True)

        arch_chat = await self.database.create_chat(
            title="Старый архивный чат",
            kind="chat",
            owner_id="admin",
        )
        await self.database.add_message(
            arch_chat["id"], role="user", content="Архивная запись", user_id="admin"
        )
        async with self.database.connect_db() as db:
            await db.execute(
                "UPDATE chats SET is_archived = 1, archived_at = ? WHERE id = ?",
                (self.database.utc_now_iso(), arch_chat["id"]),
            )
            await db.commit()
        self.database.invalidate_chat_list_cache(None)

        proj_chat = await self.database.create_chat(
            title="ШТАБ AlphaBot",
            kind="project",
            project_name="AlphaBot",
            project_path=str(proj_alpha),
            owner_id="admin",
        )
        await self.database.add_message(
            proj_chat["id"], role="user", content="Проверь бота", user_id="admin"
        )

        scope_key = self.database.build_project_memory_scope_key("AlphaBot", owner_id="admin", is_shared=False)
        await self.database.save_project_memory(
            scope_key,
            owner_id="admin",
            project_name="AlphaBot",
            passport_overview="Телеграм-бот AlphaBot",
            passport_rules="- Не хранить токены в коде",
            passport_file_map="- main.py",
        )

        # Also plant sensitive rows in OpenRouter config table to assert they vanish from `.sqlite.gz`
        await self.database.upsert_user_openrouter_config(
            "admin", new_api_key="\x73k-or-v1-TOP_SECRET_OPENROUTER_KEY_XYZ"
        )

        # 2. Configure OAuth client JSON & vault passphrase via POST /api/gdrive/config
        oauth_json = json.dumps(
            {
                "installed": {
                    "client_id": "1234567890-abc.apps.googleusercontent.com",
                    "client_secret": "\x47OCSPX-super-secret-oauth-client",
                    "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        )
        vault_pass = "My-Strong-Vault-Pass-2026!"
        st_cfg, d_cfg, _ = await _asgi_request(
            app,
            "POST",
            "/api/gdrive/config",
            cookie_header=admin_cookie,
            json_body={
                "oauth_client_json": oauth_json,
                "sync_self_hub": False,
                "include_env_vault": True,
                "vault_passphrase": vault_pass,
            },
        )
        self.assertEqual(st_cfg, 200, d_cfg)
        self.assertTrue(d_cfg["has_oauth_client"])
        self.assertTrue(d_cfg["has_vault_passphrase"])
        self.assertTrue(d_cfg["include_env_vault"])
        self.assertFalse(d_cfg["sync_self_hub"])
        # Secrets must NEVER leak in config/status responses
        raw_cfg_dump = json.dumps(d_cfg)
        self.assertNotIn("\x47OCSPX-super-secret-oauth-client", raw_cfg_dump)
        self.assertNotIn(vault_pass, raw_cfg_dump)

        st_status, d_status, _ = await _asgi_request(app, "GET", "/api/gdrive/status", cookie_header=admin_cookie)
        self.assertEqual(st_status, 200)
        self.assertTrue(d_status["has_oauth_client"])
        self.assertFalse(d_status["connected"])
        self.assertFalse(d_status["sync_in_progress"])
        self.assertNotIn("oauth_client", d_status)
        self.assertNotIn("tokens", d_status)
        self.assertNotIn("vault_passphrase", d_status)

        # 3. Preview endpoint (0 network calls!)
        self.assertEqual(len(self.fake_drive.calls), 0)
        st_prev, d_prev, _ = await _asgi_request(app, "GET", "/api/gdrive/preview", cookie_header=admin_cookie)
        self.assertEqual(st_prev, 200, d_prev)
        self.assertEqual(len(self.fake_drive.calls), 0)
        self.assertEqual(d_prev["regular_chats_count"], 2)
        self.assertEqual(d_prev["project_chats_count"], 1)
        self.assertEqual(d_prev["projects_count"], 1)
        self.assertEqual(d_prev["code_files_count"], 3)  # main.py, old_util.js, .env.example
        self.assertEqual(d_prev["vault_candidates_count"], 2)  # .env, .secrets/id_rsa
        self.assertEqual(d_prev["skipped_large_files_total"], 1)
        self.assertEqual(d_prev["skipped_dump_dirs_total"], 1)

        # 4. OAuth Start + Browser Callback HTML + Manual Exchange
        st_start, d_start, _ = await _asgi_request(
            app,
            "POST",
            "/api/gdrive/oauth/start",
            cookie_header=admin_cookie,
            json_body={},
        )
        self.assertEqual(st_start, 200, d_start)
        auth_url = d_start["auth_url"]
        self.assertIn("code_challenge_method=S256", auth_url)
        self.assertIn("access_type=offline", auth_url)
        self.assertIn("prompt=consent", auth_url)
        self.assertIn("drive.file", auth_url)

        st_cb, cb_html, cb_ct = await _asgi_request(
            app,
            "GET",
            f"/api/gdrive/oauth/callback?code=auth_code_1&state={urllib.parse.quote(d_start['state'])}",
            cookie_header=admin_cookie,
        )
        self.assertEqual(st_cb, 200, cb_html)
        self.assertIn("text/html", cb_ct.lower())
        self.assertIn("agy-gdrive-oauth-done", cb_html)
        self.assertIn("window.opener?.postMessage", cb_html)

        # Also verify manual redirect URL exchange flow
        st_start2, d_start2, _ = await _asgi_request(
            app, "POST", "/api/gdrive/oauth/start", cookie_header=admin_cookie, json_body={}
        )
        self.assertEqual(st_start2, 200)
        pasted_url = f"http://127.0.0.1:8765/api/gdrive/oauth/callback?code=manual_code_2&state={d_start2['state']}"
        st_ex, d_ex, _ = await _asgi_request(
            app,
            "POST",
            "/api/gdrive/oauth/exchange",
            cookie_header=admin_cookie,
            json_body={"callback_url": pasted_url},
        )
        self.assertEqual(st_ex, 200, d_ex)
        self.assertTrue(d_ex["connected"])
        self.assertEqual(d_ex["account_email"], "vault.admin@example.com")

        # 5. Initial Sync (Run 1): uploads every layer cleanly
        st_sync1, d_sync1, _ = await _asgi_request(app, "POST", "/api/gdrive/sync", cookie_header=admin_cookie)
        self.assertEqual(st_sync1, 200, d_sync1)
        self.assertGreater(d_sync1["summary"]["uploaded"], 0)
        self.assertEqual(d_sync1["summary"]["trashed"], 0)
        self.assertTrue(d_sync1["summary"]["vault_included"])
        self.assertEqual(self.fake_drive.count_delete_calls(), 0)

        active1 = self.fake_drive.active_files_by_path()
        reg_chat_path = next(p for p in active1 if p.startswith("Диалоги/Идеи/") and p.endswith(".md"))
        arch_chat_path = next(p for p in active1 if p.startswith("Диалоги/_Архив/") and p.endswith(".md"))
        proj_chat_path = next(p for p in active1 if p.startswith("Проекты/AlphaBot/_HUB/chats/") and p.endswith(".md"))

        # Verify Markdown bookmarks & passport content
        reg_md = active1[reg_chat_path]["content"].decode("utf-8")
        self.assertIn("## 🔖 Закладки", reg_md)
        self.assertIn("Готово: 4 слоя фильтра и сейф.", reg_md)
        self.assertIn("Проекты/AlphaBot/_HUB/PASSPORT.md", active1)
        passport_md = active1["Проекты/AlphaBot/_HUB/PASSPORT.md"]["content"].decode("utf-8")
        self.assertIn("Телеграм-бот AlphaBot", passport_md)
        self.assertIn(proj_chat_path, active1)
        self.assertIn(arch_chat_path, active1)

        # Verify clean code uploaded and secrets/binaries/dumps excluded from `code/`
        self.assertIn("Проекты/AlphaBot/code/main.py", active1)
        self.assertIn("Проекты/AlphaBot/code/old_util.js", active1)
        self.assertIn("Проекты/AlphaBot/code/.env.example", active1)
        self.assertNotIn("Проекты/AlphaBot/code/.env", active1)
        self.assertNotIn("Проекты/AlphaBot/code/.secrets/id_rsa", active1)
        self.assertNotIn("Проекты/AlphaBot/code/ZeroNote.apk", active1)
        self.assertNotIn("Проекты/AlphaBot/code/big_dump.json", active1)
        self.assertFalse(any("avito_cards" in p for p in active1))

        # Verify `_restore_vault/hub_dialogs_and_memory.sqlite.gz` contains ONLY safe tables & ZERO secrets
        self.assertIn("_restore_vault/hub_dialogs_and_memory.sqlite.gz", active1)
        raw_sqlite_bytes = gzip.decompress(active1["_restore_vault/hub_dialogs_and_memory.sqlite.gz"]["content"])
        self.assertNotIn(b"\x47OCSPX-super-secret-oauth-client", raw_sqlite_bytes)
        self.assertNotIn(b"TOP_SECRET_OPENROUTER_KEY_XYZ", raw_sqlite_bytes)
        self.assertNotIn(b"refresh_tok_", raw_sqlite_bytes)
        with tempfile.TemporaryDirectory(prefix="hub_v151_inspect_db_") as inspect_dir:
            restored_db = Path(inspect_dir) / "restored.sqlite"
            restored_db.write_bytes(raw_sqlite_bytes)
            rconn = sqlite3.connect(str(restored_db))
            try:
                tbls = {
                    row[0]
                    for row in rconn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
                    ).fetchall()
                }
                self.assertTrue(tbls.issubset({"chats", "messages", "chat_folders", "project_memories", "shared_projects"}))
                self.assertIn("chats", tbls)
                self.assertIn("messages", tbls)
                self.assertIn("project_memories", tbls)
                self.assertNotIn("users", tbls)
                self.assertNotIn("gdrive_configs", tbls)
                self.assertNotIn("user_openrouter_configs", tbls)
                chats_cnt = rconn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
                self.assertEqual(chats_cnt, 3)
            finally:
                rconn.close()

        # Verify `_restore_vault/secrets.vault.enc` decrypts byte-for-byte with user passphrase
        self.assertIn("_restore_vault/secrets.vault.enc", active1)
        vault_raw = active1["_restore_vault/secrets.vault.enc"]["content"]
        self.assertNotIn(b"alpha_live_token_999", vault_raw)
        dec_vault = self.gdrive_service.decrypt_secrets_vault(vault_raw, vault_pass)
        self.assertEqual(
            self.gdrive_service.decode_vault_entry_bytes(dec_vault["files"]["AlphaBot/.env"]),
            b"BOT_TOKEN=alpha_live_token_999\n",
        )
        self.assertEqual(
            self.gdrive_service.decode_vault_entry_bytes(dec_vault["files"]["AlphaBot/.secrets/id_rsa"]),
            b"PRIVATE_KEY_ALPHA_777\n",
        )
        self.assertIn("_restore_vault/sync_manifest.json", active1)

        # 6. Second Sync (Run 2 — zero local changes): 0 uploads/updates!
        uploads_before_run2 = self.fake_drive.count_upload_calls()
        st_sync2, d_sync2, _ = await _asgi_request(app, "POST", "/api/gdrive/sync", cookie_header=admin_cookie)
        self.assertEqual(st_sync2, 200, d_sync2)
        self.assertEqual(d_sync2["summary"]["uploaded"], 0)
        self.assertEqual(d_sync2["summary"]["updated"], 0)
        self.assertEqual(d_sync2["summary"]["trashed"], 0)
        self.assertEqual(self.fake_drive.count_upload_calls(), uploads_before_run2)

        # 7. Delta Sync (Run 3): modify 1 code file, delete 1 code file & 1 chat -> 1 code update + 2 trashed
        (proj_alpha / "main.py").write_bytes(b"VERSION = 2\n")
        (proj_alpha / "old_util.js").unlink()
        await self.database.delete_chat(arch_chat["id"])
        # Also force access_token expiration to verify transparent auto-refresh during sync!
        await self.database.save_gdrive_config(
            "admin",
            tokens={
                "access_token": "expired_tok",
                "refresh_token": "refresh_tok_2",
                "expires_at": 1,
                "scope": self.gdrive_service.GOOGLE_DRIVE_SCOPE,
            },
        )

        st_sync3, d_sync3, _ = await _asgi_request(app, "POST", "/api/gdrive/sync", cookie_header=admin_cookie)
        self.assertEqual(st_sync3, 200, d_sync3)
        self.assertEqual(d_sync3["summary"]["uploaded"], 0)
        self.assertEqual(d_sync3["summary"]["trashed"], 2)  # old_util.js + archived chat
        self.assertEqual(self.fake_drive.count_delete_calls(), 0)

        active3 = self.fake_drive.active_files_by_path()
        trashed3 = self.fake_drive.trashed_files_by_path()
        self.assertEqual(active3["Проекты/AlphaBot/code/main.py"]["content"], b"VERSION = 2\n")
        self.assertEqual(active3["Проекты/AlphaBot/code/main.py"]["update_count"], 1)
        self.assertEqual(active3["Проекты/AlphaBot/code/.env.example"]["update_count"], 0)
        self.assertIn("Проекты/AlphaBot/code/old_util.js", trashed3)
        self.assertIn(arch_chat_path, trashed3)

        # 8. Disable `include_env_vault` -> old `secrets.vault.enc` moved to Trash (`trashed == True`)
        st_off, _, _ = await _asgi_request(
            app,
            "POST",
            "/api/gdrive/config",
            cookie_header=admin_cookie,
            json_body={"include_env_vault": False},
        )
        self.assertEqual(st_off, 200)
        st_sync4, d_sync4, _ = await _asgi_request(app, "POST", "/api/gdrive/sync", cookie_header=admin_cookie)
        self.assertEqual(st_sync4, 200, d_sync4)
        self.assertFalse(d_sync4["summary"]["vault_included"])
        self.assertEqual(d_sync4["summary"]["trashed"], 1)
        self.assertNotIn("_restore_vault/secrets.vault.enc", self.fake_drive.active_files_by_path())
        self.assertIn("_restore_vault/secrets.vault.enc", self.fake_drive.trashed_files_by_path())
        self.assertEqual(self.fake_drive.count_delete_calls(), 0)

        # 9. Concurrent sync guard (`409 Conflict` under per-user `asyncio.Lock`)
        self.fake_drive.sync_delay_sec = 0.08
        try:
            task1 = asyncio.create_task(
                _asgi_request(app, "POST", "/api/gdrive/sync", cookie_header=admin_cookie)
            )
            await asyncio.sleep(0.02)
            st_conflict, d_conflict, _ = await _asgi_request(
                app, "POST", "/api/gdrive/sync", cookie_header=admin_cookie
            )
            st_ok, _, _ = await task1
            self.assertEqual(st_ok, 200)
            self.assertEqual(st_conflict, 409, d_conflict)
        finally:
            self.fake_drive.sync_delay_sec = 0.0

        # 10. Disconnect clears tokens and optionally index
        st_disc, d_disc, _ = await _asgi_request(
            app,
            "POST",
            "/api/gdrive/disconnect",
            cookie_header=admin_cookie,
            json_body={"clear_index": True},
        )
        self.assertEqual(st_disc, 200, d_disc)
        self.assertFalse(d_disc["connected"])
        self.assertEqual(await self.database.list_gdrive_sync_items("admin"), {})


if __name__ == "__main__":
    unittest.main()
