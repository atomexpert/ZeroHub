"""
Streaming Neural TTS Service for Antigravity Hub Live Voice (v=70).
Strictly 0% GPU / 0 MB VRAM, 0 temporary files on disk, pure async Python.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
from typing import Any, Optional

from . import proxy_router
from .config import resolve_outbound_proxy

DEFAULT_TTS_VOICE = "ru-RU-DmitryNeural"
DEFAULT_TTS_VOICE_EN = "en-US-AndrewMultilingualNeural"

ALLOWED_TTS_VOICES: dict[str, dict[str, str]] = {
    "ru-RU-DmitryNeural": {
        "id": "ru-RU-DmitryNeural",
        "label": "Дмитрий",
        "lang": "ru",
        "gender": "male",
    },
    "ru-RU-SvetlanaNeural": {
        "id": "ru-RU-SvetlanaNeural",
        "label": "Светлана",
        "lang": "ru",
        "gender": "female",
    },
    "en-US-AndrewMultilingualNeural": {
        "id": "en-US-AndrewMultilingualNeural",
        "label": "Andrew",
        "lang": "en",
        "gender": "male",
    },
    "en-US-AvaMultilingualNeural": {
        "id": "en-US-AvaMultilingualNeural",
        "label": "Ava",
        "lang": "en",
        "gender": "female",
    },
}

LIVE_TURBO_MODEL_CANDIDATES: list[dict[str, str]] = [
    {"id": "gemini-3.8-flash-low", "label": "⚡ Flash 3.8 Low"},
    {"id": "gemini-4-carbon-low", "label": "🧠 Carbon 4 Low"},
    {"id": "claude-sonnet-5-5-low", "label": "🎭 Sonnet 5.5 Low"},
    {"id": "codex:gpt-5.4-mini@low", "label": "⚡ GPT-5.4 Mini Low"},
    {"id": "codex:gpt-5.4@low", "label": "🟢 GPT-5.4 Low"},
    {"id": "codex:gpt-5.3-codex@medium", "label": "🟢 GPT-5.3 Codex Med"},
]

_FENCED_CODE_RE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)", re.MULTILINE)
_UNCLOSED_FENCE_RE = re.compile(r"(```[\s\S]*$|~~~[\s\S]*$)", re.MULTILINE)
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^()\s]+|\([^()\s]*\))+\)")
_BARE_URL_RE = re.compile(r"(?:https?|file|wss?|ftp)://[^\s<>)\]]+", re.IGNORECASE)
_WIN_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/](?:[^\s<>\"'|?*]+[\\/])*[^\s<>\"'|?*]+")
_UNIX_PATH_RE = re.compile(r"(?<![\w/])/(?:workspace|home|var|etc|usr|opt|tmp|app|mnt)(?:/[^\s<>\"'|?*]+)+")
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_MD_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")
_MD_BLOCKQUOTE_RE = re.compile(r"(?m)^\s{0,3}>\s?")
_MD_LIST_BULLET_RE = re.compile(r"(?m)^\s*[-*+]\s+")
_MD_TABLE_SEP_RE = re.compile(r"(?m)^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_DECOR_RE = re.compile(r"(\*\*|__|~~|\*)")
_ALNUM_SPEECH_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]")

_SENTENCE_END_CHARS = ".!?\n"


def is_edge_tts_available() -> bool:
    return importlib.util.find_spec("edge_tts") is not None


def get_allowed_tts_voices_list() -> list[dict[str, str]]:
    return list(ALLOWED_TTS_VOICES.values())


def resolve_tts_voice(voice_id: Optional[str], language: str = "ru") -> str:
    cand = str(voice_id or "").strip()
    if cand in ALLOWED_TTS_VOICES:
        return cand
    lang = str(language or "ru").strip().lower()
    if lang.startswith("en"):
        return DEFAULT_TTS_VOICE_EN
    return DEFAULT_TTS_VOICE


def normalize_tts_rate(rate: Optional[str | float | int]) -> str:
    """
    Normalize playback rate into edge-tts percentage string clamped to [-20%, +50%].
    Supports multiplier forms ('1.0', '1.15', '1.3', '1.15x') and percent forms ('+15%', '-10%').
    """
    if rate is None:
        return "+15%"
    raw = str(rate).strip().lower().replace("×", "x")
    if not raw:
        return "+15%"

    pct_val: Optional[int] = None
    if raw.endswith("%"):
        num_str = raw[:-1].strip()
        try:
            pct_val = int(round(float(num_str)))
        except ValueError:
            pct_val = 15
    else:
        if raw.endswith("x"):
            raw = raw[:-1].strip()
        try:
            fval = float(raw)
            if 0.5 <= fval <= 2.5:
                pct_val = int(round((fval - 1.0) * 100.0))
            else:
                pct_val = int(round(fval))
        except ValueError:
            pct_val = 15

    clamped = max(-20, min(50, pct_val))
    return f"+{clamped}%" if clamped >= 0 else f"{clamped}%"


def _clean_fragment_for_speech(text: str) -> str:
    s = str(text or "")
    if not s:
        return ""
    s = _MD_IMAGE_RE.sub(r"\1", s)
    s = _MD_LINK_RE.sub(r"\1", s)
    s = _BARE_URL_RE.sub(" ", s)
    s = _WIN_PATH_RE.sub(" ", s)
    s = _UNIX_PATH_RE.sub(" ", s)
    s = _HTML_TAG_RE.sub(" ", s)
    s = _MD_TABLE_SEP_RE.sub(" ", s)
    s = _MD_HEADING_RE.sub("", s)
    s = _MD_BLOCKQUOTE_RE.sub("", s)
    s = _MD_LIST_BULLET_RE.sub("", s)
    s = _INLINE_CODE_RE.sub(r"\1", s)
    s = _MD_DECOR_RE.sub("", s)
    s = s.replace("|", " ")
    s = s.replace("```", " ").replace("~~~", " ").replace("`", "")
    # Collapse whitespace while preserving single spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _smart_truncate_spoken_text(cleaned: str, max_chars: int) -> str:
    if len(cleaned) <= max_chars:
        return cleaned

    suffix = " Полный текст готов в чате."
    target_hi = min(600, max(120, max_chars - len(suffix)))
    target_lo = min(450, max(60, int(target_hi * 0.65)))

    window = cleaned[:target_hi]
    best_cut = -1
    for m in re.finditer(r"[.!?](?:\s+|$)", window):
        pos = m.end()
        if pos >= target_lo:
            best_cut = pos
    if best_cut < 0:
        for m in re.finditer(r"[.!?](?:\s+|$)", window):
            best_cut = m.end()
    if best_cut < 0:
        sp = window.rfind(" ")
        best_cut = sp if sp >= 40 else target_hi

    head = cleaned[:best_cut].strip()
    if not head:
        head = cleaned[:target_hi].strip()
    if head and head[-1] not in ".!?":
        head += "."
    return f"{head}{suffix}"


def sanitize_text_for_tts(text: str, *, max_chars: int = 750) -> str:
    """
    Clean Markdown/code/paths/URLs from AI output so Neural TTS reads natural speech.
    - Fenced code blocks are stripped; if almost nothing else remains, returns 'Код выведен на экран.'
    - Long texts (> max_chars) are truncated cleanly on a sentence boundary + 'Полный текст готов в чате.'
    """
    raw = str(text or "")
    if not raw.strip():
        return ""

    had_code_block = bool(_FENCED_CODE_RE.search(raw) or "```" in raw or "~~~" in raw)
    without_code = _FENCED_CODE_RE.sub(" ", raw)
    without_code = _UNCLOSED_FENCE_RE.sub(" ", without_code)

    cleaned = _clean_fragment_for_speech(without_code)
    alnum_count = len(_ALNUM_SPEECH_RE.findall(cleaned))

    if had_code_block and alnum_count < 4:
        return "Код выведен на экран."
    if alnum_count == 0:
        return ""

    if max_chars > 0 and len(cleaned) > max_chars:
        cleaned = _smart_truncate_spoken_text(cleaned, max_chars=max_chars)
    return cleaned


class LiveSentenceChunker:
    """
    Low-latency streaming sentence chunker for real-time AI -> TTS synthesis.
    - Tracks ``` / ~~~ code fences across token deltas so code blocks are never spoken.
    - Aggressive first chunk (chunk_index == 0): emits as soon as >= 26 chars on sentence/clause
      end ('. ', '! ', '? ', ': ', '; ', '\\n') or >= 68 chars on ', ' / ' — '.
    - Subsequent chunks: emits on sentence end at >= 45 chars, or soft-splits at >= 220..240 chars.
    - Caps total spoken characters per turn at `max_total_chars` (appending 'Полный текст готов в чате.').
    """

    def __init__(self, *, max_total_chars: int = 750) -> None:
        self.max_total_chars = max_total_chars
        self.chunk_index = 0
        self.total_spoken_chars = 0
        self.had_code_fence = False
        self.truncated_notice_emitted = False
        self._raw_carry = ""
        self._in_fence = False
        self._fence_marker = "```"
        self._text_buf = ""

    def _consume_raw_delta(self, delta: str) -> None:
        if not delta:
            return
        data = self._raw_carry + delta
        self._raw_carry = ""

        while data:
            if not self._in_fence:
                idx_bt = data.find("```")
                idx_tl = data.find("~~~")
                candidates = [(i, m) for i, m in ((idx_bt, "```"), (idx_tl, "~~~")) if i >= 0]
                if not candidates:
                    # Keep up to 2 trailing backticks/tildes in carry in case fence splits across deltas
                    tail_keep = 0
                    for k in (2, 1):
                        if data.endswith("`" * k) or data.endswith("~" * k):
                            tail_keep = k
                            break
                    if tail_keep:
                        self._text_buf += data[:-tail_keep]
                        self._raw_carry = data[-tail_keep:]
                    else:
                        self._text_buf += data
                    break

                pos, marker = min(candidates, key=lambda x: x[0])
                if pos > 0:
                    self._text_buf += data[:pos]
                self._in_fence = True
                self._fence_marker = marker
                self.had_code_fence = True
                data = data[pos + 3 :]
                # Skip optional language identifier on opening fence line until newline
                nl = data.find("\n")
                if nl >= 0:
                    data = data[nl + 1 :]
                else:
                    # Wait for newline or closing fence safely
                    pass
            else:
                close_pos = data.find(self._fence_marker)
                if close_pos < 0:
                    # Keep last 2 chars in carry in case closing fence splits across deltas
                    if len(data) > 2:
                        self._raw_carry = data[-2:]
                    else:
                        self._raw_carry = data
                    break
                self._in_fence = False
                data = data[close_pos + 3 :]
                if self._text_buf and not self._text_buf.endswith(" "):
                    self._text_buf += " "

    def _find_split_pos(self, buf: str) -> int:
        n = len(buf)
        if self.chunk_index == 0:
            if n < 26:
                return -1
            # Primary fast split for chunk #0: >= 26 chars on sentence/colon/semicolon/newline
            for i in range(25, min(n, 220)):
                ch = buf[i]
                if ch == "\n":
                    return i + 1
                if ch in ".!?:;" and (i + 1 < n and buf[i + 1].isspace()):
                    return i + 2
            # Secondary fast split for chunk #0: >= 68 chars on comma or em-dash
            if n >= 68:
                for i in range(67, min(n, 220)):
                    if buf[i] == "," and (i + 1 < n and buf[i + 1] == " "):
                        return i + 2
                    if buf.startswith(" — ", i) or buf.startswith(" - ", i):
                        return i + 3
            if n >= 180:
                sp = buf.rfind(" ", 60, 180)
                return (sp + 1) if sp > 0 else 180
            return -1

        # Subsequent chunks (chunk_index >= 1)
        if n < 45:
            return -1
        for i in range(44, min(n, 225)):
            ch = buf[i]
            if ch == "\n":
                return i + 1
            if ch in ".!?" and (i + 1 < n and buf[i + 1].isspace()):
                return i + 2

        if n >= 240:
            # Soft split on clause boundary or space near 220..240
            best_clause = -1
            for sep in ("; ", ", ", " — ", " - "):
                p = buf.rfind(sep, 80, 240)
                if p > best_clause:
                    best_clause = p + len(sep)
            if best_clause > 0:
                return best_clause
            sp = buf.rfind(" ", 90, 240)
            return (sp + 1) if sp > 0 else 220
        return -1

    def _emit_piece(self, raw_piece: str) -> list[str]:
        if self.max_total_chars > 0 and self.total_spoken_chars >= self.max_total_chars:
            if not self.truncated_notice_emitted:
                self.truncated_notice_emitted = True
                return ["Полный текст готов в чате."]
            return []

        cleaned = _clean_fragment_for_speech(raw_piece)
        if not cleaned or not _ALNUM_SPEECH_RE.search(cleaned):
            return []

        if self.max_total_chars > 0:
            remaining = self.max_total_chars - self.total_spoken_chars
            if len(cleaned) > remaining:
                cut = cleaned[: max(40, remaining)]
                sp = cut.rfind(" ")
                if sp >= 30:
                    cut = cut[:sp]
                cut = cut.rstrip(" ,;:-")
                if cut and cut[-1] not in ".!?":
                    cut += "."
                self.total_spoken_chars += len(cut)
                self.chunk_index += 1
                self.truncated_notice_emitted = True
                return [f"{cut} Полный текст готов в чате."]

        self.total_spoken_chars += len(cleaned)
        self.chunk_index += 1
        return [cleaned]

    def push(self, delta: str) -> list[str]:
        if not delta:
            return []
        if self.truncated_notice_emitted:
            return []

        self._consume_raw_delta(delta)
        out: list[str] = []

        while self._text_buf and not self.truncated_notice_emitted:
            split_at = self._find_split_pos(self._text_buf)
            if split_at <= 0:
                break
            piece = self._text_buf[:split_at]
            self._text_buf = self._text_buf[split_at:].lstrip(" ")
            emitted = self._emit_piece(piece)
            if emitted:
                out.extend(emitted)
        return out

    def flush(self) -> list[str]:
        out: list[str] = []
        if not self._in_fence and self._raw_carry:
            self._text_buf += self._raw_carry
        self._raw_carry = ""

        if not self.truncated_notice_emitted and self._text_buf.strip():
            rem = self._text_buf
            self._text_buf = ""
            out.extend(self._emit_piece(rem))
        else:
            self._text_buf = ""

        if self.chunk_index == 0 and self.had_code_fence and not out:
            self.chunk_index += 1
            return ["Код выведен на экран."]
        return out


async def _synthesize_once(
    clean_text: str,
    resolved_voice: str,
    norm_rate: str,
    proxy: Optional[str],
) -> bytes:
    import edge_tts  # Lazy import so server boots even if optional dep is missing

    comm_kwargs: dict[str, Any] = {
        "rate": norm_rate,
    }
    if proxy:
        comm_kwargs["proxy"] = proxy

    communicate = edge_tts.Communicate(clean_text, resolved_voice, **comm_kwargs)
    buf = bytearray()
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            data = chunk.get("data")
            if data:
                buf.extend(data)
    return bytes(buf)


async def synthesize_tts_mp3(
    text: str,
    voice: str = DEFAULT_TTS_VOICE,
    rate: str = "+15%",
    *,
    already_sanitized: bool = False,
    timeout_sec: float = 12.0,
) -> bytes:
    """
    Synthesize spoken text into MP3 bytes strictly in RAM (0 disk writes, 0% GPU).
    Automatically routes through resolve_outbound_proxy() if configured, with 1 retry.
    """
    clean_text = str(text or "").strip() if already_sanitized else sanitize_text_for_tts(text)
    if not clean_text:
        return b""

    resolved_voice = resolve_tts_voice(voice)
    norm_rate = normalize_tts_rate(rate)
    proxy = await proxy_router.ensure_proxy_for_context("global")

    last_exc: Optional[ BaseException ] = None
    for attempt in range(2):
        try:
            audio_bytes = await asyncio.wait_for(
                _synthesize_once(clean_text, resolved_voice, norm_rate, proxy),
                timeout=timeout_sec,
            )
            if audio_bytes:
                return audio_bytes
            raise RuntimeError("Empty MP3 stream returned from edge-tts")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                await asyncio.sleep(0.25)
    if last_exc:
        raise last_exc
    return b""
