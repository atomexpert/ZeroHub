import asyncio
import base64
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

LIVE_HUB_DB = ROOT_DIR / "data" / "hub.db"


def _stat_snapshot(p: Path) -> tuple[bool, int, int]:
    if not p.exists():
        return (False, 0, 0)
    st = p.stat()
    return (True, st.st_mtime_ns, st.st_size)


def _hidden_proc_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


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
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _MiniAsgiResponse:
        parsed_url = urllib.parse.urlsplit(path)
        raw_path = parsed_url.path or "/"
        qs = parsed_url.query

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


MOCK_XRAY_PY_SOURCE = r'''import json
import signal
import socket
import sys
import threading

assert sys.argv[1:] == ["run", "-c", "stdin:"], f"Unexpected argv: {sys.argv}"
raw = sys.stdin.read()
cfg = json.loads(raw)
port = int(cfg["inbounds"][0]["port"])
listen_host = cfg["inbounds"][0].get("listen", "127.0.0.1")

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind((listen_host, port))
srv.listen(32)

stop_evt = threading.Event()

def _handle_client(conn):
    try:
        conn.settimeout(2.0)
        data = conn.recv(4096)
        body = json.dumps({
            "success": True,
            "ip": "185.220.101.42",
            "country_code": "DE",
            "country": "Germany",
            "isp": "MockXrayHetzner",
        }).encode("utf-8")
        if data.startswith(b"CONNECT "):
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            hdr = (
                f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
            conn.sendall(hdr + body)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _accept_loop():
    srv.settimeout(0.2)
    while not stop_evt.is_set():
        try:
            c, _ = srv.accept()
            threading.Thread(target=_handle_client, args=(c,), daemon=True).start()
        except socket.timeout:
            continue
        except OSError:
            break

t = threading.Thread(target=_accept_loop, daemon=True)
t.start()

def _sig_stop(*_a):
    stop_evt.set()
    try:
        srv.close()
    except Exception:
        pass

signal.signal(signal.SIGTERM, _sig_stop)
signal.signal(signal.SIGINT, _sig_stop)

try:
    while not stop_evt.is_set():
        stop_evt.wait(0.2)
finally:
    try:
        srv.close()
    except Exception:
        pass
'''


class ProxyAndXrayRoutingV106Tests(unittest.IsolatedAsyncioTestCase):
    _live_db_before: tuple[bool, int, int] = (False, 0, 0)

    @classmethod
    def setUpClass(cls) -> None:
        cls._live_db_before = _stat_snapshot(LIVE_HUB_DB)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._assert_live_db_untouched()

    @classmethod
    def _assert_live_db_untouched(cls) -> None:
        if os.environ.get("HUB_ALLOW_EXTERNAL_LIVE_DB_ACTIVITY") == "1":
            return
        after = _stat_snapshot(LIVE_HUB_DB)
        assert after == cls._live_db_before, (
            f"CRITICAL: Live data/hub.db was touched! before={cls._live_db_before}, after={after}"
        )

    async def asyncSetUp(self) -> None:
        from app import agy_engine, auth, claude_engine, codex_engine, config, database, main, proxy_router

        self.config = config
        self.database = database
        self.auth = auth
        self.proxy_router = proxy_router
        self.agy_engine = agy_engine
        self.codex_engine = codex_engine
        self.claude_engine = claude_engine
        self.main = main

        self._tmp = tempfile.TemporaryDirectory(prefix="agy_hub_proxy_v106_")
        self.tmp_path = Path(self._tmp.name)

        self._orig_paths = {
            "DATA_DIR": config.DATA_DIR,
            "DB_PATH": config.DB_PATH,
            "USERS_DIR": config.USERS_DIR,
            "CLI_HOMES_DIR": config.CLI_HOMES_DIR,
            "XRAY_TOOLS_DIR": config.XRAY_TOOLS_DIR,
            "DB_MOD_PATH": database.DB_PATH,
            "HUB_XRAY_BIN": os.environ.get("HUB_XRAY_BIN"),
        }

        config.DATA_DIR = self.tmp_path / "data"
        config.DB_PATH = config.DATA_DIR / "hub_isolated_v106.db"
        config.USERS_DIR = self.tmp_path / "users"
        config.CLI_HOMES_DIR = config.DATA_DIR / "cli_homes"
        config.XRAY_TOOLS_DIR = config.DATA_DIR / "tools" / "xray"
        database.DB_PATH = config.DB_PATH

        for d in (config.DATA_DIR, config.USERS_DIR, config.CLI_HOMES_DIR, config.XRAY_TOOLS_DIR):
            d.mkdir(parents=True, exist_ok=True)

        await proxy_router.shutdown_all_tunnels()
        await database.init_db()
        await proxy_router.initialize_from_db()

    async def asyncTearDown(self) -> None:
        await self.proxy_router.shutdown_all_tunnels()

        if self._orig_paths["HUB_XRAY_BIN"] is None:
            os.environ.pop("HUB_XRAY_BIN", None)
        else:
            os.environ["HUB_XRAY_BIN"] = self._orig_paths["HUB_XRAY_BIN"]

        self.config.DATA_DIR = self._orig_paths["DATA_DIR"]
        self.config.DB_PATH = self._orig_paths["DB_PATH"]
        self.config.USERS_DIR = self._orig_paths["USERS_DIR"]
        self.config.CLI_HOMES_DIR = self._orig_paths["CLI_HOMES_DIR"]
        self.config.XRAY_TOOLS_DIR = self._orig_paths["XRAY_TOOLS_DIR"]
        self.database.DB_PATH = self._orig_paths["DB_MOD_PATH"]

        self._tmp.cleanup()
        self._assert_live_db_untouched()

    async def test_01_live_db_untouched_migrations_and_pbkdf2_fernet_encryption(self) -> None:
        """Verify live DB immutability, schema migrations, PBKDF2-200k Fernet encryption at rest, and clean wipe on global reset."""
        self._assert_live_db_untouched()

        conn = sqlite3.connect(self.database.DB_PATH)
        try:
            for tbl in ("account_profiles", "user_chatgpt_accounts", "user_claude_accounts"):
                cols = {row[1] for row in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
                self.assertIn("proxy_mode", cols, f"Missing proxy_mode in {tbl}")
                self.assertIn("proxy_spec_encrypted", cols, f"Missing proxy_spec_encrypted in {tbl}")
                self.assertIn("proxy_label", cols, f"Missing proxy_label in {tbl}")
        finally:
            conn.close()

        secret_uuid = "9f8e7d6c-5b4a-4321-9876-abcdef012345"
        secret_pbk = "SUPER_SECRET_REALITY_PBK_999"
        vless_uri = (
            f"vless://{secret_uuid}@de-fra1.example.org:443"
            f"?encryption=none&flow=xtls-rprx-vision&security=reality"
            f"&sni=www.cloudflare.com&fp=chrome&pbk={secret_pbk}&sid=1a2b3c4d&type=tcp#MyDE"
        )
        parsed = self.proxy_router.parse_proxy_spec(vless_uri)

        await self.database.save_global_proxy_settings(
            mode="custom",
            proxy_spec=vless_uri,
            proxy_label=parsed.safe_label,
        )
        g_pub = await self.database.get_global_proxy_settings(include_decrypted_proxy=False)
        self.assertEqual(g_pub["mode"], "custom")
        self.assertTrue(g_pub["has_custom_spec"])
        self.assertNotIn("proxy_spec", g_pub)
        self.assertNotIn(secret_uuid, json.dumps(g_pub))
        self.assertNotIn(secret_pbk, json.dumps(g_pub))

        g_dec = await self.database.get_global_proxy_settings(include_decrypted_proxy=True)
        self.assertEqual(g_dec["proxy_spec"], vless_uri)

        # Verify raw hub_meta SQLite storage never contains plaintext UUID or PBK
        conn = sqlite3.connect(self.database.DB_PATH)
        try:
            raw_meta = conn.execute(
                "SELECT value FROM hub_meta WHERE key = 'global_proxy_config'"
            ).fetchone()[0]
            self.assertNotIn(secret_uuid, raw_meta)
            self.assertNotIn(secret_pbk, raw_meta)
        finally:
            conn.close()

        # Switching global proxy back to 'direct' must wipe encrypted spec & label
        await self.database.save_global_proxy_settings(mode="direct")
        g_after_direct = await self.database.get_global_proxy_settings(include_decrypted_proxy=True)
        self.assertEqual(g_after_direct["mode"], "direct")
        self.assertFalse(g_after_direct["has_custom_spec"])
        self.assertEqual(g_after_direct["proxy_spec"], "")
        self.assertEqual(g_after_direct["custom_label"], "")

        # Per-account Google, ChatGPT, and Claude encrypted storage & clean wipe on switch to 'global'
        prof = await self.database.save_account_profile(
            email="admin@example.com",
            label="Main Pro",
            make_active=True,
            proxy_mode="custom",
            proxy_spec="socks5://alice_sec:pw_secret_777@nl1.example.net:1080",
            proxy_label="SOCKS5 · nl1.example.net:1080",
        )
        self.assertEqual(prof["proxy_mode"], "custom")
        self.assertTrue(prof["has_proxy_spec"])
        self.assertNotIn("pw_secret_777", json.dumps(prof))

        prof_reset = await self.database.save_account_profile(
            profile_id=prof["id"],
            proxy_mode="global",
        )
        self.assertEqual(prof_reset["proxy_mode"], "global")
        self.assertFalse(prof_reset["has_proxy_spec"])
        self.assertEqual(prof_reset["proxy_label"], "")

    async def test_02_uri_parsing_xray_json_generation_and_secret_masking(self) -> None:
        """Verify all proxy/Xray protocols (HTTP, SOCKS5, VLESS Reality/WS/gRPC/Vision, VMess, Trojan, SS SIP002/legacy) and zero secret leaks."""
        pr = self.proxy_router

        # 1. HTTP with credentials
        p_http = pr.parse_proxy_spec("http://http_user_x:http_secret_pw@de1.proxy.io:8080")
        self.assertEqual(p_http.scheme, "http")
        self.assertEqual(p_http.username, "http_user_x")
        self.assertEqual(p_http.password, "http_secret_pw")
        self.assertNotIn("http_user_x", p_http.safe_label)
        self.assertNotIn("http_secret_pw", p_http.safe_label)
        self.assertIn("de1.proxy.io:8080", p_http.safe_label)

        # 2. SOCKS5 with credentials
        p_s5 = pr.parse_proxy_spec("socks5://s5_u:s5_super_secret@nl.proxy.io:1080")
        self.assertEqual(p_s5.scheme, "socks5")
        self.assertFalse(p_s5.requires_xray)
        self.assertNotIn("s5_u", p_s5.safe_label)
        self.assertNotIn("s5_super_secret", p_s5.safe_label)
        self.assertIn("SOCKS5", p_s5.safe_label)

        # 3. VLESS Reality + Vision
        vless_uuid = "11111111-2222-4333-8444-555555555555"
        vless_pbk = "PBK_REALITY_SECRET_KEY_ABCDEF123456"
        vless_sid = "cafe0123dead"
        p_vless_real = pr.parse_proxy_spec(
            f"vless://{vless_uuid}@de-fra.example.com:443?encryption=none&flow=xtls-rprx-vision"
            f"&security=reality&sni=www.microsoft.com&fp=chrome&pbk={vless_pbk}&sid={vless_sid}&type=tcp#Мой%20DE"
        )
        self.assertTrue(p_vless_real.requires_xray)
        for secret in (vless_uuid, vless_pbk, vless_sid):
            self.assertNotIn(secret, p_vless_real.safe_label)
        self.assertIn("VLESS Reality", p_vless_real.safe_label)
        self.assertIn("Мой DE", p_vless_real.safe_label)
        cfg_real = pr.build_xray_config_json(p_vless_real, 23456)
        self.assertEqual(cfg_real["log"]["access"], "none")
        self.assertEqual(cfg_real["inbounds"][0]["listen"], "127.0.0.1")
        self.assertEqual(cfg_real["inbounds"][0]["port"], 23456)
        self.assertEqual(cfg_real["inbounds"][0]["protocol"], "http")
        self.assertEqual(cfg_real["outbounds"][0]["protocol"], "vless")
        self.assertEqual(
            cfg_real["outbounds"][0]["streamSettings"]["realitySettings"]["publicKey"],
            vless_pbk,
        )

        # 4. VLESS WS + TLS (ensure secret WS path does not leak into safe_label)
        p_vless_ws = pr.parse_proxy_spec(
            f"vless://{vless_uuid}@cdn.example.com:443?security=tls&type=ws&host=cdn.example.com&path=%2Fsecret-token-ws-987#CDN_WS"
        )
        self.assertNotIn("secret-token-ws-987", p_vless_ws.safe_label)
        cfg_ws = pr.build_xray_config_json(p_vless_ws, 23457)
        self.assertEqual(cfg_ws["outbounds"][0]["streamSettings"]["network"], "ws")
        self.assertEqual(
            cfg_ws["outbounds"][0]["streamSettings"]["wsSettings"]["path"],
            "/secret-token-ws-987",
        )

        # 5. VLESS gRPC + TLS
        p_vless_grpc = pr.parse_proxy_spec(
            f"vless://{vless_uuid}@grpc.example.com:443?security=tls&type=grpc&serviceName=privateGrpcSvc99#GRPC_FI"
        )
        self.assertNotIn("privateGrpcSvc99", p_vless_grpc.safe_label)
        cfg_grpc = pr.build_xray_config_json(p_vless_grpc, 23458)
        self.assertEqual(cfg_grpc["outbounds"][0]["streamSettings"]["network"], "grpc")
        self.assertEqual(
            cfg_grpc["outbounds"][0]["streamSettings"]["grpcSettings"]["serviceName"],
            "privateGrpcSvc99",
        )

        # 6. VMess Base64 JSON
        vmess_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        vmess_payload = base64.b64encode(
            json.dumps(
                {
                    "v": "2",
                    "ps": "VMess Finland",
                    "add": "fi1.example.com",
                    "port": "443",
                    "id": vmess_uuid,
                    "aid": "0",
                    "scy": "auto",
                    "net": "ws",
                    "host": "fi1.example.com",
                    "path": "/vmess-secret-route",
                    "tls": "tls",
                    "sni": "fi1.example.com",
                }
            ).encode("utf-8")
        ).decode("ascii")
        p_vmess = pr.parse_proxy_spec(f"vmess://{vmess_payload}")
        self.assertNotIn(vmess_uuid, p_vmess.safe_label)
        self.assertNotIn("vmess-secret-route", p_vmess.safe_label)
        cfg_vmess = pr.build_xray_config_json(p_vmess, 23459)
        self.assertEqual(cfg_vmess["outbounds"][0]["protocol"], "vmess")

        # 7. Trojan
        p_trojan = pr.parse_proxy_spec(
            "trojan://TR_SUPER_SECRET_PW_888@tr.example.com:443?security=tls&sni=tr.example.com&type=tcp#TrojanNL"
        )
        self.assertNotIn("TR_SUPER_SECRET_PW_888", p_trojan.safe_label)
        cfg_trojan = pr.build_xray_config_json(p_trojan, 23460)
        self.assertEqual(cfg_trojan["outbounds"][0]["protocol"], "trojan")
        self.assertEqual(
            cfg_trojan["outbounds"][0]["settings"]["servers"][0]["password"],
            "TR_SUPER_SECRET_PW_888",
        )

        # 8. Shadowsocks SIP002 & Legacy
        sip002_userinfo = base64.urlsafe_b64encode(b"aes-256-gcm:ss_secret_pw_111").decode("ascii").rstrip("=")
        p_ss1 = pr.parse_proxy_spec(f"ss://{sip002_userinfo}@ss1.example.com:8388#SS_One")
        self.assertNotIn("ss_secret_pw_111", p_ss1.safe_label)
        cfg_ss1 = pr.build_xray_config_json(p_ss1, 23461)
        self.assertEqual(cfg_ss1["outbounds"][0]["protocol"], "shadowsocks")

        legacy_ss = base64.b64encode(b"chacha20-ietf-poly1305:ss_legacy_pw_222@ss2.example.com:8389").decode("ascii")
        p_ss2 = pr.parse_proxy_spec(f"ss://{legacy_ss}#SS_Two")
        self.assertNotIn("ss_legacy_pw_222", p_ss2.safe_label)
        cfg_ss2 = pr.build_xray_config_json(p_ss2, 23462)
        self.assertEqual(cfg_ss2["outbounds"][0]["protocol"], "shadowsocks")

    async def test_03_ssrf_protection_and_rbac_isolation(self) -> None:
        """Verify non-admin SSRF blocks (loopback, RFC1918, CGNAT 100.64/10, link-local, DNS rebinding) and RBAC masking."""
        pr = self.proxy_router

        blocked_targets = [
            ("127.0.0.1", 10808),
            ("localhost", 8080),
            ("::1", 1080),
            ("0.0.0.0", 8080),
            ("192.168.1.1", 1080),
            ("10.0.0.5", 3128),
            ("172.20.0.2", 8080),
            ("100.64.0.10", 1080),
            ("169.254.169.254", 80),
            ("host.docker.internal", 8080),
            ("nas.local", 1080),
            ("corp.internal", 8080),
        ]
        for host, port in blocked_targets:
            with self.assertRaises(pr.ProxyConfigError, msg=f"Non-admin must be blocked from {host}:{port}"):
                pr.validate_proxy_target_security(host, port, is_admin=False, resolve_dns=False)

        # DNS rebinding simulation: external-looking domain resolving to 127.0.0.1
        with patch(
            "socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
        ):
            with self.assertRaises(pr.ProxyConfigError):
                pr.validate_proxy_target_security("rebind.attacker.example", 443, is_admin=False, resolve_dns=True)

        # Admin IS allowed to use 127.0.0.1 and LAN addresses
        pr.validate_proxy_target_security("127.0.0.1", 10808, is_admin=True, resolve_dns=False)
        pr.validate_proxy_target_security("192.168.1.50", 1080, is_admin=True, resolve_dns=False)

        # Create admin + regular user and test REST API RBAC & masking
        alice = await self.database.create_user(
            username="alice_proxy_rbac",
            password="AliceStrongPassword123!",
            display_name="Alice",
            role="user",
            max_access_mode="write_files",
        )
        admin_user = await self.database.get_user_by_username("admin")
        assert admin_user is not None
        admin_tok = self.auth.create_user_session_token(
            admin_user["id"], int(admin_user.get("session_version") or 1)
        )
        alice_tok = self.auth.create_user_session_token(
            alice["id"], int(alice.get("session_version") or 1)
        )

        await self.database.save_global_proxy_settings(
            mode="custom",
            proxy_spec="http://admin_u:admin_pw@127.0.0.1:10808",
            proxy_label="HTTP · 127.0.0.1:10808",
        )
        await pr.reload_routes_from_db()

        client = _MiniAsgiClient(self.main.app)
        # Non-admin cannot touch admin proxy endpoints
        r_forbid = await client.request(
            "PUT",
            "/api/admin/proxy",
            json_body={"mode": "direct"},
            headers={"Authorization": f"Bearer {alice_tok}"},
        )
        self.assertEqual(r_forbid.status_code, 403)

        # Non-admin /api/proxy/status hides host ports, paths, and system proxy
        r_status = await client.request(
            "GET",
            "/api/proxy/status",
            headers={"Authorization": f"Bearer {alice_tok}"},
        )
        self.assertEqual(r_status.status_code, 200)
        st_user = r_status.json()
        self.assertEqual(st_user["effective_label"], "Глобальный маршрут сервера")
        self.assertNotIn("10808", json.dumps(st_user))
        self.assertNotIn("system_detected_proxy", st_user)
        self.assertNotIn("bin_path", json.dumps(st_user))

        # Non-admin /api/proxy/test in global mode hides server exit IP & ISP
        with patch.object(
            pr,
            "_probe_exit_ip_sync",
            return_value={
                "ok": True,
                "exit_ip": "203.0.113.77",
                "country_code": "DE",
                "country": "Germany",
                "isp": "AdminSecretServerISP",
                "latency_ms": 44,
                "error": "",
            },
        ):
            r_probe_user = await client.request(
                "POST",
                "/api/proxy/test",
                json_body={"mode": "global"},
                headers={"Authorization": f"Bearer {alice_tok}"},
            )
            self.assertEqual(r_probe_user.status_code, 200)
            pj_user = r_probe_user.json()
            self.assertTrue(pj_user["ok"])
            self.assertEqual(pj_user["exit_ip"], "")
            self.assertEqual(pj_user["isp"], "")
            self.assertNotIn("203.0.113.77", json.dumps(pj_user))

            # Admin sees full exit IP & ISP
            r_probe_admin = await client.request(
                "POST",
                "/api/proxy/test",
                json_body={"mode": "custom"},
                headers={"Authorization": f"Bearer {admin_tok}"},
            )
            self.assertEqual(r_probe_admin.status_code, 200)
            pj_admin = r_probe_admin.json()
            self.assertEqual(pj_admin["exit_ip"], "203.0.113.77")
            self.assertEqual(pj_admin["isp"], "AdminSecretServerISP")

        # Non-admin SSRF rejected on ChatGPT / Claude import endpoints
        r_ssrf_cgpt = await client.request(
            "POST",
            "/api/chatgpt/import-auth",
            json_body={
                "auth_json": '{"tokens":{"access_token":"x"}}',
                "label": "SSRF Attempt",
                "proxy_mode": "custom",
                "proxy_spec": "http://127.0.0.1:10808",
            },
            headers={"Authorization": f"Bearer {alice_tok}"},
        )
        self.assertEqual(r_ssrf_cgpt.status_code, 400)

    async def test_04_honest_direct_mode_and_fail_closed_invariant(self) -> None:
        """Verify honest direct mode (stripping dirty env + NO_PROXY='*' + empty ProxyHandler) and strict Fail-Closed on broken custom route."""
        pr = self.proxy_router

        dirty_env = {
            "PATH": "/usr/bin",
            "HTTP_PROXY": "http://127.0.0.1:10808",
            "HTTPS_PROXY": "http://127.0.0.1:10808",
            "ALL_PROXY": "socks5://127.0.0.1:10809",
            "GRPC_PROXY": "http://127.0.0.1:10808",
            "http_proxy": "http://127.0.0.1:10808",
            "https_proxy": "http://127.0.0.1:10808",
            "npm_config_proxy": "http://127.0.0.1:10808",
            "npm_config_https_proxy": "http://127.0.0.1:10808",
        }
        clean = pr.apply_proxy_to_subprocess_env(dirty_env, None, include_npm=True)
        for k in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "GRPC_PROXY",
            "http_proxy",
            "https_proxy",
            "npm_config_proxy",
            "npm_config_https_proxy",
        ):
            self.assertNotIn(k, clean)
        self.assertEqual(clean["NO_PROXY"], "*")
        self.assertEqual(clean["no_proxy"], "*")

        # Verify urllib ProxyHandler({}) in direct mode ignores Windows registry proxy
        opener = pr.build_urllib_proxy_opener(None)
        proxy_handlers = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual(len(proxy_handlers), 1)
        self.assertEqual(proxy_handlers[0].proxies, {})

        # Fail-Closed: custom Xray URI when xray binary is missing MUST raise ProxyRouteError (never fallback to None/direct)
        vless_uri = (
            "vless://11111111-2222-4333-8444-555555555555@de1.example.com:443"
            "?encryption=none&security=tls&type=tcp#BrokenXray"
        )
        await self.database.save_global_proxy_settings(
            mode="custom",
            proxy_spec=vless_uri,
            proxy_label="VLESS TLS · de1.example.com:443",
        )
        await pr.reload_routes_from_db()

        with patch.object(pr, "resolve_xray_bin", return_value=None):
            with self.assertRaises(pr.ProxyRouteError):
                await pr.ensure_proxy_for_context("global")

            # OpenRouter must also fail closed without making direct HTTP calls
            from app import openrouter_service

            with patch.object(
                openrouter_service,
                "lookup_catalog_model",
                AsyncMock(
                    return_value={
                        "id": "deepseek/deepseek-v3.2",
                        "name": "DeepSeek V3.2",
                        "is_deprecated": False,
                        "prompt_price_per_m": 0.14,
                        "completion_price_per_m": 0.28,
                    }
                ),
            ):
                res_or = await openrouter_service.execute_openrouter_chat(
                    {
                        "user_id": "admin",
                        "is_enabled": True,
                        "api_key": "sk-or-v1-dummy",
                        "default_model": "deepseek/deepseek-v3.2",
                    },
                    prompt="ping",
                    model="deepseek/deepseek-v3.2",
                    skip_daily_check=True,
                )
            self.assertFalse(res_or["ok"])
            self.assertEqual(res_or["log"]["finish_reason"], "proxy_route_error")

    async def test_05_local_socks5_http_bridge_and_mock_xray_stdin_dedup(self) -> None:
        """Run a real loopback SOCKS5 server (RFC 1929 auth + ATYP=0x03) AND mock xray.py (-c stdin:, 0 disk configs, dedup, ref-count teardown)."""
        pr = self.proxy_router

        # --- Part A: Real loopback HTTP target + real SOCKS5 server with user:pass auth & ATYP=0x03 ---
        socks_seen: dict[str, Any] = {"auth_user": "", "auth_pass": "", "atyp": 0, "dest_host": ""}

        async def _target_http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                await reader.read(2048)
                payload = b'{"bridge_ok":true,"via":"local_socks5"}'
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode("ascii")
                    + payload
                )
                await writer.drain()
            finally:
                writer.close()

        target_srv = await asyncio.start_server(_target_http_handler, "127.0.0.1", 0)
        target_port = target_srv.sockets[0].getsockname()[1]

        async def _socks5_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                ver, nmethods = await reader.readexactly(2)
                assert ver == 0x05
                methods = await reader.readexactly(nmethods)
                assert 0x02 in methods, "Bridge must advertise username/password auth (0x02)"
                writer.write(b"\x05\x02")
                await writer.drain()

                auth_ver = (await reader.readexactly(1))[0]
                assert auth_ver == 0x01
                ulen = (await reader.readexactly(1))[0]
                uname = (await reader.readexactly(ulen)).decode("utf-8")
                plen = (await reader.readexactly(1))[0]
                upass = (await reader.readexactly(plen)).decode("utf-8")
                socks_seen["auth_user"] = uname
                socks_seen["auth_pass"] = upass
                if uname != "hub_s5_user" or upass != "hub_s5_pass_42":
                    writer.write(b"\x01\x01")
                    await writer.drain()
                    return
                writer.write(b"\x01\x00")
                await writer.drain()

                req_hdr = await reader.readexactly(4)
                assert req_hdr[0] == 0x05 and req_hdr[1] == 0x01
                atyp = req_hdr[3]
                socks_seen["atyp"] = atyp
                if atyp == 0x03:
                    dlen = (await reader.readexactly(1))[0]
                    dhost = (await reader.readexactly(dlen)).decode("utf-8")
                    socks_seen["dest_host"] = dhost
                elif atyp == 0x01:
                    raw_ip = await reader.readexactly(4)
                    socks_seen["dest_host"] = socket.inet_ntoa(raw_ip)
                dport = int.from_bytes(await reader.readexactly(2), "big")
                assert dport == target_port

                up_r, up_w = await asyncio.open_connection("127.0.0.1", target_port)
                writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
                await writer.drain()

                async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
                    try:
                        while True:
                            chunk = await r.read(4096)
                            if not chunk:
                                break
                            w.write(chunk)
                            await w.drain()
                    except Exception:
                        pass
                    finally:
                        try:
                            w.close()
                        except Exception:
                            pass

                await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
            finally:
                try:
                    writer.close()
                except Exception:
                    pass

        socks_srv = await asyncio.start_server(_socks5_handler, "127.0.0.1", 0)
        socks_port = socks_srv.sockets[0].getsockname()[1]

        try:
            socks_spec = f"socks5://hub_s5_user:hub_s5_pass_42@127.0.0.1:{socks_port}"
            bridge_url = await pr.ensure_proxy_for_context(
                "global",
                pending_owner="test:s5",
                pending_mode="custom",
                pending_spec=socks_spec,
                is_admin=True,
            )
            self.assertIsNotNone(bridge_url)
            assert bridge_url is not None
            self.assertTrue(bridge_url.startswith("http://127.0.0.1:"))

            def _do_http_via_bridge() -> dict[str, Any]:
                opener = pr.build_urllib_proxy_opener(bridge_url)
                req = urllib.request.Request(f"http://remote-only.example.test:{target_port}/hello")
                with opener.open(req, timeout=5.0) as resp:
                    return json.loads(resp.read().decode("utf-8"))

            resp_json = await asyncio.to_thread(_do_http_via_bridge)
            self.assertTrue(resp_json["bridge_ok"])
            self.assertEqual(socks_seen["auth_user"], "hub_s5_user")
            self.assertEqual(socks_seen["auth_pass"], "hub_s5_pass_42")
            # Crucial DNS-leak protection check: domain name was sent as ATYP=0x03 without local DNS resolution!
            self.assertEqual(socks_seen["atyp"], 0x03)
            self.assertEqual(socks_seen["dest_host"], "remote-only.example.test")
        finally:
            pr.release_owner("test:s5")
            socks_srv.close()
            target_srv.close()
            await socks_srv.wait_closed()
            await target_srv.wait_closed()

        # --- Part B: Mock xray.py via `-c stdin:`, 0 JSON files on disk, deduplication, and automatic teardown ---
        mock_xray_script = self.tmp_path / "mock_xray.py"
        mock_xray_script.write_text(MOCK_XRAY_PY_SOURCE, encoding="utf-8")
        os.environ["HUB_XRAY_BIN"] = str(mock_xray_script)

        shared_vless = (
            "vless://22222222-3333-4444-8555-666666666666@127.0.0.1:443"
            "?encryption=none&security=tls&sni=example.com&type=tcp#SharedDE"
        )
        cgpt_acc = await self.database.upsert_user_chatgpt_account(
            user_id="admin",
            raw_auth_json='{"tokens":{"access_token":"tok1"}}',
            chatgpt_account_id="cgpt_sh_1",
            email="cgpt1@example.com",
            label="CGPT Shared",
            proxy_mode="custom",
            proxy_spec=shared_vless,
            proxy_label="VLESS TLS · 127.0.0.1:443 (SharedDE)",
        )
        cld_acc = await self.database.upsert_user_claude_account(
            user_id="admin",
            raw_credentials_json='{"claudeAiOauth":{"accessToken":"sk-ant-oat01-aaa"}}',
            claude_account_id="cld_sh_1",
            email="cld1@example.com",
            label="Claude Shared",
            proxy_mode="custom",
            proxy_spec=shared_vless,
            proxy_label="VLESS TLS · 127.0.0.1:443 (SharedDE)",
        )
        await pr.reload_routes_from_db()

        url_cgpt = await pr.ensure_proxy_for_context(
            "chatgpt", user_id="admin", account_id=cgpt_acc["id"], is_admin=True
        )
        url_cld = await pr.ensure_proxy_for_context(
            "claude", user_id="admin", account_id=cld_acc["id"], is_admin=True
        )
        self.assertIsNotNone(url_cgpt)
        # Deduplication: identical Xray config shares the exact same single xray process & port!
        self.assertEqual(url_cgpt, url_cld)
        st_active = pr.get_proxy_status_payload(is_admin=True, probe_xray_version=False)
        self.assertEqual(st_active["active_tunnels_count"], 1)

        # Verify 0 JSON config files written anywhere inside DATA_DIR / XRAY_TOOLS_DIR
        disk_jsons = list(self.config.DATA_DIR.rglob("*.json"))
        self.assertEqual(disk_jsons, [], f"Zero Xray JSON files allowed on disk, found: {disk_jsons}")

        # Switch ChatGPT account to global -> Claude still owns the tunnel, so active_tunnels_count stays 1
        await self.database.patch_user_chatgpt_account(
            "admin", cgpt_acc["id"], proxy_mode="global"
        )
        await pr.reload_routes_from_db()
        self.assertEqual(
            pr.get_proxy_status_payload(is_admin=True, probe_xray_version=False)["active_tunnels_count"],
            1,
        )

        # Switch Claude account to global -> 0 owners left -> xray process automatically stopped!
        await self.database.patch_user_claude_account(
            "admin", cld_acc["id"], proxy_mode="global"
        )
        await pr.reload_routes_from_db()
        self.assertEqual(
            pr.get_proxy_status_payload(is_admin=True, probe_xray_version=False)["active_tunnels_count"],
            0,
        )

    async def test_06_engines_e2e_failover_and_static_v107_invariants(self) -> None:
        """Verify propagation into agy_engine, codex_engine (auth + failover), claude_engine (auth + failover), JS syntax, and CSS invariants."""
        pr = self.proxy_router

        # 1. agy_engine._build_subprocess_env propagation
        env_agy = self.agy_engine._build_subprocess_env(proxy_url="http://127.0.0.1:19901")
        self.assertEqual(env_agy["HTTP_PROXY"], "http://127.0.0.1:19901")
        self.assertEqual(env_agy["HTTPS_PROXY"], "http://127.0.0.1:19901")
        env_agy_direct = self.agy_engine._build_subprocess_env(proxy_url=None)
        self.assertNotIn("HTTP_PROXY", env_agy_direct)
        self.assertEqual(env_agy_direct["NO_PROXY"], "*")

        # 2. codex_engine: start_chatgpt_device_auth + auto_failover switches proxy env between Account 1 and Account 2
        dev_spawn_envs: list[dict[str, str]] = []

        class _FakeDeviceLoginProc:
            def __init__(self) -> None:
                self.pid = None
                self.stdin = None
                self.returncode = None
                self.stdout = _FakeCodexProc._make_reader(
                    [
                        b"Open https://auth.openai.com/codex/device and enter code ABCD-12345\n",
                    ]
                )
                self.stderr = _FakeCodexProc._make_reader([])

            async def wait(self) -> int:
                await asyncio.sleep(5.0)
                return 0

            def terminate(self) -> None:
                self.returncode = 0

            def kill(self) -> None:
                self.returncode = 0

        async def _fake_dev_spawn(*_args: Any, **kwargs: Any) -> _FakeDeviceLoginProc:
            dev_spawn_envs.append(dict(kwargs.get("env") or {}))
            return _FakeDeviceLoginProc()

        acc1 = await self.database.upsert_user_chatgpt_account(
            user_id="admin",
            raw_auth_json='{"tokens":{"access_token":"t1"}}',
            chatgpt_account_id="cgpt_u1",
            email="u1@example.com",
            label="Primary DE",
            make_active=True,
            proxy_mode="custom",
            proxy_spec="http://127.0.0.1:21001",
            proxy_label="HTTP · 127.0.0.1:21001",
        )
        acc2 = await self.database.upsert_user_chatgpt_account(
            user_id="admin",
            raw_auth_json='{"tokens":{"access_token":"t2"}}',
            chatgpt_account_id="cgpt_u2",
            email="u2@example.com",
            label="Backup FI",
            make_active=False,
            proxy_mode="custom",
            proxy_spec="http://127.0.0.1:21002",
            proxy_label="HTTP · 127.0.0.1:21002",
        )
        await pr.reload_routes_from_db()

        seen_codex_proxies: list[str] = []

        class _FakeStdin:
            def __init__(self, on_write: Any = None) -> None:
                self._buf = b""
                self._on_write = on_write

            def write(self, b: bytes) -> None:
                if self._on_write is None:
                    return
                self._buf += b
                while b"\n" in self._buf:
                    raw_ln, self._buf = self._buf.split(b"\n", 1)
                    ln = raw_ln.decode("utf-8", errors="replace").strip()
                    if ln:
                        self._on_write(ln)

            async def drain(self) -> None:
                pass

            def close(self) -> None:
                pass

        class _FakeCodexProc:
            def __init__(self, attempt_idx: int) -> None:
                self.pid = None
                self.returncode: Optional[int] = None
                self._attempt = attempt_idx
                self.stdout = asyncio.StreamReader()
                self.stderr = self._make_reader([])
                self.stdin = _FakeStdin(self._handle_rpc_line)

            def _emit_json(self, obj: dict[str, Any]) -> None:
                self.stdout.feed_data(json.dumps(obj).encode("utf-8") + b"\n")

            def _handle_rpc_line(self, line: str) -> None:
                try:
                    msg = json.loads(line)
                except Exception:
                    return
                rid = msg.get("id")
                method = msg.get("method")
                if method == "initialize":
                    self._emit_json({"jsonrpc": "2.0", "id": rid, "result": {"userAgent": "mock"}})
                elif method == "thread/start":
                    self._emit_json(
                        {"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": f"th_{self._attempt}"}}}
                    )
                elif method == "turn/start":
                    self._emit_json(
                        {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "result": {"turn": {"id": f"tu_{self._attempt}", "status": "inProgress"}},
                        }
                    )
                    if self._attempt == 0:
                        self._emit_json(
                            {
                                "jsonrpc": "2.0",
                                "method": "turn/completed",
                                "params": {
                                    "turn": {
                                        "id": "tu_0",
                                        "status": "failed",
                                        "error": {
                                            "message": "You have hit your ChatGPT usage_limit_reached (5-hour quota exhausted)."
                                        },
                                    }
                                },
                            }
                        )
                        self.returncode = 0
                        self.stdout.feed_eof()
                    else:
                        self._emit_json(
                            {
                                "jsonrpc": "2.0",
                                "method": "item/agentMessage/delta",
                                "params": {"itemId": "msg_1", "delta": "OK from Backup FI"},
                            }
                        )
                        self._emit_json(
                            {
                                "jsonrpc": "2.0",
                                "method": "turn/completed",
                                "params": {"turn": {"id": "tu_1", "status": "completed"}},
                            }
                        )
                        self.returncode = 0
                        self.stdout.feed_eof()
                elif method == "account/rateLimits/read":
                    self._emit_json(
                        {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "result": {
                                "rateLimits": {
                                    "planType": "plus",
                                    "primary": {
                                        "usedPercent": 15.0,
                                        "windowDurationMins": 300,
                                        "resetsAt": 1999999999,
                                    },
                                }
                            },
                        }
                    )

            @staticmethod
            def _make_reader(lines: list[bytes]) -> asyncio.StreamReader:
                r = asyncio.StreamReader()
                for ln in lines:
                    r.feed_data(ln)
                r.feed_eof()
                return r

            async def wait(self) -> int:
                self.returncode = 0
                return 0

            def terminate(self) -> None:
                self.returncode = 0

            def kill(self) -> None:
                self.returncode = 0

        with (
            patch.object(self.codex_engine, "resolve_codex_bin", return_value="codex-mock"),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_dev_spawn),
        ):
            dev_flow = await self.codex_engine.start_chatgpt_device_auth(
                "admin",
                label="PreLoginCustom",
                proxy_mode="custom",
                proxy_spec="http://127.0.0.1:21999",
                is_admin=True,
            )
            self.assertEqual(dev_flow["proxy_mode"], "custom")
            self.assertEqual(dev_spawn_envs[0].get("HTTP_PROXY"), "http://127.0.0.1:21999")
            await self.codex_engine.cancel_chatgpt_device_auth_flow("admin", dev_flow["flow_id"])

        async def _fake_codex_spawn(*_args: Any, **kwargs: Any) -> _FakeCodexProc:
            env = kwargs.get("env") or {}
            seen_codex_proxies.append(str(env.get("HTTP_PROXY") or ""))
            return _FakeCodexProc(len(seen_codex_proxies) - 1)

        codex_events: list[dict[str, Any]] = []
        with (
            patch.object(self.codex_engine, "resolve_codex_bin", return_value="codex-mock"),
            patch.object(self.codex_engine, "sync_user_cli_rules", return_value=None),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_codex_spawn),
        ):
            async for ev in self.codex_engine.stream_codex_turn(
                chat_id="chat_failover_test",
                kind="project",
                project_name="DemoProj",
                model="codex:gpt-5.4@medium",
                access_mode="read_only",
                prompt="Hello failover",
                attachments=[],
                codex_thread_id="",
                codex_account_id="",
                history_messages=[],
                user={"id": "admin", "role": "admin", "username": "admin"},
                resolved_project_dir=self.tmp_path,
            ):
                codex_events.append(ev)

        self.assertEqual(seen_codex_proxies, ["http://127.0.0.1:21001", "http://127.0.0.1:21002"])
        self.assertTrue(any(e.get("type") == "chatgpt_failover" for e in codex_events))
        codex_text = "".join(str(e.get("delta") or "") for e in codex_events if e.get("type") == "text_delta")
        self.assertIn("OK from Backup FI", codex_text)
        self.assertEqual(acc1["id"] != acc2["id"], True)

        # 3. claude_engine: start_claude_oauth_flow + auto_failover switches proxy env between Claude Account 1 and Account 2
        claude_oauth_envs: list[dict[str, str]] = []

        class _FakeClaudeOAuthProc:
            def __init__(self) -> None:
                self.pid = None
                self.stdin = _FakeStdin()
                self.returncode = None
                self.stdout = _FakeCodexProc._make_reader(
                    [b"Visit https://claude.ai/oauth/authorize?client_id=test to sign in\n"]
                )
                self.stderr = _FakeCodexProc._make_reader([])

            async def wait(self) -> int:
                await asyncio.sleep(5.0)
                return 0

            def terminate(self) -> None:
                self.returncode = 0

            def kill(self) -> None:
                self.returncode = 0

        async def _fake_claude_oauth_spawn(*_args: Any, **kwargs: Any) -> _FakeClaudeOAuthProc:
            claude_oauth_envs.append(dict(kwargs.get("env") or {}))
            return _FakeClaudeOAuthProc()

        with (
            patch.object(self.claude_engine, "resolve_claude_bin", return_value="claude-mock"),
            patch.object(
                self.claude_engine,
                "build_claude_exec_argv",
                side_effect=lambda b, *a: [b, *a],
            ),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_claude_oauth_spawn),
        ):
            cld_flow = await self.claude_engine.start_claude_oauth_flow(
                "admin",
                "ClaudePreLogin",
                proxy_mode="custom",
                proxy_spec="http://127.0.0.1:22999",
                is_admin=True,
            )
            self.assertEqual(cld_flow["proxy_mode"], "custom")
            self.assertEqual(claude_oauth_envs[0].get("HTTP_PROXY"), "http://127.0.0.1:22999")
            await self.claude_engine.cancel_claude_oauth_flow("admin", cld_flow["flow_id"])

        cld1 = await self.database.upsert_user_claude_account(
            user_id="admin",
            raw_credentials_json='{"claudeAiOauth":{"accessToken":"sk-ant-oat01-111"}}',
            claude_account_id="cld_u1",
            email="c1@example.com",
            label="Claude DE",
            make_active=True,
            proxy_mode="custom",
            proxy_spec="http://127.0.0.1:22001",
            proxy_label="HTTP · 127.0.0.1:22001",
        )
        cld2 = await self.database.upsert_user_claude_account(
            user_id="admin",
            raw_credentials_json='{"claudeAiOauth":{"accessToken":"sk-ant-oat01-222"}}',
            claude_account_id="cld_u2",
            email="c2@example.com",
            label="Claude NL",
            make_active=False,
            proxy_mode="custom",
            proxy_spec="http://127.0.0.1:22002",
            proxy_label="HTTP · 127.0.0.1:22002",
        )
        await pr.reload_routes_from_db()

        seen_claude_proxies: list[str] = []

        class _FakeClaudeProc:
            def __init__(self, attempt_idx: int) -> None:
                self.pid = None
                self.stdin = _FakeStdin()
                self.returncode = 1 if attempt_idx == 0 else 0
                if attempt_idx == 0:
                    lines = [
                        json.dumps({"type": "system", "subtype": "init", "session_id": "sess_cld_0"}).encode()
                        + b"\n",
                        json.dumps(
                            {
                                "type": "result",
                                "subtype": "error_during_execution",
                                "is_error": True,
                                "result": 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}} 5-hour limit reached ∙ resets 5pm',
                            }
                        ).encode()
                        + b"\n",
                    ]
                else:
                    lines = [
                        json.dumps({"type": "system", "subtype": "init", "session_id": "sess_cld_1"}).encode()
                        + b"\n",
                        json.dumps(
                            {
                                "type": "stream_event",
                                "event": {
                                    "type": "content_block_delta",
                                    "index": 0,
                                    "delta": {"type": "text_delta", "text": "OK from Claude NL"},
                                },
                            }
                        ).encode()
                        + b"\n",
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {"content": [{"type": "text", "text": "OK from Claude NL"}]},
                            }
                        ).encode()
                        + b"\n",
                        json.dumps(
                            {
                                "type": "result",
                                "subtype": "success",
                                "is_error": False,
                                "result": "OK from Claude NL",
                                "session_id": "sess_cld_1",
                                "usage": {"input_tokens": 12, "output_tokens": 6},
                            }
                        ).encode()
                        + b"\n",
                    ]
                self.stdout = _FakeCodexProc._make_reader(lines)
                self.stderr = _FakeCodexProc._make_reader([])

            async def wait(self) -> int:
                return int(self.returncode or 0)

            def terminate(self) -> None:
                pass

            def kill(self) -> None:
                pass

        async def _fake_claude_spawn(*_args: Any, **kwargs: Any) -> _FakeClaudeProc:
            env = kwargs.get("env") or {}
            seen_claude_proxies.append(str(env.get("HTTP_PROXY") or ""))
            return _FakeClaudeProc(len(seen_claude_proxies) - 1)

        claude_events: list[dict[str, Any]] = []
        with (
            patch.object(self.claude_engine, "resolve_claude_bin", return_value="claude-mock"),
            patch.object(
                self.claude_engine,
                "build_claude_exec_argv",
                side_effect=lambda b, *a: [b, *a],
            ),
            patch.object(self.claude_engine, "sync_user_cli_rules", return_value=None),
            patch.object(self.claude_engine, "refresh_claude_account_quotas", AsyncMock(return_value={})),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_claude_spawn),
        ):
            async for ev in self.claude_engine.stream_claude_turn(
                chat_id="chat_cld_failover",
                kind="project",
                project_name="DemoProj",
                model="claude-code:sonnet@high",
                access_mode="read_only",
                prompt="Hello Claude failover",
                attachments=[],
                claude_session_id="",
                claude_account_id="",
                history_messages=[],
                user={"id": "admin", "role": "admin", "username": "admin"},
                resolved_project_dir=self.tmp_path,
            ):
                claude_events.append(ev)

        self.assertEqual(seen_claude_proxies, ["http://127.0.0.1:22001", "http://127.0.0.1:22002"])
        self.assertTrue(any(e.get("type") == "claude_failover" for e in claude_events))
        claude_text = "".join(str(e.get("delta") or "") for e in claude_events if e.get("type") == "text_delta")
        self.assertIn("OK from Claude NL", claude_text)
        self.assertNotEqual(cld1["id"], cld2["id"])

        # 4. Static JS syntax check (`node --check`), UTF-8 without BOM, cache sync, and CSS invariant
        env_no_gpu = {**os.environ, "CUDA_VISIBLE_DEVICES": "", "ORT_DISABLE_ALL_GPU": "1"}
        for js_rel in ("app/static/app.js", "app/static/sw.js"):
            proc = subprocess.run(
                ["node", "--check", str(ROOT_DIR / js_rel)],
                capture_output=True,
                text=True,
                env=env_no_gpu,
                **_hidden_proc_kwargs(),
            )
            self.assertEqual(proc.returncode, 0, f"node --check failed for {js_rel}:\n{proc.stderr}")

        touched_files = [
            "app/config.py",
            "app/proxy_router.py",
            "app/database.py",
            "app/agy_engine.py",
            "app/codex_engine.py",
            "app/claude_engine.py",
            "app/openrouter_service.py",
            "app/tts_service.py",
            "app/voice.py",
            "app/main.py",
            "app/static/index.html",
            "app/static/styles.css",
            "app/static/app.js",
            "app/static/sw.js",
            "tests/test_proxy_and_xray_routing_v106.py",
        ]
        for rel in touched_files:
            raw = (ROOT_DIR / rel).read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{rel} MUST be UTF-8 without BOM!")
            raw.decode("utf-8")

        sw_text = (ROOT_DIR / "app" / "static" / "sw.js").read_text(encoding="utf-8")
        html_text = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")

        m_sw_cache = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_text)
        m_sw_css = re.search(r"/static/styles\.css\?v=(\d+)", sw_text)
        m_sw_js = re.search(r"/static/app\.js\?v=(\d+)", sw_text)
        m_html_css = re.search(r"/static/styles\.css\?v=(\d+)", html_text)
        m_html_js = re.search(r"/static/app\.js\?v=(\d+)", html_text)
        for m in (m_sw_cache, m_sw_css, m_sw_js, m_html_css, m_html_js):
            self.assertIsNotNone(m)
            assert m is not None
            self.assertGreaterEqual(int(m.group(1)), 106)
        assert m_sw_cache and m_sw_css and m_sw_js and m_html_css and m_html_js
        versions = {
            int(m_sw_cache.group(1)),
            int(m_sw_css.group(1)),
            int(m_sw_js.group(1)),
            int(m_html_css.group(1)),
            int(m_html_js.group(1)),
        }
        self.assertEqual(len(versions), 1, f"Static cache version mismatch: {versions}")

        for expected_id in (
            "adminGlobalProxyCard",
            "globalProxyModeGroup",
            "globalProxySpecInput",
            "installXrayCoreBtn",
            "testGlobalProxyBtn",
            "accProxyModeGroup",
            "cgptNewProxyModeGroup",
            "claudeNewProxyModeGroup",
        ):
            self.assertIn(f'id="{expected_id}"', html_text)

        css_text = (ROOT_DIR / "app" / "static" / "styles.css").read_text(encoding="utf-8")
        forbidden_props = (
            "transform",
            "filter",
            "backdrop-filter",
            "perspective",
            "contain",
            "container-type",
        )
        for m in re.finditer(r"(?<![-\w])main\.workspace\s*\{([^}]*)\}", css_text):
            block = m.group(1)
            for prop in forbidden_props:
                self.assertIsNone(
                    re.search(rf"(?<![-\w]){re.escape(prop)}\s*:", block),
                    f"Forbidden property `{prop}` found inside `main.workspace` rule!",
                )


if __name__ == "__main__":
    unittest.main()
