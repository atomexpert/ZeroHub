"""VAPID and single-record RFC 8291 Web Push delivery."""
import asyncio
import base64
import binascii
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import database, proxy_router

_KEY_NAME = "webpush_vapid_v1"
_PROXY_PUSH_DOMAINS = (
    "fcm.googleapis.com",
    "push.apple.com",
    "push.services.mozilla.com",
    "notify.windows.com",
)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Некорректный ключ Push")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except binascii.Error as exc:
        raise ValueError("Некорректный ключ Push") from exc


def _public_key(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


async def get_vapid_key() -> ec.EllipticCurvePrivateKey:
    async with database.connect_db() as db:
        async def read():
            async with db.execute("SELECT value FROM hub_meta WHERE key = ?", (_KEY_NAME,)) as cursor:
                return await cursor.fetchone()

        row = await read()
        if row is None:
            key = ec.generate_private_key(ec.SECP256R1())
            scalar = _b64(key.private_numbers().private_value.to_bytes(32, "big"))
            encrypted = database.encrypt_scoped_proxy_spec("webpush_vapid", scalar)
            await db.execute(
                "INSERT OR IGNORE INTO hub_meta (key, value, updated_at) VALUES (?, ?, ?)",
                (_KEY_NAME, encrypted, database.utc_now_iso()),
            )
            await db.commit()
            row = await read()
    scalar = database.decrypt_scoped_proxy_spec("webpush_vapid", row[0])
    return ec.derive_private_key(int.from_bytes(_decode(scalar), "big"), ec.SECP256R1())


async def vapid_public_key() -> str:
    return _b64(_public_key(await get_vapid_key()))


def sign_vapid_jwt(key: ec.EllipticCurvePrivateKey, audience: str, *, now: int | None = None) -> str:
    claims = {
        "aud": audience,
        "sub": os.getenv("WEBPUSH_VAPID_SUBJECT", "mailto:noreply@zerohub.app"),
        "exp": (int(time.time()) if now is None else now) + 43200,
    }
    message = _b64(b'{"typ":"JWT","alg":"ES256"}') + "." + _b64(json.dumps(claims).encode())
    r, s = decode_dss_signature(key.sign(message.encode("ascii"), ec.ECDSA(hashes.SHA256())))
    return message + "." + _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def _client_key(point: bytes, secret: bytes) -> ec.EllipticCurvePublicKey:
    if len(point) != 65 or point[0] != 4 or len(secret) != 16:
        raise ValueError("Некорректные ключи Push")
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)


def validate_push_keys(p256dh: str, auth: str) -> None:
    if len(p256dh) != 87 or len(auth) != 22:
        raise ValueError("Некорректные ключи Push")
    _client_key(_decode(p256dh), _decode(auth))


def _record_keys(shared: bytes, auth: bytes, salt: bytes, ua: bytes, sender: bytes) -> tuple[bytes, bytes]:
    ikm = HKDF(hashes.SHA256(), 32, auth, b"WebPush: info\x00" + ua + sender).derive(shared)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    return cek, nonce


def encrypt_webpush_aes128gcm(ua_public: bytes, auth: bytes, plaintext: bytes) -> bytes:
    if len(plaintext) > 3200:
        raise ValueError("Payload exceeds 3200 bytes")
    client = _client_key(ua_public, auth)
    sender = ec.generate_private_key(ec.SECP256R1())
    sender_public = _public_key(sender)
    salt = os.urandom(16)
    cek, nonce = _record_keys(sender.exchange(ec.ECDH(), client), auth, salt, ua_public, sender_public)
    return salt + struct.pack(">IB", 4096, 65) + sender_public + AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)


def decrypt_webpush_aes128gcm(client: ec.EllipticCurvePrivateKey, auth: bytes, record: bytes) -> bytes:
    if len(record) < 103 or record[16:21] != struct.pack(">IB", 4096, 65):
        raise ValueError("Invalid Web Push record")
    sender_public = record[21:86]
    sender = _client_key(sender_public, auth)
    cek, nonce = _record_keys(
        client.exchange(ec.ECDH(), sender), auth, record[:16], _public_key(client), sender_public
    )
    plaintext = AESGCM(cek).decrypt(nonce, record[86:], None).rstrip(b"\x00")
    if not plaintext.endswith(b"\x02"):
        raise ValueError("Invalid Web Push record delimiter")
    return plaintext[:-1]


def encode_payload(payload: dict) -> bytes:
    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8", errors="replace")

    fields = {**payload, "body": ""}
    base = encode(fields)
    if len(base) > 3200:
        raise ValueError("Push metadata exceeds 3200 bytes")
    body = str(payload.get("body") or "")[:3200]
    # The JSON string's escaping can consume extra bytes for quotes and control characters.
    while len(encode(body)) - 2 > 3200 - len(base):
        body = body[:len(body) // 2]
    return encode({**fields, "body": body})


def validate_push_endpoint(endpoint_url: str) -> tuple[urllib.parse.SplitResult, tuple[str, ...]]:
    if len(endpoint_url) > 2048 or not endpoint_url.isascii() or re.search(r"[\x00-\x20\x7f\\]", endpoint_url):
        raise ValueError("Некорректный адрес Push")
    parsed = urllib.parse.urlsplit(endpoint_url)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.netloc.lower() not in (host, host + ":443")
        or parsed.fragment or "#" in endpoint_url or len(host) > 253
        or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}", host)
        or host.endswith((".localhost", ".local", ".internal"))
    ):
        raise ValueError("Требуется публичный HTTPS-адрес Push")
    addresses = tuple(dict.fromkeys(
        item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    ))
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast or ip.is_reserved or getattr(ip, "is_site_local", False):
            raise ValueError("Требуется публичный HTTPS-адрес Push")
    if not addresses:
        raise ValueError("Адрес Push не найден")
    return parsed, addresses


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    handler_order = 400

    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


_DEFAULT_SSL_CONTEXT_LOCK = threading.Lock()
_DEFAULT_SSL_CONTEXT: ssl.SSLContext | None = None


def _get_default_ssl_context() -> ssl.SSLContext:
    global _DEFAULT_SSL_CONTEXT
    if _DEFAULT_SSL_CONTEXT is None:
        with _DEFAULT_SSL_CONTEXT_LOCK:
            if _DEFAULT_SSL_CONTEXT is None:
                _DEFAULT_SSL_CONTEXT = ssl.create_default_context()
    return _DEFAULT_SSL_CONTEXT


def _post_direct(parsed, addresses, body, headers) -> int:
    host = parsed.hostname
    context = _get_default_ssl_context()
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    # Use a validated address for the connection and the original hostname for TLS.
    conn = http.client.HTTPSConnection(host, timeout=8.0, context=context)
    for address in addresses[:2]:
        try:
            with socket.create_connection((address, 443), timeout=8.0) as sock:
                conn.sock = context.wrap_socket(sock, server_hostname=host)
                conn.request("POST", target, body, headers)
                with conn.getresponse() as response:
                    return response.status
        except (OSError, http.client.HTTPException) as exc:
            failure = exc
        finally:
            conn.close()
    raise failure


def _post_proxy(proxy, endpoint, body, headers) -> int:
    opener = proxy_router.build_urllib_proxy_opener(proxy)
    opener.add_handler(_NoRedirect())
    request = urllib.request.Request(endpoint, body, headers, method="POST")
    try:
        with opener.open(request, timeout=8.0) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code


@dataclass(frozen=True)
class PushResult:
    sent: bool = False
    expired: bool = False
    error: str = ""


async def send_webpush(sub: dict, payload: dict, key: ec.EllipticCurvePrivateKey) -> PushResult:
    endpoint = sub["endpoint"]
    try:
        parsed, addresses = await asyncio.to_thread(validate_push_endpoint, endpoint)
        body = encrypt_webpush_aes128gcm(_decode(sub["p256dh"]), _decode(sub["auth"]), encode_payload(payload))
    except (ValueError, OSError):
        return PushResult(error="Некорректная подписка или адрес Push недоступен")
    audience = "https://" + parsed.hostname
    headers = {
        "TTL": "86400",
        "Urgency": "high",
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "Authorization": f"vapid t={sign_vapid_jwt(key, audience)}, k={_b64(_public_key(key))}",
    }
    try:
        status = await asyncio.to_thread(_post_direct, parsed, addresses, body, headers)
    except (OSError, http.client.HTTPException):
        # DNS is resolved by the proxy. Limit that path to browser vendors' push services.
        host = parsed.hostname
        if not any(host == domain or host.endswith("." + domain) for domain in _PROXY_PUSH_DOMAINS):
            return PushResult(error="Не удалось подключиться к серверу Push")
        try:
            proxy = await proxy_router.ensure_proxy_for_context("global")
            if not proxy:
                return PushResult(error="Не удалось подключиться к серверу Push")
            status = await asyncio.to_thread(_post_proxy, proxy, endpoint, body, headers)
        except (OSError, http.client.HTTPException, ValueError, RuntimeError):
            return PushResult(error="Сервер Push недоступен через прокси")
    if 200 <= status < 300:
        return PushResult(sent=True)
    return PushResult(expired=status in {404, 410}, error=f"Сервер Push вернул HTTP {status}")


async def notify_user(user_id: str, payload: dict) -> tuple[int, int]:
    subscriptions = await database.list_user_push_subscriptions(user_id, include_secrets=True)
    if not subscriptions:
        return 0, 0
    key = await get_vapid_key()

    async def deliver(sub):
        result = await send_webpush(sub, payload, key)
        if result.expired:
            await database.delete_expired_push_endpoint(sub["endpoint"], sub_id=sub["id"])
        else:
            await database.record_push_subscription_result(sub["id"], result.error)
        return result.sent

    sent = sum(await asyncio.gather(*(deliver(sub) for sub in subscriptions)))
    return sent, len(subscriptions) - sent
