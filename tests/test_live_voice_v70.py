"""
Automated isolated tests for Antigravity Hub v=70 (Live Voice):
1. `sanitize_text_for_tts` & `LiveSentenceChunker`: code fence suppression, Markdown/URL/path stripping,
   code-only fallback ("Код выведен на экран."), long-text truncation ("Полный текст готов в чате."),
   and aggressive chunk #0 splitting across streaming deltas.
2. Ephemeral turbo model invariant: `live_voice=True` with `model="gemini-3.8-flash-low"` NEVER overwrites
   `chat.model` (`"gemini-4-carbon-high"`) in SQLite, while user/assistant message rows and `stream_agy_turn`
   use `"gemini-3.8-flash-low"`, and `user_msg.content` stays clean without `[РЕЖИМ ЖИВОЙ ГОЛОСОВОЙ БЕСЕДЫ...]`.
3. Streaming TTS pipeline & RBAC isolation: `live_tts_chunk` (ordered `seq=0, 1, ...`, base64 MP3) and
   `live_tts_done` are delivered ONLY to the triggering user's WebSocket (never leaked to another user's WS),
   and `cancel_generation` aborts synthesis and emits `live_tts_cancelled`.
4. Continuous `/ws/voice` (`live_call=True`): emits `live_call_ready`, detects speech + trailing silence via
   turn-taking VAD to emit `live_utterance` while keeping the socket open for a 2nd utterance, and supports
   mid-call `reset_buffer` and `flush_utterance`.
5. REST endpoints `GET /api/voice/live-capabilities` & `POST /api/voice/tts` (including outbound proxy pass-through).

Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER touches live `data/hub.db` or `run_server.py`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Any

import numpy as np

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
    def __init__(self, app: Any, *, client: tuple[str, int] = ("127.0.0.1", 52700)) -> None:
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


class _AsgiWebSocketSession:
    """In-memory ASGI WebSocket client for fast deterministic async testing."""

    def __init__(self, app: Any, path: str, *, query: str = "") -> None:
        self._app = app
        self._path = path
        self._query = query
        self._in_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._out_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[Any] | None = None
        self.accepted = False
        self.close_code: int | None = None

    async def __aenter__(self) -> "_AsgiWebSocketSession":
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": self._path,
            "raw_path": self._path.encode("ascii"),
            "query_string": self._query.encode("ascii"),
            "headers": [(b"host", b"127.0.0.1:8080")],
            "client": ("127.0.0.1", 52701),
            "server": ("127.0.0.1", 8080),
            "subprotocols": [],
        }
        await self._in_q.put({"type": "websocket.connect"})

        async def _receive() -> dict[str, Any]:
            return await self._in_q.get()

        async def _send(msg: dict[str, Any]) -> None:
            await self._out_q.put(msg)

        self._task = asyncio.create_task(self._app(scope, _receive, _send))
        first = await asyncio.wait_for(self._out_q.get(), timeout=5.0)
        if first.get("type") == "websocket.accept":
            self.accepted = True
        elif first.get("type") == "websocket.close":
            self.close_code = int(first.get("code") or 1000)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def send_json(self, data: dict[str, Any]) -> None:
        await self._in_q.put({"type": "websocket.receive", "text": json.dumps(data)})

    async def send_bytes(self, raw: bytes) -> None:
        await self._in_q.put({"type": "websocket.receive", "bytes": raw})

    async def recv_json(self, timeout: float = 4.0) -> dict[str, Any]:
        while True:
            msg = await asyncio.wait_for(self._out_q.get(), timeout=timeout)
            mtype = msg.get("type")
            if mtype == "websocket.send" and msg.get("text") is not None:
                return json.loads(msg["text"])
            if mtype == "websocket.close":
                self.close_code = int(msg.get("code") or 1000)
                raise RuntimeError(f"WebSocket closed with code {self.close_code}")

    async def drain_available_json(self, wait_sec: float = 0.08) -> list[dict[str, Any]]:
        await asyncio.sleep(wait_sec)
        items: list[dict[str, Any]] = []
        while not self._out_q.empty():
            msg = self._out_q.get_nowait()
            if msg.get("type") == "websocket.send" and msg.get("text") is not None:
                items.append(json.loads(msg["text"]))
        return items

    async def close(self) -> None:
        if self._task and not self._task.done():
            await self._in_q.put({"type": "websocket.disconnect", "code": 1000})
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except Exception:
                self._task.cancel()


class TestLiveVoiceV70(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database
        import app.main as main
        from unittest.mock import AsyncMock, patch

        # Guest reserve checks are external CLI I/O, independently of mocked TTS/turns.
        quota_patch = patch.object(main, "fetch_account_quotas", AsyncMock(return_value={
            "ok": True, "gemini_5h_pct": 100, "claude_5h_pct": 100,
        }))
        quota_patch.start()
        self.addCleanup(quota_patch.stop)

        self._live_db = ROOT_DIR / "data" / "hub.db"
        self._live_stat_before = (
            (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            if self._live_db.exists()
            else None
        )

        self._tmp = tempfile.TemporaryDirectory(
            prefix="agy_test_v70_", ignore_cleanup_errors=True
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
            "cfg_sbx": config.CHAT_SANDBOX_DIR,
            "eng_sbx": agy_engine.CHAT_SANDBOX_DIR,
        }

        tmp_db = self.tmp_root / "test_v70.db"
        tmp_cli = self.tmp_root / "cli_homes"
        tmp_agy = self.tmp_root / "agy_cli_data"
        tmp_ws = self.tmp_root / "workspaces"
        tmp_sh = self.tmp_root / "shared_projects"
        tmp_up = self.tmp_root / "uploads"
        tmp_sbx = self.tmp_root / "chat_sandbox"
        for d in (tmp_cli, tmp_agy, tmp_ws, tmp_sh, tmp_up, tmp_sbx):
            d.mkdir(parents=True, exist_ok=True)

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
        config.CHAT_SANDBOX_DIR = tmp_sbx
        agy_engine.CHAT_SANDBOX_DIR = tmp_sbx

    async def asyncTearDown(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database

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
        config.CHAT_SANDBOX_DIR = self._orig["cfg_sbx"]
        agy_engine.CHAT_SANDBOX_DIR = self._orig["eng_sbx"]

        _unlink_test_junctions(self.tmp_root)
        self._tmp.cleanup()

        if self._live_stat_before is not None:
            live_after = (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            self.assertEqual(
                self._live_stat_before,
                live_after,
                "Live data/hub.db must NEVER be modified by tests!",
            )

    def test_sanitizer_chunker_and_prompt_augmentation(self) -> None:
        import app.agy_engine as agy_engine
        import app.tts_service as tts_service

        # 1) Code-only reply -> short Russian notice
        code_only = "```python\ndef hello():\n    return 42\n```"
        self.assertEqual(
            tts_service.sanitize_text_for_tts(code_only),
            "Код выведен на экран.",
        )

        # 2) Markdown links, URLs, Windows file paths, formatting symbols stripped
        rich_md = (
            "## Готово!\n"
            "Откройте [модуль main.py](file:///D:/Internet/Antigravity/Antigravity_Hub/app/main.py#L10) "
            "или https://example.com/docs/test и проверьте `D:\\Internet\\Antigravity\\run.bat` — **всё работает**!"
        )
        cleaned = tts_service.sanitize_text_for_tts(rich_md)
        self.assertIn("Готово!", cleaned)
        self.assertIn("модуль main.py", cleaned)
        self.assertIn("всё работает!", cleaned)
        self.assertNotIn("file:///", cleaned)
        self.assertNotIn("https://", cleaned)
        self.assertNotIn("D:\\Internet", cleaned)
        self.assertNotIn("**", cleaned)

        # 3) Long text > 750 chars smartly truncated + notice
        long_txt = ("Это проверочное предложение для теста голосового синтеза. " * 20).strip()
        self.assertGreater(len(long_txt), 900)
        trunc = tts_service.sanitize_text_for_tts(long_txt, max_chars=750)
        self.assertLessEqual(len(trunc), 750)
        self.assertTrue(trunc.endswith("Полный текст готов в чате."))

        # 4) Rate normalization clamped to [-20%, +50%]
        self.assertEqual(tts_service.normalize_tts_rate("1.0"), "+0%")
        self.assertEqual(tts_service.normalize_tts_rate("1.15"), "+15%")
        self.assertEqual(tts_service.normalize_tts_rate("1.3×"), "+30%")
        self.assertEqual(tts_service.normalize_tts_rate("+95%"), "+50%")
        self.assertEqual(tts_service.normalize_tts_rate("-60%"), "-20%")

        # 5) LiveSentenceChunker streaming deltas + code fence skipping
        chunker = tts_service.LiveSentenceChunker(max_total_chars=750)
        emitted: list[str] = []
        stream_deltas = [
            "Привет! Я уже проверил лог сервера. ",
            "Вот патч:\n```py",
            "thon\nx = 1 + 2\nprint(x)\n```",
            "\nТеперь пересобери проект и проверь статус соединения на главной странице.",
        ]
        for d in stream_deltas:
            emitted.extend(chunker.push(d))
        emitted.extend(chunker.flush())

        self.assertGreaterEqual(len(emitted), 2)
        self.assertEqual(emitted[0], "Привет! Я уже проверил лог сервера.")
        joined_spoken = " ".join(emitted)
        self.assertNotIn("print(x)", joined_spoken)
        self.assertIn("Теперь пересобери проект", joined_spoken)

        # 6) Prompt augmentation adds LIVE VOICE instruction only when live_voice=True
        normal_prompt = agy_engine._augment_prompt_for_mode(
            kind="chat",
            prompt="Привет",
            attachments=[],
            is_first_turn=False,
            live_voice=False,
        )
        self.assertEqual(normal_prompt, "Привет")

        live_prompt = agy_engine._augment_prompt_for_mode(
            kind="chat",
            prompt="Привет",
            attachments=[],
            is_first_turn=False,
            live_voice=True,
        )
        self.assertIn("[РЕЖИМ ЖИВОЙ ГОЛОСОВОЙ БЕСЕДЫ (LIVE VOICE):", live_prompt)
        self.assertTrue(live_prompt.endswith("Привет"))

    async def test_rest_tts_and_live_capabilities_with_proxy(self) -> None:
        import edge_tts

        import app.auth as auth
        import app.database as database
        import app.main as main
        import app.tts_service as tts_service

        await database.init_db()
        user = await database.create_user(
            username="voice_rest_user",
            display_name="Voice REST",
            password="pass1234",
        )
        token = auth.create_user_session_token(user["id"], user.get("session_version", 1))
        headers = {"Authorization": f"Bearer {token}"}

        captured_calls: list[dict[str, Any]] = []
        orig_comm = edge_tts.Communicate
        orig_proxy_fn = tts_service.resolve_outbound_proxy
        orig_fetch_models = main.fetch_available_models

        class _FakeCommunicate:
            def __init__(self, text: str, voice: str, **kwargs: Any) -> None:
                captured_calls.append({"text": text, "voice": voice, **kwargs})

            async def stream(self):
                yield {"type": "WordBoundary", "offset": 0}
                yield {"type": "audio", "data": b"ID3FAKE_MP3_PART_1"}
                yield {"type": "audio", "data": b"_PART_2"}

        async def _fake_models(force_refresh: bool = False):
            return [
                {"id": "gemini-4-carbon-high", "label": "Gemini 4 Carbon (High)"},
                {"id": "gemini-3.8-flash-low", "label": "Gemini 3.8 Flash (Low)"},
            ]

        edge_tts.Communicate = _FakeCommunicate
        tts_service.resolve_outbound_proxy = lambda: "http://127.0.0.1:10809"
        main.fetch_available_models = _fake_models
        try:
            client = _MiniAsgiClient(main.app)

            cap_res = await client.request(
                "GET", "/api/voice/live-capabilities", headers=headers
            )
            self.assertEqual(cap_res.status_code, 200)
            cap_json = cap_res.json()
            self.assertTrue(cap_json["live_voice_supported"])
            self.assertTrue(cap_json["tts_available"])
            self.assertEqual(cap_json["default_voice"], "ru-RU-DmitryNeural")
            self.assertEqual(cap_json["default_turbo_model"], "gemini-3.8-flash-low")

            tts_res = await client.request(
                "POST",
                "/api/voice/tts",
                headers=headers,
                json_body={
                    "text": "**Привет**, мир! Открой https://example.com",
                    "voice": "ru-RU-SvetlanaNeural",
                    "rate": "1.3",
                },
            )
            self.assertEqual(tts_res.status_code, 200)
            self.assertEqual(tts_res.headers.get("content-type"), "audio/mpeg")
            self.assertEqual(tts_res.content, b"ID3FAKE_MP3_PART_1_PART_2")
            self.assertEqual(len(captured_calls), 1)
            self.assertEqual(captured_calls[0]["voice"], "ru-RU-SvetlanaNeural")
            self.assertEqual(captured_calls[0]["rate"], "+30%")
            self.assertEqual(captured_calls[0]["proxy"], "http://127.0.0.1:10809")
            self.assertNotIn("https://", captured_calls[0]["text"])
        finally:
            edge_tts.Communicate = orig_comm
            tts_service.resolve_outbound_proxy = orig_proxy_fn
            main.fetch_available_models = orig_fetch_models

    async def test_ephemeral_turbo_model_and_streaming_tts_rbac_isolation(self) -> None:
        import app.auth as auth
        import app.database as database
        import app.main as main
        import app.tts_service as tts_service

        await database.init_db()
        alice = await database.create_user(
            username="alice_live",
            display_name="Alice",
            password="pass1234",
        )
        bob = await database.create_user(
            username="bob_live",
            display_name="Bob",
            password="pass1234",
        )
        alice_token = auth.create_user_session_token(alice["id"], alice.get("session_version", 1))
        bob_token = auth.create_user_session_token(bob["id"], bob.get("session_version", 1))

        # Create chat pinned to heavy model `gemini-4-carbon-high`
        chat = await database.create_chat(
            owner_id=alice["id"],
            kind="chat",
            title="Архитектурный чат",
            model="gemini-4-carbon-high",
            access_mode="read_only",
        )

        captured_turn_kwargs: dict[str, Any] = {}
        orig_stream = main.stream_agy_turn
        orig_synth = tts_service.synthesize_tts_mp3
        orig_fetch_models = main.fetch_available_models
        orig_quota_refresh = main._refresh_and_broadcast_quotas

        async def _fake_models(force_refresh: bool = False):
            return [
                {"id": "gemini-4-carbon-high", "label": "Gemini 4 Carbon (High)"},
                {"id": "gemini-3.8-flash-low", "label": "Gemini 3.8 Flash (Low)"},
            ]

        async def _fake_stream_agy_turn(**kwargs: Any):
            captured_turn_kwargs.update(kwargs)
            d1 = "Первое предложение готово для быстрой озвучки. "
            d2 = "Второе предложение завершает голосовой ответ."
            yield {"type": "text_delta", "delta": d1, "seq": 1}
            await asyncio.sleep(0.02)
            yield {"type": "text_delta", "delta": d2, "seq": 2}
            yield {
                "type": "done",
                "content": d1 + d2,
                "thinking": "",
                "tool_calls": [],
                "usage": {"input_tokens": 20, "output_tokens": 18, "total_tokens": 38},
            }

        async def _fake_synth(text: str, voice: str = "ru-RU-DmitryNeural", rate: str = "+15%", **kw: Any) -> bytes:
            return f"MP3:{voice}:{rate}:{text}".encode("utf-8")

        async def _noop_quota_refresh(*_a: Any, **_kw: Any) -> None:
            return None

        main.fetch_available_models = _fake_models
        main.stream_agy_turn = _fake_stream_agy_turn
        tts_service.synthesize_tts_mp3 = _fake_synth
        main._refresh_and_broadcast_quotas = _noop_quota_refresh

        try:
            async with _AsgiWebSocketSession(
                main.app, "/ws", query=f"token={alice_token}"
            ) as ws_alice, _AsgiWebSocketSession(
                main.app, "/ws", query=f"token={bob_token}"
            ) as ws_bob:
                self.assertTrue(ws_alice.accepted)
                self.assertTrue(ws_bob.accepted)

                await ws_alice.send_json(
                    {
                        "action": "send_message",
                        "chat_id": chat["id"],
                        "prompt": "Коротко расскажи статус",
                        "model": "gemini-3.8-flash-low",
                        "access_mode": "read_only",
                        "live_voice": True,
                        "live_session_id": "lv_test_1",
                        "tts_voice": "ru-RU-DmitryNeural",
                        "tts_rate": "+15%",
                    }
                )

                alice_events: list[dict[str, Any]] = []
                got_tts_done = False
                for _ in range(30):
                    ev = await ws_alice.recv_json(timeout=4.0)
                    alice_events.append(ev)
                    if ev.get("type") == "live_tts_done":
                        got_tts_done = True
                        break

                self.assertTrue(got_tts_done)
                bob_events = await ws_bob.drain_available_json(wait_sec=0.08)

                # RBAC: Bob MUST NOT receive any of Alice's live_tts_chunk / live_tts_done / chat events
                bob_tts = [e for e in bob_events if str(e.get("type", "")).startswith("live_tts")]
                self.assertEqual(bob_tts, [])

                tts_chunks = [e for e in alice_events if e.get("type") == "live_tts_chunk"]
                self.assertGreaterEqual(len(tts_chunks), 2)
                self.assertEqual([c["seq"] for c in tts_chunks], list(range(len(tts_chunks))))
                self.assertEqual(tts_chunks[0]["live_session_id"], "lv_test_1")
                decoded_0 = base64.b64decode(tts_chunks[0]["audio_b64"]).decode("utf-8")
                self.assertIn("Первое предложение готово", decoded_0)

                # CRITICAL INVARIANT: chat["model"] in SQLite MUST still be `gemini-4-carbon-high`!
                fresh_chat = await database.get_chat(chat["id"])
                self.assertIsNotNone(fresh_chat)
                self.assertEqual(fresh_chat["model"], "gemini-4-carbon-high")

                # Meanwhile stream_agy_turn & saved messages used `gemini-3.8-flash-low` and live_voice=True!
                self.assertEqual(captured_turn_kwargs.get("model"), "gemini-3.8-flash-low")
                self.assertTrue(captured_turn_kwargs.get("live_voice"))

                msgs = await database.get_messages(chat["id"])
                self.assertEqual(len(msgs), 2)
                user_m, asst_m = msgs[0], msgs[1]
                self.assertEqual(user_m["model"], "gemini-3.8-flash-low")
                self.assertEqual(asst_m["model"], "gemini-3.8-flash-low")
                self.assertEqual(user_m["content"], "Коротко расскажи статус")
                self.assertNotIn("РЕЖИМ ЖИВОЙ ГОЛОСОВОЙ БЕСЕДЫ", user_m["content"])
        finally:
            main.fetch_available_models = orig_fetch_models
            main.stream_agy_turn = orig_stream
            tts_service.synthesize_tts_mp3 = orig_synth
            main._refresh_and_broadcast_quotas = orig_quota_refresh

    async def test_live_tts_cancel_on_barge_in_cancel_generation(self) -> None:
        import app.auth as auth
        import app.database as database
        import app.main as main
        import app.tts_service as tts_service

        await database.init_db()
        alice = await database.create_user(
            username="alice_cancel",
            display_name="Alice Cancel",
            password="pass1234",
        )
        alice_token = auth.create_user_session_token(alice["id"], alice.get("session_version", 1))
        chat = await database.create_chat(
            owner_id=alice["id"],
            kind="chat",
            title="Barge-In тест",
            model="gemini-4-carbon-high",
        )

        cancel_triggered = asyncio.Event()
        orig_stream = main.stream_agy_turn
        orig_cancel = main.cancel_chat_generation
        orig_synth = tts_service.synthesize_tts_mp3
        orig_fetch_models = main.fetch_available_models

        async def _fake_models(force_refresh: bool = False):
            return [{"id": "gemini-3.8-flash-low", "label": "Flash 3.8 Low"}]

        async def _fake_cancel(cid: str) -> bool:
            cancel_triggered.set()
            return True

        async def _fake_stream(**_kwargs: Any):
            yield {"type": "text_delta", "delta": "Начинаю длинный голосовой ответ сейчас. ", "seq": 1}
            await asyncio.wait_for(cancel_triggered.wait(), timeout=4.0)
            yield {"type": "cancelled", "content": "Начинаю длинный голосовой ответ сейчас.", "thinking": "", "tool_calls": []}

        async def _slow_synth(text: str, **_kw: Any) -> bytes:
            await asyncio.sleep(0.05)
            return f"MP3:{text}".encode("utf-8")

        main.fetch_available_models = _fake_models
        main.stream_agy_turn = _fake_stream
        main.cancel_chat_generation = _fake_cancel
        tts_service.synthesize_tts_mp3 = _slow_synth

        try:
            async with _AsgiWebSocketSession(
                main.app, "/ws", query=f"token={alice_token}"
            ) as ws_alice:
                await ws_alice.send_json(
                    {
                        "action": "send_message",
                        "chat_id": chat["id"],
                        "prompt": "Начни говорить",
                        "model": "gemini-3.8-flash-low",
                        "live_voice": True,
                        "live_session_id": "lv_barge_1",
                    }
                )
                # Wait until assistant_start / text_delta arrives, then simulate Barge-In cancel_generation
                saw_cancelled_tts = False
                sent_cancel = False
                for _ in range(30):
                    ev = await ws_alice.recv_json(timeout=4.0)
                    if ev.get("type") == "text_delta" and not sent_cancel:
                        sent_cancel = True
                        await ws_alice.send_json({"action": "cancel_generation", "chat_id": chat["id"]})
                    if ev.get("type") == "live_tts_cancelled":
                        saw_cancelled_tts = True
                        break
                self.assertTrue(saw_cancelled_tts)
        finally:
            main.fetch_available_models = orig_fetch_models
            main.stream_agy_turn = orig_stream
            main.cancel_chat_generation = orig_cancel
            tts_service.synthesize_tts_mp3 = orig_synth

    async def test_ws_voice_continuous_live_call_multi_utterance_and_reset(self) -> None:
        import app.auth as auth
        import app.database as database
        import app.main as main
        import app.voice as voice

        await database.init_db()
        user = await database.create_user(
            username="duplex_user",
            display_name="Duplex",
            password="pass1234",
        )
        token = auth.create_user_session_token(user["id"], user.get("session_version", 1))

        stt_counter = 0
        orig_transcribe = voice.transcribe_pcm_window

        async def _fake_transcribe(pcm: np.ndarray, *, is_live: bool = False, **_kw: Any) -> dict[str, Any]:
            nonlocal stt_counter
            if is_live:
                return {
                    "text": "промежуточный текст",
                    "engine": "gigaam",
                    "model": "gigaam-v3-e2e-ctc",
                    "native_punct": True,
                    "latency_ms": 12,
                }
            stt_counter += 1
            return {
                "text": f"Первая фраза номер {stt_counter}.",
                "engine": "gigaam",
                "model": "gigaam-v3-e2e-rnnt",
                "native_punct": True,
                "latency_ms": 25,
            }

        voice.transcribe_pcm_window = _fake_transcribe
        try:
            async with _AsgiWebSocketSession(
                main.app, "/ws/voice", query=f"token={token}"
            ) as vws:
                self.assertTrue(vws.accepted)
                await vws.send_json(
                    {
                        "action": "start",
                        "live_call": True,
                        "sample_rate": 16000,
                        "language": "ru",
                        "engine": "gigaam",
                        "smart_format": True,
                    }
                )

                ev1 = await vws.recv_json(timeout=3.0)
                ev2 = await vws.recv_json(timeout=3.0)
                types_init = {ev1.get("type"), ev2.get("type")}
                self.assertIn("ready", types_init)
                self.assertIn("live_call_ready", types_init)

                # Helper to build 16 kHz int16 PCM frames
                loud_200ms = (np.ones(3200, dtype=np.float32) * 0.12 * 32767).astype(np.int16).tobytes()
                silent_200ms = np.zeros(3200, dtype=np.int16).tobytes()

                # Utterance #1: 600ms speech (3x200ms) + 800ms silence (4x200ms >= 0.78s)
                for _ in range(3):
                    await vws.send_bytes(loud_200ms)
                for _ in range(4):
                    await vws.send_bytes(silent_200ms)

                utt1 = None
                for _ in range(15):
                    ev = await vws.recv_json(timeout=3.0)
                    if ev.get("type") == "live_utterance":
                        utt1 = ev
                        break
                self.assertIsNotNone(utt1)
                self.assertIn("Первая фраза номер 1", utt1["text"])

                # Test mid-call reset_buffer: send 400ms speech, then reset_buffer, then silence -> NO utterance emitted
                await vws.send_bytes(loud_200ms)
                await vws.send_bytes(loud_200ms)
                await vws.send_json({"action": "reset_buffer"})
                got_reset = False
                for _ in range(10):
                    ev = await vws.recv_json(timeout=3.0)
                    if ev.get("type") == "buffer_reset":
                        got_reset = True
                        break
                self.assertTrue(got_reset)

                # Utterance #2 on the SAME open WebSocket via explicit `flush_utterance`
                await vws.send_bytes(loud_200ms)
                await vws.send_bytes(loud_200ms)
                await vws.send_json({"action": "flush_utterance"})

                utt2 = None
                for _ in range(15):
                    ev = await vws.recv_json(timeout=3.0)
                    if ev.get("type") == "live_utterance":
                        utt2 = ev
                        break
                self.assertIsNotNone(utt2)
                self.assertIn("Первая фраза номер 2", utt2["text"])
                # Confirm utterances do NOT accumulate stale prior segments across turns!
                self.assertNotIn("номер 1", utt2["text"])
        finally:
            voice.transcribe_pcm_window = orig_transcribe


if __name__ == "__main__":
    unittest.main()
