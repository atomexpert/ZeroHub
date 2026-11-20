from __future__ import annotations

import asyncio
import base64
import collections
import ctypes
import hashlib
import io
import ipaddress
import json
import logging
import os
import platform
import re
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import config

logger = logging.getLogger("agy_hub.proxy_router")


class ProxyConfigError(ValueError):
    """Raised when a proxy URI has invalid syntax or violates SSRF policy."""


class ProxyRouteError(RuntimeError):
    """Fail-Closed error when an explicit custom proxy / Xray tunnel cannot be established."""


_PROXY_ENV_KEYS: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "GRPC_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "grpc_proxy",
    "npm_config_proxy",
    "npm_config_https_proxy",
    "NPM_CONFIG_PROXY",
    "NPM_CONFIG_HTTPS_PROXY",
)

_LOCAL_NO_PROXY_HOSTS = "localhost,127.0.0.1,::1,host.docker.internal"
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")

_PROBE_ENDPOINTS: list[tuple[str, str]] = [
    ("ipwho", "https://ipwho.is/?fields=success,ip,country_code,country,isp,connection"),
    ("cf_trace", "https://www.cloudflare.com/cdn-cgi/trace"),
    ("ipify", "https://api.ipify.org?format=json"),
]


@dataclass
class ParsedProxySpec:
    protocol: str  # http | https | socks5 | socks5h | vless | vmess | trojan | ss
    host: str
    port: int
    username: str = ""
    password: str = ""
    remark: str = ""
    security: str = ""
    transport: str = ""
    xray_outbound: Optional[dict[str, Any]] = None
    safe_label: str = ""
    config_hash: str = ""

    @property
    def scheme(self) -> str:
        return self.protocol

    @property
    def needs_xray(self) -> bool:
        return self.protocol in ("vless", "vmess", "trojan", "ss")

    @property
    def requires_xray(self) -> bool:
        return self.needs_xray

    @property
    def needs_local_bridge(self) -> bool:
        if self.protocol in ("socks5", "socks5h"):
            return True
        if self.protocol in ("http", "https") and (self.username or self.password):
            return True
        return False

    @property
    def direct_proxy_url(self) -> str:
        host_fmt = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"{self.protocol}://{host_fmt}:{self.port}"


# ---------------------------------------------------------------------------
# 1. Proxy & Xray URI Parser + Safe Secret Masking
# ---------------------------------------------------------------------------

_UUID_LIKE_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_=-]{22,}")


def _sanitize_remark_for_label(raw_remark: str) -> str:
    txt = urllib.parse.unquote(str(raw_remark or "")).strip()
    txt = re.sub(r"[\x00-\x1f\x7f]+", " ", txt).strip()
    if not txt:
        return ""
    txt = _UUID_LIKE_RE.sub("", txt)
    txt = _LONG_TOKEN_RE.sub("", txt)
    txt = re.sub(r"\s{2,}", " ", txt).strip(" -·|()[]")
    if len(txt) > 28:
        txt = txt[:27].rstrip() + "…"
    return txt


def build_safe_proxy_label(parsed: ParsedProxySpec) -> str:
    """Build a human-readable route label guaranteed free of UUIDs, passwords, pbk, sid, or tokens."""
    host_disp = parsed.host
    if len(host_disp) > 42:
        host_disp = host_disp[:39] + "…"
    host_port = f"[{host_disp}]:{parsed.port}" if ":" in host_disp else f"{host_disp}:{parsed.port}"

    p = parsed.protocol
    sec = (parsed.security or "").lower()
    if p == "http":
        proto_tag = "HTTP"
    elif p == "https":
        proto_tag = "HTTPS"
    elif p in ("socks5", "socks5h"):
        proto_tag = "SOCKS5"
    elif p == "vless":
        if sec == "reality":
            proto_tag = "VLESS Reality"
        elif sec == "tls":
            proto_tag = "VLESS TLS"
        else:
            proto_tag = "VLESS"
    elif p == "vmess":
        proto_tag = "VMess TLS" if sec == "tls" else "VMess"
    elif p == "trojan":
        proto_tag = "Trojan Reality" if sec == "reality" else "Trojan"
    elif p == "ss":
        proto_tag = "Shadowsocks"
    else:
        proto_tag = p.upper()

    clean_rem = _sanitize_remark_for_label(parsed.remark)
    if clean_rem and clean_rem.lower() not in host_disp.lower():
        return f"{proto_tag} · {host_port} ({clean_rem})"
    return f"{proto_tag} · {host_port}"


def _b64_decode_padded(raw: str) -> bytes:
    s = re.sub(r"\s+", "", str(raw or ""))
    if not s:
        raise ProxyConfigError("Пустая Base64-строка в спецификации прокси")
    pad = (-len(s)) % 4
    s_padded = s + ("=" * pad)
    try:
        return base64.urlsafe_b64decode(s_padded.encode("ascii"))
    except Exception:
        try:
            return base64.b64decode(s_padded.encode("ascii"))
        except Exception as exc:
            raise ProxyConfigError("Некорректное Base64-кодирование в ссылке прокси") from exc


def _first_qs(qs: dict[str, list[str]], *keys: str, default: str = "") -> str:
    for k in keys:
        vals = qs.get(k)
        if vals and str(vals[0]).strip():
            return str(vals[0]).strip()
    return default


def _parse_host_port_authority(authority: str) -> tuple[str, int]:
    auth = (authority or "").strip()
    if not auth:
        raise ProxyConfigError("Не указан хост и порт прокси-сервера")
    try:
        sp = urllib.parse.urlsplit(f"//{auth}")
        host = (sp.hostname or "").strip()
        port = sp.port
    except ValueError as exc:
        raise ProxyConfigError(f"Некорректный адрес прокси: {exc}") from exc
    if not host or port is None or not (1 <= int(port) <= 65535):
        raise ProxyConfigError("Укажите корректные хост и порт (1..65535)")
    return host, int(port)


def _build_xray_stream_settings(
    *,
    host: str,
    transport_raw: str,
    security_raw: str,
    sni: str = "",
    fp: str = "",
    alpn_raw: str = "",
    pbk: str = "",
    sid: str = "",
    spx: str = "",
    path: str = "",
    ws_host: str = "",
    service_name: str = "",
    mode: str = "",
    header_type: str = "",
    allow_insecure: bool = False,
) -> tuple[dict[str, Any], str, str]:
    net = (transport_raw or "tcp").strip().lower()
    if net in ("splithttp", "xhttp"):
        xray_net = "xhttp"
    elif net in ("h2", "http"):
        xray_net = "http"
    elif net in ("tcp", "ws", "grpc", "httpupgrade"):
        xray_net = net
    else:
        raise ProxyConfigError(f"Неподдерживаемый транспорт Xray: {net}")

    sec = (security_raw or "none").strip().lower()
    if sec in ("", "0", "false"):
        sec = "none"
    if sec not in ("none", "tls", "reality"):
        raise ProxyConfigError(f"Неподдерживаемый режим шифрования транспорта: {sec}")

    stream: dict[str, Any] = {
        "network": xray_net,
        "security": sec,
    }

    eff_sni = (sni or ws_host or host).strip()
    alpn_list = [x.strip() for x in (alpn_raw or "").split(",") if x.strip()]

    if sec == "tls":
        tls_cfg: dict[str, Any] = {"serverName": eff_sni}
        if fp:
            tls_cfg["fingerprint"] = fp
        if alpn_list:
            tls_cfg["alpn"] = alpn_list
        if allow_insecure:
            tls_cfg["allowInsecure"] = True
        stream["tlsSettings"] = tls_cfg
    elif sec == "reality":
        if not pbk:
            raise ProxyConfigError("В ссылке Reality отсутствует обязательный публичный ключ (`pbk`)")
        stream["realitySettings"] = {
            "show": False,
            "fingerprint": fp or "chrome",
            "serverName": eff_sni,
            "publicKey": pbk,
            "shortId": sid or "",
            "spiderX": spx or "/",
        }

    if xray_net == "tcp":
        if (header_type or "").strip().lower() == "http":
            req_headers: dict[str, Any] = {}
            if ws_host:
                req_headers["Host"] = [h.strip() for h in ws_host.split(",") if h.strip()]
            stream["tcpSettings"] = {
                "header": {
                    "type": "http",
                    "request": {
                        "path": [path or "/"],
                        "headers": req_headers,
                    },
                }
            }
    elif xray_net == "ws":
        ws_cfg: dict[str, Any] = {"path": path or "/"}
        if ws_host:
            ws_cfg["host"] = ws_host
            ws_cfg["headers"] = {"Host": ws_host}
        stream["wsSettings"] = ws_cfg
    elif xray_net == "grpc":
        stream["grpcSettings"] = {
            "serviceName": service_name or path.lstrip("/"),
            "multiMode": (mode or "").strip().lower() == "multi",
        }
    elif xray_net == "httpupgrade":
        hu_cfg: dict[str, Any] = {"path": path or "/"}
        if ws_host:
            hu_cfg["host"] = ws_host
        stream["httpupgradeSettings"] = hu_cfg
    elif xray_net == "xhttp":
        xh_cfg: dict[str, Any] = {"path": path or "/", "mode": mode or "auto"}
        if ws_host:
            xh_cfg["host"] = ws_host
        stream["xhttpSettings"] = xh_cfg
    elif xray_net == "http":
        h2_cfg: dict[str, Any] = {"path": path or "/"}
        if ws_host:
            h2_cfg["host"] = [h.strip() for h in ws_host.split(",") if h.strip()]
        stream["httpSettings"] = h2_cfg

    return stream, xray_net, sec


def _finalize_parsed_spec(spec: ParsedProxySpec) -> ParsedProxySpec:
    spec.safe_label = build_safe_proxy_label(spec)
    if spec.xray_outbound is not None:
        canonical = json.dumps(spec.xray_outbound, sort_keys=True, separators=(",", ":"))
    else:
        canonical = json.dumps(
            {
                "protocol": spec.protocol,
                "host": spec.host.lower(),
                "port": spec.port,
                "username": spec.username,
                "password": spec.password,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    spec.config_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return spec


def parse_proxy_spec(raw_spec: str) -> ParsedProxySpec:
    """Parse and normalize HTTP/HTTPS, SOCKS5, VLESS, VMess, Trojan, or Shadowsocks proxy URI."""
    raw = str(raw_spec or "").strip()
    if not raw:
        raise ProxyConfigError("Пустая строка адреса прокси")
    if len(raw) > 4096:
        raise ProxyConfigError("Спецификация прокси превышает допустимую длину (4096 символов)")
    if any(ch in raw for ch in ("\r", "\n", "\x00")):
        raise ProxyConfigError("Спецификация прокси содержит недопустимые управляющие символы")

    if "://" not in raw:
        raw = f"http://{raw}"

    scheme_prefix = raw.split("://", 1)[0].strip().lower()
    if scheme_prefix == "socks":
        raw = "socks5://" + raw.split("://", 1)[1]
        scheme_prefix = "socks5"

    # 1. VMess (`vmess://<base64-json>`)
    if scheme_prefix == "vmess":
        rest = raw.split("://", 1)[1]
        frag_remark = ""
        if "#" in rest:
            rest, frag_remark = rest.split("#", 1)
        decoded_bytes = _b64_decode_padded(urllib.parse.unquote(rest))
        try:
            vm = json.loads(decoded_bytes.decode("utf-8"))
        except Exception as exc:
            raise ProxyConfigError("Некорректный JSON внутри ссылки vmess://") from exc
        if not isinstance(vm, dict):
            raise ProxyConfigError("Структура vmess:// должна быть JSON-объектом")

        host = str(vm.get("add") or "").strip()
        try:
            port = int(str(vm.get("port") or "0").strip())
        except ValueError as exc:
            raise ProxyConfigError("Некорректный порт в ссылке vmess://") from exc
        user_uuid = str(vm.get("id") or "").strip()
        if not host or not (1 <= port <= 65535) or not user_uuid:
            raise ProxyConfigError("В ссылке vmess:// отсутствуют обязательные поля add/port/id")
        if any(c.isspace() for c in host):
            raise ProxyConfigError("Некорректное имя хоста в vmess://")

        try:
            alter_id = max(0, int(str(vm.get("aid") or "0").strip() or "0"))
        except ValueError:
            alter_id = 0
        cipher = str(vm.get("scy") or vm.get("security") or "auto").strip() or "auto"
        tls_field = str(vm.get("tls") or "").strip().lower()
        sec_mode = tls_field if tls_field in ("tls", "reality") else "none"
        stream, xray_net, eff_sec = _build_xray_stream_settings(
            host=host,
            transport_raw=str(vm.get("net") or "tcp"),
            security_raw=sec_mode,
            sni=str(vm.get("sni") or ""),
            fp=str(vm.get("fp") or ""),
            alpn_raw=str(vm.get("alpn") or ""),
            pbk=str(vm.get("pbk") or ""),
            sid=str(vm.get("sid") or ""),
            spx=str(vm.get("spx") or ""),
            path=str(vm.get("path") or ""),
            ws_host=str(vm.get("host") or ""),
            service_name=str(vm.get("path") or ""),
            header_type=str(vm.get("type") or ""),
            allow_insecure=str(vm.get("allowInsecure") or "").lower() in ("1", "true"),
        )
        outbound = {
            "tag": "proxy",
            "protocol": "vmess",
            "settings": {
                "vnext": [
                    {
                        "address": host,
                        "port": port,
                        "users": [
                            {
                                "id": user_uuid,
                                "alterId": alter_id,
                                "security": cipher,
                            }
                        ],
                    }
                ]
            },
            "streamSettings": stream,
        }
        remark = frag_remark or str(vm.get("ps") or "")
        return _finalize_parsed_spec(
            ParsedProxySpec(
                protocol="vmess",
                host=host,
                port=port,
                username=user_uuid,
                remark=remark,
                security=eff_sec,
                transport=xray_net,
                xray_outbound=outbound,
            )
        )

    # 2. Shadowsocks (`ss://...`) — SIP002 & Legacy Base64
    if scheme_prefix == "ss":
        rest = raw.split("://", 1)[1]
        remark = ""
        if "#" in rest:
            rest, remark = rest.split("#", 1)
        query_part = ""
        if "?" in rest:
            rest, query_part = rest.split("?", 1)
        _ = query_part  # optional plugin params ignored for core SS outbound

        if "@" in rest:
            userinfo_raw, hostport_raw = rest.rsplit("@", 1)
            host, port = _parse_host_port_authority(hostport_raw)
            unquoted_ui = urllib.parse.unquote(userinfo_raw)
            if ":" in unquoted_ui:
                method, password = unquoted_ui.split(":", 1)
            else:
                dec_ui = _b64_decode_padded(unquoted_ui).decode("utf-8", errors="strict")
                if ":" not in dec_ui:
                    raise ProxyConfigError("Некорректные данные method:password в ссылке ss://")
                method, password = dec_ui.split(":", 1)
        else:
            dec_all = _b64_decode_padded(urllib.parse.unquote(rest)).decode("utf-8", errors="strict")
            if "@" not in dec_all:
                raise ProxyConfigError("Некорректный формат классической ссылки ss://")
            creds_part, hostport_raw = dec_all.rsplit("@", 1)
            if ":" not in creds_part:
                raise ProxyConfigError("В ссылке ss:// отсутствует разделитель method:password")
            method, password = creds_part.split(":", 1)
            host, port = _parse_host_port_authority(hostport_raw)

        method = method.strip()
        if not method or not password:
            raise ProxyConfigError("В ссылке ss:// должны быть указаны шифр и пароль")
        outbound = {
            "tag": "proxy",
            "protocol": "shadowsocks",
            "settings": {
                "servers": [
                    {
                        "address": host,
                        "port": port,
                        "method": method,
                        "password": password,
                    }
                ]
            },
        }
        return _finalize_parsed_spec(
            ParsedProxySpec(
                protocol="ss",
                host=host,
                port=port,
                username=method,
                password=password,
                remark=remark,
                security="ss",
                transport="tcp",
                xray_outbound=outbound,
            )
        )

    # 3. Standard URL-structured protocols (`http`, `https`, `socks5`, `socks5h`, `vless`, `trojan`)
    if scheme_prefix not in ("http", "https", "socks5", "socks5h", "vless", "trojan"):
        raise ProxyConfigError(
            f"Неподдерживаемый протокол «{scheme_prefix}://». Поддерживаются: http, https, socks5, vless, vmess, trojan, ss."
        )

    try:
        sp = urllib.parse.urlsplit(raw)
        host = (sp.hostname or "").strip()
        port_opt = sp.port
    except ValueError as exc:
        raise ProxyConfigError(f"Некорректный URI прокси: {exc}") from exc

    if not host or any(c.isspace() for c in host):
        raise ProxyConfigError("Не указан корректный хост прокси-сервера")
    if port_opt is None or not (1 <= int(port_opt) <= 65535):
        raise ProxyConfigError("Укажите явный порт прокси-сервера (1..65535)")
    port = int(port_opt)

    username = urllib.parse.unquote(sp.username) if sp.username is not None else ""
    password = urllib.parse.unquote(sp.password) if sp.password is not None else ""
    remark = sp.fragment or ""
    qs = urllib.parse.parse_qs(sp.query, keep_blank_values=True)

    if scheme_prefix in ("http", "https", "socks5", "socks5h"):
        return _finalize_parsed_spec(
            ParsedProxySpec(
                protocol=scheme_prefix,
                host=host,
                port=port,
                username=username,
                password=password,
                remark=remark,
            )
        )

    if scheme_prefix == "vless":
        if not username:
            raise ProxyConfigError("В ссылке vless:// отсутствует UUID пользователя")
        encryption = _first_qs(qs, "encryption", default="none") or "none"
        flow = _first_qs(qs, "flow", default="")
        sec_raw = _first_qs(qs, "security", default="none")
        net_raw = _first_qs(qs, "type", default="tcp")
        stream, xray_net, eff_sec = _build_xray_stream_settings(
            host=host,
            transport_raw=net_raw,
            security_raw=sec_raw,
            sni=_first_qs(qs, "sni", "serverName"),
            fp=_first_qs(qs, "fp", "fingerprint"),
            alpn_raw=_first_qs(qs, "alpn"),
            pbk=_first_qs(qs, "pbk", "publicKey"),
            sid=_first_qs(qs, "sid", "shortId"),
            spx=_first_qs(qs, "spx", "spiderX"),
            path=_first_qs(qs, "path"),
            ws_host=_first_qs(qs, "host"),
            service_name=_first_qs(qs, "serviceName"),
            mode=_first_qs(qs, "mode"),
            header_type=_first_qs(qs, "headerType"),
            allow_insecure=_first_qs(qs, "allowInsecure", "insecure").lower() in ("1", "true"),
        )
        user_entry: dict[str, Any] = {
            "id": username,
            "encryption": encryption,
        }
        if flow:
            user_entry["flow"] = flow
        outbound = {
            "tag": "proxy",
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": host,
                        "port": port,
                        "users": [user_entry],
                    }
                ]
            },
            "streamSettings": stream,
        }
        return _finalize_parsed_spec(
            ParsedProxySpec(
                protocol="vless",
                host=host,
                port=port,
                username=username,
                remark=remark,
                security=eff_sec,
                transport=xray_net,
                xray_outbound=outbound,
            )
        )

    # trojan://password@host:port?...#remark
    trojan_pw = f"{username}:{password}" if (username and sp.password is not None) else (username or password)
    if not trojan_pw:
        raise ProxyConfigError("В ссылке trojan:// отсутствует пароль")
    sec_raw = _first_qs(qs, "security", default="tls") or "tls"
    net_raw = _first_qs(qs, "type", default="tcp")
    stream, xray_net, eff_sec = _build_xray_stream_settings(
        host=host,
        transport_raw=net_raw,
        security_raw=sec_raw,
        sni=_first_qs(qs, "sni", "serverName"),
        fp=_first_qs(qs, "fp", "fingerprint"),
        alpn_raw=_first_qs(qs, "alpn"),
        pbk=_first_qs(qs, "pbk", "publicKey"),
        sid=_first_qs(qs, "sid", "shortId"),
        spx=_first_qs(qs, "spx", "spiderX"),
        path=_first_qs(qs, "path"),
        ws_host=_first_qs(qs, "host"),
        service_name=_first_qs(qs, "serviceName"),
        mode=_first_qs(qs, "mode"),
        header_type=_first_qs(qs, "headerType"),
        allow_insecure=_first_qs(qs, "allowInsecure", "insecure").lower() in ("1", "true"),
    )
    outbound = {
        "tag": "proxy",
        "protocol": "trojan",
        "settings": {
            "servers": [
                {
                    "address": host,
                    "port": port,
                    "password": trojan_pw,
                }
            ]
        },
        "streamSettings": stream,
    }
    return _finalize_parsed_spec(
        ParsedProxySpec(
            protocol="trojan",
            host=host,
            port=port,
            password=trojan_pw,
            remark=remark,
            security=eff_sec,
            transport=xray_net,
            xray_outbound=outbound,
        )
    )


# ---------------------------------------------------------------------------
# 2. SSRF & Non-Admin Network Target Validation
# ---------------------------------------------------------------------------


def _is_forbidden_non_admin_ip(raw_ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address = raw_ip
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        ip_obj = ip_obj.ipv4_mapped
    if (
        ip_obj.is_loopback
        or ip_obj.is_unspecified
        or ip_obj.is_private
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
    ):
        return True
    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in _CGNAT_NET:
        return True
    if not ip_obj.is_global:
        return True
    return False


def _try_parse_ip_literal(host: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    clean = host.strip("[]")
    if "%" in clean:
        clean = clean.split("%", 1)[0]
    try:
        return ipaddress.ip_address(clean)
    except ValueError:
        pass
    try:
        packed = socket.inet_aton(clean)
        return ipaddress.IPv4Address(packed)
    except OSError:
        return None


def validate_proxy_target_security(
    host: str,
    port: int,
    *,
    is_admin: bool,
    resolve_dns: bool = True,
) -> None:
    """Enforce port bounds and block loopback / LAN / Tailscale / link-local SSRF targets for non-admins."""
    clean_host = (host or "").strip()
    if not clean_host or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in clean_host):
        raise ProxyConfigError("Некорректное имя хоста прокси-сервера")
    if "/" in clean_host or "@" in clean_host or "\\" in clean_host:
        raise ProxyConfigError("Недопустимые символы в имени хоста прокси-сервера")
    try:
        p_num = int(port)
    except (TypeError, ValueError) as exc:
        raise ProxyConfigError("Некорректный порт прокси-сервера") from exc
    if not (1 <= p_num <= 65535):
        raise ProxyConfigError("Порт прокси-сервера должен быть в диапазоне 1..65535")

    if is_admin:
        return

    low = clean_host.strip("[]").rstrip(".").lower()
    if (
        low in ("localhost", "host.docker.internal", "gateway.docker.internal", "metadata.google.internal")
        or low.endswith(".localhost")
        or low.endswith(".local")
        or low.endswith(".internal")
        or low.endswith(".lan")
        or low.endswith(".home.arpa")
    ):
        raise ProxyConfigError(
            "Обычным пользователям запрещено указывать локальные или внутренние адреса прокси-сервера"
        )

    ip_lit = _try_parse_ip_literal(clean_host)
    if ip_lit is not None:
        if _is_forbidden_non_admin_ip(ip_lit):
            raise ProxyConfigError(
                "Обычным пользователям разрешены только внешние публичные прокси-серверы (локальные и частные подсети заблокированы)"
            )
        return

    if resolve_dns:
        try:
            infos = socket.getaddrinfo(clean_host, p_num, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ProxyConfigError(
                f"Не удалось разрешить домен прокси-сервера «{clean_host}»"
            ) from exc
        if not infos:
            raise ProxyConfigError(f"Домен «{clean_host}» не имеет публичных IP-адресов")
        for _fam, _socktype, _proto, _canon, sockaddr in infos:
            addr_str = str(sockaddr[0]).split("%", 1)[0]
            try:
                resolved_ip = ipaddress.ip_address(addr_str)
            except ValueError as exc:
                raise ProxyConfigError("Некорректный IP-адрес при DNS-резолве прокси") from exc
            if _is_forbidden_non_admin_ip(resolved_ip):
                raise ProxyConfigError(
                    "Домен прокси-сервера указывает на локальную или внутреннюю подсеть (заблокировано защитой SSRF)"
                )


# ---------------------------------------------------------------------------
# 3. Clean Subprocess Env & Stdlib Urllib Proxy Helpers (Honest `direct`)
# ---------------------------------------------------------------------------


def apply_proxy_to_subprocess_env(
    env: dict[str, str],
    proxy_url: Optional[str],
    *,
    include_npm: bool = False,
) -> dict[str, str]:
    """Strip all inherited proxy env vars; set explicit local/remote HTTP proxy or `NO_PROXY="*"` for honest `direct`."""
    bypass = []
    for key, value in env.items():
        if key.lower() == "no_proxy":
            bypass.extend(x.strip() for x in value.split(",") if x.strip() and x.strip() != "*")
    for key in list(env.keys()):
        if key in _PROXY_ENV_KEYS or key.lower() in {k.lower() for k in _PROXY_ENV_KEYS}:
            env.pop(key, None)
        elif key.lower() == "no_proxy":
            env.pop(key, None)

    clean_proxy = (proxy_url or "").strip()
    if clean_proxy:
        env["HTTP_PROXY"] = clean_proxy
        env["HTTPS_PROXY"] = clean_proxy
        env["http_proxy"] = clean_proxy
        env["https_proxy"] = clean_proxy
        if include_npm:
            env["npm_config_proxy"] = clean_proxy
            env["npm_config_https_proxy"] = clean_proxy
        env["NO_PROXY"] = ",".join(dict.fromkeys(bypass + _LOCAL_NO_PROXY_HOSTS.split(",")))
        env["no_proxy"] = env["NO_PROXY"]
    else:
        env["NO_PROXY"] = "*"
        env["no_proxy"] = "*"
    return env


class _DirectProxyHandler(urllib.request.ProxyHandler):
    """Explicit no-proxy handler (`proxies == {}`) retained in `opener.handlers`
    that suppresses Windows `winreg` system proxy auto-detection."""

    def __init__(self) -> None:
        super().__init__({})

    def default_open(self, req: urllib.request.Request) -> None:
        return None


def build_urllib_proxy_opener(proxy_url: Optional[str]) -> urllib.request.OpenerDirector:
    """Build a stdlib OpenerDirector that ALWAYS overrides Windows registry (`ProxyHandler({})` when `direct`)."""
    clean = (proxy_url or "").strip()
    if clean:
        handler: urllib.request.ProxyHandler = urllib.request.ProxyHandler({"http": clean, "https": clean})
    else:
        handler = _DirectProxyHandler()
    return urllib.request.build_opener(handler)


# ---------------------------------------------------------------------------
# 4. Local Asyncio HTTP-CONNECT -> SOCKS5 / HTTP-Basic-Auth Bridge
# ---------------------------------------------------------------------------


async def _pipe_streams(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(32768)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            if not writer.is_closing():
                writer.close()
        except Exception:
            pass


async def _open_socks5_tunnel(
    spec: ParsedProxySpec,
    target_host: str,
    target_port: int,
    *,
    enforce_non_admin_ssrf: bool = False,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if enforce_non_admin_ssrf:
        await asyncio.to_thread(
            validate_proxy_target_security, spec.host, spec.port, is_admin=False, resolve_dns=True
        )

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(spec.host, spec.port),
        timeout=10.0,
    )
    try:
        has_auth = bool(spec.username or spec.password)
        if has_auth:
            writer.write(b"\x05\x02\x00\x02")
        else:
            writer.write(b"\x05\x01\x00")
        await writer.drain()

        ver_method = await asyncio.wait_for(reader.readexactly(2), timeout=8.0)
        if ver_method[0] != 0x05:
            raise ProxyRouteError("Удалённый узел не ответил по протоколу SOCKS5")
        chosen = ver_method[1]

        if chosen == 0x02:
            u_b = (spec.username or "").encode("utf-8")
            p_b = (spec.password or "").encode("utf-8")
            if len(u_b) > 255 or len(p_b) > 255:
                raise ProxyRouteError("Слишком длинный логин или пароль SOCKS5")
            writer.write(b"\x01" + bytes([len(u_b)]) + u_b + bytes([len(p_b)]) + p_b)
            await writer.drain()
            auth_resp = await asyncio.wait_for(reader.readexactly(2), timeout=8.0)
            if auth_resp[1] != 0x00:
                raise ProxyRouteError("Ошибка авторизации на SOCKS5-прокси (неверный логин или пароль)")
        elif chosen != 0x00:
            raise ProxyRouteError("SOCKS5-прокси отклонил предложенные методы авторизации")

        clean_target = target_host.strip("[]")
        addr_payload: bytes
        try:
            ip_obj = ipaddress.ip_address(clean_target)
            if isinstance(ip_obj, ipaddress.IPv4Address):
                addr_payload = b"\x01" + ip_obj.packed
            else:
                addr_payload = b"\x04" + ip_obj.packed
        except ValueError:
            # ATYP=0x03: remote DNS resolution on the SOCKS5 server (zero local DNS leak)
            dom_b = clean_target.encode("idna")
            if len(dom_b) > 255:
                raise ProxyRouteError("Слишком длинное доменное имя назначения")
            addr_payload = b"\x03" + bytes([len(dom_b)]) + dom_b

        writer.write(b"\x05\x01\x00" + addr_payload + struct.pack("!H", int(target_port)))
        await writer.drain()

        hdr = await asyncio.wait_for(reader.readexactly(4), timeout=10.0)
        if hdr[0] != 0x05 or hdr[1] != 0x00:
            raise ProxyRouteError(f"SOCKS5-прокси отклонил подключение к цели (код {hdr[1]})")
        atyp = hdr[3]
        if atyp == 0x01:
            await asyncio.wait_for(reader.readexactly(4 + 2), timeout=6.0)
        elif atyp == 0x04:
            await asyncio.wait_for(reader.readexactly(16 + 2), timeout=6.0)
        elif atyp == 0x03:
            dlen = (await asyncio.wait_for(reader.readexactly(1), timeout=6.0))[0]
            await asyncio.wait_for(reader.readexactly(dlen + 2), timeout=6.0)
        else:
            raise ProxyRouteError(f"Неизвестный тип адреса SOCKS5 (ATYP={atyp})")

        return reader, writer
    except Exception:
        try:
            writer.close()
        except Exception:
            pass
        raise


async def _open_upstream_http_proxy_conn(
    spec: ParsedProxySpec,
    *,
    enforce_non_admin_ssrf: bool = False,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if enforce_non_admin_ssrf:
        await asyncio.to_thread(
            validate_proxy_target_security, spec.host, spec.port, is_admin=False, resolve_dns=True
        )
    ssl_ctx = ssl.create_default_context() if spec.protocol == "https" else None
    return await asyncio.wait_for(
        asyncio.open_connection(
            spec.host,
            spec.port,
            ssl=ssl_ctx,
            server_hostname=spec.host if ssl_ctx else None,
        ),
        timeout=10.0,
    )


def _basic_proxy_auth_header(spec: ParsedProxySpec) -> str:
    raw = f"{spec.username}:{spec.password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


async def _handle_local_bridge_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    spec: ParsedProxySpec,
    enforce_non_admin_ssrf: bool,
) -> None:
    up_writer: Optional[asyncio.StreamWriter] = None
    try:
        raw_head = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), timeout=10.0)
        if len(raw_head) > 32768:
            return
        head_txt = raw_head.decode("iso-8859-1", errors="replace")
        lines = head_txt.split("\r\n")
        if not lines or not lines[0]:
            return
        parts = lines[0].split(" ")
        if len(parts) < 3:
            return
        method, target, http_ver = parts[0].upper(), parts[1], parts[2]

        if method == "CONNECT":
            host_port = target
            if host_port.startswith("["):
                rb = host_port.find("]")
                t_host = host_port[1:rb]
                t_port = int(host_port[rb + 2 :]) if host_port[rb + 1 : rb + 2] == ":" else 443
            elif ":" in host_port:
                h_part, p_part = host_port.rsplit(":", 1)
                t_host, t_port = h_part, int(p_part)
            else:
                t_host, t_port = host_port, 443

            if spec.protocol in ("socks5", "socks5h"):
                up_reader, up_writer = await _open_socks5_tunnel(
                    spec, t_host, t_port, enforce_non_admin_ssrf=enforce_non_admin_ssrf
                )
            else:
                up_reader, up_writer = await _open_upstream_http_proxy_conn(
                    spec, enforce_non_admin_ssrf=enforce_non_admin_ssrf
                )
                auth_hdr = _basic_proxy_auth_header(spec)
                connect_req = (
                    f"CONNECT {t_host}:{t_port} HTTP/1.1\r\n"
                    f"Host: {t_host}:{t_port}\r\n"
                    f"Proxy-Authorization: {auth_hdr}\r\n"
                    f"Proxy-Connection: keep-alive\r\n\r\n"
                ).encode("iso-8859-1")
                up_writer.write(connect_req)
                await up_writer.drain()
                up_resp = await asyncio.wait_for(up_reader.readuntil(b"\r\n\r\n"), timeout=10.0)
                first_up = up_resp.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
                if " 200" not in first_up:
                    client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                    await client_writer.drain()
                    return

            client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await client_writer.drain()
            await asyncio.gather(
                _pipe_streams(client_reader, up_writer),
                _pipe_streams(up_reader, client_writer),
            )
            return

        # Plain HTTP request through local HTTP proxy (`GET http://example.com/path HTTP/1.1`)
        if spec.protocol in ("socks5", "socks5h"):
            sp = urllib.parse.urlsplit(target)
            t_host = sp.hostname or ""
            t_port = sp.port or (443 if sp.scheme == "https" else 80)
            if not t_host:
                for hdr_line in lines[1:]:
                    if hdr_line.lower().startswith("host:"):
                        hv = hdr_line.split(":", 1)[1].strip()
                        if ":" in hv and not hv.endswith("]"):
                            t_host, p_s = hv.rsplit(":", 1)
                            t_port = int(p_s)
                        else:
                            t_host = hv
                        break
            if not t_host:
                return
            rel_path = sp.path or "/"
            if sp.query:
                rel_path = f"{rel_path}?{sp.query}"

            up_reader, up_writer = await _open_socks5_tunnel(
                spec, t_host, t_port, enforce_non_admin_ssrf=enforce_non_admin_ssrf
            )
            rebuilt_lines = [f"{method} {rel_path} {http_ver}"]
            for hdr_line in lines[1:]:
                low_h = hdr_line.lower()
                if low_h.startswith("proxy-authorization:") or low_h.startswith("proxy-connection:"):
                    continue
                rebuilt_lines.append(hdr_line)
            up_writer.write("\r\n".join(rebuilt_lines).encode("iso-8859-1"))
            await up_writer.drain()
            await asyncio.gather(
                _pipe_streams(client_reader, up_writer),
                _pipe_streams(up_reader, client_writer),
            )
        else:
            up_reader, up_writer = await _open_upstream_http_proxy_conn(
                spec, enforce_non_admin_ssrf=enforce_non_admin_ssrf
            )
            auth_hdr = _basic_proxy_auth_header(spec)
            rebuilt_lines = [lines[0], f"Proxy-Authorization: {auth_hdr}"]
            for hdr_line in lines[1:]:
                if hdr_line.lower().startswith("proxy-authorization:"):
                    continue
                rebuilt_lines.append(hdr_line)
            up_writer.write("\r\n".join(rebuilt_lines).encode("iso-8859-1"))
            await up_writer.drain()
            await asyncio.gather(
                _pipe_streams(client_reader, up_writer),
                _pipe_streams(up_reader, client_writer),
            )
    except Exception:
        try:
            if not client_writer.is_closing():
                client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                await client_writer.drain()
        except Exception:
            pass
    finally:
        if up_writer is not None:
            try:
                up_writer.close()
            except Exception:
                pass
        try:
            client_writer.close()
        except Exception:
            pass


class _BridgeLoopRunner:
    """Dedicated background event-loop thread so local SOCKS5/HTTP bridges work across any thread or test loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

    def ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._loop.is_running() and self._thread and self._thread.is_alive():
                return self._loop
            ready = threading.Event()
            holder: list[asyncio.AbstractEventLoop] = []

            def _run() -> None:
                if os.name == "nt":
                    loop = asyncio.SelectorEventLoop()
                else:
                    loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                holder.append(loop)
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    try:
                        pending = asyncio.all_tasks(loop)
                        for t in pending:
                            t.cancel()
                        if pending:
                            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    except Exception:
                        pass
                    loop.close()

            t = threading.Thread(target=_run, name="agy-proxy-bridge-loop", daemon=True)
            t.start()
            ready.wait(timeout=5.0)
            self._loop = holder[0]
            self._thread = t
            return self._loop

    def start_bridge(self, spec: ParsedProxySpec, *, enforce_non_admin_ssrf: bool) -> tuple[asyncio.AbstractServer, int]:
        loop = self.ensure_loop()

        async def _create() -> tuple[asyncio.AbstractServer, int]:
            srv = await asyncio.start_server(
                lambda r, w: _handle_local_bridge_client(r, w, spec, enforce_non_admin_ssrf),
                host="127.0.0.1",
                port=0,
            )
            sock = srv.sockets[0]
            port = int(sock.getsockname()[1])
            return srv, port

        fut = asyncio.run_coroutine_threadsafe(_create(), loop)
        return fut.result(timeout=5.0)

    def stop_bridge(self, srv: asyncio.AbstractServer) -> None:
        with self._lock:
            loop = self._loop
        if loop is None or not loop.is_running():
            return

        async def _close() -> None:
            srv.close()
            try:
                await asyncio.wait_for(srv.wait_closed(), timeout=2.0)
            except Exception:
                pass

        try:
            fut = asyncio.run_coroutine_threadsafe(_close(), loop)
            fut.result(timeout=3.0)
        except Exception:
            pass

    def shutdown(self) -> None:
        with self._lock:
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)


_BRIDGE_RUNNER = _BridgeLoopRunner()


# ---------------------------------------------------------------------------
# 5. Xray-core Discovery, 1-Click Installer, and Win32 Job Object Lifecycle
# ---------------------------------------------------------------------------

_XRAY_JOB_HANDLE: Optional[int] = None
_XRAY_INSTALL_LOCK = asyncio.Lock()
_XRAY_VER_CACHE: dict[str, tuple[int, str]] = {}


def _ensure_xray_win32_job() -> Optional[int]:
    """Create a Win32 Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) so child xray.exe never leaks if Hub exits."""
    global _XRAY_JOB_HANDLE
    if os.name != "nt":
        return None
    if _XRAY_JOB_HANDLE is not None:
        return _XRAY_JOB_HANDLE
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            kernel32.CloseHandle(job)
            return None
        _XRAY_JOB_HANDLE = int(job)
        return _XRAY_JOB_HANDLE
    except Exception:
        return None


def _attach_pid_to_xray_job(pid: int) -> None:
    if os.name != "nt":
        return
    job = _ensure_xray_win32_job()
    if not job:
        return
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h_proc = kernel32.OpenProcess(0x0100 | 0x0001, False, int(pid))
        if h_proc:
            try:
                kernel32.AssignProcessToJobObject(job, h_proc)
            finally:
                kernel32.CloseHandle(h_proc)
    except Exception:
        pass


def _extract_v2rayn_dir_from_run_value(raw_cmd: str) -> Optional[Path]:
    s = (raw_cmd or "").strip()
    if not s:
        return None
    if s.startswith('"'):
        end_q = s.find('"', 1)
        exe_str = s[1:end_q] if end_q > 1 else s.strip('"')
    else:
        m = re.match(r"^(.+?\.exe)\b", s, flags=re.IGNORECASE)
        exe_str = m.group(1) if m else s.split(" ", 1)[0]
    try:
        p = Path(exe_str)
        if p.exists():
            return p.resolve().parent
    except OSError:
        pass
    return None


def _find_xray_near_v2rayn() -> Optional[Path]:
    if os.name != "nt":
        return None
    candidate_roots: list[Path] = []
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
        ) as key:
            for val_name in ("v2rayN", "v2rayn"):
                try:
                    raw_val, _ = winreg.QueryValueEx(key, val_name)
                    v_dir = _extract_v2rayn_dir_from_run_value(str(raw_val or ""))
                    if v_dir:
                        candidate_roots.append(v_dir)
                except FileNotFoundError:
                    continue
    except Exception:
        pass

    base_resolved = config.BASE_DIR.resolve()
    for extra in (
        base_resolved.parent / "v2rayN",
        base_resolved.parent.parent / "v2rayN",
        Path(r"D:\Internet\v2rayN"),
        Path(r"C:\v2rayN"),
        Path(r"D:\v2rayN"),
    ):
        candidate_roots.append(extra)

    for env_key in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        ev = os.environ.get(env_key, "").strip()
        if ev:
            candidate_roots.append(Path(ev) / "v2rayN")

    seen: set[str] = set()
    for root in candidate_roots:
        try:
            key_s = str(root).lower()
            if key_s in seen or not root.exists():
                continue
            seen.add(key_s)
            for rel in (
                Path("bin") / "xray" / "xray.exe",
                Path("bin") / "xray.exe",
                Path("xray.exe"),
            ):
                cand = root / rel
                if cand.is_file():
                    return cand.resolve()
        except OSError:
            continue
    return None


def resolve_xray_bin_with_source() -> tuple[Optional[str], str]:
    """Locate `xray` binary (`env` -> `portable` -> `path` -> `v2rayN`)."""
    env_bin = os.getenv("HUB_XRAY_BIN", "").strip()
    if env_bin:
        try:
            p = Path(env_bin)
            if p.is_file():
                return str(p.resolve()), "env"
        except OSError:
            pass

    exe_name = "xray.exe" if config.IS_WINDOWS else "xray"
    portable_bin = config.XRAY_TOOLS_DIR / exe_name
    try:
        if portable_bin.is_file():
            return str(portable_bin.resolve()), "portable"
    except OSError:
        pass

    which_bin = shutil.which("xray")
    if which_bin:
        try:
            p = Path(which_bin)
            if p.is_file():
                return str(p.resolve()), "path"
        except OSError:
            pass

    v2_bin = _find_xray_near_v2rayn()
    if v2_bin is not None:
        return str(v2_bin), "v2rayN"

    return None, ""


def resolve_xray_bin() -> Optional[str]:
    bin_path, _src = resolve_xray_bin_with_source()
    return bin_path


def _build_xray_spawn_argv(xray_bin: str, *args: str) -> list[str]:
    if str(xray_bin).lower().endswith(".py"):
        return [sys.executable, str(xray_bin), *args]
    return [str(xray_bin), *args]


def _probe_xray_version_sync(xray_bin: str) -> str:
    try:
        st = Path(xray_bin).stat()
        mtime_ns = int(st.st_mtime_ns)
    except OSError:
        return ""
    cached = _XRAY_VER_CACHE.get(xray_bin)
    if cached and cached[0] == mtime_ns:
        return cached[1]

    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["ORT_DISABLE_ALL_GPU"] = "1"
    ver = ""
    try:
        proc = subprocess.run(
            _build_xray_spawn_argv(xray_bin, "version"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=4.0,
            **config.subprocess_hidden_kwargs(),
        )
        out = ((proc.stdout or b"") + b"\n" + (proc.stderr or b"")).decode("utf-8", errors="replace").strip()
        if out:
            first_line = out.splitlines()[0].strip()
            m = re.search(r"Xray\s+v?([0-9]+\.[0-9]+\.[0-9]+)", first_line, flags=re.IGNORECASE)
            ver = m.group(1) if m else first_line[:48]
    except Exception:
        ver = ""
    if ver:
        _XRAY_VER_CACHE[xray_bin] = (mtime_ns, ver)
    return ver


def get_xray_info(*, probe_version: bool = True) -> dict[str, Any]:
    bin_path, source = resolve_xray_bin_with_source()
    if not bin_path:
        return {
            "available": False,
            "version": "",
            "source": "",
            "bin_path": "",
        }
    ver = _probe_xray_version_sync(bin_path) if probe_version else ""
    return {
        "available": True,
        "version": ver,
        "source": source,
        "bin_path": bin_path,
    }


def _select_xray_release_asset_name() -> str:
    mach = platform.machine().lower()
    is_arm = any(a in mach for a in ("arm64", "aarch64"))
    if config.IS_WINDOWS:
        return "Xray-windows-arm64-v8a.zip" if is_arm else "Xray-windows-64.zip"
    return "Xray-linux-arm64-v8a.zip" if is_arm else "Xray-linux-64.zip"


def _download_bytes_with_fallback(url: str, *, timeout: float = 45.0) -> bytes:
    proxy_candidates: list[Optional[str]] = []
    cur_global = resolve_global_proxy_sync()
    sys_det = config.detect_system_proxy()
    for p in (cur_global, sys_det, None):
        if p not in proxy_candidates:
            proxy_candidates.append(p)

    last_err: Optional[Exception] = None
    for p_url in proxy_candidates:
        try:
            opener = build_urllib_proxy_opener(p_url)
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "AntigravityHub/1.5",
                    "Accept": "application/vnd.github+json, application/octet-stream, */*",
                },
            )
            with opener.open(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:
            last_err = exc
            continue
    raise RuntimeError(f"Не удалось скачать релиз Xray-core с GitHub: {last_err}")


def _install_xray_sync() -> dict[str, Any]:
    meta_raw = _download_bytes_with_fallback(
        "https://api.github.com/repos/XTLS/Xray-core/releases/latest",
        timeout=20.0,
    )
    meta = json.loads(meta_raw.decode("utf-8", errors="replace"))
    if not isinstance(meta, dict):
        raise RuntimeError("Некорректный ответ GitHub API для XTLS/Xray-core")

    wanted_asset = _select_xray_release_asset_name()
    dl_url = ""
    for asset in meta.get("assets") or []:
        if isinstance(asset, dict) and str(asset.get("name") or "") == wanted_asset:
            dl_url = str(asset.get("browser_download_url") or "")
            break
    if not dl_url:
        raise RuntimeError(f"В последнем релизе XTLS/Xray-core не найден пакет {wanted_asset}")

    zip_bytes = _download_bytes_with_fallback(dl_url, timeout=120.0)
    target_dir = config.XRAY_TOOLS_DIR.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    exe_name = "xray.exe" if config.IS_WINDOWS else "xray"
    allowed_names = {exe_name.lower(), "geoip.dat", "geosite.dat"}
    extracted_exe = False

    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            leaf = Path(member.filename).name
            if leaf.lower() not in allowed_names:
                continue
            dest_name = exe_name if leaf.lower() == exe_name.lower() else leaf.lower()
            tmp_file = target_dir / f".tmp_{secrets.token_hex(4)}_{dest_name}"
            final_file = target_dir / dest_name
            data = zf.read(member)
            tmp_file.write_bytes(data)
            if not config.IS_WINDOWS and dest_name == exe_name:
                tmp_file.chmod(0o755)
            os.replace(tmp_file, final_file)
            if dest_name == exe_name:
                extracted_exe = True

    if not extracted_exe:
        raise RuntimeError(f"В архиве {wanted_asset} не найден исполняемый файл {exe_name}")

    _XRAY_VER_CACHE.clear()
    info = get_xray_info(probe_version=True)
    if not info.get("available"):
        raise RuntimeError("Xray-core распакован, но не прошёл проверку запуска")
    return info


async def install_or_update_xray_core() -> dict[str, Any]:
    """Download and verify official portable `XTLS/Xray-core` into `data/tools/xray/`."""
    async with _XRAY_INSTALL_LOCK:
        return await asyncio.to_thread(_install_xray_sync)


# ---------------------------------------------------------------------------
# 6. Active Tunnels & Routing Table Manager (Thread-Safe + Fail-Closed)
# ---------------------------------------------------------------------------


@dataclass
class _ManagedTunnel:
    config_hash: str
    kind: str  # "xray" | "bridge"
    local_port: int
    proxy_url: str
    label: str
    owners: set[str] = field(default_factory=set)
    proc: Optional[subprocess.Popen[bytes]] = None
    bridge_server: Optional[asyncio.AbstractServer] = None

    def is_alive(self) -> bool:
        if self.kind == "xray":
            return self.proc is not None and self.proc.poll() is None
        return self.bridge_server is not None and self.bridge_server.is_serving()


@dataclass
class _RouteSpecEntry:
    owner_key: str
    mode: str  # "system_auto" | "direct" | "global" | "custom"
    raw_spec: str = ""
    safe_label: str = ""
    corrupted: bool = False
    is_admin: bool = True


_ROUTER_LOCK = threading.RLock()
_TUNNEL_OP_LOCK = threading.Lock()
_INITIALIZED: bool = False
_GLOBAL_ENTRY = _RouteSpecEntry(owner_key="global", mode="system_auto")
_ACTIVE_AGY_PROFILE_ID: str = ""
_ROUTE_ENTRIES: dict[str, _RouteSpecEntry] = {}
_MANAGED_TUNNELS: dict[str, _ManagedTunnel] = {}
_OWNER_TO_HASH: dict[str, str] = {}


def is_initialized() -> bool:
    return _INITIALIZED


def _allocate_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _is_local_port_open(port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _stop_tunnel_instance(tun: _ManagedTunnel) -> None:
    if tun.kind == "xray" and tun.proc is not None:
        proc = tun.proc
        tun.proc = None
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
    elif tun.kind == "bridge" and tun.bridge_server is not None:
        srv = tun.bridge_server
        tun.bridge_server = None
        _BRIDGE_RUNNER.stop_bridge(srv)


def _detach_owner_locked(owner_key: str) -> Optional[_ManagedTunnel]:
    old_hash = _OWNER_TO_HASH.pop(owner_key, None)
    if not old_hash:
        return None
    tun = _MANAGED_TUNNELS.get(old_hash)
    if not tun:
        return None
    tun.owners.discard(owner_key)
    if not tun.owners:
        _MANAGED_TUNNELS.pop(old_hash, None)
        return tun
    return None


def release_owner(owner_key: str) -> None:
    to_stop: Optional[_ManagedTunnel] = None
    with _ROUTER_LOCK:
        _ROUTE_ENTRIES.pop(owner_key, None)
        to_stop = _detach_owner_locked(owner_key)
    if to_stop is not None:
        _stop_tunnel_instance(to_stop)


def build_xray_config_json(parsed: ParsedProxySpec, local_port: int) -> dict[str, Any]:
    """Build canonical Xray-core JSON configuration with `access: none` and loopback HTTP inbound."""
    if not parsed.xray_outbound:
        raise ProxyConfigError("Данная спецификация не является профилем Xray-core")
    return {
        "log": {"loglevel": "warning", "access": "none"},
        "inbounds": [
            {
                "tag": "hub-http-in",
                "listen": "127.0.0.1",
                "port": int(local_port),
                "protocol": "http",
                "settings": {"timeout": 300, "allowTransparent": False},
            }
        ],
        "outbounds": [
            parsed.xray_outbound,
            {"tag": "direct", "protocol": "freedom", "settings": {}},
        ],
    }


def _spawn_xray_tunnel_sync(parsed: ParsedProxySpec) -> _ManagedTunnel:
    xray_bin = resolve_xray_bin()
    if not xray_bin:
        raise ProxyRouteError(
            "Для запуска профиля VLESS / VMess / Trojan / Shadowsocks требуется Xray-core. "
            "Откройте Настройки → Сеть и нажмите «Установить Xray-core»."
        )
    assert parsed.xray_outbound is not None
    local_port = _allocate_free_loopback_port()
    xray_cfg = build_xray_config_json(parsed, local_port)
    cfg_bytes = json.dumps(xray_cfg, ensure_ascii=False).encode("utf-8")

    env = os.environ.copy()
    apply_proxy_to_subprocess_env(env, None)
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["ORT_DISABLE_ALL_GPU"] = "1"
    bin_dir = Path(xray_bin).resolve().parent
    if (bin_dir / "geoip.dat").is_file() or (bin_dir / "geosite.dat").is_file():
        env["XRAY_LOCATION_ASSET"] = str(bin_dir)

    try:
        proc = subprocess.Popen(
            _build_xray_spawn_argv(xray_bin, "run", "-c", "stdin:"),
            cwd=str(bin_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            **config.subprocess_hidden_kwargs(),
        )
    except Exception as exc:
        raise ProxyRouteError(f"Не удалось запустить процесс Xray-core: {exc}") from exc

    # v110: дренаж stdout/stderr до завершения xray.exe — иначе после ~64 КБ логов
    # процесс блокируется на WriteFile в переполненный пайп и замораживает трафик.
    out_tail, err_tail, drain_threads = _start_pipe_drainers(proc)

    _attach_pid_to_xray_job(proc.pid)

    try:
        assert proc.stdin is not None
        proc.stdin.write(cfg_bytes)
        proc.stdin.flush()
        proc.stdin.close()
    except Exception as exc:
        try:
            proc.kill()
        except Exception:
            pass
        raise ProxyRouteError(f"Не удалось передать конфигурацию в stdin Xray-core: {exc}") from exc

    deadline = time.monotonic() + 3.5
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        if _is_local_port_open(local_port, timeout=0.08):
            ready = True
            break
        time.sleep(0.04)

    if not ready:
        err_detail = ""
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    proc.kill()
                    proc.wait(timeout=1.0)
            for th in drain_threads:
                th.join(timeout=1.0)
            err_b = b"".join(list(err_tail))
            out_b = b"".join(list(out_tail))
            raw_msg = ((err_b or b"") + b"\n" + (out_b or b"")).decode("utf-8", errors="replace").strip()
            raw_msg = _UUID_LIKE_RE.sub("***", raw_msg)
            err_detail = raw_msg[-260:] if raw_msg else ""
        except Exception:
            err_detail = ""
        suffix = f": {err_detail}" if err_detail else " (таймаут открытия локального порта)"
        raise ProxyRouteError(f"Туннель Xray ({parsed.safe_label}) не смог запуститься{suffix}")

    return _ManagedTunnel(
        config_hash=parsed.config_hash,
        kind="xray",
        local_port=local_port,
        proxy_url=f"http://127.0.0.1:{local_port}",
        label=parsed.safe_label,
        proc=proc,
    )


_PIPE_DRAIN_TAIL_LINES = 40


def _drain_pipe_forever(stream: Any, tail: "collections.deque[bytes]") -> None:
    try:
        for chunk in iter(stream.readline, b""):
            tail.append(chunk)
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _start_pipe_drainers(
    proc: subprocess.Popen,
) -> tuple["collections.deque[bytes]", "collections.deque[bytes]", list[threading.Thread]]:
    """Daemon-потоки, молча вычитывающие stdout/stderr дочернего процесса до EOF
    (последние 40 строк каждого потока сохраняются для текста ошибки старта)."""
    out_tail: "collections.deque[bytes]" = collections.deque(maxlen=_PIPE_DRAIN_TAIL_LINES)
    err_tail: "collections.deque[bytes]" = collections.deque(maxlen=_PIPE_DRAIN_TAIL_LINES)
    threads: list[threading.Thread] = []
    for stream, tail, name in ((proc.stdout, out_tail, "out"), (proc.stderr, err_tail, "err")):
        if stream is None:
            continue
        th = threading.Thread(
            target=_drain_pipe_forever,
            args=(stream, tail),
            name=f"xray-drain-{name}-{proc.pid}",
            daemon=True,
        )
        th.start()
        threads.append(th)
    return out_tail, err_tail, threads


def _ensure_tunnel_for_spec_sync(
    owner_key: str,
    raw_spec: str,
    *,
    is_admin: bool,
    resolve_dns_ssrf: bool = False,
) -> str:
    try:
        parsed = parse_proxy_spec(raw_spec)
        validate_proxy_target_security(
            parsed.host,
            parsed.port,
            is_admin=is_admin,
            resolve_dns=(resolve_dns_ssrf and not is_admin),
        )
    except ProxyConfigError as exc:
        raise ProxyRouteError(f"Ошибка конфигурации маршрута прокси: {exc}") from exc

    # Plain unauthenticated HTTP/HTTPS proxy needs no local tunnel process
    if not parsed.needs_xray and not parsed.needs_local_bridge:
        to_stop = None
        with _ROUTER_LOCK:
            to_stop = _detach_owner_locked(owner_key)
        if to_stop is not None:
            _stop_tunnel_instance(to_stop)
        return parsed.direct_proxy_url

    with _TUNNEL_OP_LOCK:
        stale_dead: Optional[_ManagedTunnel] = None
        old_to_stop: Optional[_ManagedTunnel] = None
        with _ROUTER_LOCK:
            existing = _MANAGED_TUNNELS.get(parsed.config_hash)
            if existing is not None:
                if existing.is_alive() and _is_local_port_open(existing.local_port, timeout=0.15):
                    if _OWNER_TO_HASH.get(owner_key) != parsed.config_hash:
                        old_to_stop = _detach_owner_locked(owner_key)
                        existing.owners.add(owner_key)
                        _OWNER_TO_HASH[owner_key] = parsed.config_hash
                    if old_to_stop is not None:
                        _stop_tunnel_instance(old_to_stop)
                    return existing.proxy_url
                # Existing tunnel died — clean up and respawn
                _MANAGED_TUNNELS.pop(parsed.config_hash, None)
                stale_dead = existing

        if stale_dead is not None:
            _stop_tunnel_instance(stale_dead)

        if parsed.needs_xray:
            new_tun = _spawn_xray_tunnel_sync(parsed)
        else:
            srv, b_port = _BRIDGE_RUNNER.start_bridge(
                parsed, enforce_non_admin_ssrf=(not is_admin)
            )
            new_tun = _ManagedTunnel(
                config_hash=parsed.config_hash,
                kind="bridge",
                local_port=b_port,
                proxy_url=f"http://127.0.0.1:{b_port}",
                label=parsed.safe_label,
                bridge_server=srv,
            )

        with _ROUTER_LOCK:
            if _OWNER_TO_HASH.get(owner_key) != parsed.config_hash:
                old_to_stop = _detach_owner_locked(owner_key)
            if stale_dead is not None:
                new_tun.owners.update(stale_dead.owners)
            new_tun.owners.add(owner_key)
            _MANAGED_TUNNELS[parsed.config_hash] = new_tun
            for ow in new_tun.owners:
                _OWNER_TO_HASH[ow] = parsed.config_hash

        if old_to_stop is not None:
            _stop_tunnel_instance(old_to_stop)
        return new_tun.proxy_url


def _ensure_effective_entry_sync(entry: _RouteSpecEntry) -> Optional[str]:
    if entry.mode == "direct":
        to_stop = None
        with _ROUTER_LOCK:
            to_stop = _detach_owner_locked(entry.owner_key)
        if to_stop is not None:
            _stop_tunnel_instance(to_stop)
        return None

    if entry.mode == "system_auto":
        detected = config.detect_system_proxy()
        if not detected:
            to_stop = None
            with _ROUTER_LOCK:
                to_stop = _detach_owner_locked(entry.owner_key)
            if to_stop is not None:
                _stop_tunnel_instance(to_stop)
            return None
        try:
            return _ensure_tunnel_for_spec_sync(entry.owner_key, detected, is_admin=True)
        except Exception:
            # System auto proxy may have an unusual format; pass raw HTTP if safe
            return detected if detected.lower().startswith(("http://", "https://")) else None

    if entry.mode == "custom":
        if entry.corrupted or not entry.raw_spec.strip():
            raise ProxyRouteError(
                "Задан пользовательский маршрут прокси, но его параметры повреждены или пусты (защита Fail-Closed заблокировала прямой выход)."
            )
        return _ensure_tunnel_for_spec_sync(
            entry.owner_key,
            entry.raw_spec,
            is_admin=entry.is_admin,
            resolve_dns_ssrf=(not entry.is_admin),
        )

    return None


def _select_target_entry_locked(
    provider: str,
    user_id: str = "",
    account_id: str = "",
) -> _RouteSpecEntry:
    prov = (provider or "global").strip().lower()
    uid = (user_id or "").strip()
    aid = (account_id or "").strip()

    if prov == "agy":
        prof_id = aid or _ACTIVE_AGY_PROFILE_ID
        if prof_id:
            ent = _ROUTE_ENTRIES.get(f"agy:{prof_id}")
            if ent and ent.mode == "custom":
                return ent
        return _GLOBAL_ENTRY

    if prov in ("chatgpt", "claude") and uid and aid:
        ent = _ROUTE_ENTRIES.get(f"{prov}:{uid}:{aid}")
        if ent and ent.mode == "custom":
            return ent
        return _GLOBAL_ENTRY

    return _GLOBAL_ENTRY


def resolve_proxy_sync(
    provider: str = "global",
    *,
    user_id: str = "",
    account_id: str = "",
) -> Optional[str]:
    """Fast thread-safe read of an already-active proxy URL (for sync callers)."""
    with _ROUTER_LOCK:
        if not _INITIALIZED:
            return config._legacy_resolve_outbound_proxy()
        entry = _select_target_entry_locked(provider, user_id=user_id, account_id=account_id)
        if entry.mode == "direct":
            return None
        if entry.mode == "system_auto":
            h = _OWNER_TO_HASH.get(entry.owner_key)
            if h and h in _MANAGED_TUNNELS and _MANAGED_TUNNELS[h].is_alive():
                return _MANAGED_TUNNELS[h].proxy_url
            det = config.detect_system_proxy()
            if not det:
                return None
            if det.lower().startswith(("socks5://", "socks5h://", "socks://")):
                try:
                    return _ensure_tunnel_for_spec_sync(entry.owner_key, det, is_admin=True)
                except Exception:
                    return None
            return det
        if entry.mode == "custom":
            if entry.corrupted or not entry.raw_spec.strip():
                raise ProxyRouteError("Маршрут прокси повреждён или не настроен (Fail-Closed)")
            h = _OWNER_TO_HASH.get(entry.owner_key)
            if h and h in _MANAGED_TUNNELS and _MANAGED_TUNNELS[h].is_alive():
                return _MANAGED_TUNNELS[h].proxy_url
            parsed = parse_proxy_spec(entry.raw_spec)
            if not parsed.needs_xray and not parsed.needs_local_bridge:
                return parsed.direct_proxy_url
    return _ensure_effective_entry_sync(entry)


def resolve_global_proxy_sync() -> Optional[str]:
    try:
        return resolve_proxy_sync("global")
    except ProxyRouteError:
        raise


async def ensure_proxy_for_context(
    provider: str = "global",
    *,
    user_id: str = "",
    account_id: str = "",
    pending_owner: str = "",
    pending_mode: Optional[str] = None,
    pending_spec: Optional[str] = None,
    is_admin: bool = True,
) -> Optional[str]:
    """Ensure local Xray / SOCKS5 tunnel is running for the context and return HTTP proxy URL (or `None` for `direct`).
    Strictly Fail-Closed: raises `ProxyRouteError` if a custom route cannot be established."""
    if not _INITIALIZED:
        await initialize_from_db()

    if pending_mode is not None:
        p_mode = str(pending_mode or "global").strip().lower()
        ow = (pending_owner or f"pending:{uuid.uuid4().hex[:10]}").strip()
        if p_mode == "direct":
            await asyncio.to_thread(release_owner, ow)
            return None
        if p_mode == "system_auto":
            temp_entry = _RouteSpecEntry(owner_key=ow, mode="system_auto", is_admin=True)
            return await asyncio.to_thread(_ensure_effective_entry_sync, temp_entry)
        if p_mode == "custom":
            clean_spec = str(pending_spec or "").strip()
            if not clean_spec:
                raise ProxyRouteError("Укажите адрес прокси или Xray-профиль для режима «Свой прокси»")
            temp_entry = _RouteSpecEntry(
                owner_key=ow,
                mode="custom",
                raw_spec=clean_spec,
                is_admin=is_admin,
            )
            return await asyncio.to_thread(_ensure_effective_entry_sync, temp_entry)
        # p_mode == "global" -> fall through to global route
        provider = "global"

    with _ROUTER_LOCK:
        entry = _select_target_entry_locked(provider, user_id=user_id, account_id=account_id)
        entry_copy = _RouteSpecEntry(
            owner_key=entry.owner_key,
            mode=entry.mode,
            raw_spec=entry.raw_spec,
            safe_label=entry.safe_label,
            corrupted=entry.corrupted,
            is_admin=entry.is_admin,
        )

    return await asyncio.to_thread(_ensure_effective_entry_sync, entry_copy)


async def reload_routes_from_db(*, warm_active_tunnels: bool = True) -> None:
    """Synchronize in-memory routing table with SQLite and stop orphaned tunnels."""
    global _INITIALIZED, _GLOBAL_ENTRY, _ACTIVE_AGY_PROFILE_ID

    from . import database

    bundle = await database.list_all_custom_proxy_routes_for_router()
    g_cfg = bundle["global"]
    g_entry = _RouteSpecEntry(
        owner_key="global",
        mode=str(g_cfg.get("mode") or "system_auto"),
        raw_spec=str(g_cfg.get("proxy_spec") or ""),
        safe_label=str(g_cfg.get("proxy_label") or ""),
        corrupted=bool(g_cfg.get("spec_corrupted", False)),
        is_admin=True,
    )

    new_entries: dict[str, _RouteSpecEntry] = {}
    active_agy_id = str(bundle.get("active_agy_profile_id") or "")

    for item in bundle.get("accounts") or []:
        ow = str(item["owner_key"])
        new_entries[ow] = _RouteSpecEntry(
            owner_key=ow,
            mode=str(item.get("proxy_mode") or "global"),
            raw_spec=str(item.get("proxy_spec") or ""),
            safe_label=str(item.get("proxy_label") or ""),
            corrupted=bool(item.get("proxy_spec_corrupted", False)),
            is_admin=bool(item.get("is_admin", False)),
        )

    to_stop: list[_ManagedTunnel] = []
    warm_targets: list[_RouteSpecEntry] = []

    with _ROUTER_LOCK:
        _GLOBAL_ENTRY = g_entry
        _ACTIVE_AGY_PROFILE_ID = active_agy_id

        # Preserve ephemeral `pending:*` / `probe:*` owners currently in flight
        kept_ephemeral = {
            k: v for k, v in _ROUTE_ENTRIES.items() if k.startswith(("pending:", "probe:"))
        }
        _ROUTE_ENTRIES.clear()
        _ROUTE_ENTRIES.update(kept_ephemeral)
        _ROUTE_ENTRIES.update(new_entries)
        _INITIALIZED = True

        # Detach any owners that were deleted or switched back to `global` / `direct`
        for ow in list(_OWNER_TO_HASH.keys()):
            if ow.startswith(("pending:", "probe:")):
                continue
            if ow == "global":
                if g_entry.mode == "direct":
                    stopped = _detach_owner_locked("global")
                    if stopped:
                        to_stop.append(stopped)
                elif g_entry.mode == "custom" and g_entry.raw_spec.strip():
                    try:
                        p = parse_proxy_spec(g_entry.raw_spec)
                        if _OWNER_TO_HASH.get("global") != p.config_hash or (not p.needs_xray and not p.needs_local_bridge):
                            stopped = _detach_owner_locked("global")
                            if stopped:
                                to_stop.append(stopped)
                    except Exception:
                        stopped = _detach_owner_locked("global")
                        if stopped:
                            to_stop.append(stopped)
                continue

            ent = _ROUTE_ENTRIES.get(ow)
            if ent is None or ent.mode != "custom" or not ent.raw_spec.strip():
                stopped = _detach_owner_locked(ow)
                if stopped:
                    to_stop.append(stopped)
            else:
                try:
                    p = parse_proxy_spec(ent.raw_spec)
                    if _OWNER_TO_HASH.get(ow) != p.config_hash or (not p.needs_xray and not p.needs_local_bridge):
                        stopped = _detach_owner_locked(ow)
                        if stopped:
                            to_stop.append(stopped)
                except Exception:
                    stopped = _detach_owner_locked(ow)
                    if stopped:
                        to_stop.append(stopped)

        if warm_active_tunnels:
            if g_entry.mode in ("custom", "system_auto"):
                warm_targets.append(g_entry)
            if active_agy_id and f"agy:{active_agy_id}" in _ROUTE_ENTRIES:
                agy_ent = _ROUTE_ENTRIES[f"agy:{active_agy_id}"]
                if agy_ent.mode == "custom":
                    warm_targets.append(agy_ent)
            for item in bundle.get("accounts") or []:
                if item.get("is_active") and item.get("is_enabled", True):
                    ent = _ROUTE_ENTRIES.get(str(item["owner_key"]))
                    if ent and ent.mode == "custom":
                        warm_targets.append(ent)

    for tun in to_stop:
        await asyncio.to_thread(_stop_tunnel_instance, tun)

    for w_ent in warm_targets:
        try:
            await asyncio.to_thread(_ensure_effective_entry_sync, w_ent)
        except Exception as exc:
            logger.warning("Warm-up of proxy route %s deferred/failed: %s", w_ent.owner_key, exc)


async def initialize_from_db() -> None:
    await reload_routes_from_db(warm_active_tunnels=True)


async def shutdown_all_tunnels() -> None:
    """Gracefully terminate all managed `xray.exe` instances and local SOCKS5/HTTP bridges."""
    global _INITIALIZED
    with _ROUTER_LOCK:
        tunnels = list(_MANAGED_TUNNELS.values())
        _MANAGED_TUNNELS.clear()
        _OWNER_TO_HASH.clear()
        _ROUTE_ENTRIES.clear()
        _INITIALIZED = False

    for tun in tunnels:
        await asyncio.to_thread(_stop_tunnel_instance, tun)
    await asyncio.to_thread(_BRIDGE_RUNNER.shutdown)


def _format_detected_proxy_safe(raw_proxy: Optional[str]) -> str:
    if not raw_proxy:
        return ""
    try:
        p = parse_proxy_spec(raw_proxy)
        return p.safe_label
    except Exception:
        sp = urllib.parse.urlsplit(raw_proxy if "://" in raw_proxy else f"http://{raw_proxy}")
        if sp.hostname and sp.port:
            return f"{sp.scheme or 'http'}://{sp.hostname}:{sp.port}"
        return ""


def get_proxy_status_payload(*, is_admin: bool, probe_xray_version: bool = True) -> dict[str, Any]:
    """Return RBAC-safe proxy & Xray status payload for `/api/proxy/status` and `/api/system/info`."""
    xray_info = get_xray_info(probe_version=probe_xray_version and is_admin)
    if not is_admin:
        return {
            "mode": "global",
            "effective_label": "Глобальный маршрут сервера",
            "xray": {"available": bool(xray_info.get("available"))},
        }

    with _ROUTER_LOCK:
        mode = _GLOBAL_ENTRY.mode if _INITIALIZED else "system_auto"
        custom_label = _GLOBAL_ENTRY.safe_label if _INITIALIZED else ""
        has_custom = bool(_GLOBAL_ENTRY.raw_spec or custom_label) if _INITIALIZED else False
        tunnels_count = sum(1 for t in _MANAGED_TUNNELS.values() if t.is_alive())

    sys_raw = config.detect_system_proxy()
    sys_safe = _format_detected_proxy_safe(sys_raw)

    if mode == "direct":
        eff_label = "Выкл (Напрямую)"
    elif mode == "custom":
        eff_label = f"Свой: {custom_label or 'настроен'}"
    else:
        eff_label = f"Авто: {sys_safe}" if sys_safe else "Авто: Напрямую (системный прокси выкл.)"

    return {
        "mode": mode,
        "custom_label": custom_label,
        "has_custom_spec": has_custom,
        "system_detected_proxy": sys_safe,
        "effective_label": eff_label,
        "xray": xray_info,
        "active_tunnels_count": tunnels_count,
    }


# ---------------------------------------------------------------------------
# 7. Exit IP & Latency Probe (`POST /api/proxy/test`)
# ---------------------------------------------------------------------------


def _probe_exit_ip_sync(proxy_url: Optional[str], timeout_sec: float = 8.0) -> dict[str, Any]:
    opener = build_urllib_proxy_opener(proxy_url)
    t0 = time.perf_counter()
    last_err = ""

    for kind, url in _PROBE_ENDPOINTS:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "AntigravityHub-RouteProbe/1.0",
                    "Accept": "application/json, text/plain, */*",
                },
            )
            with opener.open(req, timeout=timeout_sec) as resp:
                raw = resp.read(16384).decode("utf-8", errors="replace").strip()
            latency_ms = max(1, int(round((time.perf_counter() - t0) * 1000.0)))

            if kind == "ipwho":
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("success") is not False and data.get("ip"):
                    isp_val = str(data.get("isp") or "")
                    if not isp_val and isinstance(data.get("connection"), dict):
                        isp_val = str(data["connection"].get("isp") or data["connection"].get("org") or "")
                    return {
                        "ok": True,
                        "exit_ip": str(data.get("ip") or ""),
                        "country_code": str(data.get("country_code") or "").upper(),
                        "country": str(data.get("country") or ""),
                        "isp": isp_val,
                        "latency_ms": latency_ms,
                        "error": "",
                    }
            elif kind == "cf_trace":
                kv: dict[str, str] = {}
                for line in raw.splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        kv[k.strip()] = v.strip()
                if kv.get("ip"):
                    return {
                        "ok": True,
                        "exit_ip": kv["ip"],
                        "country_code": kv.get("loc", "").upper(),
                        "country": "",
                        "isp": "",
                        "latency_ms": latency_ms,
                        "error": "",
                    }
            elif kind == "ipify":
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("ip"):
                    return {
                        "ok": True,
                        "exit_ip": str(data["ip"]),
                        "country_code": "",
                        "country": "",
                        "isp": "",
                        "latency_ms": latency_ms,
                        "error": "",
                    }
        except Exception as exc:
            last_err = str(exc)
            t0 = time.perf_counter()
            continue

    return {
        "ok": False,
        "exit_ip": "",
        "country_code": "",
        "country": "",
        "isp": "",
        "latency_ms": 0,
        "error": f"Проверка связи через узел не удалась: {last_err or 'нет ответа от серверов проверки IP'}",
    }


async def probe_proxy_route(
    *,
    current_user: dict[str, Any],
    mode: Optional[str] = None,
    proxy_spec: Optional[str] = None,
    provider: Optional[str] = None,
    account_id: Optional[str] = None,
) -> dict[str, Any]:
    """Probe exit IP, country, ISP, and latency for either an unsaved draft or a saved account/global route.
    Enforces strict SSRF checks and hides server's global exit IP from non-admin users."""
    from . import database

    is_admin = bool(current_user.get("role") == "admin")
    uid = str(current_user.get("id") or "")
    prov = (provider or "").strip().lower()
    aid = (account_id or "").strip()
    clean_mode = (mode or "").strip().lower()
    clean_spec = (proxy_spec or "").strip()

    if prov == "agy" and not is_admin:
        raise ProxyConfigError("Доступ к маршрутам аккаунтов администратора запрещён")

    temp_owner = f"probe:{uuid.uuid4().hex[:10]}"
    mask_global_for_non_admin = False
    route_label = ""

    try:
        # Case 1: Explicit draft `mode` supplied from modal
        if clean_mode:
            if clean_mode in ("system_auto", "direct") and not is_admin:
                raise ProxyConfigError("Режимы системного прокси и прямого выхода сервера доступны только администратору")

            if clean_mode == "global":
                mask_global_for_non_admin = not is_admin
                proxy_url = await ensure_proxy_for_context("global")
                st = get_proxy_status_payload(is_admin=is_admin, probe_xray_version=False)
                route_label = str(st.get("effective_label") or "Глобальный маршрут сервера")
            elif clean_mode in ("system_auto", "direct"):
                proxy_url = await ensure_proxy_for_context(
                    "global",
                    pending_owner=temp_owner,
                    pending_mode=clean_mode,
                    is_admin=True,
                )
                route_label = (
                    "Выкл (Напрямую)"
                    if clean_mode == "direct"
                    else f"Авто ({_format_detected_proxy_safe(config.detect_system_proxy()) or 'Напрямую'})"
                )
            elif clean_mode == "custom":
                # If user clicked "Проверить IP" on an already-saved custom account/global without re-typing secret
                if not clean_spec:
                    if prov == "global" or (not prov and is_admin):
                        saved_g = await database.get_global_proxy_settings(include_decrypted_proxy=True)
                        clean_spec = str(saved_g.get("proxy_spec") or "")
                    elif prov == "agy" and is_admin:
                        prof = (
                            await database.get_account_profile_by_id(aid, include_decrypted_proxy=True)
                            if aid
                            else await database.get_active_account_profile(include_decrypted_proxy=True)
                        )
                        clean_spec = str((prof or {}).get("proxy_spec") or "")
                    elif prov == "chatgpt" and aid:
                        acc = await database.get_user_chatgpt_account(uid, aid, include_decrypted_proxy=True)
                        if not acc:
                            raise ProxyConfigError("Аккаунт ChatGPT не найден")
                        clean_spec = str(acc.get("proxy_spec") or "")
                    elif prov == "claude" and aid:
                        acc = await database.get_user_claude_account(uid, aid, include_decrypted_proxy=True)
                        if not acc:
                            raise ProxyConfigError("Аккаунт Claude не найден")
                        clean_spec = str(acc.get("proxy_spec") or "")

                if not clean_spec:
                    raise ProxyConfigError("Введите адрес прокси или ссылку Xray для проверки")

                parsed = parse_proxy_spec(clean_spec)
                await asyncio.to_thread(
                    validate_proxy_target_security,
                    parsed.host,
                    parsed.port,
                    is_admin=is_admin,
                    resolve_dns=True,
                )
                route_label = parsed.safe_label
                proxy_url = await ensure_proxy_for_context(
                    prov or "global",
                    pending_owner=temp_owner,
                    pending_mode="custom",
                    pending_spec=clean_spec,
                    is_admin=is_admin,
                )
            else:
                raise ProxyConfigError(f"Неизвестный режим маршрута: {clean_mode}")

        # Case 2: Saved provider / account check
        else:
            if prov == "chatgpt" and aid:
                acc = await database.get_user_chatgpt_account(uid, aid)
                if not acc:
                    raise ProxyConfigError("Аккаунт ChatGPT не найден")
                if acc.get("proxy_mode") != "custom":
                    mask_global_for_non_admin = not is_admin
                    route_label = "Глобальный маршрут сервера" if not is_admin else get_proxy_status_payload(is_admin=True, probe_xray_version=False)["effective_label"]
                else:
                    route_label = str(acc.get("proxy_label") or "Свой прокси")
                proxy_url = await ensure_proxy_for_context("chatgpt", user_id=uid, account_id=aid, is_admin=is_admin)
            elif prov == "claude" and aid:
                acc = await database.get_user_claude_account(uid, aid)
                if not acc:
                    raise ProxyConfigError("Аккаунт Claude не найден")
                if acc.get("proxy_mode") != "custom":
                    mask_global_for_non_admin = not is_admin
                    route_label = "Глобальный маршрут сервера" if not is_admin else get_proxy_status_payload(is_admin=True, probe_xray_version=False)["effective_label"]
                else:
                    route_label = str(acc.get("proxy_label") or "Свой прокси")
                proxy_url = await ensure_proxy_for_context("claude", user_id=uid, account_id=aid, is_admin=is_admin)
            elif prov == "agy" and is_admin:
                prof = (
                    await database.get_account_profile_by_id(aid)
                    if aid
                    else await database.get_active_account_profile()
                )
                if prof and prof.get("proxy_mode") == "custom":
                    route_label = str(prof.get("proxy_label") or "Свой прокси")
                else:
                    route_label = get_proxy_status_payload(is_admin=True, probe_xray_version=False)["effective_label"]
                proxy_url = await ensure_proxy_for_context("agy", account_id=aid, is_admin=True)
            else:
                mask_global_for_non_admin = not is_admin
                st = get_proxy_status_payload(is_admin=is_admin, probe_xray_version=False)
                route_label = str(st.get("effective_label") or "Глобальный маршрут сервера")
                proxy_url = await ensure_proxy_for_context("global")

        res = await asyncio.to_thread(_probe_exit_ip_sync, proxy_url, 8.0)
        res["route_label"] = route_label

        if mask_global_for_non_admin:
            return {
                "ok": bool(res.get("ok")),
                "exit_ip": "",
                "country_code": "",
                "country": "",
                "isp": "",
                "latency_ms": int(res.get("latency_ms") or 0),
                "route_label": "Глобальный маршрут сервера",
                "masked": True,
                "error": "Глобальный маршрут сервера недоступен" if not res.get("ok") else "",
            }
        return res
    except ProxyRouteError as exc:
        return {
            "ok": False,
            "exit_ip": "",
            "country_code": "",
            "country": "",
            "isp": "",
            "latency_ms": 0,
            "route_label": route_label,
            "error": str(exc),
        }
    finally:
        await asyncio.to_thread(release_owner, temp_owner)
