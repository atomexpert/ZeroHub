"""Safe project Git checkpoint, diff, rollback, and pre-commit shield service (v=148).

All child subprocesses run hidden (`config.subprocess_hidden_kwargs()`), non-interactive
(`GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`), CPU-only (`CUDA_VISIBLE_DEVICES=""`),
with parent `GIT_*` hook variables stripped so child operations never leak into the Hub repo.
"""
from __future__ import annotations

import fnmatch
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import config

GIT_HOOK_ENV_VARS: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
)

BLOCKED_ANY_LEVEL_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".secrets",
        "private-profiles",
    }
)

BLOCKED_ROOT_DIRS: frozenset[str] = frozenset(
    {
        "data",
        "users",
        "shared_projects",
        "tmp",
        "scratch",
    }
)

ALLOWED_EXACT_NAMES: frozenset[str] = frozenset(
    {
        ".env.example",
    }
)

BLOCKED_NAME_PATTERNS: tuple[str, ...] = (
    ".env*",
    "*.db",
    "*.db-*",
    "*.db*",
    "*.sqlite*",
    "*.session",
    "*.session-*",
    "*.session*",
    "*.pem",
    "*.key",
    "*secret*.json",
    "*credentials*.json",
)

INTERNAL_AUTO_EXCLUDED_PATHS: frozenset[str] = frozenset(
    {
        ".agents/rules/hub_project.md",
    }
)

DEFAULT_SAFE_GITIGNORE: str = (
    "# Antigravity Hub — безопасный базовый .gitignore\n"
    ".env\n"
    ".env.*\n"
    "!.env.example\n"
    ".secrets/\n"
    "private-profiles/\n"
    "*secret*.json\n"
    "*credentials*.json\n"
    "*.db\n"
    "*.db-*\n"
    "*.sqlite*\n"
    "*.session\n"
    "*.session-*\n"
    "*.pem\n"
    "*.key\n"
    ".venv/\n"
    "venv/\n"
    "node_modules/\n"
    "__pycache__/\n"
    "*.py[cod]\n"
    ".pytest_cache/\n"
    "data/\n"
    "users/\n"
    "shared_projects/\n"
    "tmp/\n"
    "scratch/\n"
    ".agents/rules/hub_project.md\n"
)

DEFAULT_GIT_AUTHOR_NAME: str = "Antigravity Hub"
DEFAULT_GIT_AUTHOR_EMAIL: str = "hub@antigravity.local"

_COMMIT_REF_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")

_LOCKS_GUARD = threading.Lock()
_PROJECT_LOCKS: dict[str, threading.RLock] = {}


class GitServiceError(ValueError):
    """Structured error raised by `git_service` with an HTTP-friendly status code."""

    def __init__(self, message: str, *, status_code: int = 400, code: str = "git_error") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class GitCheckpointVerifyError(GitServiceError):
    """Raised when syntax verification or a project pre-commit hook blocks a checkpoint."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=400, code="verify_failed")


def _get_project_lock(resolved_dir: Path) -> threading.RLock:
    key = str(resolved_dir).replace("\\", "/").lower() if config.IS_WINDOWS else str(resolved_dir)
    with _LOCKS_GUARD:
        lock = _PROJECT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROJECT_LOCKS[key] = lock
        return lock


def _validate_project_dir(proj_dir: Path) -> Path:
    if proj_dir is None:
        raise GitServiceError("Путь проекта не указан", status_code=400)
    try:
        resolved = Path(proj_dir).resolve()
    except OSError as exc:
        raise GitServiceError(f"Некорректный путь проекта: {exc}", status_code=400) from exc
    if not resolved.exists() or not resolved.is_dir():
        raise GitServiceError("Папка проекта не найдена на диске", status_code=404)
    return resolved


def normalize_rel_git_path(raw_path: str) -> str:
    p = str(raw_path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.strip("/")
    return p


def is_blocked_git_path(rel_path: str) -> bool:
    """Return True if `rel_path` matches a secret, DB, session, venv, or forbidden runtime folder."""
    norm = normalize_rel_git_path(rel_path)
    if not norm:
        return True
    parts = [seg for seg in norm.split("/") if seg]
    if not parts or any(seg in {".", ".."} for seg in parts):
        return True
    low_norm = "/".join(parts).lower()
    if low_norm in INTERNAL_AUTO_EXCLUDED_PATHS:
        return True
    for seg in parts[:-1]:
        if seg.lower() in BLOCKED_ANY_LEVEL_DIRS:
            return True
    if parts[-1].lower() in BLOCKED_ANY_LEVEL_DIRS:
        return True
    if parts[0].lower() in BLOCKED_ROOT_DIRS:
        return True
    name_low = parts[-1].lower()
    if name_low in ALLOWED_EXACT_NAMES:
        return False
    return any(fnmatch.fnmatchcase(name_low, pat) for pat in BLOCKED_NAME_PATTERNS)


def _build_git_env(
    proj_dir: Optional[Path] = None,
    *,
    author_fallback: bool = False,
) -> dict[str, str]:
    env = dict(os.environ)
    for var in GIT_HOOK_ENV_VARS:
        env.pop(var, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["ORT_DISABLE_ALL_GPU"] = "1"

    if author_fallback and proj_dir is not None:
        rc_n, out_n, _ = _exec_git_raw(proj_dir, ["config", "--get", "user.name"], env=env, timeout=5.0)
        cfg_has_name = rc_n == 0 and bool(out_n.strip())
        rc_e, out_e, _ = _exec_git_raw(proj_dir, ["config", "--get", "user.email"], env=env, timeout=5.0)
        cfg_has_email = rc_e == 0 and bool(out_e.strip())

        if not cfg_has_name:
            if not env.get("GIT_AUTHOR_NAME", "").strip():
                env["GIT_AUTHOR_NAME"] = DEFAULT_GIT_AUTHOR_NAME
            if not env.get("GIT_COMMITTER_NAME", "").strip():
                env["GIT_COMMITTER_NAME"] = env["GIT_AUTHOR_NAME"]
        if not cfg_has_email:
            if not env.get("GIT_AUTHOR_EMAIL", "").strip():
                env["GIT_AUTHOR_EMAIL"] = DEFAULT_GIT_AUTHOR_EMAIL
            if not env.get("GIT_COMMITTER_EMAIL", "").strip():
                env["GIT_COMMITTER_EMAIL"] = env["GIT_AUTHOR_EMAIL"]
    return env


def _resolve_git_bin() -> str:
    git_bin = shutil.which("git") or (shutil.which("git.exe") if config.IS_WINDOWS else None)
    if not git_bin:
        raise GitServiceError("На сервере не установлен Git CLI", status_code=503)
    return git_bin


def _exec_git_raw(
    cwd: Path,
    args: list[str],
    *,
    env: Optional[dict[str, str]] = None,
    timeout: float = 20.0,
) -> tuple[int, str, str]:
    git_bin = _resolve_git_bin()
    eff_env = env if env is not None else _build_git_env(cwd)
    try:
        proc = subprocess.run(
            [git_bin, "-c", "core.quotepath=false", *args],
            cwd=str(cwd),
            env=eff_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            **config.subprocess_hidden_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise GitServiceError(
            f"Превышено время ожидания команды git ({args[0] if args else 'git'})",
            status_code=504,
        ) from exc
    except OSError as exc:
        raise GitServiceError(f"Ошибка запуска git: {exc}", status_code=500) from exc
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode, stdout, stderr


def _exec_git_bytes(
    cwd: Path,
    args: list[str],
    *,
    env: Optional[dict[str, str]] = None,
    timeout: float = 20.0,
) -> tuple[int, bytes, str]:
    git_bin = _resolve_git_bin()
    eff_env = env if env is not None else _build_git_env(cwd)
    try:
        proc = subprocess.run(
            [git_bin, "-c", "core.quotepath=false", *args],
            cwd=str(cwd),
            env=eff_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            **config.subprocess_hidden_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise GitServiceError(
            f"Превышено время ожидания команды git ({args[0] if args else 'git'})",
            status_code=504,
        ) from exc
    except OSError as exc:
        raise GitServiceError(f"Ошибка запуска git: {exc}", status_code=500) from exc
    stderr = proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode, proc.stdout, stderr


def _is_own_git_repo(resolved_dir: Path) -> bool:
    git_marker = resolved_dir / ".git"
    if not git_marker.exists():
        return False
    rc, out, _ = _exec_git_raw(resolved_dir, ["rev-parse", "--show-toplevel"], timeout=5.0)
    if rc != 0 or not out.strip():
        return False
    try:
        top = Path(out.strip()).resolve()
    except OSError:
        return False
    if not config._same_fs_path(top, resolved_dir):
        return False
    rc_st, _, _ = _exec_git_raw(resolved_dir, ["status", "--porcelain=v1"], timeout=8.0)
    return rc_st == 0


def _ensure_git_info_exclude(resolved_dir: Path) -> None:
    try:
        info_dir = resolved_dir / ".git" / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        exclude_file = info_dir / "exclude"
        existing = ""
        if exclude_file.is_file():
            existing = exclude_file.read_text(encoding="utf-8", errors="replace")
        existing_lines = {ln.strip() for ln in existing.splitlines()}
        rule = ".agents/rules/hub_project.md"
        if rule not in existing_lines:
            prefix = "" if (not existing or existing.endswith("\n")) else "\n"
            with exclude_file.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(f"{prefix}{rule}\n")
    except OSError:
        pass


def ensure_project_git_ready(proj_dir: Path) -> dict[str, Any]:
    """Idempotently initialize `.git` inside `proj_dir` and create a safe `.gitignore` if absent."""
    resolved = _validate_project_dir(proj_dir)
    with _get_project_lock(resolved):
        git_created = False
        if not _is_own_git_repo(resolved):
            rc, _, err = _exec_git_raw(resolved, ["init", "-q"], timeout=10.0)
            if rc != 0 or not _is_own_git_repo(resolved):
                raise GitServiceError(
                    f"Не удалось инициализировать Git-репозиторий проекта: {err.strip() or 'git init failed'}",
                    status_code=500,
                )
            git_created = True

        _ensure_git_info_exclude(resolved)

        gitignore_path = resolved / ".gitignore"
        gitignore_created = False
        is_self_hub = config._same_fs_path(resolved, config.BASE_DIR.resolve())
        if not is_self_hub and not gitignore_path.exists():
            try:
                gitignore_path.write_text(DEFAULT_SAFE_GITIGNORE, encoding="utf-8", newline="\n")
                gitignore_created = True
            except OSError as exc:
                raise GitServiceError(f"Не удалось создать .gitignore: {exc}", status_code=500) from exc

        return {
            "ok": True,
            "initialized": True,
            "git_created": git_created,
            "gitignore_created": gitignore_created,
            "gitignore_exists": gitignore_path.is_file(),
        }


def _detect_project_shield(resolved_dir: Path, *, repo_initialized: bool) -> tuple[bool, str]:
    has_hub_verifier = (resolved_dir / "tools" / "verify_hub.py").is_file()
    has_proj_verifier = (resolved_dir / "tools" / "verify_project.py").is_file()
    if not has_hub_verifier and not has_proj_verifier:
        return False, "none"

    kind = "hub" if has_hub_verifier else "project"
    if not repo_initialized:
        return False, kind

    hook_active = False
    rc, hooks_out, _ = _exec_git_raw(resolved_dir, ["config", "--get", "core.hooksPath"], timeout=5.0)
    hooks_cfg = normalize_rel_git_path(hooks_out) if rc == 0 else ""
    if hooks_cfg:
        hook_cand = (resolved_dir / hooks_cfg / "pre-commit")
        if hook_cand.is_file():
            try:
                hook_txt = hook_cand.read_text(encoding="utf-8", errors="replace")
                if ("verify_hub.py" if kind == "hub" else "verify_project.py") in hook_txt:
                    hook_active = True
            except OSError:
                hook_active = False
    else:
        legacy_hook = resolved_dir / ".git" / "hooks" / "pre-commit"
        if legacy_hook.is_file():
            try:
                hook_txt = legacy_hook.read_text(encoding="utf-8", errors="replace")
                if ("verify_hub.py" if kind == "hub" else "verify_project.py") in hook_txt:
                    hook_active = True
            except OSError:
                hook_active = False

    return hook_active, kind


def _parse_porcelain_z(raw_bytes: bytes) -> list[tuple[str, str, str]]:
    """Parse `git status --porcelain=v1 -z` into `(xy, path, orig_path_if_renamed)` tuples."""
    if not raw_bytes:
        return []
    tokens = raw_bytes.split(b"\x00")
    entries: list[tuple[str, str, str]] = []
    idx = 0
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        idx += 1
        if len(tok) < 4 or tok[2:3] != b" ":
            continue
        xy = tok[:2].decode("ascii", errors="replace")
        path = normalize_rel_git_path(tok[3:].decode("utf-8", errors="replace"))
        orig_path = ""
        if ("R" in xy or "C" in xy) and idx < n:
            orig_path = normalize_rel_git_path(tokens[idx].decode("utf-8", errors="replace"))
            idx += 1
        if path:
            entries.append((xy, path, orig_path))
    return entries


def _format_status_code(xy: str) -> str:
    if xy == "??":
        return "??"
    stripped = xy.strip()
    return stripped or "M"


def _read_checkpoints_log(resolved_dir: Path, limit: int) -> list[dict[str, Any]]:
    eff_limit = max(1, min(int(limit or 15), 100))
    marker = "__AGY_CP__"
    fmt = f"{marker}%x1f%H%x1f%h%x1f%s%x1f%an%x1f%ct%x1f%cI"
    rc, out, _ = _exec_git_raw(
        resolved_dir,
        ["log", f"-n{eff_limit}", f"--pretty=format:{fmt}", "--numstat"],
        timeout=12.0,
    )
    if rc != 0 or not out.strip():
        return []

    checkpoints: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    for raw_line in out.splitlines():
        line = raw_line.strip("\r")
        if line.startswith(f"{marker}\x1f"):
            if current is not None:
                checkpoints.append(current)
            parts = line.split("\x1f")
            if len(parts) < 7:
                current = None
                continue
            _, sha, short_sha, subject, author, ts_raw, iso_date = parts[:7]
            try:
                ts_int = int(ts_raw.strip())
            except ValueError:
                ts_int = 0
            current = {
                "sha": sha.strip(),
                "short_sha": short_sha.strip() or sha.strip()[:7],
                "subject": subject.strip(),
                "author": author.strip(),
                "timestamp": ts_int,
                "iso_date": iso_date.strip(),
                "files_changed": 0,
            }
        elif current is not None and line.strip():
            if "\t" in line:
                current["files_changed"] += 1
    if current is not None:
        checkpoints.append(current)
    return checkpoints


def _get_head_sha(resolved_dir: Path) -> str:
    rc, out, _ = _exec_git_raw(resolved_dir, ["rev-parse", "--verify", "--quiet", "HEAD"], timeout=5.0)
    if rc != 0:
        return ""
    return out.strip()


def _get_current_branch(resolved_dir: Path) -> str:
    rc, out, _ = _exec_git_raw(resolved_dir, ["symbolic-ref", "--quiet", "--short", "HEAD"], timeout=5.0)
    if rc == 0 and out.strip():
        return out.strip()
    rc2, out2, _ = _exec_git_raw(resolved_dir, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=5.0)
    if rc2 == 0 and out2.strip():
        return out2.strip()
    return "main"


def _count_stashes(resolved_dir: Path) -> int:
    rc, out, _ = _exec_git_raw(resolved_dir, ["stash", "list"], timeout=6.0)
    if rc != 0 or not out.strip():
        return 0
    return sum(1 for ln in out.splitlines() if ln.strip())


def get_project_git_status(proj_dir: Path, limit: int = 15) -> dict[str, Any]:
    """Return structured Git status, safe changed files, checkpoints history, stashes, and shield state."""
    resolved = _validate_project_dir(proj_dir)
    initialized = _is_own_git_repo(resolved)
    shield_installed, shield_kind = _detect_project_shield(resolved, repo_initialized=initialized)

    if not initialized:
        return {
            "initialized": False,
            "branch": "",
            "head_sha": "",
            "head_short": "",
            "is_clean": True,
            "changed_count": 0,
            "changed_files": [],
            "ignored_or_blocked_count": 0,
            "checkpoints": [],
            "stashes_count": 0,
            "shield_installed": shield_installed,
            "shield_kind": shield_kind,
        }

    branch = _get_current_branch(resolved)
    head_sha = _get_head_sha(resolved)
    head_short = head_sha[:7] if head_sha else ""

    rc_st, raw_st, _ = _exec_git_bytes(
        resolved,
        ["status", "--porcelain=v1", "-z", "-uall", "--ignored=matching"],
        timeout=12.0,
    )
    entries = _parse_porcelain_z(raw_st) if rc_st == 0 else []

    changed_files: list[dict[str, Any]] = []
    ignored_or_blocked_count = 0
    for xy, rel_path, orig_path in entries:
        low_path = rel_path.lower()
        if low_path in INTERNAL_AUTO_EXCLUDED_PATHS:
            continue
        if xy == "!!":
            # Count ignored secrets/databases/files without inflating on standard .git/ internals
            ignored_or_blocked_count += 1
            continue
        if is_blocked_git_path(rel_path) or (orig_path and is_blocked_git_path(orig_path)):
            ignored_or_blocked_count += 1
            continue
        staged = xy[0] not in (" ", "?", "!")
        changed_files.append(
            {
                "path": rel_path,
                "status": _format_status_code(xy),
                "staged": staged,
            }
        )

    checkpoints = _read_checkpoints_log(resolved, limit=limit) if head_sha else []
    stashes_count = _count_stashes(resolved)

    return {
        "initialized": True,
        "branch": branch,
        "head_sha": head_sha,
        "head_short": head_short,
        "is_clean": len(changed_files) == 0,
        "changed_count": len(changed_files),
        "changed_files": changed_files,
        "ignored_or_blocked_count": ignored_or_blocked_count,
        "checkpoints": checkpoints,
        "stashes_count": stashes_count,
        "shield_installed": shield_installed,
        "shield_kind": shield_kind,
    }


def _resolve_verified_commit_sha(resolved_dir: Path, raw_commit: str) -> str:
    clean = str(raw_commit or "").strip()
    if not clean or clean.startswith("-") or not _COMMIT_REF_RE.fullmatch(clean):
        raise GitServiceError("Некорректный идентификатор точки сохранения (SHA)", status_code=400)
    rc, out, _ = _exec_git_raw(
        resolved_dir,
        ["rev-parse", "--verify", "--quiet", f"{clean}^{{commit}}"],
        timeout=5.0,
    )
    if rc != 0 or not out.strip():
        raise GitServiceError(f"Точка сохранения '{clean[:12]}' не найдена в истории проекта", status_code=404)
    return out.strip()


def _filter_patch_sections_against_blocked_paths(patch_text: str) -> str:
    """Strip any `diff --git a/... b/...` block touching a blocked secret/DB path."""
    if not patch_text or "diff --git " not in patch_text:
        return patch_text
    chunks = re.split(r"(?=^diff --git )", patch_text, flags=re.MULTILINE)
    kept: list[str] = []
    hdr_re = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)
    for chunk in chunks:
        if not chunk.startswith("diff --git "):
            kept.append(chunk)
            continue
        m = hdr_re.match(chunk)
        if m:
            p_a = normalize_rel_git_path(m.group(1))
            p_b = normalize_rel_git_path(m.group(2))
            if is_blocked_git_path(p_a) or is_blocked_git_path(p_b):
                continue
        kept.append(chunk)
    return "".join(kept)


def _build_untracked_file_preview(resolved_dir: Path, rel_path: str, per_file_cap: int = 8000) -> str:
    target = (resolved_dir / rel_path).resolve()
    try:
        target.relative_to(resolved_dir)
    except ValueError:
        return ""
    if not target.is_file() or config._is_junction_or_symlink(target):
        return ""
    try:
        size = target.stat().st_size
        with target.open("rb") as fh:
            sample = fh.read(per_file_cap + 512)
    except OSError:
        return ""
    if b"\x00" in sample:
        return f"diff --git a/{rel_path} b/{rel_path}\nnew file mode 100644\nBinary file {rel_path} ({size} bytes)\n"
    text = sample[:per_file_cap].decode("utf-8", errors="replace")
    lines = text.splitlines()
    plus_body = "\n".join(f"+{ln}" for ln in lines)
    trunc_note = "\n+... [превью нового файла сокращено]" if len(sample) > per_file_cap else ""
    line_count = max(1, len(lines))
    return (
        f"diff --git a/{rel_path} b/{rel_path}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n"
        f"+++ b/{rel_path}\n"
        f"@@ -0,0 +1,{line_count} @@\n"
        f"{plus_body}{trunc_note}\n"
    )


def get_project_git_diff(
    proj_dir: Path,
    commit: Optional[str] = None,
    path: Optional[str] = None,
    max_chars: int = 48000,
) -> dict[str, Any]:
    """Return bounded diff for the working tree (`commit=None`) or a specific commit (`commit=<sha>`)."""
    resolved = _validate_project_dir(proj_dir)
    eff_max = max(512, min(int(max_chars or 48000), 250_000))

    filter_rel = ""
    if path is not None and str(path).strip():
        filter_rel = normalize_rel_git_path(path)
        if not filter_rel or ".." in filter_rel.split("/"):
            raise GitServiceError("Недопустимый относительный путь файла", status_code=400)
        if is_blocked_git_path(filter_rel):
            return {
                "ok": True,
                "diff": "",
                "truncated": False,
                "commit": commit or None,
                "blocked": True,
            }

    if not _is_own_git_repo(resolved):
        return {
            "ok": True,
            "diff": "",
            "truncated": False,
            "commit": None,
        }

    if commit is not None and str(commit).strip():
        sha = _resolve_verified_commit_sha(resolved, str(commit))
        show_args = ["show", "--stat", "--patch", sha]
        if filter_rel:
            show_args.extend(["--", filter_rel])
        rc, out, err = _exec_git_raw(resolved, show_args, timeout=15.0)
        if rc != 0:
            raise GitServiceError(f"Не удалось прочитать дифф коммита: {err.strip()}", status_code=400)
        cleaned = _filter_patch_sections_against_blocked_paths(out)
        truncated = len(cleaned) > eff_max
        if truncated:
            cleaned = cleaned[:eff_max] + "\n... [вывод диффа обрезан по лимиту]"
        return {
            "ok": True,
            "diff": cleaned,
            "truncated": truncated,
            "commit": sha,
        }

    # Working tree diff (`commit=None`)
    rc_st, raw_st, _ = _exec_git_bytes(resolved, ["status", "--porcelain=v1", "-z", "-uall"], timeout=12.0)
    entries = _parse_porcelain_z(raw_st) if rc_st == 0 else []

    tracked_paths: list[str] = []
    untracked_paths: list[str] = []
    for xy, rel_path, orig_path in entries:
        if is_blocked_git_path(rel_path) or (orig_path and is_blocked_git_path(orig_path)):
            continue
        if filter_rel and rel_path != filter_rel and orig_path != filter_rel:
            continue
        if xy == "??":
            untracked_paths.append(rel_path)
        else:
            if rel_path not in tracked_paths:
                tracked_paths.append(rel_path)
            if orig_path and orig_path not in tracked_paths:
                tracked_paths.append(orig_path)

    diff_parts: list[str] = []
    head_sha = _get_head_sha(resolved)
    if tracked_paths:
        # Chunk pathspecs safely
        for start in range(0, len(tracked_paths), 60):
            batch = tracked_paths[start : start + 60]
            if head_sha:
                rc_d, out_d, _ = _exec_git_raw(
                    resolved,
                    ["diff", "--no-ext-diff", "HEAD", "--", *batch],
                    timeout=15.0,
                )
                # Replace binary patch payloads cleanly while keeping `Binary files ... differ`
                if rc_d == 0 and out_d.strip():
                    diff_parts.append(_filter_patch_sections_against_blocked_paths(out_d))
            else:
                for base_cmd in (["diff", "--no-ext-diff", "--cached", "--", *batch], ["diff", "--no-ext-diff", "--", *batch]):
                    rc_d, out_d, _ = _exec_git_raw(resolved, base_cmd, timeout=15.0)
                    if rc_d == 0 and out_d.strip():
                        diff_parts.append(_filter_patch_sections_against_blocked_paths(out_d))

    current_len = sum(len(part) for part in diff_parts)
    for u_rel in untracked_paths:
        if current_len >= eff_max + 4096:
            break
        preview = _build_untracked_file_preview(resolved, u_rel)
        if preview:
            diff_parts.append(preview)
            current_len += len(preview)

    combined = "\n".join(p.rstrip("\n") for p in diff_parts if p.strip())
    if combined:
        combined += "\n"
    truncated = len(combined) > eff_max
    if truncated:
        combined = combined[:eff_max] + "\n... [вывод диффа обрезан по лимиту]"

    return {
        "ok": True,
        "diff": combined,
        "truncated": truncated,
        "commit": None,
    }


def _verify_project_files_syntax(resolved_dir: Path, rel_paths: list[str]) -> None:
    """Fast UTF-8 + `py_compile` (`.py`) + `node --check` (`.js`/`.mjs`/`.cjs`) check before checkpoint."""
    py_targets: list[tuple[str, Path]] = []
    js_targets: list[tuple[str, Path]] = []

    for rel in rel_paths:
        full_p = (resolved_dir / rel).resolve()
        try:
            full_p.relative_to(resolved_dir)
        except ValueError as exc:
            raise GitCheckpointVerifyError(f"Выход пути за пределы проекта: {rel}") from exc
        if not full_p.exists() or not full_p.is_file() or config._is_junction_or_symlink(full_p):
            continue
        suf = full_p.suffix.lower()
        if suf in {".py", ".js", ".mjs", ".cjs"}:
            try:
                raw = full_p.read_bytes()
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GitCheckpointVerifyError(f"Файл '{rel}' не является валидным UTF-8: {exc}") from exc
            if text.startswith("\ufeff") or "\ufffd" in text:
                raise GitCheckpointVerifyError(
                    f"Файл '{rel}' содержит UTF-8 BOM или повреждённый символ U+FFFD"
                )
        if suf == ".py":
            py_targets.append((rel, full_p))
        elif suf in {".js", ".mjs", ".cjs"}:
            js_targets.append((rel, full_p))

    if py_targets:
        with tempfile.TemporaryDirectory(prefix="agy_cp_pyc_") as tmp_pyc:
            for idx, (rel, full_p) in enumerate(py_targets):
                try:
                    py_compile.compile(
                        str(full_p),
                        cfile=str(Path(tmp_pyc) / f"chk_{idx}.pyc"),
                        doraise=True,
                    )
                except py_compile.PyCompileError as exc:
                    raise GitCheckpointVerifyError(
                        f"Синтаксическая ошибка Python в '{rel}': {exc.msg}"
                    ) from exc

    if js_targets:
        node_bin = config._find_node_bin()
        if node_bin:
            clean_env = _build_git_env(resolved_dir)
            for rel, full_p in js_targets:
                try:
                    proc = subprocess.run(
                        [node_bin, "--check", str(full_p)],
                        cwd=str(resolved_dir),
                        env=clean_env,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        timeout=15.0,
                        **config.subprocess_hidden_kwargs(),
                    )
                except subprocess.TimeoutExpired as exc:
                    raise GitCheckpointVerifyError(
                        f"Превышено время проверки синтаксиса JS для '{rel}'"
                    ) from exc
                if proc.returncode != 0:
                    err_txt = (proc.stderr or proc.stdout).decode("utf-8", errors="replace").strip()
                    first_err = "\n".join(err_txt.splitlines()[:4])
                    raise GitCheckpointVerifyError(
                        f"Синтаксическая ошибка JavaScript в '{rel}': {first_err}"
                    )


def _build_auto_checkpoint_message(rel_paths: list[str]) -> str:
    count = len(rel_paths)
    names = [Path(p).name for p in rel_paths[:3]]
    preview = ", ".join(names) + (", ..." if count > 3 else "")
    mod10 = count % 10
    mod100 = count % 100
    if mod10 == 1 and mod100 != 11:
        verb_noun = f"обновлён {count} файл"
    elif 2 <= mod10 <= 4 and not (12 <= mod100 <= 14):
        verb_noun = f"обновлено {count} файла"
    else:
        verb_noun = f"обновлено {count} файлов"
    return f"Точка сохранения: {verb_noun} ({preview})"


def _unstage_paths(resolved_dir: Path, paths_to_unstage: list[str], *, has_head: bool) -> None:
    if not paths_to_unstage:
        return
    for start in range(0, len(paths_to_unstage), 60):
        batch = paths_to_unstage[start : start + 60]
        if has_head:
            _exec_git_raw(resolved_dir, ["reset", "-q", "HEAD", "--", *batch], timeout=10.0)
        else:
            _exec_git_raw(resolved_dir, ["rm", "-r", "--cached", "-q", "--ignore-unmatch", "--", *batch], timeout=10.0)


def create_project_checkpoint(
    proj_dir: Path,
    message: str = "",
    paths: Optional[list[str]] = None,
    run_verify: bool = True,
) -> dict[str, Any]:
    """Create a safe Git checkpoint commit of allowed project files, blocking secrets/DBs and broken syntax."""
    resolved = _validate_project_dir(proj_dir)
    with _get_project_lock(resolved):
        ensure_project_git_ready(resolved)
        head_before = _get_head_sha(resolved)

        rc_st, raw_st, err_st = _exec_git_bytes(
            resolved,
            ["status", "--porcelain=v1", "-z", "-uall"],
            timeout=15.0,
        )
        if rc_st != 0:
            raise GitServiceError(f"Не удалось прочитать статус Git: {err_st.strip()}", status_code=500)

        entries = _parse_porcelain_z(raw_st)
        staged_blocked: list[str] = []
        available_paths: list[str] = []
        rename_pairs: dict[str, str] = {}

        for xy, rel_path, orig_path in entries:
            if is_blocked_git_path(rel_path) or (orig_path and is_blocked_git_path(orig_path)):
                if xy[0] not in (" ", "?", "!"):
                    staged_blocked.append(rel_path)
                    if orig_path:
                        staged_blocked.append(orig_path)
                continue
            if rel_path not in available_paths:
                available_paths.append(rel_path)
            if orig_path:
                rename_pairs[rel_path] = orig_path

        if staged_blocked:
            _unstage_paths(resolved, staged_blocked, has_head=bool(head_before))

        if paths is not None:
            requested_norm: list[str] = []
            for raw_p in paths:
                np = normalize_rel_git_path(raw_p)
                if not np:
                    continue
                if ".." in np.split("/"):
                    raise GitServiceError(f"Недопустимый путь файла: {raw_p}", status_code=400)
                if is_blocked_git_path(np):
                    continue
                if np not in requested_norm:
                    requested_norm.append(np)
            avail_set = set(available_paths)
            selected_paths = [p for p in requested_norm if p in avail_set]
        else:
            selected_paths = list(available_paths)

        if not selected_paths:
            return {
                "ok": True,
                "created": False,
                "reason": "clean",
                "files_count": 0,
                "files": [],
            }

        if run_verify:
            _verify_project_files_syntax(resolved, selected_paths)

        stage_targets: list[str] = []
        for p in selected_paths:
            if p not in stage_targets:
                stage_targets.append(p)
            orig = rename_pairs.get(p)
            if orig and orig not in stage_targets and not is_blocked_git_path(orig):
                stage_targets.append(orig)

        # Reset index to HEAD first when explicit `paths` subset is requested so unrelated staged edits aren't swept in
        if paths is not None:
            rc_idx, raw_idx, _ = _exec_git_bytes(resolved, ["diff", "--cached", "--name-only", "-z"], timeout=10.0)
            if rc_idx == 0 and raw_idx:
                currently_staged = [normalize_rel_git_path(x.decode("utf-8", errors="replace")) for x in raw_idx.split(b"\x00") if x]
                unrelated = [p for p in currently_staged if p and p not in set(stage_targets)]
                _unstage_paths(resolved, unrelated, has_head=bool(head_before))

        for start in range(0, len(stage_targets), 60):
            batch = stage_targets[start : start + 60]
            rc_add, _, err_add = _exec_git_raw(resolved, ["add", "-A", "--", *batch], timeout=20.0)
            if rc_add != 0:
                raise GitServiceError(f"Не удалось проиндексировать файлы проекта: {err_add.strip()}", status_code=500)

        rc_cached, raw_cached, _ = _exec_git_bytes(resolved, ["diff", "--cached", "--name-only", "-z"], timeout=10.0)
        staged_final = [
            normalize_rel_git_path(tok.decode("utf-8", errors="replace"))
            for tok in (raw_cached.split(b"\x00") if rc_cached == 0 and raw_cached else [])
            if tok
        ]
        # Hard invariant: never allow a blocked path in the commit index
        leaked_blocked = [p for p in staged_final if is_blocked_git_path(p)]
        if leaked_blocked:
            _unstage_paths(resolved, leaked_blocked, has_head=bool(head_before))
            staged_final = [p for p in staged_final if not is_blocked_git_path(p)]

        if not staged_final:
            return {
                "ok": True,
                "created": False,
                "reason": "clean",
                "files_count": 0,
                "files": [],
            }

        clean_msg = re.sub(r"[\r\n]+", " ", str(message or "").strip())[:240]
        commit_subject = clean_msg or _build_auto_checkpoint_message(staged_final)

        commit_env = _build_git_env(resolved, author_fallback=True)
        rc_cm, out_cm, err_cm = _exec_git_raw(
            resolved,
            ["commit", "-m", commit_subject],
            env=commit_env,
            timeout=120.0,
        )
        if rc_cm != 0:
            _unstage_paths(resolved, staged_final, has_head=bool(head_before))
            combined_err = (err_cm.strip() or out_cm.strip() or "git commit rejected").strip()
            raise GitCheckpointVerifyError(
                f"Точка сохранения отклонена проверкой проекта: {combined_err[:800]}"
            )

        new_sha = _get_head_sha(resolved)
        return {
            "ok": True,
            "created": True,
            "sha": new_sha,
            "short_sha": new_sha[:7],
            "message": commit_subject,
            "files_count": len(staged_final),
            "files": staged_final,
        }


def rollback_project_to_checkpoint(
    proj_dir: Path,
    mode: str,
    target_sha: str = "",
) -> dict[str, Any]:
    """Safely roll back uncommitted changes or restore a checkpoint with automatic `git stash` backup."""
    resolved = _validate_project_dir(proj_dir)
    if config._same_fs_path(resolved, config.BASE_DIR.resolve()):
        raise GitServiceError(
            "Откат ядра живого сервера Antigravity Hub выполняйте вручную при остановленном сервере",
            status_code=400,
            code="self_hub_protected",
        )

    clean_mode = str(mode or "").strip()
    if clean_mode not in {"discard_uncommitted", "restore_checkpoint"}:
        raise GitServiceError(
            "Недопустимый режим отката (ожидается 'discard_uncommitted' или 'restore_checkpoint')",
            status_code=400,
        )

    with _get_project_lock(resolved):
        if not _is_own_git_repo(resolved):
            raise GitServiceError("В проекте ещё не инициализирован Git-репозиторий", status_code=400)

        head_before = _get_head_sha(resolved)
        if not head_before:
            raise GitServiceError(
                "В проекте ещё нет ни одной точки сохранения (HEAD отсутствует)",
                status_code=400,
            )

        verified_target_sha = ""
        if clean_mode == "restore_checkpoint":
            if not str(target_sha or "").strip():
                raise GitServiceError("Не указан SHA целевой точки сохранения (target_sha)", status_code=400)
            verified_target_sha = _resolve_verified_commit_sha(resolved, target_sha)

        # Collect dirty non-blocked paths so `.env` / `*.db` / `*.session` stay untouched on disk
        rc_st, raw_st, _ = _exec_git_bytes(resolved, ["status", "--porcelain=v1", "-z", "-uall"], timeout=15.0)
        entries = _parse_porcelain_z(raw_st) if rc_st == 0 else []
        stashable_paths: list[str] = []
        staged_blocked: list[str] = []
        for xy, rel_path, orig_path in entries:
            if is_blocked_git_path(rel_path) or (orig_path and is_blocked_git_path(orig_path)):
                if xy[0] not in (" ", "?", "!"):
                    staged_blocked.append(rel_path)
                    if orig_path:
                        staged_blocked.append(orig_path)
                continue
            if rel_path not in stashable_paths:
                stashable_paths.append(rel_path)
            if orig_path and orig_path not in stashable_paths:
                stashable_paths.append(orig_path)

        if staged_blocked:
            _unstage_paths(resolved, staged_blocked, has_head=True)

        backup_stash_created = False
        stash_label = ""
        if stashable_paths:
            ts_tag = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            stash_label = f"agy-hub-backup-{ts_tag}-{int(time.time() * 1000) % 1000:03d}"
            stash_env = _build_git_env(resolved, author_fallback=True)
            stashes_before = _count_stashes(resolved)
            rc_sp, out_sp, err_sp = _exec_git_raw(
                resolved,
                ["stash", "push", "-u", "-m", stash_label, "--", *stashable_paths],
                env=stash_env,
                timeout=30.0,
            )
            if rc_sp != 0:
                raise GitServiceError(
                    f"Не удалось сохранить резервную копию в git stash перед откатом: {err_sp.strip() or out_sp.strip()}",
                    status_code=500,
                )
            stashes_after = _count_stashes(resolved)
            backup_stash_created = stashes_after > stashes_before

        if clean_mode == "discard_uncommitted":
            head_now = _get_head_sha(resolved)
            return {
                "ok": True,
                "mode": clean_mode,
                "head_sha": head_now,
                "head_short": head_now[:7],
                "backup_stash_created": backup_stash_created,
                "stash_label": stash_label if backup_stash_created else "",
            }

        # `mode == "restore_checkpoint"`: bring index + tracked worktree to exact `verified_target_sha`
        # without rewriting branch history (`HEAD` stays at `head_before`), then create a new forward commit!
        rc_res, _, err_res = _exec_git_raw(
            resolved,
            ["restore", f"--source={verified_target_sha}", "--staged", "--worktree", "--no-overlay", "--", "."],
            timeout=30.0,
        )
        if rc_res != 0:
            # Fallback for older Git binaries without `restore --no-overlay`
            rc_rt, _, err_rt = _exec_git_raw(
                resolved,
                ["read-tree", "--reset", "-u", verified_target_sha],
                timeout=30.0,
            )
            if rc_rt != 0:
                raise GitServiceError(
                    f"Не удалось восстановить дерево файлов из точки {verified_target_sha[:7]}: {err_rt.strip() or err_res.strip()}",
                    status_code=500,
                )

        # Ensure no blocked paths were ever present in old commit index
        rc_c, raw_c, _ = _exec_git_bytes(resolved, ["diff", "--cached", "--name-only", "-z"], timeout=10.0)
        if rc_c == 0 and raw_c:
            staged_after_restore = [
                normalize_rel_git_path(x.decode("utf-8", errors="replace"))
                for x in raw_c.split(b"\x00")
                if x
            ]
            blocked_restored = [p for p in staged_after_restore if is_blocked_git_path(p)]
            if blocked_restored:
                _unstage_paths(resolved, blocked_restored, has_head=True)

        rc_diff_idx, _, _ = _exec_git_raw(resolved, ["diff", "--cached", "--quiet"], timeout=10.0)
        if rc_diff_idx == 0:
            return {
                "ok": True,
                "mode": clean_mode,
                "target_sha": verified_target_sha,
                "target_short": verified_target_sha[:7],
                "head_sha": head_before,
                "head_short": head_before[:7],
                "commit_created": False,
                "backup_stash_created": backup_stash_created,
                "stash_label": stash_label if backup_stash_created else "",
            }

        restore_subject = f"Возврат к точке сохранения {verified_target_sha[:7]}"
        commit_env = _build_git_env(resolved, author_fallback=True)
        rc_cm, out_cm, err_cm = _exec_git_raw(
            resolved,
            ["commit", "-m", restore_subject],
            env=commit_env,
            timeout=120.0,
        )
        if rc_cm != 0:
            combined_err = (err_cm.strip() or out_cm.strip() or "git commit failed").strip()
            raise GitServiceError(
                f"Не удалось зафиксировать коммит возврата: {combined_err[:800]}",
                status_code=400,
            )

        new_head = _get_head_sha(resolved)
        return {
            "ok": True,
            "mode": clean_mode,
            "target_sha": verified_target_sha,
            "target_short": verified_target_sha[:7],
            "head_sha": new_head,
            "head_short": new_head[:7],
            "message": restore_subject,
            "commit_created": True,
            "backup_stash_created": backup_stash_created,
            "stash_label": stash_label if backup_stash_created else "",
        }


_VERIFY_PROJECT_SCRIPT_TEMPLATE: str = '''"""Universal project pre-commit & verification shield installed by Antigravity Hub (v=148)."""
from __future__ import annotations

import argparse
import fnmatch
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_NAME_PATTERNS = (
    ".env*",
    "*.db",
    "*.db-*",
    "*.db*",
    "*.sqlite*",
    "*.session",
    "*.session-*",
    "*.session*",
    "*.pem",
    "*.key",
    "*secret*.json",
    "*credentials*.json",
)
FORBIDDEN_ANY_LEVEL_DIRS = {".secrets", "private-profiles"}
ALLOWED_EXACT_NAMES = {".env.example"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".secrets", "private-profiles", "data", "tmp", "scratch"}
GIT_HOOK_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
)


def _hidden_kwargs() -> dict:
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        "startupinfo": si,
    }


def _clean_child_env() -> dict[str, str]:
    env = dict(os.environ)
    for var in GIT_HOOK_ENV_VARS:
        env.pop(var, None)
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["ORT_DISABLE_ALL_GPU"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _normalize(path_str: str) -> str:
    p = str(path_str or "").strip().replace("\\\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


def is_forbidden_staged_path(rel_path: str) -> bool:
    norm = _normalize(rel_path)
    if not norm:
        return False
    if norm.lower() == ".agents/rules/hub_project.md":
        return True
    parts = [seg for seg in norm.split("/") if seg]
    if any(seg.lower() in FORBIDDEN_ANY_LEVEL_DIRS for seg in parts):
        return True
    name = parts[-1].lower()
    if name in ALLOWED_EXACT_NAMES:
        return False
    return any(fnmatch.fnmatchcase(name, pat) for pat in FORBIDDEN_NAME_PATTERNS)


def _git_staged_paths() -> list[str]:
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", "diff", "--cached", "--name-only", "--diff-filter=d", "-z"],
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        **_hidden_kwargs(),
    )
    if proc.returncode != 0:
        raise RuntimeError("Не удалось прочитать список индексированных файлов из Git")
    return [_normalize(tok.decode("utf-8", errors="replace")) for tok in proc.stdout.split(b"\\x00") if tok]


def _read_staged_or_disk_bytes(rel_path: str, staged_mode: bool) -> bytes | None:
    if staged_mode:
        proc = subprocess.run(
            ["git", "show", f":{rel_path}"],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            **_hidden_kwargs(),
        )
        if proc.returncode == 0:
            return proc.stdout
    disk_p = ROOT / rel_path
    if disk_p.is_file():
        return disk_p.read_bytes()
    return None


def _discover_project_code_files() -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            suf = Path(fname).suffix.lower()
            if suf in {".py", ".js", ".mjs", ".cjs"}:
                rel = Path(dirpath, fname).relative_to(ROOT).as_posix()
                found.append(rel)
    return sorted(found)


def _find_node() -> str:
    found = shutil.which("node.exe") or shutil.which("node")
    if found:
        return found
    if os.name == "nt":
        cand = Path(r"C:\\Program Files\\nodejs\\node.exe")
        if cand.is_file():
            return str(cand)
    return ""


def _check_utf8_and_syntax(rel_paths: list[str], *, staged_mode: bool) -> list[str]:
    errors: list[str] = []
    node_bin = _find_node()
    clean_env = _clean_child_env()

    with tempfile.TemporaryDirectory(prefix="verify_proj_") as tmp_dir:
        tmp_root = Path(tmp_dir)
        for idx, rel in enumerate(rel_paths):
            suf = Path(rel).suffix.lower()
            if suf not in {".py", ".js", ".mjs", ".cjs"}:
                continue
            raw = _read_staged_or_disk_bytes(rel, staged_mode=staged_mode)
            if raw is None:
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                errors.append(f"invalid UTF-8 in {rel}: {exc}")
                continue
            if text.startswith("\\ufeff") or "\\ufffd" in text:
                errors.append(f"UTF-8 BOM or replacement char in {rel}")
                continue

            tmp_src = tmp_root / f"src_{idx}{suf}"
            tmp_src.write_bytes(raw)
            if suf == ".py":
                try:
                    py_compile.compile(str(tmp_src), cfile=str(tmp_root / f"src_{idx}.pyc"), doraise=True)
                except py_compile.PyCompileError as exc:
                    errors.append(f"Python syntax error in {rel}: {exc.msg}")
            elif node_bin:
                proc = subprocess.run(
                    [node_bin, "--check", str(tmp_src)],
                    cwd=str(ROOT),
                    env=clean_env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=15.0,
                    **_hidden_kwargs(),
                )
                if proc.returncode != 0:
                    err_msg = (proc.stderr or proc.stdout).decode("utf-8", errors="replace").strip()
                    errors.append(f"JS syntax error in {rel}: {err_msg.splitlines()[0] if err_msg else 'node --check failed'}")
    return errors


def _run_local_venv_pytest_if_available() -> int:
    tests_dir = ROOT / "tests"
    if not tests_dir.is_dir():
        return 0
    venv_candidates = (
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
    )
    venv_py = next((p for p in venv_candidates if p.is_file()), None)
    if venv_py is None:
        return 0
    env = _clean_child_env()
    probe = subprocess.run(
        [str(venv_py), "-c", "import pytest"],
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10.0,
        **_hidden_kwargs(),
    )
    if probe.returncode != 0:
        return 0
    res = subprocess.run(
        [str(venv_py), "-m", "pytest", "-q", "tests"],
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=90.0,
        **_hidden_kwargs(),
    )
    if res.stdout:
        sys.stdout.buffer.write(res.stdout)
        sys.stdout.buffer.flush()
    return res.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged-guard", action="store_true", help="Validate staged index before git commit")
    opts = parser.parse_args()

    if opts.staged_guard:
        staged = _git_staged_paths()
        forbidden = [p for p in staged if is_forbidden_staged_path(p)]
        if forbidden:
            print(
                "VERIFY_PROJECT_BLOCK: секреты или файлы БД запрещены к коммиту: " + ", ".join(forbidden),
                file=sys.stderr,
                flush=True,
            )
            return 1
        target_files = staged
    else:
        target_files = _discover_project_code_files()

    syntax_errors = _check_utf8_and_syntax(target_files, staged_mode=opts.staged_guard)
    if syntax_errors:
        for item in syntax_errors:
            print(f"VERIFY_PROJECT_BLOCK: {item}", file=sys.stderr, flush=True)
        return 1

    if _run_local_venv_pytest_if_available() != 0:
        print("VERIFY_PROJECT_BLOCK: локальные тесты pytest завершились с ошибкой", file=sys.stderr, flush=True)
        return 1

    print("VERIFY_PROJECT_OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _build_project_pre_commit_hook_sh() -> str:
    fallback_py = Path(sys.executable).resolve().as_posix()
    return (
        "#!/bin/sh\n"
        "# Antigravity Hub Project Shield pre-commit lock (v=148).\n"
        "# Blocks .env / *.db / *.session secrets, broken UTF-8/Python/JS syntax, and failing local .venv pytest.\n"
        "\n"
        "ROOT=$(git rev-parse --show-toplevel) || exit 1\n"
        'cd "$ROOT" || exit 1\n'
        "\n"
        'PY=""\n'
        f'for CAND in ".venv/Scripts/python.exe" ".venv/bin/python" "venv/Scripts/python.exe" "venv/bin/python" "{fallback_py}"; do\n'
        '  if [ -n "$CAND" ] && [ -f "$CAND" ]; then\n'
        '    PY="$CAND"\n'
        "    break\n"
        "  fi\n"
        "done\n"
        "\n"
        'if [ -z "$PY" ]; then\n'
        "  for CMD_CAND in python3 python; do\n"
        '    if command -v "$CMD_CAND" >/dev/null 2>&1 && "$CMD_CAND" -c "import sys" >/dev/null 2>&1; then\n'
        '      PY="$CMD_CAND"\n'
        "      break\n"
        "    fi\n"
        "  done\n"
        "fi\n"
        "\n"
        'if [ -z "$PY" ]; then\n'
        '  echo "pre-commit: Python interpreter not found for tools/verify_project.py" >&2\n'
        "  exit 1\n"
        "fi\n"
        "\n"
        'exec "$PY" tools/verify_project.py --staged-guard\n'
    )


def install_project_shield(proj_dir: Path) -> dict[str, Any]:
    """Install 1-click `tools/verify_project.py` + `tools/git_hooks/pre-commit` shield in a project.
    For `Antigravity_Hub` itself (`BASE_DIR`), preserves `tools/verify_hub.py` and only ensures `core.hooksPath`."""
    resolved = _validate_project_dir(proj_dir)
    with _get_project_lock(resolved):
        if config._same_fs_path(resolved, config.BASE_DIR.resolve()):
            hook_file = resolved / "tools" / "git_hooks" / "pre-commit"
            if not hook_file.is_file():
                raise GitServiceError("Штатный хук tools/git_hooks/pre-commit не найден в ядре Хаба", status_code=500)
            rc_cfg, _, err_cfg = _exec_git_raw(resolved, ["config", "core.hooksPath", "tools/git_hooks"], timeout=5.0)
            if rc_cfg != 0:
                raise GitServiceError(f"Не удалось настроить core.hooksPath: {err_cfg.strip()}", status_code=500)
            return {
                "ok": True,
                "shield_installed": True,
                "shield_kind": "hub",
                "hooks_path": "tools/git_hooks",
                "verifier_path": "tools/verify_hub.py",
                "hook_path": "tools/git_hooks/pre-commit",
            }

        ensure_project_git_ready(resolved)

        tools_dir = resolved / "tools"
        hooks_dir = tools_dir / "git_hooks"
        verifier_file = tools_dir / "verify_project.py"
        hook_file = hooks_dir / "pre-commit"

        try:
            hooks_dir.mkdir(parents=True, exist_ok=True)
            verifier_file.write_text(_VERIFY_PROJECT_SCRIPT_TEMPLATE, encoding="utf-8", newline="\n")
            hook_file.write_text(_build_project_pre_commit_hook_sh(), encoding="utf-8", newline="\n")
            if not config.IS_WINDOWS:
                hook_file.chmod(hook_file.stat().st_mode | 0o755)
        except OSError as exc:
            raise GitServiceError(f"Не удалось записать файлы защитного щита проекта: {exc}", status_code=500) from exc

        rc_cfg, _, err_cfg = _exec_git_raw(resolved, ["config", "core.hooksPath", "tools/git_hooks"], timeout=5.0)
        if rc_cfg != 0:
            raise GitServiceError(f"Не удалось активировать core.hooksPath: {err_cfg.strip()}", status_code=500)

        return {
            "ok": True,
            "shield_installed": True,
            "shield_kind": "project",
            "hooks_path": "tools/git_hooks",
            "verifier_path": "tools/verify_project.py",
            "hook_path": "tools/git_hooks/pre-commit",
        }
