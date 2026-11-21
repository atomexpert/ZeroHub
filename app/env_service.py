"""Safe project `.env` and file-secrets inventory & non-destructive editor service (v155).

Provides:
- Zero-trust path validation (`..`, absolute paths, symlinks/reparse points, blocked build/vcs dirs, 256 KiB cap)
- Project `.env` discovery (depth <= 2, root `.env` always first, templates excluded) and read-only metadata
  inventory of file secrets (`.secrets/`, `*secret*.json`, `*credentials*.json`, `*.pem`, `*.key`)
- Structure-preserving dotenv AST parser & non-destructive entry patcher (keeps `# comments`, blank lines,
  `export KEY=...`, single/double quotes, escapes, and section ordering)
- Hard protection of `HUB_SECRET_KEY` when editing the Hub project (`Antigravity_Hub` / `config.BASE_DIR`) itself
- Diff & bidirectional sync with companion `.env.example` (`import_missing` / `export_template` with zero secret leaks)
- Root `.gitignore` protection verification and non-destructive rule appending
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException

from . import config, gdrive_service, workspace_mcp

_ENV_PATH_LOCKS_GUARD = threading.Lock()
_ENV_PATH_LOCKS: dict[str, threading.Lock] = {}


class EnvConflictError(HTTPException):
    """Raised when optimistic SHA-256 concurrency check fails while saving a `.env` file."""

    def __init__(
        self,
        detail: str = "Файл .env был изменён на диске (конфликт версий). Обновите файл перед сохранением.",
    ) -> None:
        super().__init__(status_code=409, detail=detail)


def _get_env_path_lock(target: Path) -> threading.Lock:
    canon = os.path.normcase(str(target.resolve(strict=False)))
    with _ENV_PATH_LOCKS_GUARD:
        lock = _ENV_PATH_LOCKS.get(canon)
        if lock is None:
            if len(_ENV_PATH_LOCKS) > 512:
                for key, existing_lock in list(_ENV_PATH_LOCKS.items()):
                    if not existing_lock.locked():
                        _ENV_PATH_LOCKS.pop(key, None)
            lock = threading.Lock()
            _ENV_PATH_LOCKS[canon] = lock
        return lock

MAX_ENV_FILE_BYTES = 262_144  # 256 KiB
MAX_ENV_DISCOVERY_DEPTH = 2
MAX_SECRET_FILES_COUNT = 200

PROTECTED_SELF_HUB_KEYS: frozenset[str] = frozenset({"HUB_SECRET_KEY", "GITHUB_PUBLISH_TOKEN"})

ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENTRY_LINE_RE = re.compile(r"^([ \t]*)(export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)$")
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_GIT_SHORTNAME_RE = re.compile(r"^git~\d+$", re.IGNORECASE)

BLOCKED_ENV_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "target",
        "dist",
    }
)

SELF_HUB_SKIP_TOP_DIRS: frozenset[str] = frozenset(
    {
        "users",
        "shared_projects",
        "data",
        "scratch",
        "tmp",
    }
)

ENV_TEMPLATE_NAMES: frozenset[str] = frozenset(
    {
        ".env.example",
        ".env.sample",
        ".env.template",
    }
)

ENV_TEMPLATE_SUFFIXES: tuple[str, ...] = (
    ".example",
    ".sample",
    ".template",
)

SECRET_FILE_PATTERNS: tuple[str, ...] = (
    "*secret*.json",
    "*credentials*.json",
    "*.pem",
    "*.key",
)

_WIN_RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


# ---------------------------------------------------------------------------
# 1. Identity & Zero-Trust Path Validation
# ---------------------------------------------------------------------------


def is_self_hub_project(proj_dir: Path) -> bool:
    """Return True if `proj_dir` points to the live Hub codebase (`config.BASE_DIR`)."""
    try:
        return Path(proj_dir).resolve(strict=False) == config.BASE_DIR.resolve(strict=False)
    except Exception:
        return False


def _is_env_template_filename(name: str) -> bool:
    low = (name or "").strip().lower()
    if low in ENV_TEMPLATE_NAMES:
        return True
    if low.startswith(".env.") and any(low.endswith(sfx) for sfx in ENV_TEMPLATE_SUFFIXES):
        return True
    return False


def _is_live_env_filename(name: str) -> bool:
    low = (name or "").strip().lower()
    if not low:
        return False
    if _is_env_template_filename(low):
        return False
    return low == ".env" or (low.startswith(".env.") and len(low) > 5)


def _is_blocked_dir_name(name: str) -> bool:
    low = (name or "").strip().lower().rstrip(". ")
    if not low:
        return True
    return low in BLOCKED_ENV_DIRS or bool(_GIT_SHORTNAME_RE.match(low))


def _format_mtime_iso(mtime: float) -> str:
    try:
        return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _validate_rel_segments(rel_path: str) -> list[str]:
    if rel_path is None:
        raise ValueError("Не указан путь к файлу .env.")
    raw = str(rel_path).strip()
    if not raw:
        raise ValueError("Не указан путь к файлу .env.")
    if "\x00" in raw:
        raise PermissionError("Нулевой байт в пути файла запрещён.")
    if raw.startswith(("/", "\\")) or _WIN_DRIVE_RE.match(raw):
        raise PermissionError("Абсолютные пути запрещены: используйте относительный путь внутри проекта.")
    if Path(raw).is_absolute():
        raise PermissionError("Абсолютные пути запрещены: используйте относительный путь внутри проекта.")

    posix = raw.replace("\\", "/")
    while posix.startswith("./"):
        posix = posix[2:]
    if not posix or posix.endswith("/"):
        raise ValueError("Некорректный относительный путь к файлу .env.")

    parts = [seg for seg in posix.split("/") if seg != ""]
    if not parts:
        raise ValueError("Некорректный относительный путь к файлу .env.")

    for seg in parts:
        if seg in (".", ".."):
            raise PermissionError("Выход за пределы папки проекта через '..' запрещён.")
        if any(ord(ch) < 32 for ch in seg):
            raise PermissionError("Недопустимые управляющие символы в пути файла.")
        stem_upper = seg.split(".", 1)[0].upper()
        if stem_upper in _WIN_RESERVED_NAMES:
            raise PermissionError(f"Зарезервированное системное имя пути запрещено: {seg}")

    # Directory segments (excluding filename)
    dir_parts = parts[:-1]
    if len(dir_parts) > MAX_ENV_DISCOVERY_DEPTH:
        raise ValueError(
            f"Слишком глубокая вложенность для .env (максимум {MAX_ENV_DISCOVERY_DEPTH} уровня каталогов)."
        )
    for d_seg in dir_parts:
        if _is_blocked_dir_name(d_seg):
            raise PermissionError(f"Доступ к служебной директории '{d_seg}' запрещён.")
    return parts


def resolve_safe_env_target(
    proj_dir: Path,
    rel_path: str = ".env",
    *,
    allow_example: bool = False,
) -> tuple[Path, str]:
    """Validate and resolve a safe `.env` (or companion `.env.example`) target inside `proj_dir`."""
    root = Path(proj_dir).resolve(strict=False)
    if not root.is_dir():
        raise ValueError("Папка проекта не найдена.")
    if workspace_mcp._is_reparse_or_symlink(root):
        raise PermissionError("Корневая папка проекта является символической ссылкой или точкой повторного разбора.")

    parts = _validate_rel_segments(rel_path)
    filename = parts[-1]
    if allow_example:
        if not (_is_live_env_filename(filename) or _is_env_template_filename(filename)):
            raise ValueError(f"Ожидался файл .env или шаблон .env.example, получено: {filename}")
    else:
        if not _is_live_env_filename(filename):
            raise ValueError(
                f"Недопустимое имя файла окружения: {filename}. Допустимы только .env и .env.<суффикс> (не шаблоны)."
            )

    if is_self_hub_project(root) and len(parts) > 1 and parts[0].lower() in SELF_HUB_SKIP_TOP_DIRS:
        raise PermissionError(f"Доступ к системному каталогу '{parts[0]}' в Antigravity_Hub запрещён.")

    target = root.joinpath(*parts)
    if not workspace_mcp._is_within_dir(target, root):
        raise PermissionError("Целевой путь выходит за пределы папки проекта.")
    if workspace_mcp._has_reparse_in_chain(target, root):
        raise PermissionError("Символические ссылки и Windows reparse points запрещены для .env.")

    if target.exists():
        if workspace_mcp._is_reparse_or_symlink(target) or not target.is_file():
            raise PermissionError("Целевой .env должен быть обычным файлом (не папкой и не ссылкой).")
        try:
            st = os.lstat(target)
        except OSError as exc:
            raise ValueError(f"Не удалось прочитать метаданные файла: {exc}") from exc
        if int(st.st_size) > MAX_ENV_FILE_BYTES:
            raise ValueError(
                f"Файл превышает безопасный лимит {MAX_ENV_FILE_BYTES // 1024} КБ ({st.st_size} Б)."
            )

    return target, "/".join(parts)


def get_example_target_for_env(proj_dir: Path, rel_env_path: str) -> tuple[Path, str]:
    """Return companion `.env.example` target in the same directory as `rel_env_path`."""
    _, norm_env = resolve_safe_env_target(proj_dir, rel_env_path, allow_example=False)
    parent_rel = norm_env.rsplit("/", 1)[0] if "/" in norm_env else ""
    example_rel = f"{parent_rel}/.env.example" if parent_rel else ".env.example"
    return resolve_safe_env_target(proj_dir, example_rel, allow_example=True)


# ---------------------------------------------------------------------------
# 2. `.gitignore` Checking & Non-Destructive Protection
# ---------------------------------------------------------------------------


def is_env_gitignore_protected(proj_dir: Path, rel_path: str) -> bool:
    """Check whether `rel_path` is ignored by the project's root `.gitignore`."""
    root = Path(proj_dir).resolve(strict=False)
    gitignore_path = root / ".gitignore"
    if not gitignore_path.is_file() or workspace_mcp._is_reparse_or_symlink(gitignore_path):
        return False
    matcher = gdrive_service.GitIgnoreMatcher.from_file(gitignore_path)
    return matcher.is_ignored(rel_path.replace("\\", "/").lstrip("/"), is_dir=False)


def _atomic_write_text_utf8(target: Path, text: str, *, max_bytes: int = MAX_ENV_FILE_BYTES) -> None:
    if "\x00" in text:
        raise ValueError("Файл содержит недопустимые нулевые байты (NUL).")
    clean_text = text.lstrip("\ufeff")
    encoded = clean_text.encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError(f"Размер файла превышает допустимый предел {max_bytes // 1024} КБ.")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(target.parent),
            prefix=f".{target.name}.tmp_",
            delete=False,
        ) as fh:
            tmp_path = Path(fh.name)
            fh.write(encoded)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def ensure_env_in_gitignore(proj_dir: Path, rel_env_path: str = ".env") -> bool:
    """Ensure root `.gitignore` covers `.env`, `.env.*` and preserves `!.env.example`.

    Returns True if `.gitignore` was created or updated, False if already protected.
    """
    root = Path(proj_dir).resolve(strict=False)
    if not root.is_dir() or workspace_mcp._is_reparse_or_symlink(root):
        return False
    gitignore_path = root / ".gitignore"
    if gitignore_path.exists() and (
        workspace_mcp._is_reparse_or_symlink(gitignore_path) or not gitignore_path.is_file()
    ):
        return False

    norm_rel = rel_env_path.replace("\\", "/").lstrip("/")
    if not gitignore_path.exists():
        content = (
            "# Защита секретов окружения (ZeroHub)\n"
            ".env\n"
            ".env.*\n"
            "!.env.example\n"
        )
        _atomic_write_text_utf8(gitignore_path, content, max_bytes=1_048_576)
        return True

    try:
        existing = gitignore_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    newline = "\r\n" if "\r\n" in existing else "\n"
    matcher = gdrive_service.GitIgnoreMatcher.from_text(existing)
    existing_lines = {ln.strip() for ln in existing.splitlines() if ln.strip() and not ln.strip().startswith("#")}

    to_add: list[str] = []
    if not matcher.is_ignored(".env", is_dir=False) and ".env" not in existing_lines:
        to_add.append(".env")
    if not matcher.is_ignored(".env.local", is_dir=False) and ".env.*" not in existing_lines:
        to_add.append(".env.*")
    if (
        norm_rel
        and norm_rel not in (".env", ".env.local")
        and not matcher.is_ignored(norm_rel, is_dir=False)
        and ".env.*" not in to_add
        and ".env.*" not in existing_lines
    ):
        to_add.append(".env.*")

    # Always keep !.env.example exception when .env.* is active or added
    has_env_glob = ".env.*" in existing_lines or ".env.*" in to_add or matcher.is_ignored(".env.example", is_dir=False)
    if has_env_glob and "!.env.example" not in existing_lines:
        to_add.append("!.env.example")

    if not to_add:
        return False

    body = existing
    if body and not body.endswith(("\n", "\r")):
        body += newline
    if body and not body.endswith(f"{newline}{newline}"):
        body += newline
    addition = newline.join(["# Защита .env (ZeroHub)", *to_add]) + newline
    _atomic_write_text_utf8(gitignore_path, body + addition, max_bytes=1_048_576)
    return True


# ---------------------------------------------------------------------------
# 3. Project `.env` Discovery & Read-Only File Secrets Inventory
# ---------------------------------------------------------------------------


def discover_project_env_files(proj_dir: Path) -> list[dict[str, Any]]:
    """Discover `.env` and `.env.<suffix>` up to depth 2; always place root `.env` first."""
    root = Path(proj_dir).resolve(strict=False)
    if not root.is_dir() or workspace_mcp._is_reparse_or_symlink(root):
        return []

    self_hub = is_self_hub_project(root)
    gitignore_matcher = gdrive_service.GitIgnoreMatcher.from_file(root / ".gitignore")
    found: dict[str, dict[str, Any]] = {}

    def _walk(cur_dir: Path, rel_prefix: str, depth: int) -> None:
        try:
            with os.scandir(cur_dir) as it:
                entries = list(it)
        except OSError:
            return
        for entry in entries:
            name = entry.name
            entry_path = Path(entry.path)
            if workspace_mcp._is_reparse_or_symlink(entry_path):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue

            if is_dir:
                if depth >= MAX_ENV_DISCOVERY_DEPTH:
                    continue
                if _is_blocked_dir_name(name):
                    continue
                if self_hub and depth == 0 and name.lower() in SELF_HUB_SKIP_TOP_DIRS:
                    continue
                sub_rel = f"{rel_prefix}/{name}" if rel_prefix else name
                _walk(entry_path, sub_rel, depth + 1)
                continue

            if not is_file or not _is_live_env_filename(name):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if int(st.st_size) > MAX_ENV_FILE_BYTES:
                continue
            rel_posix = f"{rel_prefix}/{name}" if rel_prefix else name
            found[rel_posix] = {
                "path": rel_posix,
                "exists": True,
                "size_bytes": int(st.st_size),
                "updated_at": _format_mtime_iso(st.st_mtime),
                "gitignore_protected": gitignore_matcher.is_ignored(rel_posix, is_dir=False),
            }

    _walk(root, "", 0)

    root_env = found.pop(".env", None)
    if root_env is None:
        root_env = {
            "path": ".env",
            "exists": False,
            "size_bytes": 0,
            "updated_at": "",
            "gitignore_protected": gitignore_matcher.is_ignored(".env", is_dir=False),
        }

    others = sorted(
        found.values(),
        key=lambda item: (item["path"].count("/"), item["path"].lower()),
    )
    return [root_env, *others]


def scan_project_secret_files(proj_dir: Path) -> list[dict[str, Any]]:
    """Collect read-only metadata for project file secrets (`.secrets/`, `*secret*.json`, `*credentials*.json`, `*.pem`, `*.key`).

    Never reads file contents.
    """
    root = Path(proj_dir).resolve(strict=False)
    if not root.is_dir() or workspace_mcp._is_reparse_or_symlink(root):
        return []

    self_hub = is_self_hub_project(root)
    gitignore_matcher = gdrive_service.GitIgnoreMatcher.from_file(root / ".gitignore")
    results: list[dict[str, Any]] = []

    def _walk(cur_dir: Path, rel_prefix: str, in_secrets_dir: bool, depth: int) -> None:
        if len(results) >= MAX_SECRET_FILES_COUNT or depth > 6:
            return
        try:
            with os.scandir(cur_dir) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            return
        for entry in entries:
            if len(results) >= MAX_SECRET_FILES_COUNT:
                return
            name = entry.name
            low = name.lower()
            entry_path = Path(entry.path)
            if workspace_mcp._is_reparse_or_symlink(entry_path):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue

            sub_rel = f"{rel_prefix}/{name}" if rel_prefix else name
            if is_dir:
                if _is_blocked_dir_name(name):
                    continue
                if self_hub and depth == 0 and low in SELF_HUB_SKIP_TOP_DIRS:
                    continue
                _walk(entry_path, sub_rel, in_secrets_dir or low == ".secrets", depth + 1)
                continue

            if not is_file:
                continue
            matched = in_secrets_dir or any(
                fnmatch.fnmatchcase(low, pat) for pat in SECRET_FILE_PATTERNS
            )
            if not matched:
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            results.append(
                {
                    "rel_path": sub_rel,
                    "size_bytes": int(st.st_size),
                    "updated_at": _format_mtime_iso(st.st_mtime),
                    "gitignore_protected": gitignore_matcher.is_ignored(sub_rel, is_dir=False),
                }
            )

    _walk(root, "", False, 0)
    results.sort(key=lambda item: item["rel_path"].lower())
    return results


# ---------------------------------------------------------------------------
# 4. Structure-Preserving Dotenv AST Parser & Non-Destructive Patcher
# ---------------------------------------------------------------------------


def validate_env_key(key: str) -> str:
    clean = str(key or "").strip()
    if not clean or not ENV_KEY_RE.match(clean):
        raise ValueError(
            f"Некорректное имя переменной '{key}'. Допустимы только латинские буквы, цифры и '_', начиная не с цифры."
        )
    return clean


def _unescape_double_quoted(raw_inner: str) -> str:
    out: list[str] = []
    i = 0
    n = len(raw_inner)
    while i < n:
        ch = raw_inner[i]
        if ch == "\\" and i + 1 < n:
            nxt = raw_inner[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "r":
                out.append("\r")
            elif nxt == "t":
                out.append("\t")
            elif nxt == '"':
                out.append('"')
            elif nxt == "\\":
                out.append("\\")
            else:
                out.append("\\" + nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _find_closing_double_quote(text: str, start: int = 1) -> int:
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            return i
        i += 1
    return -1


def _parse_rhs_single_line(raw_rhs: str) -> tuple[str, str, str, bool]:
    """Parse RHS of `KEY=<raw_rhs>`.

    Returns `(value, quote_style, inline_comment, is_unclosed_double_quote)`.
    """
    lead_stripped = raw_rhs.lstrip(" \t")
    if not lead_stripped:
        return "", "", "", False

    first = lead_stripped[0]
    if first == '"':
        close_idx = _find_closing_double_quote(lead_stripped, 1)
        if close_idx == -1:
            return lead_stripped[1:], '"', "", True
        inner = lead_stripped[1:close_idx]
        rest = lead_stripped[close_idx + 1 :].strip()
        inline_comment = rest if rest.startswith("#") else ""
        return _unescape_double_quoted(inner), '"', inline_comment, False

    if first == "'":
        close_idx = lead_stripped.find("'", 1)
        if close_idx == -1:
            return lead_stripped[1:], "'", "", False
        inner = lead_stripped[1:close_idx]
        rest = lead_stripped[close_idx + 1 :].strip()
        inline_comment = rest if rest.startswith("#") else ""
        return inner, "'", inline_comment, False

    # Unquoted value: inline `# comment` starts after whitespace (`KEY=val # comment` or `KEY= # comment`)
    if first == "#" and raw_rhs.startswith((" ", "\t")):
        return "", "", lead_stripped, False

    in_comment_idx = -1
    for idx, ch in enumerate(lead_stripped):
        if ch == "#" and idx > 0 and lead_stripped[idx - 1] in (" ", "\t"):
            in_comment_idx = idx
            break
    if in_comment_idx >= 0:
        val_part = lead_stripped[:in_comment_idx].rstrip(" \t")
        comment_part = lead_stripped[in_comment_idx:].strip()
        return val_part, "", comment_part, False

    return lead_stripped.rstrip(" \t"), "", "", False


def _strip_comment_prefix(line: str) -> str:
    stripped = line.lstrip(" \t")
    if stripped.startswith("#"):
        stripped = stripped[1:]
        if stripped.startswith(" "):
            stripped = stripped[1:]
    return stripped


def parse_dotenv_text(text: str) -> dict[str, Any]:
    """Parse `.env` text into an AST preserving blank lines, comments, quotes, `export`, and line endings."""
    clean = (text or "").lstrip("\ufeff")
    newline = "\r\n" if "\r\n" in clean else "\n"
    trailing_newline = clean.endswith("\n") or clean.endswith("\r")
    raw_lines = clean.splitlines()

    nodes: list[dict[str, Any]] = []
    pending_comment_indices: list[int] = []
    entries_by_key_order: list[str] = []
    entries_map: dict[str, dict[str, Any]] = {}

    i = 0
    total = len(raw_lines)
    while i < total:
        line = raw_lines[i]
        line_no = i + 1
        stripped = line.strip()

        if not stripped:
            nodes.append({"type": "blank", "raw": line, "line_no": line_no})
            pending_comment_indices = []
            i += 1
            continue

        if stripped.startswith("#"):
            idx = len(nodes)
            nodes.append({"type": "comment", "raw": line, "line_no": line_no})
            pending_comment_indices.append(idx)
            i += 1
            continue

        m = _ENTRY_LINE_RE.match(line)
        if not m:
            nodes.append({"type": "other", "raw": line, "line_no": line_no})
            pending_comment_indices = []
            i += 1
            continue

        indent = m.group(1) or ""
        export_prefix = m.group(2) or ""
        key = m.group(3)
        raw_rhs = m.group(4) or ""
        consumed_lines = [line]

        val, quote, inline_comment, unclosed_dq = _parse_rhs_single_line(raw_rhs)
        if unclosed_dq:
            # Accumulate literal multiline double-quoted value until unescaped `"`
            accum = [raw_rhs.lstrip(" \t")[1:]]
            j = i + 1
            closed = False
            while j < total:
                nxt_line = raw_lines[j]
                consumed_lines.append(nxt_line)
                close_pos = _find_closing_double_quote(nxt_line, 0)
                if close_pos >= 0:
                    accum.append(nxt_line[:close_pos])
                    rest = nxt_line[close_pos + 1 :].strip()
                    inline_comment = rest if rest.startswith("#") else ""
                    val = _unescape_double_quoted("\n".join(accum))
                    closed = True
                    i = j
                    break
                accum.append(nxt_line)
                j += 1
            if not closed:
                val = _unescape_double_quoted("\n".join(accum))
                i = total - 1

        bound_indices = list(pending_comment_indices)
        pending_comment_indices = []
        comment_text = "\n".join(
            _strip_comment_prefix(nodes[c_idx]["raw"]) for c_idx in bound_indices
        ).strip()

        node = {
            "type": "entry",
            "raw": newline.join(consumed_lines),
            "indent": indent,
            "export_prefix": export_prefix,
            "key": key,
            "value": val,
            "quote": quote,
            "inline_comment": inline_comment,
            "comment_node_indices": bound_indices,
            "comment": comment_text,
            "line_no": line_no,
        }
        nodes.append(node)

        entry_summary = {
            "key": key,
            "value": val,
            "comment": comment_text,
            "is_empty": val == "",
            "has_export": bool(export_prefix),
            "quote": quote,
            "inline_comment": inline_comment,
            "line_no": line_no,
        }
        if key not in entries_map:
            entries_by_key_order.append(key)
        entries_map[key] = entry_summary
        i += 1

    entries = [entries_map[k] for k in entries_by_key_order]
    return {
        "newline": newline,
        "trailing_newline": trailing_newline,
        "nodes": nodes,
        "entries": entries,
    }


def _format_env_value(value: str, preferred_quote: str = "") -> str:
    val = "" if value is None else str(value)
    if val == "":
        return ""

    needs_double_quotes = (
        val != val.strip()
        or "\n" in val
        or "\r" in val
        or "#" in val
        or '"' in val
        or "'" in val
    )
    if preferred_quote == "'" and not any(ch in val for ch in ("'", "\n", "\r")):
        return f"'{val}'"

    if needs_double_quotes or preferred_quote == '"':
        escaped = (
            val.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\r", "\\r")
            .replace("\n", "\\n")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'

    return val


def _format_comment_lines(comment: str, indent: str = "") -> list[str]:
    clean = str(comment or "").strip()
    if not clean:
        return []
    out: list[str] = []
    for ln in clean.splitlines():
        trimmed = ln.strip()
        if not trimmed:
            continue
        if trimmed.startswith("#"):
            out.append(f"{indent}{trimmed}")
        else:
            out.append(f"{indent}# {trimmed}")
    return out


def apply_entries_to_dotenv(
    existing_text: str,
    entries: list[dict[str, Any]],
    *,
    merge: bool = False,
) -> str:
    """Patch existing `.env` text non-destructively using structured `entries`.

    - Validates all keys (`^[A-Za-z_][A-Za-z0-9_]*$`) and rejects duplicates.
    - Updates modified keys in-place without losing existing `# comments`, blank lines, or section order.
    - Leaves untouched keys byte-for-byte identical (preserving original quotes, `export`, inline comments).
    - With merge=True, keeps entries omitted by the caller.
    - Removes deleted keys while preserving standalone/section comments and blank lines.
    - Appends newly added keys at the end of the file (with `# comment` above if provided).
    """
    normalized_items: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for raw_item in entries or []:
        if not isinstance(raw_item, dict):
            raise ValueError("Некорректный формат записи переменной окружения.")
        key = validate_env_key(raw_item.get("key", ""))
        if key in by_key:
            raise ValueError(f"Дублирующееся имя переменной: {key}")
        val = "" if raw_item.get("value") is None else str(raw_item.get("value"))
        if "\x00" in val:
            raise ValueError(f"Значение переменной {key} содержит недопустимый нулевой байт.")
        has_comment = "comment" in raw_item and raw_item.get("comment") is not None
        comment_val = str(raw_item.get("comment") or "").strip() if has_comment else None
        item = {"key": key, "value": val, "comment": comment_val}
        normalized_items.append(item)
        by_key[key] = item

    parsed = parse_dotenv_text(existing_text or "")
    newline = parsed["newline"]
    nodes = parsed["nodes"]

    # Map node_index -> replacement lines (list[str]) or None (omit)
    # Default: keep every node as its raw string.
    node_output: dict[int, Optional[list[str]]] = {
        idx: [node["raw"]] for idx, node in enumerate(nodes)
    }

    # For duplicate keys in the existing file, keep/patch the LAST occurrence so effective order matches dotenv.
    last_node_idx_by_key: dict[str, int] = {}
    for idx, node in enumerate(nodes):
        if node["type"] == "entry":
            last_node_idx_by_key[node["key"]] = idx

    matched_keys: set[str] = set()
    for idx, node in enumerate(nodes):
        if node["type"] != "entry":
            continue
        k = node["key"]
        if merge and k not in by_key:
            continue
        if k not in by_key or last_node_idx_by_key.get(k) != idx:
            # Deleted key (or overshadowed earlier duplicate): drop entry line only, preserve comments/blanks
            node_output[idx] = None
            continue

        matched_keys.add(k)
        desired = by_key[k]
        val_changed = desired["value"] != node["value"]
        comment_changed = desired["comment"] is not None and desired["comment"] != node["comment"]

        if not val_changed and not comment_changed:
            # 100% lossless preservation
            continue

        if comment_changed:
            bound = node["comment_node_indices"]
            new_comment_lines = _format_comment_lines(desired["comment"] or "", indent=node["indent"])
            if bound:
                for c_idx in bound:
                    node_output[c_idx] = None
                node_output[bound[0]] = new_comment_lines
            elif new_comment_lines:
                # Insert new comment lines directly before this entry
                formatted_entry = node["raw"]
                if val_changed:
                    formatted_val = _format_env_value(desired["value"], preferred_quote=node["quote"])
                    inline_sfx = f" {node['inline_comment']}" if node["inline_comment"] else ""
                    formatted_entry = (
                        f"{node['indent']}{node['export_prefix']}{k}={formatted_val}{inline_sfx}"
                    )
                node_output[idx] = [*new_comment_lines, formatted_entry]
                continue

        if val_changed:
            formatted_val = _format_env_value(desired["value"], preferred_quote=node["quote"])
            inline_sfx = f" {node['inline_comment']}" if node["inline_comment"] else ""
            node_output[idx] = [
                f"{node['indent']}{node['export_prefix']}{k}={formatted_val}{inline_sfx}"
            ]

    out_lines: list[str] = []
    for idx in range(len(nodes)):
        chunk = node_output.get(idx)
        if chunk:
            out_lines.extend(chunk)

    # Append brand-new keys in caller order
    new_items = [it for it in normalized_items if it["key"] not in matched_keys]
    if new_items:
        for item in new_items:
            comment_lines = _format_comment_lines(item["comment"] or "")
            if comment_lines and out_lines and out_lines[-1].strip() != "":
                out_lines.append("")
            out_lines.extend(comment_lines)
            formatted_val = _format_env_value(item["value"])
            out_lines.append(f"{item['key']}={formatted_val}")

    if not out_lines:
        return ""
    return newline.join(out_lines) + newline


# ---------------------------------------------------------------------------
# 5. Self-Hub Master Key Guard
# ---------------------------------------------------------------------------


def _enforce_self_hub_protected_keys(
    proj_dir: Path,
    existing_text: str,
    candidate_text: str,
) -> None:
    """Block removing or changing protected keys when editing `Antigravity_Hub`."""
    if not is_self_hub_project(proj_dir):
        return
    if not existing_text:
        return

    existing_entries = {e["key"]: e["value"] for e in parse_dotenv_text(existing_text)["entries"]}
    candidate_entries = {e["key"]: e["value"] for e in parse_dotenv_text(candidate_text)["entries"]}

    for protected_key in sorted(PROTECTED_SELF_HUB_KEYS):
        if protected_key not in existing_entries:
            continue
        disk_val = existing_entries[protected_key]
        if protected_key not in candidate_entries:
            raise ValueError(
                f"Ключ {protected_key} защищён для системного проекта Antigravity_Hub: "
                f"его нельзя удалять, иначе расшифровка hub.db будет повреждена."
            )
        if candidate_entries[protected_key] != disk_val:
            raise ValueError(
                f"Ключ {protected_key} защищён для системного проекта Antigravity_Hub: "
                f"его значение нельзя изменять через веб-редактор, чтобы не сломать расшифровку hub.db."
            )


# ---------------------------------------------------------------------------
# 6. High-Level Read / Save / Sync Operations
# ---------------------------------------------------------------------------


def read_project_env_state(
    proj_dir: Path,
    rel_file: str = ".env",
    *,
    gdrive_vault_active: bool = False,
) -> dict[str, Any]:
    """Read complete UI/API state for a project's `.env` editor."""
    root = Path(proj_dir).resolve(strict=False)
    self_hub = is_self_hub_project(root)
    target, norm_file = resolve_safe_env_target(root, rel_file or ".env", allow_example=False)

    exists = target.is_file()
    raw_content = target.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff") if exists else ""
    parsed = parse_dotenv_text(raw_content)

    entries_out: list[dict[str, Any]] = []
    empty_keys_count = 0
    present_keys: set[str] = set()
    for item in parsed["entries"]:
        key = item["key"]
        val = item["value"]
        is_empty = val == ""
        if is_empty:
            empty_keys_count += 1
        present_keys.add(key)
        entries_out.append(
            {
                "key": key,
                "value": val,
                "comment": item["comment"],
                "is_empty": is_empty,
                "locked": bool(self_hub and key in PROTECTED_SELF_HUB_KEYS),
            }
        )

    example_target, example_rel = get_example_target_for_env(root, norm_file)
    example_exists = example_target.is_file()
    missing_keys: list[dict[str, str]] = []
    if example_exists:
        ex_text = example_target.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
        ex_parsed = parse_dotenv_text(ex_text)
        for ex_item in ex_parsed["entries"]:
            if ex_item["key"] not in present_keys:
                missing_keys.append(
                    {
                        "key": ex_item["key"],
                        "comment": ex_item["comment"],
                    }
                )

    available_files = discover_project_env_files(root)
    # Ensure active file is listed even if it's a newly targeted custom `.env.<suffix>`
    if not any(f["path"] == norm_file for f in available_files):
        available_files.append(
            {
                "path": norm_file,
                "exists": exists,
                "size_bytes": len(raw_content.encode("utf-8")) if exists else 0,
                "updated_at": _format_mtime_iso(target.stat().st_mtime) if exists else "",
                "gitignore_protected": is_env_gitignore_protected(root, norm_file),
            }
        )

    return {
        "is_self_hub": self_hub,
        "active_file": norm_file,
        "available_files": available_files,
        "exists": exists,
        "gitignore_protected": is_env_gitignore_protected(root, norm_file),
        "gdrive_vault_active": bool(gdrive_vault_active),
        "entries": entries_out,
        "raw_content": raw_content,
        "empty_keys_count": empty_keys_count,
        "example": {
            "exists": example_exists,
            "path": example_rel,
            "missing_keys": missing_keys,
        },
        "secret_files": scan_project_secret_files(root),
    }


def save_project_env_file(
    proj_dir: Path,
    *,
    rel_file: str = ".env",
    mode: str = "entries",
    entries: Optional[list[dict[str, Any]]] = None,
    raw_content: Optional[str] = None,
    ensure_gitignore: bool = True,
    expected_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """Atomically save a project's `.env` file in `entries` or `raw` mode with safety guards."""
    root = Path(proj_dir).resolve(strict=False)
    target, norm_file = resolve_safe_env_target(root, rel_file or ".env", allow_example=False)

    path_lock = _get_env_path_lock(target)
    with path_lock:
        raw_existing_bytes = target.read_bytes() if target.is_file() else b""
        current_sha256 = hashlib.sha256(raw_existing_bytes).hexdigest()
        if expected_sha256 is not None:
            clean_expected = str(expected_sha256).strip().lower()
            if clean_expected and clean_expected != current_sha256:
                raise EnvConflictError(
                    "Файл .env был изменён на диске (конфликт версий SHA-256). "
                    "Обновите файл и повторите сохранение."
                )

        existing_text = (
            raw_existing_bytes.decode("utf-8", errors="replace").lstrip("\ufeff")
            if raw_existing_bytes
            else ""
        )

        if mode == "entries":
            if entries is None:
                raise ValueError("Список записей `entries` обязателен для табличного режима.")
            new_text = apply_entries_to_dotenv(existing_text, entries)
        elif mode == "raw":
            if raw_content is None:
                raise ValueError("Поле `raw_content` обязательно для текстового режима.")
            if "\x00" in raw_content:
                raise ValueError("Содержимое .env не может содержать нулевые байты (NUL).")
            new_text = raw_content.lstrip("\ufeff")
            if len(new_text.encode("utf-8")) > MAX_ENV_FILE_BYTES:
                raise ValueError(
                    f"Размер файла превышает допустимый лимит {MAX_ENV_FILE_BYTES // 1024} КБ."
                )
        else:
            raise ValueError(f"Неподдерживаемый режим сохранения: {mode}")

        _enforce_self_hub_protected_keys(root, existing_text, new_text)
        _atomic_write_text_utf8(target, new_text, max_bytes=MAX_ENV_FILE_BYTES)

    gitignore_updated = False
    if ensure_gitignore:
        gitignore_updated = ensure_env_in_gitignore(root, norm_file)

    written_bytes = new_text.encode("utf-8")
    return {
        "ok": True,
        "active_file": norm_file,
        "gitignore_updated": gitignore_updated,
        "bytes_written": len(written_bytes),
        "sha256": hashlib.sha256(written_bytes).hexdigest(),
    }


def _build_sanitized_example_text(env_text: str) -> tuple[str, int]:
    """Generate a `.env.example` template from `.env`, keeping structure/comments and blanking all values."""
    parsed = parse_dotenv_text(env_text or "")
    newline = parsed["newline"]
    out_lines: list[str] = []
    keys_count = 0
    for node in parsed["nodes"]:
        if node["type"] == "entry":
            keys_count += 1
            out_lines.append(f"{node['indent']}{node['export_prefix']}{node['key']}=")
        else:
            out_lines.append(node["raw"])
    if not out_lines:
        return "", 0
    return newline.join(out_lines) + newline, keys_count


def sync_env_example(
    proj_dir: Path,
    *,
    rel_file: str = ".env",
    direction: str = "import_missing",
    ensure_gitignore: bool = True,
) -> dict[str, Any]:
    """Synchronize `.env` with its companion `.env.example`.

    - `import_missing`: appends keys present in `.env.example` but missing in `.env` as empty `KEY=`
      (with their `# comments`), never overwriting or clearing existing `.env` values.
    - `export_template`: creates/updates `.env.example` mirroring `.env` structure & comments with ALL
      values stripped to `KEY=` (0 secret leakage).
    """
    root = Path(proj_dir).resolve(strict=False)
    env_target, norm_env = resolve_safe_env_target(root, rel_file or ".env", allow_example=False)
    example_target, norm_example = get_example_target_for_env(root, norm_env)

    if direction == "import_missing":
        if not example_target.is_file():
            raise ValueError(f"Файл шаблона {norm_example} не найден.")
        ex_text = example_target.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
        ex_parsed = parse_dotenv_text(ex_text)

        env_exists = env_target.is_file()
        env_text = (
            env_target.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
            if env_exists
            else ""
        )
        env_parsed = parse_dotenv_text(env_text)
        existing_keys = {e["key"] for e in env_parsed["entries"]}

        missing = [e for e in ex_parsed["entries"] if e["key"] not in existing_keys]
        added_keys: list[str] = []
        if missing or not env_exists:
            merged_entries = [
                {"key": e["key"], "value": e["value"], "comment": e["comment"]}
                for e in env_parsed["entries"]
            ]
            for item in missing:
                merged_entries.append(
                    {"key": item["key"], "value": "", "comment": item["comment"]}
                )
                added_keys.append(item["key"])
            updated_text = apply_entries_to_dotenv(env_text, merged_entries)
            _enforce_self_hub_protected_keys(root, env_text, updated_text)
            _atomic_write_text_utf8(env_target, updated_text, max_bytes=MAX_ENV_FILE_BYTES)

        gitignore_updated = False
        if ensure_gitignore:
            gitignore_updated = ensure_env_in_gitignore(root, norm_env)

        return {
            "ok": True,
            "direction": "import_missing",
            "active_file": norm_env,
            "example_path": norm_example,
            "added_keys": added_keys,
            "added_count": len(added_keys),
            "gitignore_updated": gitignore_updated,
        }

    if direction == "export_template":
        if not env_target.is_file():
            raise ValueError(f"Файл {norm_env} ещё не создан — нечего выгружать в {norm_example}.")
        env_text = env_target.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
        template_text, exported_count = _build_sanitized_example_text(env_text)
        _atomic_write_text_utf8(example_target, template_text, max_bytes=MAX_ENV_FILE_BYTES)
        return {
            "ok": True,
            "direction": "export_template",
            "active_file": norm_env,
            "example_path": norm_example,
            "exported_keys_count": exported_count,
        }

    raise ValueError(f"Неподдерживаемое направление синхронизации: {direction}")
