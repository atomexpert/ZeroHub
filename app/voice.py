from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Iterator, Optional

# Strictly isolate voice ASR workers from any GPU / CUDA / DirectML providers (0% GPU / 0 MB VRAM)
os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ.setdefault("ORT_DISABLE_ALL_GPU", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from . import proxy_router
from .config import (
    GIGAAM_MODELS_DIR,
    GROQ_API_KEY,
    VOICE_CPU_THREADS,
    VOICE_DEVICE,
    VOICE_MODEL_DEFAULT,
    VOICE_PRIMARY_ENGINE,
    WHISPER_MODELS_DIR,
    resolve_outbound_proxy,
)

# Direct HuggingFace Hub downloads for onnx_asr into data/models/gigaam
os.environ["HF_HUB_CACHE"] = str(GIGAAM_MODELS_DIR)
os.environ["HUGGINGFACE_HUB_CACHE"] = str(GIGAAM_MODELS_DIR)

import av
import numpy as np

logger = logging.getLogger("agy_hub.voice")

ALLOWED_MODELS = ("small", "base", "tiny")
LIVE_PREVIEW_FAST_MODEL = "base"

GIGAAM_FAST_MODEL = "gigaam-v3-e2e-ctc"
GIGAAM_ACCURATE_MODEL = "gigaam-v3-e2e-rnnt"
ALLOWED_GIGAAM_MODELS = (GIGAAM_ACCURATE_MODEL, GIGAAM_FAST_MODEL)

_model_lock = threading.Lock()
_gigaam_lock = threading.Lock()
_preview_cancel_lock = threading.Lock()
_active_preview_cancel: Optional[threading.Event] = None

# Dedicated 2-worker ASR pool so voice inference never competes with SQLite/file I/O
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="voice-asr")

_loaded_models: dict[str, Any] = {}
_loaded_gigaam_models: dict[str, Any] = {}
_active_model_name: str = GIGAAM_ACCURATE_MODEL
_loading_state: dict[str, Any] = {
    "status": "idle",  # idle | loading | ready | error
    "model": _active_model_name,
    "error": "",
}


def _has_onnx_asr() -> bool:
    return importlib.util.find_spec("onnx_asr") is not None


_UNSET_PROXY = object()
_PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy")


@contextmanager
def _scoped_hf_proxy_env(proxy_url: Any = _UNSET_PROXY) -> Iterator[Optional[str]]:
    """Apply active global outbound proxy route strictly for the duration of an uncached HuggingFace download."""
    saved = {k: os.environ.get(k) for k in _PROXY_ENV_VARS}
    proxy = (
        proxy_router.resolve_proxy_sync("global")
        if proxy_url is _UNSET_PROXY
        else proxy_url
    )
    try:
        proxy_router.apply_proxy_to_subprocess_env(os.environ, proxy)
        yield proxy or None
    finally:
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


def _ensure_hf_cache_dir() -> None:
    GIGAAM_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HUB_CACHE"] = str(GIGAAM_MODELS_DIR)
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(GIGAAM_MODELS_DIR)
    try:
        import huggingface_hub.constants as hf_const

        hf_const.HF_HUB_CACHE = str(GIGAAM_MODELS_DIR)
    except Exception:
        pass


def _build_ort_session_options() -> Any:
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.intra_op_num_threads = int(VOICE_CPU_THREADS)
    so.inter_op_num_threads = 1
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return so


def is_model_cached(model_name: str) -> bool:
    if model_name in ALLOWED_GIGAAM_MODELS:
        return is_gigaam_cached(model_name)
    if model_name not in ALLOWED_MODELS:
        return False
    repo_dir = WHISPER_MODELS_DIR / f"models--Systran--faster-whisper-{model_name}"
    return repo_dir.exists() and any(repo_dir.rglob("model.bin"))


def is_gigaam_cached(model_name: str = GIGAAM_ACCURATE_MODEL) -> bool:
    repo_dir = GIGAAM_MODELS_DIR / "models--istupakov--gigaam-v3-onnx"
    if not repo_dir.exists():
        return False
    prefix = "v3_e2e_ctc" if "ctc" in model_name else "v3_e2e_rnnt"
    return any(prefix in p.name for p in repo_dir.rglob("*.onnx"))


def _pick_live_preview_model(requested_model: str) -> str:
    req = requested_model if requested_model in ALLOWED_MODELS else "small"
    if req == "small":
        if "base" in _loaded_models or is_model_cached("base"):
            return "base"
        if "tiny" in _loaded_models or is_model_cached("tiny"):
            return "tiny"
        return LIVE_PREVIEW_FAST_MODEL
    return req


def get_voice_status() -> dict[str, Any]:
    onnx_ok = _has_onnx_asr()
    engines: list[str] = []
    if onnx_ok:
        engines.append("gigaam")
    engines.append("whisper")
    if GROQ_API_KEY:
        engines.append("groq")

    gigaam_ready = bool(_loaded_gigaam_models)
    loaded_all = list(_loaded_gigaam_models.keys()) + list(_loaded_models.keys())
    cached_all = [m for m in ALLOWED_GIGAAM_MODELS if is_gigaam_cached(m)] + [
        m for m in ALLOWED_MODELS if is_model_cached(m)
    ]
    primary = VOICE_PRIMARY_ENGINE if VOICE_PRIMARY_ENGINE in engines else (engines[0] if engines else "whisper")

    return {
        "available": True,
        "status": _loading_state["status"],
        "primary_engine": primary,
        "available_engines": engines,
        "gigaam_ready": gigaam_ready,
        "groq_configured": bool(GROQ_API_KEY),
        "active_model": _active_model_name,
        "device": "cpu",
        "cpu_threads": int(VOICE_CPU_THREADS),
        "gpu_used": False,
        "models_dir": str(WHISPER_MODELS_DIR),
        "gigaam_models_dir": str(GIGAAM_MODELS_DIR),
        "cached_models": cached_all,
        "loaded_models": loaded_all,
        "formatter_ready": True,
        "error": _loading_state["error"],
    }


def _load_gigaam_sync(model_id: str, proxy_url: Any = _UNSET_PROXY) -> Any:
    global _active_model_name
    if model_id not in ALLOWED_GIGAAM_MODELS:
        model_id = GIGAAM_ACCURATE_MODEL

    with _gigaam_lock:
        if model_id in _loaded_gigaam_models:
            return _loaded_gigaam_models[model_id]

        os.environ["CUDA_VISIBLE_DEVICES"] = ""
        _ensure_hf_cache_dir()

        import onnx_asr

        _loading_state["status"] = "loading"
        _loading_state["model"] = model_id
        _loading_state["error"] = ""

        so = _build_ort_session_options()
        providers = ["CPUExecutionProvider"]
        already_cached = is_gigaam_cached(model_id)

        t0 = time.perf_counter()
        try:
            def _do_load_gigaam() -> tuple[Any, str]:
                try:
                    m = onnx_asr.load_model(
                        model_id,
                        quantization="int8",
                        providers=providers,
                        sess_options=so,
                    )
                    return m, "int8"
                except Exception as quant_exc:
                    logger.info(
                        "GigaAM '%s' int8 load fell back to fp32/default (%s)",
                        model_id,
                        quant_exc,
                    )
                    m = onnx_asr.load_model(
                        model_id,
                        quantization=None,
                        providers=providers,
                        sess_options=so,
                    )
                    return m, "fp32"

            if already_cached:
                model, quant_used = _do_load_gigaam()
            else:
                with _scoped_hf_proxy_env(proxy_url):
                    model, quant_used = _do_load_gigaam()

            _loaded_gigaam_models[model_id] = model
            _active_model_name = model_id
            _loading_state["status"] = "ready"
            _loading_state["model"] = model_id
            logger.info(
                "GigaAM model '%s' ready (CPUExecutionProvider/%s, intra_threads=%d, inter_threads=1, gpu=0%%) in %.2fs",
                model_id,
                quant_used,
                int(VOICE_CPU_THREADS),
                time.perf_counter() - t0,
            )
            return model
        except Exception as exc:
            _loading_state["status"] = "error"
            _loading_state["error"] = str(exc)
            logger.exception("Failed to load GigaAM model '%s'", model_id)
            raise


def _load_whisper_sync(model_name: str, proxy_url: Any = _UNSET_PROXY) -> Any:
    if model_name not in ALLOWED_MODELS:
        model_name = "small"

    with _model_lock:
        if model_name in _loaded_models:
            return _loaded_models[model_name]

        os.environ["CUDA_VISIBLE_DEVICES"] = ""
        from faster_whisper import WhisperModel

        _loading_state["status"] = "loading"
        _loading_state["model"] = model_name
        _loading_state["error"] = ""

        # Strictly CPU int8 with bounded threads to protect host GPU mining
        cpu_threads = int(VOICE_CPU_THREADS)
        compute_type = "int8"

        t0 = time.perf_counter()
        try:
            if is_model_cached(model_name):
                model = WhisperModel(
                    model_name,
                    device="cpu",
                    compute_type=compute_type,
                    cpu_threads=cpu_threads,
                    download_root=str(WHISPER_MODELS_DIR),
                    local_files_only=True,
                )
            else:
                with _scoped_hf_proxy_env(proxy_url):
                    model = WhisperModel(
                        model_name,
                        device="cpu",
                        compute_type=compute_type,
                        cpu_threads=cpu_threads,
                        download_root=str(WHISPER_MODELS_DIR),
                    )
            if len(_loaded_models) >= 3:
                _loaded_models.clear()
            _loaded_models[model_name] = model
            _loading_state["status"] = "ready"
            logger.info(
                "Whisper model '%s' ready (cpu/%s, threads=%d, gpu=0%%) in %.2fs",
                model_name,
                compute_type,
                cpu_threads,
                time.perf_counter() - t0,
            )
            return model
        except Exception as exc:
            _loading_state["status"] = "error"
            _loading_state["error"] = str(exc)
            logger.exception("Failed to load Whisper model '%s'", model_name)
            raise


def resolve_voice_route(
    *,
    engine: str = "gigaam",
    language: str = "ru",
    model_name: str = "accurate",
    is_live: bool = False,
    allow_groq: bool = False,
) -> tuple[str, str]:
    """Resolve `(effective_engine, effective_model_id)` for CPU/Groq routing."""
    lang = (language or "ru").strip().lower()
    eng = (engine or VOICE_PRIMARY_ENGINE or "gigaam").strip().lower()
    m_req = (model_name or "accurate").strip().lower()

    if eng in ("whisper_only", "whisper") or lang == "en":
        if is_live or m_req in ("fast", "base", GIGAAM_FAST_MODEL, "ctc"):
            w_model = "base"
        elif m_req == "tiny":
            w_model = "tiny"
        else:
            w_model = "small"
        if is_live:
            w_model = _pick_live_preview_model(w_model)
        return ("whisper", w_model)

    if eng == "groq" and GROQ_API_KEY and allow_groq and not is_live:
        return ("groq", "whisper-large-v3-turbo")

    if _has_onnx_asr():
        if is_live or m_req in ("fast", "base", "tiny", GIGAAM_FAST_MODEL, "ctc"):
            return ("gigaam", GIGAAM_FAST_MODEL)
        return ("gigaam", GIGAAM_ACCURATE_MODEL)

    # Fallback if onnx_asr is unavailable
    w_fb = "base" if (is_live or m_req in ("fast", "base")) else "small"
    return ("whisper", w_fb)


def pcm_float32_to_wav_bytes(pcm: np.ndarray, sample_rate: int = 16000) -> bytes:
    """Encode 1D float32 [-1..1] waveform to 16-bit mono WAV bytes in memory."""
    if pcm.size == 0:
        pcm = np.zeros(160, dtype=np.float32)
    clipped = np.clip(pcm, -1.0, 1.0)
    pcm_i16 = (clipped * 32767.0).astype(np.int16)
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_i16.tobytes())
    return bio.getvalue()


def _transcribe_groq_sync(pcm: np.ndarray, language: str, proxy_url: Any = _UNSET_PROXY) -> str:
    """Send 16 kHz mono WAV in-memory to Groq whisper-large-v3-turbo via outbound proxy (timeout 6s)."""
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not configured")

    wav_bytes = pcm_float32_to_wav_bytes(pcm, sample_rate=16000)
    boundary = f"----AgyHubVoice{uuid.uuid4().hex}"
    lang_norm = (language or "").strip().lower()

    parts: list[bytes] = []

    def _add_field(name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        parts.append(f"{value}\r\n".encode("utf-8"))

    _add_field("model", "whisper-large-v3-turbo")
    _add_field("response_format", "json")
    _add_field("temperature", "0")
    if lang_norm in ("ru", "en"):
        _add_field("language", lang_norm)
    _add_field(
        "prompt",
        "Antigravity Hub, Gemini, Claude, GigaAM, FastAPI, Python, WebSocket, Docker, JavaScript.",
    )

    parts.append(f"--{boundary}\r\n".encode("utf-8"))
    parts.append(
        b'Content-Disposition: form-data; name="file"; filename="voice.wav"\r\n'
        b"Content-Type: audio/wav\r\n\r\n"
    )
    parts.append(wav_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "AntigravityHub/1.0",
        },
    )
    eff_proxy = resolve_outbound_proxy() if proxy_url is _UNSET_PROXY else proxy_url
    opener = proxy_router.build_urllib_proxy_opener(eff_proxy)
    with opener.open(req, timeout=6.0) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    payload = json.loads(raw)
    return str(payload.get("text") or "").strip()


def decode_audio_bytes_to_16k_float32(audio_bytes: bytes) -> tuple[np.ndarray, float]:
    """Decode browser WebM/Opus, MP4/AAC, OGG, or WAV bytes cleanly with PyAV."""
    if not audio_bytes:
        return np.zeros(0, dtype=np.float32), 0.0

    bio = io.BytesIO(audio_bytes)
    resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
    chunks: list[np.ndarray] = []

    with av.open(bio, mode="r") as container:
        audio_streams = [s for s in container.streams if s.type == "audio"]
        if not audio_streams:
            return np.zeros(0, dtype=np.float32), 0.0
        try:
            for frame in container.decode(audio=0):
                for out_frame in resampler.resample(frame):
                    chunks.append(out_frame.to_ndarray().reshape(-1))
        except (av.FFmpegError, EOFError):
            # Mobile MediaRecorder live snapshots (webm/opus or fragmented mp4/aac) may end on a
            # partial packet boundary; keep all decoded samples if non-empty, else re-raise.
            if not chunks:
                raise
        try:
            for out_frame in resampler.resample(None):
                chunks.append(out_frame.to_ndarray().reshape(-1))
        except (av.FFmpegError, EOFError):
            if not chunks:
                raise

    if not chunks:
        return np.zeros(0, dtype=np.float32), 0.0

    audio_np = np.concatenate(chunks).astype(np.float32)
    duration = float(len(audio_np) / 16000.0)
    return audio_np, duration


_WHISPER_HALLUCINATION_RE = re.compile(
    r"(?i)(?:субтитры\s+(?:сделал|создал|подготовил|добавил)|редактор\s+субтитров|"
    r"продолжение\s+следует|спасибо\s+за\s+просмотр|dima\s*torzok|amara\.org)"
)


def _transcribe_whisper_pcm_sync(
    audio_np: np.ndarray,
    *,
    language: str,
    chosen_model: str,
    live_preview: bool = False,
    cancel_event: Optional[threading.Event] = None,
    proxy_url: Any = _UNSET_PROXY,
) -> tuple[str, list[tuple[str, float, float]], str]:
    duration_sec = float(len(audio_np) / 16000.0)
    rms = float(np.sqrt(np.mean(np.square(audio_np)))) if audio_np.size else 0.0
    model = _load_whisper_sync(chosen_model, proxy_url=proxy_url)

    if cancel_event is not None and cancel_event.is_set():
        return "", [], (language or "ru")

    lang_norm = (language or "ru").strip().lower()
    if lang_norm in ("auto", ""):
        lang_arg: Optional[str] = None
        prompt_hint = "Antigravity Hub, FastAPI, Python, CSS, HTML, JavaScript, Docker, Git, API."
    elif lang_norm == "en":
        lang_arg = "en"
        prompt_hint = "Antigravity Hub, FastAPI, Python, CSS, HTML, JavaScript, Docker, Git, API."
    else:
        lang_arg = "ru"
        prompt_hint = "Antigravity Hub, FastAPI, Python, styles.css, index.html, app.js, WebSocket, Docker, Git, Tailscale."

    effective_prompt = None if (live_preview or duration_sec < 2.0) else prompt_hint
    use_vad = (not live_preview) or (duration_sec > 6.0)
    vad_params = {
        "threshold": 0.22,
        "min_speech_duration_ms": 80,
        "min_silence_duration_ms": 800,
        "speech_pad_ms": 350,
    }

    segments, info = model.transcribe(
        audio_np,
        language=lang_arg,
        beam_size=1 if live_preview else 2,
        best_of=1,
        temperature=0.0,
        repetition_penalty=1.2,
        no_repeat_ngram_size=3,
        max_new_tokens=64 if live_preview else 224,
        compression_ratio_threshold=2.4,
        log_prob_threshold=None,
        no_speech_threshold=0.85,
        vad_filter=use_vad,
        vad_parameters=vad_params,
        condition_on_previous_text=False,
        initial_prompt=effective_prompt,
    )

    def _collect_segments(seg_iter: Any) -> Optional[list[tuple[str, float, float]]]:
        items: list[tuple[str, float, float]] = []
        for seg in seg_iter:
            if cancel_event is not None and cancel_event.is_set():
                return None
            txt = (seg.text or "").strip()
            if not txt:
                continue
            if _WHISPER_HALLUCINATION_RE.search(txt):
                continue
            items.append(
                (
                    txt,
                    float(getattr(seg, "start", 0.0)),
                    float(getattr(seg, "end", 0.0)),
                )
            )
        return items

    seg_items = _collect_segments(segments)
    if seg_items is None:
        return "", [], (lang_arg or "ru")

    if not seg_items and rms >= 0.0018:
        if cancel_event is not None and cancel_event.is_set():
            return "", [], (lang_arg or "ru")
        segments_fb, info = model.transcribe(
            audio_np,
            language=lang_arg,
            beam_size=1,
            best_of=1,
            temperature=0.0,
            repetition_penalty=1.2,
            no_repeat_ngram_size=3,
            max_new_tokens=64 if live_preview else 224,
            compression_ratio_threshold=2.4,
            log_prob_threshold=None,
            no_speech_threshold=0.90,
            vad_filter=False,
            condition_on_previous_text=False,
            initial_prompt=None,
        )
        seg_items_fb = _collect_segments(segments_fb)
        if seg_items_fb is None:
            return "", [], (lang_arg or "ru")
        seg_items = seg_items_fb

    final_text = " ".join(it[0] for it in seg_items).strip()
    detected_lang = getattr(info, "language", lang_arg or "ru")
    return final_text, seg_items, detected_lang


def _transcribe_gigaam_pcm_sync(
    audio_np: np.ndarray,
    model_id: str,
    proxy_url: Any = _UNSET_PROXY,
) -> str:
    """Run GigaAM-v3 E2E ONNX CPU inference; chunk cleanly if clip > 22s."""
    if audio_np.size == 0:
        return ""
    asr = _load_gigaam_sync(model_id, proxy_url=proxy_url)
    pcm = np.ascontiguousarray(audio_np, dtype=np.float32)
    max_chunk_samples = 22 * 16000
    if pcm.shape[0] <= max_chunk_samples:
        res = asr.recognize(pcm, sample_rate=16000)
        return (res if isinstance(res, str) else str(res or "")).strip()

    # For long single-shot HTTP fallback clips (>22s), split on low-energy boundaries
    pieces: list[str] = []
    pos = 0
    total = pcm.shape[0]
    target_step = 18 * 16000
    while pos < total:
        end = min(total, pos + max_chunk_samples)
        if end < total:
            search_start = max(pos + 8 * 16000, min(end - 3 * 16000, pos + target_step))
            win_step = 1600  # 100 ms
            best_cut = end
            best_rms = 999.0
            for cut in range(search_start, end - win_step, win_step):
                seg_w = pcm[cut : cut + win_step]
                r = float(np.sqrt(np.mean(np.square(seg_w))))
                if r < best_rms:
                    best_rms = r
                    best_cut = cut + (win_step // 2)
            end = best_cut
        sub = np.ascontiguousarray(pcm[pos:end], dtype=np.float32)
        if sub.shape[0] >= 2400:
            sub_txt = asr.recognize(sub, sample_rate=16000)
            s_clean = (sub_txt if isinstance(sub_txt, str) else str(sub_txt or "")).strip()
            if s_clean:
                pieces.append(s_clean)
        pos = end
    return join_speech_segments(pieces)


def _transcribe_pcm_window_sync(
    pcm: np.ndarray,
    language: str,
    engine: str,
    model_name: str,
    is_live: bool,
    allow_groq: bool,
    cancel_event: Optional[threading.Event] = None,
    proxy_url: Any = _UNSET_PROXY,
) -> dict[str, Any]:
    global _active_model_name

    t_start = time.perf_counter()
    duration_sec = float(pcm.shape[0] / 16000.0) if pcm is not None and pcm.size else 0.0
    eff_engine, eff_model = resolve_voice_route(
        engine=engine,
        language=language,
        model_name=model_name,
        is_live=is_live,
        allow_groq=allow_groq,
    )

    min_dur = 0.28 if is_live else 0.14
    if duration_sec < min_dur:
        return {
            "text": "",
            "engine": eff_engine,
            "model": eff_model,
            "native_punct": eff_engine == "gigaam",
            "duration_sec": round(duration_sec, 2),
            "latency_ms": 0,
            "skipped": "too_short",
        }

    rms = float(np.sqrt(np.mean(np.square(pcm))))
    if rms < 0.0007:
        return {
            "text": "",
            "engine": eff_engine,
            "model": eff_model,
            "native_punct": eff_engine == "gigaam",
            "duration_sec": round(duration_sec, 2),
            "latency_ms": 0,
            "skipped": "silence",
        }

    if cancel_event is not None and cancel_event.is_set():
        return {
            "text": "",
            "engine": eff_engine,
            "model": eff_model,
            "native_punct": eff_engine == "gigaam",
            "duration_sec": round(duration_sec, 2),
            "latency_ms": 0,
            "skipped": "cancelled",
        }

    if eff_engine == "groq":
        try:
            txt = _transcribe_groq_sync(pcm, language, proxy_url=proxy_url)
            if _WHISPER_HALLUCINATION_RE.search(txt):
                txt = ""
            latency_ms = round((time.perf_counter() - t_start) * 1000)
            return {
                "text": txt,
                "engine": "groq",
                "model": "whisper-large-v3-turbo",
                "native_punct": True,
                "language": (language or "ru"),
                "duration_sec": round(duration_sec, 2),
                "latency_ms": latency_ms,
            }
        except Exception as groq_exc:
            logger.warning("Groq transcription failed (%s), falling back immediately to local GigaAM RNN-T", groq_exc)
            eff_engine = "gigaam" if _has_onnx_asr() else "whisper"
            eff_model = GIGAAM_ACCURATE_MODEL if eff_engine == "gigaam" else "small"

    if eff_engine == "gigaam":
        try:
            txt = _transcribe_gigaam_pcm_sync(pcm, eff_model, proxy_url=proxy_url)
            if not is_live:
                _active_model_name = eff_model
            latency_ms = round((time.perf_counter() - t_start) * 1000)
            return {
                "text": txt,
                "engine": "gigaam",
                "model": eff_model,
                "native_punct": True,
                "language": "ru",
                "duration_sec": round(duration_sec, 2),
                "latency_ms": latency_ms,
            }
        except Exception as giga_exc:
            logger.warning("GigaAM inference error (%s), falling back to faster-whisper", giga_exc)
            eff_engine = "whisper"
            eff_model = "base" if is_live else "small"

    txt, _segs, det_lang = _transcribe_whisper_pcm_sync(
        pcm,
        language=language,
        chosen_model=eff_model,
        live_preview=is_live,
        cancel_event=cancel_event,
        proxy_url=proxy_url,
    )
    if not is_live:
        _active_model_name = eff_model
    latency_ms = round((time.perf_counter() - t_start) * 1000)
    return {
        "text": txt,
        "engine": "whisper",
        "model": eff_model,
        "native_punct": False,
        "language": det_lang,
        "duration_sec": round(duration_sec, 2),
        "latency_ms": latency_ms,
    }


def _route_needs_outbound_proxy(eff_engine: str, eff_model: str) -> bool:
    if eff_engine == "groq":
        return bool(GROQ_API_KEY)
    if eff_engine == "gigaam":
        return not is_gigaam_cached(eff_model)
    if eff_engine == "whisper":
        return not is_model_cached(eff_model)
    return False


async def transcribe_pcm_window(
    pcm: np.ndarray,
    *,
    language: str = "ru",
    engine: str = "gigaam",
    model_name: str = "accurate",
    is_live: bool = False,
    allow_groq: bool = False,
) -> dict[str, Any]:
    """Transcribe a 16 kHz mono float32 numpy waveform in the dedicated CPU ASR pool."""
    proxy_url: Any = _UNSET_PROXY
    eff_engine, eff_model = resolve_voice_route(
        engine=engine,
        language=language,
        model_name=model_name,
        is_live=is_live,
        allow_groq=allow_groq,
    )
    if _route_needs_outbound_proxy(eff_engine, eff_model):
        try:
            proxy_url = await proxy_router.ensure_proxy_for_context("global")
        except proxy_router.ProxyRouteError as exc:
            if eff_engine == "groq":
                logger.warning("Global proxy route failed for Groq (%s), using offline local ASR", exc)
                engine = "gigaam" if _has_onnx_asr() else "whisper"
                allow_groq = False
            else:
                raise
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _executor,
        _transcribe_pcm_window_sync,
        pcm,
        language,
        engine,
        model_name,
        is_live,
        allow_groq,
        None,
        proxy_url,
    )


def _find_silence_split_sample(audio_np: np.ndarray) -> Optional[int]:
    """Find a natural >=450ms pause inside a >6.0s HTTP live window for commit_text splitting."""
    total = audio_np.shape[0]
    if total < int(6.0 * 16000):
        return None
    frame = 1600  # 100 ms
    min_lead = int(2.2 * 16000)
    max_pos = total - int(1.4 * 16000)
    silent_run = 0
    best_split: Optional[int] = None
    for idx in range(min_lead, max_pos, frame):
        chunk = audio_np[idx : idx + frame]
        r = float(np.sqrt(np.mean(np.square(chunk))))
        if r < 0.007:
            silent_run += 1
            if silent_run >= 5:  # >= 500 ms pause
                best_split = idx - (silent_run * frame // 2)
        else:
            silent_run = 0
    return best_split


def _transcribe_sync(
    audio_bytes: bytes,
    language: str,
    engine: str,
    model_name: str,
    live_preview: bool = False,
    offset_sec: float = 0.0,
    cancel_event: Optional[threading.Event] = None,
    proxy_url: Any = _UNSET_PROXY,
) -> dict[str, Any]:
    t_start = time.perf_counter()
    eff_engine, eff_model = resolve_voice_route(
        engine=engine,
        language=language,
        model_name=model_name,
        is_live=live_preview,
        allow_groq=not live_preview,
    )

    if cancel_event is not None and cancel_event.is_set():
        return {
            "text": "",
            "skipped": "cancelled",
            "duration_sec": 0.0,
            "latency_ms": 0,
            "engine": eff_engine,
            "model": eff_model,
            "native_punct": eff_engine == "gigaam",
        }

    audio_np, duration_sec = decode_audio_bytes_to_16k_float32(audio_bytes)
    if cancel_event is not None and cancel_event.is_set():
        return {
            "text": "",
            "skipped": "cancelled",
            "duration_sec": round(duration_sec, 2),
            "latency_ms": round((time.perf_counter() - t_start) * 1000),
            "engine": eff_engine,
            "model": eff_model,
            "native_punct": eff_engine == "gigaam",
        }

    window_duration = duration_sec
    effective_offset = 0.0
    if live_preview and offset_sec > 0.0:
        if offset_sec < duration_sec - 0.2:
            start_sample = max(0, int(offset_sec * 16000))
            audio_np = audio_np[start_sample:]
            window_duration = float(len(audio_np) / 16000.0)
            effective_offset = float(offset_sec)
        else:
            return {
                "text": "",
                "duration_sec": round(duration_sec, 2),
                "latency_ms": round((time.perf_counter() - t_start) * 1000),
                "engine": eff_engine,
                "model": eff_model,
                "native_punct": eff_engine == "gigaam",
                "skipped": "too_short",
            }

    min_win_dur = 0.38 if live_preview else 0.15
    if window_duration < min_win_dur:
        return {
            "text": "",
            "duration_sec": round(duration_sec, 2),
            "latency_ms": round((time.perf_counter() - t_start) * 1000),
            "engine": eff_engine,
            "model": eff_model,
            "native_punct": eff_engine == "gigaam",
            "skipped": "too_short",
        }

    rms = float(np.sqrt(np.mean(np.square(audio_np)))) if audio_np.size else 0.0
    if rms < 0.0008:
        return {
            "text": "",
            "duration_sec": round(duration_sec, 2),
            "latency_ms": round((time.perf_counter() - t_start) * 1000),
            "engine": eff_engine,
            "model": eff_model,
            "native_punct": eff_engine == "gigaam",
            "skipped": "silence",
        }

    # For Whisper HTTP live preview, preserve segment-based commit_text behavior
    if eff_engine == "whisper":
        final_text, seg_items, det_lang = _transcribe_whisper_pcm_sync(
            audio_np,
            language=language,
            chosen_model=eff_model,
            live_preview=live_preview,
            cancel_event=cancel_event,
            proxy_url=proxy_url,
        )
        latency_ms = round((time.perf_counter() - t_start) * 1000)
        out: dict[str, Any] = {
            "text": final_text,
            "language": det_lang,
            "duration_sec": round(duration_sec, 2),
            "latency_ms": latency_ms,
            "engine": "whisper",
            "model": eff_model,
            "native_punct": False,
        }
        if live_preview and window_duration > 4.5 and len(seg_items) >= 2:
            out["commit_text"] = " ".join(it[0] for it in seg_items[:-1]).strip()
            out["commit_end_sec"] = round(effective_offset + seg_items[-2][2], 2)
            out["tail_text"] = seg_items[-1][0]
        return out

    # GigaAM / Groq path for HTTP fallback
    if live_preview and window_duration > 6.5 and eff_engine == "gigaam":
        split_sample = _find_silence_split_sample(audio_np)
        if split_sample is not None and split_sample > 32000:
            head_res = _transcribe_pcm_window_sync(
                audio_np[:split_sample],
                language=language,
                engine="gigaam",
                model_name=GIGAAM_ACCURATE_MODEL,
                is_live=False,
                allow_groq=False,
                cancel_event=cancel_event,
                proxy_url=proxy_url,
            )
            tail_res = _transcribe_pcm_window_sync(
                audio_np[split_sample:],
                language=language,
                engine="gigaam",
                model_name=GIGAAM_FAST_MODEL,
                is_live=True,
                allow_groq=False,
                cancel_event=cancel_event,
                proxy_url=proxy_url,
            )
            c_txt = (head_res.get("text") or "").strip()
            t_txt = (tail_res.get("text") or "").strip()
            full_txt = join_speech_segments([c_txt], t_txt)
            return {
                "text": full_txt,
                "commit_text": c_txt,
                "commit_end_sec": round(effective_offset + (split_sample / 16000.0), 2),
                "tail_text": t_txt,
                "language": "ru",
                "duration_sec": round(duration_sec, 2),
                "latency_ms": round((time.perf_counter() - t_start) * 1000),
                "engine": "gigaam",
                "model": head_res.get("model") or eff_model,
                "native_punct": True,
            }

    res = _transcribe_pcm_window_sync(
        audio_np,
        language=language,
        engine=eff_engine,
        model_name=model_name,
        is_live=live_preview,
        allow_groq=not live_preview,
        cancel_event=cancel_event,
        proxy_url=proxy_url,
    )
    res["duration_sec"] = round(duration_sec, 2)
    res["latency_ms"] = round((time.perf_counter() - t_start) * 1000)
    return res


async def preload_default_model() -> None:
    """Warm up GigaAM-v3 E2E (CTC + RNN-T) on CPU in background after startup; soft-fallback to Whisper."""
    loop = asyncio.get_running_loop()
    if _has_onnx_asr() and VOICE_PRIMARY_ENGINE != "whisper":
        try:
            giga_proxy: Any = _UNSET_PROXY
            if not is_gigaam_cached(GIGAAM_FAST_MODEL) or not is_gigaam_cached(GIGAAM_ACCURATE_MODEL):
                giga_proxy = await proxy_router.ensure_proxy_for_context("global")
            await loop.run_in_executor(_executor, _load_gigaam_sync, GIGAAM_FAST_MODEL, giga_proxy)
            await loop.run_in_executor(_executor, _load_gigaam_sync, GIGAAM_ACCURATE_MODEL, giga_proxy)
            return
        except Exception as exc:
            logger.warning("Background GigaAM preload failed (%s), falling back to faster-whisper", exc)

    try:
        w_def = VOICE_MODEL_DEFAULT if VOICE_MODEL_DEFAULT in ALLOWED_MODELS else "small"
        w_proxy: Any = _UNSET_PROXY
        need_w_dl = not is_model_cached(w_def) or (
            w_def == "small" and not is_model_cached(LIVE_PREVIEW_FAST_MODEL)
        )
        if need_w_dl:
            w_proxy = await proxy_router.ensure_proxy_for_context("global")
        await loop.run_in_executor(_executor, _load_whisper_sync, w_def, w_proxy)
        if w_def == "small":
            await loop.run_in_executor(_executor, _load_whisper_sync, LIVE_PREVIEW_FAST_MODEL, w_proxy)
    except Exception as exc:
        logger.warning("Background Whisper preload skipped/failed: %s", exc)


async def transcribe_audio(
    audio_bytes: bytes,
    *,
    language: str = "ru",
    engine: str = "gigaam",
    model_name: str = "accurate",
    live_preview: bool = False,
    offset_sec: float = 0.0,
) -> dict[str, Any]:
    global _active_preview_cancel
    with _preview_cancel_lock:
        if _active_preview_cancel is not None:
            _active_preview_cancel.set()
        cancel_event: Optional[threading.Event] = (
            threading.Event() if live_preview else None
        )
        _active_preview_cancel = cancel_event

    proxy_url: Any = _UNSET_PROXY
    eff_engine, eff_model = resolve_voice_route(
        engine=engine,
        language=language,
        model_name=model_name,
        is_live=live_preview,
        allow_groq=not live_preview,
    )
    if _route_needs_outbound_proxy(eff_engine, eff_model):
        try:
            proxy_url = await proxy_router.ensure_proxy_for_context("global")
        except proxy_router.ProxyRouteError as exc:
            if eff_engine == "groq":
                logger.warning("Global proxy route failed for Groq (%s), using offline local ASR", exc)
                engine = "gigaam" if _has_onnx_asr() else "whisper"
            else:
                raise

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _executor,
        _transcribe_sync,
        audio_bytes,
        language,
        engine,
        model_name,
        live_preview,
        offset_sec,
        cancel_event,
        proxy_url,
    )


# ---------------------------------------------------------------------------
# Fast Deterministic Speech Post-Processor (<1 ms, zero external processes)
# ---------------------------------------------------------------------------

_VOICE_EXPLICIT_PUNCT_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(?:новая\s+строка|новый\s+абзац)\b[,.!?;:]*"), "\n"),
    (re.compile(r"(?i)\bточка\s+с\s+запятой\b[,.!?;:]*"), ";"),
    (re.compile(r"(?i)\bвопросительный\s+знак\b[,.!?;:]*"), "?"),
    (re.compile(r"(?i)\bвосклицательный\s+знак\b[,.!?;:]*"), "!"),
]

_VOICE_NON_NATIVE_PUNCT_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\bзапятая\b[,.!?;:]*"), ","),
    (re.compile(r"(?i)\bдвоеточие\b[,.!?;:]*"), ":"),
    (re.compile(r"(?i)\bтире\b[,.!?;:]*"), " — "),
]

_RU_FILLERS_RE = re.compile(
    r"(?i)(?:^|(?<=\s)|(?<=,))"
    r"(?:э+-?э+|э{2,}|м{2,}|ну\s+короче|короче\s+говоря|ну\s+это\s+самое|это\s+самое|так\s+сказать)"
    r"(?=\s|[,.!?;:]|$)"
)

# Multi-word & phonetic IT/filename normalizations (ordered longest/most-specific first)
_IT_TERMS_RULES: list[tuple[re.Pattern[str], str]] = [
    # Filenames (spoken Russian & Latin case-fixes)
    (re.compile(r"(?i)\b(?:опен[\s_-]*роутер[\s_-]*сервис\s*(?:точка\s*)?пай|openrouter_service\.py)\b"), "openrouter_service.py"),
    (re.compile(r"(?i)\b(?:аги[\s_-]*энджин\s*(?:точка\s*)?пай|agy_engine\.py)\b"), "agy_engine.py"),
    (re.compile(r"(?i)\b(?:датабей[зс]\s*(?:точка\s*)?пай|database\.py)\b"), "database.py"),
    (re.compile(r"(?i)\b(?:войс\s*(?:точка\s*)?пай|voice\.py)\b"), "voice.py"),
    (re.compile(r"(?i)\b(?:мейн\s*(?:точка\s*)?пай|main\.py)\b"), "main.py"),
    (re.compile(r"(?i)\b(?:стайлс\s*(?:точка\s*)?цсс|styles\.css)\b"), "styles.css"),
    (re.compile(r"(?i)\b(?:индекс\s*(?:точка\s*)?хтмл|index\.html)\b"), "index.html"),
    (re.compile(r"(?i)\b(?:апп\s*(?:точка\s*)?джей\s*эс|app\.js)\b"), "app.js"),
    (re.compile(r"(?i)\b(?:эс\s*дабл[\s-]*ю\s*(?:точка\s*)?джей\s*эс|sw\.js)\b"), "sw.js"),
    (re.compile(r"(?i)\bбат[\s-]+файл\b"), "bat-файл"),
    # Antigravity & AI models
    (re.compile(r"(?i)\b(?:антигравити[ий]?\s+хаб|antigravity\s+hub)\b"), "Antigravity Hub"),
    (re.compile(r"(?i)\b(?:антигравити[ий]?|antigravity)\b"), "Antigravity"),
    (re.compile(r"(?i)\b(?:опен[\s-]*р[оа]утер(?:а|у|ом|е|ы)?|open[\s-]*router)\b"), "OpenRouter"),
    (re.compile(r"(?i)\b(?:джемини|гемини|gemini)\b"), "Gemini"),
    (re.compile(r"(?i)\b(?:карбон|carbon)\b"), "Carbon"),
    (re.compile(r"(?i)\b(?:аргон|argon)\b"), "Argon"),
    (re.compile(r"(?i)\b(?:клод|claude)\b"), "Claude"),
    (re.compile(r"(?i)\b(?:соннет|сонет|sonnet)\b"), "Sonnet"),
    (re.compile(r"(?i)\b(?:опус|opus)\b"), "Opus"),
    (re.compile(r"(?i)\b(?:гига\s*ам|gigaam)\b"), "GigaAM"),
    (re.compile(r"(?i)\b(?:виспер|уиспер|whisper)\b"), "Whisper"),
    (re.compile(r"(?i)\b(?:оннкс|оникс(?=\s+(?:cpu|цпу|asr|модел|инференс|рантайм))|onnx)\b"), "ONNX"),
    # Hardware & Networking
    (re.compile(r"(?i)\b(?:райзен|ryzen)\b"), "Ryzen"),
    (re.compile(r"(?i)\b(?:микротик|mikrotik)\b"), "MikroTik"),
    (re.compile(r"(?i)\b(?:риалми|реалми|realme)\b"), "Realme"),
    (re.compile(r"(?i)\b(?:тейлскейл|tailscale)\b"), "Tailscale"),
    # Frameworks, protocols & stack
    (re.compile(r"(?i)\b(?:фаст\s*апи|fastapi)\b"), "FastAPI"),
    (re.compile(r"(?i)\b(?:ювикорн|увикорн|uvicorn)\b"), "Uvicorn"),
    (re.compile(r"(?i)\b(?:эскуэллайт|скллайт|sqlite)\b"), "SQLite"),
    (re.compile(r"(?i)\b(?:пайдантик|пидантик|pydantic)\b"), "Pydantic"),
    (re.compile(r"(?i)\bwebsockets\b"), "WebSockets"),
    (re.compile(r"(?i)\b(?:веб\s*сокет(?:ы|ов|ам)?|websocket)\b"), "WebSocket"),
    (re.compile(r"(?i)\b(?:аудио\s*ворклет|audioworklet)\b"), "AudioWorklet"),
    (re.compile(r"(?i)\b(?:докер|docker)\b"), "Docker"),
    (re.compile(r"(?i)\b(?:пауэршелл|повершелл|powershell)\b"), "PowerShell"),
    (re.compile(r"(?i)\b(?:гит\s*хаб|github)\b"), "GitHub"),
    (re.compile(r"(?i)\b(?:гит|git)\b"), "Git"),
    (re.compile(r"(?i)\b(?:джейсон|json)\b"), "JSON"),
    (re.compile(r"(?i)\b(?:хтмл|html)\b"), "HTML"),
    (re.compile(r"(?i)\b(?:цсс|css)\b"), "CSS"),
    (re.compile(r"(?i)\b(?:джава\s*скрипт|javascript)\b"), "JavaScript"),
    (re.compile(r"(?i)\b(?:тайп\s*скрипт|typescript)\b"), "TypeScript"),
    (re.compile(r"(?i)\b(?:питон|пайтон|python)\b"), "Python"),
    (re.compile(r"(?i)\brust\b"), "Rust"),
    (re.compile(r"(?i)\b(?:пва|pwa)\b"), "PWA"),
    (re.compile(r"(?i)\b(?:хттпс|https)\b"), "HTTPS"),
    (re.compile(r"(?i)\bssh\b"), "SSH"),
    (re.compile(r"(?i)\b(?:впс|vps)\b"), "VPS"),
    (re.compile(r"(?i)\blan\b"), "LAN"),
    (re.compile(r"(?i)\b(?:гпу|gpu)\b"), "GPU"),
    (re.compile(r"(?i)\b(?:цпу|cpu)\b"), "CPU"),
    (re.compile(r"(?i)\b(?:врам|vram)\b"), "VRAM"),
    (re.compile(r"(?i)\b(?:апи|api)\b"), "API"),
    (re.compile(r"(?i)\b(?:сиэлай|cli)\b"), "CLI"),
    (re.compile(r"(?i)\bui\b"), "UI"),
    (re.compile(r"(?i)\bux\b"), "UX"),
    (re.compile(r"(?i)\bdom\b"), "DOM"),
    (re.compile(r"(?i)\b(?:скл|эскуэль|sql)\b"), "SQL"),
]

_CANONICAL_IT_STARTS: set[str] = {
    repl.split()[0] for _, repl in _IT_TERMS_RULES if repl
}

_RU_QUESTION_START_RE = re.compile(
    r"(?i)^(?:а\s+|и\s+|но\s+|слушай[,\s]+|подскажи[,\s]+|скажи[,\s]+)?"
    r"(?:почему|зачем|как|где|когда|куда|откуда|кто|что|чей|какой|какая|какое|какие|сколько|"
    r"можно\s+ли|нужно\s+ли|надо\s+ли|стоит\s+ли|правда\s+ли|неужели|разве|"
    r"why|how|where|when|what|who|which|can\s+you|could\s+you|is\s+it|are\s+there|do\s+you)\b"
)

_CLAUSE_CONTINUATION_RE = re.compile(
    r"^(?:а|но|да|или|либо|что|чтобы|потому\s+что|так\s+как|поскольку|"
    r"который|которая|которое|которые|которого|которой|которых|которому|которым|"
    r"где|куда|откуда|когда|если|хотя|причём|причем|однако|зато|ведь|ибо)\b",
    re.IGNORECASE,
)


def _apply_context_terms(txt: str, context_terms: Optional[list[str]]) -> str:
    if not context_terms:
        return txt
    seen: set[str] = set()
    for raw_term in context_terms:
        term = (raw_term or "").strip()
        if len(term) < 2:
            continue
        low = term.lower()
        if low in seen:
            continue
        seen.add(low)
        # Exact token match (case-insensitive) -> canonical project casing
        txt = re.sub(rf"(?i)(?<![\w.]){re.escape(term)}(?![\w.])", term, txt)
        # Spoken underscore variant: "foo нижнее подчёркивание bar" -> "foo_bar"
        if "_" in term:
            parts = [re.escape(p) for p in term.split("_") if p]
            if len(parts) >= 2:
                spoken_us = r"\s+(?:нижнее\s+подч[её]ркивание|андерскор)\s+".join(parts)
                txt = re.sub(rf"(?i)(?<![\w.]){spoken_us}(?![\w.])", term, txt)
    return txt


def _lowercase_leading_if_safe(text: str) -> str:
    """Lowercase the first character of a continuation chunk unless it starts with an IT term or acronym."""
    if not text:
        return text
    first_token = re.split(r"[\s,.;:!?()\"'«»]+", text, maxsplit=1)[0]
    if not first_token:
        return text
    if first_token in _CANONICAL_IT_STARTS:
        return text
    # Keep acronyms (2+ uppercase letters like CPU, JSON, UI) untouched
    if len(first_token) >= 2 and any(ch.isupper() for ch in first_token[1:]):
        return text
    if first_token[0].isupper():
        return first_token[0].lower() + text[1:]
    return text


def join_speech_segments(segments: list[str], tail: str = "") -> str:
    """Smartly glue incremental utterance segments without mid-sentence periods or jarring capitals."""
    all_parts = [s.strip() for s in segments if s and s.strip()]
    if tail and tail.strip():
        all_parts.append(tail.strip())
    if not all_parts:
        return ""

    acc = all_parts[0]
    for nxt in all_parts[1:]:
        if not acc:
            acc = nxt
            continue
        # If previous segment ended with '.' right before a subordinate clause connector, turn '.' into ','
        if acc.endswith(".") and not acc.endswith("..") and _CLAUSE_CONTINUATION_RE.match(nxt):
            acc = acc[:-1] + ","
            nxt = _lowercase_leading_if_safe(nxt)
        elif acc[-1] not in ".!?…\n":
            nxt = _lowercase_leading_if_safe(nxt)

        if acc.endswith("\n"):
            acc = acc + nxt
        else:
            acc = f"{acc} {nxt}"
    return acc.strip()


def _format_speech_text_sync(
    raw_text: str,
    language: str = "ru",
    *,
    is_partial: bool = False,
    native_punct: bool = False,
    context_terms: Optional[list[str]] = None,
) -> str:
    txt = (raw_text or "").strip()
    if not txt:
        return ""

    # 1) Spoken punctuation commands (keep GigaAM native punctuation intact when native_punct=True)
    for pattern, repl in _VOICE_EXPLICIT_PUNCT_RULES:
        txt = pattern.sub(repl, txt)
    if not native_punct:
        for pattern, repl in _VOICE_NON_NATIVE_PUNCT_RULES:
            txt = pattern.sub(repl, txt)

    # 2) Remove filler hesitations and accidental adjacent word stutters
    txt = _RU_FILLERS_RE.sub(" ", txt)
    if native_punct:
        txt = re.sub(r"\b([a-zA-Zа-яА-ЯёЁ]{2,})\s+\1\b", r"\1", txt, flags=re.IGNORECASE)
    else:
        txt = re.sub(r"\b(\w+)(?:(?:\s*[,;]\s*|\s+)\1\b)+", r"\1", txt, flags=re.IGNORECASE)

    # 3) Normalize IT terms, filenames, and active user project names
    for pattern, repl in _IT_TERMS_RULES:
        txt = pattern.sub(repl, txt)
    txt = _apply_context_terms(txt, context_terms)

    # 4) Clean whitespace and handle capitalization / terminal punctuation according to is_partial & native_punct
    lines = txt.split("\n")
    cleaned_lines: list[str] = []
    for idx, line in enumerate(lines):
        s = re.sub(r"[ \t]+", " ", line).strip()
        if not s:
            continue
        s = re.sub(r"\s+([,.;:!?])", r"\1", s)
        if not native_punct:
            s = re.sub(r",+([,.;:!?])", r"\1", s)
            s = re.sub(r"([,;:!?])([^\s,.;:!?\"'»)])", r"\1 \2", s)
            s = re.sub(r"\s*—\s*", " — ", s)
            s = re.sub(r"[ \t]+", " ", s).strip(" ,;-")
        else:
            s = re.sub(r"[ \t]+", " ", s).strip()
        if not s:
            continue

        # Never force-capitalize the opening of a partial chunk (only capitalize after .!? inside it, or on final text)
        if not is_partial or idx > 0:
            s = s[0].upper() + s[1:]
        s = re.sub(
            r"([.!?]\s+)([a-zа-яё])",
            lambda m: m.group(1) + m.group(2).upper(),
            s,
        )

        # Re-assert exact casing for filenames / IT terms (e.g. app.js at start of line)
        for pattern, repl in _IT_TERMS_RULES:
            s = pattern.sub(repl, s)
        s = _apply_context_terms(s, context_terms)
        cleaned_lines.append(s)

    result = "\n".join(cleaned_lines).strip()
    if not result:
        return (raw_text or "").strip()

    # Strictly forbidden to append terminal . / ? when is_partial=True
    if not is_partial and result[-1] not in ".!?…:;\"'»)”:":
        last_line = cleaned_lines[-1] if cleaned_lines else result
        if _RU_QUESTION_START_RE.search(last_line):
            result += "?"
        else:
            result += "."

    return result


async def format_speech_text(
    raw_text: str,
    *,
    language: str = "ru",
    is_partial: bool = False,
    native_punct: bool = False,
    context_terms: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Deterministically format speech text in <1 ms without spawning external processes."""
    clean_in = re.sub(r"\s+", " ", (raw_text or "")).strip()
    if not clean_in:
        return {"text": "", "raw_text": "", "smart_formatted": False, "latency_ms": 0}

    t0 = time.perf_counter()
    formatted = _format_speech_text_sync(
        raw_text,
        language=language,
        is_partial=is_partial,
        native_punct=native_punct,
        context_terms=context_terms,
    )
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    return {
        "text": formatted,
        "raw_text": clean_in,
        "smart_formatted": True,
        "latency_ms": latency_ms,
    }
