from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import posixpath
import re
import secrets
import shlex
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from . import config, database

MCP_SANDBOX_SERVER_NAME = "hub_sandbox"
MCP_PROTOCOL_VERSION = "2025-03-26"
_SUPPORTED_MCP_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}
SANDBOX_CONTAINER_SCHEMA = "v2"

SANDBOX_IDLE_TTL_SECONDS = 1500  # 25 minutes
SANDBOX_MAX_BG_SERVICE_TTL_SECONDS = 86400  # 24 hours ceiling for background scripts
SANDBOX_REAPER_INTERVAL_SECONDS = 120
SANDBOX_MAX_TIMEOUT_SEC = 180
SANDBOX_MAX_OUTPUT_CHARS = 32000
_TRUNCATE_HEAD_CHARS = 14000
_TRUNCATE_TAIL_CHARS = 16000

_LOOPBACK_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)

_CONTAINER_LAST_USED: dict[str, float] = {}
_CONTAINER_STARTED_MONO: dict[str, float] = {}
_CONTAINER_META: dict[str, dict[str, str]] = {}
_CONTAINER_LOCKS: dict[str, asyncio.Lock] = {}
_REAPER_TASK: Optional[asyncio.Task[None]] = None
_SANDBOX_PROCESSES_NOTIFIER: Optional[Callable[..., Awaitable[None]]] = None
_SANDBOX_WATCHED_SERVICES: dict[str, dict[str, tuple[str, Path, str, str]]] = {}


def set_sandbox_processes_notifier(
    cb: Optional[Callable[..., Awaitable[None]]],
) -> None:
    global _SANDBOX_PROCESSES_NOTIFIER
    _SANDBOX_PROCESSES_NOTIFIER = cb


def _schedule_sandbox_processes_notify(
    project_name: str,
    project_dir: Path,
    user_id: str,
    transition: Optional[dict[str, Any]] = None,
) -> None:
    if _SANDBOX_PROCESSES_NOTIFIER is None or not project_name:
        return
    try:
        loop = asyncio.get_running_loop()
        args = (project_name, project_dir.resolve(), user_id)
        call = _SANDBOX_PROCESSES_NOTIFIER(*args, transition) if transition else _SANDBOX_PROCESSES_NOTIFIER(*args)
        loop.create_task(call)
    except Exception:
        pass


@dataclass
class _TrackedExec:
    chat_id: str
    container_name: str
    exec_tag: str
    proc: asyncio.subprocess.Process


_ACTIVE_EXECS_BY_CHAT: dict[str, list[_TrackedExec]] = {}


@dataclass
class ResolvedSandboxRun:
    user: dict[str, Any]
    chat_id: str
    project_name: str
    project_dir: Path
    sandbox_network: str


# ---------------------------------------------------------------------------
# 4.1. HMAC-SHA256 Token, Loopback Guard & CLI Config/Skill Sync
# ---------------------------------------------------------------------------


def _compute_sandbox_mcp_signature(user_id: str, session_version: int) -> str:
    msg = f"sandbox-mcp:v1:{user_id}:{int(session_version)}".encode("utf-8")
    return hmac.new(config.HUB_SECRET_KEY.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def build_sandbox_mcp_token(user_id: str, session_version: int) -> str:
    clean_uid = (user_id or "").strip()
    sv_int = int(session_version or 0)
    if not clean_uid or sv_int < 1:
        raise ValueError("user_id и положительный session_version обязательны для выпуска MCP-токена")
    raw_claim = f"{clean_uid}:{sv_int}".encode("utf-8")
    claim_b64 = base64.urlsafe_b64encode(raw_claim).decode("ascii").rstrip("=")
    sig = _compute_sandbox_mcp_signature(clean_uid, sv_int)
    return f"{claim_b64}.{sig}"


def verify_sandbox_mcp_token(token: str) -> Optional[tuple[str, int]]:
    """Verify HMAC-SHA256 signature and extract `(user_id, session_version)`."""
    raw = (token or "").strip()
    if "." not in raw or len(raw) > 320:
        return None
    claim_b64, candidate_sig = raw.split(".", 1)
    if not claim_b64 or len(candidate_sig) != 64:
        return None
    pad = "=" * ((4 - len(claim_b64) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode((claim_b64 + pad).encode("ascii")).decode("utf-8")
    except Exception:
        return None
    if ":" not in decoded:
        return None
    user_id, sv_str = decoded.rsplit(":", 1)
    user_id = user_id.strip()
    if not user_id:
        return None
    try:
        sv_int = int(sv_str)
    except ValueError:
        return None
    if sv_int < 1:
        return None
    expected_sig = _compute_sandbox_mcp_signature(user_id, sv_int)
    if not hmac.compare_digest(expected_sig, candidate_sig):
        return None
    return (user_id, sv_int)


def is_loopback_mcp_ip(host: str) -> bool:
    raw = (host or "").strip()
    if not raw:
        return False
    if raw.lower() == "localhost":
        return True
    if "%" in raw:
        raw = raw.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in _LOOPBACK_NETWORKS)


def is_loopback_mcp_request(request: Request) -> bool:
    """Accept requests strictly from Windows host loopback (`127.0.0.1`, `::1`, `localhost`)."""
    for proxy_hdr in (
        "x-forwarded-for",
        "x-real-ip",
        "cf-connecting-ip",
        "true-client-ip",
        "forwarded",
    ):
        if (request.headers.get(proxy_hdr) or "").strip():
            return False
    client_host = request.client.host if request.client else ""
    return is_loopback_mcp_ip(client_host)


def is_user_sandbox_eligible(user: Optional[dict[str, Any]]) -> bool:
    if not user or not bool(user.get("is_active", True)):
        return False
    role = str(user.get("role") or "").strip().lower()
    max_mode = str(user.get("max_access_mode") or "").strip().lower()
    return role == "admin" or max_mode in ("docker_full", "host_full")


def _atomic_write_text(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if target.exists() and target.read_text(encoding="utf-8") == content:
            return
    except OSError:
        pass
    tmp = target.parent / f".{target.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp"
    tmp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(tmp, target)


def _sync_sandbox_mcp_config_file(
    cfg_file: Path,
    *,
    server_url: Optional[str],
) -> None:
    existing_root: dict[str, Any] = {}
    if cfg_file.exists():
        try:
            loaded = json.loads(cfg_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing_root = loaded
        except Exception:
            existing_root = {}

    servers = existing_root.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    else:
        servers = dict(servers)

    if server_url:
        servers[MCP_SANDBOX_SERVER_NAME] = {"url": server_url}
        existing_root["mcpServers"] = servers
        rendered = json.dumps(existing_root, ensure_ascii=False, indent=2) + "\n"
        _atomic_write_text(cfg_file, rendered)
    else:
        servers.pop(MCP_SANDBOX_SERVER_NAME, None)
        other_root_keys = {k for k in existing_root.keys() if k != "mcpServers"}
        if not servers and not other_root_keys:
            cfg_file.unlink(missing_ok=True)
        else:
            existing_root["mcpServers"] = servers
            rendered = json.dumps(existing_root, ensure_ascii=False, indent=2) + "\n"
            _atomic_write_text(cfg_file, rendered)


def _build_docker_sandbox_skill_markdown(
    sandbox_network: str = "internet",
    *,
    is_admin: bool = False,
    can_host_shell: bool = False,
) -> str:
    net_note = (
        "Интернет для скачивания пакетов (`npm`, `pnpm`, `pip`, `cargo`) включён; доступ к локальным портам хост-сервера закрыт."
        if sandbox_network != "none"
        else "Песочница работает в полном офлайн-режиме (`--network none`) без выхода во внешнюю сеть."
    )
    if is_admin or can_host_shell:
        desc_line = (
            "  Изолированный Linux-терминал в Docker-песочнице общих проектов (MCP-сервер hub_sandbox): выполнение\n"
            "  сборки, тестов, git, Node.js, Python и Rust/Cargo через sandbox_exec, управление фоновыми\n"
            "  скриптами, парсерами, ботами и веб-серверами через sandbox_service_ctl и сброс окружения через\n"
            "  sandbox_reset. Активируй ТОЛЬКО в общих Docker-проектах, где штатный run_command заблокирован;\n"
            "  в проектах с прямым терминалом ПК сразу используй run_command.\n"
        )
        intro_line = (
            "**Важно:** в проектах с прямым доступом к терминалу ПК (где штатный `run_command` разрешён) "
            "**НЕ вызывай** `hub_sandbox`, а сразу работай через `run_command`. Этот контур (`hub_sandbox`) предназначен "
            "только для общих проектов с Docker-изоляцией, где терминал хоста заблокирован (`Ubuntu 24.04`, `Node.js 22`, "
            "`pnpm`, `Python 3`, `Rust/Cargo`, `git`, `curl`).\n\n"
        )
    else:
        desc_line = (
            "  Изолированный Linux-терминал в Docker-песочнице проекта (MCP-сервер hub_sandbox): выполнение\n"
            "  сборки, тестов, git, Node.js, Python и Rust/Cargo через sandbox_exec, управление фоновыми\n"
            "  скриптами, парсерами, ботами и веб-серверами через sandbox_service_ctl и чистый пересоздающий\n"
            "  сброс окружения через sandbox_reset. Активируй в проектных чатах в режиме «Терминал», когда требуется выполнить команды.\n"
        )
        intro_line = (
            "Терминал хост-сервера в этом контуре закрыт. Для выполнения команд и запуска кода подключён тёплый "
            "Linux-контейнер (`Ubuntu 24.04`, `Node.js 22`, `pnpm`, `Python 3 + openpyxl/requests/bs4`, `Rust/Cargo`, `git`, `curl`, `sqlite3`, `ripgrep`).\n\n"
        )

    return (
        "---\n"
        "name: docker-sandbox\n"
        "description: >-\n"
        f"{desc_line}"
        "---\n\n"
        "# Изолированная Docker-песочница (`hub_sandbox`)\n\n"
        f"{intro_line}"
        "## Главные правила работы\n"
        "1. **Чтение и правка кода:** всегда читай и редактируй текстовые файлы штатными инструментами `read_file`, "
        "`edit_file`, `write_file` в папке проекта — эта же папка синхронно примонтирована внутри Linux-песочницы "
        "как `/workspace`.\n"
        "2. **Короткие команды сборки и тестов (`sandbox_exec`):** в режиме «Терминал» запускай сборку, линтеры, тесты, "
        "`node`, `npm`, `pnpm`, `python3`, `pytest`, `cargo`, `git` через `sandbox_exec(command=..., workdir=\".\")`. "
        "Никогда не передавай абсолютные пути хоста (`D:\\...` или `/home/...`) в `workdir` — используй только относительные пути "
        "внутри `/workspace`.\n"
        "3. **Вложения чата (`.xlsx`, `.csv`, `.zip`, бинарные файлы):** если пользователь прикрепил файлы к текущему диалогу, "
        "они автоматически доступны внутри контейнера только для чтения по пути из переменной окружения `$AGY_CHAT_UPLOADS` "
        "(папка `/uploads/<chat_id>`).\n"
        "4. **Фоновые скрипты, парсеры, воркеры, серверы и боты (`sandbox_service_ctl`):** любые длительные рабочие скрипты, "
        "парсеры, воркеры, веб-серверы, API или боты **не блокируй** внутри `sandbox_exec`! Запускай их в фоне через "
        "`sandbox_service_ctl(action=\"start\", name=\"app\", command=\"...\")`, чтобы пользователь видел живую карточку процесса, "
        "потребление RAM и логи в интерфейсе Hub. Проверяй ответ через `sandbox_exec(\"curl -i http://127.0.0.1:3000\")` "
        "(и логи через `sandbox_service_ctl(action=\"logs\", name=\"app\")`), а временные тестовые серверы останавливай через "
        "`sandbox_service_ctl(action=\"stop\", name=\"app\")`.\n"
        "5. **Чистый сброс (`sandbox_reset`):** если в контейнере зависли процессы или засорился `/tmp`, вызови "
        "`sandbox_reset()` — контейнер пересоздастся за секунду, а все файлы кода в `/workspace` останутся целыми.\n\n"
        f"**Сеть песочницы:** {net_note}\n"
    )


def sync_user_sandbox_cli_files(
    user: Optional[dict[str, Any]],
    *,
    force_disable_user_id: Optional[str] = None,
    sandbox_ready: bool = True,
    admin_has_docker_projects: bool = True,
) -> bool:
    """Idempotently sync or remove `hub_sandbox` in `mcp_config.json` and `docker-sandbox/SKILL.md`.

    Preserves `hub_openrouter` and any other MCP servers in `mcp_config.json`.
    Disables `hub_sandbox` cleanly when Docker image/daemon is not ready (`sandbox_ready=False`)
    or when `admin` has no shared Docker projects, avoiding dead MCP calls in CLI turns.
    Returns True if `hub_sandbox` is enabled after sync, False otherwise.
    """
    uid = str((user or {}).get("id") or force_disable_user_id or "").strip()
    if not uid:
        return False

    home_dir = config.ensure_user_cli_home(uid)
    config_dir = home_dir / ".gemini" / "config"
    mcp_file = config_dir / "mcp_config.json"
    mcp_docker_file = config_dir / "mcp_config.docker.json"
    skill_dir = config_dir / "skills" / "docker-sandbox"
    skill_file = skill_dir / "SKILL.md"
    agents_skill_dir = home_dir / ".agents" / "skills" / "docker-sandbox"
    agents_skill_file = agents_skill_dir / "SKILL.md"
    claude_skill_dir = home_dir / ".claude" / "skills" / "docker-sandbox"
    claude_skill_file = claude_skill_dir / "SKILL.md"
    claude_accounts_dir = home_dir / ".claude" / "accounts"

    mcp_docker_file.unlink(missing_ok=True)

    is_admin = str((user or {}).get("role") or "").strip().lower() == "admin"
    should_enable = bool(
        user is not None
        and is_user_sandbox_eligible(user)
        and not force_disable_user_id
        and sandbox_ready
        and (not is_admin or admin_has_docker_projects)
    )

    if should_enable and user is not None:
        sv = max(1, int(user.get("session_version") or 1))
        token = build_sandbox_mcp_token(uid, sv)
        local_url = f"http://127.0.0.1:{config.HUB_PORT}/mcp/sandbox/{token}"
        _sync_sandbox_mcp_config_file(mcp_file, server_url=local_url)
        sbx_net = str(user.get("sandbox_network") or "internet").strip().lower()
        can_host_shell = bool(
            is_admin
            or str(user.get("max_access_mode") or "").strip().lower() == "host_full"
        )
        skill_md = _build_docker_sandbox_skill_markdown(
            sbx_net, is_admin=is_admin, can_host_shell=can_host_shell
        )
        _atomic_write_text(skill_file, skill_md)
        _atomic_write_text(agents_skill_file, skill_md)
        _atomic_write_text(claude_skill_file, skill_md)
        if claude_accounts_dir.is_dir():
            for child in claude_accounts_dir.iterdir():
                if child.is_dir() and not child.name.startswith("_pending_"):
                    try:
                        _atomic_write_text(child / "skills" / "docker-sandbox" / "SKILL.md", skill_md)
                    except OSError:
                        pass
        return True

    _sync_sandbox_mcp_config_file(mcp_file, server_url=None)
    skill_file.unlink(missing_ok=True)
    agents_skill_file.unlink(missing_ok=True)
    claude_skill_file.unlink(missing_ok=True)
    extra_dirs: list[Path] = []
    if claude_accounts_dir.is_dir():
        for child in claude_accounts_dir.iterdir():
            if child.is_dir():
                acc_sk = child / "skills" / "docker-sandbox"
                (acc_sk / "SKILL.md").unlink(missing_ok=True)
                extra_dirs.append(acc_sk)
    for sdir in (skill_dir, agents_skill_dir, claude_skill_dir, *extra_dirs):
        try:
            if sdir.exists() and not any(sdir.iterdir()):
                sdir.rmdir()
        except OSError:
            pass
    return False


async def _resolve_sandbox_sync_flags(docker_ready: Optional[bool] = None) -> tuple[bool, bool]:
    if docker_ready is None:
        try:
            from .agy_engine import check_docker_status

            d_info = await check_docker_status()
            sbx_ready = bool(d_info.get("sandbox_mcp_ready"))
        except Exception:
            sbx_ready = False
    else:
        sbx_ready = bool(docker_ready)

    try:
        shared_items = await database.list_shared_projects(is_admin=True)
        admin_has_docker = any(
            str(sp.get("execution_mode") or "docker").strip().lower() == "docker"
            for sp in shared_items
        )
    except Exception:
        admin_has_docker = False

    return sbx_ready, admin_has_docker


async def sync_user_sandbox_cli_files_by_id(
    user_id: str,
    *,
    docker_ready: Optional[bool] = None,
) -> bool:
    uid = (user_id or "").strip()
    if not uid:
        return False
    user = await database.get_user_by_id(uid)
    if not user:
        return False
    sbx_ready, admin_has_docker = await _resolve_sandbox_sync_flags(docker_ready)
    return await asyncio.to_thread(
        sync_user_sandbox_cli_files,
        user,
        sandbox_ready=sbx_ready,
        admin_has_docker_projects=admin_has_docker,
    )


async def sync_all_users_sandbox_cli_files(*, docker_ready: Optional[bool] = None) -> None:
    users = await database.list_users()
    sbx_ready, admin_has_docker = await _resolve_sandbox_sync_flags(docker_ready)
    for u in users:
        try:
            await asyncio.to_thread(
                sync_user_sandbox_cli_files,
                u,
                sandbox_ready=sbx_ready,
                admin_has_docker_projects=admin_has_docker,
            )
        except Exception:
            pass


async def _authenticate_sandbox_user_from_token(mcp_token: str) -> Optional[dict[str, Any]]:
    parsed = verify_sandbox_mcp_token(mcp_token)
    if not parsed:
        return None
    user_id, token_sv = parsed
    user = await database.get_user_by_id(user_id)
    if not user:
        return None
    if not bool(user.get("is_active", True)):
        return None
    if int(user.get("session_version") or 1) != token_sv:
        return None
    if not is_user_sandbox_eligible(user):
        return None
    return user


# ---------------------------------------------------------------------------
# 4.2. Zero-Trust Workspace Resolution via `ACTIVE_RUNS` & `workdir` Guard
# ---------------------------------------------------------------------------


def _is_within_dir(child: Path, parent: Path) -> bool:
    try:
        c_res = child.resolve()
        p_res = parent.resolve()
        return c_res == p_res or p_res in c_res.parents
    except Exception:
        return False


def resolve_safe_container_workdir(raw_workdir: str, host_project_dir: Path) -> tuple[str, Path]:
    """Normalize relative `workdir` into `/workspace[/...]` and verify host subfolder containment."""
    base_host = host_project_dir.resolve()
    wd = str(raw_workdir if raw_workdir is not None else ".").strip().replace("\\", "/")

    if not wd or wd == ".":
        container_wd = "/workspace"
    else:
        if "\x00" in wd or re.match(r"^[a-zA-Z]:", wd):
            raise ValueError(
                "Path traversal forbidden: абсолютные пути хоста запрещены (используйте относительный путь внутри /workspace)."
            )
        if wd == "/workspace" or wd.startswith("/workspace/"):
            container_wd = posixpath.normpath(wd)
        elif wd.startswith("/"):
            raise ValueError(
                "Path traversal forbidden: выход за пределы /workspace запрещён."
            )
        else:
            container_wd = posixpath.normpath("/workspace/" + wd.lstrip("/"))

    if container_wd != "/workspace" and not container_wd.startswith("/workspace/"):
        raise ValueError(
            "Path traversal forbidden: выход за пределы /workspace запрещён."
        )

    rel_sub = "" if container_wd == "/workspace" else container_wd[len("/workspace/") :]
    target_host = (base_host / rel_sub).resolve() if rel_sub else base_host
    if not _is_within_dir(target_host, base_host):
        raise ValueError(
            "Path traversal forbidden: симлинк или путь выводит за пределы рабочей папки проекта."
        )
    if not target_host.exists() or not target_host.is_dir():
        raise ValueError(
            f"Подпапка '{container_wd}' не существует внутри проекта '{base_host.name}'."
        )
    return container_wd, target_host


def resolve_active_sandbox_run(
    user: dict[str, Any],
    *,
    requested_project: str = "",
) -> ResolvedSandboxRun:
    """Resolve the caller's active `full_access` project run strictly from server-side `ACTIVE_RUNS`."""
    from .agy_engine import ACTIVE_RUNS

    uid = str(user.get("id") or "").strip()
    candidates = [
        r
        for r in ACTIVE_RUNS.values()
        if not r.cancelled
        and r.user_id == uid
        and r.sandbox_allowed is True
        and r.kind == "project"
        and r.access_mode == "full_access"
        and r.project_dir is not None
    ]

    if not candidates:
        raise PermissionError(
            "Docker-песочница доступна только во время активного хода в проектном чате в режиме «Терминал»"
        )

    clean_req_proj = str(requested_project or "").strip()

    if len(candidates) == 1:
        chosen = candidates[0]
        if clean_req_proj and clean_req_proj.lower() != str(chosen.project_name or "").strip().lower():
            raise PermissionError(
                f"Активный ход идёт в проекте «{chosen.project_name}», а запрошен проект «{clean_req_proj}»."
            )
    else:
        if clean_req_proj:
            matched = [
                r
                for r in candidates
                if str(r.project_name or "").strip().lower() == clean_req_proj.lower()
            ]
            if not matched:
                active_names = sorted({str(r.project_name or "") for r in candidates if r.project_name})
                raise PermissionError(
                    f"Среди активных ходов не найден проект «{clean_req_proj}». "
                    f"Активные проекты: {', '.join(active_names)}."
                )
            matched.sort(
                key=lambda r: float(getattr(r, "started_ts", getattr(r, "started_at", 0.0))),
                reverse=True,
            )
            chosen = matched[0]
        else:
            unique_dirs = {r.project_dir.resolve() for r in candidates if r.project_dir is not None}
            if len(unique_dirs) == 1:
                candidates.sort(
                    key=lambda r: float(getattr(r, "started_ts", getattr(r, "started_at", 0.0))),
                    reverse=True,
                )
                chosen = candidates[0]
            else:
                active_names = sorted({str(r.project_name or "") for r in candidates if r.project_name})
                hint = active_names[0] if active_names else "имя_проекта"
                raise PermissionError(
                    "У вас одновременно запущено несколько параллельных ходов в разных проектах "
                    f"({', '.join(active_names)}). Передайте аргумент project=\"{hint}\" для точного выбора песочницы."
                )

    raw_net = str(chosen.sandbox_network or user.get("sandbox_network") or "internet").strip().lower()
    net_mode = "none" if raw_net == "none" else "internet"
    return ResolvedSandboxRun(
        user=user,
        chat_id=chosen.chat_id,
        project_name=chosen.project_name,
        project_dir=chosen.project_dir.resolve(),
        sandbox_network=net_mode,
    )


# ---------------------------------------------------------------------------
# 4.3. Warm Container Lifecycle, Network Isolation & Idle Reaper
# ---------------------------------------------------------------------------


def _slugify_ascii(raw: str, max_len: int, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", (raw or "").strip().lower()).strip("-_")
    if not slug:
        slug = fallback
    slug = slug[:max_len].strip("-_")
    return slug or fallback


def build_sandbox_container_name(user_id: str, project_dir: Path) -> str:
    resolved = project_dir.resolve()
    safe_user = _slugify_ascii(user_id, 14, "u")
    safe_proj = _slugify_ascii(resolved.name, 18, "proj")
    digest_input = f"{user_id}::{os.path.normcase(str(resolved))}".encode("utf-8")
    sha1_8 = hashlib.sha1(digest_input).hexdigest()[:8]
    return f"agy-sbx-{safe_user}-{safe_proj}-{sha1_8}"


def build_sandbox_uploads_host_dir(user_id: str, project_dir: Path) -> Path:
    cname = build_sandbox_container_name(user_id, project_dir)
    return (config.SANDBOX_UPLOADS_DIR.resolve() / cname).resolve()


def _cleanup_sandbox_uploads_host_dir(container_name: str) -> None:
    clean_name = re.sub(r"[^a-zA-Z0-9_.\-]+", "", (container_name or "").strip())
    if not clean_name:
        return
    root = config.SANDBOX_UPLOADS_DIR.resolve()
    target = (root / clean_name).resolve()
    if target != root and _is_within_dir(target, root) and target.exists():
        shutil.rmtree(target, ignore_errors=True)


def resolve_sandbox_container_user() -> str:
    """Resolve unprivileged `uid:gid` for Docker sandbox across Windows & Linux.

    - Windows (Docker Desktop / VirtioFS): always `1000:1000` (`dev`).
    - Linux non-root service account: matches host `os.getuid():os.getgid()` so bind-mounted
      `/workspace` files have zero UID/GID permission mismatches between host `agy` and container.
    - Linux `root` (`uid == 0`): NEVER runs container as root (`0:0`) — falls back to `1000:1000` (`dev`).
    """
    if config.IS_WINDOWS or not hasattr(os, "getuid") or not hasattr(os, "getgid"):
        return "1000:1000"
    uid = int(os.getuid())
    gid = int(os.getgid())
    if uid <= 0:
        return "1000:1000"
    return f"{uid}:{gid if gid > 0 else uid}"


def _ensure_linux_workspace_permissions(resolved_dir: Path) -> None:
    """When Hub runs as `root` on Linux while container runs as `1000:1000`, ensure managed
    guest/shared workspaces are writable by `1000:1000`."""
    if config.IS_WINDOWS or not hasattr(os, "getuid") or not hasattr(os, "chown"):
        return
    try:
        if int(os.getuid()) != 0:
            return
        managed_roots = (
            config.get_effective_users_dir(),
            config.USER_WORKSPACES_DIR.resolve(),
            config.SHARED_PROJECTS_DIR.resolve(),
        )
        if not any(_is_within_dir(resolved_dir, mr) for mr in managed_roots):
            return
        os.chown(resolved_dir, 1000, 1000)
        for root_str, dirs, files in os.walk(resolved_dir):
            for name in dirs:
                p = os.path.join(root_str, name)
                if not os.path.islink(p):
                    try:
                        st = os.stat(p)
                        if st.st_uid == 0:
                            os.chown(p, 1000, 1000)
                    except OSError:
                        pass
            for name in files:
                p = os.path.join(root_str, name)
                if not os.path.islink(p):
                    try:
                        st = os.stat(p)
                        if st.st_uid == 0:
                            os.chown(p, 1000, 1000)
                    except OSError:
                        pass
    except OSError:
        pass


def sync_chat_uploads_to_sandbox(chat_id: str, sbx_uploads_dir: Path) -> str:
    """Mirror strictly this chat's uploaded attachments into `/uploads/<safe_chat_id>` (read-only mount).

    Never exposes other chats' uploads and skips internal `large_prompt_input.md`.
    Returns container path `/uploads/<safe_chat_id>` when attachments exist, else `""`.
    """
    raw_cid = (chat_id or "").strip()
    safe_cid = re.sub(r"[^a-zA-Z0-9_.\-]+", "_", raw_cid).strip("._")
    if not raw_cid or not safe_cid:
        return ""

    cand_src_dirs: list[Path] = []
    # 1. Legacy `data/uploads/<chat_id>` (lowest precedence if duplicate filenames exist)
    legacy_root = config.get_effective_legacy_uploads_dir()
    leg_src = (legacy_root / raw_cid).resolve()
    if leg_src != legacy_root and _is_within_dir(leg_src, legacy_root) and leg_src.is_dir():
        cand_src_dirs.append(leg_src)

    # 2. Per-user `users/<slug>/uploads/<safe_cid>`
    users_root = config.get_effective_users_dir()
    safe_chat_slug = config.sanitize_chat_folder_id(raw_cid)
    if users_root.is_dir():
        try:
            for u_entry in users_root.iterdir():
                if not u_entry.is_dir():
                    continue
                u_up = (u_entry / "uploads" / safe_chat_slug).resolve()
                if _is_within_dir(u_up, users_root) and u_up.is_dir() and u_up not in cand_src_dirs:
                    cand_src_dirs.append(u_up)
        except OSError:
            pass

    # 3. Shared project `shared_projects/_uploads/<safe_cid>` (highest precedence for shared chats)
    shared_up_root = (config.SHARED_PROJECTS_DIR.resolve() / "_uploads").resolve()
    sh_src = (shared_up_root / safe_chat_slug).resolve()
    if sh_src != shared_up_root and _is_within_dir(sh_src, shared_up_root) and sh_src.is_dir() and sh_src not in cand_src_dirs:
        cand_src_dirs.append(sh_src)

    if not cand_src_dirs:
        return ""

    sbx_root = sbx_uploads_dir.resolve()
    sbx_root.mkdir(parents=True, exist_ok=True)
    dst_dir = (sbx_root / safe_cid).resolve()
    if dst_dir == sbx_root or not _is_within_dir(dst_dir, sbx_root):
        return ""

    by_name: dict[str, Path] = {}
    for src_dir in cand_src_dirs:
        try:
            for entry in src_dir.iterdir():
                if (
                    entry.is_file()
                    and not entry.is_symlink()
                    and entry.name != "large_prompt_input.md"
                    and not entry.name.startswith(".")
                ):
                    by_name[entry.name] = entry
        except OSError:
            continue
    valid_files: list[Path] = list(by_name.values())

    if not valid_files:
        if dst_dir.exists():
            shutil.rmtree(dst_dir, ignore_errors=True)
        return ""

    dst_dir.mkdir(parents=True, exist_ok=True)
    if not config.IS_WINDOWS:
        try:
            os.chmod(sbx_root, 0o755)
            os.chmod(dst_dir, 0o755)
        except OSError:
            pass

    wanted_names = {f.name for f in valid_files}
    try:
        for existing in list(dst_dir.iterdir()):
            if existing.name not in wanted_names:
                if existing.is_dir() and not existing.is_symlink():
                    shutil.rmtree(existing, ignore_errors=True)
                else:
                    existing.unlink(missing_ok=True)
    except OSError:
        pass

    copied_any = False
    for src_f in valid_files:
        dst_f = dst_dir / src_f.name
        try:
            s_stat = src_f.stat()
            need_sync = True
            if dst_f.exists():
                d_stat = dst_f.stat()
                if d_stat.st_size == s_stat.st_size and int(d_stat.st_mtime) >= int(s_stat.st_mtime):
                    need_sync = False
            if need_sync:
                dst_f.unlink(missing_ok=True)
                try:
                    os.link(src_f, dst_f)
                except OSError:
                    shutil.copy2(src_f, dst_f)
                if not config.IS_WINDOWS:
                    try:
                        os.chmod(dst_f, 0o644)
                    except OSError:
                        pass
            copied_any = True
        except OSError:
            continue

    return f"/uploads/{safe_cid}" if copied_any else ""


def _get_container_lock(container_name: str) -> asyncio.Lock:
    lock = _CONTAINER_LOCKS.get(container_name)
    if lock is None:
        lock = asyncio.Lock()
        _CONTAINER_LOCKS[container_name] = lock
    return lock


def _discard_container_lock(container_name: str) -> None:
    lock = _CONTAINER_LOCKS.get(container_name)
    if lock is not None and not lock.locked():
        _CONTAINER_LOCKS.pop(container_name, None)


async def _run_docker_short(args: list[str], *, timeout: float = 20.0) -> tuple[int, str, str]:
    docker_bin = config.resolve_docker_bin()
    if not docker_bin:
        return (127, "", "Docker CLI не найден на сервере")
    try:
        proc = await asyncio.create_subprocess_exec(
            docker_bin,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **config.subprocess_hidden_kwargs(),
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                pass
            return (124, "", "Превышен таймаут обращения к Docker CLI")
        except asyncio.CancelledError:
            try:
                if proc.returncode is None:
                    proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except BaseException:
                pass
            raise
        return (
            proc.returncode if proc.returncode is not None else 1,
            out_b.decode("utf-8", errors="replace").strip(),
            err_b.decode("utf-8", errors="replace").strip(),
        )
    except FileNotFoundError:
        return (127, "", "Docker CLI не найден на сервере")
    except Exception as exc:
        return (1, "", str(exc))


def _sanitize_docker_label_value(val: str) -> str:
    clean = re.sub(r"[\r\n\x00]+", " ", str(val or "")).strip()
    return clean[:120]


async def _inspect_container_state(container_name: str) -> dict[str, Any]:
    fmt = (
        "{{.State.Running}}|{{.HostConfig.NetworkMode}}|"
        "{{index .Config.Labels \"agy.hub.network\"}}|"
        "{{index .Config.Labels \"agy.hub.schema\"}}|{{.State.StartedAt}}"
    )
    rc, out, _ = await _run_docker_short(["inspect", "--format", fmt, container_name], timeout=8.0)
    if rc != 0 or not out:
        return {
            "exists": False,
            "running": False,
            "network_mode": "",
            "label_network": "",
            "schema": "",
            "started_at": "",
        }
    parts = out.split("|", 4)
    while len(parts) < 5:
        parts.append("")
    running_str, host_net, label_net, schema_ver, started_at = parts
    return {
        "exists": True,
        "running": running_str.strip().lower() == "true",
        "network_mode": host_net.strip().lower(),
        "label_network": label_net.strip().lower(),
        "schema": schema_ver.strip().lower(),
        "started_at": started_at.strip(),
    }


def _network_matches(state: dict[str, Any], desired_network: str) -> bool:
    desired = "none" if desired_network == "none" else "internet"
    host_net = state.get("network_mode") or ""
    lbl_net = state.get("label_network") or ""
    if desired == "none":
        return host_net == "none"
    if host_net == "none":
        return False
    if lbl_net and lbl_net != desired:
        return False
    return True


async def ensure_warm_sandbox_container(
    *,
    user_id: str,
    project_dir: Path,
    project_name: str,
    sandbox_network: str = "internet",
    force_recreate: bool = False,
) -> str:
    """Ensure the isolated warm container `agy-sbx-...` is running with matching network & mount schema."""
    resolved_dir = project_dir.resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    _ensure_linux_workspace_permissions(resolved_dir)

    sbx_uploads_dir = build_sandbox_uploads_host_dir(user_id, resolved_dir)
    sbx_uploads_dir.mkdir(parents=True, exist_ok=True)
    if not config.IS_WINDOWS:
        try:
            os.chmod(sbx_uploads_dir, 0o755)
        except OSError:
            pass

    net_mode = "none" if str(sandbox_network or "").strip().lower() == "none" else "internet"
    container_name = build_sandbox_container_name(user_id, resolved_dir)
    container_user = resolve_sandbox_container_user()

    async with _get_container_lock(container_name):
        if not force_recreate:
            st = await _inspect_container_state(container_name)
            if (
                st["exists"]
                and st["running"]
                and _network_matches(st, net_mode)
                and st.get("schema") == SANDBOX_CONTAINER_SCHEMA
            ):
                _CONTAINER_LAST_USED[container_name] = time.monotonic()
                _CONTAINER_META[container_name] = {
                    "user_id": user_id,
                    "project_name": project_name,
                    "project_dir": str(resolved_dir),
                    "network": net_mode,
                }
                return container_name
            if st["exists"]:
                await _run_docker_short(["rm", "-f", container_name], timeout=15.0)
        else:
            await _run_docker_short(["rm", "-f", container_name], timeout=15.0)

        run_args: list[str] = [
            "run",
            "-d",
            "--init",
            "--name",
            container_name,
            "--label",
            "agy.hub.managed=1",
            "--label",
            f"agy.hub.schema={SANDBOX_CONTAINER_SCHEMA}",
            "--label",
            f"agy.hub.user_id={_sanitize_docker_label_value(user_id)}",
            "--label",
            f"agy.hub.project={_sanitize_docker_label_value(project_name)}",
            "--label",
            f"agy.hub.network={net_mode}",
            "--cpus=2.0",
            "--memory=2g",
            "--memory-swap=2g",
            "--pids-limit=128",
            "--user",
            container_user,
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "-e",
            "HOME=/home/dev",
            "-e",
            "CUDA_VISIBLE_DEVICES=",
            "-e",
            "NVIDIA_VISIBLE_DEVICES=none",
            "-e",
            "TERM=dumb",
            "-e",
            "CI=true",
            "--add-host=host.docker.internal:127.0.0.1",
            "--add-host=gateway.docker.internal:127.0.0.1",
        ]
        if net_mode == "none":
            run_args.extend(["--network", "none"])
        run_args.extend(
            [
                "-v",
                f"{str(resolved_dir)}:/workspace",
                "-v",
                f"{str(sbx_uploads_dir)}:/uploads:ro",
                "-w",
                "/workspace",
                config.DOCKER_SANDBOX_IMAGE,
                "sleep",
                "infinity",
            ]
        )

        rc, _, err = await _run_docker_short(run_args, timeout=30.0)
        if rc != 0:
            raise RuntimeError(
                f"Не удалось запустить Docker-песочницу ({config.DOCKER_SANDBOX_IMAGE}): {err or f'код {rc}'}"
            )

        now_mono = time.monotonic()
        _CONTAINER_LAST_USED[container_name] = now_mono
        _CONTAINER_STARTED_MONO[container_name] = now_mono
        _CONTAINER_META[container_name] = {
            "user_id": user_id,
            "project_name": project_name,
            "project_dir": str(resolved_dir),
            "network": net_mode,
        }
        return container_name


async def get_project_sandbox_status(
    user_id: str,
    project_dir: Path,
    project_name: str,
) -> dict[str, Any]:
    resolved_dir = project_dir.resolve()
    container_name = build_sandbox_container_name(user_id, resolved_dir)
    st = await _inspect_container_state(container_name)
    last_used = _CONTAINER_LAST_USED.get(container_name)
    idle_sec = max(0, int(time.monotonic() - last_used)) if last_used is not None and st["running"] else None
    eff_net = "none" if st.get("network_mode") == "none" else ("internet" if st.get("exists") else "")
    return {
        "container_name": container_name,
        "exists": st["exists"],
        "running": st["running"],
        "network_mode": eff_net,
        "started_at": st["started_at"],
        "idle_seconds": idle_sec,
        "idle_ttl_seconds": SANDBOX_IDLE_TTL_SECONDS,
        "project_name": project_name,
    }


async def reset_project_sandbox(
    user_id: str,
    project_dir: Path,
    project_name: str,
    sandbox_network: str = "internet",
) -> dict[str, Any]:
    await ensure_warm_sandbox_container(
        user_id=user_id,
        project_dir=project_dir,
        project_name=project_name,
        sandbox_network=sandbox_network,
        force_recreate=True,
    )
    return await get_project_sandbox_status(user_id, project_dir, project_name)


async def stop_user_sandboxes(user_id: str) -> int:
    clean_uid = (user_id or "").strip()
    if not clean_uid:
        return 0
    rc, out, _ = await _run_docker_short(
        [
            "ps",
            "-a",
            "--filter",
            "label=agy.hub.managed=1",
            "--filter",
            f"label=agy.hub.user_id={_sanitize_docker_label_value(clean_uid)}",
            "--format",
            "{{.Names}}",
        ],
        timeout=12.0,
    )
    if rc != 0 or not out:
        return 0
    names = [line.strip() for line in out.splitlines() if line.strip()]
    removed = 0
    for name in names:
        rm_rc, _, _ = await _run_docker_short(["rm", "-f", name], timeout=15.0)
        _CONTAINER_LAST_USED.pop(name, None)
        _CONTAINER_META.pop(name, None)
        _discard_container_lock(name)
        _cleanup_sandbox_uploads_host_dir(name)
        if rm_rc == 0:
            removed += 1
    return removed


async def stop_project_sandboxes(
    *,
    project_dir: Optional[Path] = None,
    project_name: str = "",
) -> int:
    """Stop and remove warm sandbox containers associated with an old `project_dir` or `project_name`
    so subsequent turns mount the updated `shared_projects.path` into `/workspace`."""
    candidates: set[str] = set()
    norm_dir = os.path.normcase(str(project_dir.resolve())) if project_dir is not None else ""
    clean_proj = (project_name or "").strip()

    for cname, meta in list(_CONTAINER_META.items()):
        m_dir = os.path.normcase(str(meta.get("project_dir") or ""))
        m_proj = str(meta.get("project_name") or "").strip()
        if (norm_dir and m_dir == norm_dir) or (clean_proj and m_proj.lower() == clean_proj.lower()):
            candidates.add(cname)

    if clean_proj:
        rc, out, _ = await _run_docker_short(
            [
                "ps",
                "-a",
                "--filter",
                "label=agy.hub.managed=1",
                "--filter",
                f"label=agy.hub.project={_sanitize_docker_label_value(clean_proj)}",
                "--format",
                "{{.Names}}",
            ],
            timeout=12.0,
        )
        if rc == 0 and out:
            for line in out.splitlines():
                if line.strip():
                    candidates.add(line.strip())

    removed = 0
    for name in sorted(candidates):
        rm_rc, _, _ = await _run_docker_short(["rm", "-f", name], timeout=15.0)
        _CONTAINER_LAST_USED.pop(name, None)
        _CONTAINER_META.pop(name, None)
        _discard_container_lock(name)
        _cleanup_sandbox_uploads_host_dir(name)
        if rm_rc == 0:
            removed += 1
    return removed


def _active_container_names_from_runs() -> set[str]:
    try:
        from .agy_engine import ACTIVE_RUNS
    except Exception:
        return set()
    busy: set[str] = set()
    for r in ACTIVE_RUNS.values():
        if not r.cancelled and r.kind == "project" and r.project_dir is not None and r.user_id:
            try:
                busy.add(build_sandbox_container_name(r.user_id, r.project_dir))
            except Exception:
                pass
    return busy


async def list_running_managed_container_names() -> set[str]:
    rc, out, _ = await _run_docker_short(
        [
            "ps",
            "--filter",
            "label=agy.hub.managed=1",
            "--format",
            "{{.Names}}",
        ],
        timeout=8.0,
    )
    if rc != 0 or not out:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


async def _container_has_live_bg_service(container_name: str) -> bool:
    check_script = (
        'for f in /tmp/agy_svc_*.pid; do '
        '[ -f "$f" ] || continue; '
        'p=$(cat "$f" 2>/dev/null || true); '
        'if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then '
        'echo "LIVE"; exit 0; '
        'fi; '
        'done; exit 1'
    )
    rc, out, _ = await _run_docker_short(
        [
            "exec",
            "-u",
            resolve_sandbox_container_user(),
            container_name,
            "bash",
            "-lc",
            check_script,
        ],
        timeout=5.0,
    )
    return rc == 0 and "LIVE" in out


async def _seed_managed_containers() -> None:
    names = await list_running_managed_container_names()
    if not names:
        return
    now = time.monotonic()
    for name in names:
        _CONTAINER_LAST_USED.setdefault(name, now)
        _CONTAINER_STARTED_MONO.setdefault(name, now)


async def _check_sandbox_service_crashes(container_name: str) -> None:
    tracked = _SANDBOX_WATCHED_SERVICES.get(container_name)
    if not tracked:
        return
    uid, project_dir, project_name, _ = next(iter(tracked.values()))
    snapshot = dict(tracked)
    views = await list_project_sandbox_services(uid, project_dir, project_name, running_containers={container_name})
    for view in views:
        name = _sanitize_sandbox_service_name(view["name"])
        record = snapshot.get(name)
        if not record or tracked.get(name) is not record or record[3] != view["started_at"] or view["alive"]:
            continue
        tracked.pop(name)
        if view["status"] == "crashed":
            error_tail = ""
            try:
                logs = await get_project_sandbox_service_logs(uid, project_dir, project_name, name, tail_lines=10)
                if logs.get("started_at") == view["started_at"]:
                    error_tail = str(logs.get("log_tail") or "")
            except (OSError, RuntimeError):
                pass
            _schedule_sandbox_processes_notify(project_name, project_dir, uid, {
                "transition": "crashed", "service": view, "error_tail": error_tail,
            })


async def reap_idle_sandboxes_once() -> int:
    rc, out, _ = await _run_docker_short(
        [
            "ps",
            "-a",
            "--filter",
            "label=agy.hub.managed=1",
            "--format",
            "{{.Names}}|{{.State}}",
        ],
        timeout=12.0,
    )
    if rc != 0 or not out:
        return 0

    now = time.monotonic()
    busy_names = _active_container_names_from_runs()
    reaped = 0

    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 1)
        name = parts[0].strip()
        state_str = parts[1].strip().lower() if len(parts) > 1 else ""
        if not name:
            continue
        if state_str == "running":
            await _check_sandbox_service_crashes(name)
        if name in busy_names:
            continue
        last_used = _CONTAINER_LAST_USED.get(name)
        if last_used is None:
            _CONTAINER_LAST_USED[name] = now
            _CONTAINER_STARTED_MONO.setdefault(name, now)
            continue
        is_dead = state_str in {"exited", "dead", "created"}
        if is_dead or (now - last_used) > SANDBOX_IDLE_TTL_SECONDS:
            if not is_dead:
                started_mono = _CONTAINER_STARTED_MONO.get(name, last_used)
                if (now - started_mono) < SANDBOX_MAX_BG_SERVICE_TTL_SECONDS:
                    if await _container_has_live_bg_service(name):
                        _CONTAINER_LAST_USED[name] = now
                        continue
            rm_rc, _, _ = await _run_docker_short(["rm", "-f", name], timeout=15.0)
            _CONTAINER_LAST_USED.pop(name, None)
            _CONTAINER_STARTED_MONO.pop(name, None)
            _CONTAINER_META.pop(name, None)
            _SANDBOX_WATCHED_SERVICES.pop(name, None)
            _discard_container_lock(name)
            _cleanup_sandbox_uploads_host_dir(name)
            if rm_rc == 0:
                reaped += 1
    return reaped


async def _sandbox_reaper_loop() -> None:
    try:
        await _seed_managed_containers()
    except Exception:
        pass
    while True:
        await asyncio.sleep(SANDBOX_REAPER_INTERVAL_SECONDS)
        try:
            await reap_idle_sandboxes_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


def start_sandbox_reaper() -> None:
    global _REAPER_TASK
    if _REAPER_TASK is not None and not _REAPER_TASK.done():
        return
    try:
        loop = asyncio.get_running_loop()
        _REAPER_TASK = loop.create_task(_sandbox_reaper_loop(), name="agy_sandbox_reaper")
    except RuntimeError:
        pass


async def stop_sandbox_reaper() -> None:
    global _REAPER_TASK
    task = _REAPER_TASK
    _REAPER_TASK = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# 4.4. Exec Tracking, Output Cleaning & Three `hub_sandbox` MCP Tools
# ---------------------------------------------------------------------------


def _clean_progress_cr(text: str) -> str:
    if not text:
        return ""
    norm = text.replace("\r\n", "\n")
    if "\r" not in norm:
        return norm
    cleaned_lines: list[str] = []
    for line in norm.split("\n"):
        if "\r" not in line:
            cleaned_lines.append(line)
            continue
        segments = [seg for seg in line.split("\r") if seg]
        cleaned_lines.append(segments[-1] if segments else "")
    return "\n".join(cleaned_lines)


def _truncate_text_with_marker(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    if budget <= 200:
        return text[:budget]
    head_len = min(_TRUNCATE_HEAD_CHARS, int(budget * 0.45))
    tail_len = min(_TRUNCATE_TAIL_CHARS, max(0, budget - head_len - 64))
    omitted = max(1, len(text) - head_len - tail_len)
    marker = f"\n...[вывод сокращён: {omitted} симв.]...\n"
    return text[:head_len] + marker + (text[-tail_len:] if tail_len > 0 else "")


def clean_and_limit_sandbox_output(stdout_raw: str, stderr_raw: str) -> tuple[str, str]:
    out = _clean_progress_cr(stdout_raw)
    err = _clean_progress_cr(stderr_raw)
    total_len = len(out) + len(err)
    if total_len <= SANDBOX_MAX_OUTPUT_CHARS:
        return out, err

    if not err:
        return _truncate_text_with_marker(out, SANDBOX_MAX_OUTPUT_CHARS), ""
    if not out:
        return "", _truncate_text_with_marker(err, SANDBOX_MAX_OUTPUT_CHARS)

    # Share the 32 000 char budget proportionally while guaranteeing visibility for both streams
    err_budget = max(4000, min(len(err), int(SANDBOX_MAX_OUTPUT_CHARS * (len(err) / total_len))))
    out_budget = max(4000, SANDBOX_MAX_OUTPUT_CHARS - err_budget)
    if out_budget + err_budget > SANDBOX_MAX_OUTPUT_CHARS:
        err_budget = max(2000, SANDBOX_MAX_OUTPUT_CHARS - out_budget)
    return (
        _truncate_text_with_marker(out, out_budget),
        _truncate_text_with_marker(err, err_budget),
    )


async def _kill_exec_processes_in_container(container_name: str, exec_tag: str) -> None:
    if not container_name or not exec_tag:
        return
    py_kill = (
        "import os, signal, time\n"
        f"needle = b'AGY_EXEC_TAG={exec_tag}\\x00'\n"
        "me = os.getpid()\n"
        "pids = []\n"
        "for d in os.listdir('/proc'):\n"
        "    if not d.isdigit(): continue\n"
        "    p = int(d)\n"
        "    if p == me: continue\n"
        "    try:\n"
        "        env = open(f'/proc/{p}/environ', 'rb').read()\n"
        "        if needle in env: pids.append(p)\n"
        "    except Exception: pass\n"
        "for p in pids:\n"
        "    try: os.kill(p, signal.SIGTERM)\n"
        "    except Exception: pass\n"
        "if pids:\n"
        "    time.sleep(0.2)\n"
        "    for p in pids:\n"
        "        try: os.kill(p, signal.SIGKILL)\n"
        "        except Exception: pass\n"
    )
    await _run_docker_short(
        ["exec", "-u", resolve_sandbox_container_user(), container_name, "python3", "-c", py_kill],
        timeout=5.0,
    )


def _register_tracked_exec(entry: _TrackedExec) -> None:
    bucket = _ACTIVE_EXECS_BY_CHAT.setdefault(entry.chat_id, [])
    bucket.append(entry)


def _unregister_tracked_exec(entry: _TrackedExec) -> None:
    bucket = _ACTIVE_EXECS_BY_CHAT.get(entry.chat_id)
    if not bucket:
        return
    try:
        bucket.remove(entry)
    except ValueError:
        pass
    if not bucket:
        _ACTIVE_EXECS_BY_CHAT.pop(entry.chat_id, None)


async def cancel_chat_sandbox_execs(chat_id: str) -> int:
    """Immediately kill all active `docker exec` subprocesses (and container children) for `chat_id`."""
    entries = list(_ACTIVE_EXECS_BY_CHAT.pop(chat_id, []))
    if not entries:
        return 0
    for entry in entries:
        try:
            if entry.proc.returncode is None:
                entry.proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            pass
    await asyncio.gather(
        *[_kill_exec_processes_in_container(e.container_name, e.exec_tag) for e in entries],
        return_exceptions=True,
    )
    return len(entries)


async def execute_sandbox_exec(
    run_ctx: ResolvedSandboxRun,
    *,
    command: str,
    workdir: str = ".",
    timeout_sec: int = 60,
) -> dict[str, Any]:
    clean_cmd = str(command or "").strip()
    if not clean_cmd:
        raise ValueError("Параметр command не может быть пустым.")

    safe_wd, _ = resolve_safe_container_workdir(workdir, run_ctx.project_dir)
    clamped_timeout = max(1, min(SANDBOX_MAX_TIMEOUT_SEC, int(timeout_sec or 60)))

    docker_bin = config.resolve_docker_bin()
    if not docker_bin:
        raise RuntimeError("Docker CLI не найден на сервере. Установите и запустите Docker.")

    uid_str = str(run_ctx.user["id"])
    container_name = await ensure_warm_sandbox_container(
        user_id=uid_str,
        project_dir=run_ctx.project_dir,
        project_name=run_ctx.project_name,
        sandbox_network=run_ctx.sandbox_network,
    )
    sbx_uploads_dir = build_sandbox_uploads_host_dir(uid_str, run_ctx.project_dir)
    container_uploads = await asyncio.to_thread(
        sync_chat_uploads_to_sandbox, run_ctx.chat_id, sbx_uploads_dir
    )

    exec_tag = secrets.token_hex(8)
    started = time.monotonic()

    exec_cmd: list[str] = [
        docker_bin,
        "exec",
        "-i",
        "-u",
        resolve_sandbox_container_user(),
        "-e",
        "HOME=/home/dev",
        "-e",
        f"AGY_EXEC_TAG={exec_tag}",
    ]
    if container_uploads:
        exec_cmd.extend(["-e", f"AGY_CHAT_UPLOADS={container_uploads}"])
    exec_cmd.extend(
        [
            "-w",
            safe_wd,
            container_name,
            "bash",
            "-lc",
            clean_cmd,
        ]
    )

    proc = await asyncio.create_subprocess_exec(
        *exec_cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **config.subprocess_hidden_kwargs(),
    )

    tracked = _TrackedExec(
        chat_id=run_ctx.chat_id,
        container_name=container_name,
        exec_tag=exec_tag,
        proc=proc,
    )
    _register_tracked_exec(tracked)
    timed_out = False

    try:
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=float(clamped_timeout))
        except asyncio.TimeoutError:
            timed_out = True
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await _kill_exec_processes_in_container(container_name, exec_tag)
            try:
                out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            except Exception:
                out_b, err_b = b"", b""
        except asyncio.CancelledError:
            try:
                if proc.returncode is None:
                    proc.kill()
            except ProcessLookupError:
                pass
            except Exception:
                pass
            try:
                await asyncio.wait_for(
                    _kill_exec_processes_in_container(container_name, exec_tag),
                    timeout=2.5,
                )
            except BaseException:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except BaseException:
                pass
            raise
    finally:
        _unregister_tracked_exec(tracked)
        _CONTAINER_LAST_USED[container_name] = time.monotonic()

    duration_ms = max(1, int((time.monotonic() - started) * 1000))
    stdout_str, stderr_str = clean_and_limit_sandbox_output(
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )
    if timed_out:
        timeout_note = f"[Таймаут {clamped_timeout} с превышен: процесс остановлен внутри Docker-песочницы]"
        stderr_str = f"{stderr_str}\n{timeout_note}".strip() if stderr_str else timeout_note
        exit_code = 124
    else:
        exit_code = proc.returncode if proc.returncode is not None else 1

    return {
        "ok": exit_code == 0 and not timed_out,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "workdir": safe_wd,
        "network_mode": run_ctx.sandbox_network,
        "container": container_name,
        "stdout": stdout_str,
        "stderr": stderr_str,
    }


def _format_sandbox_exec_report(res: dict[str, Any]) -> str:
    header = (
        f"[hub_sandbox · exit_code={res['exit_code']} · {res['duration_ms']} ms · "
        f"workdir={res['workdir']} · network={res['network_mode']}]"
    )
    parts = [header]
    if res.get("stdout"):
        parts.append(f"--- stdout ---\n{res['stdout']}")
    if res.get("stderr"):
        parts.append(f"--- stderr ---\n{res['stderr']}")
    if not res.get("stdout") and not res.get("stderr"):
        parts.append("(пустой вывод)")
    return "\n".join(parts)


_DOCKER_INSPECT_SERVICES_PY = r"""
import datetime, glob, json, os, re

def load_listening_ports():
    ports = set()
    for p in ("/proc/net/tcp", "/proc/net/tcp6"):
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.read().splitlines()[1:]
            for ln in lines:
                cols = ln.split()
                if len(cols) >= 4 and cols[3] == "0A":
                    local_addr = cols[1]
                    if ":" in local_addr:
                        ports.add(int(local_addr.split(":")[-1], 16))
        except Exception:
            pass
    return ports

def is_pid_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    stat_p = f"/proc/{pid}/stat"
    if os.path.exists(stat_p):
        try:
            with open(stat_p, "r", encoding="utf-8", errors="ignore") as fh:
                raw = fh.read()
            rp = raw.rfind(")")
            if rp > 0:
                rest = raw[rp + 2:].split()
                if rest and rest[0] == "Z":
                    return False
        except Exception:
            pass
    return True

def read_rss_mb(pid):
    st_p = f"/proc/{pid}/status"
    if not os.path.exists(st_p):
        return None
    try:
        with open(st_p, "r", encoding="utf-8", errors="ignore") as fh:
            for ln in fh:
                if ln.startswith("VmRSS:"):
                    m = re.search(r"(\d+)", ln)
                    if m:
                        return round(int(m.group(1)) / 1024.0, 1)
    except Exception:
        pass
    return None

stems = set()
for pat in ("/tmp/agy_svc_*.json", "/tmp/agy_svc_*.pid"):
    for path in glob.glob(pat):
        base = os.path.basename(path)
        stem = re.sub(r"\.(json|pid)$", "", base)
        if stem.startswith("agy_svc_"):
            stems.add(stem[len("agy_svc_"):])

if not stems:
    print("[]")
    raise SystemExit(0)

listening = load_listening_ports()
items = []
for safe_name in sorted(stems):
    prefix = f"/tmp/agy_svc_{safe_name}"
    meta = {}
    if os.path.exists(prefix + ".json"):
        try:
            with open(prefix + ".json", "r", encoding="utf-8", errors="ignore") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                meta = loaded
        except Exception:
            meta = {}
    pid = 0
    if os.path.exists(prefix + ".pid"):
        try:
            with open(prefix + ".pid", "r", encoding="utf-8", errors="ignore") as fh:
                pid = int((fh.read() or "0").strip() or "0")
        except Exception:
            pid = 0
    alive = is_pid_alive(pid)
    ram_mb = read_rss_mb(pid) if alive else None
    exit_code = None
    rc_iso = ""
    if os.path.exists(prefix + ".rc"):
        try:
            with open(prefix + ".rc", "r", encoding="utf-8", errors="ignore") as fh:
                rc_txt = (fh.read() or "").strip()
            if rc_txt:
                exit_code = int(rc_txt.splitlines()[0].strip())
            mtime = os.path.getmtime(prefix + ".rc")
            rc_iso = datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc).isoformat()
        except Exception:
            pass
    manual_stopped = bool(meta.get("manual_stopped"))
    ack_crash = bool(meta.get("ack_crash"))
    if alive:
        status = "running"
        exit_code = None
        crash_unseen = False
    elif manual_stopped:
        status = "stopped"
        crash_unseen = False
    elif exit_code is not None:
        if exit_code == 0:
            status = "completed"
            crash_unseen = False
        else:
            status = "crashed"
            crash_unseen = not ack_crash
    else:
        status = "stopped"
        crash_unseen = False

    port_val = meta.get("port")
    try:
        port_int = int(port_val) if port_val not in (None, "", 0, False) else None
    except Exception:
        port_int = None

    finished_at = str(meta.get("finished_at") or "")
    if not alive and status in ("completed", "crashed") and not finished_at:
        finished_at = rc_iso

    items.append({
        "name": str(meta.get("name") or safe_name),
        "safe_name": safe_name,
        "command": str(meta.get("command") or ""),
        "workdir": str(meta.get("workdir") or "."),
        "pid": pid if alive else 0,
        "alive": alive,
        "status": status,
        "exit_code": exit_code,
        "ram_mb": ram_mb,
        "port": port_int,
        "port_listening": bool(alive and port_int and port_int in listening),
        "started_at": str(meta.get("started_at") or ""),
        "finished_at": finished_at,
        "stopped_at": str(meta.get("stopped_at") or ""),
        "crash_unseen": crash_unseen,
        "log_path": prefix + ".log",
    })

print(json.dumps(items, ensure_ascii=False))
""".strip()


def _sanitize_sandbox_service_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(name or "app").strip())[:24] or "app"


def _normalize_sandbox_port(port: Any) -> Optional[int]:
    if port is None or port == "" or port is False:
        return None
    p = int(port)
    if p == 0:
        return None
    if p < 1 or p > 65535:
        raise ValueError("Порт должен быть в диапазоне 1..65535.")
    return p


def _compute_iso_duration_sec(started_at: str, end_at: Optional[str] = None) -> Optional[float]:
    raw_start = (started_at or "").strip()
    if not raw_start:
        return None
    try:
        start_dt = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    raw_end = (end_at or "").strip()
    if raw_end:
        try:
            end_dt = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
        except Exception:
            end_dt = datetime.now(timezone.utc)
    else:
        end_dt = datetime.now(timezone.utc)
    return round(max(0.0, (end_dt - start_dt).total_seconds()), 1)


async def list_project_sandbox_services(
    user_id: str,
    project_dir: Path,
    project_name: str,
    *,
    running_containers: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    """List background scripts/services in a warm Docker container WITHOUT starting a cold container."""
    clean_uid = str(user_id or "").strip()
    if not clean_uid or not project_name:
        return []
    resolved_dir = project_dir.resolve()
    container_name = build_sandbox_container_name(clean_uid, resolved_dir)

    if running_containers is not None:
        if container_name not in running_containers:
            return []
    else:
        st = await _inspect_container_state(container_name)
        if not st.get("running"):
            return []

    rc, out, _ = await _run_docker_short(
        [
            "exec",
            "-u",
            resolve_sandbox_container_user(),
            container_name,
            "python3",
            "-c",
            _DOCKER_INSPECT_SERVICES_PY,
        ],
        timeout=8.0,
    )
    if rc != 0 or not out.strip():
        return []
    try:
        raw_items = json.loads(out.strip())
    except Exception:
        return []
    if not isinstance(raw_items, list):
        return []

    views: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        alive = bool(item.get("alive"))
        status = str(item.get("status") or ("running" if alive else "stopped"))
        started_at = str(item.get("started_at") or "")
        finished_at = str(item.get("finished_at") or "")
        stopped_at = str(item.get("stopped_at") or "")
        end_ref = None if alive else (finished_at or stopped_at or None)
        duration_sec = _compute_iso_duration_sec(started_at, end_ref) if (alive or end_ref) else None
        views.append(
            {
                "runtime": "docker",
                "project_name": project_name,
                "project_dir": str(resolved_dir),
                "name": str(item.get("name") or item.get("safe_name") or "app"),
                "command": str(item.get("command") or ""),
                "workdir": str(item.get("workdir") or "."),
                "pid": int(item.get("pid") or 0) if alive else 0,
                "alive": alive,
                "status": status,
                "exit_code": item.get("exit_code"),
                "ram_mb": item.get("ram_mb") if alive else None,
                "duration_sec": duration_sec,
                "port": item.get("port"),
                "port_listening": bool(item.get("port_listening")) if alive else False,
                "allow_gpu": False,
                "started_at": started_at,
                "finished_at": finished_at,
                "stopped_at": stopped_at,
                "crash_unseen": bool(item.get("crash_unseen")),
                "log_path": str(item.get("log_path") or ""),
            }
        )

    views.sort(
        key=lambda s: (
            0 if s.get("alive") else (1 if s.get("status") == "crashed" else 2),
            str(s.get("name") or "").lower(),
        )
    )
    return views


_DOCKER_CLEAR_INACTIVE_SERVICES_PY = r"""
import glob, json, os, re

def is_pid_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    stat_p = f"/proc/{pid}/stat"
    if os.path.exists(stat_p):
        try:
            with open(stat_p, "r", encoding="utf-8", errors="ignore") as fh:
                raw = fh.read()
            rp = raw.rfind(")")
            if rp > 0:
                rest = raw[rp + 2:].split()
                if rest and rest[0] == "Z":
                    return False
        except Exception:
            pass
    return True

stems = set()
for pat in ("/tmp/agy_svc_*.json", "/tmp/agy_svc_*.pid"):
    for path in glob.glob(pat):
        base = os.path.basename(path)
        stem = re.sub(r"\.(json|pid)$", "", base)
        if stem.startswith("agy_svc_"):
            stems.add(stem[len("agy_svc_"):])

cleared_names = []
kept_running = 0
for safe_name in sorted(stems):
    prefix = f"/tmp/agy_svc_{safe_name}"
    pid = 0
    if os.path.exists(prefix + ".pid"):
        try:
            with open(prefix + ".pid", "r", encoding="utf-8", errors="ignore") as fh:
                pid = int((fh.read() or "0").strip() or "0")
        except Exception:
            pid = 0
    if is_pid_alive(pid):
        kept_running += 1
        continue
    svc_name = safe_name
    if os.path.exists(prefix + ".json"):
        try:
            with open(prefix + ".json", "r", encoding="utf-8", errors="ignore") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict) and loaded.get("name"):
                svc_name = str(loaded["name"])
        except Exception:
            pass
    for ext in (".json", ".pid", ".rc", ".rc.tmp", ".log"):
        try:
            os.remove(prefix + ext)
        except OSError:
            pass
    cleared_names.append(svc_name)

print(json.dumps({"cleared_names": cleared_names, "kept_running_count": kept_running}, ensure_ascii=False))
""".strip()


async def clear_inactive_services(
    user_id: str,
    project_dir: Path,
    project_name: str,
    *,
    running_containers: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Remove strictly inactive (`alive == False`) service records & logs in a warm Docker container."""
    clean_uid = str(user_id or "").strip()
    if not clean_uid or not project_name:
        return {
            "ok": True,
            "runtime": "docker",
            "project_name": project_name or "",
            "cleared_count": 0,
            "cleared_names": [],
            "kept_running_count": 0,
        }
    resolved_dir = project_dir.resolve()
    container_name = build_sandbox_container_name(clean_uid, resolved_dir)

    if running_containers is not None:
        if container_name not in running_containers:
            return {
                "ok": True,
                "runtime": "docker",
                "project_name": project_name,
                "cleared_count": 0,
                "cleared_names": [],
                "kept_running_count": 0,
            }
    else:
        st = await _inspect_container_state(container_name)
        if not st.get("running"):
            return {
                "ok": True,
                "runtime": "docker",
                "project_name": project_name,
                "cleared_count": 0,
                "cleared_names": [],
                "kept_running_count": 0,
            }

    rc, out, _ = await _run_docker_short(
        [
            "exec",
            "-u",
            resolve_sandbox_container_user(),
            container_name,
            "python3",
            "-c",
            _DOCKER_CLEAR_INACTIVE_SERVICES_PY,
        ],
        timeout=10.0,
    )
    cleared_names: list[str] = []
    kept_running = 0
    if rc == 0 and out.strip():
        try:
            parsed = json.loads(out.strip())
            if isinstance(parsed, dict):
                raw_names = parsed.get("cleared_names")
                if isinstance(raw_names, list):
                    cleared_names = [str(x) for x in raw_names if x]
                kept_running = int(parsed.get("kept_running_count") or 0)
        except Exception:
            cleared_names = []

    if cleared_names:
        _schedule_sandbox_processes_notify(project_name, resolved_dir, clean_uid)

    return {
        "ok": True,
        "runtime": "docker",
        "project_name": project_name,
        "cleared_count": len(cleared_names),
        "cleared_names": cleared_names,
        "kept_running_count": kept_running,
    }


clear_inactive_sandbox_services = clear_inactive_services


async def _read_sandbox_service_meta_json(container_name: str, safe_name: str) -> dict[str, Any]:
    meta_file = f"/tmp/agy_svc_{safe_name}.json"
    rc, out, _ = await _run_docker_short(
        [
            "exec",
            "-u",
            resolve_sandbox_container_user(),
            container_name,
            "bash",
            "-lc",
            f"if [ -f {meta_file} ]; then cat {meta_file} 2>/dev/null || true; fi",
        ],
        timeout=5.0,
    )
    if rc != 0 or not out.strip():
        return {}
    try:
        parsed = json.loads(out.strip())
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


async def get_project_sandbox_service_logs(
    user_id: str,
    project_dir: Path,
    project_name: str,
    name: str,
    *,
    tail_lines: int = 80,
) -> dict[str, Any]:
    safe_name = _sanitize_sandbox_service_name(name)
    clamped_tail = max(10, min(500, int(tail_lines or 80)))
    resolved_dir = project_dir.resolve()
    container_name = build_sandbox_container_name(str(user_id), resolved_dir)
    st = await _inspect_container_state(container_name)
    if not st.get("running"):
        return {
            "ok": True,
            "runtime": "docker",
            "name": safe_name,
            "project_name": project_name,
            "project_dir": str(resolved_dir),
            "alive": False,
            "status": "not_found",
            "pid": 0,
            "port": None,
            "crash_unseen": False,
            "log_tail": "",
        }

    services = await list_project_sandbox_services(
        str(user_id),
        resolved_dir,
        project_name,
        running_containers={container_name},
    )
    matched = next(
        (
            s
            for s in services
            if _sanitize_sandbox_service_name(str(s.get("name") or "")) == safe_name
        ),
        None,
    )
    log_file = f"/tmp/agy_svc_{safe_name}.log"
    _, out, _ = await _run_docker_short(
        [
            "exec",
            "-u",
            resolve_sandbox_container_user(),
            container_name,
            "bash",
            "-lc",
            f"if [ -f {log_file} ]; then tail -n {clamped_tail} {log_file} 2>/dev/null || true; fi",
        ],
        timeout=8.0,
    )
    tail_clean, _ = clean_and_limit_sandbox_output(out, "")
    base_view = matched or {
        "runtime": "docker",
        "name": safe_name,
        "project_name": project_name,
        "project_dir": str(resolved_dir),
        "command": "",
        "workdir": ".",
        "alive": False,
        "status": "not_found",
        "pid": 0,
        "exit_code": None,
        "ram_mb": None,
        "duration_sec": None,
        "port": None,
        "port_listening": False,
        "allow_gpu": False,
        "started_at": "",
        "finished_at": "",
        "stopped_at": "",
        "crash_unseen": False,
        "log_path": log_file,
    }
    return {
        "ok": True,
        **base_view,
        "log_tail": tail_clean,
    }


async def control_project_sandbox_service(
    user_id: str,
    project_dir: Path,
    project_name: str,
    *,
    action: str,
    name: str = "app",
    command: str = "",
    workdir: str = ".",
    port: Optional[int] = None,
    sandbox_network: str = "internet",
    tail_lines: int = 80,
    chat_id: str = "",
    ensure_warm_for_status: bool = False,
) -> dict[str, Any]:
    clean_action = str(action or "").strip().lower()
    allowed_actions = {"start", "restart", "status", "logs", "stop", "delete", "ack"}
    if clean_action not in allowed_actions:
        raise ValueError(
            "action должен быть одним из: 'start', 'restart', 'status', 'logs', 'stop', 'delete', 'ack'."
        )

    uid_str = str(user_id or "").strip()
    resolved_dir = project_dir.resolve()
    safe_name = _sanitize_sandbox_service_name(name)
    clamped_tail = max(10, min(300, int(tail_lines or 80)))
    norm_port = _normalize_sandbox_port(port)

    container_name = build_sandbox_container_name(uid_str, resolved_dir)

    if clean_action in {"stop", "delete", "ack", "status", "logs"} and not ensure_warm_for_status:
        st = await _inspect_container_state(container_name)
        if not st.get("running"):
            return {
                "ok": True,
                "action": clean_action,
                "runtime": "docker",
                "project_name": project_name,
                "project_dir": str(resolved_dir),
                "name": safe_name,
                "command": "",
                "workdir": ".",
                "pid": 0,
                "alive": False,
                "status": "deleted" if clean_action == "delete" else "stopped",
                "exit_code": None,
                "ram_mb": None,
                "duration_sec": None,
                "port": norm_port,
                "port_listening": False,
                "allow_gpu": False,
                "started_at": "",
                "finished_at": "",
                "stopped_at": "",
                "crash_unseen": False,
                "log_tail": "",
                "text": f"[hub_sandbox · service={safe_name} · action={clean_action} · workdir=.]\nSTATE=stopped PID=none",
            }

    clean_cmd = str(command or "").strip()
    eff_workdir_input = str(workdir if workdir is not None else ".").strip() or "."

    if clean_action == "restart" and not clean_cmd:
        st_restart = await _inspect_container_state(container_name)
        if not st_restart.get("running"):
            raise ValueError(
                f"Сервис «{safe_name}» не найден в активном контейнере. Передайте непустой command для запуска."
            )
        saved_meta = await _read_sandbox_service_meta_json(container_name, safe_name)
        clean_cmd = str(saved_meta.get("command") or "").strip()
        if not clean_cmd:
            raise ValueError(
                f"Для action='restart' сервиса «{safe_name}» не найдена сохранённая команда; передайте command явно."
            )
        if eff_workdir_input in ("", ".") and saved_meta.get("workdir"):
            eff_workdir_input = str(saved_meta.get("workdir") or ".")
        if norm_port is None and saved_meta.get("port"):
            norm_port = _normalize_sandbox_port(saved_meta.get("port"))

    if clean_action == "start" and not clean_cmd:
        raise ValueError("Для action='start' необходимо передать непустой command.")

    safe_wd, rel_wd_display = resolve_safe_container_workdir(eff_workdir_input, resolved_dir)

    container_name = await ensure_warm_sandbox_container(
        user_id=uid_str,
        project_dir=resolved_dir,
        project_name=project_name,
        sandbox_network=sandbox_network,
    )
    _CONTAINER_LAST_USED[container_name] = time.monotonic()

    container_uploads = ""
    if chat_id:
        sbx_uploads_dir = build_sandbox_uploads_host_dir(uid_str, resolved_dir)
        container_uploads = await asyncio.to_thread(
            sync_chat_uploads_to_sandbox, chat_id, sbx_uploads_dir
        )

    pid_file = f"/tmp/agy_svc_{safe_name}.pid"
    log_file = f"/tmp/agy_svc_{safe_name}.log"
    json_file = f"/tmp/agy_svc_{safe_name}.json"
    rc_file = f"/tmp/agy_svc_{safe_name}.rc"

    stop_snippet = (
        f"if [ -f {pid_file} ]; then "
        f"OLD_PID=$(cat {pid_file} 2>/dev/null || true); "
        'if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then '
        'kill -TERM -"$OLD_PID" 2>/dev/null || kill -TERM "$OLD_PID" 2>/dev/null || true; '
        "sleep 0.5; "
        'if kill -0 "$OLD_PID" 2>/dev/null; then '
        'kill -KILL -"$OLD_PID" 2>/dev/null || kill -KILL "$OLD_PID" 2>/dev/null || true; '
        "fi; "
        "fi; "
        f"rm -f {pid_file}; "
        "fi"
    )

    extra_env: list[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    if clean_action in {"start", "restart"}:
        meta_payload = {
            "name": safe_name,
            "command": clean_cmd,
            "workdir": rel_wd_display or ".",
            "started_at": now_iso,
            "finished_at": "",
            "stopped_at": "",
            "manual_stopped": False,
            "ack_crash": False,
            "port": norm_port,
        }
        meta_b64 = base64.b64encode(json.dumps(meta_payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
        extra_env.extend(
            [
                "-e",
                f"AGY_SVC_CMD={clean_cmd}",
                "-e",
                f"AGY_SVC_RC={rc_file}",
                "-e",
                f"AGY_SVC_LOG={log_file}",
            ]
        )
        wrapper_inner = (
            'bash -lc "$AGY_SVC_CMD"; '
            '_RC=$?; '
            'printf "%s\\n" "$_RC" > "${AGY_SVC_RC}.tmp" && mv -f "${AGY_SVC_RC}.tmp" "$AGY_SVC_RC"'
        )
        quoted_wrapper = shlex.quote(wrapper_inner)
        script = (
            f"{stop_snippet}; "
            f"rm -f {rc_file} {rc_file}.tmp; "
            f"printf '%s' '{meta_b64}' | base64 -d > {json_file}; "
            f": > {log_file}; "
            f"if command -v setsid >/dev/null 2>&1; then "
            f"setsid bash -c {quoted_wrapper} > {log_file} 2>&1 < /dev/null & "
            f"else "
            f"nohup bash -c {quoted_wrapper} > {log_file} 2>&1 < /dev/null & "
            f"fi; "
            f'NEW_PID=$!; echo "$NEW_PID" > {pid_file}; '
            "sleep 0.4; "
            'if kill -0 "$NEW_PID" 2>/dev/null; then '
            'echo "STATE=running PID=$NEW_PID"; '
            "else "
            f'EXIT_RC=$(cat {rc_file} 2>/dev/null || echo "?"); '
            'echo "STATE=exited PID=$NEW_PID EXIT_CODE=$EXIT_RC"; '
            "fi; "
            f"tail -n {clamped_tail} {log_file} 2>/dev/null || true"
        )
    elif clean_action == "status":
        script = (
            f"if [ -f {pid_file} ]; then "
            f"CUR_PID=$(cat {pid_file} 2>/dev/null || true); "
            'if [ -n "$CUR_PID" ] && kill -0 "$CUR_PID" 2>/dev/null; then '
            'echo "STATE=running PID=$CUR_PID"; '
            "else "
            f'EXIT_RC=$(cat {rc_file} 2>/dev/null || echo ""); '
            'echo "STATE=stopped PID=${CUR_PID:-none} EXIT_CODE=${EXIT_RC:-none}"; '
            "fi; "
            "else "
            'echo "STATE=stopped PID=none"; '
            "fi; "
            f"if [ -f {log_file} ]; then tail -n 25 {log_file} 2>/dev/null || true; fi"
        )
    elif clean_action == "logs":
        script = (
            f"if [ -f {log_file} ]; then "
            f"tail -n {clamped_tail} {log_file} 2>/dev/null || true; "
            "else "
            f'echo "(лог-файл {log_file} ещё не создан)"; '
            "fi"
        )
    elif clean_action == "stop":
        saved_meta = await _read_sandbox_service_meta_json(container_name, safe_name)
        saved_meta.setdefault("name", safe_name)
        saved_meta["manual_stopped"] = True
        saved_meta["stopped_at"] = now_iso
        saved_meta["ack_crash"] = True
        meta_b64 = base64.b64encode(json.dumps(saved_meta, ensure_ascii=False).encode("utf-8")).decode("ascii")
        script = (
            f"printf '%s' '{meta_b64}' | base64 -d > {json_file}; "
            f"{stop_snippet}; "
            f'echo "STATE=stopped NAME={safe_name}"'
        )
    elif clean_action == "delete":
        script = (
            f"{stop_snippet}; "
            f"rm -f {pid_file} {log_file} {json_file} {rc_file} {rc_file}.tmp; "
            f'echo "STATE=deleted NAME={safe_name}"'
        )
    else:  # ack
        saved_meta = await _read_sandbox_service_meta_json(container_name, safe_name)
        if saved_meta:
            saved_meta["ack_crash"] = True
            meta_b64 = base64.b64encode(json.dumps(saved_meta, ensure_ascii=False).encode("utf-8")).decode("ascii")
            script = f"printf '%s' '{meta_b64}' | base64 -d > {json_file}; echo \"STATE=acked NAME={safe_name}\""
        else:
            script = f'echo "STATE=acked NAME={safe_name}"'

    svc_exec_args: list[str] = [
        "exec",
        "-u",
        resolve_sandbox_container_user(),
        "-e",
        "HOME=/home/dev",
    ]
    if container_uploads:
        svc_exec_args.extend(["-e", f"AGY_CHAT_UPLOADS={container_uploads}"])
    svc_exec_args.extend(extra_env)
    svc_exec_args.extend(["-w", safe_wd, container_name, "bash", "-lc", script])

    rc, out, err = await _run_docker_short(
        svc_exec_args,
        timeout=15.0,
    )
    out_clean, err_clean = clean_and_limit_sandbox_output(out, err)
    first_line = out_clean.splitlines()[0] if out_clean else ""
    exited_nonzero = False
    if clean_action in {"start", "restart"} and "STATE=exited" in first_line:
        m_rc = re.search(r"EXIT_CODE=(-?\d+)", first_line)
        if not m_rc or int(m_rc.group(1)) != 0:
            exited_nonzero = True
    is_err = rc != 0 or exited_nonzero
    header = f"[hub_sandbox · service={safe_name} · action={clean_action} · workdir={safe_wd}]"
    body = out_clean or err_clean or "(нет вывода)"
    formatted_text = f"{header}\n{body}" + (f"\n--- stderr ---\n{err_clean}" if err_clean and out_clean else "")

    view: dict[str, Any] = {
        "runtime": "docker",
        "project_name": project_name,
        "project_dir": str(resolved_dir),
        "name": safe_name,
        "command": clean_cmd,
        "workdir": rel_wd_display or ".",
        "pid": 0,
        "alive": False,
        "status": "deleted" if clean_action == "delete" else "stopped",
        "exit_code": None,
        "ram_mb": None,
        "duration_sec": None,
        "port": norm_port,
        "port_listening": False,
        "allow_gpu": False,
        "started_at": now_iso if clean_action in {"start", "restart"} else "",
        "finished_at": "",
        "stopped_at": now_iso if clean_action == "stop" else "",
        "crash_unseen": False,
    }
    if clean_action != "delete":
        services = await list_project_sandbox_services(
            uid_str,
            resolved_dir,
            project_name,
            running_containers={container_name},
        )
        matched = next(
            (
                s
                for s in services
                if _sanitize_sandbox_service_name(str(s.get("name") or "")) == safe_name
            ),
            None,
        )
        if matched:
            view = matched

    log_lines = out_clean.splitlines()
    tail_body = "\n".join(log_lines[1:]) if (log_lines and log_lines[0].startswith("STATE=")) else out_clean
    transition = None
    if clean_action in {"start", "restart"} and view["alive"]:
        tracked = _SANDBOX_WATCHED_SERVICES.setdefault(container_name, {})
        tracked[safe_name] = (uid_str, resolved_dir, project_name, view["started_at"])
    elif clean_action in {"start", "restart"} and view["status"] == "crashed":
        _SANDBOX_WATCHED_SERVICES.get(container_name, {}).pop(safe_name, None)
        transition = {"transition": "crashed", "service": view, "error_tail": tail_body}
    elif clean_action in {"stop", "delete"} and not is_err:
        _SANDBOX_WATCHED_SERVICES.get(container_name, {}).pop(safe_name, None)
    if clean_action in {"start", "restart", "stop", "delete", "ack"}:
        _schedule_sandbox_processes_notify(project_name, resolved_dir, uid_str, transition)
    return {
        "ok": not is_err,
        "action": clean_action,
        **view,
        "log_tail": tail_body,
        "text": formatted_text,
    }


async def execute_sandbox_service_ctl(
    run_ctx: ResolvedSandboxRun,
    *,
    action: str,
    name: str = "app",
    command: str = "",
    workdir: str = ".",
    port: Optional[int] = None,
    tail_lines: int = 80,
) -> dict[str, Any]:
    return await control_project_sandbox_service(
        user_id=str(run_ctx.user["id"]),
        project_dir=run_ctx.project_dir,
        project_name=run_ctx.project_name,
        action=action,
        name=name,
        command=command,
        workdir=workdir,
        port=port,
        sandbox_network=run_ctx.sandbox_network,
        tail_lines=tail_lines,
        chat_id=run_ctx.chat_id,
        ensure_warm_for_status=True,
    )


async def execute_sandbox_reset(run_ctx: ResolvedSandboxRun) -> dict[str, Any]:
    await cancel_chat_sandbox_execs(run_ctx.chat_id)
    uid_str = str(run_ctx.user["id"])
    st = await reset_project_sandbox(
        user_id=uid_str,
        project_dir=run_ctx.project_dir,
        project_name=run_ctx.project_name,
        sandbox_network=run_ctx.sandbox_network,
    )
    _schedule_sandbox_processes_notify(run_ctx.project_name, run_ctx.project_dir, uid_str)
    return {
        "ok": True,
        "status": st,
        "text": (
            f"[hub_sandbox · reset выполнен · container={st['container_name']} · network={run_ctx.sandbox_network}]\n"
            "Чистый тёплый Docker-контейнер пересоздан и запущен. "
            "Все файлы кода в /workspace полностью сохранены, фоновые процессы остановлены, а /tmp очищен."
        ),
    }


# ---------------------------------------------------------------------------
# Streamable HTTP MCP JSON-RPC 2.0 Dispatcher (`/mcp/sandbox/{mcp_token}`)
# ---------------------------------------------------------------------------


def _mcp_tools_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "sandbox_exec",
            "description": (
                "Выполнить короткую bash-команду внутри изолированного Linux Docker-контейнера проекта (/workspace). "
                "Доступны Node.js 22, npm, pnpm, Python 3, pip, pytest, Rust/Cargo, git, curl, ps, ss. "
                "Для веб-серверов, ботов и длительных фоновых скриптов/парсеров/воркеров используй sandbox_service_ctl вместо блокировки sandbox_exec."
            ),
            "annotations": {
                "title": "Execute Sandbox Command",
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": False,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Команда bash для выполнения внутри /workspace.",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Относительная подпапка внутри /workspace (по умолчанию '.').",
                        "default": ".",
                    },
                    "timeout_sec": {
                        "type": "integer",
                        "description": "Таймаут выполнения в секундах (1..180, по умолчанию 60).",
                        "default": 60,
                        "minimum": 1,
                        "maximum": SANDBOX_MAX_TIMEOUT_SEC,
                    },
                    "project": {
                        "type": "string",
                        "description": "Необязательное имя проекта (нужно только если параллельно открыты несколько разных проектов).",
                        "default": "",
                    },
                },
                "required": ["command"],
            },
        },
        {
            "name": "sandbox_service_ctl",
            "description": (
                "Управление фоновым процессом, длительным скриптом, парсером, воркером или веб-сервером внутри тёплой Docker-песочницы проекта "
                "(start / restart / status / logs / stop / delete) без блокировки хода и с живой карточкой мониторинга в UI Hub. "
                "Логи пишутся в /tmp/agy_svc_<name>.log."
            ),
            "annotations": {
                "title": "Manage Sandbox Background Service",
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": False,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["start", "restart", "status", "logs", "stop", "delete"],
                        "description": "Действие: 'start', 'restart', 'status', 'logs', 'stop' или 'delete'.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Короткое имя сервиса или скрипта (например 'app', 'api', 'parser', 'worker'). По умолчанию 'app'.",
                        "default": "app",
                    },
                    "command": {
                        "type": "string",
                        "description": "Команда запуска сервиса (обязательна для action='start'; при 'restart' можно опустить для повтора сохранённой команды).",
                        "default": "",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Относительная подпапка внутри /workspace (по умолчанию '.').",
                        "default": ".",
                    },
                    "port": {
                        "type": "integer",
                        "description": "Необязательный TCP-порт сервиса (1..65535) для отображения ссылки и проверки прослушивания.",
                    },
                    "tail_lines": {
                        "type": "integer",
                        "description": "Количество последних строк лога (10..300, по умолчанию 80).",
                        "default": 80,
                        "minimum": 10,
                        "maximum": 300,
                    },
                    "project": {
                        "type": "string",
                        "description": "Необязательное имя проекта при нескольких параллельных ходах.",
                        "default": "",
                    },
                },
                "required": ["action"],
            },
        },
        {
            "name": "sandbox_reset",
            "description": (
                "Пересоздать тёплый Docker-контейнер текущего проекта с нуля (docker rm -f + чистый старт). "
                "Все файлы проекта в /workspace сохраняются, а зависшие процессы и /tmp полностью очищаются."
            ),
            "annotations": {
                "title": "Reset Sandbox Container",
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Необязательное имя проекта при нескольких параллельных ходах.",
                        "default": "",
                    },
                },
            },
        },
    ]


def _tool_error_result(req_id: Any, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [{"type": "text", "text": f"Ошибка hub_sandbox: {message}"}],
            "isError": True,
        },
    }


async def _dispatch_sandbox_jsonrpc_message(
    msg: dict[str, Any],
    user: dict[str, Any],
) -> Optional[dict[str, Any]]:
    method = str(msg.get("method") or "").strip()
    has_id = "id" in msg
    req_id = msg.get("id")

    if not has_id or method.startswith("notifications/"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "initialize":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        client_proto = str(params.get("protocolVersion") or "").strip()
        negotiated = client_proto if client_proto in _SUPPORTED_MCP_PROTOCOLS else MCP_PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": MCP_SANDBOX_SERVER_NAME,
                    "version": "1.0.0",
                },
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": _mcp_tools_definitions()},
        }

    if method == "tools/call":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        tool_name = str(params.get("name") or "").strip()
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}

        if tool_name not in {"sandbox_exec", "sandbox_service_ctl", "sandbox_reset"}:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Неизвестный инструмент MCP: {tool_name}"},
            }

        try:
            run_ctx = resolve_active_sandbox_run(
                user,
                requested_project=str(args.get("project") or ""),
            )
        except PermissionError as perm_err:
            return _tool_error_result(req_id, str(perm_err))

        try:
            if tool_name == "sandbox_exec":
                exec_res = await execute_sandbox_exec(
                    run_ctx,
                    command=str(args.get("command") or ""),
                    workdir=str(args.get("workdir") if args.get("workdir") is not None else "."),
                    timeout_sec=int(args.get("timeout_sec") if args.get("timeout_sec") is not None else 60),
                )
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": _format_sandbox_exec_report(exec_res)}],
                        "isError": not bool(exec_res.get("ok")),
                    },
                }

            if tool_name == "sandbox_service_ctl":
                raw_port = args.get("port")
                port_arg = int(raw_port) if raw_port not in (None, "", False) else None
                svc_res = await execute_sandbox_service_ctl(
                    run_ctx,
                    action=str(args.get("action") or ""),
                    name=str(args.get("name") if args.get("name") is not None else "app"),
                    command=str(args.get("command") or ""),
                    workdir=str(args.get("workdir") if args.get("workdir") is not None else "."),
                    port=port_arg,
                    tail_lines=int(args.get("tail_lines") if args.get("tail_lines") is not None else 80),
                )
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": svc_res["text"]}],
                        "isError": not bool(svc_res.get("ok")),
                    },
                }

            reset_res = await execute_sandbox_reset(run_ctx)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": reset_res["text"]}],
                    "isError": False,
                },
            }
        except (ValueError, RuntimeError) as err:
            return _tool_error_result(req_id, str(err))
        except Exception as exc:
            return _tool_error_result(req_id, f"Внутренняя ошибка песочницы: {exc}")

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Метод JSON-RPC не поддерживается: {method}"},
    }


async def handle_mcp_sandbox_http(request: Request, mcp_token: str) -> Response:
    """FastAPI Streamable HTTP MCP handler for `POST`, `GET`, `DELETE` `/mcp/sandbox/{mcp_token}`."""
    if not is_loopback_mcp_request(request):
        return JSONResponse(
            status_code=403,
            content={"error": "Доступ к MCP Docker-песочницы разрешён строго с loopback хоста (127.0.0.1 / ::1)."},
        )

    user = await _authenticate_sandbox_user_from_token(mcp_token)
    if not user:
        return JSONResponse(
            status_code=403,
            content={"error": "MCP-токен Docker-песочницы недействителен, отозван или доступ отключён."},
        )

    method = request.method.upper()
    if method == "GET":
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})

    if method == "DELETE":
        return Response(status_code=200)

    if method != "POST":
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})

    try:
        raw_body = await request.body()
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else None
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Некорректный JSON в запросе MCP"},
            },
        )

    if isinstance(payload, list):
        if not payload:
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Пустой batch-массив JSON-RPC"},
                },
            )
        responses: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            resp_obj = await _dispatch_sandbox_jsonrpc_message(item, user)
            if resp_obj is not None:
                responses.append(resp_obj)
        if not responses:
            return Response(status_code=202)
        return JSONResponse(status_code=200, content=responses)

    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Ожидался JSON-RPC объект"},
            },
        )

    resp_obj = await _dispatch_sandbox_jsonrpc_message(payload, user)
    if resp_obj is None:
        return Response(status_code=202)
    return JSONResponse(status_code=200, content=resp_obj)
