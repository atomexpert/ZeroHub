# -*- coding: utf-8 -*-
"""v139: Mobile voice input recovery — synchronous user-gesture AudioContext unlock,
1200 ms PCM-liveness watchdog, non-discardable MediaRecorder backup on empty /ws/voice final,
mobile broken Web Speech API (`live`) auto-fallback to server ASR, PyAV WebM-Opus / MP4-AAC
decoding, and cold-start HuggingFace download routing via proxy_router."""
from __future__ import annotations

import ast
import io
import json
import os
import py_compile
import re
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import av
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_audit_fixes_v128 as isolated
import test_smart_hq_return_v115 as hq_iso

ROOT_DIR = Path(__file__).resolve().parents[1]


async def _post_asgi_multipart(
    app: Any,
    path: str,
    *,
    fields: dict[str, str],
    file_field: str,
    filename: str,
    content_type: str,
    file_bytes: bytes,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    boundary = f"----AgyTestBoundary{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode("utf-8"))
        parts.append(f"{v}\r\n".encode("utf-8"))
    parts.append(f"--{boundary}\r\n".encode("utf-8"))
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    req_headers: list[tuple[bytes, bytes]] = [
        (b"host", b"127.0.0.1:8765"),
        (b"content-type", f"multipart/form-data; boundary={boundary}".encode("ascii")),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    for hk, hv in (headers or {}).items():
        req_headers.append((hk.lower().encode("latin-1"), hv.encode("latin-1")))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": req_headers,
        "client": ("127.0.0.1", 52888),
        "server": ("127.0.0.1", 8765),
    }
    sent = False
    status_code = 500
    resp_chunks: list[bytes] = []

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = int(message["status"])
        elif message["type"] == "http.response.body":
            resp_chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    raw = b"".join(resp_chunks).decode("utf-8", errors="replace")
    return status_code, (json.loads(raw) if raw.strip() else {})


def _encode_pyav_test_audio(pcm_i16: np.ndarray, fmt: str, codec_name: str, sample_rate: int = 48000) -> bytes:
    bio = io.BytesIO()
    with av.open(bio, mode="w", format=fmt) as out:
        stream = out.add_stream(codec_name, rate=sample_rate)
        stream.layout = "mono"
        frame_size = stream.codec_context.frame_size or 960
        for idx in range(0, len(pcm_i16), frame_size):
            chunk = pcm_i16[idx : idx + frame_size]
            if len(chunk) < frame_size:
                chunk = np.pad(chunk, (0, frame_size - len(chunk)))
            af = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
            af.sample_rate = sample_rate
            for pkt in stream.encode(af):
                out.mux(pkt)
        for pkt in stream.encode(None):
            out.mux(pkt)
    return bio.getvalue()


class MobileVoiceRecoveryV139Tests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(isolated.AuditFixesV128Tests.setUpClass.__func__)
    tearDownClass = classmethod(isolated.AuditFixesV128Tests.tearDownClass.__func__)
    asyncSetUp = isolated.AuditFixesV128Tests.asyncSetUp
    asyncTearDown = isolated.AuditFixesV128Tests.asyncTearDown
    _assert_live_db_untouched = isolated.AuditFixesV128Tests._assert_live_db_untouched

    async def test_01_backend_mobile_containers_pyav_decode_and_transcribe_endpoint(self):
        from app import auth, voice

        sr_in = 48000
        t = np.linspace(0, 1.25, int(sr_in * 1.25), endpoint=False, dtype=np.float32)
        tone = (0.36 * np.sin(2 * np.pi * 440.0 * t) + 0.16 * np.sin(2 * np.pi * 880.0 * t)).astype(np.float32)
        pcm_i16 = (np.clip(tone, -1.0, 1.0) * 32767.0).astype(np.int16)

        wav_bytes = voice.pcm_float32_to_wav_bytes(tone[::3], sample_rate=16000)
        webm_opus_bytes = _encode_pyav_test_audio(pcm_i16, "webm", "libopus", sr_in)
        mp4_aac_bytes = _encode_pyav_test_audio(pcm_i16, "mp4", "aac", sr_in)

        for label, payload in (
            ("WAV", wav_bytes),
            ("WebM-Opus", webm_opus_bytes),
            ("MP4-AAC", mp4_aac_bytes),
        ):
            arr, dur = voice.decode_audio_bytes_to_16k_float32(payload)
            rms = float(np.sqrt(np.mean(np.square(arr)))) if arr.size else 0.0
            self.assertGreater(dur, 1.15, f"{label} duration too short: {dur}")
            self.assertLess(dur, 1.38, f"{label} duration unexpected: {dur}")
            self.assertGreater(rms, 0.20, f"{label} RMS too low: {rms}")

        # Truncated trailing packet in a live WebM-Opus MediaRecorder snapshot must still decode valid frames
        truncated_webm = webm_opus_bytes[:-14]
        arr_trunc, dur_trunc = voice.decode_audio_bytes_to_16k_float32(truncated_webm)
        self.assertGreater(dur_trunc, 0.90)
        self.assertGreater(float(np.sqrt(np.mean(np.square(arr_trunc)))), 0.18)

        # Authenticate admin user in isolated temp DB and test POST /api/voice/transcribe on mobile containers
        admin_user = await self.database.get_user_by_username("admin")
        self.assertIsNotNone(admin_user)
        token = auth.create_user_session_token(
            admin_user["id"], int(admin_user.get("session_version") or 1)
        )
        headers = {"Authorization": f"Bearer {token}"}

        with (
            patch.object(voice, "_has_onnx_asr", return_value=True),
            patch.object(voice, "is_gigaam_cached", return_value=True),
            patch.object(
                voice,
                "_transcribe_gigaam_pcm_sync",
                side_effect=lambda pcm, model_id, proxy_url=voice._UNSET_PROXY: (
                    f"проверка голоса в антигравити хаб ({int(round(len(pcm) / 16000.0 * 100))}cs)"
                ),
            ) as mock_giga,
        ):
            # 1) Android Chrome audio/webm;codecs=opus backup blob
            st_webm, j_webm = await _post_asgi_multipart(
                self.main_mod.app,
                "/api/voice/transcribe",
                headers=headers,
                fields={
                    "language": "ru",
                    "engine": "gigaam",
                    "model": "accurate",
                    "smart_format": "true",
                    "project_name": "Antigravity_Hub",
                },
                file_field="audio",
                filename="voice.webm",
                content_type="audio/webm;codecs=opus",
                file_bytes=webm_opus_bytes,
            )
            self.assertEqual(st_webm, 200, j_webm)
            self.assertEqual(j_webm["engine"], "gigaam")
            self.assertIn("Проверка голоса в Antigravity Hub", j_webm["text"])

            # 2) iOS Safari / mobile audio/mp4 AAC blob (even if legacy engine="live" was passed)
            st_mp4, j_mp4 = await _post_asgi_multipart(
                self.main_mod.app,
                "/api/voice/transcribe",
                headers=headers,
                fields={
                    "language": "ru",
                    "engine": "live",
                    "model": "accurate",
                    "smart_format": "true",
                },
                file_field="audio",
                filename="voice.m4a",
                content_type="audio/mp4",
                file_bytes=mp4_aac_bytes,
            )
            self.assertEqual(st_mp4, 200, j_mp4)
            self.assertEqual(j_mp4["engine"], "gigaam")
            self.assertIn("Проверка голоса в Antigravity Hub", j_mp4["text"])
            self.assertEqual(mock_giga.call_count, 2)

        self._assert_live_db_untouched()

    async def test_02_backend_cold_start_hf_download_uses_proxy_router_global_route(self):
        from app import proxy_router, voice

        sr = 16000
        t = np.linspace(0, 0.8, int(sr * 0.8), endpoint=False, dtype=np.float32)
        wav_bytes = voice.pcm_float32_to_wav_bytes(
            (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32),
            sample_rate=sr,
        )

        observed_env_proxies: list[tuple[str | None, str | None]] = []

        class FakeAsrModel:
            def recognize(self, pcm: np.ndarray, sample_rate: int = 16000) -> str:
                return "скачано через прокси"

        fake_onnx_mod = MagicMock()

        def _fake_load_model(*_args, **_kwargs):
            observed_env_proxies.append(
                (os.environ.get("HTTP_PROXY"), os.environ.get("HTTPS_PROXY"))
            )
            return FakeAsrModel()

        fake_onnx_mod.load_model.side_effect = _fake_load_model
        saved_http = os.environ.get("HTTP_PROXY")
        saved_https = os.environ.get("HTTPS_PROXY")

        try:
            with (
                patch.dict(sys.modules, {"onnx_asr": fake_onnx_mod}),
                patch.dict(voice._loaded_gigaam_models, {}, clear=True),
                patch.object(voice, "_has_onnx_asr", return_value=True),
                patch.object(voice, "is_gigaam_cached", return_value=False),
                patch.object(voice, "_build_ort_session_options", return_value=object()),
                patch.object(
                    proxy_router,
                    "ensure_proxy_for_context",
                    new=AsyncMock(return_value="http://127.0.0.1:19888"),
                ) as mock_ensure_proxy,
            ):
                res = await voice.transcribe_audio(
                    wav_bytes,
                    language="ru",
                    engine="gigaam",
                    model_name="accurate",
                )
                self.assertEqual(res["text"], "скачано через прокси")
                mock_ensure_proxy.assert_awaited_once_with("global")
                self.assertEqual(
                    observed_env_proxies,
                    [("http://127.0.0.1:19888", "http://127.0.0.1:19888")],
                )

            # Environment must be cleanly restored after _scoped_hf_proxy_env exits
            self.assertEqual(os.environ.get("HTTP_PROXY"), saved_http)
            self.assertEqual(os.environ.get("HTTPS_PROXY"), saved_https)

            # When model IS cached, ensure_proxy_for_context must NOT be called (offline-safe)
            with (
                patch.object(voice, "_has_onnx_asr", return_value=True),
                patch.object(voice, "is_gigaam_cached", return_value=True),
                patch.object(voice, "_transcribe_gigaam_pcm_sync", return_value="локальный кэш"),
                patch.object(
                    proxy_router,
                    "ensure_proxy_for_context",
                    new=AsyncMock(return_value="http://127.0.0.1:19888"),
                ) as mock_ensure_cached,
            ):
                res_cached = await voice.transcribe_audio(
                    wav_bytes,
                    language="ru",
                    engine="gigaam",
                    model_name="accurate",
                )
                self.assertEqual(res_cached["text"], "локальный кэш")
                mock_ensure_cached.assert_not_awaited()
        finally:
            if saved_http is None:
                os.environ.pop("HTTP_PROXY", None)
            else:
                os.environ["HTTP_PROXY"] = saved_http
            if saved_https is None:
                os.environ.pop("HTTPS_PROXY", None)
            else:
                os.environ["HTTPS_PROXY"] = saved_https

        self._assert_live_db_untouched()

    def test_03_frontend_mobile_voice_recovery_node_vm_e2e(self):
        static_dir = ROOT_DIR / "app" / "static"
        index_html = (static_dir / "index.html").read_text(encoding="utf-8")
        sw_js = (static_dir / "sw.js").read_text(encoding="utf-8")

        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_js)
        self.assertIsNotNone(m_sw)
        ver = int(m_sw.group(1))  # type: ignore[union-attr]
        self.assertGreaterEqual(ver, 139)
        self.assertIn(f'/static/styles.css?v={ver}"', index_html)
        self.assertIn(f'/static/app.js?v={ver}"', index_html)
        self.assertIn(f'"/static/styles.css?v={ver}"', sw_js)
        self.assertIn(f'"/static/app.js?v={ver}"', sw_js)

        for rel_py in ("app/voice.py", "tests/test_mobile_voice_recovery_v139.py"):
            py_compile.compile(str(ROOT_DIR / rel_py), doraise=True)
        for rel_js in ("app/static/app.js", "app/static/sw.js"):
            chk = subprocess.run(
                ["node", "--check", str(ROOT_DIR / rel_js)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                **hq_iso._subprocess_hidden_kwargs(),
            )
            self.assertEqual(chk.returncode, 0, chk.stderr)
        for rel in (
            "app/voice.py",
            "app/static/app.js",
            "app/static/index.html",
            "app/static/sw.js",
            "tests/test_mobile_voice_recovery_v139.py",
        ):
            raw = (ROOT_DIR / rel).read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{rel} has UTF-8 BOM")
            raw.decode("utf-8")

        tree = ast.parse(Path(hq_iso.__file__).read_text(encoding="utf-8"))
        base_harness = next(
            n.value.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "node_harness" for t in n.targets)
        )
        # Cut off v115's assertions after `assert.ok(api, ...);` so we reuse its clean VM bootstrap
        cut_marker = 'assert.ok(api, "__AGY_ADVISOR_API__ must be exported");'
        self.assertIn(cut_marker, base_harness)
        vm_bootstrap = base_harness.split(cut_marker, 1)[0] + cut_marker + "\n"

        mobile_test_js = (
            vm_bootstrap
            + r"""
(async () => {
  const eventsLog = [];
  let fakeNow = 1795080000000;
  sandbox.Date.now = () => fakeNow;
  sandbox.performance.now = () => fakeNow;

  // Controlled timer & rAF scheduler for deterministic mobile timing tests
  let nextTimerId = 1;
  const timers = new Map();
  sandbox.setTimeout = (fn, ms = 0) => {
    const id = nextTimerId++;
    timers.set(id, { id, fn, due: fakeNow + Math.max(0, Number(ms) || 0), interval: null });
    return id;
  };
  sandbox.clearTimeout = (id) => {
    timers.delete(id);
  };
  sandbox.setInterval = (fn, ms = 0) => {
    const id = nextTimerId++;
    const step = Math.max(10, Number(ms) || 10);
    timers.set(id, { id, fn, due: fakeNow + step, interval: step });
    return id;
  };
  sandbox.clearInterval = (id) => {
    timers.delete(id);
  };
  sandbox.requestAnimationFrame = (fn) => sandbox.setTimeout(() => fn(fakeNow), 16);
  sandbox.cancelAnimationFrame = (id) => sandbox.clearTimeout(id);

  async function flushMicrotasks(rounds = 8) {
    for (let i = 0; i < rounds; i++) {
      await Promise.resolve();
    }
  }

  async function advanceTime(deltaMs) {
    const target = fakeNow + deltaMs;
    while (true) {
      let earliest = null;
      for (const t of timers.values()) {
        if (t.due <= target && (!earliest || t.due < earliest.due)) {
          earliest = t;
        }
      }
      if (!earliest) break;
      fakeNow = earliest.due;
      if (earliest.interval !== null) {
        earliest.due = fakeNow + earliest.interval;
      } else {
        timers.delete(earliest.id);
      }
      earliest.fn();
      await flushMicrotasks(4);
    }
    fakeNow = target;
    await flushMicrotasks(6);
  }

  // Browser globals required by voice recording pipeline
  sandbox.isSecureContext = true;
  sandbox.AbortController = AbortController;
  sandbox.FormData = FormData;
  sandbox.Blob = Blob;
  sandbox.ArrayBuffer = ArrayBuffer;
  sandbox.Int16Array = Int16Array;
  sandbox.Float32Array = Float32Array;
  sandbox.Uint8Array = Uint8Array;
  if (!sandbox.URL.createObjectURL) {
    sandbox.URL.createObjectURL = () => "blob:mock-worklet";
  }
  sandbox.getComputedStyle = () => ({ getPropertyValue: () => "#6dc392" });
  const fake2dCtx = {
    fillStyle: "",
    globalAlpha: 1,
    setTransform: () => {},
    clearRect: () => {},
    beginPath: () => {},
    roundRect: () => {},
    rect: () => {},
    fill: () => {},
  };
  for (const cEl of [api.els.voiceWaveCanvas, api.els.liveVoiceWaveCanvas]) {
    if (cEl) {
      cEl.clientWidth = 220;
      cEl.clientHeight = 28;
      cEl.getContext = () => fake2dCtx;
    }
  }
  sandbox.navigator.userAgent =
    "Mozilla/5.0 (Linux; Android 14; RMX3840) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36";
  sandbox.navigator.maxTouchPoints = 5;

  let getUserMediaResolved = false;
  let audioCtxForceSuspended = true;
  const createdAudioContexts = [];
  const createdRecorders = [];
  const createdVoiceSockets = [];
  const transcribeRequests = [];
  let nextTranscribeReplyText = "Распознанный текст с телефона.";

  sandbox.navigator.mediaDevices = {
    enumerateDevices: async () => [],
    getUserMedia: async () => {
      eventsLog.push("getUserMedia:start");
      await Promise.resolve();
      await Promise.resolve();
      getUserMediaResolved = true;
      eventsLog.push("getUserMedia:resolved");
      return {
        getTracks: () => [
          {
            stop: () => {
              eventsLog.push("track:stop");
            },
          },
        ],
      };
    },
  };

  class FakeAudioWorkletNode {
    constructor() {
      this.port = {
        onmessage: null,
        postMessage: () => {},
      };
    }
    connect() {}
    disconnect() {}
  }
  sandbox.AudioWorkletNode = FakeAudioWorkletNode;

  class FakeAudioContext {
    constructor() {
      this.sampleRate = 48000;
      this.state = "suspended";
      this.closed = false;
      this.resumeCalls = 0;
      this.createdBeforeGumResolved = !getUserMediaResolved;
      this.destination = {};
      this.audioWorklet = {
        addModule: async () => {},
      };
      eventsLog.push(`AudioContext:new(beforeGum=${this.createdBeforeGumResolved})`);
      createdAudioContexts.push(this);
    }
    resume() {
      this.resumeCalls += 1;
      eventsLog.push(`AudioContext:resume(beforeGum=${!getUserMediaResolved})`);
      if (!audioCtxForceSuspended) {
        this.state = "running";
      }
      return Promise.resolve();
    }
    close() {
      this.closed = true;
      this.state = "closed";
      eventsLog.push("AudioContext:close");
      return Promise.resolve();
    }
    createGain() {
      return { gain: { value: 1 }, connect: () => {} };
    }
    createMediaStreamSource() {
      return { connect: () => {} };
    }
    createAnalyser() {
      return {
        fftSize: 256,
        frequencyBinCount: 128,
        smoothingTimeConstant: 0.55,
        getByteTimeDomainData: (buf) => buf.fill(128),
        getByteFrequencyData: (buf) => buf.fill(0),
        connect: () => {},
      };
    }
  }
  sandbox.AudioContext = FakeAudioContext;

  class FakeMediaRecorder {
    static isTypeSupported(mime) {
      return mime === "audio/webm;codecs=opus" || mime === "audio/mp4";
    }
    constructor(stream, opts = {}) {
      this.stream = stream;
      this.mimeType = opts.mimeType || "audio/webm;codecs=opus";
      this.state = "inactive";
      this.ondataavailable = null;
      this.onstop = null;
      this._PendingTailBytes = 380;
      createdRecorders.push(this);
    }
    start() {
      this.state = "recording";
      eventsLog.push("MediaRecorder:start");
    }
    emitChunk(byteLen) {
      if (this.state !== "recording") return;
      const chunk = new Blob([new Uint8Array(byteLen).fill(77)], { type: this.mimeType });
      if (typeof this.ondataavailable === "function") {
        this.ondataavailable({ data: chunk });
      }
    }
    requestData() {
      if (this.state === "recording" && this._PendingTailBytes > 0) {
        const n = this._PendingTailBytes;
        this._PendingTailBytes = 0;
        this.emitChunk(n);
      }
    }
    stop() {
      if (this.state === "inactive") return;
      this.state = "inactive";
      eventsLog.push("MediaRecorder:stop");
      const tailN = this._PendingTailBytes;
      this._PendingTailBytes = 0;
      sandbox.setTimeout(() => {
        if (tailN > 0 && typeof this.ondataavailable === "function") {
          const tail = new Blob([new Uint8Array(tailN).fill(88)], { type: this.mimeType });
          this.ondataavailable({ data: tail });
        }
        if (typeof this.onstop === "function") {
          this.onstop({});
        }
      }, 15);
    }
  }
  sandbox.MediaRecorder = FakeMediaRecorder;

  class FakeVoiceWebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.binaryType = "blob";
      this.sent = [];
      this.onopen = null;
      this.onmessage = null;
      this.onerror = null;
      this.onclose = null;
      createdVoiceSockets.push(this);
      sandbox.setTimeout(() => {
        if (this.readyState !== 0) return;
        this.readyState = FakeVoiceWebSocket.OPEN;
        if (typeof this.onopen === "function") this.onopen({});
      }, 10);
    }
    send(payload) {
      this.sent.push(payload);
      if (typeof payload === "string") {
        try {
          const parsed = JSON.parse(payload);
          if (parsed.type === "finish") {
            // Simulate 0 PCM frames received on server -> immediate empty `final`!
            sandbox.setTimeout(() => {
              if (this.readyState === FakeVoiceWebSocket.OPEN && typeof this.onmessage === "function") {
                this.onmessage({
                  data: JSON.stringify({
                    type: "final",
                    text: "",
                    duration_sec: 0,
                    latency_ms: 1,
                    model: "gigaam-v3-e2e-rnnt",
                  }),
                });
              }
            }, 5);
          }
        } catch {}
      }
    }
    close() {
      this.readyState = 3;
    }
  }
  FakeVoiceWebSocket.OPEN = 1;
  sandbox.WebSocket = FakeVoiceWebSocket;

  sandbox.fetch = async (url, options = {}) => {
    const u = String(url);
    if (u === "/api/voice/transcribe") {
      const fd = options.body;
      const audioFile = fd.get("audio");
      const isLive = fd.get("live_preview") === "true";
      const reqInfo = {
        engine: fd.get("engine"),
        model: fd.get("model"),
        livePreview: isLive,
        size: audioFile ? audioFile.size : 0,
      };
      transcribeRequests.push(reqInfo);
      const reply = isLive
        ? { text: "Черновик с телефона", engine: "gigaam", model: "gigaam-v3-e2e-ctc", duration_sec: 1.4 }
        : { text: nextTranscribeReplyText, engine: "gigaam", model: "gigaam-v3-e2e-rnnt", duration_sec: 1.8 };
      return {
        ok: true,
        status: 200,
        headers: { get: (h) => (String(h).toLowerCase() === "content-type" ? "application/json" : "") },
        json: async () => reply,
        text: async () => JSON.stringify(reply),
      };
    }
    return {
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({}),
      text: async () => "{}",
    };
  };

  assert.equal(api.isMobileSpeechDevice(), true, "Android UA must be detected as mobile speech device");
  api.state.token = "mob_tok_v139";
  api.state.voice.engine = "gigaam";
  api.state.voice.model = "accurate";
  api.state.voice.lang = "ru";
  api.els.promptInput.value = "";

  // =========================================================================
  // SCENARIO A + B:
  // 1) AudioContext + resume() called synchronously BEFORE getUserMedia resolves;
  // 2) startTime initialized without NaN before rAF ticks;
  // 3) AudioContext stays suspended (0 PCM frames in /ws/voice, empty `final` on Done at 850ms),
  //    MediaRecorder recorded chunks -> client preserves backupBlob, POSTs to /api/voice/transcribe,
  //    and populates promptInput!
  // =========================================================================
  getUserMediaResolved = false;
  audioCtxForceSuspended = true;
  eventsLog.length = 0;
  transcribeRequests.length = 0;
  nextTranscribeReplyText = "Привет с телефона через резервный MediaRecorder.";

  const togglePromise = api.toggleVoiceInput();
  // Immediately after synchronous call frame (BEFORE awaiting togglePromise / getUserMedia!):
  assert.equal(createdAudioContexts.length, 1, "AudioContext must be created synchronously inside tap");
  assert.equal(createdAudioContexts[0].createdBeforeGumResolved, true);
  assert.ok(createdAudioContexts[0].resumeCalls >= 1, "ctx.resume() must fire synchronously in user gesture");
  assert.equal(
    eventsLog[0],
    "AudioContext:new(beforeGum=true)",
    `Unexpected order: ${eventsLog.join(" -> ")}`
  );
  assert.equal(eventsLog[1], "AudioContext:resume(beforeGum=true)");

  await togglePromise;
  assert.equal(api.state.voice.recording, true);
  assert.ok(Number.isFinite(api.state.voice.startTime) && api.state.voice.startTime > 0);
  await advanceTime(50); // WS opens, rAF ticks run without NaN
  assert.equal(api.state.voice.wsConnected, true);
  assert.equal(api.state.voice.pcmChunksCount, 0);

  // MediaRecorder emits 3 chunks (3 * 350B = 1050B + 380B tail on stop = 1430B)
  const rec1 = createdRecorders[createdRecorders.length - 1];
  rec1.emitChunk(350);
  await advanceTime(350);
  rec1.emitChunk(350);
  await advanceTime(350);
  rec1.emitChunk(350);
  await advanceTime(100); // Total elapsed ~850ms (< 1200ms watchdog)

  // User taps "✓ Готово": /ws/voice returns `{type: "final", text: "", duration_sec: 0}`
  api.finishVoiceRecording();
  await advanceTime(80);
  await flushMicrotasks(12);

  assert.equal(
    transcribeRequests.length,
    1,
    "Empty /ws/voice final MUST send backupBlob to /api/voice/transcribe instead of wiping audioChunks!"
  );
  assert.equal(transcribeRequests[0].engine, "gigaam");
  assert.equal(transcribeRequests[0].livePreview, false);
  assert.equal(
    transcribeRequests[0].size,
    1430,
    "backupBlob must include all chunks plus flushed tail!"
  );
  assert.equal(api.els.promptInput.value, "Привет с телефона через резервный MediaRecorder.");
  assert.equal(api.state.voice.recording, false);
  assert.equal(api.state.voice.transcribing, false);

  // =========================================================================
  // SCENARIO C:
  // Recording > 1200 ms with suspended AudioContext -> 1200 ms PCM-liveness watchdog
  // closes dead AudioContext mid-stream, switches to MediaRecorder + live chunk preview,
  // and finalizes via /api/voice/transcribe!
  // =========================================================================
  api.els.promptInput.value = "";
  transcribeRequests.length = 0;
  getUserMediaResolved = false;
  audioCtxForceSuspended = true;
  nextTranscribeReplyText = "Длинная фраза после срабатывания PCM-вотчдога.";

  await api.toggleVoiceInput();
  const ctxC = createdAudioContexts[createdAudioContexts.length - 1];
  const recC = createdRecorders[createdRecorders.length - 1];
  await advanceTime(250);
  recC.emitChunk(520);
  await advanceTime(500);
  recC.emitChunk(520);

  // Cross 1200 ms watchdog threshold
  await advanceTime(550);
  assert.equal(ctxC.closed, true, "1200ms PCM watchdog must close dead AudioContext to free Android mic");
  assert.equal(api.state.voice.audioCtx, null);
  assert.equal(api.state.voice.activeMode, "whisper", "Must seamlessly switch activeMode to whisper/HTTP");
  assert.equal(api.state.voice.recording, true, "Recording must continue seamlessly");

  // Allow one HTTP live preview tick (~420ms)
  await advanceTime(500);
  assert.ok(
    transcribeRequests.some((r) => r.livePreview === true),
    "Chunk preview loop must send live_preview=true request"
  );
  assert.equal(api.els.promptInput.value, "Черновик с телефона");

  // User taps "✓ Готово"
  api.finishVoiceRecording();
  await advanceTime(80);
  await flushMicrotasks(12);
  assert.equal(api.els.promptInput.value, "Длинная фраза после срабатывания PCM-вотчдога.");

  // =========================================================================
  // SCENARIO D:
  // Legacy `localStorage` mobile `engine = "live"` (`webkitSpeechRecognition` fails with
  // `network` OR `rapidFails >= 2`) -> auto-switches to server GigaAM/Whisper!
  // =========================================================================
  let srBehavior = "network_error";
  class FakeSpeechRecognition {
    constructor() {
      this.lang = "ru-RU";
      this.continuous = true;
      this.interimResults = true;
      this.onstart = null;
      this.onresult = null;
      this.onerror = null;
      this.onend = null;
    }
    start() {
      if (srBehavior === "network_error") {
        sandbox.setTimeout(() => {
          if (typeof this.onerror === "function") this.onerror({ error: "network" });
        }, 40);
      } else if (srBehavior === "rapid_empty_end") {
        sandbox.setTimeout(() => {
          if (typeof this.onstart === "function") this.onstart({});
        }, 10);
        sandbox.setTimeout(() => {
          if (typeof this.onend === "function") this.onend({});
        }, 120);
      }
    }
    stop() {
      if (typeof this.onend === "function") this.onend({});
    }
    abort() {}
  }
  sandbox.webkitSpeechRecognition = FakeSpeechRecognition;

  // D1: `network` error when `v.engine === "live"` on mobile
  api.els.promptInput.value = "";
  transcribeRequests.length = 0;
  api.state.voice.engine = "live";
  api.state.voice.liveBlockedThisSession = false;
  nextTranscribeReplyText = "Автопереключение с webkitSpeechRecognition.";

  await api.toggleVoiceInput();
  assert.equal(api.state.voice.activeMode, "live");
  await advanceTime(80);
  await flushMicrotasks(10);
  assert.equal(api.state.voice.liveBlockedThisSession, true);
  assert.equal(api.state.voice.activeMode, "whisper");

  const recD1 = createdRecorders[createdRecorders.length - 1];
  recD1.emitChunk(700);
  await advanceTime(600);
  api.finishVoiceRecording();
  await advanceTime(80);
  await flushMicrotasks(12);
  assert.equal(api.els.promptInput.value, "Автопереключение с webkitSpeechRecognition.");
  assert.equal(
    transcribeRequests[transcribeRequests.length - 1].engine,
    "gigaam",
    "Server ASR engine must normalize legacy 'live' to 'gigaam'"
  );

  // D2: `rapidFails >= 2` silent closes on mobile even when `onstart` fired
  srBehavior = "rapid_empty_end";
  api.els.promptInput.value = "";
  api.state.voice.engine = "live";
  api.state.voice.liveBlockedThisSession = false;
  await api.toggleVoiceInput();
  await advanceTime(500);
  await flushMicrotasks(10);
  assert.equal(api.state.voice.liveBlockedThisSession, true, "rapidFails >= 2 must block broken live mode");
  assert.equal(api.state.voice.activeMode, "whisper", "rapidFails >= 2 must switch to server recording");
  api.cancelVoiceRecording();

  // D3: Live Voice Call also unlocks BOTH playbackCtx & micAudioCtx synchronously before getUserMedia resolves!
  getUserMediaResolved = false;
  const ctxCountBeforeCall = createdAudioContexts.length;
  api.state.activeChatId = "chat_live_mob";
  api.state.activeChat = { id: "chat_live_mob", kind: "chat", title: "Тест звонка" };
  const liveCallPromise = api.startLiveVoiceCall();
  const createdDuringTap = createdAudioContexts.slice(ctxCountBeforeCall);
  assert.equal(
    createdDuringTap.length,
    2,
    "startLiveVoiceCall must unlock both playbackCtx and micAudioCtx synchronously in user gesture!"
  );
  assert.ok(createdDuringTap.every((c) => c.createdBeforeGumResolved === true));
  await liveCallPromise;
  api.stopLiveVoiceCall({ silent: true });

  console.log("V139_MOBILE_VOICE_RECOVERY_OK");
  process.exit(0);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
        )

        vm_script = self.tmp_root / "v139_mobile_voice_vm.js"
        vm_script.write_text(mobile_test_js, encoding="utf-8")
        vm_res = subprocess.run(
            ["node", str(vm_script), str(static_dir / "app.js")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=25,
            **hq_iso._subprocess_hidden_kwargs(),
        )
        self.assertEqual(
            vm_res.returncode,
            0,
            f"STDOUT:\n{vm_res.stdout}\nSTDERR:\n{vm_res.stderr}",
        )
        self.assertIn("V139_MOBILE_VOICE_RECOVERY_OK", vm_res.stdout)
        self._assert_live_db_untouched()


if __name__ == "__main__":
    unittest.main()
