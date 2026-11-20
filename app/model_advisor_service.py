"""Dynamic Model Advisor 2.0: trait-based series passports, 0ms cohort inference,
background AI auto-calibration (Layer 2), and live upstream turn health penalties (Layer 3).
"""
from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from . import database, gdrive_service, openrouter_service
from .agy_credentials import TelemetryDeferred

_logger = logging.getLogger("agy_hub.model_advisor")

EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh")
EFFORT_DISPLAY_ORDER: tuple[str, ...] = ("xhigh", "high", "medium", "low")

CALIBRATION_STARTUP_DELAY_SEC: float = 12.0
CALIBRATION_POLL_INTERVAL_SEC: float = 60.0
CALIBRATION_RETRY_BACKOFF_HOURS: int = 2
HEALTH_WINDOW_SEC: float = 15.0 * 60.0

# ---------------------------------------------------------------------------
# Reference Seed Passports (`SEED_SERIES_PASSPORTS`)
# Preserves exact `capability` & `burn_class` of existing series for 100%
# backward compatibility while adding `gemini-4-neon` and `gemini-4-hydrogen`.
# ---------------------------------------------------------------------------

SEED_SERIES_PASSPORTS: dict[str, dict[str, Any]] = {
    "gemini-4-neon": {
        "vendor": "gemini",
        "display_name": "Gemini 4 Neon",
        "tiers": {
            "low": {"capability": 86, "burn_class": 1, "burn_score": 1.0},
            "medium": {"capability": 90, "burn_class": 1, "burn_score": 1.2},
            "high": {"capability": 93, "burn_class": 2, "burn_score": 1.65},
            "xhigh": {"capability": 95, "burn_class": 3, "burn_score": 2.8},
        },
        "coding": 1.0,
        "debug": 0.98,
        "ui": 0.86,
        "docs": 0.85,
        "role_ru": "Кодинг и агентные задачи (−32% расхода)",
    },
    "gemini-4-carbon": {
        "vendor": "gemini",
        "display_name": "Gemini 4 Carbon",
        "tiers": {
            "low": {"capability": 85, "burn_class": 1, "burn_score": 1.1},
            "medium": {"capability": 88, "burn_class": 1, "burn_score": 1.4},
            "high": {"capability": 92, "burn_class": 2, "burn_score": 2.4},
            "xhigh": {"capability": 94, "burn_class": 3, "burn_score": 3.2},
        },
        "coding": 0.92,
        "debug": 0.90,
        "ui": 1.0,
        "docs": 0.82,
        "role_ru": "Универсальный флагман и точный UI",
    },
    "gemini-4-argon": {
        "vendor": "gemini",
        "display_name": "Gemini 4 Argon",
        "tiers": {
            "low": {"capability": 80, "burn_class": 1, "burn_score": 0.9},
            "medium": {"capability": 83, "burn_class": 1, "burn_score": 1.0},
            "high": {"capability": 86, "burn_class": 1, "burn_score": 1.25},
            "xhigh": {"capability": 88, "burn_class": 2, "burn_score": 1.9},
        },
        "coding": 0.78,
        "debug": 0.76,
        "ui": 0.80,
        "docs": 0.95,
        "role_ru": "Экономная рабочая серия для повседневных задач",
    },
    "gemini-4-hydrogen": {
        "vendor": "gemini",
        "display_name": "Gemini 4 Hydrogen",
        "tiers": {
            "low": {"capability": 73, "burn_class": 1, "burn_score": 0.8},
            "medium": {"capability": 75, "burn_class": 1, "burn_score": 0.9},
            "high": {"capability": 78, "burn_class": 1, "burn_score": 1.0},
            "xhigh": {"capability": 78, "burn_class": 1, "burn_score": 1.1},
        },
        "coding": 0.62,
        "debug": 0.58,
        "ui": 0.70,
        "docs": 1.0,
        "role_ru": "Быстрая экономичная серия для текстов и правок",
    },
    "gemini-flash": {
        "vendor": "gemini",
        "display_name": "Gemini Flash",
        "tiers": {
            "low": {"capability": 71, "burn_class": 1, "burn_score": 0.8},
            "medium": {"capability": 74, "burn_class": 1, "burn_score": 0.9},
            "high": {"capability": 74, "burn_class": 1, "burn_score": 1.0},
            "xhigh": {"capability": 76, "burn_class": 2, "burn_score": 1.6},
        },
        "coding": 0.60,
        "debug": 0.55,
        "ui": 0.68,
        "docs": 1.0,
        "role_ru": "Скоростная серия для лёгких задач и сводок",
    },
    "gpt-6-astra": {
        "vendor": "gpt",
        "display_name": "GPT-6 Astra",
        "tiers": {
            "low": {"capability": 89, "burn_class": 2, "burn_score": 2.2},
            "medium": {"capability": 92, "burn_class": 3, "burn_score": 3.1},
            "high": {"capability": 95, "burn_class": 4, "burn_score": 4.2},
            "xhigh": {"capability": 97, "burn_class": 5, "burn_score": 5.0},
        },
        "coding": 1.0,
        "debug": 1.0,
        "ui": 0.82,
        "docs": 0.72,
        "role_ru": "Тяжёлый флагман для архитектуры и сложной отладки",
    },
    "gpt-6.1-sol": {
        "vendor": "gpt",
        "display_name": "GPT-6.1 Sol",
        "tiers": {
            "low": {"capability": 86, "burn_class": 1, "burn_score": 1.0},
            "medium": {"capability": 90, "burn_class": 2, "burn_score": 1.5},
            "high": {"capability": 93, "burn_class": 2, "burn_score": 1.85},
            "xhigh": {"capability": 95, "burn_class": 4, "burn_score": 3.8},
        },
        "coding": 0.96,
        "debug": 0.96,
        "ui": 0.84,
        "docs": 0.82,
        "role_ru": "Энергоэффективный лидер по коду и отладке",
    },
    "gpt-6.1-terra": {
        "vendor": "gpt",
        "display_name": "GPT-6.1 Terra",
        "tiers": {
            "low": {"capability": 83, "burn_class": 1, "burn_score": 0.9},
            "medium": {"capability": 86, "burn_class": 1, "burn_score": 1.1},
            "high": {"capability": 89, "burn_class": 1, "burn_score": 1.3},
            "xhigh": {"capability": 91, "burn_class": 3, "burn_score": 2.8},
        },
        "coding": 0.86,
        "debug": 0.85,
        "ui": 0.76,
        "docs": 0.92,
        "role_ru": "Экономный кодер с низким расходом квоты",
    },
    "gpt-5.4": {
        "vendor": "gpt",
        "display_name": "GPT-5.4",
        "tiers": {
            "low": {"capability": 84, "burn_class": 1, "burn_score": 1.0},
            "medium": {"capability": 87, "burn_class": 2, "burn_score": 1.7},
            "high": {"capability": 90, "burn_class": 2, "burn_score": 2.1},
            "xhigh": {"capability": 92, "burn_class": 4, "burn_score": 3.9},
        },
        "coding": 0.92,
        "debug": 0.92,
        "ui": 0.78,
        "docs": 0.80,
        "role_ru": "Надёжный инженер для бэкенда и тестов",
    },
    "gpt-5.3-codex": {
        "vendor": "gpt",
        "display_name": "GPT-5.3 Codex",
        "tiers": {
            "low": {"capability": 80, "burn_class": 1, "burn_score": 1.0},
            "medium": {"capability": 84, "burn_class": 2, "burn_score": 1.8},
            "high": {"capability": 87, "burn_class": 2, "burn_score": 2.2},
            "xhigh": {"capability": 89, "burn_class": 4, "burn_score": 4.0},
        },
        "coding": 0.88,
        "debug": 0.86,
        "ui": 0.68,
        "docs": 0.75,
        "role_ru": "Предыдущее поколение Codex",
    },
    "gpt-5.4-mini": {
        "vendor": "gpt",
        "display_name": "GPT-5.4 Mini",
        "tiers": {
            "low": {"capability": 75, "burn_class": 1, "burn_score": 0.8},
            "medium": {"capability": 79, "burn_class": 1, "burn_score": 0.95},
            "high": {"capability": 79, "burn_class": 1, "burn_score": 1.1},
            "xhigh": {"capability": 81, "burn_class": 2, "burn_score": 1.8},
        },
        "coding": 0.72,
        "debug": 0.70,
        "ui": 0.66,
        "docs": 0.96,
        "role_ru": "Лёгкая и быстрая модель OpenAI",
    },
    "gpt-oss": {
        "vendor": "gpt",
        "display_name": "GPT-OSS",
        "tiers": {
            "low": {"capability": 64, "burn_class": 1, "burn_score": 0.8},
            "medium": {"capability": 64, "burn_class": 1, "burn_score": 0.85},
            "high": {"capability": 68, "burn_class": 1, "burn_score": 1.0},
            "xhigh": {"capability": 68, "burn_class": 1, "burn_score": 1.1},
        },
        "coding": 0.52,
        "debug": 0.50,
        "ui": 0.48,
        "docs": 0.88,
        "role_ru": "Открытая базовая модель для простых текстов",
    },
    "claude-opus": {
        "vendor": "claude",
        "display_name": "Claude Opus",
        "tiers": {
            "low": {"capability": 90, "burn_class": 2, "burn_score": 2.3},
            "medium": {"capability": 93, "burn_class": 3, "burn_score": 3.3},
            "high": {"capability": 95, "burn_class": 4, "burn_score": 4.3},
            "xhigh": {"capability": 97, "burn_class": 5, "burn_score": 5.0},
        },
        "coding": 0.98,
        "debug": 1.0,
        "ui": 0.94,
        "docs": 0.78,
        "role_ru": "Флагман Anthropic для сложной архитектуры",
    },
    "claude-sonnet": {
        "vendor": "claude",
        "display_name": "Claude Sonnet",
        "tiers": {
            "low": {"capability": 85, "burn_class": 1, "burn_score": 1.1},
            "medium": {"capability": 89, "burn_class": 2, "burn_score": 1.7},
            "high": {"capability": 92, "burn_class": 2, "burn_score": 2.2},
            "xhigh": {"capability": 94, "burn_class": 3, "burn_score": 3.3},
        },
        "coding": 0.95,
        "debug": 0.92,
        "ui": 1.0,
        "docs": 0.86,
        "role_ru": "Сбалансированный универсал для кода и UI",
    },
    "claude-haiku": {
        "vendor": "claude",
        "display_name": "Claude Haiku",
        "tiers": {
            "low": {"capability": 74, "burn_class": 1, "burn_score": 0.8},
            "medium": {"capability": 78, "burn_class": 1, "burn_score": 0.95},
            "high": {"capability": 78, "burn_class": 1, "burn_score": 1.05},
            "xhigh": {"capability": 78, "burn_class": 2, "burn_score": 1.5},
        },
        "coding": 0.66,
        "debug": 0.62,
        "ui": 0.72,
        "docs": 1.0,
        "role_ru": "Молниеносная модель для текстов и мелких правок",
    },
}

# ---------------------------------------------------------------------------
# Module runtime state (discovered models, calibration lock, WS callback, health)
# ---------------------------------------------------------------------------

_DISCOVERED_MODELS_BY_ID: dict[str, dict[str, Any]] = {}
_CALIBRATION_LOCK: Optional[asyncio.Lock] = None
_IS_CALIBRATING: bool = False
_MODELS_UPDATED_NOTIFIER: Optional[Callable[[], Awaitable[None]]] = None
_SERIES_HEALTH_ERRORS: dict[str, list[float]] = {}

_EFFORT_PAREN_RE = re.compile(
    r"\s*\(\s*@?(extra[\s_-]+high|xhigh|max|ultra|high|pro|thinking|medium|auto|low|minimal|fast)\s*\)\s*$",
    re.IGNORECASE,
)
_EFFORT_AT_RE = re.compile(
    r"@(extra[\s_-]?high|xhigh|max|ultra|high|pro|thinking|medium|auto|low|minimal|fast)\b",
    re.IGNORECASE,
)
_EFFORT_SUFFIX_RE = re.compile(
    r"(?:^|[-_\s])(extra[-_\s]high|xhigh|max|ultra|high|pro|thinking|medium|auto|low|minimal|fast)$",
    re.IGNORECASE,
)
_TRANSIENT_HEALTH_KEYWORDS: tuple[str, ...] = (
    "503",
    "502",
    "504",
    "overloaded",
    "capacity",
    "resource_exhausted",
    "service unavailable",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "deadline exceeded",
    "stream stall",
    "no progress",
    "таймаут",
    "перегруж",
    "без прогресса",
)


def _get_calibration_lock() -> asyncio.Lock:
    global _CALIBRATION_LOCK
    if _CALIBRATION_LOCK is None:
        _CALIBRATION_LOCK = asyncio.Lock()
    return _CALIBRATION_LOCK


def set_advisor_ws_notifier(
    fn: Optional[Callable[[], Awaitable[None]]],
) -> None:
    global _MODELS_UPDATED_NOTIFIER
    _MODELS_UPDATED_NOTIFIER = fn


def notify_models_discovered(models: Optional[list[dict[str, Any]]]) -> None:
    """O(1) non-blocking registration of discovered models across all engines."""
    if not isinstance(models, list):
        return
    for item in models:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid:
            continue
        rec: dict[str, Any] = {
            "id": mid,
            "label": str(item.get("label") or "").strip(),
            "family": str(item.get("family") or "").strip(),
        }
        if item.get("context_window"):
            try:
                rec["context_window"] = int(item["context_window"])
            except (TypeError, ValueError):
                pass
        if item.get("description"):
            rec["description"] = str(item["description"]).strip()[:240]
        _DISCOVERED_MODELS_BY_ID[mid] = rec


def clear_discovered_models() -> None:
    _DISCOVERED_MODELS_BY_ID.clear()


def get_advisor_calibrator_status() -> dict[str, Any]:
    return {
        "is_calibrating": _IS_CALIBRATING,
        "discovered_count": len(_DISCOVERED_MODELS_BY_ID),
    }


# ---------------------------------------------------------------------------
# Identity Normalization (`normalize_model_identity`)
# ---------------------------------------------------------------------------


def _map_raw_effort_token(token: str) -> str:
    norm = re.sub(r"[\s_-]+", "", (token or "").strip().lower())
    if norm in ("extrahigh", "xhigh", "max", "ultra"):
        return "xhigh"
    if norm in ("high", "pro", "thinking"):
        return "high"
    if norm in ("medium", "auto"):
        return "medium"
    if norm in ("low", "minimal", "fast"):
        return "low"
    return ""


def _humanize_slug_title(raw_slug: str) -> str:
    slug = (raw_slug or "").strip()
    if not slug:
        return "Model"
    parts = [p for p in re.split(r"[-_\s]+", slug) if p]
    out: list[str] = []
    i = 0
    while i < len(parts):
        low = parts[i].lower()
        if low == "gpt" and i + 1 < len(parts) and re.match(r"^\d+(?:\.\d+)?$", parts[i + 1]):
            out.append(f"GPT-{parts[i + 1]}")
            i += 2
            continue
        if low == "gpt":
            out.append("GPT")
        elif low == "oss":
            out.append("OSS")
        elif low == "codex":
            out.append("Codex")
        elif re.match(r"^\d+(?:\.\d+)?$", parts[i]):
            out.append(parts[i])
        else:
            out.append(parts[i][0].upper() + parts[i][1:])
        i += 1
    return re.sub(r"\b(\d+) (\d+)\b", r"\1.\2", " ".join(out))


def _slugify_series_key(text: str) -> str:
    low = (text or "").strip().lower()
    low = re.sub(r"^(?:codex|claude-code):", "", low)
    low = re.sub(r"@[^@\s]+$", "", low)
    low = _EFFORT_PAREN_RE.sub("", low).strip()
    low = re.sub(r"[^a-z0-9.]+", "-", low).strip("-")
    low = re.sub(r"-{2,}", "-", low)
    return low or "model"


def _extract_version_score(hay: str) -> float:
    m = re.search(
        r"(?:gemini|claude|opus|sonnet|haiku|gpt|chatgpt|o)[-\s]*(\d+(?:[.-]\d+)?)",
        hay,
        re.IGNORECASE,
    )
    if m:
        try:
            return float(m.group(1).replace("-", "."))
        except ValueError:
            pass
    m2 = re.search(r"\b(\d+(?:\.\d+)?)\b", hay)
    if m2:
        try:
            return float(m2.group(1))
        except ValueError:
            pass
    return 0.0


def normalize_model_identity(entry: Any) -> dict[str, Any]:
    """Normalize a model dict or model ID string into vendor, effort, canonical `series_key`, and clean `base_title`."""
    if isinstance(entry, dict):
        raw_id = str(entry.get("id") or "").strip()
        raw_label = str(entry.get("label") or "").strip()
        raw_family = str(entry.get("family") or "").strip().lower()
        description = str(entry.get("description") or "").strip()[:240]
        try:
            ctx_win = int(entry.get("context_window") or 0)
        except (TypeError, ValueError):
            ctx_win = 0
    else:
        raw_id = str(entry or "").strip()
        raw_label = ""
        raw_family = ""
        description = ""
        ctx_win = 0

    id_low = raw_id.lower()
    label_low = raw_label.lower()
    hay = f"{id_low} {label_low} {raw_family}"

    # 1. Vendor detection
    if id_low.startswith("codex:"):
        vendor = "gpt"
    elif id_low.startswith("claude-code:"):
        vendor = "claude_code"
    elif raw_family in ("gemini", "claude", "claude_code", "gpt"):
        vendor = raw_family
    elif re.search(r"\b(?:gemini|carbon|neon|argon|hydrogen)\b", hay) or "gemini" in hay:
        vendor = "gemini"
    elif re.search(r"\b(?:claude|opus|sonnet|haiku)\b", hay) or "claude" in hay:
        vendor = "claude"
    elif (
        re.search(r"\b(?:gpt|chatgpt|openai|codex|o1|o3|o4|astra|sol|terra)\b", hay)
        or "gpt-" in hay
    ):
        vendor = "gpt"
    else:
        vendor = "other"

    # 2. Effort extraction (Extra High checked before High!)
    effort = ""
    at_m = _EFFORT_AT_RE.search(f"{id_low} {label_low}")
    if at_m:
        effort = _map_raw_effort_token(at_m.group(1))
    if not effort and raw_label:
        p_m = _EFFORT_PAREN_RE.search(raw_label)
        if p_m:
            effort = _map_raw_effort_token(p_m.group(1))
    if not effort:
        stripped_id = re.sub(r"^(?:codex|claude-code):", "", id_low)
        stripped_id = re.sub(r"@[^@\s]+$", "", stripped_id)
        s_m = _EFFORT_SUFFIX_RE.search(stripped_id)
        if s_m:
            effort = _map_raw_effort_token(s_m.group(1))
    if not effort:
        if re.search(r"(?:^|[-_(\s])extra[-_\s]?high(?:$|[-_)\s])", hay):
            effort = "xhigh"
        elif re.search(r"(?:^|[-_(\s])xhigh(?:$|[-_)\s])", hay):
            effort = "xhigh"
        elif "thinking" in id_low:
            effort = "high"
        else:
            effort = "medium" if vendor == "gemini" else "high"

    # 3. Strip effort from label/id to form clean `base_title`
    clean_slug = re.sub(r"^(?:codex|claude-code):", "", raw_id, flags=re.IGNORECASE)
    clean_slug = re.sub(r"@[^@\s]+$", "", clean_slug).strip()
    clean_slug_no_eff = _EFFORT_SUFFIX_RE.sub("", clean_slug).strip("-_ ")

    if raw_label:
        base_title = _EFFORT_PAREN_RE.sub("", raw_label).strip()
        if not base_title:
            base_title = _humanize_slug_title(clean_slug_no_eff or clean_slug)
    else:
        base_title = _humanize_slug_title(clean_slug_no_eff or clean_slug)

    # 4. Canonical `series_key` mapping (preserving existing series keys verbatim)
    series_key = ""
    if vendor == "gemini":
        if re.search(r"\bneon\b", hay) or "neon" in id_low:
            series_key = "gemini-4-neon"
        elif re.search(r"\bcarbon\b", hay) or "carbon" in id_low:
            series_key = "gemini-4-carbon"
        elif re.search(r"\bargon\b", hay) or "argon" in id_low:
            series_key = "gemini-4-argon"
        elif re.search(r"\bhydrogen\b", hay) or "hydrogen" in id_low:
            series_key = "gemini-4-hydrogen"
        elif re.search(r"\bflash\b", hay) or "flash" in id_low:
            series_key = "gemini-flash"
    elif vendor == "gpt":
        if re.search(r"\bastra\b", hay) or "gpt-6-astra" in id_low:
            series_key = "gpt-6-astra"
        elif re.search(r"\bsol\b", hay) or "6.1-sol" in id_low or "6-sol" in id_low:
            series_key = "gpt-6.1-sol"
        elif re.search(r"\bterra\b", hay) or "6.1-terra" in id_low or "6-terra" in id_low:
            series_key = "gpt-6.1-terra"
        elif re.search(r"\b5\.[123][-_]?codex\b", hay) or "gpt-5.3-codex" in id_low or "gpt-5.2-codex" in id_low:
            series_key = "gpt-5.3-codex"
        elif re.search(r"\boss\b", hay) or "gpt-oss" in id_low:
            series_key = "gpt-oss"
        elif re.search(r"\b5\.4\b", hay) and re.search(r"\bmini\b", hay):
            series_key = "gpt-5.4-mini"
        elif re.search(r"\b5\.4\b", hay) and not re.search(r"\b(codex|mini|nano|pro)\b", hay):
            series_key = "gpt-5.4"
        elif id_low in ("gpt-5.4", "codex:gpt-5.4") or clean_slug_no_eff.lower() == "gpt-5.4":
            series_key = "gpt-5.4"
    elif vendor in ("claude", "claude_code"):
        if re.search(r"\bopus\b", hay) or "opus" in id_low:
            series_key = "claude-opus"
        elif re.search(r"\bsonnet\b", hay) or "sonnet" in id_low:
            series_key = "claude-sonnet"
        elif re.search(r"\bhaiku\b", hay) or "haiku" in id_low:
            series_key = "claude-haiku"

    if not series_key:
        # Derive deterministic canonical slug for any new/unknown series
        series_key = _slugify_series_key(clean_slug_no_eff or base_title or raw_id)
        if vendor == "claude_code" and not series_key.startswith("claude-"):
            series_key = f"claude-{series_key}"

    return {
        "id": raw_id,
        "label": raw_label or base_title,
        "vendor": vendor,
        "effort": effort,
        "series_key": series_key,
        "base_title": base_title,
        "version_score": _extract_version_score(f"{series_key} {base_title}"),
        "context_window": ctx_win,
        "description": description,
    }


# ---------------------------------------------------------------------------
# Validation & Clamping
# ---------------------------------------------------------------------------


def _clamp_int(val: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(round(float(val)))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _clamp_float(val: Any, lo: float, hi: float, default: float) -> float:
    try:
        n = float(val)
    except (TypeError, ValueError):
        return default
    if n != n:  # NaN
        return default
    return round(max(lo, min(hi, n)), 2)


def validate_and_clamp_passport(
    raw: Optional[dict[str, Any]],
    *,
    fallback_passport: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Validate and clamp any AI/DB passport into safe bounded advisor ranges."""
    src = raw if isinstance(raw, dict) else {}
    fb = fallback_passport if isinstance(fallback_passport, dict) else {}
    fb_tiers = fb.get("tiers") if isinstance(fb.get("tiers"), dict) else {}

    default_tiers = {
        "low": {"capability": 85, "burn_class": 1, "burn_score": 1.0},
        "medium": {"capability": 89, "burn_class": 1, "burn_score": 1.3},
        "high": {"capability": 92, "burn_class": 2, "burn_score": 2.0},
        "xhigh": {"capability": 94, "burn_class": 3, "burn_score": 3.2},
    }

    raw_tiers = src.get("tiers") if isinstance(src.get("tiers"), dict) else {}
    # Also accept flat capability/burn_class if AI returned top-level fields
    flat_cap = src.get("capability")
    flat_burn_class = src.get("burn_class")
    flat_burn_score = src.get("burn_score")

    clamped_tiers: dict[str, dict[str, Any]] = {}
    for lvl in EFFORT_LEVELS:
        base_fb = fb_tiers.get(lvl) if isinstance(fb_tiers.get(lvl), dict) else default_tiers[lvl]
        t_in = raw_tiers.get(lvl) if isinstance(raw_tiers.get(lvl), dict) else {}
        cap_val = t_in.get("capability", t_in.get("cap"))
        if cap_val is None and lvl == "high" and flat_cap is not None:
            cap_val = flat_cap
        bc_val = t_in.get("burn_class", t_in.get("burn"))
        if bc_val is None and lvl == "high" and flat_burn_class is not None:
            bc_val = flat_burn_class
        bs_val = t_in.get("burn_score")
        if bs_val is None and lvl == "high" and flat_burn_score is not None:
            bs_val = flat_burn_score

        cap = _clamp_int(cap_val, 50, 97, int(base_fb.get("capability", default_tiers[lvl]["capability"])))
        bc = _clamp_int(bc_val, 1, 5, int(base_fb.get("burn_class", default_tiers[lvl]["burn_class"])))
        bs_default = float(base_fb.get("burn_score", float(bc)))
        bs = _clamp_float(bs_val if bs_val is not None else (float(bc) if bc_val is not None else bs_default), 0.8, 5.0, bs_default)
        clamped_tiers[lvl] = {
            "capability": cap,
            "burn_class": bc,
            "burn_score": bs,
        }

    # If AI only returned high tier and left others equal to fallback, derive sensible tier spread from high
    if raw_tiers and "high" in raw_tiers and len(raw_tiers) == 1:
        hi = clamped_tiers["high"]
        clamped_tiers["low"] = {
            "capability": _clamp_int(hi["capability"] - 7, 50, 97, hi["capability"]),
            "burn_class": _clamp_int(hi["burn_class"] - 1, 1, 5, 1),
            "burn_score": _clamp_float(hi["burn_score"] * 0.55, 0.8, 5.0, 1.0),
        }
        clamped_tiers["medium"] = {
            "capability": _clamp_int(hi["capability"] - 3, 50, 97, hi["capability"]),
            "burn_class": _clamp_int(max(1, hi["burn_class"] - 1), 1, 5, 1),
            "burn_score": _clamp_float(hi["burn_score"] * 0.72, 0.8, 5.0, 1.3),
        }
        clamped_tiers["xhigh"] = {
            "capability": _clamp_int(hi["capability"] + 2, 50, 97, hi["capability"]),
            "burn_class": _clamp_int(hi["burn_class"] + 1, 1, 5, 3),
            "burn_score": _clamp_float(hi["burn_score"] * 1.5, 0.8, 5.0, 3.2),
        }

    coding = _clamp_float(src.get("coding"), 0.1, 1.0, float(fb.get("coding", 0.85)))
    debug = _clamp_float(src.get("debug"), 0.1, 1.0, float(fb.get("debug", 0.85)))
    ui = _clamp_float(src.get("ui"), 0.1, 1.0, float(fb.get("ui", 0.82)))
    docs = _clamp_float(src.get("docs"), 0.1, 1.0, float(fb.get("docs", 0.82)))

    raw_role = re.sub(r"\s+", " ", str(src.get("role_ru") or fb.get("role_ru") or "").strip())
    role_ru = raw_role[:70] if raw_role else "Универсальная серия рассуждений"

    return {
        "tiers": clamped_tiers,
        "coding": coding,
        "debug": debug,
        "ui": ui,
        "docs": docs,
        "role_ru": role_ru,
    }


# ---------------------------------------------------------------------------
# Layer 1 — Instant 0ms Cohort Passport Inference
# ---------------------------------------------------------------------------


def infer_cohort_passport(
    series_info: dict[str, Any],
    pool: list[dict[str, Any]],
) -> dict[str, Any]:
    """Infer an accurate immediate (0ms) passport for any unseen series using vendor pool cohort signals."""
    vendor = str(series_info.get("vendor") or "other").lower()
    series_key = str(series_info.get("series_key") or "").lower()
    base_title = str(series_info.get("base_title") or series_info.get("display_name") or "").lower()
    description = str(series_info.get("description") or "").lower()
    ctx_win = int(series_info.get("context_window") or 0)
    version_score = float(series_info.get("version_score") or 0.0)
    efforts = set(series_info.get("efforts") or [])

    hay = f"{series_key} {base_title} {description}"
    vendor_versions = [
        float(m.get("version_score") or 0.0)
        for m in (pool or [])
        if str(m.get("vendor") or "").lower() == vendor and float(m.get("version_score") or 0.0) > 0
    ]
    max_vendor_version = max(vendor_versions) if vendor_versions else 0.0

    is_econ = bool(re.search(r"\b(?:lite|nano|oss|tiny|micro)\b", hay))
    is_fast = bool(re.search(r"\b(?:flash|mini|haiku|fast|instant|turbo|air)\b", hay))

    if is_econ:
        return {
            "tiers": {
                "low": {"capability": 62, "burn_class": 1, "burn_score": 0.8},
                "medium": {"capability": 64, "burn_class": 1, "burn_score": 0.9},
                "high": {"capability": 68, "burn_class": 1, "burn_score": 1.0},
                "xhigh": {"capability": 68, "burn_class": 1, "burn_score": 1.1},
            },
            "coding": 0.55,
            "debug": 0.52,
            "ui": 0.58,
            "docs": 0.92,
            "role_ru": "Экономичная серия (авто-пул)",
            "source": "cohort",
        }

    if is_fast:
        return {
            "tiers": {
                "low": {"capability": 74, "burn_class": 1, "burn_score": 0.85},
                "medium": {"capability": 77, "burn_class": 1, "burn_score": 0.95},
                "high": {"capability": 80, "burn_class": 1, "burn_score": 1.1},
                "xhigh": {"capability": 80, "burn_class": 2, "burn_score": 1.6},
            },
            "coding": 0.72,
            "debug": 0.68,
            "ui": 0.75,
            "docs": 0.98,
            "role_ru": "Быстрая серия (авто-пул)",
            "source": "cohort",
        }

    # Full modern reasoning series without downgrading markers
    is_current_or_newer = (
        max_vendor_version <= 0.0
        or version_score <= 0.0
        or version_score >= max_vendor_version
    )
    has_reasoning_ladder = bool({"xhigh", "high"} & efforts) or len(efforts) >= 2
    gen_adj = 0
    if not is_current_or_newer and (max_vendor_version - version_score) > 1.0:
        gen_adj = -2
    elif is_current_or_newer and max_vendor_version > 0 and version_score > max_vendor_version:
        gen_adj = 1

    coding = 0.88
    debug = 0.88
    ui = 0.85
    docs = 0.82
    role_ru = "Новая серия рассуждений (авто-пул)"

    if re.search(r"\b(?:codex|coding|code|agentic|swe|engineer|dev)\b", description):
        coding = 0.94
        debug = 0.92
        role_ru = "Новая кодерская серия (авто-пул)"
    elif re.search(r"\b(?:vision|ui|frontend|design|multimodal)\b", description):
        ui = 0.92
        role_ru = "Новая универсальная и UI-серия (авто-пул)"
    elif not has_reasoning_ladder and efforts == {"low"}:
        gen_adj = min(gen_adj, -2)

    if ctx_win >= 500_000:
        docs = max(docs, 0.88)

    return {
        "tiers": {
            "low": {"capability": _clamp_int(85 + gen_adj, 50, 97, 85), "burn_class": 1, "burn_score": 1.0},
            "medium": {"capability": _clamp_int(89 + gen_adj, 50, 97, 89), "burn_class": 1, "burn_score": 1.3},
            "high": {"capability": _clamp_int(92 + gen_adj, 50, 97, 92), "burn_class": 2, "burn_score": 2.0},
            "xhigh": {"capability": _clamp_int(94 + gen_adj, 50, 97, 94), "burn_class": 3, "burn_score": 3.2},
        },
        "coding": coding,
        "debug": debug,
        "ui": ui,
        "docs": docs,
        "role_ru": role_ru,
        "source": "cohort",
    }


# ---------------------------------------------------------------------------
# Layer 3 — Live Engine Turn Health Tracking
# ---------------------------------------------------------------------------


def is_transient_health_error(error_text: str) -> bool:
    low = (error_text or "").strip().lower()
    if not low:
        return False
    return any(kw in low for kw in _TRANSIENT_HEALTH_KEYWORDS)


def _prune_health_timestamps(series_key: str, now_mono: float) -> list[float]:
    cutoff = now_mono - HEALTH_WINDOW_SEC
    ts_list = _SERIES_HEALTH_ERRORS.get(series_key)
    if not ts_list:
        return []
    fresh = [t for t in ts_list if t >= cutoff]
    if fresh:
        _SERIES_HEALTH_ERRORS[series_key] = fresh
    else:
        _SERIES_HEALTH_ERRORS.pop(series_key, None)
    return fresh


def get_series_health_penalty(
    series_key_or_model_id: str,
    *,
    now_mono: Optional[float] = None,
) -> int:
    raw = str(series_key_or_model_id or "").strip()
    if not raw:
        return 0
    now = time.monotonic() if now_mono is None else float(now_mono)
    key = raw.lower()
    if key not in _SERIES_HEALTH_ERRORS:
        key = normalize_model_identity(raw)["series_key"]
    fresh = _prune_health_timestamps(key, now)
    cnt = len(fresh)
    if cnt >= 3:
        return -25
    if cnt >= 2:
        return -12
    return 0


def record_model_turn_health(
    model_id: str,
    *,
    ok: bool,
    error_text: str = "",
    now_mono: Optional[float] = None,
) -> int:
    """Update sliding 15-minute upstream health penalty for the model's series."""
    if not model_id:
        return 0
    series_key = normalize_model_identity(model_id)["series_key"]
    if not series_key:
        return 0
    now = time.monotonic() if now_mono is None else float(now_mono)
    if ok:
        _SERIES_HEALTH_ERRORS.pop(series_key, None)
        return 0
    if not is_transient_health_error(error_text):
        return get_series_health_penalty(series_key, now_mono=now)
    fresh = _prune_health_timestamps(series_key, now)
    fresh.append(now)
    _SERIES_HEALTH_ERRORS[series_key] = fresh[-12:]
    return get_series_health_penalty(series_key, now_mono=now)


def reset_health_state() -> None:
    _SERIES_HEALTH_ERRORS.clear()


# ---------------------------------------------------------------------------
# Layer 2 — Background Auto-Calibration (`run_auto_calibration_once` + loop)
# ---------------------------------------------------------------------------


def _match_openrouter_hint(
    series_key: str,
    display_name: str,
    or_catalog: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    if not or_catalog:
        return None
    tokens = [t for t in re.split(r"[-_\s]+", series_key.lower()) if len(t) >= 2]
    if not tokens:
        return None
    best: Optional[tuple[int, dict[str, Any]]] = None
    for item in or_catalog:
        if not isinstance(item, dict):
            continue
        hay = f"{item.get('id', '')} {item.get('name', '')}".lower()
        hits = sum(1 for tok in tokens if tok in hay)
        if hits >= max(2, len(tokens) - 1):
            if best is None or hits > best[0]:
                best = (hits, item)
    if not best:
        return None
    m = best[1]
    return {
        "id": str(m.get("id") or ""),
        "name": str(m.get("name") or ""),
        "prompt_price_per_m": m.get("prompt_price_per_m"),
        "completion_price_per_m": m.get("completion_price_per_m"),
        "context_length": m.get("context_length"),
        "supports_reasoning": bool(m.get("supports_reasoning")),
    }


def _extract_probe_series_map(raw_result: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_result, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    if isinstance(raw_result.get("series"), dict):
        for k, v in raw_result["series"].items():
            if isinstance(v, dict):
                out[str(k).strip().lower()] = v
        return out
    if isinstance(raw_result.get("items"), list):
        for item in raw_result["items"]:
            if isinstance(item, dict) and item.get("series_key"):
                out[str(item["series_key"]).strip().lower()] = item
        return out
    for k, v in raw_result.items():
        if isinstance(v, dict) and ("tiers" in v or "capability" in v or "coding" in v):
            out[str(k).strip().lower()] = v
    return out


async def run_auto_calibration_once(
    *,
    models: Optional[list[dict[str, Any]]] = None,
    force: bool = False,
    has_active_runs_fn: Optional[Callable[[], bool]] = None,
    probe_fn: Optional[Callable[[list[dict[str, Any]]], Awaitable[dict[str, Any]]]] = None,
    openrouter_catalog_fn: Optional[Callable[[], Awaitable[list[dict[str, Any]]]]] = None,
) -> dict[str, Any]:
    """Inspect discovered models and run a single batched telemetry AI calibration for unknown series."""
    global _IS_CALIBRATING
    from . import agy_engine

    # 1. Never interfere with active user turns
    active_check = has_active_runs_fn or (lambda: bool(agy_engine.ACTIVE_RUNS))
    if active_check():
        return {"status": "skipped_active_runs", "calibrated": [], "pending": 0}

    lock = _get_calibration_lock()
    if lock.locked():
        return {"status": "already_running", "calibrated": [], "pending": 0}

    async with lock:
        if active_check():
            return {"status": "skipped_active_runs", "calibrated": [], "pending": 0}
        _IS_CALIBRATING = True
        try:
            now_dt = datetime.now(timezone.utc)
            now_iso = now_dt.isoformat()
            await database.clear_stale_advisor_errors(now_iso=now_iso, force_all=force)

            raw_pool = list(models) if models is not None else list(_DISCOVERED_MODELS_BY_ID.values())
            if not raw_pool:
                raw_pool = list(agy_engine.get_cached_models())

            norm_pool = [normalize_model_identity(m) for m in raw_pool]
            grouped: dict[str, dict[str, Any]] = {}
            for nm in norm_pool:
                sk = nm["series_key"]
                g = grouped.setdefault(
                    sk,
                    {
                        "series_key": sk,
                        "vendor": nm["vendor"],
                        "display_name": nm["base_title"],
                        "base_title": nm["base_title"],
                        "version_score": nm["version_score"],
                        "context_window": nm["context_window"],
                        "description": nm["description"],
                        "efforts": set(),
                    },
                )
                g["efforts"].add(nm["effort"])
                if nm["context_window"] > g["context_window"]:
                    g["context_window"] = nm["context_window"]
                if nm["description"] and not g["description"]:
                    g["description"] = nm["description"]

            db_rows = await database.list_model_advisor_catalog()
            db_by_key = {row["series_key"]: row for row in db_rows}

            unknown_items: list[dict[str, Any]] = []
            for sk, info in grouped.items():
                if sk in SEED_SERIES_PASSPORTS:
                    continue
                existing = db_by_key.get(sk)
                if existing and existing.get("source") == "auto_ai" and existing.get("passport"):
                    continue
                if not force and existing:
                    retry_after = str(existing.get("retry_after") or "").strip()
                    if retry_after and retry_after > now_iso:
                        continue
                cohort = infer_cohort_passport(info, norm_pool)
                unknown_items.append(
                    {
                        "series_key": sk,
                        "vendor": info["vendor"],
                        "display_name": info["display_name"],
                        "efforts": sorted(info["efforts"]),
                        "context_window": info["context_window"],
                        "description": info["description"],
                        "cohort_baseline": cohort,
                    }
                )

            if not unknown_items:
                return {"status": "noop", "calibrated": [], "pending": 0}

            # Optional OpenRouter price/context hints (never fatal)
            or_catalog: list[dict[str, Any]] = []
            try:
                if openrouter_catalog_fn is not None:
                    or_catalog = await openrouter_catalog_fn()
                elif openrouter_service._MODELS_CACHE.get("items"):
                    or_catalog = list(openrouter_service._MODELS_CACHE["items"])
                else:
                    or_catalog = await openrouter_service.get_openrouter_models_catalog()
            except Exception:
                or_catalog = []

            for item in unknown_items:
                hint = _match_openrouter_hint(item["series_key"], item["display_name"], or_catalog)
                if hint:
                    item["openrouter_hint"] = hint

            # Single batched telemetry probe
            probe = probe_fn or agy_engine.probe_unknown_models_passport_via_cli
            try:
                raw_probe_out = await probe(unknown_items)
            except TelemetryDeferred:
                return {"status": "deferred", "calibrated": [], "pending": len(unknown_items)}
            except Exception as exc:
                retry_iso = (now_dt + timedelta(hours=CALIBRATION_RETRY_BACKOFF_HOURS)).isoformat()
                err_txt = str(exc)[:300]
                for item in unknown_items:
                    sk = item["series_key"]
                    prev = db_by_key.get(sk)
                    await database.upsert_model_advisor_entry(
                        sk,
                        vendor=item["vendor"],
                        display_name=item["display_name"],
                        source=prev["source"] if prev else "cohort",
                        passport=prev["passport"] if (prev and prev.get("passport")) else item["cohort_baseline"],
                        retry_after=retry_iso,
                        last_error=err_txt,
                    )
                _logger.warning("Advisor auto-calibration failed (backoff %dh): %s", CALIBRATION_RETRY_BACKOFF_HOURS, err_txt)
                return {
                    "status": "error",
                    "error": err_txt,
                    "calibrated": [],
                    "pending": len(unknown_items),
                }

            series_map = _extract_probe_series_map(raw_probe_out)
            calibrated: list[str] = []
            cal_ts = datetime.now(timezone.utc).isoformat()
            retry_missing = (datetime.now(timezone.utc) + timedelta(hours=CALIBRATION_RETRY_BACKOFF_HOURS)).isoformat()

            for item in unknown_items:
                sk = item["series_key"]
                cand = series_map.get(sk)
                if cand is None:
                    # Fallback lookup by normalized display_name/key
                    for k_resp, v_resp in series_map.items():
                        if _slugify_series_key(k_resp) == sk:
                            cand = v_resp
                            break
                if isinstance(cand, dict):
                    clamped = validate_and_clamp_passport(cand, fallback_passport=item["cohort_baseline"])
                    await database.upsert_model_advisor_entry(
                        sk,
                        vendor=item["vendor"],
                        display_name=item["display_name"],
                        source="auto_ai",
                        passport=clamped,
                        calibrated_at=cal_ts,
                        retry_after="",
                        last_error="",
                    )
                    calibrated.append(sk)
                else:
                    prev = db_by_key.get(sk)
                    await database.upsert_model_advisor_entry(
                        sk,
                        vendor=item["vendor"],
                        display_name=item["display_name"],
                        source=prev["source"] if prev else "cohort",
                        passport=prev["passport"] if (prev and prev.get("passport")) else item["cohort_baseline"],
                        retry_after=retry_missing,
                        last_error="Серия отсутствует в ответе авто-калибратора",
                    )

            if calibrated:
                try:
                    gdrive_service.notify_gdrive_change("admin", "model_advisor_catalog")
                except Exception:
                    pass
                if _MODELS_UPDATED_NOTIFIER is not None:
                    try:
                        await _MODELS_UPDATED_NOTIFIER()
                    except Exception:
                        pass

            return {
                "status": "ok",
                "calibrated": calibrated,
                "pending": len(unknown_items),
            }
        finally:
            _IS_CALIBRATING = False


async def run_advisor_auto_calibration_loop(
    *,
    has_active_runs_fn: Optional[Callable[[], bool]] = None,
    startup_delay_sec: float = CALIBRATION_STARTUP_DELAY_SEC,
    poll_interval_sec: float = CALIBRATION_POLL_INTERVAL_SEC,
) -> None:
    await asyncio.sleep(startup_delay_sec)
    while True:
        try:
            await run_auto_calibration_once(has_active_runs_fn=has_active_runs_fn)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(poll_interval_sec)


# ---------------------------------------------------------------------------
# Enrichment & Aggregated Catalog View (`enrich_models_with_advisor`, `build_catalog_view`)
# ---------------------------------------------------------------------------


def _resolve_series_passport_from_sources(
    series_info: dict[str, Any],
    norm_pool: list[dict[str, Any]],
    db_by_key: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], str, str]:
    sk = series_info["series_key"]
    if sk in SEED_SERIES_PASSPORTS:
        return copy.deepcopy(SEED_SERIES_PASSPORTS[sk]), "seed", ""
    db_entry = db_by_key.get(sk)
    if db_entry and isinstance(db_entry.get("passport"), dict) and db_entry["passport"].get("tiers"):
        src = str(db_entry.get("source") or "cohort")
        clamped = validate_and_clamp_passport(db_entry["passport"])
        return clamped, src, str(db_entry.get("calibrated_at") or "")
    cohort = infer_cohort_passport(series_info, norm_pool)
    return cohort, "cohort", ""


async def enrich_models_with_advisor(
    models: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach compact `advisor` passport payload to every model entry without mutating core fields."""
    if not models:
        return []
    db_by_key: dict[str, dict[str, Any]] = {}
    try:
        for row in await database.list_model_advisor_catalog():
            db_by_key[row["series_key"]] = row
    except Exception:
        db_by_key = {}

    norm_list = [normalize_model_identity(m) for m in models]

    # Collect per-series efforts & metadata across the pool for accurate Layer 1 cohort inference
    series_meta: dict[str, dict[str, Any]] = {}
    for nm in norm_list:
        sk = nm["series_key"]
        meta = series_meta.setdefault(
            sk,
            {
                "series_key": sk,
                "vendor": nm["vendor"],
                "base_title": nm["base_title"],
                "version_score": nm["version_score"],
                "context_window": nm["context_window"],
                "description": nm["description"],
                "efforts": set(),
            },
        )
        meta["efforts"].add(nm["effort"])
        if nm["context_window"] > meta["context_window"]:
            meta["context_window"] = nm["context_window"]
        if nm["description"] and not meta["description"]:
            meta["description"] = nm["description"]

    resolved_cache: dict[str, tuple[dict[str, Any], str, str]] = {}
    enriched: list[dict[str, Any]] = []

    for orig, nm in zip(models, norm_list):
        sk = nm["series_key"]
        if sk not in resolved_cache:
            resolved_cache[sk] = _resolve_series_passport_from_sources(
                series_meta[sk], norm_list, db_by_key
            )
        passport, source, calibrated_at = resolved_cache[sk]
        tiers = passport.get("tiers") or {}
        eff = nm["effort"]
        tier = tiers.get(eff) or tiers.get("high") or {"capability": 88, "burn_class": 2, "burn_score": 2.0}
        health_pen = get_series_health_penalty(sk)

        out_entry = dict(orig)
        out_entry["advisor"] = {
            "series_key": sk,
            "base_title": nm["base_title"],
            "effort": eff,
            "capability": int(tier["capability"]),
            "burn_class": int(tier["burn_class"]),
            "burn_score": float(tier["burn_score"]),
            "coding": float(passport.get("coding", 0.85)),
            "debug": float(passport.get("debug", 0.85)),
            "ui": float(passport.get("ui", 0.82)),
            "docs": float(passport.get("docs", 0.82)),
            "role_ru": str(passport.get("role_ru") or ""),
            "source": source,
            "health_penalty": int(health_pen),
            "calibrated_at": calibrated_at,
        }
        enriched.append(out_entry)

    return enriched


async def build_catalog_view(models: list[dict[str, Any]]) -> dict[str, Any]:
    """Build aggregated series catalog sorted by High capability for `/api/advisor/catalog`."""
    enriched = await enrich_models_with_advisor(models)
    by_series: dict[str, dict[str, Any]] = {}

    for m in enriched:
        adv = m.get("advisor") or {}
        sk = str(adv.get("series_key") or "").strip()
        if not sk:
            continue
        nm = normalize_model_identity(m)
        eff = str(adv.get("effort") or nm["effort"] or "high")
        rec = by_series.get(sk)
        if rec is None:
            rec = {
                "series_key": sk,
                "display_name": str(adv.get("base_title") or nm["base_title"]),
                "vendor": nm["vendor"],
                "source": str(adv.get("source") or "cohort"),
                "coding": float(adv.get("coding", 0.85)),
                "debug": float(adv.get("debug", 0.85)),
                "ui": float(adv.get("ui", 0.82)),
                "docs": float(adv.get("docs", 0.82)),
                "role_ru": str(adv.get("role_ru") or ""),
                "health_penalty": int(adv.get("health_penalty") or 0),
                "calibrated_at": str(adv.get("calibrated_at") or ""),
                "tiers": {},
                "efforts": [],
            }
            by_series[sk] = rec

        rec["tiers"][eff] = {
            "capability": int(adv.get("capability", 88)),
            "burn_class": int(adv.get("burn_class", 2)),
            "burn_score": float(adv.get("burn_score", 2.0)),
        }
        if eff not in rec["efforts"]:
            rec["efforts"].append(eff)

    items: list[dict[str, Any]] = []
    for rec in by_series.values():
        rec["efforts"].sort(
            key=lambda e: EFFORT_DISPLAY_ORDER.index(e) if e in EFFORT_DISPLAY_ORDER else 9
        )
        tiers_map = rec["tiers"]
        ref_tier = (
            tiers_map.get("high")
            or tiers_map.get("medium")
            or tiers_map.get("xhigh")
            or tiers_map.get("low")
            or {"capability": 85, "burn_class": 2, "burn_score": 2.0}
        )
        rec["capability"] = int(ref_tier["capability"])
        rec["burn_class"] = int(ref_tier["burn_class"])
        rec["burn_score"] = float(ref_tier["burn_score"])
        items.append(rec)

    items.sort(key=lambda it: (-it["capability"], it["burn_score"], it["display_name"].lower()))
    calibrated_count = sum(1 for it in items if it.get("source") == "auto_ai")
    return {
        "ok": True,
        "is_calibrating": _IS_CALIBRATING,
        "calibrated_count": calibrated_count,
        "total_series": len(items),
        "items": items,
    }
