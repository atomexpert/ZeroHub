"""Offline notification contracts."""
import asyncio
import base64
import json
import socket
import urllib.error
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi.testclient import TestClient

from app import auth, config, database, main, push_service, sandbox_service
from test_ui_workspace_minimal_v142 import run_workspace_chromium


def decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def push_db(tmp_path, monkeypatch):
    path = tmp_path / "hub.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "ws_manager", main.ConnectionManager())
    monkeypatch.setattr(main, "_PROCESS_PUSH_RECENT", {})
    monkeypatch.setenv("WEBPUSH_VAPID_SUBJECT", "mailto:noreply@zerohub.app")
    monkeypatch.setenv("HUB_SECRET_KEY", "notification-fixture-master-key")
    run(database.init_db())
    database.invalidate_user_cache()
    yield
    database.invalidate_user_cache()


def test_vapid_and_encrypted_record(push_db):
    async def race():
        return await asyncio.gather(push_service.vapid_public_key(), push_service.vapid_public_key())

    a, b = run(race())
    assert a == b and len(decode(a)) == 65
    key = run(push_service.get_vapid_key())
    header, claims, signature = push_service.sign_vapid_jwt(key, "https://fcm.googleapis.com", now=100).split(".")
    assert json.loads(decode(header))["alg"] == "ES256"
    assert json.loads(decode(claims)) == {
        "aud": "https://fcm.googleapis.com", "exp": 43300, "sub": "mailto:noreply@zerohub.app",
    }
    raw = decode(signature)
    assert len(raw) == 64
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decode(a))
    public.verify(
        encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
        f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()),
    )

    client = ec.generate_private_key(ec.SECP256R1())
    secret = b"a" * 16
    payload = push_service.encode_payload({"title": "ZeroHub", "body": '"\\Ж🙂' * 2500})
    assert len(payload) <= 3200
    record = push_service.encrypt_webpush_aes128gcm(push_service._public_key(client), secret, payload)
    assert record[16:21] == b"\x00\x00\x10\x00\x41"
    assert push_service.decrypt_webpush_aes128gcm(client, secret, record) == payload


ENDPOINT = "https://fcm.googleapis.com/fcm/send/private-device-token"


def resolve(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]


def client_keys():
    key = ec.generate_private_key(ec.SECP256R1())
    return key, {"p256dh": push_service._b64(push_service._public_key(key)), "auth": push_service._b64(b"a" * 16)}


def test_ssrf_and_proxy_expiry(push_db, monkeypatch):
    for endpoint in (
        "http://example.com", "https://127.0.0.1", "https://192.168.1.10", "https://[::1]",
        "https://a.localhost", "https://example.com:8443", "https://user@example.com",
    ):
        with pytest.raises(ValueError):
            push_service.validate_push_endpoint(endpoint)
    for ips in [("127.0.0.1",), ("192.168.0.4",), ("100.64.0.1",), ("169.254.1.2",),
                ("224.0.0.1",), ("8.8.8.8", "10.0.0.4")]:
        monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: resolve(*ips))
        with pytest.raises(ValueError):
            push_service.validate_push_endpoint(ENDPOINT)

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: resolve("8.8.8.8"))
    key, keys = client_keys()
    run(database.upsert_push_subscription("admin", ENDPOINT, keys["p256dh"], keys["auth"], "Chrome", "fixture"))
    calls = []

    def direct(parsed, addresses, body, headers):
        calls.append("direct")
        assert addresses == ("8.8.8.8",)
        assert headers["TTL"] == "86400" and headers["Urgency"] == "high"
        assert headers["Content-Encoding"] == "aes128gcm"
        assert headers["Authorization"].startswith("vapid t=")
        assert json.loads(push_service.decrypt_webpush_aes128gcm(key, b"a" * 16, body))["title"] == "Test"
        raise urllib.error.URLError(TimeoutError())

    def proxy(route, url, body, headers):
        calls.append("proxy")
        assert route == "http://proxy.test:8888" and url == ENDPOINT
        return 410

    monkeypatch.setattr(push_service, "_post_direct", direct)
    monkeypatch.setattr(push_service, "_post_proxy", proxy)
    choose = AsyncMock(return_value="http://proxy.test:8888")
    monkeypatch.setattr(push_service.proxy_router, "ensure_proxy_for_context", choose)
    assert run(push_service.notify_user("admin", {"title": "Test"})) == (0, 1)
    choose.assert_awaited_once_with("global")
    assert calls == ["direct", "proxy"]
    assert run(database.list_user_push_subscriptions("admin")) == []


def test_api_focus_and_crash_without_websockets(push_db, monkeypatch):
    client = TestClient(main.app)
    root = "/api/notifications"
    assert client.get(root + "/state").status_code == 401

    async def admin():
        return await database.get_user_by_id("admin")

    monkeypatch.setitem(main.app.dependency_overrides, auth.get_current_user, admin)
    monkeypatch.setattr(auth, "authenticate_ws_user", AsyncMock(return_value=run(admin())))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: resolve("8.8.8.8"))
    _, keys = client_keys()
    result = client.get(root + "/state").json()
    assert result["prefs"]["in_app_toast"] and not result["prefs"]["only_long_turns"]
    assert len(decode(result["vapid_public_key"])) == 65
    assert client.post(root + "/push/test").status_code == 400
    assert client.post(root + "/push/subscribe", json={"endpoint": "https://127.0.0.1", "keys": keys}).status_code == 400
    saved = client.post(root + "/push/subscribe", json={
        "endpoint": ENDPOINT, "keys": keys, "device_label": "iPhone · PWA",
    }).json()
    assert saved["ok"]
    row = saved["subscriptions"][0]
    assert row["id"] == saved["current_sub_id"] and row["device_label"] == "iPhone · PWA"
    assert row["endpoint"] == "fcm.googleapis.com/…-token" and not set(keys) & row.keys()
    assert ENDPOINT not in json.dumps(saved)
    monkeypatch.setattr(push_service, "_post_direct", lambda *args: 201)
    assert client.post(root + "/push/test").json() == {"ok": True, "sent_count": 1, "failed_count": 0}

    with client.websocket_connect("/ws") as ws:
        def focus(visible):
            ws.send_json({"action": "client_focus", "visible": visible, "active_chat_id": "chat-push"})
            ws.send_json({"action": "ping"})
            assert ws.receive_json()["type"] == "pong"

        focus(True)
        assert main.is_user_actively_viewing_chat("admin", "chat-push")
        prefs = client.put(root + "/prefs", json={"only_long_turns": True, "sound": "yes", "ignored": True}).json()
        assert prefs["notification_prefs"]["only_long_turns"]
        assert not prefs["notification_prefs"]["sound"] and "ignored" not in prefs["notification_prefs"]
        assert ws.receive_json()["type"] == "notification_prefs_updated"

        notify = AsyncMock(return_value=(1, 0))
        monkeypatch.setattr(push_service, "notify_user", notify)
        chat = {"id": "chat-push", "kind": "chat", "owner_id": "admin", "title": "Проверка"}
        msg = {
            "content": "<TASK_PROFILE>hidden</TASK_PROFILE>\n| a | b |\n## Готово [APK](file:///D:/Zero.apk)",
            "usage": {"last_run_duration_ms": 15000},
        }
        run(main._dispatch_turn_webpush(chat, "turn_done", msg))
        notify.assert_not_awaited()
        focus(False)
        msg["usage"]["last_run_duration_ms"] = 14999
        run(main._dispatch_turn_webpush(chat, "turn_done", msg))
        notify.assert_not_awaited()
        msg["usage"]["last_run_duration_ms"] = 15000
        run(main._dispatch_turn_webpush(chat, "turn_done", msg))
        run(main._dispatch_turn_webpush(chat, "turn_error", None, "Сбой"))
        assert notify.await_count == 2
        _, payload = notify.await_args_list[0].args
        assert payload["body"] == "Готово APK · 15 с" and payload["data"]["url"] == "/?open_chat=chat-push"
        assert notify.await_args.args[1]["title"] == "⚠️ Проверка"
    assert not main.ws_manager.active and not main.ws_manager.client_focus

    async def crash():
        event = {"transition": "crashed", "service": {"name": "worker", "pid": 27, "exit_code": 1}}
        await main.broadcast_project_processes_update("ZeroNote", transition_event=event, docker_user_id="admin")
        await main.broadcast_project_processes_update("ZeroNote", transition_event=event, docker_user_id="admin")
        await asyncio.gather(*tuple(main._TELEMETRY_BG_TASKS))

    notify.reset_mock()
    run(crash())
    notify.assert_awaited_once()
    assert notify.await_args.args[1]["data"]["url"] == "/?open_processes=ZeroNote"
    assert client.post(root + "/push/unsubscribe", json={"sub_id": row["id"]}).json()["subscriptions"] == []


def test_shared_host_crash_only_reaches_eligible_members(push_db, tmp_path, monkeypatch):
    member = {"id": "member", "role": "user", "is_active": True, "max_access_mode": "host_full"}
    read_only = {**member, "id": "reader", "max_access_mode": "read_only"}
    other = {**member, "id": "outside"}
    users = [run(database.get_user_by_id("admin")), member, read_only, other]
    shared = {"name": "Demo", "member_ids": ["member", "reader"]}
    resolved = {"name": "Demo", "path": str(tmp_path), "is_shared": True, "execution_mode": "host"}
    monkeypatch.setattr(database, "get_shared_project_by_name", AsyncMock(return_value=shared))
    monkeypatch.setattr(database, "list_users", AsyncMock(return_value=users))
    monkeypatch.setattr(database, "get_user_by_id", AsyncMock(side_effect=lambda uid: next(u for u in users if u["id"] == uid)))
    monkeypatch.setattr(main.brain_reader, "resolve_user_project", lambda *args: resolved)
    notify = AsyncMock(return_value=(1, 0))
    monkeypatch.setattr(push_service, "notify_user", notify)

    event = {"service": {"name": "worker", "pid": 42, "exit_code": 2}, "error_tail": "failed to boot"}
    run(main._dispatch_process_crash_webpush("Demo", tmp_path, event, None))
    assert {call.args[0] for call in notify.await_args_list} == {"admin", "member"}
    assert all(call.args[1]["body"].endswith("failed to boot") for call in notify.await_args_list)


def test_docker_watchdog_emits_crash_once(tmp_path, monkeypatch):
    name = "fixture"
    tracked = {name: {"worker": ("owner", tmp_path, "Demo", "started")}}
    monkeypatch.setattr(sandbox_service, "_SANDBOX_WATCHED_SERVICES", tracked)
    report = AsyncMock(return_value=None)
    monkeypatch.setattr(sandbox_service, "_SANDBOX_PROCESSES_NOTIFIER", report)
    svc = {"name": "worker", "started_at": "started", "alive": False, "status": "crashed", "exit_code": 1}
    listing = AsyncMock(return_value=[svc])
    monkeypatch.setattr(sandbox_service, "list_project_sandbox_services", listing)
    monkeypatch.setattr(sandbox_service, "get_project_sandbox_service_logs", AsyncMock(return_value={
        "started_at": "started", "log_tail": "exit",
    }))

    async def inspect_twice():
        await sandbox_service._check_sandbox_service_crashes(name)
        await sandbox_service._check_sandbox_service_crashes(name)
        await asyncio.sleep(0)

    run(inspect_twice())
    report.assert_awaited_once_with("Demo", tmp_path.resolve(), "owner", {
        "transition": "crashed", "service": svc, "error_tail": "exit",
    })


BROWSER_CHECKS = r"""
function check(ok, message) { if (!ok) throw new Error(message); }
async function exercise(width, height, theme) {
  const frame = document.createElement("iframe");
  frame.style.cssText = "position:absolute;border:0;width:" + width + "px;height:" + height + "px";
  const ready = new Promise(resolve => { frame.onload = resolve; });
  frame.srcdoc = workspacePage;
  document.body.append(frame);
  await ready;
  try {
    const w = frame.contentWindow, d = frame.contentDocument, api = w.__AGY_ADVISOR_API__;
    const get = id => d.getElementById(id);
    const flush = () => new Promise(resolve => setTimeout(resolve, 30));
    const checkLayout = node => check(node.clientWidth > 0 && node.scrollWidth <= node.clientWidth + 1,
      "overflow in " + node.id + " (" + node.scrollWidth + " > " + node.clientWidth + ")");
    d.documentElement.dataset.theme = theme;
    const profile = width + "-" + theme;
    api.state.token = "notification-fixture";
    api.state.currentUser = { id: profile, username: profile, role: "admin" };
    const worker = new w.EventTarget();
    Object.defineProperty(w.navigator, "serviceWorker", { value: worker });
    Object.defineProperty(w, "isSecureContext", { value: true });
    Object.defineProperty(d, "hidden", { value: false });
    d.hasFocus = () => true;
    Object.defineProperty(w.navigator, "locks", { value: { request: async (name, callback) => callback() } });
    w.PushManager = function () {};
    w.Notification = { permission: "granted", requestPermission: async () => "granted" };

    let subscription = null;
    let prefs = { on_turn_done: true, in_app_toast: true, only_long_turns: true };
    let devices = [];
    const endpoint = "https://fcm.googleapis.com/send/fixture";
    const pushCalls = [];
    worker.getRegistration = async () => ({
      active: {},
      pushManager: {
        getSubscription: async () => subscription,
        subscribe: async opts => {
          pushCalls.push(opts);
          subscription = {
            endpoint,
            toJSON: () => ({ endpoint, keys: { p256dh: "fixture-dh", auth: "fixture-auth" } }),
            unsubscribe: async () => { subscription = null; return true; },
          };
          return subscription;
        },
      },
    });

    const chatOpened = [];
    const calls = [];
    w.fetch = async (url, options) => {
      const res = value => new w.Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
      calls.push(url);
      if (url.startsWith("/api/chats/") && url.includes("?include_thinking=false")) {
        const id = url.split("/")[3].split("?")[0];
        chatOpened.push(id);
        return res({ chat: { id, kind: "chat", title: "Диалог " + id }, messages: [] });
      }
      if (url === "/api/notifications/state") {
        return res({ prefs, subscriptions: devices, vapid_public_key: "BA" });
      }
      if (url === "/api/notifications/prefs") {
        prefs = { ...prefs, ...JSON.parse(options.body) };
        return res({ notification_prefs: prefs });
      }
      if (url.endsWith("/push/subscribe")) {
        const payload = JSON.parse(options.body);
        check(payload.endpoint === endpoint && payload.keys.auth === "fixture-auth" && payload.device_label,
          "subscription payload");
        devices = [{ id: "device", device_label: "Мобильное устройство " + "ДлинноеИмя".repeat(10) }];
        return res({ subscriptions: devices, current_sub_id: "device" });
      }
      if (url.endsWith("/push/unsubscribe")) {
        check(JSON.parse(options.body).sub_id === "device", "wrong device removed");
        devices = [];
        return res({ subscriptions: devices });
      }
      return res({});
    };

    const modal = get("settingsModal");
    modal.classList.remove("hidden");
    modal.querySelector(".settings-modal-card").style.animation = "none";
    api.bindNotificationEvents();
    await api.loadNotificationState();
    check(get("notifOnTurnDoneChk").checked && get("notifOnlyLongTurnsChk").checked, "prefs not rendered");
    const checkbox = get("notifOnTurnErrorChk");
    checkbox.click();
    await flush();
    check(prefs.on_turn_error === false && !checkbox.checked, "checkbox did not save");

    get("notifTogglePushBtn").click();
    await flush();
    check(pushCalls.length === 1 && pushCalls[0].userVisibleOnly &&
          pushCalls[0].applicationServerKey[0] === 4, "browser not subscribed");
    check(api.state.notifications.currentDevicePushActive && get("notifDevicesList").children.length === 1,
      "push active state not shown");
    for (const id of ["settingsPaneAppearance", "settingsNotificationsCard", "notifDevicesList"]) checkLayout(get(id));
    check(d.documentElement.scrollWidth <= width, "page overflow");
    get("notifDevicesList").querySelector("button").click();
    await flush();
    check(!subscription && !api.state.notifications.currentDevicePushActive &&
      !get("notifDevicesList").children.length && calls.includes("/api/notifications/push/unsubscribe"), "unlink failed");

    modal.classList.add("hidden");
    const event = { chat: { title: "Готовая сборка" }, message: { id: "m", usage: { last_run_duration_ms: 14999 } } };
    const toasts = () => Array.from(get("toastContainer").querySelectorAll(".toast.is-done"));
    api.notifyTurnEvent("turn_done", event, "chat-done");
    await flush();
    check(toasts().length === 0, "short turn triggered a toast");
    event.message.usage.last_run_duration_ms = 15000;
    api.notifyTurnEvent("turn_done", event, "chat-done");
    api.notifyTurnEvent("turn_done", event, "chat-done");
    await flush();
    check(toasts().length === 1, "expected one background toast, got " + toasts().length);
    toasts()[0].querySelector(".toast-action-btn").click();
    await flush();
    check(chatOpened.includes("chat-done"), "toast did not open chat");

    worker.dispatchEvent(new w.MessageEvent("message", {
      data: { type: "zerohub_notification_click", kind: "turn_done", chat_id: "clicked" },
    }));
    await flush();
    check(chatOpened.includes("clicked"), "SW click did not open chat");
    return profile;
  } finally { frame.remove(); }
}

(async () => {
  const result = [];
  for (const [width, height] of [[1440, 900], [390, 844], [320, 568]]) {
    for (const theme of ["dark", "light"]) {
      try { result.push(await exercise(width, height, theme)); }
      catch (error) { throw new Error(width + "x" + height + " " + theme + ": " + error.message); }
    }
  }
  document.getElementById("report").textContent = JSON.stringify({ result });
})().catch(error => {
  document.getElementById("report").textContent = JSON.stringify({ error: error.message });
});
"""


def test_notification_settings_in_chromium(tmp_path):
    assert run_workspace_chromium(tmp_path, BROWSER_CHECKS) == [
        f"{width}-{theme}" for width in (1440, 390, 320) for theme in ("dark", "light")
    ]
