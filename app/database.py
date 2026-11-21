from __future__ import annotations

import asyncio
import base64
import contextvars
import copy
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from urllib.parse import urlsplit
import weakref
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken

from . import config
from .agy_engine import (
    classify_model_family,
    compute_chat_context_stats,
    get_model_context_window,
    resolve_model_context_limits,
)
from .config import (
    DB_PATH,
    HUB_SECRET_KEY,
    MAX_CUSTOM_RULES_BYTES,
    MAX_OPENROUTER_RULES_BYTES,
    PROJECTS_ROOT,
    SHARED_PROJECTS_DIR,
    USERS_DIR,
    USER_WORKSPACES_DIR,
    cleanup_user_cli_home,
    ensure_user_cli_home,
    ensure_user_claude_home,
    ensure_user_codex_home,
    get_claude_cli_info,
    get_codex_cli_info,
    get_or_create_hub_secret,
    get_pin,
    sync_user_cli_rules,
)

PBKDF2_ITERATIONS = 210_000
PROXY_PBKDF2_ITERATIONS = 200_000


@functools.lru_cache(maxsize=256)
def _pbkdf2_fernet_cached(secret: str, salt: bytes, iterations: int) -> Fernet:
    """v110: LRU-кэш PBKDF2-HMAC-SHA256 (120k–200k итераций) — без повторной блокировки Event Loop."""
    raw_key = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations, dklen=32)
    return Fernet(base64.urlsafe_b64encode(raw_key))


def _derive_proxy_fernet(scope_id: str) -> Fernet:
    """Domain-isolated Fernet key via PBKDF2-HMAC-SHA256 (200k) bound to `scope_id` (`_global`, `agy_admin`, `chatgpt:<uid>`, `claude:<uid>`)."""
    clean_scope = (scope_id or "").strip()
    if not clean_scope:
        raise ValueError("Не указан идентификатор домена шифрования прокси")
    secret = get_or_create_hub_secret()
    salt = b"agy-hub-proxy-v1:" + clean_scope.encode("utf-8")
    return _pbkdf2_fernet_cached(secret, salt, PROXY_PBKDF2_ITERATIONS)


def encrypt_scoped_proxy_spec(scope_id: str, raw_spec: str) -> str:
    clean = (raw_spec or "").strip()
    if not clean:
        return ""
    return _derive_proxy_fernet(scope_id).encrypt(clean.encode("utf-8")).decode("ascii")


def decrypt_scoped_proxy_spec(scope_id: str, encrypted_spec: str) -> str:
    tok = (encrypted_spec or "").strip()
    if not tok:
        return ""
    try:
        return _derive_proxy_fernet(scope_id).decrypt(tok.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, Exception):
        return ""


def _safe_label_for_proxy_spec(raw_spec: str, explicit_label: Optional[str] = None) -> str:
    if explicit_label and str(explicit_label).strip():
        return str(explicit_label).strip()[:160]
    clean = (raw_spec or "").strip()
    if not clean:
        return ""
    try:
        from .proxy_router import parse_proxy_spec

        return parse_proxy_spec(clean).safe_label[:160]
    except Exception:
        sp_part = clean.split("://", 1)[0].upper() if "://" in clean else "PROXY"
        return f"{sp_part[:12]} · настроен"


def _normalize_account_proxy_mode(raw_mode: Optional[str]) -> str:
    m = str(raw_mode or "global").strip().lower()
    if m == "custom":
        return "custom"
    if m == "global":
        return "global"
    raise ValueError("Недопустимый режим маршрута аккаунта (разрешены 'global' или 'custom')")


def _prepare_account_proxy_columns(
    *,
    scope_id: str,
    existing_row: Optional[Mapping[str, Any]],
    proxy_mode: Optional[str],
    proxy_spec: Optional[str],
    proxy_label: Optional[str] = None,
) -> tuple[str, str, str]:
    """Compute `(proxy_mode, proxy_spec_encrypted, proxy_label)` for create/edit of an account row.
    - Preserves existing encrypted secret when `proxy_mode=='custom'` and `proxy_spec` is blank/None on edit.
    - Wipes `proxy_spec_encrypted` and `proxy_label` when switched to `proxy_mode=='global'`."""
    ex_mode_raw = str((existing_row or {}).get("proxy_mode") or "global").strip().lower()
    ex_mode = "custom" if ex_mode_raw == "custom" else "global"
    ex_enc = str((existing_row or {}).get("proxy_spec_encrypted") or "")
    ex_lbl = str((existing_row or {}).get("proxy_label") or "")

    clean_spec = str(proxy_spec or "").strip() if proxy_spec is not None else ""
    if proxy_mode is None and proxy_spec is None:
        return ex_mode, ex_enc, ex_lbl

    if proxy_mode is not None:
        eff_mode = _normalize_account_proxy_mode(proxy_mode)
    else:
        eff_mode = "custom" if clean_spec else ex_mode

    if eff_mode == "global":
        return "global", "", ""

    # eff_mode == "custom"
    if clean_spec:
        enc = encrypt_scoped_proxy_spec(scope_id, clean_spec)
        lbl = _safe_label_for_proxy_spec(clean_spec, proxy_label)
        return "custom", enc, lbl

    if ex_mode == "custom" and ex_enc:
        return "custom", ex_enc, (str(proxy_label).strip()[:160] if proxy_label else ex_lbl)

    raise ValueError("Укажите адрес прокси или ссылку Xray для режима «Свой прокси»")


def _eff_users_dir() -> Path:
    if USERS_DIR != config._BOOT_USERS_DIR:
        return USERS_DIR.resolve()
    if USER_WORKSPACES_DIR != config._BOOT_USER_WORKSPACES_DIR:
        return USER_WORKSPACES_DIR.resolve()
    return config.get_effective_users_dir()


def _eff_shared_projects_dir() -> Path:
    if SHARED_PROJECTS_DIR != config._BOOT_SHARED_PROJECTS_DIR:
        return SHARED_PROJECTS_DIR.resolve()
    return config.SHARED_PROJECTS_DIR.resolve()


def _eff_projects_root() -> Path:
    if PROJECTS_ROOT != config._BOOT_PROJECTS_ROOT:
        return PROJECTS_ROOT.resolve()
    return config.PROJECTS_ROOT.resolve()


def _eff_base_dir() -> Path:
    bdir = config.BASE_DIR.resolve()
    if bdir != config._BOOT_BASE_DIR.resolve():
        return bdir
    ddir = config.DATA_DIR.resolve()
    if ddir.parent != bdir:
        return ddir.parent.resolve()
    return bdir


def _ensure_user_space(user_or_username: Any) -> dict[str, Path]:
    slug = config.sanitize_user_folder_slug(user_or_username)
    users_root = _eff_users_dir()
    users_root.mkdir(parents=True, exist_ok=True)
    u_root = (users_root / slug).resolve()
    u_root.relative_to(users_root)
    space = {
        "root": u_root,
        "projects": (u_root / "projects").resolve(),
        "chats": (u_root / "chats").resolve(),
        "uploads": (u_root / "uploads").resolve(),
        "context": (u_root / "context").resolve(),
    }
    for p in space.values():
        p.mkdir(parents=True, exist_ok=True)
    return space


def _rewrite_relocated_path(
    raw_path: str,
    old_base: Path | str,
    new_base: Path | str,
) -> tuple[str, bool]:
    raw = str(raw_path or "")
    old_str = str(old_base or "").strip()
    new_str = str(new_base or "").strip()
    if not raw.strip() or not old_str or not new_str:
        return raw, False

    old_norm = re.sub(r"(?<!^)/+", "/", old_str.replace("\\", "/")).rstrip("/")
    raw_norm = re.sub(r"(?<!^)/+", "/", raw.strip().replace("\\", "/")).rstrip("/")
    if not old_norm or not raw_norm:
        return raw, False

    try:
        new_abs = str(Path(new_str).resolve())
    except OSError:
        new_abs = os.path.normpath(new_str)
    new_slash = new_abs.replace("\\", "/").rstrip("/")

    case_insensitive = os.name == "nt" or bool(re.match(r"^[A-Za-z]:/", old_norm))
    old_cmp = old_norm.lower() if case_insensitive else old_norm
    raw_cmp = raw_norm.lower() if case_insensitive else raw_norm
    use_forward_slashes = ("/" in raw) and ("\\" not in raw)

    if raw_cmp == old_cmp:
        rebuilt = new_slash if use_forward_slashes else new_abs
        return rebuilt, rebuilt != raw

    if raw_cmp.startswith(old_cmp + "/"):
        rel_tail = raw_norm[len(old_norm) :].lstrip("/")
        if use_forward_slashes:
            rebuilt = f"{new_slash}/{rel_tail}" if rel_tail else new_slash
        else:
            rebuilt = str(Path(new_abs) / Path(rel_tail)) if rel_tail else new_abs
        return rebuilt, rebuilt != raw

    return raw, False


def _same_normalized_base_path(a: str, b: str) -> bool:
    na = re.sub(r"(?<!^)/+", "/", (a or "").strip().replace("\\", "/")).rstrip("/")
    nb = re.sub(r"(?<!^)/+", "/", (b or "").strip().replace("\\", "/")).rstrip("/")
    if os.name == "nt" or re.match(r"^[A-Za-z]:/", na) or re.match(r"^[A-Za-z]:/", nb):
        return na.lower() == nb.lower()
    return na == nb


async def relocate_hub_paths_if_base_moved(
    db: aiosqlite.Connection,
    current_base_dir: Path,
) -> dict[str, int]:
    """Detect when `Antigravity_Hub` folder moved to another drive/path and atomically rewrite DB & service paths."""
    from . import host_service_manager

    cur_resolved = current_base_dir.resolve()
    cur_str = str(cur_resolved)
    now = utc_now_iso()
    stats = {
        "chats": 0,
        "shared_projects": 0,
        "users": 0,
        "messages": 0,
        "project_memories": 0,
        "host_services": 0,
    }

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS hub_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        """
    )
    cur_meta = await db.execute("SELECT value FROM hub_meta WHERE key = 'last_base_dir' LIMIT 1;")
    meta_row = await cur_meta.fetchone()
    old_base_str = str(meta_row[0] or "").strip() if meta_row else ""

    if old_base_str and not _same_normalized_base_path(old_base_str, cur_str):
        self_names = {"antigravity_hub", cur_resolved.name.lower()}
        old_tail_name = old_base_str.replace("\\", "/").rstrip("/").split("/")[-1].lower()
        if old_tail_name:
            self_names.add(old_tail_name)

        # 1. chats.project_path
        cur_c = await db.execute("SELECT id, project_name, project_path FROM chats;")
        for cid, pname, ppath in await cur_c.fetchall():
            pname_clean = str(pname or "").strip()
            ppath_clean = str(ppath or "").strip()
            if pname_clean.lower() in self_names:
                if ppath_clean != cur_str:
                    await db.execute("UPDATE chats SET project_path = ? WHERE id = ?;", (cur_str, cid))
                    stats["chats"] += 1
                continue
            new_pp, changed = _rewrite_relocated_path(ppath_clean, old_base_str, cur_resolved)
            if changed:
                await db.execute("UPDATE chats SET project_path = ? WHERE id = ?;", (new_pp, cid))
                stats["chats"] += 1

        # 2. shared_projects.path
        cur_sp = await db.execute("SELECT id, path FROM shared_projects;")
        for sp_id, sp_path in await cur_sp.fetchall():
            new_sp, changed = _rewrite_relocated_path(str(sp_path or ""), old_base_str, cur_resolved)
            if changed:
                await db.execute("UPDATE shared_projects SET path = ? WHERE id = ?;", (new_sp, sp_id))
                stats["shared_projects"] += 1

        # 3. users.workspace_dir
        cur_u = await db.execute("SELECT id, workspace_dir FROM users;")
        for uid, ws_path in await cur_u.fetchall():
            new_ws, changed = _rewrite_relocated_path(str(ws_path or ""), old_base_str, cur_resolved)
            if changed:
                await db.execute("UPDATE users SET workspace_dir = ? WHERE id = ?;", (new_ws, uid))
                stats["users"] += 1

        # 4. messages.attachments_json (`disk_path`)
        cur_m = await db.execute(
            "SELECT id, attachments_json FROM messages WHERE attachments_json != '' AND attachments_json != '[]';"
        )
        for mid, raw_att in await cur_m.fetchall():
            try:
                att_list = json.loads(raw_att) if raw_att else []
            except Exception:
                continue
            if not isinstance(att_list, list):
                continue
            att_changed = False
            for item in att_list:
                if not isinstance(item, dict):
                    continue
                dp = str(item.get("disk_path") or "")
                if dp:
                    new_dp, dp_ch = _rewrite_relocated_path(dp, old_base_str, cur_resolved)
                    if dp_ch:
                        item["disk_path"] = new_dp
                        att_changed = True
            if att_changed:
                await db.execute(
                    "UPDATE messages SET attachments_json = ? WHERE id = ?;",
                    (json.dumps(att_list, ensure_ascii=False), mid),
                )
                stats["messages"] += 1

        # 5. project_memories.extra_dirs_json
        cur_pm = await db.execute(
            "SELECT scope_key, extra_dirs_json FROM project_memories WHERE extra_dirs_json != '' AND extra_dirs_json != '[]';"
        )
        for sk, raw_ed in await cur_pm.fetchall():
            try:
                ed_list = json.loads(raw_ed) if raw_ed else []
            except Exception:
                continue
            if not isinstance(ed_list, list):
                continue
            ed_changed = False
            new_ed_list: list[str] = []
            for ed_item in ed_list:
                ed_str = str(ed_item or "")
                new_ed, ch = _rewrite_relocated_path(ed_str, old_base_str, cur_resolved)
                if ch:
                    ed_changed = True
                new_ed_list.append(new_ed)
            if ed_changed:
                await db.execute(
                    "UPDATE project_memories SET extra_dirs_json = ? WHERE scope_key = ?;",
                    (json.dumps(new_ed_list, ensure_ascii=False), sk),
                )
                stats["project_memories"] += 1

        # 6. Host services registry (`project_dir`, `workdir`, `log_path`)
        try:
            stats["host_services"] = host_service_manager.HOST_SERVICE_MANAGER.relocate_registry_paths(
                old_base_str, cur_resolved
            )
        except Exception:
            pass

    await db.execute(
        """
        INSERT INTO hub_meta (key, value, updated_at)
        VALUES ('last_base_dir', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
        """,
        (cur_str, now),
    )
    return stats


def _safe_move_or_copy_dir(src: Path, dst: Path) -> bool:
    """Safely move `src` directory to `dst`, falling back to non-symlink copy on Windows file locks."""
    if not src.is_dir() or config._is_junction_or_symlink(src):
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        try:
            shutil.move(str(src), str(dst))
            return True
        except OSError:
            pass

    dst.mkdir(parents=True, exist_ok=True)
    copied_any = False
    for root_str, dirs, files in os.walk(src):
        root_p = Path(root_str)
        dirs[:] = [d for d in dirs if not config._is_junction_or_symlink(root_p / d)]
        rel = root_p.relative_to(src)
        target_sub = dst / rel
        target_sub.mkdir(parents=True, exist_ok=True)
        for fname in files:
            sf = root_p / fname
            if config._is_junction_or_symlink(sf):
                continue
            df = target_sub / fname
            try:
                if not df.exists():
                    shutil.copy2(sf, df)
                copied_any = True
            except OSError:
                continue
    try:
        shutil.rmtree(src)
    except OSError:
        pass
    return copied_any or dst.is_dir()


async def _migrate_v96_to_v97_layout(
    db: aiosqlite.Connection,
    effective_base_dir: Path,
) -> None:
    """Idempotent v96 -> v97 internal directory & DB path migration."""
    from . import brain_reader

    now = utc_now_iso()
    eff_base_str = str(effective_base_dir.resolve())
    target_sp_root = _eff_shared_projects_dir()
    target_sp_root.mkdir(parents=True, exist_ok=True)
    (target_sp_root / "_uploads").mkdir(parents=True, exist_ok=True)
    (target_sp_root / "_context").mkdir(parents=True, exist_ok=True)

    # 1. Ensure `users/<slug>/{projects,chats,uploads,context}` for admin & all users
    _ensure_user_space("admin")
    cur_users = await db.execute("SELECT id, username, role, workspace_dir FROM users;")
    user_rows = await cur_users.fetchall()
    user_slug_by_id: dict[str, str] = {"admin": "admin"}
    uid_by_slug: dict[str, str] = {"admin": "admin"}

    for uid_raw, uname_raw, role_raw, ws_raw in user_rows:
        uid = str(uid_raw or "").strip()
        uname = str(uname_raw or "").strip()
        urole = str(role_raw or "user").strip().lower()
        u_obj = {"id": uid, "username": uname, "role": urole}
        slug = config.sanitize_user_folder_slug(u_obj)
        space = _ensure_user_space(u_obj)
        user_slug_by_id[uid] = slug
        uid_by_slug[slug] = uid

        if uid == "admin" or urole == "admin" or slug == "admin":
            desired_ws = (
                str(_eff_projects_root())
                if PROJECTS_ROOT != config._BOOT_PROJECTS_ROOT
                else str(space["projects"])
            )
        else:
            desired_ws = str(space["projects"])

        if str(ws_raw or "").strip() != desired_ws:
            await db.execute(
                "UPDATE users SET workspace_dir = ? WHERE id = ?;",
                (desired_ws, uid),
            )

    # 2. Pin `Antigravity_Hub` self-hosted project chats to `effective_base_dir`
    await db.execute(
        """
        UPDATE chats
        SET project_path = ?
        WHERE kind = 'project'
          AND LOWER(project_name) IN ('antigravity_hub', LOWER(?))
          AND project_path != ?;
        """,
        (eff_base_str, effective_base_dir.name, eff_base_str),
    )

    # 3. Migrate legacy `data/shared_projects/<Name>` -> `shared_projects/<Name>`
    legacy_sp_root = (config.DATA_DIR / "shared_projects").resolve()
    if legacy_sp_root != target_sp_root and legacy_sp_root.is_dir():
        for src_proj in list(legacy_sp_root.iterdir()):
            if (
                not src_proj.is_dir()
                or src_proj.name.startswith(".")
                or src_proj.name.lower() in {"_uploads", "_context"}
                or config._is_junction_or_symlink(src_proj)
            ):
                continue
            dst_proj = (target_sp_root / src_proj.name).resolve()
            if not dst_proj.exists() or brain_reader.inspect_project_dir_health(dst_proj)["is_empty_skeleton"]:
                if dst_proj.exists():
                    shutil.rmtree(dst_proj, ignore_errors=True)
                _safe_move_or_copy_dir(src_proj, dst_proj)
            if dst_proj.is_dir():
                brain_reader.ensure_isolated_project_git_boundary(dst_proj)
                await db.execute(
                    "UPDATE shared_projects SET path = ? WHERE LOWER(name) = LOWER(?);",
                    (str(dst_proj), src_proj.name),
                )
                await db.execute(
                    "UPDATE chats SET project_path = ? WHERE kind = 'project' AND LOWER(project_name) = LOWER(?);",
                    (str(dst_proj), src_proj.name),
                )

    # 4. Migrate legacy `data/workspaces/<slug>/<Proj>` -> `users/<slug>/projects/<Proj>`
    legacy_ws_root = (config.DATA_DIR / "workspaces").resolve()
    target_users_root = _eff_users_dir()
    if legacy_ws_root != target_users_root and legacy_ws_root.is_dir():
        for slug_dir in list(legacy_ws_root.iterdir()):
            if (
                not slug_dir.is_dir()
                or slug_dir.name.startswith(".")
                or config._is_junction_or_symlink(slug_dir)
            ):
                continue
            slug = config.sanitize_user_folder_slug(slug_dir.name)
            u_space = _ensure_user_space(slug)
            matched_uid = uid_by_slug.get(slug, "")
            for src_proj in list(slug_dir.iterdir()):
                if (
                    not src_proj.is_dir()
                    or src_proj.name.startswith(".")
                    or config._is_junction_or_symlink(src_proj)
                ):
                    continue
                dst_proj = (u_space["projects"] / src_proj.name).resolve()
                if not dst_proj.exists() or brain_reader.inspect_project_dir_health(dst_proj)["is_empty_skeleton"]:
                    if dst_proj.exists():
                        shutil.rmtree(dst_proj, ignore_errors=True)
                    _safe_move_or_copy_dir(src_proj, dst_proj)
                if dst_proj.is_dir():
                    brain_reader.ensure_isolated_project_git_boundary(dst_proj)
                    if matched_uid:
                        await db.execute(
                            """
                            UPDATE chats
                            SET project_path = ?
                            WHERE kind = 'project' AND owner_id = ? AND LOWER(project_name) = LOWER(?);
                            """,
                            (str(dst_proj), matched_uid, src_proj.name),
                        )
                    await db.execute(
                        """
                        UPDATE chats
                        SET project_path = ?
                        WHERE kind = 'project' AND LOWER(project_path) = LOWER(?);
                        """,
                        (str(dst_proj), str(src_proj.resolve())),
                    )

    # 5. Migrate legacy `data/uploads/<chat_id>` -> `users/<owner_slug>/uploads/<chat_id>` / `shared_projects/_uploads/<chat_id>`
    legacy_up_root = config.get_effective_legacy_uploads_dir()
    if legacy_up_root.is_dir():
        cur_sp_names = await db.execute("SELECT name FROM shared_projects;")
        shared_names_low = {str(r[0] or "").strip().lower() for r in await cur_sp_names.fetchall() if r and r[0]}
        cur_chats = await db.execute("SELECT id, owner_id, kind, project_name FROM chats;")
        chat_lookup: dict[str, tuple[str, str, str, str]] = {}
        for cid_r, own_r, kind_r, pname_r in await cur_chats.fetchall():
            cid_s = str(cid_r or "").strip()
            if not cid_s:
                continue
            info_t = (cid_s, str(own_r or "admin"), str(kind_r or "chat"), str(pname_r or ""))
            chat_lookup[cid_s] = info_t
            chat_lookup[config.sanitize_chat_folder_id(cid_s)] = info_t

        for src_up in list(legacy_up_root.iterdir()):
            if (
                not src_up.is_dir()
                or src_up.name.startswith(".")
                or config._is_junction_or_symlink(src_up)
            ):
                continue
            matched = chat_lookup.get(src_up.name)
            if not matched:
                continue  # Leave orphan upload folders untouched in `data/uploads`
            real_cid, own_id, c_kind, c_proj = matched
            safe_cid = config.sanitize_chat_folder_id(real_cid)
            is_shared_chat = c_kind == "project" and c_proj.strip().lower() in shared_names_low
            if is_shared_chat:
                dst_up = (target_sp_root / "_uploads" / safe_cid).resolve()
            else:
                own_slug = user_slug_by_id.get(own_id, config.sanitize_user_folder_slug(own_id))
                dst_up = (_ensure_user_space(own_slug)["uploads"] / safe_cid).resolve()

            if dst_up == src_up.resolve():
                continue
            if _safe_move_or_copy_dir(src_up, dst_up):
                cur_att = await db.execute(
                    "SELECT id, attachments_json FROM messages WHERE chat_id = ? AND attachments_json != '' AND attachments_json != '[]';",
                    (real_cid,),
                )
                for mid, raw_att in await cur_att.fetchall():
                    try:
                        att_items = json.loads(raw_att) if raw_att else []
                    except Exception:
                        continue
                    if not isinstance(att_items, list):
                        continue
                    changed_att = False
                    for att in att_items:
                        if not isinstance(att, dict):
                            continue
                        dp = str(att.get("disk_path") or "")
                        if dp:
                            new_dp, dp_ch = _rewrite_relocated_path(dp, src_up, dst_up)
                            if dp_ch:
                                att["disk_path"] = new_dp
                                changed_att = True
                    if changed_att:
                        await db.execute(
                            "UPDATE messages SET attachments_json = ? WHERE id = ?;",
                            (json.dumps(att_items, ensure_ascii=False), mid),
                        )

    await db.execute(
        """
        INSERT INTO hub_meta (key, value, updated_at)
        VALUES ('storage_layout_version', 'v97', ?)
        ON CONFLICT(key) DO UPDATE SET value = 'v97', updated_at = excluded.updated_at;
        """,
        (now,),
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_secret(secret: str) -> str:
    """Hash a password or PIN with salted PBKDF2-HMAC-SHA256."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${dk}"


def verify_secret(candidate: str, stored_hash: str) -> bool:
    if not candidate or not stored_hash:
        return False
    parts = stored_hash.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iters = int(parts[1])
    except ValueError:
        return False
    salt = parts[2]
    expected_dk = parts[3]
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        candidate.encode("utf-8"),
        salt.encode("utf-8"),
        iters,
    ).hex()
    return hmac.compare_digest(dk, expected_dk)


# ---------------------------------------------------------------------------
# v110: Пул тёплых соединений SQLite (вместо спавна потока ОС + сброса Page Cache
# на каждый `aiosqlite.connect`). Пул привязан к каноническому пути DB_PATH и к
# текущему Event Loop (между тестами pytest loop меняется — пул пересоздаётся).
# ---------------------------------------------------------------------------
_DB_POOL_MAX_SIZE = 4
_DB_POOL_ACQUIRE_TIMEOUT_SEC = 5.0
_DB_CONN_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA temp_store=MEMORY;",
    "PRAGMA cache_size=-16000;",
    "PRAGMA mmap_size=134217728;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA busy_timeout=15000;",
)

_active_pool_path: str = ""
_pool_loop: Optional[asyncio.AbstractEventLoop] = None
_pool_sem: Optional[asyncio.Semaphore] = None
_pool_generation: int = 0
_idle_conns: list[aiosqlite.Connection] = []
_all_conns: set[aiosqlite.Connection] = set()
_overflow_conns: set[aiosqlite.Connection] = set()
_conn_generation: "weakref.WeakKeyDictionary[aiosqlite.Connection, int]" = weakref.WeakKeyDictionary()
_DB_CONN_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("hub_db_conn_depth", default=0)
_RESOLVED_DB_PATH_MEMO: tuple[str, str] = ("", "")


class _HubTrackedSqliteConnection(sqlite3.Connection):
    """sqlite3.Connection, запоминающий свои курсоры.

    При возврате соединения в пул все курсоры аренды закрываются: недочитанный
    SELECT иначе удерживал бы read-snapshot WAL и следующий арендатор видел бы
    устаревшие данные (раньше это решалось закрытием соединения целиком).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._hub_cursors: "weakref.WeakSet[sqlite3.Cursor]" = weakref.WeakSet()

    def cursor(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:  # type: ignore[override]
        cur = super().cursor(*args, **kwargs)
        self._hub_cursors.add(cur)
        return cur

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:  # type: ignore[override]
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:  # type: ignore[override]
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:  # type: ignore[override]
        return self.cursor().executescript(sql_script)

    def _hub_reset_cursors(self) -> None:
        for cur in list(self._hub_cursors):
            try:
                cur.close()
            except Exception:
                pass
        self._hub_cursors = weakref.WeakSet()


def _resolved_db_path() -> str:
    global _RESOLVED_DB_PATH_MEMO
    raw = str(DB_PATH)
    if _RESOLVED_DB_PATH_MEMO[0] == raw and _RESOLVED_DB_PATH_MEMO[1]:
        return _RESOLVED_DB_PATH_MEMO[1]
    try:
        resolved = str(Path(raw).resolve())
    except Exception:
        resolved = raw
    _RESOLVED_DB_PATH_MEMO = (raw, resolved)
    return resolved


async def _safe_close_conn(conn: aiosqlite.Connection) -> None:
    try:
        await asyncio.wait_for(conn.close(), timeout=5.0)
    except BaseException:
        try:
            conn.stop()
        except Exception:
            pass


async def _open_pooled_conn(path: str) -> aiosqlite.Connection:
    conn = aiosqlite.connect(path, timeout=15.0, factory=_HubTrackedSqliteConnection)
    try:
        # Поток пула не должен блокировать завершение интерпретатора.
        conn._thread.daemon = True  # type: ignore[attr-defined]
    except Exception:
        pass
    await conn
    try:
        for pragma in _DB_CONN_PRAGMAS:
            try:
                await conn.execute(pragma)
            except sqlite3.OperationalError:
                if "journal_mode" not in pragma:
                    raise
        await conn._execute(conn._conn._hub_reset_cursors)  # type: ignore[attr-defined]
    except BaseException:
        await _safe_close_conn(conn)
        raise
    return conn


async def _prepare_conn_for_reuse(conn: aiosqlite.Connection) -> bool:
    try:
        raw = conn._connection  # type: ignore[attr-defined]
        if raw is None:
            return False
        reset = getattr(raw, "_hub_reset_cursors", None)
        if reset is not None:
            await conn._execute(reset)  # type: ignore[attr-defined]
        if conn.in_transaction:
            await conn.rollback()
        conn.row_factory = None
        return True
    except Exception:
        return False


def _reset_pool_state_locked(path: str, loop: Optional[asyncio.AbstractEventLoop]) -> list[aiosqlite.Connection]:
    """Синхронно (без await) переключает пул; возвращает idle-коннекты к закрытию."""
    global _active_pool_path, _pool_loop, _pool_sem, _pool_generation
    stale = list(_idle_conns)
    _idle_conns.clear()
    _all_conns.clear()
    _pool_generation += 1
    _active_pool_path = path
    _pool_loop = loop
    _pool_sem = asyncio.Semaphore(_DB_POOL_MAX_SIZE) if loop is not None else None
    return stale


async def _release_conn(conn: aiosqlite.Connection, pooled: bool) -> None:
    gen = _conn_generation.get(conn, -1)
    keep = (
        pooled
        and gen == _pool_generation
        and conn in _all_conns
        and len(_idle_conns) < _DB_POOL_MAX_SIZE
    )
    try:
        if keep and await _prepare_conn_for_reuse(conn):
            if gen == _pool_generation and conn in _all_conns:
                _idle_conns.append(conn)
                return
        _all_conns.discard(conn)
        _overflow_conns.discard(conn)
        await _safe_close_conn(conn)
    except BaseException:
        _all_conns.discard(conn)
        _overflow_conns.discard(conn)
        try:
            conn.stop()
        except Exception:
            pass
        raise


@asynccontextmanager
async def connect_db():
    """Аренда тёплого соединения SQLite из пула (drop-in замена `aiosqlite.connect`)."""
    path = _resolved_db_path()
    loop = asyncio.get_running_loop()
    if path != _active_pool_path or _pool_loop is not loop or _pool_sem is None:
        stale = _reset_pool_state_locked(path, loop)
        for old in stale:
            await _safe_close_conn(old)
    sem = _pool_sem
    assert sem is not None
    acquired = False
    if _DB_CONN_DEPTH.get() <= 0:
        try:
            await asyncio.wait_for(sem.acquire(), timeout=_DB_POOL_ACQUIRE_TIMEOUT_SEC)
            acquired = True
        except asyncio.TimeoutError:
            acquired = False
    # Вложенная аренда (функция БД внутри блока другой функции БД) или исчерпанный
    # пул -> временное overflow-соединение: никаких дедлоков на Semaphore.
    conn: Optional[aiosqlite.Connection] = None
    try:
        if acquired:
            while _idle_conns:
                cand = _idle_conns.pop()
                if getattr(cand, "_connection", None) is not None and cand in _all_conns:
                    conn = cand
                    break
                _all_conns.discard(cand)
            if conn is None:
                gen = _pool_generation
                cand = await _open_pooled_conn(path)
                if _pool_generation != gen:
                    await _safe_close_conn(cand)
                    path = _resolved_db_path()
                    gen = _pool_generation
                    cand = await _open_pooled_conn(path)
                conn = cand
                _conn_generation[conn] = gen
                if _pool_generation == gen:
                    _all_conns.add(conn)
        else:
            conn = await _open_pooled_conn(path)
            _conn_generation[conn] = -1
            _overflow_conns.add(conn)
        conn.row_factory = None
        token = _DB_CONN_DEPTH.set(_DB_CONN_DEPTH.get() + 1)
        try:
            yield conn
        except BaseException:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                _DB_CONN_DEPTH.reset(token)
            except Exception:
                pass
    finally:
        try:
            if conn is not None:
                await _release_conn(conn, pooled=acquired)
        finally:
            if acquired:
                sem.release()


def _clear_db_memory_caches() -> None:
    inv_users = globals().get("invalidate_user_cache")
    if callable(inv_users):
        inv_users(None)
    for name in ("_CHAT_LIST_CACHE", "_USER_BY_ID_CACHE"):
        cache = globals().get(name)
        if isinstance(cache, dict):
            cache.clear()
    reset_ledger = globals().get("_reset_usage_ledger_cache")
    if callable(reset_ledger):
        reset_ledger()


async def close_db_pool() -> None:
    """Закрывает все соединения пула и очищает in-memory кэши БД (lifespan / автотесты)."""
    conns = list(_idle_conns) + list(_all_conns) + list(_overflow_conns)
    _reset_pool_state_locked("", None)
    _overflow_conns.clear()
    _clear_db_memory_caches()
    seen: set[int] = set()
    for c in conns:
        if id(c) in seen:
            continue
        seen.add(id(c))
        await _safe_close_conn(c)


def close_db_pool_sync(timeout: float = 5.0) -> None:
    """Синхронное закрытие пула без Event Loop (tearDown legacy-тестов на Windows)."""
    conns = list(_idle_conns) + list(_all_conns) + list(_overflow_conns)
    _reset_pool_state_locked("", None)
    _overflow_conns.clear()
    _clear_db_memory_caches()
    seen: set[int] = set()
    for c in conns:
        if id(c) in seen:
            continue
        seen.add(id(c))
        try:
            c.stop()
        except Exception:
            pass
        th = getattr(c, "_thread", None)
        if th is not None and th.is_alive():
            th.join(timeout)


async def _ensure_column(
    db: aiosqlite.Connection,
    table: str,
    col_name: str,
    col_def: str,
) -> None:
    cur = await db.execute(f"PRAGMA table_info({table});")
    existing = {str(row[1]) for row in await cur.fetchall()}
    if col_name not in existing:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def};")


async def init_db() -> None:
    async with connect_db() as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                is_active INTEGER NOT NULL DEFAULT 1,
                workspace_dir TEXT NOT NULL DEFAULT '',
                max_access_mode TEXT NOT NULL DEFAULT 'write_files',
                sandbox_network TEXT NOT NULL DEFAULT 'internet',
                allowed_models_json TEXT NOT NULL DEFAULT '[]',
                projects_order_json TEXT NOT NULL DEFAULT '[]',
                advisor_prefs_json TEXT NOT NULL DEFAULT '{}',
                notification_prefs_json TEXT NOT NULL DEFAULT '{}',
                daily_token_limit INTEGER NOT NULL DEFAULT 0,
                quota_reserve_pct INTEGER NOT NULL DEFAULT 15,
                custom_rules TEXT NOT NULL DEFAULT '',
                use_desktop_globals INTEGER NOT NULL DEFAULT 0,
                session_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS shared_projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                path TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                execution_mode TEXT NOT NULL DEFAULT 'docker',
                member_ids_json TEXT NOT NULL DEFAULT '[]',
                created_by TEXT NOT NULL DEFAULT 'admin',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT 'admin',
                kind TEXT NOT NULL DEFAULT 'chat',
                project_name TEXT NOT NULL DEFAULT '',
                project_path TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT 'Новый диалог',
                model TEXT NOT NULL DEFAULT 'gemini-4-carbon-high',
                access_mode TEXT NOT NULL DEFAULT 'read_only',
                pinned INTEGER NOT NULL DEFAULT 0,
                icon TEXT NOT NULL DEFAULT '',
                folder TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                agy_conversation_id TEXT NOT NULL DEFAULT '',
                agy_account_id TEXT NOT NULL DEFAULT '',
                codex_thread_id TEXT NOT NULL DEFAULT '',
                codex_account_id TEXT NOT NULL DEFAULT '',
                claude_session_id TEXT NOT NULL DEFAULT '',
                claude_account_id TEXT NOT NULL DEFAULT '',
                preferred_agy_account_id TEXT NOT NULL DEFAULT '',
                preferred_codex_account_id TEXT NOT NULL DEFAULT '',
                preferred_claude_account_id TEXT NOT NULL DEFAULT '',
                last_engine TEXT NOT NULL DEFAULT 'agy',
                source_desktop_id TEXT NOT NULL DEFAULT '',
                compacted_summary TEXT NOT NULL DEFAULT '',
                compacted_up_to_msg_id INTEGER NOT NULL DEFAULT 0,
                compacted_count INTEGER NOT NULL DEFAULT 0,
                is_archived INTEGER NOT NULL DEFAULT 0,
                archived_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_chats_kind_updated
                ON chats(kind, pinned DESC, updated_at DESC);

            CREATE INDEX IF NOT EXISTS idx_chats_project
                ON chats(project_name, pinned DESC, updated_at DESC);

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL DEFAULT 'admin',
                role TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                thinking TEXT NOT NULL DEFAULT '',
                tool_calls_json TEXT NOT NULL DEFAULT '[]',
                attachments_json TEXT NOT NULL DEFAULT '[]',
                model TEXT NOT NULL DEFAULT '',
                usage_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'done',
                bookmarked INTEGER NOT NULL DEFAULT 0,
                custom_title TEXT NOT NULL DEFAULT '',
                auto_title TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_chat_id
                ON messages(chat_id, id ASC);

            CREATE TABLE IF NOT EXISTS account_profiles (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL DEFAULT '',
                plan_note TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 0,
                proxy_mode TEXT NOT NULL DEFAULT 'global',
                proxy_spec_encrypted TEXT NOT NULL DEFAULT '',
                proxy_label TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS project_memories (
                scope_key TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT 'admin',
                project_name TEXT NOT NULL,
                passport_overview TEXT NOT NULL DEFAULT '',
                passport_rules TEXT NOT NULL DEFAULT '',
                passport_file_map TEXT NOT NULL DEFAULT '',
                changelog_json TEXT NOT NULL DEFAULT '[]',
                extra_dirs_json TEXT NOT NULL DEFAULT '[]',
                migrated_from_paths_json TEXT NOT NULL DEFAULT '[]',
                disable_default_guardrails INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_project_memories_owner_proj
                ON project_memories(owner_id, project_name);

            CREATE TABLE IF NOT EXISTS user_openrouter_configs (
                user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                api_key_encrypted TEXT NOT NULL DEFAULT '',
                key_mask TEXT NOT NULL DEFAULT '',
                key_nonce TEXT NOT NULL DEFAULT '',
                is_enabled INTEGER NOT NULL DEFAULT 1,
                default_model TEXT NOT NULL DEFAULT '',
                custom_rules TEXT NOT NULL DEFAULT '',
                max_price_per_m REAL NOT NULL DEFAULT 0.0,
                daily_spend_limit_usd REAL NOT NULL DEFAULT 0.0,
                smart_titles_enabled INTEGER NOT NULL DEFAULT 0,
                smart_titles_model TEXT NOT NULL DEFAULT 'openai/gpt-4.1-mini',
                cached_status_json TEXT NOT NULL DEFAULT '{}',
                cached_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS openrouter_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                chat_id TEXT NOT NULL DEFAULT '',
                chat_title TEXT NOT NULL DEFAULT '',
                project_name TEXT NOT NULL DEFAULT '',
                requested_model TEXT NOT NULL DEFAULT '',
                resolved_model TEXT NOT NULL DEFAULT '',
                provider_name TEXT NOT NULL DEFAULT '',
                generation_id TEXT NOT NULL DEFAULT '',
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ok',
                finish_reason TEXT NOT NULL DEFAULT '',
                prompt_preview TEXT NOT NULL DEFAULT '',
                response_preview TEXT NOT NULL DEFAULT '',
                error_text TEXT NOT NULL DEFAULT '',
                purpose TEXT NOT NULL DEFAULT 'mcp_chat',
                items_count INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_openrouter_logs_user_id_desc
                ON openrouter_logs(user_id, id DESC);

            CREATE INDEX IF NOT EXISTS idx_openrouter_logs_user_created_desc
                ON openrouter_logs(user_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS user_chatgpt_accounts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                chatgpt_account_id TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL DEFAULT '',
                plan_type TEXT NOT NULL DEFAULT '',
                auth_json_encrypted TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 0,
                is_enabled INTEGER NOT NULL DEFAULT 1,
                auto_failover INTEGER NOT NULL DEFAULT 1,
                cached_quotas_json TEXT NOT NULL DEFAULT '{}',
                cached_models_json TEXT NOT NULL DEFAULT '[]',
                cached_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                proxy_mode TEXT NOT NULL DEFAULT 'global',
                proxy_spec_encrypted TEXT NOT NULL DEFAULT '',
                proxy_label TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_chatgpt_acc_user
                ON user_chatgpt_accounts(user_id, is_active DESC, sort_order ASC);

            CREATE TABLE IF NOT EXISTS user_claude_accounts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                claude_account_id TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL DEFAULT '',
                plan_type TEXT NOT NULL DEFAULT '',
                auth_method TEXT NOT NULL DEFAULT 'oauth',
                credentials_json_encrypted TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 0,
                is_enabled INTEGER NOT NULL DEFAULT 1,
                auto_failover INTEGER NOT NULL DEFAULT 1,
                cached_quotas_json TEXT NOT NULL DEFAULT '{}',
                cached_models_json TEXT NOT NULL DEFAULT '[]',
                cached_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                proxy_mode TEXT NOT NULL DEFAULT 'global',
                proxy_spec_encrypted TEXT NOT NULL DEFAULT '',
                proxy_label TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_user_claude_accounts_user
                ON user_claude_accounts(user_id, is_active);

            CREATE TABLE IF NOT EXISTS hub_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                device_label TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_error TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user
                ON push_subscriptions(user_id, last_seen_at DESC);

            CREATE TABLE IF NOT EXISTS gdrive_configs (
                user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                oauth_client_encrypted TEXT NOT NULL DEFAULT '',
                tokens_encrypted TEXT NOT NULL DEFAULT '',
                account_email TEXT NOT NULL DEFAULT '',
                root_folder_id TEXT NOT NULL DEFAULT '',
                root_folder_name TEXT NOT NULL DEFAULT 'ZeroHub',
                is_enabled INTEGER NOT NULL DEFAULT 0,
                sync_self_hub INTEGER NOT NULL DEFAULT 1,
                excluded_projects_json TEXT NOT NULL DEFAULT '[]',
                include_env_vault INTEGER NOT NULL DEFAULT 0,
                vault_passphrase_encrypted TEXT NOT NULL DEFAULT '',
                last_sync_at TEXT NOT NULL DEFAULT '',
                last_sync_status TEXT NOT NULL DEFAULT '',
                last_sync_error TEXT NOT NULL DEFAULT '',
                last_sync_summary_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS gdrive_sync_items (
                user_id TEXT NOT NULL,
                logical_path TEXT NOT NULL,
                item_kind TEXT NOT NULL DEFAULT '',
                sha256 TEXT NOT NULL DEFAULT '',
                size_bytes INTEGER NOT NULL DEFAULT 0,
                drive_file_id TEXT NOT NULL DEFAULT '',
                drive_parent_id TEXT NOT NULL DEFAULT '',
                synced_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (user_id, logical_path)
            );

            CREATE INDEX IF NOT EXISTS idx_gdrive_sync_items_user_kind
                ON gdrive_sync_items(user_id, item_kind);

            CREATE TABLE IF NOT EXISTS model_advisor_catalog (
                series_key TEXT PRIMARY KEY,
                vendor TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'cohort',
                passport_json TEXT NOT NULL DEFAULT '{}',
                calibrated_at TEXT NOT NULL DEFAULT '',
                retry_after TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            """
        )

        # Safe idempotent migrations for existing hub.db installations
        cur = await db.execute("PRAGMA table_info(chats);")
        existing_cols = {row[1] for row in await cur.fetchall()}
        if "icon" not in existing_cols:
            await db.execute("ALTER TABLE chats ADD COLUMN icon TEXT NOT NULL DEFAULT '';")
        if "folder" not in existing_cols:
            await db.execute("ALTER TABLE chats ADD COLUMN folder TEXT NOT NULL DEFAULT '';")
        if "sort_order" not in existing_cols:
            await db.execute("ALTER TABLE chats ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0;")
        if "owner_id" not in existing_cols:
            await db.execute("ALTER TABLE chats ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'admin';")
        if "compacted_summary" not in existing_cols:
            await db.execute("ALTER TABLE chats ADD COLUMN compacted_summary TEXT NOT NULL DEFAULT '';")
        if "compacted_up_to_msg_id" not in existing_cols:
            await db.execute("ALTER TABLE chats ADD COLUMN compacted_up_to_msg_id INTEGER NOT NULL DEFAULT 0;")
        if "compacted_count" not in existing_cols:
            await db.execute("ALTER TABLE chats ADD COLUMN compacted_count INTEGER NOT NULL DEFAULT 0;")
        if "codex_thread_id" not in existing_cols:
            await db.execute("ALTER TABLE chats ADD COLUMN codex_thread_id TEXT NOT NULL DEFAULT '';")
        if "codex_account_id" not in existing_cols:
            await db.execute("ALTER TABLE chats ADD COLUMN codex_account_id TEXT NOT NULL DEFAULT '';")
        if "last_engine" not in existing_cols:
            await db.execute("ALTER TABLE chats ADD COLUMN last_engine TEXT NOT NULL DEFAULT 'agy';")
        if "claude_session_id" not in existing_cols:
            await db.execute("ALTER TABLE chats ADD COLUMN claude_session_id TEXT NOT NULL DEFAULT '';")
        if "claude_account_id" not in existing_cols:
            await db.execute("ALTER TABLE chats ADD COLUMN claude_account_id TEXT NOT NULL DEFAULT '';")
        if "is_archived" not in existing_cols:
            await db.execute("ALTER TABLE chats ADD COLUMN is_archived INTEGER NOT NULL DEFAULT 0;")
        if "archived_at" not in existing_cols:
            await db.execute("ALTER TABLE chats ADD COLUMN archived_at TEXT NOT NULL DEFAULT '';")
        if "source_desktop_id" not in existing_cols:
            await db.execute("ALTER TABLE chats ADD COLUMN source_desktop_id TEXT NOT NULL DEFAULT '';")

        cur_m = await db.execute("PRAGMA table_info(messages);")
        msg_cols = {row[1] for row in await cur_m.fetchall()}
        if "user_id" not in msg_cols:
            await db.execute("ALTER TABLE messages ADD COLUMN user_id TEXT NOT NULL DEFAULT 'admin';")
        if "bookmarked" not in msg_cols:
            await db.execute("ALTER TABLE messages ADD COLUMN bookmarked INTEGER NOT NULL DEFAULT 0;")
        if "custom_title" not in msg_cols:
            await db.execute("ALTER TABLE messages ADD COLUMN custom_title TEXT NOT NULL DEFAULT '';")
        await _ensure_column(db, "messages", "auto_title", "TEXT NOT NULL DEFAULT ''")

        await _ensure_column(
            db,
            "user_openrouter_configs",
            "smart_titles_enabled",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await _ensure_column(
            db,
            "user_openrouter_configs",
            "smart_titles_model",
            "TEXT NOT NULL DEFAULT 'openai/gpt-4.1-mini'",
        )

        await _ensure_column(
            db,
            "openrouter_logs",
            "purpose",
            "TEXT NOT NULL DEFAULT 'mcp_chat'",
        )
        await _ensure_column(
            db,
            "openrouter_logs",
            "items_count",
            "INTEGER NOT NULL DEFAULT 1",
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_or_logs_user_purpose ON openrouter_logs(user_id, purpose);"
        )
        # v110: индексы горячих путей (создаются после миграций колонок owner_id/sort_order/bookmarked)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chats_owner_kind_updated "
            "ON chats(owner_id, kind, pinned DESC, sort_order ASC, updated_at DESC);"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_assistant_usage "
            "ON messages(created_at, user_id) "
            "WHERE role = 'assistant' AND usage_json != '' AND usage_json != '{}';"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_bookmarked ON messages(chat_id) WHERE bookmarked = 1;"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chats_kind_proj_nocase ON chats(kind, LOWER(project_name));"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chats_source_desktop "
            "ON chats(source_desktop_id) WHERE source_desktop_id != '';"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chats_agy_conv "
            "ON chats(agy_conversation_id) WHERE agy_conversation_id != '';"
        )

        cur_u_cols = await db.execute("PRAGMA table_info(users);")
        user_cols = {row[1] for row in await cur_u_cols.fetchall()}
        if "custom_rules" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN custom_rules TEXT NOT NULL DEFAULT '';")
        if "use_desktop_globals" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN use_desktop_globals INTEGER NOT NULL DEFAULT 0;")
        if "sandbox_network" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN sandbox_network TEXT NOT NULL DEFAULT 'internet';")
        if "projects_order_json" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN projects_order_json TEXT NOT NULL DEFAULT '[]';")
        await _ensure_notification_prefs_column(db, user_cols)
        if "advisor_prefs_json" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN advisor_prefs_json TEXT NOT NULL DEFAULT '{}';")
        await db.execute(
            "UPDATE users SET use_desktop_globals = 0 WHERE use_desktop_globals != 0;"
        )

        cur_pm_cols = await db.execute("PRAGMA table_info(project_memories);")
        pm_cols = {row[1] for row in await cur_pm_cols.fetchall()}
        if "extra_dirs_json" not in pm_cols:
            await db.execute("ALTER TABLE project_memories ADD COLUMN extra_dirs_json TEXT NOT NULL DEFAULT '[]';")
        if "migrated_from_paths_json" not in pm_cols:
            await db.execute("ALTER TABLE project_memories ADD COLUMN migrated_from_paths_json TEXT NOT NULL DEFAULT '[]';")
        if "disable_default_guardrails" not in pm_cols:
            await db.execute("ALTER TABLE project_memories ADD COLUMN disable_default_guardrails INTEGER NOT NULL DEFAULT 0;")

        # Release v106: Per-account & global proxy/Xray routing columns + hub_meta seed
        for column, definition in {
            "credentials_json_encrypted": "TEXT NOT NULL DEFAULT ''",
            "auth_method": "TEXT NOT NULL DEFAULT ''", "is_enabled": "INTEGER NOT NULL DEFAULT 1",
            "auto_failover": "INTEGER NOT NULL DEFAULT 1", "cached_quotas_json": "TEXT NOT NULL DEFAULT '{}'",
            "cached_models_json": "TEXT NOT NULL DEFAULT '[]'", "cached_at": "TEXT NOT NULL DEFAULT ''",
            "last_error": "TEXT NOT NULL DEFAULT ''", "sort_order": "INTEGER NOT NULL DEFAULT 0",
            "created_at": "TEXT NOT NULL DEFAULT ''",
        }.items():
            await _ensure_column(db, "account_profiles", column, definition)
        await _ensure_column(db, "chats", "agy_account_id", "TEXT NOT NULL DEFAULT ''")
        await _ensure_column(db, "chats", "preferred_agy_account_id", "TEXT NOT NULL DEFAULT ''")
        await _ensure_column(db, "chats", "preferred_codex_account_id", "TEXT NOT NULL DEFAULT ''")
        await _ensure_column(db, "chats", "preferred_claude_account_id", "TEXT NOT NULL DEFAULT ''")
        for _tbl in ("account_profiles", "user_chatgpt_accounts", "user_claude_accounts"):
            await _ensure_column(db, _tbl, "proxy_mode", "TEXT NOT NULL DEFAULT 'global'")
            await _ensure_column(db, _tbl, "proxy_spec_encrypted", "TEXT NOT NULL DEFAULT ''")
            await _ensure_column(db, _tbl, "proxy_label", "TEXT NOT NULL DEFAULT ''")

        cur_gp = await db.execute("SELECT value FROM hub_meta WHERE key = 'global_proxy_config' LIMIT 1;")
        if not await cur_gp.fetchone():
            env_hp = os.getenv("HUB_PROXY", "auto").strip()
            env_hp_low = env_hp.lower()
            if not env_hp or env_hp_low == "auto":
                seed_gp = {"mode": "system_auto", "spec_encrypted": "", "label": ""}
            elif env_hp_low in ("none", "off", "false", "direct"):
                seed_gp = {"mode": "direct", "spec_encrypted": "", "label": ""}
            else:
                norm_seed = env_hp if "://" in env_hp else f"http://{env_hp}"
                seed_gp = {
                    "mode": "custom",
                    "spec_encrypted": encrypt_scoped_proxy_spec("_global", norm_seed),
                    "label": _safe_label_for_proxy_spec(norm_seed),
                }
            await db.execute(
                "INSERT INTO hub_meta (key, value, updated_at) VALUES ('global_proxy_config', ?, ?);",
                (json.dumps(seed_gp, ensure_ascii=False), utc_now_iso()),
            )

        # Idempotent v153 migration: rename legacy default Google Drive root folder to ZeroHub once
        cur_gd_ren = await db.execute(
            "SELECT value FROM hub_meta WHERE key = 'gdrive_root_renamed_zerohub_v153' LIMIT 1;"
        )
        if not await cur_gd_ren.fetchone():
            now_gd_mig = utc_now_iso()
            await db.execute(
                """
                UPDATE gdrive_configs
                SET root_folder_name = ?, updated_at = ?
                WHERE TRIM(COALESCE(root_folder_name, '')) IN ('', 'Antigravity_Hub');
                """,
                (DEFAULT_GDRIVE_ROOT_FOLDER_NAME, now_gd_mig),
            )
            await db.execute(
                "INSERT INTO hub_meta (key, value, updated_at) VALUES ('gdrive_root_renamed_zerohub_v153', '1', ?);",
                (now_gd_mig,),
            )

        # Seed built-in Administrator account using current HUB_PIN if not created yet
        cur_u = await db.execute("SELECT id FROM users WHERE id = 'admin' OR LOWER(username) = 'admin' LIMIT 1;")
        if not await cur_u.fetchone():
            now = utc_now_iso()
            initial_pin = get_pin() or "1234"
            admin_space = _ensure_user_space("admin")
            init_admin_ws = (
                str(_eff_projects_root())
                if PROJECTS_ROOT != config._BOOT_PROJECTS_ROOT
                else str(admin_space["projects"])
            )
            await db.execute(
                """
                INSERT INTO users (
                    id, username, display_name, password_hash, role, is_active,
                    workspace_dir, max_access_mode, allowed_models_json,
                    daily_token_limit, quota_reserve_pct, custom_rules,
                    use_desktop_globals, session_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'admin', 1, ?, 'host_full', '[]', 0, 0, '', 0, 1, ?, ?)
                """,
                (
                    "admin",
                    "admin",
                    "Администратор",
                    hash_secret(initial_pin),
                    init_admin_ws,
                    now,
                    now,
                ),
            )

        effective_base = _eff_base_dir()
        await relocate_hub_paths_if_base_moved(db, effective_base)
        await _migrate_v96_to_v97_layout(db, effective_base)
        await db.commit()
        _clear_db_memory_caches()

        # Sync isolated CLI homes, GEMINI.md, users/<slug>/context/RULES.md, and project context mirrors
        db.row_factory = aiosqlite.Row
        cur_sync = await db.execute("SELECT id, username, role, custom_rules FROM users;")
        user_rule_rows = await cur_sync.fetchall()
        cur_pm_all = await db.execute("SELECT * FROM project_memories;")
        pm_all_rows = await cur_pm_all.fetchall()

    user_slug_map: dict[str, str] = {"admin": "admin"}
    try:
        ensure_user_cli_home("_system")
        for ur in user_rule_rows:
            uid_s = str(ur["id"])
            uname_s = str(ur["username"] or "")
            urole_s = str(ur["role"] or "user")
            user_slug_map[uid_s] = config.sanitize_user_folder_slug(
                {"id": uid_s, "username": uname_s, "role": urole_s}
            )
            sync_user_cli_rules(uid_s, str(ur["custom_rules"] or ""), username=uname_s)
            if USERS_DIR != config._BOOT_USERS_DIR or USER_WORKSPACES_DIR != config._BOOT_USER_WORKSPACES_DIR:
                space = _ensure_user_space(user_slug_map[uid_s])
                clean_r = str(ur["custom_rules"] or "").replace("\r\n", "\n").replace("\r", "\n").strip()
                mirror_txt = (clean_r + "\n") if clean_r else "# Глобальные правила ИИ\n\n_(Пользовательские правила не заданы.)_\n"
                config._atomic_sync_text_file(space["context"] / "RULES.md", mirror_txt)
        for pm_r in pm_all_rows:
            mem_obj = _row_to_project_memory(
                pm_r,
                scope_key=str(pm_r["scope_key"] or ""),
                project_name=str(pm_r["project_name"] or ""),
                owner_id=str(pm_r["owner_id"] or "admin"),
            )
            _sync_project_context_mirror(mem_obj, user_slug_by_id=user_slug_map)
    except Exception:
        pass


def _row_to_chat(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d["pinned"] = bool(d.get("pinned", 0))
    d["icon"] = d.get("icon") or ""
    d["folder"] = d.get("folder") or ""
    d["sort_order"] = int(d.get("sort_order") or 0)
    d["codex_thread_id"] = d.get("codex_thread_id") or ""
    d["agy_account_id"] = d.get("agy_account_id") or ""
    d["codex_account_id"] = d.get("codex_account_id") or ""
    d["last_engine"] = d.get("last_engine") or "agy"
    d["claude_session_id"] = d.get("claude_session_id") or ""
    d["claude_account_id"] = d.get("claude_account_id") or ""
    d["preferred_agy_account_id"] = d.get("preferred_agy_account_id") or ""
    d["preferred_codex_account_id"] = d.get("preferred_codex_account_id") or ""
    d["preferred_claude_account_id"] = d.get("preferred_claude_account_id") or ""
    d["compacted_summary"] = d.get("compacted_summary") or ""
    d["compacted_up_to_msg_id"] = int(d.get("compacted_up_to_msg_id") or 0)
    d["compacted_count"] = int(d.get("compacted_count") or 0)
    d["is_archived"] = bool(d.get("is_archived") or 0)
    d["archived_at"] = str(d.get("archived_at") or "")
    if "bookmark_count" in d:
        d["bookmark_count"] = int(d.get("bookmark_count") or 0)
    return d


_TOOL_BODY_KEYS = {"content", "target", "replacement", "replacementchunks", "old_string", "new_string", "patch", "script", "output"}
_BRAIN_REF_RE = re.compile(r"(?:brain[/\\]|conversation://)([0-9a-fA-F]{8}-[0-9a-fA-F-]{27,36})")
_WRITE_TOOL_NAMES_FOR_PATHS = frozenset(
    {
        "apply_patch",
        "write_file",
        "edit_file",
        "multi_edit",
        "filechange",
        "write",
        "edit",
        "multiedit",
    }
)
_PATCH_FILE_HDR_RE = re.compile(r"^\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(.+?)\s*$")
_PATCH_MOVE_TO_RE = re.compile(r"^\*\*\*\s+Move to:\s*(.+?)\s*$")


def _extract_raw_write_paths_and_renames(
    raw_params: Any,
) -> tuple[list[str], list[dict[str, str]]]:
    paths: list[str] = []
    seen_paths: set[str] = set()
    renames: list[dict[str, str]] = []
    seen_renames: set[tuple[str, str]] = set()

    def _add_path(val: Any) -> str:
        if not isinstance(val, str):
            return ""
        clean_p = val.strip()[:260]
        if not clean_p:
            return ""
        if clean_p not in seen_paths and len(paths) < 60:
            seen_paths.add(clean_p)
            paths.append(clean_p)
        return clean_p

    def _add_rename(old_p: Any, new_p: Any) -> None:
        o_c = _add_path(old_p)
        n_c = _add_path(new_p)
        if o_c and n_c and o_c != n_c and (o_c, n_c) not in seen_renames and len(renames) < 60:
            seen_renames.add((o_c, n_c))
            renames.append({"from": o_c, "to": n_c})

    def _scan_patch_str(s: str) -> None:
        cur_src = ""
        for raw_ln in s.splitlines():
            m_hdr = _PATCH_FILE_HDR_RE.match(raw_ln)
            if m_hdr:
                cur_src = _add_path(m_hdr.group(1))
                continue
            m_mv = _PATCH_MOVE_TO_RE.match(raw_ln)
            if m_mv:
                mv_dst = m_mv.group(1)
                if cur_src:
                    _add_rename(cur_src, mv_dst)
                else:
                    _add_path(mv_dst)

    def _walk(obj: Any, depth: int = 0) -> None:
        if depth > 12 or obj is None:
            return
        if isinstance(obj, str):
            if "*** " in obj:
                _scan_patch_str(obj)
            if obj.lstrip().startswith(("{", "[")):
                try:
                    _walk(json.loads(obj), depth + 1)
                except Exception:
                    pass
        elif isinstance(obj, dict):
            if obj.get("from") and obj.get("to") and isinstance(obj.get("from"), str) and isinstance(obj.get("to"), str):
                _add_rename(obj["from"], obj["to"])
            if obj.get("path") and obj.get("move_path") and isinstance(obj.get("path"), str) and isinstance(obj.get("move_path"), str):
                _add_rename(obj["path"], obj["move_path"])
            for k, v in obj.items():
                if k in ("Path", "TargetFile", "file_path", "path", "notebook_path") and isinstance(v, str):
                    _add_path(v)
                elif k == "paths" and isinstance(v, list):
                    for it in v:
                        _add_path(it)
                elif k == "renames" and isinstance(v, list):
                    for r_it in v:
                        if isinstance(r_it, dict):
                            _add_rename(r_it.get("from"), r_it.get("to"))
                elif isinstance(v, (dict, list, str)):
                    _walk(v, depth + 1)
        elif isinstance(obj, list):
            for it in obj:
                _walk(it, depth + 1)

    _walk(raw_params)
    return paths, renames


def compact_tool_calls_payload(tool_calls: Any) -> list[dict[str, Any]]:
    """Copy tool metadata without retaining file bodies or command output, while preserving touched paths & renames."""
    def clean(value: Any, depth: int = 0) -> Any:
        if depth > 16:
            return None
        if isinstance(value, dict):
            return {k: clean(v, depth + 1) for k, v in value.items()
                    if str(k).lower() not in _TOOL_BODY_KEYS}
        if isinstance(value, list):
            return [clean(v, depth + 1) for v in value]
        return value[:500] if isinstance(value, str) else value

    result = []
    for tc in tool_calls if isinstance(tool_calls, list) else []:
        if not isinstance(tc, dict):
            continue
        tname = str(tc.get("name") or "").strip()
        tname_low = tname.lower()
        raw_params_src = tc.get("parameters") or tc.get("input") or tc.get("args") or {}
        is_lazy_hub_write = (
            tname_low == "call_mcp_tool"
            and isinstance(raw_params_src, dict)
            and str(raw_params_src.get("ServerName") or raw_params_src.get("server_name") or "").lower() == "hub_files"
            and str(raw_params_src.get("ToolName") or raw_params_src.get("tool_name") or "").lower() in ("write_file", "edit_file")
        )
        extracted_paths: list[str] = []
        extracted_renames: list[dict[str, str]] = []
        if (
            tname_low in _WRITE_TOOL_NAMES_FOR_PATHS
            or is_lazy_hub_write
            or ("hub_files" in tname_low and ("write" in tname_low or "edit" in tname_low))
        ):
            extracted_paths, extracted_renames = _extract_raw_write_paths_and_renames(raw_params_src)

        item = clean(tc)
        if extracted_paths or extracted_renames:
            params_out = item.get("parameters")
            if not isinstance(params_out, dict):
                params_out = {}
                item["parameters"] = params_out
            if extracted_paths:
                params_out["paths"] = extracted_paths[:60]
            if extracted_renames:
                params_out["renames"] = extracted_renames[:60]

        try:
            item["output_len"] = max(0, int(tc.get("output_len") or len(str(tc.get("output") or ""))))
        except (ValueError, TypeError, OverflowError):
            item["output_len"] = len(str(tc.get("output") or ""))
        refs = set(_BRAIN_REF_RE.findall(str(tc.get("output") or "")))
        existing = tc.get("brain_refs")
        if isinstance(existing, list):
            refs.update(str(r) for r in existing if re.fullmatch(r"[0-9a-fA-F-]{8,64}", str(r)))
        if refs:
            item["brain_refs"] = sorted(refs)
        result.append(item)
    return result


def _row_to_message(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    for json_col, out_key, default in (
        ("tool_calls_json", "tool_calls", []),
        ("attachments_json", "attachments", []),
        ("usage_json", "usage", {}),
    ):
        raw = d.pop(json_col, None)
        try:
            d[out_key] = json.loads(raw) if raw else default
        except Exception:
            d[out_key] = default
    d["tool_calls"] = compact_tool_calls_payload(d.get("tool_calls"))
    if "has_thinking" in d:
        d["has_thinking"] = bool(d["has_thinking"])
    else:
        d["has_thinking"] = bool(str(d.get("thinking") or "").strip())
    d["bookmarked"] = bool(d.get("bookmarked", 0))
    d["custom_title"] = str(d.get("custom_title") or "")
    d["auto_title"] = str(d.get("auto_title") or "")
    return d


# ---------------------------------------------------------------------------
# Users & Shared Projects (Multi-User RBAC + Sandboxed Workspaces)
# ---------------------------------------------------------------------------

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.\-]{2,32}$")
_PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9а-яА-ЯёЁ_.\- ]{2,64}$")
VALID_USER_MAX_MODES = {"read_only", "write_files", "docker_full", "host_full"}
VALID_SANDBOX_NETWORK_MODES = {"internet", "none"}
VALID_SHARED_EXEC_MODES = {"docker", "sandbox_files", "host"}
VALID_ADVISOR_STRATEGIES = {"balanced", "max_quality"}
DEFAULT_ADVISOR_PREFS: dict[str, Any] = {
    "enabled": True,
    "strategy": "balanced",
    "auto_pick_new_tab": True,
    "composer_hints": True,
}


def _coerce_advisor_bool(val: Any, default: bool) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        low = val.strip().lower()
        if low in {"1", "true", "yes", "on"}:
            return True
        if low in {"0", "false", "no", "off"}:
            return False
    return default


def normalize_advisor_prefs(raw: Any) -> dict[str, Any]:
    parsed: Any = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except Exception:
            parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    raw_strategy = str(parsed.get("strategy") or "").strip().lower()
    strategy = raw_strategy if raw_strategy in VALID_ADVISOR_STRATEGIES else "balanced"
    return {
        "enabled": _coerce_advisor_bool(
            parsed.get("enabled"), DEFAULT_ADVISOR_PREFS["enabled"]
        ),
        "strategy": strategy,
        "auto_pick_new_tab": _coerce_advisor_bool(
            parsed.get("auto_pick_new_tab"), DEFAULT_ADVISOR_PREFS["auto_pick_new_tab"]
        ),
        "composer_hints": _coerce_advisor_bool(
            parsed.get("composer_hints"), DEFAULT_ADVISOR_PREFS["composer_hints"]
        ),
    }


def _normalize_sandbox_network(val: Any) -> str:
    clean = str(val or "").strip().lower()
    return clean if clean in VALID_SANDBOX_NETWORK_MODES else "internet"


_NOTIFICATION_PREF_DEFAULTS = {
    "in_app_toast": True,
    "system_push": True,
    "sound": False,
    "on_turn_done": True,
    "on_turn_error": True,
    "on_process_crash": True,
    "only_long_turns": False,
}


def normalize_notification_prefs(raw: Any) -> dict[str, bool]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        key: raw[key] if isinstance(raw.get(key), bool) else default
        for key, default in _NOTIFICATION_PREF_DEFAULTS.items()
    }


def _row_to_user(row: aiosqlite.Row, *, include_secret: bool = False) -> dict[str, Any]:
    d = dict(row)
    d["is_active"] = bool(d.get("is_active", 1))
    d["sandbox_network"] = _normalize_sandbox_network(d.get("sandbox_network"))
    d["daily_token_limit"] = int(d.get("daily_token_limit") or 0)
    d["quota_reserve_pct"] = int(d.get("quota_reserve_pct") or 0)
    d["custom_rules"] = str(d.get("custom_rules") or "")
    d["use_desktop_globals"] = False
    d["session_version"] = int(d.get("session_version") or 1)
    d.pop("projects_order_json", None)
    raw_advisor = d.pop("advisor_prefs_json", "{}")
    d["advisor_prefs"] = normalize_advisor_prefs(raw_advisor)
    d["notification_prefs"] = normalize_notification_prefs(d.pop("notification_prefs_json", "{}"))
    raw_models = d.pop("allowed_models_json", "[]")
    try:
        parsed = json.loads(raw_models) if raw_models else []
        d["allowed_models"] = [str(x) for x in parsed if str(x).strip()] if isinstance(parsed, list) else []
    except Exception:
        d["allowed_models"] = []
    if not include_secret:
        d.pop("password_hash", None)
    return d


_USER_BY_ID_CACHE_TTL_SEC = 15.0
_USER_BY_ID_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_USER_CACHE_EPOCH = 0


def _copy_user_dict(user: dict[str, Any]) -> dict[str, Any]:
    out = dict(user)
    if isinstance(out.get("allowed_models"), list):
        out["allowed_models"] = list(out["allowed_models"])
    for key in ("advisor_prefs", "notification_prefs"):
        if isinstance(out.get(key), dict):
            out[key] = dict(out[key])
    return out


def invalidate_user_cache(user_id: Optional[str] = None) -> None:
    """Сброс in-memory кэша `get_user_by_id` (всего или по одному пользователю)."""
    global _USER_CACHE_EPOCH
    _USER_CACHE_EPOCH += 1
    if user_id is None:
        _USER_BY_ID_CACHE.clear()
        return
    uid = str(user_id)
    for key in [k for k in _USER_BY_ID_CACHE if k[1] == uid]:
        _USER_BY_ID_CACHE.pop(key, None)


async def get_user_by_id(user_id: str) -> Optional[dict[str, Any]]:
    if not user_id:
        return None
    cache_key = (_resolved_db_path(), str(user_id))
    hit = _USER_BY_ID_CACHE.get(cache_key)
    now_mono = time.monotonic()
    if hit is not None and (now_mono - hit[0]) < _USER_BY_ID_CACHE_TTL_SEC:
        return _copy_user_dict(hit[1])
    epoch_before = _USER_CACHE_EPOCH
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE id = ? LIMIT 1", (user_id,))
        row = await cur.fetchone()
        user = _row_to_user(row) if row else None
    if user is None:
        _USER_BY_ID_CACHE.pop(cache_key, None)
        return None
    if epoch_before != _USER_CACHE_EPOCH:
        return user
    if len(_USER_BY_ID_CACHE) > 512:
        _USER_BY_ID_CACHE.clear()
    _USER_BY_ID_CACHE[cache_key] = (now_mono, _copy_user_dict(user))
    return user


async def get_user_by_username(username: str) -> Optional[dict[str, Any]]:
    clean = (username or "").strip()
    if not clean:
        return None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?) LIMIT 1",
            (clean,),
        )
        row = await cur.fetchone()
        return _row_to_user(row) if row else None


async def get_user_auth_by_username(username: str) -> Optional[dict[str, Any]]:
    clean = (username or "").strip()
    if not clean:
        return None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?) LIMIT 1",
            (clean,),
        )
        row = await cur.fetchone()
        return _row_to_user(row, include_secret=True) if row else None


async def get_user_auth_by_id(user_id: str) -> Optional[dict[str, Any]]:
    if not user_id:
        return None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE id = ? LIMIT 1", (user_id,))
        row = await cur.fetchone()
        return _row_to_user(row, include_secret=True) if row else None


async def list_users() -> list[dict[str, Any]]:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT u.*,
                (SELECT COUNT(*) FROM chats c WHERE c.owner_id = u.id) AS chat_count
            FROM users u
            ORDER BY CASE WHEN u.role = 'admin' THEN 0 ELSE 1 END ASC, u.created_at ASC
            """
        )
        rows = await cur.fetchall()
        return [_row_to_user(r) for r in rows]


def _normalize_projects_order_list(project_names: Any, limit: int = 300) -> list[str]:
    if not isinstance(project_names, list):
        return []
    out: list[str] = []
    seen_low: set[str] = set()
    for item in project_names:
        if not isinstance(item, str):
            continue
        clean = item.strip()
        if not clean:
            continue
        low = clean.lower()
        if low in seen_low:
            continue
        seen_low.add(low)
        out.append(clean)
        if len(out) >= limit:
            break
    return out


async def _ensure_projects_order_column(db: aiosqlite.Connection) -> None:
    cur = await db.execute("PRAGMA table_info(users);")
    cols = {row[1] for row in await cur.fetchall()}
    if "projects_order_json" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN projects_order_json TEXT NOT NULL DEFAULT '[]';"
        )
        await db.commit()


async def get_user_projects_order(user_id: str) -> list[str]:
    clean_uid = (user_id or "").strip()
    if not clean_uid:
        return []
    async with connect_db() as db:
        cur = await db.execute(
            "SELECT projects_order_json FROM users WHERE id = ? LIMIT 1",
            (clean_uid,),
        )
        row = await cur.fetchone()
        if not row or not row[0]:
            return []
        try:
            parsed = json.loads(row[0])
        except Exception:
            return []
        return _normalize_projects_order_list(parsed)


async def set_user_projects_order(user_id: str, project_names: list[str]) -> list[str]:
    clean_uid = (user_id or "").strip()
    clean_order = _normalize_projects_order_list(project_names)
    if not clean_uid:
        return clean_order
    now = utc_now_iso()
    async with connect_db() as db:
        await db.execute(
            "UPDATE users SET projects_order_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(clean_order, ensure_ascii=False), now, clean_uid),
        )
        await db.commit()
        invalidate_user_cache(clean_uid)
    return clean_order


async def create_user(
    *,
    username: str,
    display_name: str = "",
    password: str,
    role: str = "user",
    max_access_mode: str = "write_files",
    sandbox_network: str = "internet",
    allowed_models: Optional[list[str]] = None,
    daily_token_limit: int = 0,
    quota_reserve_pct: int = 15,
) -> dict[str, Any]:
    clean_username = (username or "").strip()
    if not _USERNAME_RE.fullmatch(clean_username):
        raise ValueError(
            "Логин должен содержать от 2 до 32 символов (латиница, цифры, точка, дефис или _)"
        )
    if clean_username.lower() in {"admin", "root", "system", "shared"}:
        raise ValueError("Этот системный логин зарезервирован")

    clean_pw = (password or "").strip()
    if len(clean_pw) < 4:
        raise ValueError("Пароль или ПИН-код пользователя должен быть не короче 4 символов")

    clean_role = "user"
    clean_mode = max_access_mode if max_access_mode in VALID_USER_MAX_MODES else "write_files"
    clean_sbx_net = _normalize_sandbox_network(sandbox_network)

    user_space = _ensure_user_space(clean_username)
    ws_dir = user_space["projects"]

    clean_models = [str(m).strip() for m in (allowed_models or []) if str(m).strip()]
    clean_limit = max(0, int(daily_token_limit or 0))
    clean_reserve = max(0, min(95, int(quota_reserve_pct if quota_reserve_pct is not None else 15)))
    clean_display = (display_name or "").strip()[:60] or clean_username

    uid = str(uuid.uuid4())
    now = utc_now_iso()

    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur_dup = await db.execute(
            "SELECT id FROM users WHERE LOWER(username) = LOWER(?) LIMIT 1",
            (clean_username,),
        )
        if await cur_dup.fetchone():
            raise ValueError(f"Пользователь с логином '{clean_username}' уже существует")

        await db.execute(
            """
            INSERT INTO users (
                id, username, display_name, password_hash, role, is_active,
                workspace_dir, max_access_mode, sandbox_network, allowed_models_json,
                daily_token_limit, quota_reserve_pct, session_version,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                uid,
                clean_username,
                clean_display,
                hash_secret(clean_pw),
                clean_role,
                str(ws_dir),
                clean_mode,
                clean_sbx_net,
                json.dumps(clean_models, ensure_ascii=False),
                clean_limit,
                clean_reserve,
                now,
                now,
            ),
        )
        await db.commit()
        invalidate_user_cache(uid)
        cur = await db.execute("SELECT * FROM users WHERE id = ?", (uid,))
        row = await cur.fetchone()
        created_user = _row_to_user(row)
    try:
        sync_user_cli_rules(uid, "", username=clean_username)
        if USERS_DIR != config._BOOT_USERS_DIR or USER_WORKSPACES_DIR != config._BOOT_USER_WORKSPACES_DIR:
            config._atomic_sync_text_file(
                user_space["context"] / "RULES.md",
                "# Глобальные правила ИИ\n\n_(Пользовательские правила не заданы.)_\n",
            )
    except Exception:
        pass
    return created_user


async def update_user(
    user_id: str,
    *,
    display_name: Optional[str] = None,
    password: Optional[str] = None,
    is_active: Optional[bool] = None,
    max_access_mode: Optional[str] = None,
    sandbox_network: Optional[str] = None,
    allowed_models: Optional[list[str]] = None,
    daily_token_limit: Optional[int] = None,
    quota_reserve_pct: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    existing = await get_user_by_id(user_id)
    if not existing:
        return None

    is_builtin_admin = existing["id"] == "admin" or existing["role"] == "admin"
    fields: list[str] = []
    params: list[Any] = []
    bump_session = False

    if display_name is not None:
        fields.append("display_name = ?")
        params.append(display_name.strip()[:60] or existing["username"])

    if password is not None and password.strip():
        clean_pw = password.strip()
        if len(clean_pw) < 4:
            raise ValueError("Пароль или ПИН-код должен быть не короче 4 символов")
        fields.append("password_hash = ?")
        params.append(hash_secret(clean_pw))
        bump_session = True

    if is_active is not None:
        if is_builtin_admin and not is_active:
            raise ValueError("Нельзя заблокировать главный аккаунт администратора")
        fields.append("is_active = ?")
        params.append(1 if is_active else 0)
        if not is_active:
            bump_session = True

    if max_access_mode is not None and not is_builtin_admin:
        clean_mode = max_access_mode if max_access_mode in VALID_USER_MAX_MODES else "write_files"
        fields.append("max_access_mode = ?")
        params.append(clean_mode)

    if sandbox_network is not None:
        clean_sbx_net = _normalize_sandbox_network(sandbox_network)
        fields.append("sandbox_network = ?")
        params.append(clean_sbx_net)

    if allowed_models is not None and not is_builtin_admin:
        clean_models = [str(m).strip() for m in allowed_models if str(m).strip()]
        fields.append("allowed_models_json = ?")
        params.append(json.dumps(clean_models, ensure_ascii=False))

    if daily_token_limit is not None and not is_builtin_admin:
        fields.append("daily_token_limit = ?")
        params.append(max(0, int(daily_token_limit)))

    if quota_reserve_pct is not None and not is_builtin_admin:
        fields.append("quota_reserve_pct = ?")
        params.append(max(0, min(95, int(quota_reserve_pct))))

    if bump_session:
        fields.append("session_version = session_version + 1")

    if not fields:
        return existing

    fields.append("updated_at = ?")
    params.append(utc_now_iso())
    params.append(user_id)

    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            f"UPDATE users SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        await db.commit()
        invalidate_user_cache(user_id)
        cur = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        return _row_to_user(row) if row else None


async def update_user_rules(
    user_id: str,
    *,
    custom_rules: str,
) -> Optional[dict[str, Any]]:
    existing = await get_user_by_id(user_id)
    if not existing:
        return None

    clean_rules = (custom_rules or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    byte_len = len(clean_rules.encode("utf-8"))
    if byte_len > MAX_CUSTOM_RULES_BYTES:
        raise ValueError(
            f"Текст правил превышает лимит 24 КБ ({byte_len:,} из {MAX_CUSTOM_RULES_BYTES:,} байт)".replace(
                ",", " "
            )
        )

    now = utc_now_iso()
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """
            UPDATE users
            SET custom_rules = ?, use_desktop_globals = 0, updated_at = ?
            WHERE id = ?
            """,
            (clean_rules, now, user_id),
        )
        await db.commit()
        invalidate_user_cache(user_id)
        cur = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        updated = _row_to_user(row) if row else None

    uname_for_sync = str(existing.get("username") or "")
    sync_user_cli_rules(user_id, clean_rules, username=uname_for_sync)
    if USERS_DIR != config._BOOT_USERS_DIR or USER_WORKSPACES_DIR != config._BOOT_USER_WORKSPACES_DIR:
        try:
            u_space = _ensure_user_space(existing)
            mirror_txt = (clean_rules + "\n") if clean_rules else "# Глобальные правила ИИ\n\n_(Пользовательские правила не заданы.)_\n"
            config._atomic_sync_text_file(u_space["context"] / "RULES.md", mirror_txt)
        except Exception:
            pass
    return updated


async def _ensure_advisor_prefs_column(db: aiosqlite.Connection) -> None:
    cur = await db.execute("PRAGMA table_info(users);")
    cols = {row[1] for row in await cur.fetchall()}
    if "advisor_prefs_json" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN advisor_prefs_json TEXT NOT NULL DEFAULT '{}';"
        )
        await db.commit()


async def update_user_advisor_prefs(
    user_id: str,
    prefs: dict[str, Any],
) -> dict[str, Any]:
    clean_uid = (user_id or "").strip()
    if not clean_uid:
        raise ValueError("Пользователь не указан")
    now = utc_now_iso()
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT advisor_prefs_json FROM users WHERE id = ? LIMIT 1",
            (clean_uid,),
        )
        row = await cur.fetchone()
        if not row:
            raise LookupError("Пользователь не найден")
        merged = normalize_advisor_prefs(row["advisor_prefs_json"])
        if isinstance(prefs, dict):
            for key in ("enabled", "strategy", "auto_pick_new_tab", "composer_hints"):
                if key in prefs and prefs[key] is not None:
                    merged[key] = prefs[key]
        saved = normalize_advisor_prefs(merged)
        await db.execute(
            "UPDATE users SET advisor_prefs_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(saved, ensure_ascii=False), now, clean_uid),
        )
        await db.commit()
        invalidate_user_cache(clean_uid)
    return saved


async def _ensure_notification_prefs_column(db: aiosqlite.Connection, user_cols: set[str]) -> None:
    if "notification_prefs_json" not in user_cols:
        await db.execute("ALTER TABLE users ADD COLUMN notification_prefs_json TEXT NOT NULL DEFAULT '{}';")


async def update_user_notification_prefs(user_id: str, patch: dict[str, Any]) -> dict[str, bool]:
    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT notification_prefs_json FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            raise LookupError("Пользователь не найден")
        saved = normalize_notification_prefs(row[0])
        saved.update({key: value for key, value in patch.items() if key in saved and isinstance(value, bool)})
        await db.execute(
            "UPDATE users SET notification_prefs_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(saved), utc_now_iso(), user_id),
        )
        await db.commit()
    invalidate_user_cache(user_id)
    return saved


async def upsert_push_subscription(
    user_id: str, endpoint: str, p256dh: str, auth: str, device_label: str, user_agent: str
) -> str:
    # A fresh ID prevents an in-flight response from expiring a renewed subscription.
    sub_id = uuid.uuid4().hex
    now = utc_now_iso()
    async with connect_db() as db:
        await db.execute(
            """
            INSERT INTO push_subscriptions
                (id, user_id, endpoint, p256dh, auth, device_label, user_agent, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                id = excluded.id, user_id = excluded.user_id, p256dh = excluded.p256dh, auth = excluded.auth,
                device_label = excluded.device_label, user_agent = excluded.user_agent,
                last_seen_at = excluded.last_seen_at, last_error = '',
                created_at = CASE WHEN user_id = excluded.user_id THEN created_at ELSE excluded.created_at END
            WHERE user_id = excluded.user_id OR (p256dh = excluded.p256dh AND auth = excluded.auth)
            """,
            (sub_id, user_id, endpoint, p256dh, auth, device_label, user_agent, now, now),
        )
        async with db.execute("SELECT id FROM push_subscriptions WHERE endpoint = ?", (endpoint,)) as cur:
            if (await cur.fetchone())[0] != sub_id:
                raise ValueError("Эта подписка уже привязана к другому устройству")
        await db.execute(
            """DELETE FROM push_subscriptions WHERE id IN (
                SELECT id FROM push_subscriptions WHERE user_id = ?
                ORDER BY last_seen_at DESC, rowid DESC LIMIT -1 OFFSET 12
            )""",
            (user_id,),
        )
        await db.commit()
    return sub_id


async def list_user_push_subscriptions(user_id: str, include_secrets: bool = False) -> list[dict[str, Any]]:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM push_subscriptions WHERE user_id = ? ORDER BY last_seen_at DESC, rowid DESC",
            (user_id,),
        ) as cur:
            subscriptions = [dict(row) for row in await cur.fetchall()]
    if not include_secrets:
        for item in subscriptions:
            endpoint = item["endpoint"]
            item["endpoint"] = f"{urlsplit(endpoint).hostname}/…{endpoint[-6:]}"
            item["endpoint_fingerprint"] = hashlib.sha256(endpoint.encode()).hexdigest()
            for key in ("p256dh", "auth", "user_id"):
                item.pop(key)
    return subscriptions


async def delete_push_subscription(user_id: str, *, sub_id: str = "", endpoint: str = "") -> bool:
    if not (sub_id or endpoint):
        return False
    column, value = ("id", sub_id) if sub_id else ("endpoint", endpoint)
    async with connect_db() as db:
        cursor = await db.execute(f"DELETE FROM push_subscriptions WHERE user_id = ? AND {column} = ?", (user_id, value))
        await db.commit()
        return cursor.rowcount > 0


async def delete_expired_push_endpoint(endpoint: str, *, sub_id: str = "") -> None:
    async with connect_db() as db:
        await db.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ? AND (? = '' OR id = ?)",
            (endpoint, sub_id, sub_id),
        )
        await db.commit()


async def record_push_subscription_result(sub_id: str, error_text: str = "") -> None:
    async with connect_db() as db:
        await db.execute("UPDATE push_subscriptions SET last_error = ? WHERE id = ?", (error_text[:200], sub_id))
        await db.commit()


async def delete_user(user_id: str) -> bool:
    existing = await get_user_by_id(user_id)
    if not existing:
        return False
    if existing["id"] == "admin" or existing["role"] == "admin":
        raise ValueError("Нельзя удалить главного администратора")

    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON;")
        # Remove user from any shared projects' member_ids_json
        cur_sp = await db.execute("SELECT id, member_ids_json FROM shared_projects;")
        for sp_row in await cur_sp.fetchall():
            try:
                mids = json.loads(sp_row["member_ids_json"] or "[]")
            except Exception:
                mids = []
            if user_id in mids:
                new_mids = [m for m in mids if m != user_id]
                await db.execute(
                    "UPDATE shared_projects SET member_ids_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(new_mids, ensure_ascii=False), utc_now_iso(), sp_row["id"]),
                )
        # Delete user's personal chats & OpenRouter vault/logs from DB (files in data/workspaces/<user> stay safe on disk)
        await db.execute("DELETE FROM openrouter_logs WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM user_openrouter_configs WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM user_chatgpt_accounts WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM user_claude_accounts WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM chats WHERE owner_id = ?", (user_id,))
        cur = await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await db.commit()
        invalidate_user_cache(user_id)
        invalidate_chat_list_cache(None)
        _reset_usage_ledger_cache()
        deleted = cur.rowcount > 0

    if deleted:
        try:
            cleanup_user_cli_home(user_id)
        except Exception:
            pass
    return deleted


def _row_to_shared_project(row: aiosqlite.Row) -> dict[str, Any]:
    from . import brain_reader

    d = dict(row)
    raw_members = d.pop("member_ids_json", "[]")
    try:
        parsed = json.loads(raw_members) if raw_members else []
        d["member_ids"] = [str(x) for x in parsed if str(x).strip()] if isinstance(parsed, list) else []
    except Exception:
        d["member_ids"] = []
    d["storage_location"] = brain_reader.infer_shared_project_storage_location(str(d.get("path") or ""))
    return d


async def list_shared_projects(
    *,
    for_user_id: Optional[str] = None,
    is_admin: bool = False,
) -> list[dict[str, Any]]:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM shared_projects ORDER BY updated_at DESC, name COLLATE NOCASE ASC"
        )
        rows = await cur.fetchall()
        items = [_row_to_shared_project(r) for r in rows]
        if is_admin or not for_user_id:
            return items
        return [sp for sp in items if for_user_id in sp["member_ids"]]


async def get_shared_project(sp_id: str) -> Optional[dict[str, Any]]:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM shared_projects WHERE id = ? LIMIT 1", (sp_id,))
        row = await cur.fetchone()
        return _row_to_shared_project(row) if row else None


async def get_shared_project_by_name(name: str) -> Optional[dict[str, Any]]:
    clean = (name or "").strip()
    if not clean:
        return None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM shared_projects WHERE LOWER(name) = LOWER(?) LIMIT 1",
            (clean,),
        )
        row = await cur.fetchone()
        return _row_to_shared_project(row) if row else None


async def create_shared_project(
    *,
    name: str,
    description: str = "",
    execution_mode: str = "docker",
    member_ids: Optional[list[str]] = None,
    created_by: str = "admin",
    storage_location: Optional[str] = None,
) -> dict[str, Any]:
    import asyncio
    from . import brain_reader

    clean_name = (name or "").strip()
    if (
        not _PROJECT_NAME_RE.fullmatch(clean_name)
        or ".." in clean_name
        or "/" in clean_name
        or "\\" in clean_name
    ):
        raise ValueError(
            "Название общего проекта должно содержать от 2 до 64 символов (без слэшей и '..')"
        )

    exec_mode = execution_mode if execution_mode in VALID_SHARED_EXEC_MODES else "docker"
    eff_loc = (storage_location or "shared_dir").strip().lower() or "shared_dir"
    proj_dir = brain_reader.validate_shared_project_path(clean_name, storage_location=eff_loc)

    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur_dup = await db.execute(
            "SELECT id FROM shared_projects WHERE LOWER(name) = LOWER(?) LIMIT 1",
            (clean_name,),
        )
        if await cur_dup.fetchone():
            raise ValueError(f"Общий проект '{clean_name}' уже существует")

    proj_dir.mkdir(parents=True, exist_ok=True)
    readme_file = proj_dir / "README.md"
    if not readme_file.exists():
        readme_file.write_text(
            f"# {clean_name}\n\n"
            f"{description.strip() or 'Общий проект Antigravity Hub для совместной разработки.'}\n",
            encoding="utf-8",
        )
    await asyncio.to_thread(brain_reader.ensure_isolated_project_git_boundary, proj_dir)

    clean_members = sorted({str(m).strip() for m in (member_ids or []) if str(m).strip() and m != "admin"})
    sp_id = str(uuid.uuid4())
    now = utc_now_iso()

    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur_dup = await db.execute(
            "SELECT id FROM shared_projects WHERE LOWER(name) = LOWER(?) LIMIT 1",
            (clean_name,),
        )
        if await cur_dup.fetchone():
            raise ValueError(f"Общий проект '{clean_name}' уже существует")

        await db.execute(
            """
            INSERT INTO shared_projects (
                id, name, path, description, execution_mode,
                member_ids_json, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sp_id,
                clean_name,
                str(proj_dir),
                (description or "").strip()[:240],
                exec_mode,
                json.dumps(clean_members, ensure_ascii=False),
                created_by,
                now,
                now,
            ),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM shared_projects WHERE id = ?", (sp_id,))
        row = await cur.fetchone()
        return _row_to_shared_project(row)


async def update_shared_project(
    sp_id: str,
    *,
    description: Optional[str] = None,
    execution_mode: Optional[str] = None,
    member_ids: Optional[list[str]] = None,
    storage_location: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    import asyncio
    from . import brain_reader, sandbox_service

    existing = await get_shared_project(sp_id)
    if not existing:
        return None

    fields: list[str] = []
    params: list[Any] = []
    old_proj_dir: Optional[Path] = None
    new_proj_dir: Optional[Path] = None

    if storage_location is not None and str(storage_location).strip():
        req_loc = str(storage_location).strip().lower()
        validated_dir = brain_reader.validate_shared_project_path(
            existing["name"], storage_location=req_loc
        )
        cur_path_raw = str(existing.get("path") or "").strip()
        cur_dir = Path(cur_path_raw).resolve() if cur_path_raw else None
        validated_dir.mkdir(parents=True, exist_ok=True)
        readme_file = validated_dir / "README.md"
        if not readme_file.exists():
            desc_for_readme = (
                description.strip()
                if description is not None
                else str(existing.get("description") or "").strip()
            )
            readme_file.write_text(
                f"# {existing['name']}\n\n"
                f"{desc_for_readme or 'Общий проект Antigravity Hub для совместной разработки.'}\n",
                encoding="utf-8",
            )
        await asyncio.to_thread(brain_reader.ensure_isolated_project_git_boundary, validated_dir)
        if cur_dir is None or cur_dir != validated_dir:
            old_proj_dir = cur_dir
            new_proj_dir = validated_dir
            fields.append("path = ?")
            params.append(str(validated_dir))

    if description is not None:
        fields.append("description = ?")
        params.append(description.strip()[:240])
    if execution_mode is not None:
        clean_mode = execution_mode if execution_mode in VALID_SHARED_EXEC_MODES else "docker"
        fields.append("execution_mode = ?")
        params.append(clean_mode)
    if member_ids is not None:
        clean_members = sorted({str(m).strip() for m in member_ids if str(m).strip() and m != "admin"})
        fields.append("member_ids_json = ?")
        params.append(json.dumps(clean_members, ensure_ascii=False))

    if not fields:
        return existing

    fields.append("updated_at = ?")
    params.append(utc_now_iso())
    params.append(sp_id)

    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            f"UPDATE shared_projects SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        if new_proj_dir is not None:
            await db.execute(
                "UPDATE chats SET project_path = ? WHERE kind = 'project' AND LOWER(project_name) = LOWER(?)",
                (str(new_proj_dir), existing["name"]),
            )
        await db.commit()
        cur = await db.execute("SELECT * FROM shared_projects WHERE id = ?", (sp_id,))
        row = await cur.fetchone()
        updated_sp = _row_to_shared_project(row) if row else None

    if new_proj_dir is not None:
        try:
            await sandbox_service.stop_project_sandboxes(
                project_dir=old_proj_dir,
                project_name=existing["name"],
            )
        except Exception:
            pass

    return updated_sp


async def delete_shared_project(sp_id: str) -> bool:
    async with connect_db() as db:
        cur = await db.execute("DELETE FROM shared_projects WHERE id = ?", (sp_id,))
        await db.commit()
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Chats & Messages CRUD (Scoped by User & Shared Projects)
# ---------------------------------------------------------------------------


async def create_chat(
    *,
    owner_id: str = "admin",
    kind: str = "chat",
    project_name: str = "",
    project_path: str = "",
    title: str = "Новый диалог",
    model: str = "gemini-4-carbon-high",
    access_mode: str = "read_only",
    icon: str = "",
    folder: str = "",
    sort_order: int = 0,
    source_desktop_id: str = "",
    agy_conversation_id: str = "",
    preferred_agy_account_id: str = "",
    preferred_codex_account_id: str = "",
    preferred_claude_account_id: str = "",
) -> dict[str, Any]:
    chat_id = str(uuid.uuid4())
    now = utc_now_iso()
    clean_icon = (icon or "").strip()[:12]
    clean_folder = (folder or "").strip()[:60] if kind == "chat" else ""
    clean_pref_agy = (preferred_agy_account_id or "").strip()[:80]
    clean_pref_codex = (preferred_codex_account_id or "").strip()[:80]
    clean_pref_claude = (preferred_claude_account_id or "").strip()[:80]
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.execute(
            """
            INSERT INTO chats (
                id, owner_id, kind, project_name, project_path, title,
                model, access_mode, pinned, icon, folder, sort_order,
                agy_conversation_id, source_desktop_id,
                preferred_agy_account_id, preferred_codex_account_id, preferred_claude_account_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                owner_id or "admin",
                kind,
                project_name,
                project_path,
                title,
                model,
                access_mode,
                clean_icon,
                clean_folder,
                int(sort_order),
                agy_conversation_id,
                source_desktop_id,
                clean_pref_agy,
                clean_pref_codex,
                clean_pref_claude,
                now,
                now,
            ),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM chats WHERE id = ?", (chat_id,))
        row = await cur.fetchone()
        return _row_to_chat(row)


async def compute_single_chat_context_stats(chat: dict[str, Any]) -> dict[str, Any]:
    item = dict(chat)
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ? AND role != 'system' AND status != 'compacted'", (chat["id"],))
        total = int((await cur.fetchone())[0])
        item["message_count"] = total
        await _attach_context_metrics_in_db(db, [item])
    stats = item.get("context_stats") or compute_chat_context_stats(chat, [])
    if total and stats.get("source") == "empty":
        # A cutoff may cover every visible row even if its summary is empty.
        stats = compute_chat_context_stats(chat, [{"id": 0, "role": "user", "status": "done", "content": ""}])
    stats["total_messages_count"] = total
    return stats


async def _attach_context_metrics_in_db(
    db: aiosqlite.Connection,
    items: list[dict[str, Any]],
) -> None:
    for it in items:
        it["context_pct"] = 0.0
        it["context_used_tokens"] = 0

    candidates = [
        it
        for it in items
        if int(it.get("message_count") or 0) > 0 or int(it.get("compacted_count") or 0) > 0
    ]
    if not candidates:
        return

    by_id: dict[str, dict[str, Any]] = {str(it["id"]): it for it in candidates}
    all_ids = list(by_id.keys())

    for offset in range(0, len(all_ids), 200):
        chunk_ids = all_ids[offset : offset + 200]
        ph = ", ".join("?" for _ in chunk_ids)

        summary_by_cid: dict[str, str] = {cid: str(by_id[cid].get("compacted_summary") or "") for cid in chunk_ids}
        if any(int(by_id[cid].get("compacted_count") or 0) > 0 for cid in chunk_ids):
            cur_s = await db.execute(
                f"SELECT id, compacted_summary FROM chats WHERE id IN ({ph}) AND compacted_summary != ''",
                chunk_ids,
            )
            for s_row in await cur_s.fetchall():
                summary_by_cid[str(s_row[0])] = str(s_row[1] or "")

        cur_m = await db.execute(
            f"""
            SELECT
                m.id,
                m.chat_id,
                m.role,
                m.status,
                m.content,
                m.usage_json,
                CASE
                    WHEN m.id = (
                        SELECT m2.id FROM messages m2
                        WHERE m2.chat_id = m.chat_id AND m2.role = 'assistant' AND m2.status = 'done'
                        ORDER BY m2.id DESC LIMIT 1
                    ) THEN m.tool_calls_json
                    ELSE ''
                END AS latest_tool_calls_json,
                CASE
                    WHEN m.tool_calls_json != '' AND m.tool_calls_json != '[]' AND json_valid(m.tool_calls_json)
                    THEN json_array_length(m.tool_calls_json)
                    ELSE 0
                END AS tool_calls_len
            FROM messages m
            JOIN chats c ON c.id = m.chat_id
            WHERE m.chat_id IN ({ph})
              AND m.role != 'system'
              AND m.status != 'compacted'
              AND m.id > c.compacted_up_to_msg_id
            ORDER BY m.chat_id, m.id ASC
            """,
            chunk_ids,
        )
        msgs_by_cid: dict[str, list[dict[str, Any]]] = {}
        for mr in await cur_m.fetchall():
            cid = str(mr["chat_id"])
            raw_usage = mr["usage_json"]
            usage_obj: dict[str, Any] = {}
            if raw_usage and raw_usage != "{}":
                try:
                    parsed_u = json.loads(raw_usage)
                    if isinstance(parsed_u, dict):
                        usage_obj = parsed_u
                except Exception:
                    usage_obj = {}

            raw_latest_tcs = mr["latest_tool_calls_json"]
            tcs_list: list[dict[str, Any]] = []
            if raw_latest_tcs and raw_latest_tcs != "[]":
                try:
                    parsed_tc = json.loads(raw_latest_tcs)
                    if isinstance(parsed_tc, list):
                        tcs_list = [
                            {"step_index": tc.get("step_index")}
                            if isinstance(tc, dict)
                            else {}
                            for tc in parsed_tc
                        ]
                except Exception:
                    tcs_list = [{}] * int(mr["tool_calls_len"] or 0)
            else:
                tc_len = int(mr["tool_calls_len"] or 0)
                if tc_len > 0:
                    tcs_list = [{}] * tc_len

            msgs_by_cid.setdefault(cid, []).append(
                {
                    "id": int(mr["id"]),
                    "role": str(mr["role"] or ""),
                    "status": str(mr["status"] or ""),
                    "content": str(mr["content"] or ""),
                    "usage": usage_obj,
                    "tool_calls": tcs_list,
                }
            )

        for cid in chunk_ids:
            chat_item = by_id[cid]
            calc_chat = {
                **chat_item,
                "compacted_summary": summary_by_cid.get(cid, ""),
            }
            stats = compute_chat_context_stats(calc_chat, msgs_by_cid.get(cid, []))
            chat_item["context_stats"] = stats
            chat_item["context_pct"] = float(stats.get("pct") or 0.0)
            chat_item["context_used_tokens"] = int(stats.get("used_tokens") or 0)


async def list_chats(
    *,
    kind: Optional[str] = None,
    project_name: Optional[str] = None,
    query: Optional[str] = None,
    owner_id: Optional[str] = None,
    shared_project_names: Optional[list[str]] = None,
    recent_only: bool = False,
    limit: Optional[int] = None,
    include_context_pct: bool = True,
    include_archived: bool = True,
) -> list[dict[str, Any]]:
    # Explicit column list: never pull the (potentially huge) compacted_summary blob,
    # and no LEFT JOIN messages / DISTINCT — full-text match uses a correlated EXISTS.
    sql = """
        SELECT c.id, c.owner_id, c.kind, c.project_name, c.project_path, c.title,
            c.model, c.access_mode, c.pinned, c.icon, c.folder, c.sort_order,
            c.agy_conversation_id, c.agy_account_id, c.codex_thread_id, c.codex_account_id, c.last_engine,
            c.claude_session_id, c.claude_account_id,
            c.preferred_agy_account_id, c.preferred_codex_account_id, c.preferred_claude_account_id,
            c.source_desktop_id,
            c.compacted_up_to_msg_id, c.compacted_count,
            c.is_archived, c.archived_at,
            c.created_at, c.updated_at,
            (
                SELECT SUBSTR(m.content, 1, 140)
                FROM messages m
                WHERE m.chat_id = c.id AND m.content != '' AND m.role != 'system'
                ORDER BY m.id DESC LIMIT 1
            ) AS preview,
            (
                SELECT COUNT(*) FROM messages m WHERE m.chat_id = c.id AND m.role != 'system'
            ) AS message_count,
            (
                SELECT COUNT(*) FROM messages bm WHERE bm.chat_id = c.id AND bm.bookmarked = 1
            ) AS bookmark_count,
            (
                SELECT m.status
                FROM messages m
                WHERE m.chat_id = c.id AND m.role = 'assistant'
                ORDER BY m.id DESC LIMIT 1
            ) AS last_assistant_status
        FROM chats c
        WHERE 1=1
    """
    params: list[Any] = []
    if owner_id:
        shared_low = [s.strip().lower() for s in (shared_project_names or []) if s.strip()]
        if shared_low and kind != "chat":
            placeholders = ", ".join("?" for _ in shared_low)
            sql += f" AND (c.owner_id = ? OR (c.kind = 'project' AND LOWER(c.project_name) IN ({placeholders})))"
            params.append(owner_id)
            params.extend(shared_low)
        else:
            sql += " AND c.owner_id = ?"
            params.append(owner_id)

    if kind:
        sql += " AND c.kind = ?"
        params.append(kind)
    if project_name:
        sql += " AND LOWER(c.project_name) = LOWER(?)"
        params.append(project_name)
    if not include_archived:
        sql += " AND c.is_archived = 0"
    if query and query.strip():
        q = f"%{query.strip()}%"
        sql += (
            " AND (c.title LIKE ? OR c.folder LIKE ? OR EXISTS ("
            "SELECT 1 FROM messages ms WHERE ms.chat_id = c.id "
            "AND (ms.content LIKE ? OR ms.thinking LIKE ?)))"
        )
        params.extend([q, q, q, q])

    if recent_only:
        sql += " ORDER BY c.updated_at DESC"
    else:
        sql += " ORDER BY c.pinned DESC, c.sort_order ASC, c.updated_at DESC"

    if limit is not None and 1 <= int(limit) <= 50:
        sql += " LIMIT ?"
        params.append(int(limit))

    if not (query and query.strip()):
        # v110: быстрый путь без поиска — подзапросы к messages и расчёт контекста
        # только для чатов с промахом _CHAT_LIST_CACHE.
        return await _list_chats_cached(
            sql_suffix=sql[sql.index("FROM chats c"):],
            params=params,
            include_context_pct=include_context_pct,
        )

    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
        items = [_row_to_chat(r) for r in rows]
        for item in items:
            item.pop("compacted_summary", None)
        if include_context_pct and items:
            await _attach_context_metrics_in_db(db, items)
        return items


_CHAT_LIST_BASE_COLUMNS = """
        SELECT c.id, c.owner_id, c.kind, c.project_name, c.project_path, c.title,
            c.model, c.access_mode, c.pinned, c.icon, c.folder, c.sort_order,
            c.agy_conversation_id, c.agy_account_id, c.codex_thread_id, c.codex_account_id, c.last_engine,
            c.claude_session_id, c.claude_account_id,
            c.preferred_agy_account_id, c.preferred_codex_account_id, c.preferred_claude_account_id,
            c.source_desktop_id,
            c.compacted_up_to_msg_id, c.compacted_count,
            c.is_archived, c.archived_at,
            c.created_at, c.updated_at
"""

_CHAT_LIST_METRICS_SQL = """
        SELECT c.id,
            (
                SELECT SUBSTR(m.content, 1, 140)
                FROM messages m
                WHERE m.chat_id = c.id AND m.content != '' AND m.role != 'system'
                ORDER BY m.id DESC LIMIT 1
            ) AS preview,
            (
                SELECT COUNT(*) FROM messages m WHERE m.chat_id = c.id AND m.role != 'system'
            ) AS message_count,
            (
                SELECT COUNT(*) FROM messages bm WHERE bm.chat_id = c.id AND bm.bookmarked = 1
            ) AS bookmark_count,
            (
                SELECT m.status
                FROM messages m
                WHERE m.chat_id = c.id AND m.role = 'assistant'
                ORDER BY m.id DESC LIMIT 1
            ) AS last_assistant_status
        FROM chats c
        WHERE c.id IN ({ph})
"""

_CHAT_LIST_CACHE_MAX_AGE_SEC = 300.0
_CHAT_LIST_METRIC_KEYS = ("preview", "message_count", "bookmark_count", "last_assistant_status")
_CHAT_LIST_CTX_KEYS = ("context_pct", "context_used_tokens")
# (resolved_db_path, chat_id) -> (stamp, metrics)
_CHAT_LIST_CACHE: dict[tuple[str, str], tuple[tuple[Any, ...], dict[str, Any]]] = {}
_CHAT_LIST_CACHE_EPOCH = 0


def invalidate_chat_list_cache(chat_id: Optional[str] = None) -> None:
    """Сброс кэша метрик/превью боковой панели (одного чата или всех)."""
    global _CHAT_LIST_CACHE_EPOCH
    _CHAT_LIST_CACHE_EPOCH += 1
    if chat_id is None:
        _CHAT_LIST_CACHE.clear()
        return
    cid = str(chat_id)
    for key in [k for k in _CHAT_LIST_CACHE if k[1] == cid]:
        _CHAT_LIST_CACHE.pop(key, None)


def _chat_list_stamp(c: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(c.get("updated_at") or ""),
        str(c.get("model") or ""),
        str(c.get("kind") or ""),
        int(bool(c.get("pinned", 0))),
        int(bool(c.get("is_archived", 0))),
        str(c.get("archived_at") or ""),
        int(c.get("compacted_up_to_msg_id") or 0),
        int(c.get("compacted_count") or 0),
        str(c.get("agy_conversation_id") or ""),
        str(c.get("agy_account_id") or ""),
        str(c.get("codex_thread_id") or ""),
        str(c.get("codex_account_id") or ""),
        str(c.get("claude_session_id") or ""),
        str(c.get("claude_account_id") or ""),
        str(c.get("preferred_agy_account_id") or ""),
        str(c.get("preferred_codex_account_id") or ""),
        str(c.get("preferred_claude_account_id") or ""),
        str(c.get("last_engine") or ""),
    )


async def _list_chats_cached(
    *,
    sql_suffix: str,
    params: list[Any],
    include_context_pct: bool,
) -> list[dict[str, Any]]:
    path = _resolved_db_path()
    epoch_before = _CHAT_LIST_CACHE_EPOCH
    now_m = time.monotonic()
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(_CHAT_LIST_BASE_COLUMNS + "        " + sql_suffix, params)
        rows = await cur.fetchall()
        items = [_row_to_chat(r) for r in rows]

        dirty: list[dict[str, Any]] = []
        need_ctx: list[dict[str, Any]] = []
        stamps: dict[str, tuple[Any, ...]] = {}
        for item in items:
            item.pop("compacted_summary", None)
            cid = str(item["id"])
            stamp = _chat_list_stamp(item)
            stamps[cid] = stamp
            hit = _CHAT_LIST_CACHE.get((path, cid))
            if (
                hit is not None
                and hit[0] == stamp
                and (now_m - float(hit[1].get("_built_at", 0.0))) < _CHAT_LIST_CACHE_MAX_AGE_SEC
            ):
                metrics = hit[1]
                for k in _CHAT_LIST_METRIC_KEYS:
                    item[k] = metrics.get(k)
                if include_context_pct:
                    if all(k in metrics for k in _CHAT_LIST_CTX_KEYS):
                        for k in _CHAT_LIST_CTX_KEYS:
                            item[k] = metrics[k]
                    else:
                        need_ctx.append(item)
            else:
                dirty.append(item)
                if include_context_pct:
                    need_ctx.append(item)

        if dirty:
            by_id = {str(it["id"]): it for it in dirty}
            dirty_ids = list(by_id.keys())
            for offset in range(0, len(dirty_ids), 200):
                chunk = dirty_ids[offset : offset + 200]
                ph = ", ".join("?" for _ in chunk)
                cur_m = await db.execute(_CHAT_LIST_METRICS_SQL.format(ph=ph), chunk)
                for mr in await cur_m.fetchall():
                    it = by_id.get(str(mr["id"]))
                    if it is None:
                        continue
                    it["preview"] = mr["preview"]
                    it["message_count"] = int(mr["message_count"] or 0)
                    it["bookmark_count"] = int(mr["bookmark_count"] or 0)
                    it["last_assistant_status"] = mr["last_assistant_status"]
            for it in dirty:
                it.setdefault("preview", None)
                it.setdefault("message_count", 0)
                it.setdefault("bookmark_count", 0)
                it.setdefault("last_assistant_status", None)

        if need_ctx:
            await _attach_context_metrics_in_db(db, need_ctx)

    if epoch_before == _CHAT_LIST_CACHE_EPOCH:
        refreshed = {id(it) for it in dirty} | {id(it) for it in need_ctx}
        if len(_CHAT_LIST_CACHE) > 4096:
            _CHAT_LIST_CACHE.clear()
        for item in items:
            if id(item) not in refreshed:
                continue
            cid = str(item["id"])
            metrics: dict[str, Any] = {k: item.get(k) for k in _CHAT_LIST_METRIC_KEYS}
            metrics["_built_at"] = now_m
            if all(k in item for k in _CHAT_LIST_CTX_KEYS):
                for k in _CHAT_LIST_CTX_KEYS:
                    metrics[k] = item[k]
            _CHAT_LIST_CACHE[(path, cid)] = (stamps[cid], metrics)
    return items


async def list_chat_folders(owner_id: Optional[str] = None) -> list[str]:
    async with connect_db() as db:
        if owner_id:
            cur = await db.execute(
                "SELECT DISTINCT folder FROM chats WHERE kind = 'chat' AND owner_id = ? AND folder != '' ORDER BY folder COLLATE NOCASE ASC",
                (owner_id,),
            )
        else:
            cur = await db.execute(
                "SELECT DISTINCT folder FROM chats WHERE kind = 'chat' AND folder != '' ORDER BY folder COLLATE NOCASE ASC"
            )
        rows = await cur.fetchall()
        return [str(r[0]) for r in rows if r[0]]


async def get_chat(chat_id: str) -> Optional[dict[str, Any]]:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM chats WHERE id = ?", (chat_id,))
        row = await cur.fetchone()
        return _row_to_chat(row) if row else None


async def find_chat_by_source_desktop_id(
    source_desktop_id: str,
    *,
    owner_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    clean_id = (source_desktop_id or "").strip()
    if not clean_id:
        return None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        if owner_id:
            cur = await db.execute(
                """
                SELECT c.*,
                    (SELECT COUNT(*) FROM messages m WHERE m.chat_id = c.id) AS _msg_cnt
                FROM chats c
                WHERE c.source_desktop_id = ? AND c.owner_id = ?
                ORDER BY (c.agy_conversation_id != '') DESC, _msg_cnt DESC, c.updated_at DESC
                LIMIT 1
                """,
                (clean_id, owner_id),
            )
        else:
            cur = await db.execute(
                """
                SELECT c.*,
                    (SELECT COUNT(*) FROM messages m WHERE m.chat_id = c.id) AS _msg_cnt
                FROM chats c
                WHERE c.source_desktop_id = ?
                ORDER BY (c.agy_conversation_id != '') DESC, _msg_cnt DESC, c.updated_at DESC
                LIMIT 1
                """,
                (clean_id,),
            )
        row = await cur.fetchone()
        if not row:
            return None
        d = _row_to_chat(row)
        d.pop("_msg_cnt", None)
        return d


async def get_imported_desktop_map(
    *,
    owner_id: Optional[str] = None,
) -> dict[str, dict[str, str]]:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        if owner_id:
            cur = await db.execute(
                """
                SELECT c.id, c.title, c.source_desktop_id,
                    (SELECT COUNT(*) FROM messages m WHERE m.chat_id = c.id) AS _msg_cnt
                FROM chats c
                WHERE c.source_desktop_id != '' AND c.owner_id = ?
                ORDER BY (c.agy_conversation_id != '') DESC, _msg_cnt DESC, c.updated_at DESC
                """,
                (owner_id,),
            )
        else:
            cur = await db.execute(
                """
                SELECT c.id, c.title, c.source_desktop_id,
                    (SELECT COUNT(*) FROM messages m WHERE m.chat_id = c.id) AS _msg_cnt
                FROM chats c
                WHERE c.source_desktop_id != ''
                ORDER BY (c.agy_conversation_id != '') DESC, _msg_cnt DESC, c.updated_at DESC
                """
            )
        rows = await cur.fetchall()
        out: dict[str, dict[str, str]] = {}
        for r in rows:
            sid = str(r["source_desktop_id"] or "").strip()
            if sid and sid not in out:
                out[sid] = {"id": str(r["id"]), "title": str(r["title"] or "Диалог")}
        return out


async def update_chat(
    chat_id: str,
    *,
    title: Optional[str] = None,
    model: Optional[str] = None,
    access_mode: Optional[str] = None,
    pinned: Optional[bool] = None,
    icon: Optional[str] = None,
    folder: Optional[str] = None,
    sort_order: Optional[int] = None,
    kind: Optional[str] = None,
    project_name: Optional[str] = None,
    project_path: Optional[str] = None,
    agy_conversation_id: Optional[str] = None,
    codex_thread_id: Optional[str] = None,
    agy_account_id: Optional[str] = None,
    codex_account_id: Optional[str] = None,
    last_engine: Optional[str] = None,
    claude_session_id: Optional[str] = None,
    claude_account_id: Optional[str] = None,
    preferred_agy_account_id: Optional[str] = None,
    preferred_codex_account_id: Optional[str] = None,
    preferred_claude_account_id: Optional[str] = None,
    compacted_summary: Optional[str] = None,
    compacted_up_to_msg_id: Optional[int] = None,
    compacted_count: Optional[int] = None,
    is_archived: Optional[bool] = None,
    touch_updated_at: bool = False,
) -> Optional[dict[str, Any]]:
    fields: list[str] = []
    params: list[Any] = []
    if is_archived is True:
        pinned = False
    if title is not None:
        fields.append("title = ?")
        params.append(title.strip() or "Без названия")
    if model is not None:
        fields.append("model = ?")
        params.append(model)
    if access_mode is not None:
        fields.append("access_mode = ?")
        params.append(access_mode)
    if pinned is not None:
        fields.append("pinned = ?")
        params.append(1 if pinned else 0)
    if icon is not None:
        fields.append("icon = ?")
        params.append(icon.strip()[:12])
    if folder is not None:
        fields.append("folder = ?")
        params.append(folder.strip()[:60])
    if sort_order is not None:
        fields.append("sort_order = ?")
        params.append(int(sort_order))
    if kind is not None:
        fields.append("kind = ?")
        params.append("project" if kind == "project" else "chat")
    if project_name is not None:
        fields.append("project_name = ?")
        params.append(project_name.strip())
    if project_path is not None:
        fields.append("project_path = ?")
        params.append(project_path.strip())
    if agy_conversation_id is not None:
        fields.append("agy_conversation_id = ?")
        params.append(agy_conversation_id)
    if codex_thread_id is not None:
        fields.append("codex_thread_id = ?")
        params.append(codex_thread_id)
    if agy_account_id is not None:
        fields.append("agy_account_id = ?")
        params.append(agy_account_id)
    if codex_account_id is not None:
        fields.append("codex_account_id = ?")
        params.append(codex_account_id)
    if last_engine is not None:
        fields.append("last_engine = ?")
        _le = str(last_engine).strip().lower()
        params.append(_le if _le in ("", "agy", "codex", "claude") else "agy")
    if claude_session_id is not None:
        fields.append("claude_session_id = ?")
        params.append(claude_session_id)
    if claude_account_id is not None:
        fields.append("claude_account_id = ?")
        params.append(claude_account_id)
    if preferred_agy_account_id is not None:
        fields.append("preferred_agy_account_id = ?")
        params.append(preferred_agy_account_id.strip()[:80])
    if preferred_codex_account_id is not None:
        fields.append("preferred_codex_account_id = ?")
        params.append(preferred_codex_account_id.strip()[:80])
    if preferred_claude_account_id is not None:
        fields.append("preferred_claude_account_id = ?")
        params.append(preferred_claude_account_id.strip()[:80])
    if compacted_summary is not None:
        fields.append("compacted_summary = ?")
        params.append(compacted_summary)
    if compacted_up_to_msg_id is not None:
        fields.append("compacted_up_to_msg_id = ?")
        params.append(int(compacted_up_to_msg_id))
    if compacted_count is not None:
        fields.append("compacted_count = ?")
        params.append(int(compacted_count))
    if is_archived is True:
        fields.append("is_archived = ?")
        params.append(1)
        fields.append("archived_at = ?")
        params.append(utc_now_iso())
    elif is_archived is False:
        fields.append("is_archived = ?")
        params.append(0)
        fields.append("archived_at = ?")
        params.append("")
    if touch_updated_at:
        fields.append("updated_at = ?")
        params.append(utc_now_iso())

    if not fields:
        return await get_chat(chat_id)

    params.append(chat_id)
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            f"UPDATE chats SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        await db.commit()
        invalidate_chat_list_cache(chat_id)
        cur = await db.execute("SELECT * FROM chats WHERE id = ?", (chat_id,))
        row = await cur.fetchone()
        return _row_to_chat(row) if row else None


async def save_chat_compaction(
    chat_id: str,
    *,
    compacted_summary: str,
    compacted_up_to_msg_id: int,
    compacted_count: int,
) -> Optional[dict[str, Any]]:
    """Persist chat compaction summary, mirror `CONTEXT_SUMMARY.md` for free chats, and reset CLI session IDs."""
    updated = await update_chat(
        chat_id,
        compacted_summary=compacted_summary,
        compacted_up_to_msg_id=int(compacted_up_to_msg_id),
        compacted_count=int(compacted_count),
        agy_conversation_id="",
        codex_thread_id="",
        codex_account_id="",
        claude_session_id="",
        claude_account_id="",
        last_engine="",
        touch_updated_at=True,
    )
    if updated and str(updated.get("kind") or "chat") == "chat":
        try:
            owner_id = str(updated.get("owner_id") or "admin")
            owner_user = await get_user_by_id(owner_id) if owner_id != "admin" else None
            space = _ensure_user_space(owner_user or owner_id)
            safe_cid = config.sanitize_chat_folder_id(chat_id)
            chat_dir = (space["chats"] / safe_cid).resolve()
            chat_dir.mkdir(parents=True, exist_ok=True)
            summary_clean = (compacted_summary or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            title_str = str(updated.get("title") or chat_id).strip()
            md_text = (
                f"# Сводка контекста чата: {title_str}\n\n"
                f"- **ID чата:** `{chat_id}`\n"
                f"- **Сжато сообщений:** {int(compacted_count)}\n"
                f"- **Обновлено:** {utc_now_iso()}\n\n"
                f"{summary_clean or '_(Пустая сводка)_'}\n"
            )
            config._atomic_sync_text_file(chat_dir / "CONTEXT_SUMMARY.md", md_text)
        except Exception:
            pass
    return updated


async def reorder_chats(chat_ids: list[str], *, owner_id: Optional[str] = None) -> None:
    if not chat_ids:
        return
    async with connect_db() as db:
        for idx, cid in enumerate(chat_ids, start=1):
            if owner_id:
                await db.execute(
                    "UPDATE chats SET sort_order = ? WHERE id = ? AND owner_id = ?",
                    (idx, str(cid), owner_id),
                )
            else:
                await db.execute(
                    "UPDATE chats SET sort_order = ? WHERE id = ?",
                    (idx, str(cid)),
                )
        await db.commit()


async def delete_chat(chat_id: str) -> bool:
    async with connect_db() as db:
        cur = await db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        await db.commit()
        invalidate_chat_list_cache(chat_id)
        _USAGE_LEDGER_CACHE.setdefault("evicted_chats", set()).add(str(chat_id))
        _bump_usage_ledger_version()
        return cur.rowcount > 0


async def get_messages(chat_id: str, include_thinking: bool = True) -> list[dict[str, Any]]:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        if include_thinking:
            cur = await db.execute(
                """
                SELECT
                    m.*,
                    CASE WHEN TRIM(COALESCE(m.thinking, '')) != '' THEN 1 ELSE 0 END AS has_thinking,
                    COALESCE(u.display_name, u.username, '') AS author_name
                FROM messages m
                LEFT JOIN users u ON u.id = m.user_id
                WHERE m.chat_id = ?
                ORDER BY m.id ASC
                """,
                (chat_id,),
            )
        else:
            cur = await db.execute(
                """
                SELECT
                    m.id,
                    m.chat_id,
                    m.user_id,
                    m.role,
                    m.content,
                    CASE WHEN m.status = 'streaming' THEN m.thinking ELSE '' END AS thinking,
                    CASE WHEN TRIM(COALESCE(m.thinking, '')) != '' THEN 1 ELSE 0 END AS has_thinking,
                    m.tool_calls_json,
                    m.attachments_json,
                    m.model,
                    m.usage_json,
                    m.status,
                    m.bookmarked,
                    m.custom_title,
                    m.auto_title,
                    m.created_at,
                    COALESCE(u.display_name, u.username, '') AS author_name
                FROM messages m
                LEFT JOIN users u ON u.id = m.user_id
                WHERE m.chat_id = ?
                ORDER BY m.id ASC
                """,
                (chat_id,),
            )
        rows = await cur.fetchall()
        return [_row_to_message(r) for r in rows]


async def get_continue_turn_messages(
    chat_id: str,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Targeted lookup for `continue_turn`: last non-system message and last preceding user message."""
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
                m.id,
                m.chat_id,
                m.user_id,
                m.role,
                m.content,
                CASE WHEN m.status = 'streaming' THEN m.thinking ELSE '' END AS thinking,
                CASE WHEN TRIM(COALESCE(m.thinking, '')) != '' THEN 1 ELSE 0 END AS has_thinking,
                m.tool_calls_json,
                m.attachments_json,
                m.model,
                m.usage_json,
                m.status,
                m.bookmarked,
                m.custom_title,
                m.auto_title,
                m.created_at,
                COALESCE(u.display_name, u.username, '') AS author_name
            FROM messages m
            LEFT JOIN users u ON u.id = m.user_id
            WHERE m.chat_id = ? AND m.role != 'system'
            ORDER BY m.id DESC
            LIMIT 1
            """,
            (chat_id,),
        )
        target_row = await cur.fetchone()
        if not target_row:
            return None, None
        target_msg = _row_to_message(target_row)
        cur_u = await db.execute(
            """
            SELECT
                m.id,
                m.chat_id,
                m.user_id,
                m.role,
                m.content,
                '' AS thinking,
                CASE WHEN TRIM(COALESCE(m.thinking, '')) != '' THEN 1 ELSE 0 END AS has_thinking,
                m.tool_calls_json,
                m.attachments_json,
                m.model,
                m.usage_json,
                m.status,
                m.bookmarked,
                m.custom_title,
                m.auto_title,
                m.created_at,
                COALESCE(u.display_name, u.username, '') AS author_name
            FROM messages m
            LEFT JOIN users u ON u.id = m.user_id
            WHERE m.chat_id = ? AND m.role = 'user' AND m.id < ?
            ORDER BY m.id DESC
            LIMIT 1
            """,
            (chat_id, int(target_msg["id"])),
        )
        user_row = await cur_u.fetchone()
        return target_msg, (_row_to_message(user_row) if user_row else None)


async def checkpoint_streaming_message(
    message_id: int,
    *,
    content: str,
    thinking: str,
    tool_calls: Optional[list[dict[str, Any]]] = None,
) -> bool:
    """Persist intermediate streaming progress without bumping `_USAGE_LEDGER_VERSION`."""
    tc_json = json.dumps(compact_tool_calls_payload(tool_calls or []), ensure_ascii=False)
    async with connect_db() as db:
        cur = await db.execute(
            """
            UPDATE messages
            SET content = ?, thinking = ?, tool_calls_json = ?
            WHERE id = ? AND status = 'streaming'
            """,
            (content, thinking, tc_json, int(message_id)),
        )
        await db.commit()
        return bool(cur.rowcount and cur.rowcount > 0)


async def checkpoint_chat_streaming_message(
    chat_id: str,
    *,
    content: str,
    thinking: str,
    tool_calls: Optional[list[dict[str, Any]]] = None,
) -> bool:
    """Persist interrupted snapshot of a chat's active `streaming` message during shutdown."""
    tc_json = json.dumps(compact_tool_calls_payload(tool_calls or []), ensure_ascii=False)
    async with connect_db() as db:
        cur = await db.execute(
            """
            UPDATE messages
            SET content = ?, thinking = ?, tool_calls_json = ?
            WHERE id = (
                SELECT id FROM messages
                WHERE chat_id = ? AND status = 'streaming'
                ORDER BY id DESC
                LIMIT 1
            )
            """,
            (content, thinking, tc_json, str(chat_id)),
        )
        await db.commit()
        return bool(cur.rowcount and cur.rowcount > 0)


async def get_message_thinking(chat_id: str, message_id: int) -> Optional[dict[str, Any]]:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, chat_id, status, thinking
            FROM messages
            WHERE id = ? AND chat_id = ?
            LIMIT 1
            """,
            (int(message_id), chat_id),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "chat_id": str(row["chat_id"]),
            "status": str(row["status"] or "done"),
            "thinking": str(row["thinking"] or ""),
        }


async def chat_references_brain_id(chat_id: str, brain_conv_id: str) -> bool:
    """Return True if any message in `chat_id` references `brain_conv_id` in content or tool calls."""
    clean_id = (brain_conv_id or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F\-]{8,64}", clean_id):
        return False
    like_pat = f"%{clean_id}%"
    async with connect_db() as db:
        cur = await db.execute(
            """
            SELECT 1 FROM messages
            WHERE chat_id = ? AND (content LIKE ? OR tool_calls_json LIKE ?)
            LIMIT 1
            """,
            (chat_id, like_pat, like_pat),
        )
        row = await cur.fetchone()
        return row is not None


async def add_message(
    chat_id: str,
    *,
    user_id: str = "admin",
    role: str,
    content: str = "",
    thinking: str = "",
    tool_calls: Optional[list[dict[str, Any]]] = None,
    attachments: Optional[list[dict[str, Any]]] = None,
    model: str = "",
    usage: Optional[dict[str, Any]] = None,
    status: str = "done",
    custom_title: str = "",
    auto_title: str = "",
    created_at: Optional[str] = None,
) -> dict[str, Any]:
    now = created_at or utc_now_iso()
    clean_custom_title = re.sub(r"\s+", " ", str(custom_title or "")).strip()[:140].rstrip()
    clean_auto_title = re.sub(r"\s+", " ", str(auto_title or "")).strip()[:140].rstrip()
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            INSERT INTO messages (
                chat_id, user_id, role, content, thinking,
                tool_calls_json, attachments_json,
                model, usage_json, status, custom_title, auto_title, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                user_id or "admin",
                role,
                content,
                thinking,
                json.dumps(compact_tool_calls_payload(tool_calls), ensure_ascii=False),
                json.dumps(attachments or [], ensure_ascii=False),
                model,
                json.dumps(usage or {}, ensure_ascii=False),
                status,
                clean_custom_title,
                clean_auto_title,
                now,
            ),
        )
        msg_id = cur.lastrowid
        await db.execute(
            "UPDATE chats SET updated_at = ? WHERE id = ?",
            (now, chat_id),
        )
        await db.commit()
        invalidate_chat_list_cache(chat_id)
        if role == "assistant":
            _bump_usage_ledger_version(msg_id)
        row_cur = await db.execute(
            """
            SELECT m.*, COALESCE(u.display_name, u.username, '') AS author_name
            FROM messages m
            LEFT JOIN users u ON u.id = m.user_id
            WHERE m.id = ?
            """,
            (msg_id,),
        )
        row = await row_cur.fetchone()
        return _row_to_message(row)


async def add_messages_bulk(
    chat_id: str,
    *,
    user_id: str = "admin",
    messages: list[dict[str, Any]],
) -> int:
    if not messages:
        return 0
    now_touch = utc_now_iso()
    uid = user_id or "admin"
    rows_to_insert = [
        (
            chat_id,
            uid,
            m.get("role", "user"),
            m.get("content", ""),
            m.get("thinking", ""),
            json.dumps(compact_tool_calls_payload(m.get("tool_calls")), ensure_ascii=False),
            json.dumps(m.get("attachments") or [], ensure_ascii=False),
            m.get("model", ""),
            json.dumps(m.get("usage") or {}, ensure_ascii=False),
            m.get("status", "done"),
            re.sub(r"\s+", " ", str(m.get("custom_title") or "")).strip()[:140].rstrip(),
            re.sub(r"\s+", " ", str(m.get("auto_title") or "")).strip()[:140].rstrip(),
            m.get("created_at") or now_touch,
        )
        for m in messages
    ]
    async with connect_db() as db:
        await db.executemany(
            """
            INSERT INTO messages (
                chat_id, user_id, role, content, thinking,
                tool_calls_json, attachments_json,
                model, usage_json, status, custom_title, auto_title, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows_to_insert,
        )
        await db.execute(
            "UPDATE chats SET updated_at = ? WHERE id = ?",
            (now_touch, chat_id),
        )
        await db.commit()
    invalidate_chat_list_cache(chat_id)
    _bump_usage_ledger_version()
    return len(rows_to_insert)


async def update_message(
    message_id: int,
    *,
    content: Optional[str] = None,
    thinking: Optional[str] = None,
    tool_calls: Optional[list[dict[str, Any]]] = None,
    usage: Optional[dict[str, Any]] = None,
    model: Optional[str] = None,
    status: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    fields: list[str] = []
    params: list[Any] = []
    if content is not None:
        fields.append("content = ?")
        params.append(content)
    if thinking is not None:
        fields.append("thinking = ?")
        params.append(thinking)
    if tool_calls is not None:
        fields.append("tool_calls_json = ?")
        params.append(json.dumps(compact_tool_calls_payload(tool_calls), ensure_ascii=False))
    if usage is not None:
        fields.append("usage_json = ?")
        params.append(json.dumps(usage, ensure_ascii=False))
    if model is not None:
        fields.append("model = ?")
        params.append(model)
    if status is not None:
        fields.append("status = ?")
        params.append(status)

    if not fields:
        return None

    params.append(message_id)
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            f"UPDATE messages SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        await db.commit()
        if usage is not None or model is not None:
            _bump_usage_ledger_version(message_id)
        cur = await db.execute(
            """
            SELECT m.*, COALESCE(u.display_name, u.username, '') AS author_name
            FROM messages m
            LEFT JOIN users u ON u.id = m.user_id
            WHERE m.id = ?
            """,
            (message_id,),
        )
        row = await cur.fetchone()
        invalidate_chat_list_cache(str(row["chat_id"]) if row else None)
        return _row_to_message(row) if row else None


async def get_message_by_id(
    chat_id: str,
    message_id: int,
    *,
    include_thinking: bool = True,
) -> Optional[dict[str, Any]]:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        if include_thinking:
            cur = await db.execute(
                """
                SELECT
                    m.*,
                    CASE WHEN TRIM(COALESCE(m.thinking, '')) != '' THEN 1 ELSE 0 END AS has_thinking,
                    COALESCE(u.display_name, u.username, '') AS author_name
                FROM messages m
                LEFT JOIN users u ON u.id = m.user_id
                WHERE m.id = ? AND m.chat_id = ?
                LIMIT 1
                """,
                (int(message_id), str(chat_id)),
            )
        else:
            cur = await db.execute(
                """
                SELECT
                    m.id,
                    m.chat_id,
                    m.user_id,
                    m.role,
                    m.content,
                    CASE WHEN m.status = 'streaming' THEN m.thinking ELSE '' END AS thinking,
                    CASE WHEN TRIM(COALESCE(m.thinking, '')) != '' THEN 1 ELSE 0 END AS has_thinking,
                    m.tool_calls_json,
                    m.attachments_json,
                    m.model,
                    m.usage_json,
                    m.status,
                    m.bookmarked,
                    m.custom_title,
                    m.auto_title,
                    m.created_at,
                    COALESCE(u.display_name, u.username, '') AS author_name
                FROM messages m
                LEFT JOIN users u ON u.id = m.user_id
                WHERE m.id = ? AND m.chat_id = ?
                LIMIT 1
                """,
                (int(message_id), str(chat_id)),
            )
        row = await cur.fetchone()
        return _row_to_message(row) if row else None


async def set_message_bookmarked(
    chat_id: str,
    message_id: int,
    bookmarked: bool,
) -> Optional[dict[str, Any]]:
    val = 1 if bookmarked else 0
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "UPDATE messages SET bookmarked = ? WHERE id = ? AND chat_id = ?",
            (val, int(message_id), str(chat_id)),
        )
        await db.commit()
        invalidate_chat_list_cache(str(chat_id))
        if not cur.rowcount:
            return None
        row_cur = await db.execute(
            """
            SELECT m.*, COALESCE(u.display_name, u.username, '') AS author_name
            FROM messages m
            LEFT JOIN users u ON u.id = m.user_id
            WHERE m.id = ? AND m.chat_id = ?
            LIMIT 1
            """,
            (int(message_id), str(chat_id)),
        )
        row = await row_cur.fetchone()
        return _row_to_message(row) if row else None


async def set_message_custom_title(
    chat_id: str,
    message_id: int,
    custom_title: str,
) -> Optional[dict[str, Any]]:
    clean_title = re.sub(r"\s+", " ", str(custom_title or "")).strip()[:140].rstrip()
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "UPDATE messages SET custom_title = ? WHERE id = ? AND chat_id = ?",
            (clean_title, int(message_id), str(chat_id)),
        )
        await db.commit()
        if not cur.rowcount:
            return await get_message_by_id(
                str(chat_id), int(message_id), include_thinking=False
            )
    return await get_message_by_id(str(chat_id), int(message_id), include_thinking=False)


async def set_message_auto_title(
    chat_id: str,
    message_id: int,
    auto_title: str,
    *,
    respect_custom: bool = True,
) -> Optional[dict[str, Any]]:
    clean = re.sub(r"\s+", " ", str(auto_title or "")).strip()[:140].rstrip()
    cid = str(chat_id or "").strip()
    mid = int(message_id)
    if not cid:
        return None
    async with connect_db() as db:
        if respect_custom:
            cur = await db.execute(
                """
                UPDATE messages
                SET auto_title = ?
                WHERE id = ?
                  AND chat_id = ?
                  AND TRIM(COALESCE(custom_title, '')) = ''
                """,
                (clean, mid, cid),
            )
        else:
            cur = await db.execute(
                """
                UPDATE messages
                SET auto_title = ?
                WHERE id = ?
                  AND chat_id = ?
                """,
                (clean, mid, cid),
            )
        await db.commit()
        updated_count = int(cur.rowcount or 0)
    if updated_count <= 0:
        return None
    return await get_message_by_id(cid, mid, include_thinking=False)


async def set_messages_auto_titles_bulk(
    updates: list[tuple[str, int, str]],
) -> list[dict[str, Any]]:
    if not updates:
        return []
    applied: list[dict[str, Any]] = []
    async with connect_db() as db:
        for raw_cid, raw_mid, raw_title in updates:
            cid = str(raw_cid or "").strip()
            mid = int(raw_mid)
            clean = re.sub(r"\s+", " ", str(raw_title or "")).strip()[:140].rstrip()
            if not cid:
                continue
            cur = await db.execute(
                """
                UPDATE messages
                SET auto_title = ?
                WHERE id = ?
                  AND chat_id = ?
                  AND TRIM(COALESCE(custom_title, '')) = ''
                """,
                (clean, mid, cid),
            )
            if int(cur.rowcount or 0) > 0:
                applied.append(
                    {
                        "chat_id": cid,
                        "message_id": mid,
                        "auto_title": clean,
                        "custom_title": "",
                    }
                )
        await db.commit()
    return applied


async def append_message_code_handoff(
    chat_id: str,
    message_id: int,
    handoff: dict[str, Any],
) -> Optional[list[dict[str, Any]]]:
    clean_chat_id = str(chat_id or "").strip()
    if not clean_chat_id:
        return None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute(
                "SELECT usage_json FROM messages WHERE id = ? AND chat_id = ?",
                (int(message_id), clean_chat_id),
            )
            row = await cur.fetchone()
            if not row:
                await db.rollback()
                return None

            raw_usage = row["usage_json"] or ""
            try:
                usage = json.loads(raw_usage) if raw_usage else {}
            except Exception:
                usage = {}
            if not isinstance(usage, dict):
                usage = {}

            raw_existing = usage.get("code_handoffs")
            existing: list[dict[str, Any]] = []
            if isinstance(raw_existing, list):
                for item in raw_existing:
                    if isinstance(item, dict) and str(item.get("target_chat_id") or "").strip():
                        existing.append(dict(item))

            h_in = handoff if isinstance(handoff, dict) else {}
            target_chat_id = str(h_in.get("target_chat_id") or "").strip()[:120]
            if not target_chat_id:
                await db.rollback()
                return existing

            target_title = (str(h_in.get("target_title") or "").strip() or "Диалог")[:160]
            target_icon = str(h_in.get("target_icon") or "").strip()[:16]
            access_mode = (str(h_in.get("access_mode") or "").strip() or "write_files")[:32]
            project_name = str(h_in.get("project_name") or "").strip()[:120]
            sent_at = str(h_in.get("sent_at") or "").strip()[:64] or utc_now_iso()

            prev_item: Optional[dict[str, Any]] = None
            filtered: list[dict[str, Any]] = []
            for item in existing:
                if str(item.get("target_chat_id") or "").strip() == target_chat_id:
                    prev_item = item
                else:
                    filtered.append(item)

            if prev_item is not None:
                try:
                    prev_count = int(prev_item.get("send_count") or 1)
                except Exception:
                    prev_count = 1
                send_count = max(1, prev_count) + 1
                eff_icon = target_icon or str(prev_item.get("target_icon") or "").strip()[:16]
                eff_proj = project_name or str(prev_item.get("project_name") or "").strip()[:120]
            else:
                send_count = 1
                eff_icon = target_icon
                eff_proj = project_name

            filtered.append(
                {
                    "target_chat_id": target_chat_id,
                    "target_title": target_title,
                    "target_icon": eff_icon,
                    "access_mode": access_mode,
                    "project_name": eff_proj,
                    "sent_at": sent_at,
                    "send_count": send_count,
                }
            )
            code_handoffs = filtered[-12:]
            usage["code_handoffs"] = code_handoffs

            await db.execute(
                "UPDATE messages SET usage_json = ? WHERE id = ? AND chat_id = ?",
                (json.dumps(usage, ensure_ascii=False), int(message_id), clean_chat_id),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        invalidate_chat_list_cache(clean_chat_id)
        return code_handoffs


async def append_message_hq_return(
    chat_id: str,
    message_id: int,
    hq_return: dict[str, Any],
) -> Optional[list[dict[str, Any]]]:
    clean_chat_id = str(chat_id or "").strip()
    if not clean_chat_id:
        return None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute(
                "SELECT usage_json FROM messages WHERE id = ? AND chat_id = ?",
                (int(message_id), clean_chat_id),
            )
            row = await cur.fetchone()
            if not row:
                await db.rollback()
                return None

            raw_usage = row["usage_json"] or ""
            try:
                usage = json.loads(raw_usage) if raw_usage else {}
            except Exception:
                usage = {}
            if not isinstance(usage, dict):
                usage = {}

            raw_existing = usage.get("hq_returns")
            existing: list[dict[str, Any]] = []
            if isinstance(raw_existing, list):
                for item in raw_existing:
                    if isinstance(item, dict) and str(item.get("target_chat_id") or "").strip():
                        existing.append(dict(item))

            h_in = hq_return if isinstance(hq_return, dict) else {}
            target_chat_id = str(h_in.get("target_chat_id") or "").strip()[:120]
            if not target_chat_id:
                await db.rollback()
                return existing

            raw_title = str(h_in.get("target_title") or "").strip()
            raw_icon = str(h_in.get("target_icon") or "").strip()
            project_name = str(h_in.get("project_name") or "").strip()[:120]
            raw_sent_at = str(h_in.get("sent_at") or "").strip()[:64]
            return_uid = str(h_in.get("return_uid") or "").strip()[:64]
            increment_count = bool(h_in.get("increment_count", True))

            raw_target_msg_id = h_in.get("target_msg_id")
            target_msg_id: Optional[int] = None
            if raw_target_msg_id is not None and not isinstance(raw_target_msg_id, bool):
                try:
                    parsed_mid = int(raw_target_msg_id)
                    if parsed_mid > 0:
                        target_msg_id = parsed_mid
                except Exception:
                    target_msg_id = None

            prev_item: Optional[dict[str, Any]] = None
            filtered: list[dict[str, Any]] = []
            for item in existing:
                if str(item.get("target_chat_id") or "").strip() == target_chat_id:
                    prev_item = item
                else:
                    filtered.append(item)

            if prev_item is not None:
                try:
                    prev_count = max(1, int(prev_item.get("send_count") or 1))
                except Exception:
                    prev_count = 1
                prev_uid = str(prev_item.get("return_uid") or "").strip()[:64]
                same_uid = bool(return_uid and prev_uid == return_uid)
                should_increment = bool(increment_count and not same_uid)
                send_count = prev_count + 1 if should_increment else prev_count

                prev_mid_raw = prev_item.get("target_msg_id")
                prev_mid: Optional[int] = None
                if prev_mid_raw is not None and not isinstance(prev_mid_raw, bool):
                    try:
                        pm = int(prev_mid_raw)
                        if pm > 0:
                            prev_mid = pm
                    except Exception:
                        prev_mid = None

                if target_msg_id is not None:
                    eff_target_msg_id = target_msg_id
                elif not should_increment:
                    eff_target_msg_id = prev_mid
                else:
                    eff_target_msg_id = None

                eff_title = (raw_title or str(prev_item.get("target_title") or "").strip() or "Штаб")[:160]
                eff_icon = (raw_icon or str(prev_item.get("target_icon") or "").strip() or "🏠")[:16]
                eff_proj = project_name or str(prev_item.get("project_name") or "").strip()[:120]
                if should_increment:
                    eff_sent_at = raw_sent_at or utc_now_iso()
                else:
                    eff_sent_at = raw_sent_at or str(prev_item.get("sent_at") or "").strip()[:64] or utc_now_iso()
                eff_uid = return_uid or prev_uid
            else:
                send_count = 1
                eff_target_msg_id = target_msg_id
                eff_title = (raw_title or "Штаб")[:160]
                eff_icon = (raw_icon or "🏠")[:16]
                eff_proj = project_name
                eff_sent_at = raw_sent_at or utc_now_iso()
                eff_uid = return_uid

            entry_out: dict[str, Any] = {
                "target_chat_id": target_chat_id,
                "target_title": eff_title,
                "target_icon": eff_icon,
                "project_name": eff_proj,
                "target_msg_id": eff_target_msg_id,
                "sent_at": eff_sent_at,
                "send_count": send_count,
            }
            if eff_uid:
                entry_out["return_uid"] = eff_uid

            filtered.append(entry_out)
            hq_returns = filtered[-12:]
            usage["hq_returns"] = hq_returns

            await db.execute(
                "UPDATE messages SET usage_json = ? WHERE id = ? AND chat_id = ?",
                (json.dumps(usage, ensure_ascii=False), int(message_id), clean_chat_id),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        invalidate_chat_list_cache(clean_chat_id)
        _bump_usage_ledger_version(message_id)
        return hq_returns


async def has_message_hq_return_uid(
    src_chat_id: str,
    src_msg_id: int,
    target_chat_id: str,
    return_uid: str,
) -> bool:
    """Return True if `return_uid` was already delivered into `target_chat_id`."""
    clean_uid = str(return_uid or "").strip()[:64]
    clean_target = str(target_chat_id or "").strip()
    clean_src = str(src_chat_id or "").strip()
    if not clean_uid or not clean_target:
        return False
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur_t = await db.execute(
            "SELECT usage_json FROM messages WHERE chat_id = ? AND role = 'user' AND usage_json LIKE ? ORDER BY id DESC LIMIT 20",
            (clean_target, f"%{clean_uid}%"),
        )
        for t_row in await cur_t.fetchall():
            try:
                u_t = json.loads(t_row["usage_json"] or "{}")
            except Exception:
                continue
            if isinstance(u_t, dict):
                hq_s = u_t.get("hq_return_source")
                if isinstance(hq_s, dict) and str(hq_s.get("return_uid") or "").strip() == clean_uid:
                    return True
                if str(u_t.get("hq_return_uid") or "").strip() == clean_uid:
                    return True

        if clean_src and not clean_src.startswith("desktop:") and int(src_msg_id or 0) > 0:
            cur_s = await db.execute(
                "SELECT usage_json FROM messages WHERE id = ? AND chat_id = ?",
                (int(src_msg_id), clean_src),
            )
            s_row = await cur_s.fetchone()
            if s_row:
                try:
                    u_s = json.loads(s_row["usage_json"] or "{}")
                except Exception:
                    u_s = {}
                if isinstance(u_s, dict) and isinstance(u_s.get("hq_returns"), list):
                    for item in u_s["hq_returns"]:
                        if not isinstance(item, dict):
                            continue
                        try:
                            t_mid = int(item.get("target_msg_id") or 0)
                        except (TypeError, ValueError):
                            t_mid = 0
                        if (
                            str(item.get("target_chat_id") or "").strip() == clean_target
                            and str(item.get("return_uid") or "").strip() == clean_uid
                            and t_mid > 0
                        ):
                            return True
    return False


async def cleanup_stale_streaming_messages() -> int:
    """Mark any leftover `status = 'streaming'` messages from a prior server run as interrupted."""
    async with connect_db() as db:
        cur = await db.execute(
            """
            UPDATE messages
            SET status = 'cancelled',
                content = CASE
                    WHEN TRIM(COALESCE(content, '')) = ''
                    THEN '⚠️ *Генерация ответа была прервана при перезапуске сервера.*'
                    ELSE content || char(10) || char(10) || '*(Генерация прервана при перезапуске сервера)*'
                END
            WHERE status = 'streaming'
            """
        )
        await db.commit()
        invalidate_chat_list_cache(None)
        return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# Global Outbound Proxy & Per-Account Route Registry (v106)
# ---------------------------------------------------------------------------

_ALLOWED_GLOBAL_PROXY_MODES = {"system_auto", "custom", "direct"}


def _default_global_proxy_record_from_env() -> dict[str, str]:
    env_hp = os.getenv("HUB_PROXY", "auto").strip()
    env_hp_low = env_hp.lower()
    if not env_hp or env_hp_low == "auto":
        return {"mode": "system_auto", "spec_encrypted": "", "label": ""}
    if env_hp_low in ("none", "off", "false", "direct"):
        return {"mode": "direct", "spec_encrypted": "", "label": ""}
    norm_seed = env_hp if "://" in env_hp else f"http://{env_hp}"
    return {
        "mode": "custom",
        "spec_encrypted": encrypt_scoped_proxy_spec("_global", norm_seed),
        "label": _safe_label_for_proxy_spec(norm_seed),
    }


async def get_global_proxy_settings(*, include_decrypted_proxy: bool = False) -> dict[str, Any]:
    """Read global outbound proxy config from `hub_meta` (`key = 'global_proxy_config'`).
    NEVER exposes `spec_encrypted` or raw `proxy_spec` unless `include_decrypted_proxy=True`."""
    raw_val = ""
    updated_at = ""
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT value, updated_at FROM hub_meta WHERE key = 'global_proxy_config' LIMIT 1"
        )
        row = await cur.fetchone()
        if row:
            raw_val = str(row["value"] or "")
            updated_at = str(row["updated_at"] or "")

    rec: dict[str, Any] = {}
    if raw_val:
        try:
            parsed = json.loads(raw_val)
            if isinstance(parsed, dict):
                rec = parsed
        except Exception:
            rec = {}
    if not rec:
        rec = _default_global_proxy_record_from_env()

    mode = str(rec.get("mode") or "system_auto").strip().lower()
    if mode not in _ALLOWED_GLOBAL_PROXY_MODES:
        mode = "system_auto"
    spec_enc = str(rec.get("spec_encrypted") or "").strip()
    label = str(rec.get("label") or "").strip() if mode == "custom" else ""
    has_custom = bool(mode == "custom" and spec_enc)

    out: dict[str, Any] = {
        "mode": mode,
        "proxy_label": label,
        "custom_label": label,
        "has_custom_proxy": has_custom,
        "has_custom_spec": has_custom,
        "updated_at": updated_at,
    }
    if include_decrypted_proxy:
        if mode == "custom":
            dec = decrypt_scoped_proxy_spec("_global", spec_enc) if spec_enc else ""
            out["proxy_spec"] = dec
            out["spec_corrupted"] = bool(not spec_enc or not dec)
        else:
            out["proxy_spec"] = ""
            out["spec_corrupted"] = False
    return out


async def save_global_proxy_settings(
    *,
    mode: str,
    proxy_spec: Optional[str] = None,
    proxy_label: Optional[str] = None,
) -> dict[str, Any]:
    """Save global proxy mode (`system_auto` | `custom` | `direct`) into `hub_meta` (`global_proxy_config`).
    Preserves existing encrypted custom spec when `mode='custom'` and `proxy_spec` is blank;
    wipes `spec_encrypted` and `label` when switched to `system_auto` or `direct`."""
    clean_mode = str(mode or "").strip().lower()
    if clean_mode not in _ALLOWED_GLOBAL_PROXY_MODES:
        raise ValueError("Недопустимый режим глобального прокси (разрешены 'system_auto', 'custom', 'direct')")

    now = utc_now_iso()
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT value FROM hub_meta WHERE key = 'global_proxy_config' LIMIT 1"
        )
        row = await cur.fetchone()
        existing_rec: dict[str, Any] = {}
        if row and row["value"]:
            try:
                parsed = json.loads(str(row["value"]))
                if isinstance(parsed, dict):
                    existing_rec = parsed
            except Exception:
                existing_rec = {}

        ex_mode = str(existing_rec.get("mode") or "").strip().lower()
        ex_enc = str(existing_rec.get("spec_encrypted") or "").strip()
        ex_lbl = str(existing_rec.get("label") or "").strip()

        if clean_mode in ("system_auto", "direct"):
            new_rec = {"mode": clean_mode, "spec_encrypted": "", "label": ""}
        else:
            clean_spec = str(proxy_spec or "").strip() if proxy_spec is not None else ""
            if clean_spec:
                enc = encrypt_scoped_proxy_spec("_global", clean_spec)
                lbl = _safe_label_for_proxy_spec(clean_spec, proxy_label)
                new_rec = {"mode": "custom", "spec_encrypted": enc, "label": lbl}
            elif ex_mode == "custom" and ex_enc:
                lbl = str(proxy_label).strip()[:160] if proxy_label else ex_lbl
                new_rec = {"mode": "custom", "spec_encrypted": ex_enc, "label": lbl}
            else:
                raise ValueError("Укажите адрес прокси или ссылку Xray для режима «Свой прокси»")

        await db.execute(
            """
            INSERT INTO hub_meta (key, value, updated_at)
            VALUES ('global_proxy_config', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (json.dumps(new_rec, ensure_ascii=False), now),
        )
        await db.commit()

    return await get_global_proxy_settings(include_decrypted_proxy=False)


async def list_all_custom_proxy_routes_for_router() -> dict[str, Any]:
    """Internal backend-only loader for `proxy_router.reload_routes_from_db()`."""
    global_cfg = await get_global_proxy_settings(include_decrypted_proxy=True)
    accounts: list[dict[str, Any]] = []
    active_agy_profile_id = ""

    async with connect_db() as db:
        db.row_factory = aiosqlite.Row

        cur_agy = await db.execute(
            "SELECT id, is_active, is_enabled, proxy_mode, proxy_spec_encrypted, proxy_label FROM account_profiles ORDER BY is_active DESC, updated_at DESC"
        )
        for r in await cur_agy.fetchall():
            pid = str(r["id"] or "").strip()
            if not pid:
                continue
            is_act = bool(int(r["is_active"] or 0))
            if is_act and not active_agy_profile_id:
                active_agy_profile_id = pid
            pm = "custom" if str(r["proxy_mode"] or "global").strip().lower() == "custom" else "global"
            enc = str(r["proxy_spec_encrypted"] or "").strip()
            dec = decrypt_scoped_proxy_spec("agy_admin", enc) if (pm == "custom" and enc) else ""
            accounts.append(
                {
                    "owner_key": f"agy:{pid}",
                    "proxy_mode": pm,
                    "proxy_spec": dec,
                    "proxy_label": str(r["proxy_label"] or "") if pm == "custom" else "",
                    "proxy_spec_corrupted": bool(pm == "custom" and (not enc or not dec)),
                    "is_admin": True,
                    "is_active": is_act,
                    "is_enabled": bool(r["is_enabled"]),
                }
            )

        cur_cgpt = await db.execute(
            """
            SELECT a.id, a.user_id, a.is_active, a.is_enabled,
                   a.proxy_mode, a.proxy_spec_encrypted, a.proxy_label,
                   u.role AS user_role
            FROM user_chatgpt_accounts a
            LEFT JOIN users u ON u.id = a.user_id
            """
        )
        for r in await cur_cgpt.fetchall():
            aid = str(r["id"] or "").strip()
            uid = str(r["user_id"] or "").strip()
            if not aid or not uid:
                continue
            pm = "custom" if str(r["proxy_mode"] or "global").strip().lower() == "custom" else "global"
            enc = str(r["proxy_spec_encrypted"] or "").strip()
            dec = decrypt_scoped_proxy_spec(f"chatgpt:{uid}", enc) if (pm == "custom" and enc) else ""
            is_adm = str(r["user_role"] or "").strip().lower() == "admin" or uid == "admin"
            accounts.append(
                {
                    "owner_key": f"chatgpt:{uid}:{aid}",
                    "proxy_mode": pm,
                    "proxy_spec": dec,
                    "proxy_label": str(r["proxy_label"] or "") if pm == "custom" else "",
                    "proxy_spec_corrupted": bool(pm == "custom" and (not enc or not dec)),
                    "is_admin": is_adm,
                    "is_active": bool(int(r["is_active"] or 0)),
                    "is_enabled": bool(int(r["is_enabled"] if r["is_enabled"] is not None else 1)),
                }
            )

        cur_cld = await db.execute(
            """
            SELECT a.id, a.user_id, a.is_active, a.is_enabled,
                   a.proxy_mode, a.proxy_spec_encrypted, a.proxy_label,
                   u.role AS user_role
            FROM user_claude_accounts a
            LEFT JOIN users u ON u.id = a.user_id
            """
        )
        for r in await cur_cld.fetchall():
            aid = str(r["id"] or "").strip()
            uid = str(r["user_id"] or "").strip()
            if not aid or not uid:
                continue
            pm = "custom" if str(r["proxy_mode"] or "global").strip().lower() == "custom" else "global"
            enc = str(r["proxy_spec_encrypted"] or "").strip()
            dec = decrypt_scoped_proxy_spec(f"claude:{uid}", enc) if (pm == "custom" and enc) else ""
            is_adm = str(r["user_role"] or "").strip().lower() == "admin" or uid == "admin"
            accounts.append(
                {
                    "owner_key": f"claude:{uid}:{aid}",
                    "proxy_mode": pm,
                    "proxy_spec": dec,
                    "proxy_label": str(r["proxy_label"] or "") if pm == "custom" else "",
                    "proxy_spec_corrupted": bool(pm == "custom" and (not enc or not dec)),
                    "is_admin": is_adm,
                    "is_active": bool(int(r["is_active"] or 0)),
                    "is_enabled": bool(int(r["is_enabled"] if r["is_enabled"] is not None else 1)),
                }
            )

    return {
        "global": global_cfg,
        "active_agy_profile_id": active_agy_profile_id,
        "accounts": accounts,
    }


# ---------------------------------------------------------------------------
# Admin Google Account Profiles (Email / Label / Plan Note / Proxy Route)
# ---------------------------------------------------------------------------


def _row_to_profile(row: aiosqlite.Row, *, include_decrypted_proxy: bool = False, include_decrypted_credentials: bool = False) -> dict[str, Any]:
    d = dict(row)
    cred = d.pop("credentials_json_encrypted", "")
    d["has_credentials"] = bool(cred)
    if include_decrypted_credentials:
        d["credentials_json"] = decrypt_agy_credentials_json(cred)
    for key in ("is_enabled", "auto_failover"):
        d[key] = bool(d.get(key, 1))
    for key, default in (("quotas", {}), ("models", [])):
        try:
            d[key] = json.loads(d.pop("cached_" + key + "_json", "") or json.dumps(default))
        except (ValueError, TypeError):
            d[key] = default
    enc = str(d.pop("proxy_spec_encrypted", "") or "").strip()
    p_mode = "custom" if str(d.get("proxy_mode") or "global").strip().lower() == "custom" else "global"
    p_lbl = str(d.get("proxy_label") or "") if p_mode == "custom" else ""
    d["is_active"] = bool(d.get("is_active", 0))
    d["proxy_mode"] = p_mode
    d["proxy_label"] = p_lbl
    d["has_custom_proxy"] = bool(p_mode == "custom" and enc)
    d["has_proxy_spec"] = bool(p_mode == "custom" and enc)
    if include_decrypted_proxy:
        if p_mode == "custom":
            dec = decrypt_scoped_proxy_spec("agy_admin", enc) if enc else ""
            d["proxy_spec"] = dec
            d["proxy_spec_corrupted"] = bool(not enc or not dec)
        else:
            d["proxy_spec"] = ""
            d["proxy_spec_corrupted"] = False
    return d


async def list_account_profiles(*, include_decrypted_proxy: bool = False) -> list[dict[str, Any]]:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM account_profiles ORDER BY sort_order ASC, created_at ASC, updated_at DESC"
        )
        rows = await cur.fetchall()
        return [_row_to_profile(r, include_decrypted_proxy=include_decrypted_proxy) for r in rows]


async def get_account_profile_by_id(
    profile_id: str,
    *,
    include_decrypted_proxy: bool = False,
    include_decrypted_credentials: bool = False,
) -> Optional[dict[str, Any]]:
    clean_pid = (profile_id or "").strip()
    if not clean_pid:
        return None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM account_profiles WHERE id = ? LIMIT 1", (clean_pid,))
        row = await cur.fetchone()
        return _row_to_profile(row, include_decrypted_proxy=include_decrypted_proxy, include_decrypted_credentials=include_decrypted_credentials) if row else None


async def get_active_account_profile(*, require_enabled: bool = False, include_decrypted_credentials: bool = False, include_decrypted_proxy: bool = False) -> Optional[dict[str, Any]]:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM account_profiles WHERE is_active = 1 AND (? = 0 OR is_enabled = 1) ORDER BY updated_at DESC LIMIT 1", (int(require_enabled),)
        )
        row = await cur.fetchone()
        return _row_to_profile(row, include_decrypted_proxy=include_decrypted_proxy, include_decrypted_credentials=include_decrypted_credentials) if row else None


def encrypt_agy_credentials_json(raw_json: str) -> str:
    return _pbkdf2_fernet_cached(get_or_create_hub_secret(), b"agy_credentials_json:admin", 120_000).encrypt(raw_json.encode()).decode()


def decrypt_agy_credentials_json(token: str) -> str:
    try:
        return _pbkdf2_fernet_cached(get_or_create_hub_secret(), b"agy_credentials_json:admin", 120_000).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


async def upsert_agy_account_from_credentials(raw_json: str, *, email: str = "", label=None, auth_method="cockpit_import", make_active=None, **route):
    profiles = await list_account_profiles()
    match = next((p for p in profiles if email and '*' not in email and p['email'].lower() == email.lower()), None)
    if not match and auth_method == 'local_cli':
        legacy = [p for p in profiles if not p['has_credentials'] and p.get('auth_method') == 'local_cli']
        match = legacy[0] if len(legacy) == 1 else None
    saved = await save_account_profile(profile_id=match['id'] if match else 'agy_' + uuid.uuid4().hex[:6],
        email=email or None, label=label, make_active=make_active if make_active is not None else (None if match else not profiles), **route)
    async with connect_db() as db:
        await db.execute("UPDATE account_profiles SET credentials_json_encrypted=?, auth_method=?, sort_order=?, created_at=CASE WHEN created_at='' THEN ? ELSE created_at END, auto_failover=? WHERE id=?",
            (encrypt_agy_credentials_json(raw_json), auth_method, match['sort_order'] if match else max([p['sort_order'] for p in profiles], default=0)+1,
             datetime.now(timezone.utc).isoformat(), int(profiles[0]['auto_failover']) if profiles else 1, saved['id']))
        await db.commit()
    return await get_account_profile_by_id(saved['id'])


async def sync_agy_account_credentials_json(profile_id, raw_json):
    async with connect_db() as db:
        await db.execute("UPDATE account_profiles SET credentials_json_encrypted=? WHERE id=?", (encrypt_agy_credentials_json(raw_json), profile_id))
        await db.commit()


async def update_agy_account_telemetry(profile_id, *, quotas=None, models=None, email=None, plan_note=None, last_error=None):
    values = {"cached_at": datetime.now(timezone.utc).isoformat()}
    for k, v in (("cached_quotas_json", quotas), ("cached_models_json", models)):
        if v is not None:
            values[k] = json.dumps(v, ensure_ascii=False)
    for k, v in (("email", email), ("plan_note", plan_note), ("last_error", last_error)):
        if v is not None:
            values[k] = v
    async with connect_db() as db:
        await db.execute('UPDATE account_profiles SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=?', (*values.values(), profile_id))
        await db.commit()


async def patch_account_profile(profile_id, *, email=None, label=None, plan_note=None, is_enabled=None, auto_failover=None, proxy_mode=None, proxy_spec=None, proxy_label=None):
    if not await get_account_profile_by_id(profile_id):
        return None
    await save_account_profile(profile_id=profile_id, email=email, label=label, plan_note=plan_note, make_active=None,
        proxy_mode=proxy_mode, proxy_spec=proxy_spec, proxy_label=proxy_label)
    async with connect_db() as db:
        if auto_failover is not None:
            await db.execute('UPDATE account_profiles SET auto_failover=?', (int(auto_failover),))
        if is_enabled is not None:
            await db.execute('UPDATE account_profiles SET is_enabled=? WHERE id=?', (int(is_enabled), profile_id))
            if not is_enabled:
                cur = await db.execute('SELECT is_active FROM account_profiles WHERE id=?', (profile_id,))
                active = (await cur.fetchone())[0]
                cur = await db.execute('SELECT 1 FROM account_profiles WHERE is_enabled=1 LIMIT 1')
                if active and await cur.fetchone():
                    await db.execute('UPDATE account_profiles SET is_active=0')
                    await db.execute('UPDATE account_profiles SET is_active=1 WHERE id=(SELECT id FROM account_profiles WHERE is_enabled=1 ORDER BY sort_order,created_at LIMIT 1)')
        await db.commit()
    return await get_account_profile_by_id(profile_id)


async def get_agy_accounts_summary():
    profiles = await list_account_profiles()
    enabled = [p for p in profiles if p['is_enabled']]
    active = next((p for p in profiles if p['is_active']), {})
    return dict(has_account=bool(profiles), account_count=len(profiles), enabled_count=len(enabled),
        token_backed_count=sum(p['has_credentials'] for p in profiles), active_account_id=active.get('id', ''),
        active_index=next((i+1 for i,p in enumerate(enabled) if p['is_active']), 0),
        auto_failover=active.get('auto_failover', profiles[0]['auto_failover'] if profiles else True),
        accounts_brief=[{k:p[k] for k in ('id','label','email','is_enabled','is_active','has_credentials')} for p in profiles])


async def save_account_profile(
    *,
    profile_id: Optional[str] = None,
    email: Optional[str] = None,
    label: Optional[str] = None,
    plan_note: Optional[str] = None,
    fingerprint: Optional[str] = None,
    make_active: Optional[bool] = True,
    proxy_mode: Optional[str] = None,
    proxy_spec: Optional[str] = None,
    proxy_label: Optional[str] = None,
) -> dict[str, Any]:
    pid = (profile_id or "").strip() or str(uuid.uuid4())
    now = utc_now_iso()

    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM account_profiles WHERE id = ? LIMIT 1", (pid,))
        exists = await cur.fetchone()
        ex_dict = dict(exists) if exists else None

        p_mode, p_enc, p_lbl = _prepare_account_proxy_columns(
            scope_id="agy_admin",
            existing_row=ex_dict,
            proxy_mode=proxy_mode,
            proxy_spec=proxy_spec,
            proxy_label=proxy_label,
        )

        if exists:
            eff_email = email.strip()[:120] if email is not None and email.strip() else str(exists["email"] or "")
            eff_label = label.strip()[:80] if label is not None else str(exists["label"] or "")
            eff_plan = plan_note.strip()[:80] if plan_note is not None else str(exists["plan_note"] or "")
            eff_fp = fingerprint if fingerprint is not None else str(exists["fingerprint"] or "")
            if make_active is None:
                eff_active = bool(int(exists["is_active"] or 0))
            else:
                eff_active = bool(make_active)
            if eff_active:
                await db.execute("UPDATE account_profiles SET is_active = 0;")
            await db.execute(
                """
                UPDATE account_profiles
                SET email = ?, label = ?, plan_note = ?, fingerprint = ?, is_active = ?,
                    proxy_mode = ?, proxy_spec_encrypted = ?, proxy_label = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    eff_email,
                    eff_label,
                    eff_plan,
                    eff_fp,
                    1 if eff_active else 0,
                    p_mode,
                    p_enc,
                    p_lbl,
                    now,
                    pid,
                ),
            )
        else:
            clean_email = (email or "").strip()[:120]
            if not clean_email:
                raise ValueError("Укажите email профиля аккаунта")
            clean_label = (label or "").strip()[:80]
            clean_plan = (plan_note or "").strip()[:80]
            clean_fp = fingerprint or ""
            eff_active = True if make_active is None else bool(make_active)
            if eff_active:
                await db.execute("UPDATE account_profiles SET is_active = 0;")
            await db.execute(
                """
                INSERT INTO account_profiles (
                    id, email, label, plan_note, fingerprint, is_active,
                    proxy_mode, proxy_spec_encrypted, proxy_label, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    clean_email,
                    clean_label,
                    clean_plan,
                    clean_fp,
                    1 if eff_active else 0,
                    p_mode,
                    p_enc,
                    p_lbl,
                    now,
                ),
            )
        await db.commit()
        cur2 = await db.execute("SELECT * FROM account_profiles WHERE id = ?", (pid,))
        row = await cur2.fetchone()
        return _row_to_profile(row)


async def activate_account_profile(profile_id: str, fingerprint: str = "") -> Optional[dict[str, Any]]:
    now = utc_now_iso()
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM account_profiles WHERE id = ? AND is_enabled = 1", (profile_id,))
        if not await cur.fetchone():
            return None
        await db.execute("UPDATE account_profiles SET is_active = 0;")
        if fingerprint:
            await db.execute(
                "UPDATE account_profiles SET is_active = 1, fingerprint = ?, updated_at = ? WHERE id = ?",
                (fingerprint, now, profile_id),
            )
        else:
            await db.execute(
                "UPDATE account_profiles SET is_active = 1, updated_at = ? WHERE id = ?",
                (now, profile_id),
            )
        await db.commit()
        cur2 = await db.execute("SELECT * FROM account_profiles WHERE id = ?", (profile_id,))
        row = await cur2.fetchone()
        return _row_to_profile(row) if row else None


async def delete_account_profile(profile_id: str) -> bool:
    async with connect_db() as db:
        cur = await db.execute("DELETE FROM account_profiles WHERE id = ?", (profile_id,))
        await db.execute("UPDATE chats SET agy_conversation_id='', agy_account_id='' WHERE agy_account_id=?", (profile_id,))
        await db.execute("UPDATE chats SET preferred_agy_account_id='' WHERE preferred_agy_account_id=?", (profile_id,))
        # If active was deleted and another profile exists, promote the latest one
        cur_act = await db.execute("SELECT id FROM account_profiles WHERE is_active = 1 LIMIT 1")
        if not await cur_act.fetchone():
            await db.execute(
                """
                UPDATE account_profiles
                SET is_active = 1
                WHERE id = (SELECT id FROM account_profiles WHERE is_enabled=1 ORDER BY sort_order, created_at LIMIT 1)
                """
            )
        await db.commit()
        invalidate_chat_list_cache()
        if cur.rowcount > 0:
            from .config import cleanup_agy_account_dirs
            cleanup_agy_account_dirs(profile_id)
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Token Usage Telemetry & Aggregations
# ---------------------------------------------------------------------------


def _empty_token_bucket() -> dict[str, int]:
    return {
        "turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
    }


def _accumulate_bucket(bucket: dict[str, int], u: dict[str, Any]) -> None:
    inp = int(u.get("input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    thk = int(u.get("thinking_tokens") or 0)
    cch = int(u.get("cache_read_tokens") or 0)
    tot = int(u.get("total_tokens") or 0)
    if tot <= 0 and (inp or out or thk or cch):
        tot = inp + out + thk + cch
    if tot <= 0:
        return
    bucket["turns"] += 1
    bucket["input_tokens"] += inp
    bucket["output_tokens"] += out
    bucket["thinking_tokens"] += thk
    bucket["cache_read_tokens"] += cch
    bucket["total_tokens"] += tot


def _parse_iso_utc(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# v110: единый in-memory ledger расхода токенов (вместо трёх Full Table Scan
# `messages` + json.loads на каждый опрос /api/quotas и /api/system/info).
# Запись: (chat_id, user_id, model, usage_dict, created_dt, total_tokens).
# ---------------------------------------------------------------------------
_USAGE_LEDGER_VERSION = 0
_USAGE_LEDGER_MAX_AGE_SEC = 60.0
_USAGE_LEDGER_CACHE: dict[str, Any] = {
    "path": "",
    "version": -1,
    "rows": [],
    "rows_by_id": {},
    "last_id": 0,
    "dirty": set(),
    "evicted_chats": set(),
}


def _bump_usage_ledger_version(message_id: Optional[int] = None) -> None:
    global _USAGE_LEDGER_VERSION
    _USAGE_LEDGER_VERSION += 1
    if message_id is not None:
        _USAGE_LEDGER_CACHE["dirty"].add(int(message_id))


def _reset_usage_ledger_cache() -> None:
    _bump_usage_ledger_version()
    _USAGE_LEDGER_CACHE.update(
        path="",
        version=-1,
        rows=[],
        rows_by_id={},
        last_id=0,
        dirty=set(),
        evicted_chats=set(),
    )


def _usage_total_tokens(u: dict[str, Any]) -> int:
    return int(u.get("total_tokens") or 0) or (
        int(u.get("input_tokens") or 0)
        + int(u.get("output_tokens") or 0)
        + int(u.get("thinking_tokens") or 0)
        + int(u.get("cache_read_tokens") or 0)
    )


_USAGE_LEDGER_LOCK: Optional[asyncio.Lock] = None
_USAGE_LEDGER_LOCK_LOOP: Optional[asyncio.AbstractEventLoop] = None


def _get_usage_ledger_lock() -> asyncio.Lock:
    global _USAGE_LEDGER_LOCK, _USAGE_LEDGER_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _USAGE_LEDGER_LOCK is None or _USAGE_LEDGER_LOCK_LOOP is not loop:
        _USAGE_LEDGER_LOCK = asyncio.Lock()
        _USAGE_LEDGER_LOCK_LOOP = loop
    return _USAGE_LEDGER_LOCK


async def _get_usage_ledger():
    async with _get_usage_ledger_lock():
        return await _get_usage_ledger_unlocked()


async def _get_usage_ledger_unlocked() -> list[tuple[str, str, str, dict[str, Any], Optional[datetime], int]]:
    cache = _USAGE_LEDGER_CACHE
    path = _resolved_db_path()
    if cache["path"] != path:
        _reset_usage_ledger_cache()
        cache["path"] = path
    if cache["version"] == _USAGE_LEDGER_VERSION:
        return cache["rows"]
    version = _USAGE_LEDGER_VERSION
    dirty = set(cache["dirty"])
    cache["dirty"].difference_update(dirty)
    evicted_chats = set(cache.setdefault("evicted_chats", set()))
    cache["evicted_chats"].difference_update(evicted_chats)
    prev_last_id = int(cache["last_id"] or 0)
    dirty_existing = sorted(mid for mid in dirty if mid <= prev_last_id)
    raw_rows: list[Any] = []
    try:
        async with connect_db() as db:
            cur = await db.execute(
                "SELECT id, chat_id, user_id, model, usage_json, created_at "
                "FROM messages WHERE role = 'assistant' AND id > ? ORDER BY id",
                (prev_last_id,),
            )
            raw_rows.extend(await cur.fetchall())
            for idx in range(0, len(dirty_existing), 400):
                batch = dirty_existing[idx : idx + 400]
                placeholders = ",".join("?" for _ in batch)
                cur_d = await db.execute(
                    f"SELECT id, chat_id, user_id, model, usage_json, created_at "
                    f"FROM messages WHERE role = 'assistant' AND id IN ({placeholders})",
                    batch,
                )
                raw_rows.extend(await cur_d.fetchall())
    except BaseException:
        cache["dirty"].update(dirty)
        cache["evicted_chats"].update(evicted_chats)
        raise
    if evicted_chats:
        for mid in [k for k, v in cache["rows_by_id"].items() if v[0] in evicted_chats]:
            cache["rows_by_id"].pop(mid, None)
    for mid in dirty_existing:
        cache["rows_by_id"].pop(mid, None)
    for mid, chat_id, user_id, model, raw, created_at in raw_rows:
        cache["last_id"] = max(cache["last_id"], mid)
        cache["rows_by_id"].pop(mid, None)
        try:
            usage = json.loads(raw or "{}")
            if not isinstance(usage, dict):
                continue
            total = _usage_total_tokens(usage)
        except (ValueError, TypeError, AttributeError):
            continue
        if total > 0:
            cache["rows_by_id"][mid] = (
                str(chat_id or ""),
                str(user_id or ""),
                str(model or ""),
                usage,
                _parse_iso_utc(str(created_at or "")),
                total,
            )
    cache["rows"] = list(cache["rows_by_id"].values())
    cache["version"] = version
    return cache["rows"]


async def get_user_24h_tokens(user_id: str) -> int:
    """Fast 24h token sum for admin's Google CLI daily limit enforcement.
    Strictly excludes turns executed on the user's own personal ChatGPT (`chatgpt_codex`)
    or personal Anthropic Claude Code (`claude_code`) subscriptions."""
    cut_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    target_uid = str(user_id or "")
    ledger = await _get_usage_ledger()
    total = 0
    for _chat_id, r_user_id, r_model, u, dt, tot in ledger:
        if r_user_id != target_uid:
            continue
        if not dt or dt < cut_24h:
            continue
        m_str = str(r_model or "").strip()
        if m_str.startswith(("codex:", "claude-code:")):
            continue
        if not isinstance(u, dict):
            continue
        if str(u.get("provider") or "").strip().lower() in ("chatgpt_codex", "claude_code"):
            continue
        if tot > 0:
            total += tot
    return total


async def get_token_usage_stats(
    active_chat_id: Optional[str] = None,
    *,
    filter_user_id: Optional[str] = None,
    include_by_user: bool = False,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    cut_5h = now - timedelta(hours=5)
    cut_24h = now - timedelta(hours=24)
    cut_7d = now - timedelta(days=7)

    win_5h = _empty_token_bucket()
    win_24h = _empty_token_bucket()
    win_7d = _empty_token_bucket()
    all_time = _empty_token_bucket()
    active_chat_bucket = _empty_token_bucket()

    by_model_map: dict[str, dict[str, Any]] = {}
    by_user_map: dict[str, dict[str, Any]] = {}
    user_meta_map: dict[str, dict[str, Any]] = {}

    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        if include_by_user:
            cur_u = await db.execute(
                "SELECT id, username, display_name, role, is_active, daily_token_limit, quota_reserve_pct FROM users;"
            )
            for urow in await cur_u.fetchall():
                uid_k = str(urow["id"])
                user_meta_map[uid_k] = {
                    "user_id": uid_k,
                    "username": str(urow["username"]),
                    "display_name": str(urow["display_name"] or urow["username"]),
                    "role": str(urow["role"] or "user"),
                    "is_active": bool(urow["is_active"]) if urow["is_active"] is not None else True,
                    "is_orphan": False,
                    "daily_token_limit": int(urow["daily_token_limit"] or 0),
                    "quota_reserve_pct": int(
                        urow["quota_reserve_pct"] if urow["quota_reserve_pct"] is not None else 15
                    ),
                }

    ledger = await _get_usage_ledger()
    filter_uid_str = str(filter_user_id) if filter_user_id else ""
    active_cid_str = str(active_chat_id) if active_chat_id else ""

    for r_chat_id, r_user_id, r_model, u, dt, tot_check in ledger:
        if filter_uid_str and r_user_id != filter_uid_str:
            continue

        _accumulate_bucket(all_time, u)

        in_7d = bool(dt and dt >= cut_7d)
        in_24h = bool(dt and dt >= cut_24h)
        in_5h = bool(dt and dt >= cut_5h)

        if in_7d:
            _accumulate_bucket(win_7d, u)
        if in_24h:
            _accumulate_bucket(win_24h, u)
        if in_5h:
            _accumulate_bucket(win_5h, u)

        if active_cid_str and r_chat_id == active_cid_str:
            _accumulate_bucket(active_chat_bucket, u)

        m_id = (r_model or "unknown").strip() or "unknown"
        if m_id not in by_model_map:
            entry = _empty_token_bucket()
            entry["model"] = m_id
            entry["tokens_24h"] = 0
            entry["tokens_7d"] = 0
            by_model_map[m_id] = entry
        m_entry = by_model_map[m_id]
        _accumulate_bucket(m_entry, u)
        if in_24h:
            m_entry["tokens_24h"] += tot_check
        if in_7d:
            m_entry["tokens_7d"] += tot_check

        if include_by_user:
            uid_val = (r_user_id or "admin").strip() or "admin"
            if uid_val not in by_user_map:
                meta = user_meta_map.get(
                    uid_val,
                    {
                        "user_id": uid_val,
                        "username": uid_val,
                        "display_name": uid_val,
                        "role": "user",
                        "is_active": True,
                        "is_orphan": True,
                        "daily_token_limit": 0,
                        "quota_reserve_pct": 15,
                    },
                )
                u_entry = _empty_token_bucket()
                u_entry.update(meta)
                u_entry["tokens_5h"] = 0
                u_entry["tokens_24h"] = 0
                u_entry["shared_tokens_24h"] = 0
                u_entry["tokens_7d"] = 0
                by_user_map[uid_val] = u_entry
            u_entry = by_user_map[uid_val]
            _accumulate_bucket(u_entry, u)
            if in_5h:
                u_entry["tokens_5h"] += tot_check
            if in_24h:
                u_entry["tokens_24h"] += tot_check
                if not m_id.lower().startswith(("codex:", "claude-code:")) and u.get("provider") not in ("chatgpt_codex", "claude_code"):
                    u_entry["shared_tokens_24h"] += tot_check
            if in_7d:
                u_entry["tokens_7d"] += tot_check

    by_model_list = sorted(
        by_model_map.values(),
        key=lambda x: (x["total_tokens"], x["turns"]),
        reverse=True,
    )

    out: dict[str, Any] = {
        "window_5h": win_5h,
        "window_24h": win_24h,
        "window_7d": win_7d,
        "all_time": all_time,
        "active_chat": active_chat_bucket if active_chat_id else None,
        "by_model": by_model_list,
    }

    if include_by_user:
        # Ensure all registered users appear even with 0 tokens
        for uid_k, meta in user_meta_map.items():
            if uid_k not in by_user_map:
                u_entry = _empty_token_bucket()
                u_entry.update(meta)
                u_entry["tokens_5h"] = 0
                u_entry["tokens_24h"] = 0
                u_entry["shared_tokens_24h"] = 0
                u_entry["tokens_7d"] = 0
                by_user_map[uid_k] = u_entry
        out["by_user"] = sorted(
            by_user_map.values(),
            key=lambda x: (
                0 if x.get("role") == "admin" else 1,
                0 if x.get("is_active", True) else 1,
                -int(x.get("tokens_24h") or 0),
                -int(x.get("total_tokens") or 0),
                str(x.get("username") or "").lower(),
            ),
        )

    return out


# ---------------------------------------------------------------------------
# Project Brain (v58): Passport, Rules Sync, Auto-Changelog & @Chat Digests
# ---------------------------------------------------------------------------

MAX_PASSPORT_OVERVIEW_CHARS = 2500
MAX_PASSPORT_RULES_CHARS = 3000
MAX_PASSPORT_FILE_MAP_CHARS = 3000
MAX_PROJECT_CHANGELOG_ITEMS = 25
MAX_REFERENCED_CHAT_CHARS = 5000
MAX_REFERENCED_CHAT_FULL_CHARS = 100_000
MAX_REFERENCED_CHAT_LAST_CHARS = 100_000
MAX_REFERENCED_CHAT_SUMMARY_CHARS = 4_800
VALID_REFERENCED_CHAT_SCOPES = ("full", "last", "summary")

_CTX_TAG_STRIP_RE = re.compile(
    r"<(?:CONTEXT_SUMMARY|PROJECT_PULSE|REFERENCED_CHAT|TASK_PROFILE|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)\b[^>]*>[\s\S]*?</(?:CONTEXT_SUMMARY|PROJECT_PULSE|REFERENCED_CHAT|TASK_PROFILE|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>"
    r"|</?USER_REQUEST>",
    re.IGNORECASE,
)


def build_project_memory_scope_key(
    project_name: str,
    owner_id: str = "admin",
    *,
    is_shared: bool = False,
) -> str:
    clean_proj = (project_name or "").strip().lower()
    if is_shared:
        return f"shared:{clean_proj}"
    clean_owner = (owner_id or "admin").strip() or "admin"
    return f"user:{clean_owner}:{clean_proj}"


def _clean_single_line(text: str, max_len: int = 160) -> str:
    flat = re.sub(r"\s+", " ", (text or "").strip())
    if len(flat) <= max_len:
        return flat
    return flat[: max(1, max_len - 1)].rstrip() + "…"


def _normalize_changelog_entry(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    summary = _clean_single_line(str(raw.get("summary") or ""), 220)
    if not summary:
        return None
    raw_files = raw.get("files")
    files_out: list[str] = []
    if isinstance(raw_files, list):
        seen_f: set[str] = set()
        for item in raw_files:
            f_str = str(item or "").strip().replace("\\", "/")
            if not f_str:
                continue
            f_low = f_str.lower()
            if f_low in seen_f:
                continue
            seen_f.add(f_low)
            files_out.append(f_str[:120])
            if len(files_out) >= 6:
                break
    entry: dict[str, Any] = {
        "id": str(raw.get("id") or uuid.uuid4().hex[:10]),
        "ts": str(raw.get("ts") or utc_now_iso()),
        "chat_id": str(raw.get("chat_id") or ""),
        "chat_title": _clean_single_line(str(raw.get("chat_title") or "Проект"), 80),
        "files": files_out,
        "summary": summary,
    }
    if raw.get("pinned"):
        entry["pinned"] = True
    return entry


def _normalize_changelog_list(raw_val: Any) -> list[dict[str, Any]]:
    parsed: Any = raw_val
    if isinstance(raw_val, str):
        try:
            parsed = json.loads(raw_val) if raw_val.strip() else []
        except Exception:
            parsed = []
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, Any]] = []
    for item in parsed:
        norm = _normalize_changelog_entry(item)
        if norm:
            out.append(norm)
    # Sort newest first by ISO timestamp so index 0 is always the freshest event,
    # then cap to MAX_PROJECT_CHANGELOG_ITEMS (8).
    out.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    return out[:MAX_PROJECT_CHANGELOG_ITEMS]


def _normalize_project_path_list(raw_val: Any, *, max_items: int = 16) -> list[str]:
    parsed: Any = raw_val
    if isinstance(raw_val, str):
        try:
            parsed = json.loads(raw_val) if raw_val.strip() else []
        except Exception:
            parsed = []
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        s = str(item or "").strip()
        if not s or "\x00" in s:
            continue
        key = s.replace("/", "\\").rstrip("\\").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(s[:512])
        if len(out) >= max_items:
            break
    return out


def _row_to_project_memory(
    row: Optional[aiosqlite.Row],
    *,
    scope_key: str,
    project_name: str = "",
    owner_id: str = "admin",
) -> dict[str, Any]:
    if row is None:
        return {
            "scope_key": scope_key,
            "owner_id": owner_id or "admin",
            "project_name": project_name,
            "passport_overview": "",
            "passport_rules": "",
            "passport_file_map": "",
            "changelog": [],
            "changelog_count": 0,
            "extra_dirs": [],
            "migrated_from_paths": [],
            "disable_default_guardrails": False,
            "has_passport": False,
            "updated_at": "",
        }
    d = dict(row)
    overview = str(d.get("passport_overview") or "")[:MAX_PASSPORT_OVERVIEW_CHARS]
    rules = str(d.get("passport_rules") or "")[:MAX_PASSPORT_RULES_CHARS]
    file_map = str(d.get("passport_file_map") or "")[:MAX_PASSPORT_FILE_MAP_CHARS]
    changelog = _normalize_changelog_list(d.get("changelog_json", "[]"))
    extra_dirs = _normalize_project_path_list(d.get("extra_dirs_json", "[]"), max_items=16)
    migrated_from_paths = _normalize_project_path_list(
        d.get("migrated_from_paths_json", "[]"), max_items=32
    )
    disable_default_guardrails = bool(int(d.get("disable_default_guardrails") or 0))
    has_passport = bool(overview.strip() or rules.strip() or file_map.strip())
    return {
        "scope_key": str(d.get("scope_key") or scope_key),
        "owner_id": str(d.get("owner_id") or owner_id or "admin"),
        "project_name": str(d.get("project_name") or project_name),
        "passport_overview": overview,
        "passport_rules": rules,
        "passport_file_map": file_map,
        "changelog": changelog,
        "changelog_count": len(changelog),
        "extra_dirs": extra_dirs,
        "migrated_from_paths": migrated_from_paths,
        "disable_default_guardrails": disable_default_guardrails,
        "has_passport": has_passport,
        "updated_at": str(d.get("updated_at") or ""),
    }


async def get_project_memory(
    scope_key: str,
    *,
    project_name: str = "",
    owner_id: str = "admin",
) -> dict[str, Any]:
    if not scope_key:
        return _row_to_project_memory(
            None, scope_key="", project_name=project_name, owner_id=owner_id
        )
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM project_memories WHERE scope_key = ? LIMIT 1",
            (scope_key,),
        )
        row = await cur.fetchone()
        return _row_to_project_memory(
            row, scope_key=scope_key, project_name=project_name, owner_id=owner_id
        )


async def get_project_memories_summary_map(
    scope_keys: list[str],
) -> dict[str, dict[str, Any]]:
    clean_keys = list({k.strip() for k in (scope_keys or []) if k and k.strip()})
    if not clean_keys:
        return {}
    placeholders = ", ".join("?" for _ in clean_keys)
    out: dict[str, dict[str, Any]] = {}
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""
            SELECT *
            FROM project_memories
            WHERE scope_key IN ({placeholders})
            """,
            clean_keys,
        )
        rows = await cur.fetchall()
    for r in rows:
        rd = dict(r)
        sk = str(rd.get("scope_key") or "")
        ov = str(rd.get("passport_overview") or "").strip()
        ru = str(rd.get("passport_rules") or "").strip()
        fm = str(rd.get("passport_file_map") or "").strip()
        cl = _normalize_changelog_list(rd.get("changelog_json"))
        ed = _normalize_project_path_list(rd.get("extra_dirs_json"), max_items=16)
        mp = _normalize_project_path_list(rd.get("migrated_from_paths_json"), max_items=32)
        out[sk] = {
            "has_passport": bool(ov or ru or fm),
            "changelog_count": len(cl),
            "extra_dirs": ed,
            "extra_dirs_count": len(ed),
            "migrated_from_paths": mp,
            "updated_at": str(rd.get("updated_at") or ""),
        }
    return out


def _sync_project_context_mirror(
    memory: dict[str, Any],
    *,
    owner_username: str = "",
    user_slug_by_id: Optional[dict[str, str]] = None,
) -> Optional[Path]:
    """Write human-readable `<ProjectName>.md` mirror into `users/<owner_slug>/context/` or `shared_projects/_context/`."""
    try:
        proj_name = str(memory.get("project_name") or "").strip()
        if not proj_name:
            return None
        safe_file_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", proj_name).strip(" .")[:80] or "Project"
        scope_key = str(memory.get("scope_key") or "").strip()
        owner_id = str(memory.get("owner_id") or "admin").strip() or "admin"
        is_shared = scope_key.startswith("shared:")

        if is_shared:
            ctx_dir = (_eff_shared_projects_dir() / "_context").resolve()
            ctx_dir.mkdir(parents=True, exist_ok=True)
        else:
            if owner_username:
                slug = config.sanitize_user_folder_slug(owner_username)
            elif user_slug_by_id and owner_id in user_slug_by_id:
                slug = user_slug_by_id[owner_id]
            else:
                slug = config.sanitize_user_folder_slug(owner_id)
            ctx_dir = _ensure_user_space(slug)["context"]

        target_md = (ctx_dir / f"{safe_file_stem}.md").resolve()
        overview = str(memory.get("passport_overview") or "").strip()
        rules = str(memory.get("passport_rules") or "").strip()
        file_map = str(memory.get("passport_file_map") or "").strip()
        changelog = list(memory.get("changelog") or [])[:25]

        sections: list[str] = [
            f"# Паспорт и контекст проекта: {proj_name}",
            f"_Обновлено: {memory.get('updated_at') or utc_now_iso()}_",
        ]
        if overview:
            sections.append(f"## Обзор проекта\n\n{overview}")
        if rules:
            sections.append(f"## Правила и ограничения\n\n{rules}")
        if file_map:
            sections.append(f"## Карта файлов и модулей\n\n{file_map}")
        if changelog:
            pulse_lines: list[str] = ["## Последние записи пульса (до 25)"]
            for item in changelog:
                if not isinstance(item, dict):
                    continue
                ts_s = str(item.get("ts") or "")[:19].replace("T", " ")
                ctitle = str(item.get("chat_title") or "Проект").strip()
                pin_mark = "📌 " if item.get("pinned") else ""
                summ = str(item.get("summary") or "").strip()
                flist = [str(f) for f in (item.get("files") or []) if str(f).strip()]
                files_tail = f" _(файлы: `{', '.join(flist)}`)_" if flist else ""
                pulse_lines.append(f"- **[{ts_s}] {pin_mark}{ctitle}:** {summ}{files_tail}")
            sections.append("\n".join(pulse_lines))
        if not (overview or rules or file_map or changelog):
            sections.append("_(Паспорт и пульс проекта пока пусты.)_")

        rendered = "\n\n".join(sections).strip() + "\n"
        config._atomic_sync_text_file(target_md, rendered)
        return target_md
    except Exception:
        return None


async def save_project_memory(
    scope_key: str,
    *,
    owner_id: str = "admin",
    project_name: str,
    passport_overview: Optional[str] = None,
    passport_rules: Optional[str] = None,
    passport_file_map: Optional[str] = None,
    changelog: Optional[list[dict[str, Any]]] = None,
    extra_dirs: Optional[list[str]] = None,
    migrated_from_paths: Optional[list[str]] = None,
    disable_default_guardrails: Optional[bool] = None,
) -> dict[str, Any]:
    now = utc_now_iso()
    clean_owner = (owner_id or "admin").strip() or "admin"
    clean_proj = (project_name or "").strip()
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM project_memories WHERE scope_key = ? LIMIT 1",
            (scope_key,),
        )
        existing_row = await cur.fetchone()
        existing = _row_to_project_memory(
            existing_row,
            scope_key=scope_key,
            project_name=clean_proj,
            owner_id=clean_owner,
        )

        new_overview = (
            str(passport_overview).strip()[:MAX_PASSPORT_OVERVIEW_CHARS]
            if passport_overview is not None
            else existing["passport_overview"]
        )
        new_rules = (
            str(passport_rules).strip()[:MAX_PASSPORT_RULES_CHARS]
            if passport_rules is not None
            else existing["passport_rules"]
        )
        new_file_map = (
            str(passport_file_map).strip()[:MAX_PASSPORT_FILE_MAP_CHARS]
            if passport_file_map is not None
            else existing["passport_file_map"]
        )
        new_changelog = (
            _normalize_changelog_list(changelog)
            if changelog is not None
            else existing["changelog"]
        )
        new_extra_dirs = (
            _normalize_project_path_list(extra_dirs, max_items=16)
            if extra_dirs is not None
            else existing["extra_dirs"]
        )
        new_migrated_paths = (
            _normalize_project_path_list(migrated_from_paths, max_items=32)
            if migrated_from_paths is not None
            else existing["migrated_from_paths"]
        )
        new_disable_guardrails = (
            int(bool(disable_default_guardrails))
            if disable_default_guardrails is not None
            else int(bool(existing.get("disable_default_guardrails")))
        )

        await db.execute(
            """
            INSERT INTO project_memories (
                scope_key, owner_id, project_name,
                passport_overview, passport_rules, passport_file_map,
                changelog_json, extra_dirs_json, migrated_from_paths_json,
                disable_default_guardrails, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_key) DO UPDATE SET
                owner_id = excluded.owner_id,
                project_name = excluded.project_name,
                passport_overview = excluded.passport_overview,
                passport_rules = excluded.passport_rules,
                passport_file_map = excluded.passport_file_map,
                changelog_json = excluded.changelog_json,
                extra_dirs_json = excluded.extra_dirs_json,
                migrated_from_paths_json = excluded.migrated_from_paths_json,
                disable_default_guardrails = excluded.disable_default_guardrails,
                updated_at = excluded.updated_at
            """,
            (
                scope_key,
                clean_owner,
                clean_proj,
                new_overview,
                new_rules,
                new_file_map,
                json.dumps(new_changelog, ensure_ascii=False),
                json.dumps(new_extra_dirs, ensure_ascii=False),
                json.dumps(new_migrated_paths, ensure_ascii=False),
                new_disable_guardrails,
                now,
            ),
        )
        await db.commit()
        cur2 = await db.execute(
            "SELECT * FROM project_memories WHERE scope_key = ? LIMIT 1",
            (scope_key,),
        )
        saved_row = await cur2.fetchone()
        cur_u_name = await db.execute("SELECT username FROM users WHERE id = ? LIMIT 1", (clean_owner,))
        u_name_row = await cur_u_name.fetchone()
        owner_uname = str(u_name_row[0] or "") if u_name_row else ""
        saved_mem = _row_to_project_memory(
            saved_row,
            scope_key=scope_key,
            project_name=clean_proj,
            owner_id=clean_owner,
        )
    _sync_project_context_mirror(saved_mem, owner_username=owner_uname)
    return saved_mem


def get_project_migrated_paths_sync(project_name: str) -> list[str]:
    """Read-only synchronous lookup of migrated_from_paths for Desktop conversation matching."""
    clean_proj = (project_name or "").strip()
    if not clean_proj or not DB_PATH.exists():
        return []
    try:
        uri = f"file:{DB_PATH.resolve().as_posix()}?mode=ro"
        ro_conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            cur = ro_conn.execute(
                "SELECT migrated_from_paths_json FROM project_memories WHERE LOWER(project_name) = LOWER(?)",
                (clean_proj,),
            )
            rows = cur.fetchall()
        finally:
            ro_conn.close()
    except Exception:
        return []
    combined: list[str] = []
    for r in rows:
        if r and r[0]:
            combined.extend(_normalize_project_path_list(r[0], max_items=32))
    return _normalize_project_path_list(combined, max_items=32)


def get_all_projects_migrated_paths_sync() -> dict[str, list[str]]:
    """v110: пакетная карта `project_name.lower() -> migrated_from_paths` одним read-only запросом
    (вместо отдельного `sqlite3.connect` на каждый проект в `/api/projects`)."""
    if not DB_PATH.exists():
        return {}
    conn: Optional[sqlite3.Connection] = None
    try:
        uri = f"file:{DB_PATH.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        rows = conn.execute(
            "SELECT project_name, migrated_from_paths_json FROM project_memories "
            "WHERE migrated_from_paths_json != '' AND migrated_from_paths_json != '[]'"
        ).fetchall()
    except Exception:
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    grouped: dict[str, list[str]] = {}
    for proj, raw in rows:
        key = str(proj or "").strip().lower()
        if not key or not raw:
            continue
        grouped.setdefault(key, []).extend(_normalize_project_path_list(raw, max_items=32))
    return {k: _normalize_project_path_list(v, max_items=32) for k, v in grouped.items()}


async def append_project_changelog_entry(
    scope_key: str,
    *,
    owner_id: str = "admin",
    project_name: str,
    entry: dict[str, Any],
) -> dict[str, Any]:
    norm = _normalize_changelog_entry(entry)
    if not norm:
        return await get_project_memory(
            scope_key, project_name=project_name, owner_id=owner_id
        )
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM project_memories WHERE scope_key = ? LIMIT 1",
            (scope_key,),
        )
        existing_row = await cur.fetchone()
        existing = _row_to_project_memory(
            existing_row,
            scope_key=scope_key,
            project_name=project_name,
            owner_id=owner_id,
        )
        merged = _normalize_changelog_list([norm, *existing["changelog"]])
        now = utc_now_iso()
        clean_owner = (owner_id or "admin").strip() or "admin"
        clean_proj = (project_name or "").strip()
        await db.execute(
            """
            INSERT INTO project_memories (
                scope_key, owner_id, project_name,
                passport_overview, passport_rules, passport_file_map,
                changelog_json, extra_dirs_json, migrated_from_paths_json,
                disable_default_guardrails, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_key) DO UPDATE SET
                changelog_json = excluded.changelog_json,
                updated_at = excluded.updated_at
            """,
            (
                scope_key,
                clean_owner,
                clean_proj,
                existing["passport_overview"],
                existing["passport_rules"],
                existing["passport_file_map"],
                json.dumps(merged, ensure_ascii=False),
                json.dumps(existing["extra_dirs"], ensure_ascii=False),
                json.dumps(existing["migrated_from_paths"], ensure_ascii=False),
                int(bool(existing.get("disable_default_guardrails"))),
                now,
            ),
        )
        await db.commit()
        cur2 = await db.execute(
            "SELECT * FROM project_memories WHERE scope_key = ? LIMIT 1",
            (scope_key,),
        )
        saved_row = await cur2.fetchone()
        cur_u_name = await db.execute("SELECT username FROM users WHERE id = ? LIMIT 1", (clean_owner,))
        u_name_row = await cur_u_name.fetchone()
        owner_uname = str(u_name_row[0] or "") if u_name_row else ""
        saved_mem = _row_to_project_memory(
            saved_row,
            scope_key=scope_key,
            project_name=clean_proj,
            owner_id=clean_owner,
        )
    _sync_project_context_mirror(saved_mem, owner_username=owner_uname)
    return saved_mem


async def pin_project_memory_note(
    scope_key: str,
    *,
    owner_id: str = "admin",
    project_name: str,
    note: str,
    target: str = "changelog",
    chat_id: str = "",
    chat_title: str = "",
) -> dict[str, Any]:
    clean_note = (note or "").strip()
    if not clean_note:
        return await get_project_memory(
            scope_key, project_name=project_name, owner_id=owner_id
        )
    if target == "rules":
        existing = await get_project_memory(
            scope_key, project_name=project_name, owner_id=owner_id
        )
        bullet_line = re.sub(r"\s+", " ", clean_note)[:420]
        if not bullet_line.startswith(("-", "•", "*")):
            bullet_line = f"- {bullet_line}"
        cur_rules = existing["passport_rules"].rstrip()
        combined = f"{cur_rules}\n{bullet_line}".strip() if cur_rules else bullet_line
        return await save_project_memory(
            scope_key,
            owner_id=owner_id,
            project_name=project_name,
            passport_rules=combined[:MAX_PASSPORT_RULES_CHARS],
        )

    entry = {
        "id": uuid.uuid4().hex[:10],
        "ts": utc_now_iso(),
        "chat_id": chat_id or "",
        "chat_title": (chat_title or "Заметка 📌").strip()[:80],
        "files": [],
        "summary": _clean_single_line(clean_note, 200),
        "pinned": True,
    }
    return await append_project_changelog_entry(
        scope_key,
        owner_id=owner_id,
        project_name=project_name,
        entry=entry,
    )


def _ensure_git_info_exclude_rule(project_dir: Path, rel_rule_path: str) -> None:
    """Idempotently add .agents/rules/hub_project.md to .git/info/exclude without touching .gitignore."""
    try:
        git_info_dir = project_dir / ".git" / "info"
        if not git_info_dir.is_dir():
            return
        exclude_file = git_info_dir / "exclude"
        existing_text = ""
        if exclude_file.exists():
            existing_text = exclude_file.read_text(encoding="utf-8", errors="replace")
        existing_lines = {ln.strip() for ln in existing_text.splitlines()}
        if rel_rule_path in existing_lines:
            return
        prefix = "" if (not existing_text or existing_text.endswith("\n")) else "\n"
        with exclude_file.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{prefix}{rel_rule_path}\n")
    except Exception:
        pass


def sync_project_rules_file(project_dir: Path, memory: dict[str, Any]) -> None:
    """Synchronize project passport & rules into <project_dir>/.agents/rules/hub_project.md."""
    try:
        if not project_dir or not project_dir.exists() or not project_dir.is_dir():
            return
        resolved_dir = project_dir.resolve()
        rules_dir = resolved_dir / ".agents" / "rules"
        target_file = rules_dir / "hub_project.md"

        overview = str(memory.get("passport_overview") or "").strip()
        rules = str(memory.get("passport_rules") or "").strip()
        file_map = str(memory.get("passport_file_map") or "").strip()
        disable_guardrails = bool(memory.get("disable_default_guardrails"))
        proj_name = str(memory.get("project_name") or resolved_dir.name).strip()

        if not (overview or rules or file_map or disable_guardrails):
            if target_file.exists():
                try:
                    target_file.unlink()
                except OSError:
                    pass
                for d_cand in (rules_dir, resolved_dir / ".agents"):
                    try:
                        if d_cand.is_dir() and not any(d_cand.iterdir()):
                            d_cand.rmdir()
                    except OSError:
                        break
            return

        sections: list[str] = [
            f"# Паспорт и правила проекта: {proj_name}",
            "<!-- Автоматически синхронизируется Antigravity Hub (.agents/rules/hub_project.md) -->",
        ]
        if disable_guardrails:
            sections.append("<!-- hub:disable-default-guardrails -->")
        if overview:
            sections.append(f"## Суть, стек и запуск проекта\n{overview}")
        if rules:
            sections.append(f"## Жёсткие правила и ограничения\n{rules}")
        if file_map:
            sections.append(f"## Карта ключевых файлов и модулей\n{file_map}")

        rendered = "\n\n".join(sections).strip() + "\n"
        rules_dir.mkdir(parents=True, exist_ok=True)
        current_disk = ""
        if target_file.exists():
            try:
                current_disk = target_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                current_disk = ""
        if current_disk != rendered:
            target_file.write_text(rendered, encoding="utf-8", newline="\n")

        _ensure_git_info_exclude_rule(resolved_dir, ".agents/rules/hub_project.md")
    except Exception:
        pass


async def get_recent_project_chats_for_passport(
    project_name: str,
    *,
    owner_id: str = "admin",
    is_shared: bool = False,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Fetch up to `limit` recent chats of a project with compacted_summary & bookmark/tail notes."""
    clean_proj = (project_name or "").strip()
    if not clean_proj:
        return []
    cap = max(1, min(int(limit), 10))
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        if is_shared:
            cur = await db.execute(
                """
                SELECT id, title, access_mode, compacted_summary, updated_at
                FROM chats
                WHERE kind = 'project' AND LOWER(project_name) = LOWER(?)
                ORDER BY is_archived ASC, updated_at DESC
                LIMIT ?
                """,
                (clean_proj, cap),
            )
        else:
            cur = await db.execute(
                """
                SELECT id, title, access_mode, compacted_summary, updated_at
                FROM chats
                WHERE kind = 'project' AND LOWER(project_name) = LOWER(?) AND owner_id = ?
                ORDER BY is_archived ASC, updated_at DESC
                LIMIT ?
                """,
                (clean_proj, (owner_id or "admin").strip() or "admin", cap),
            )
        rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            cid = str(r["id"])
            summary = str(r["compacted_summary"] or "").strip()[:1400]
            tail_excerpt = ""
            if not summary:
                cur_m = await db.execute(
                    """
                    SELECT role, content, bookmarked
                    FROM messages
                    WHERE chat_id = ? AND role IN ('user', 'assistant') AND content != ''
                    ORDER BY bookmarked DESC, id DESC
                    LIMIT 3
                    """,
                    (cid,),
                )
                m_rows = await cur_m.fetchall()
                snippets: list[str] = []
                for mr in m_rows:
                    cleaned = _CTX_TAG_STRIP_RE.sub("", str(mr["content"] or "")).strip()
                    if not cleaned:
                        continue
                    bm_tag = "🔖 " if mr["bookmarked"] else ""
                    role_lbl = "User" if mr["role"] == "user" else "AI"
                    snippets.append(f"{bm_tag}{role_lbl}: {_clean_single_line(cleaned, 220)}")
                tail_excerpt = "\n".join(snippets)[:650]
            out.append(
                {
                    "id": cid,
                    "title": str(r["title"] or "Чат"),
                    "access_mode": str(r["access_mode"] or "read_only"),
                    "updated_at": str(r["updated_at"] or ""),
                    "compacted_summary": summary,
                    "tail_excerpt": tail_excerpt,
                }
            )
        return out


_FWD_BLOCK_DB_RE = re.compile(
    r"<FORWARDED_MESSAGES\b[^>]*>([\s\S]*?)</FORWARDED_MESSAGES>",
    re.IGNORECASE,
)
_FWD_AUTO_FRAME_DB_RE = re.compile(
    r"^(?:Ознакомься\s+с\s+пересланными\s+сообщениями\s+и\s+продолжи\s+задачу\s*/\s*дай\s+ответ\s*:|Пользователь\s+переслал\s+(?:сообщения|сообщение|контекст)\b[^\n]*|Прикреплённые\s+сообщения\s+из\s+диалога[^\n]*)\s*$",
    re.IGNORECASE,
)
_FWD_ITEM_HEADER_DB_RE = re.compile(
    r"^\s*\[Сообщение\s+\d+\s*·[^\]]*\]\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_DB_INTERRUPT_DIVIDERS = (
    "\n\n---\n⚠️",
    "\n\n---\n⏹",
    "\n\n---\n🔄",
)
_DB_INTERRUPT_SUFFIXES = (
    "*Генерация прервана при перезапуске сервера.*",
    "*Генерация остановлена пользователем.*",
    "_Генерация остановлена пользователем._",
)
_SUMMARY_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~")
_SUMMARY_LOG_LINE_RE = re.compile(
    r"^(?:\|[\s:\-|]+\||Traceback\s+\(most recent call last\):|\s*File\s+\"[^\"]+\",\s+line\s+\d+|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s+(?:INFO|DEBUG|WARNING|WARN|ERROR|CRITICAL)\b)",
    re.IGNORECASE,
)
_SUMMARY_FILLER_LINE_RE = re.compile(
    r"^(?:посмотрел(?:а|и)?\s+(?:файл|код|ваш|этот|скриншот|проект|лог)|отличный\s+(?:вопрос|план|вариант|выбор)|хороший\s+вопрос|ниже\s+(?:представлен\w*|приведён\w*|приведен\w*|показан\w*|описан\w*)|давайте\s+(?:разберём(?:ся)?|посмотрим|проверим)|конечно[,!.\s]+(?:давайте|я\s+помогу|вот)|разумеется[,!.\s]+|рад\s+помочь)[^.!?\n]*[.!?:]?\s*",
    re.IGNORECASE,
)


def _strip_interrupt_banners_db(text: str) -> str:
    if not text:
        return ""
    s = str(text)
    best_idx = -1
    for marker in _DB_INTERRUPT_DIVIDERS:
        idx = s.rfind(marker)
        if idx > best_idx:
            best_idx = idx
    if best_idx >= 0:
        s = s[:best_idx]
    while True:
        trimmed = s.rstrip()
        matched = False
        for suf in _DB_INTERRUPT_SUFFIXES:
            if trimmed.endswith(suf):
                s = trimmed[: -len(suf)]
                matched = True
                break
        if not matched:
            break
    probe = s.strip()
    if not probe:
        return ""
    if best_idx < 0 and (
        probe.startswith("⚠️ **Ошибка CLI")
        or probe.startswith("⚠️ Ошибка CLI")
        or probe in _DB_INTERRUPT_SUFFIXES
    ):
        return ""
    return s


_FWD_TAG_TOKEN_RE = re.compile(
    r"(?P<open><FORWARDED_MESSAGES\b(?P<attrs>[^>]*)>)|(?P<close></FORWARDED_MESSAGES\s*>)",
    re.IGNORECASE,
)


def find_forwarded_messages_blocks(text: str) -> list[dict[str, Any]]:
    """Find balanced outer `<FORWARDED_MESSAGES ...>...</FORWARDED_MESSAGES>` blocks."""
    if not text or "forwarded_messages" not in text.lower():
        return []
    tokens = list(_FWD_TAG_TOKEN_RE.finditer(text))
    if not tokens:
        return []
    blocks: list[dict[str, Any]] = []
    n = len(tokens)
    idx = 0
    while idx < n:
        tok = tokens[idx]
        if not tok.group("open"):
            idx += 1
            continue
        open_tok = tok
        depth = 1
        j = idx + 1
        close_tok: Optional[re.Match[str]] = None
        while j < n:
            t_j = tokens[j]
            if t_j.group("open"):
                depth += 1
                j += 1
            else:
                if depth > 1:
                    depth -= 1
                    j += 1
                else:
                    last_close = t_j
                    k = j + 1
                    while k < n and not tokens[k].group("open"):
                        last_close = tokens[k]
                        k += 1
                    close_tok = last_close
                    j = k
                    depth = 0
                    break
        if close_tok is not None:
            blocks.append(
                {
                    "start": open_tok.start(),
                    "end": close_tok.end(),
                    "attrs": open_tok.group("attrs") or "",
                    "inner": text[open_tok.end() : close_tok.start()],
                }
            )
            idx = j
        else:
            idx += 1
    return blocks


def _unwrap_forwarded_messages_db(text: str, _depth: int = 0) -> str:
    if "<FORWARDED_MESSAGES" not in text and "<forwarded_messages" not in text.lower():
        return text
    blocks = find_forwarded_messages_blocks(text)
    if not blocks:
        return text

    parts_before_ok: list[str] = []
    pos = 0
    for blk in blocks:
        before = text[pos : blk["start"]].strip()
        if before and not _FWD_AUTO_FRAME_DB_RE.match(before):
            parts_before_ok.append(before)
        inner_raw = blk["inner"] or ""
        if _depth < 4 and "forwarded_messages" in inner_raw.lower():
            inner_raw = _unwrap_forwarded_messages_db(inner_raw, _depth + 1)
        inner_clean = _FWD_ITEM_HEADER_DB_RE.sub("", inner_raw).strip()
        if inner_clean:
            parts_before_ok.append(inner_clean)
        pos = blk["end"]
    tail = text[pos:].strip()
    if tail:
        parts_before_ok.append(tail)
    return "\n\n".join(parts_before_ok)


def _unwrap_reply_quote_db(match: re.Match[str]) -> str:
    inner = (match.group(1) or "").strip()
    if not inner:
        return ""
    kept: list[str] = []
    for raw_ln in inner.splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue
        if re.match(r"^\[Контекст ответа:[^\]]*\]$", ln, re.IGNORECASE):
            continue
        kept.append(f"> {ln}")
        if len(kept) >= 45:
            break
    return ("\n" + "\n".join(kept)[:3200] + "\n") if kept else ""


def _strip_injected_prompt_tags(text: str) -> str:
    if not text:
        return ""
    cleaned = str(text).replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _strip_interrupt_banners_db(cleaned)
    cleaned = _CTX_TAG_STRIP_RE.sub("", cleaned)
    cleaned = re.sub(r"<CONTEXT_SUMMARY\b[^>]*>[\s\S]*?</CONTEXT_SUMMARY>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<PROJECT_PULSE\b[^>]*>[\s\S]*?</PROJECT_PULSE>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<HQ_RETURN_GIT_DIFF\b[^>]*>[\s\S]*?</HQ_RETURN_GIT_DIFF>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<REPLY_QUOTE\b[^>]*>([\s\S]*?)</REPLY_QUOTE>", _unwrap_reply_quote_db, cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"(?:⚡|🔍)?\s*\[Режим (?:быстрой приёмки|код-ревью) Штаба:[^\]]*\]\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^\s*\[КОНТЕКСТ ПРЕРВАННОГО ХОДА:[^\]]*\]\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\[СИСТЕМНОЕ ПРОДОЛЖЕНИЕ ХОДА:[\s\S]*?\]\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"^\s*---\s*\n+(?=\s*\[(?:Новое сообщение пользователя|Контур выполнения Antigravity Hub))",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^\s*\[Новое сообщение пользователя[^\]]*\]\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\s*\[Контур выполнения Antigravity Hub[\s\S]*?\]\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"^\s*\[(?:Предыдущий ход был (?:прерван|остановлен)|Внимание:\s*предыдущий ход)[^\]]*\]\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = _unwrap_forwarded_messages_db(cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _truncate_digest_piece(text: str, max_len: int) -> str:
    clean = (text or "").strip()
    if max_len <= 0:
        return ""
    if len(clean) <= max_len:
        return clean
    if max_len <= 4:
        return clean[:max_len]
    cut = clean[: max_len - 1]
    last_break = max(cut.rfind("\n"), cut.rfind(" "))
    if last_break >= int((max_len - 1) * 0.68):
        cut = cut[:last_break]
    return cut.rstrip(" .,;:-–—") + "…"


def _strip_code_logs_and_filler_for_summary(raw_text: str) -> str:
    base = _strip_injected_prompt_tags(raw_text)
    if not base:
        return ""
    no_code = _SUMMARY_FENCED_CODE_RE.sub("\n", base)
    out_lines: list[str] = []
    in_table_run = 0
    for raw_ln in no_code.split("\n"):
        ln = raw_ln.strip()
        if not ln:
            if out_lines and out_lines[-1] != "":
                out_lines.append("")
            in_table_run = 0
            continue
        if _SUMMARY_LOG_LINE_RE.match(ln):
            continue
        if ln.startswith("|") and ln.endswith("|") and ln.count("|") >= 3:
            in_table_run += 1
            if in_table_run > 2:
                continue
        else:
            in_table_run = 0
        ln_clean = _SUMMARY_FILLER_LINE_RE.sub("", ln).strip()
        if not ln_clean:
            continue
        out_lines.append(ln_clean)
    return "\n".join(out_lines).strip()


def _clean_summary_inline(text: str) -> str:
    s = re.sub(r"`([^`]+)`", r"\1", text or "")
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"(\*\*|__|\*|(?<!\w)_|_(?!\w))", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _extract_user_turn_topic(user_row: Any, max_len: int = 120) -> str:
    for col in ("custom_title", "auto_title"):
        try:
            val = str(user_row[col] or "").strip()
        except Exception:
            val = ""
        if val:
            return _truncate_digest_piece(re.sub(r"\s+", " ", val), max_len)
    cleaned = _strip_code_logs_and_filler_for_summary(str(user_row["content"] or ""))
    if not cleaned:
        return "Запрос"
    for raw_ln in cleaned.split("\n"):
        ln = _clean_summary_inline(re.sub(r"^\s*(?:#{1,6}\s+|>\s*|[-*+•]\s+|\d+[.)]\s+)", "", raw_ln.strip()))
        if len(ln) >= 3:
            return _truncate_digest_piece(ln, max_len)
    return _truncate_digest_piece(_clean_summary_inline(cleaned), max_len)


def _extract_assistant_turn_conclusions(asst_texts: list[str], max_len: int = 260) -> str:
    combined = "\n\n".join(
        t for t in (_strip_code_logs_and_filler_for_summary(x) for x in asst_texts) if t
    ).strip()
    if not combined:
        return ""

    headings: list[str] = []
    bold_bullets: list[str] = []
    conclusion_lines: list[str] = []
    prose_lines: list[str] = []

    for raw_ln in combined.split("\n"):
        ln = raw_ln.strip()
        if not ln or re.match(r"^[-=*_]{3,}$", ln):
            continue
        h_match = re.match(r"^(#{1,3})\s+(.+)$", ln)
        if h_match:
            h_txt = _clean_summary_inline(re.sub(r"\s+#+\s*$", "", h_match.group(2)))
            if len(h_txt) >= 3 and h_txt.lower() not in ("введение", "детали", "код", "пример"):
                headings.append(h_txt)
            continue

        b_match = re.match(r"^(?:[-*+•]|\d+[.)])\s+\*\*([^*]{2,90})\*\*\s*[:—–\-]?\s*(.*)$", ln)
        if b_match:
            lead = _clean_summary_inline(b_match.group(1))
            rest = _clean_summary_inline(b_match.group(2))
            item = f"{lead}: {_truncate_digest_piece(rest, 95)}" if rest else lead
            bold_bullets.append(item)
            continue

        clean_ln = _clean_summary_inline(re.sub(r"^(?:>\s*|[-*+•]\s+|\d+[.)]\s+)", "", ln))
        if len(clean_ln) < 5:
            continue
        if re.match(r"^(?:итог\w*|вывод\w*|решени\w+|рекомендаци\w+|резюме|главное)\b", clean_ln, re.IGNORECASE):
            conclusion_lines.append(_truncate_digest_piece(clean_ln, 150))
        else:
            prose_lines.append(clean_ln)

    picked: list[str] = []
    seen_norm: set[str] = set()

    def _add_unique(cand: str) -> None:
        c = cand.strip()
        if not c:
            return
        norm = re.sub(r"\W+", "", c.lower())[:48]
        if not norm or norm in seen_norm:
            return
        seen_norm.add(norm)
        picked.append(c)

    for c in conclusion_lines[:2]:
        _add_unique(c)
    for h in headings[:3]:
        _add_unique(h)
    for b in bold_bullets[:4]:
        _add_unique(b)

    if not picked and prose_lines:
        _add_unique(_truncate_digest_piece(prose_lines[0], 140))
        if len(prose_lines) > 1:
            _add_unique(_truncate_digest_piece(prose_lines[-1], 140))
    elif prose_lines and len(" · ".join(picked)) + 40 < max_len:
        _add_unique(_truncate_digest_piece(prose_lines[-1], 130))

    joined = "; ".join(picked)
    return _truncate_digest_piece(joined, max_len)


def _build_extractive_chat_summary(c_row: Any, m_rows: list[Any], budget: int) -> str:
    """Deterministic 0% GPU extractive summary of the entire conversation (<= budget chars)."""
    sections: list[str] = []

    first_user_row = next((r for r in m_rows if r["role"] == "user"), None)
    compacted_raw = _strip_code_logs_and_filler_for_summary(str(c_row["compacted_summary"] or ""))

    task_parts: list[str] = []
    if first_user_row is not None:
        first_q = _strip_code_logs_and_filler_for_summary(str(first_user_row["content"] or ""))
        if first_q:
            bm_lead = "🔖 " if int(first_user_row["bookmarked"] or 0) == 1 else ""
            task_parts.append(
                f"{bm_lead}{_truncate_digest_piece(re.sub(r'\\s+', ' ', first_q), min(350, max(120, int(budget * 0.16))))}"
            )
    if compacted_raw:
        comp_cap = min(700, max(180, int(budget * 0.24)))
        task_parts.append(f"Сводка раннего контекста: {_truncate_digest_piece(re.sub(r'\\s+', ' ', compacted_raw), comp_cap)}")

    if task_parts:
        sections.append("[Исходная задача]:\n" + "\n".join(task_parts))

    # Group messages into chronological turns (User -> Assistant)
    turns: list[dict[str, Any]] = []
    for r in m_rows:
        role = str(r["role"] or "")
        is_bm = int(r["bookmarked"] or 0) == 1
        if role == "user":
            turns.append(
                {
                    "user": r,
                    "assistants": [],
                    "bookmarked": is_bm,
                }
            )
        elif role == "assistant":
            if turns and turns[-1]["user"] is not None:
                turns[-1]["assistants"].append(str(r["content"] or ""))
                if is_bm:
                    turns[-1]["bookmarked"] = True
            else:
                turns.append(
                    {
                        "user": None,
                        "assistants": [str(r["content"] or "")],
                        "bookmarked": is_bm,
                    }
                )

    if not turns:
        return "\n\n".join(sections).strip() or "(В прикреплённом чате пока нет сохранённых сообщений.)"

    if len(turns) == 1 and not turns[0]["assistants"] and sections:
        out = "\n\n".join(s for s in sections if s).strip()
        return _truncate_digest_piece(out, budget) if out else "(В прикреплённом чате пока нет сохранённых сообщений.)"

    used_so_far = sum(len(s) for s in sections) + 36
    rem_budget = max(240, budget - used_so_far)

    n_turns = len(turns)
    weights: list[float] = []
    for idx, t in enumerate(turns):
        w = 1.0
        if t["bookmarked"]:
            w = max(w, 1.85)
        if idx >= n_turns - 2:
            w = max(w, 2.25)
        elif idx == 0:
            w = max(w, 1.25)
        weights.append(w)

    total_w = sum(weights) or 1.0
    turn_lines: list[str] = []
    for idx, t in enumerate(turns):
        raw_alloc = int((rem_budget * weights[idx]) / total_w) - 18
        turn_cap = max(75, min(520, raw_alloc))
        bm_prefix = "🔖 " if t["bookmarked"] else ""
        q_cap = max(36, min(115, int(turn_cap * 0.38)))
        a_cap = max(40, turn_cap - q_cap - 10)
        q_txt = _extract_user_turn_topic(t["user"], q_cap) if t["user"] is not None else "Ответ"
        a_txt = _extract_assistant_turn_conclusions(t["assistants"], a_cap)
        if a_txt:
            line = f"{idx + 1}. {bm_prefix}{q_txt} → {a_txt}"
        else:
            line = f"{idx + 1}. {bm_prefix}{q_txt}"
        turn_lines.append(_truncate_digest_piece(line, max(80, turn_cap + 14)))

    if turn_lines:
        # Ensure we fit comfortably inside rem_budget while keeping start, bookmarks, and tail intact
        evo_header = "[Эволюция обсуждения]:\n"
        joined_evo = evo_header + "\n".join(turn_lines)
        if len(joined_evo) > rem_budget and len(turn_lines) > 4:
            # Progressively tighten middle non-bookmarked lines first before touching start/bookmarks/last 2
            mutable_lines = list(turn_lines)
            for idx in range(1, len(mutable_lines) - 2):
                if len(evo_header + "\n".join(mutable_lines)) <= rem_budget:
                    break
                if not turns[idx]["bookmarked"]:
                    mutable_lines[idx] = _truncate_digest_piece(mutable_lines[idx], 95)
            joined_evo = evo_header + "\n".join(mutable_lines)
        sections.append(_truncate_digest_piece(joined_evo, rem_budget))

    out = "\n\n".join(s for s in sections if s).strip()
    return _truncate_digest_piece(out, budget) if out else "(В прикреплённом чате пока нет сохранённых сообщений.)"


_CODE_SPEC_PROMPT_RE = re.compile(r"^Реализуй\s+утвержд[ёе]нный\s+план\b", re.IGNORECASE)
_CODE_SPEC_META_INTRO_RE = re.compile(
    r"^(?:ниже|вот)\s+(?:готовое|самодостаточное|полное|утвержд[ёе]нное|итоговое)\b",
    re.IGNORECASE,
)


def _is_code_spec_handoff_row(row: Any) -> bool:
    try:
        keys = set(row.keys())
    except Exception:
        keys = set()
    role = str((row["role"] if "role" in keys else "") or "")
    if role != "user":
        return False
    if "usage_json" in keys:
        raw_usage = str(row["usage_json"] or "").strip()
        if raw_usage and raw_usage != "{}":
            try:
                u = json.loads(raw_usage)
                if (
                    isinstance(u, dict)
                    and isinstance(u.get("handoff_meta"), dict)
                    and u["handoff_meta"].get("kind") == "code_spec"
                ):
                    return True
            except Exception:
                pass
    if "usage" in keys and isinstance(row["usage"], dict):
        hm = row["usage"].get("handoff_meta")
        if isinstance(hm, dict) and hm.get("kind") == "code_spec":
            return True
    raw_content = str((row["content"] if "content" in keys else "") or "")
    clean_txt = _strip_injected_prompt_tags(raw_content).lstrip()
    return bool(_CODE_SPEC_PROMPT_RE.match(clean_txt))


def _extract_code_spec_short_anchor(row: Any) -> str:
    try:
        keys = set(row.keys())
    except Exception:
        keys = set()
    topic = ""
    for col in ("custom_title", "auto_title"):
        if col in keys:
            val = re.sub(r"\s+", " ", str(row[col] or "")).strip()
            if val:
                topic = _truncate_digest_piece(val, 140)
                break
    if not topic:
        raw_content = str((row["content"] if "content" in keys else "") or "")
        cleaned = _strip_injected_prompt_tags(raw_content)
        no_code = _SUMMARY_FENCED_CODE_RE.sub("\n", cleaned)
        candidate_lines: list[tuple[bool, str]] = []
        skipped_header_lead = False
        for raw_ln in no_code.split("\n"):
            ln = raw_ln.strip()
            if not ln or re.match(r"^[-=*_]{3,}$", ln):
                continue
            if not skipped_header_lead and _CODE_SPEC_PROMPT_RE.match(ln):
                skipped_header_lead = True
                continue
            skipped_header_lead = True
            is_h1 = bool(re.match(r"^#{1,2}\s+\S", ln))
            clean_ln = _clean_summary_inline(
                re.sub(r"^\s*(?:#{1,6}\s+|>\s*|[-*+•]\s+|\d+[.)]\s+)", "", ln)
            )
            if len(clean_ln) < 3:
                continue
            if _CODE_SPEC_META_INTRO_RE.match(clean_ln):
                continue
            candidate_lines.append((is_h1, clean_ln))
            if len(candidate_lines) >= 6:
                break
        picked_ln = ""
        if candidate_lines:
            h1_cand = next((txt for is_h1, txt in candidate_lines[:3] if is_h1), "")
            picked_ln = h1_cand if h1_cand else candidate_lines[0][1]
        if picked_ln:
            topic = _truncate_digest_piece(picked_ln, 140)
    return f"[Исходное ТЗ из Штаба: «{topic}»]" if topic else "[Исходное ТЗ из Штаба]"


async def build_referenced_chat_digest(
    chat_id: str,
    *,
    scope: str = "full",
    max_chars: Optional[int] = None,
    exclude_message_id: Optional[int] = None,
    focus_latest_task_segment: bool = False,
) -> Optional[dict[str, Any]]:
    """Build a pure <REFERENCED_CHAT> digest (0 tool logs, 0 thinking) in 3 scopes:
    - scope='full': compacted_summary + bookmarks 🔖 + chronological active messages (up to 100 000 chars)
    - scope='last': last completed assistant answer in full + brief preceding user prompt (up to 100 000 chars)
    - scope='summary': deterministic 0% GPU extractive summary across all turns without code/filler (up to 4 800 chars)
    """
    if not chat_id:
        return None
    clean_scope = str(scope or "full").strip().lower()
    if clean_scope not in VALID_REFERENCED_CHAT_SCOPES:
        clean_scope = "full"

    default_cap = (
        MAX_REFERENCED_CHAT_SUMMARY_CHARS
        if clean_scope == "summary"
        else MAX_REFERENCED_CHAT_LAST_CHARS
        if clean_scope == "last"
        else MAX_REFERENCED_CHAT_FULL_CHARS
    )
    if max_chars is None:
        hard_limit = default_cap
    else:
        hard_limit = max(600, min(int(max_chars), 120_000))
        if clean_scope == "summary":
            hard_limit = min(hard_limit, MAX_REFERENCED_CHAT_SUMMARY_CHARS)

    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur_c = await db.execute(
            """
            SELECT id, title, kind, project_name, access_mode,
                   compacted_summary, compacted_up_to_msg_id
            FROM chats
            WHERE id = ?
            LIMIT 1
            """,
            (chat_id,),
        )
        c_row = await cur_c.fetchone()
        if not c_row:
            return None

        # Deliberately omit thinking and tool_calls_json so 0 raw logs ever leak in
        try:
            cur_m = await db.execute(
                """
                SELECT id, role, content, status, bookmarked, custom_title, auto_title,
                       CASE WHEN role = 'user' THEN usage_json ELSE '{}' END AS usage_json
                FROM messages
                WHERE chat_id = ?
                  AND role IN ('user', 'assistant')
                  AND status NOT IN ('streaming', 'compacted')
                  AND content != ''
                ORDER BY id ASC
                """,
                (chat_id,),
            )
            m_rows = await cur_m.fetchall()
        except Exception:
            cur_m = await db.execute(
                """
                SELECT id, role, content, status, bookmarked
                FROM messages
                WHERE chat_id = ?
                  AND role IN ('user', 'assistant')
                  AND status != 'streaming'
                  AND content != ''
                ORDER BY id ASC
                """,
                (chat_id,),
            )
            m_rows = await cur_m.fetchall()

    c_dict = dict(c_row)
    title = str(c_dict.get("title") or "Чат").replace('"', "'").strip()
    mode = str(c_dict.get("access_mode") or "read_only")
    compacted_summary = _strip_injected_prompt_tags(str(c_dict.get("compacted_summary") or ""))
    compacted_up_to = int(c_dict.get("compacted_up_to_msg_id") or 0)

    m_rows = [dict(r) for r in m_rows]
    if exclude_message_id is not None and int(exclude_message_id) > 0:
        excl_id = int(exclude_message_id)
        if any(int(r["id"]) == excl_id for r in m_rows):
            if focus_latest_task_segment:
                m_rows = [r for r in m_rows if int(r["id"]) <= excl_id]
            m_rows = [r for r in m_rows if int(r["id"]) != excl_id]

    if focus_latest_task_segment:
        spec_idx = -1
        for idx in range(len(m_rows) - 1, -1, -1):
            if _is_code_spec_handoff_row(m_rows[idx]):
                spec_idx = idx
                break
        if spec_idx >= 0:
            m_rows = m_rows[spec_idx:]
            if int(m_rows[0]["id"]) > compacted_up_to:
                compacted_summary = ""
                c_dict["compacted_summary"] = ""
            if len(m_rows) == 1 and int(m_rows[0].get("bookmarked") or 0) == 0:
                return None
            if int(m_rows[0].get("bookmarked") or 0) == 0:
                m_rows[0] = {
                    **m_rows[0],
                    "content": _extract_code_spec_short_anchor(m_rows[0]),
                }
        else:
            if not m_rows and not compacted_summary:
                return None

    header = f'<REFERENCED_CHAT title="{title}" mode="{mode}" scope="{clean_scope}">'
    footer = "</REFERENCED_CHAT>"
    budget = max(200, hard_limit - len(header) - len(footer) - 8)

    was_truncated = False
    if clean_scope == "last":
        # Scope 'last': only the last completed assistant message (or last user message if no assistant reply yet)
        cleaned_rows: list[tuple[int, str, str, str]] = []
        for idx, r in enumerate(m_rows):
            c_txt = _strip_injected_prompt_tags(str(r["content"] or ""))
            if not c_txt:
                continue
            if r.get("bookmarked"):
                c_txt = "🔖 " + c_txt
            st = str(r["status"] or "done") if "status" in r.keys() else "done"
            cleaned_rows.append((idx, str(r["role"]), c_txt, st))

        target_pos = -1
        # Prefer last 'done' assistant message, fallback to any non-streaming assistant, then last user
        for pos in range(len(cleaned_rows) - 1, -1, -1):
            if cleaned_rows[pos][1] == "assistant" and cleaned_rows[pos][3] != "error":
                target_pos = pos
                break
        if target_pos < 0:
            for pos in range(len(cleaned_rows) - 1, -1, -1):
                if cleaned_rows[pos][1] == "assistant":
                    target_pos = pos
                    break
        if target_pos < 0 and cleaned_rows:
            target_pos = len(cleaned_rows) - 1

        if target_pos < 0:
            body = "(В прикреплённом чате пока нет сохранённых сообщений.)"
        else:
            _, t_role, t_text, _ = cleaned_rows[target_pos]
            last_parts: list[str] = []
            if t_role == "assistant":
                prev_user_txt = ""
                for p in range(target_pos - 1, -1, -1):
                    if cleaned_rows[p][1] == "user":
                        prev_user_txt = cleaned_rows[p][2]
                        break
                if prev_user_txt:
                    q_cap = min(2000, max(200, int(budget * 0.15)))
                    was_truncated |= len(prev_user_txt) > q_cap
                    last_parts.append(f"[На запрос пользователя]: {_truncate_digest_piece(prev_user_txt, q_cap)}")
                used_q = sum(len(x) for x in last_parts) + (4 if last_parts else 0)
                ans_cap = max(200, budget - used_q - 34)
                was_truncated |= len(t_text) > ans_cap
                last_parts.append(f"[Последний ответ ассистента]:\n{_truncate_digest_piece(t_text, ans_cap)}")
            else:
                msg_cap = max(200, budget - 38)
                was_truncated |= len(t_text) > msg_cap
                last_parts.append(f"[Последнее сообщение пользователя]:\n{_truncate_digest_piece(t_text, msg_cap)}")
            body = "\n\n".join(last_parts).strip()

    elif clean_scope == "summary":
        was_truncated = sum(len(_strip_injected_prompt_tags(str(r["content"] or ""))) for r in m_rows) + len(compacted_summary) > budget
        body = _build_extractive_chat_summary(c_dict, m_rows, budget)

    else:
        entries = []
        if compacted_summary:
            entries.append(("[Сжатая сводка чата]:\n", compacted_summary))
        for row in m_rows:
            if int(row["id"]) <= compacted_up_to and not row.get("bookmarked"):
                continue
            text = _strip_injected_prompt_tags(str(row["content"] or ""))
            if text:
                marker = "🔖 " if row.get("bookmarked") else ""
                entries.append((f"[{marker}{row['role']}]: ", text))
        overhead = sum(len(prefix) + 2 for prefix, _ in entries)
        avail_text_chars = max(0, budget - overhead)
        lengths = [len(text) for _, text in entries]
        caps = list(lengths)
        if sum(lengths) > avail_text_chars:
            was_truncated = True
            caps = [0] * len(entries)
            if entries:
                caps[-1] = min(lengths[-1], max(8000, int(avail_text_chars * .45)), avail_text_chars)
            remaining = avail_text_chars - sum(caps)
            while remaining > 0:
                needy = [i for i in range(len(entries)) if caps[i] < lengths[i]]
                if not needy:
                    break
                share = max(1, remaining // len(needy))
                for i in needy:
                    take = min(remaining, share, lengths[i] - caps[i])
                    caps[i] += take
                    remaining -= take
        body = "\n\n".join(prefix + _truncate_digest_piece(text, caps[i])
                               for i, (prefix, text) in enumerate(entries) if caps[i])

    if not body:
        body = "(В прикреплённом чате пока нет сохранённых сообщений.)"
    if len(body) > budget:
        was_truncated = True
        body = _truncate_digest_piece(body, max(80, budget))

    block = f"{header}\n{body}\n{footer}"
    return {
        "id": str(c_row["id"]),
        "title": str(c_row["title"] or "Чат"),
        "access_mode": mode,
        "scope": clean_scope,
        "block": block[:hard_limit],
        "chars": len(block[:hard_limit]),
        "truncated": bool(was_truncated or len(block) > hard_limit),
    }


# ---------------------------------------------------------------------------
# Personal OpenRouter Crypto Vault & Strict Per-User Telemetry (v59)
# ---------------------------------------------------------------------------

OPENROUTER_MAX_LOGS_PER_USER = 500
DEFAULT_OPENROUTER_SMART_TITLES_MODEL = "openai/gpt-4.1-mini"


def _require_openrouter_user_id(user_id: str) -> str:
    clean_uid = (user_id or "").strip()
    if not clean_uid:
        raise ValueError("Не указан идентификатор пользователя OpenRouter")
    return clean_uid


def _derive_openrouter_fernet(user_id: str) -> Fernet:
    """Derive a deterministic per-user Fernet key via PBKDF2-HMAC-SHA256."""
    clean_uid = _require_openrouter_user_id(user_id)
    salt = f"openrouter-vault:{clean_uid}".encode("utf-8")
    raw_key = hashlib.pbkdf2_hmac(
        "sha256",
        HUB_SECRET_KEY.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=32,
    )
    return Fernet(base64.urlsafe_b64encode(raw_key))


def encrypt_openrouter_api_key(user_id: str, raw_api_key: str) -> str:
    clean_key = (raw_api_key or "").strip()
    if not clean_key:
        return ""
    fernet = _derive_openrouter_fernet(user_id)
    return fernet.encrypt(clean_key.encode("utf-8")).decode("ascii")


def decrypt_openrouter_api_key(user_id: str, encrypted_api_key: str) -> str:
    token = (encrypted_api_key or "").strip()
    if not token:
        return ""
    try:
        fernet = _derive_openrouter_fernet(user_id)
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, Exception):
        return ""


def mask_openrouter_api_key(raw_api_key: str) -> str:
    """Format key mask as `sk-or-v1-••••9f3a` — the ONLY representation sent to UI."""
    clean = (raw_api_key or "").strip()
    if not clean:
        return ""
    tail = clean[-4:] if len(clean) >= 4 else clean
    if clean.startswith("sk-or-v1-") and len(clean) > 13:
        return f"sk-or-v1-••••{tail}"
    if clean.startswith("sk-or-") and len(clean) > 10:
        return f"sk-or-••••{tail}"
    if len(clean) >= 10:
        return f"{clean[:6]}••••{tail}"
    return f"••••{tail}"


def _default_openrouter_config_dict(user_id: str) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "has_key": False,
        "key_mask": "",
        "is_enabled": True,
        "default_model": "",
        "custom_rules": "",
        "max_price_per_m": 0.0,
        "daily_spend_limit_usd": 0.0,
        "smart_titles_enabled": False,
        "smart_titles_model": DEFAULT_OPENROUTER_SMART_TITLES_MODEL,
        "cached_status": {},
        "cached_at": "",
        "updated_at": "",
    }


_LEGACY_QUERY_OPENROUTER_RE = re.compile(r"(?:(?<=\b)|(?<=_))query_openrouter\b")


def normalize_legacy_openrouter_rules(text: str) -> str:
    """Replace legacy `query_openrouter` tool references with `openrouter_chat` without altering other rules."""
    if not text:
        return ""
    return _LEGACY_QUERY_OPENROUTER_RE.sub("openrouter_chat", str(text))


def _row_to_public_openrouter_config(row: Optional[aiosqlite.Row], user_id: str) -> dict[str, Any]:
    if not row:
        return _default_openrouter_config_dict(user_id)
    d = dict(row)
    raw_cached = str(d.get("cached_status_json") or "{}")
    try:
        parsed_cached = json.loads(raw_cached) if raw_cached else {}
        if not isinstance(parsed_cached, dict):
            parsed_cached = {}
    except Exception:
        parsed_cached = {}
    has_key = bool(str(d.get("api_key_encrypted") or "").strip() and str(d.get("key_nonce") or "").strip())
    raw_st_model = str(d.get("smart_titles_model") or DEFAULT_OPENROUTER_SMART_TITLES_MODEL).strip()
    return {
        "user_id": str(d.get("user_id") or user_id),
        "has_key": has_key,
        "key_mask": str(d.get("key_mask") or "") if has_key else "",
        "is_enabled": bool(int(d.get("is_enabled", 1) or 0)),
        "default_model": str(d.get("default_model") or "").strip(),
        "custom_rules": normalize_legacy_openrouter_rules(str(d.get("custom_rules") or "")),
        "max_price_per_m": max(0.0, float(d.get("max_price_per_m") or 0.0)),
        "daily_spend_limit_usd": max(0.0, float(d.get("daily_spend_limit_usd") or 0.0)),
        "smart_titles_enabled": bool(int(d.get("smart_titles_enabled", 0) or 0)),
        "smart_titles_model": raw_st_model or DEFAULT_OPENROUTER_SMART_TITLES_MODEL,
        "cached_status": parsed_cached,
        "cached_at": str(d.get("cached_at") or ""),
        "updated_at": str(d.get("updated_at") or ""),
    }


async def get_user_openrouter_config(user_id: str) -> dict[str, Any]:
    """Return strictly user-scoped public OpenRouter config (never exposes encrypted key or nonce)."""
    clean_uid = _require_openrouter_user_id(user_id)
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM user_openrouter_configs WHERE user_id = ? LIMIT 1",
            (clean_uid,),
        )
        row = await cur.fetchone()
        return _row_to_public_openrouter_config(row, clean_uid)


async def get_user_openrouter_bridge_meta(user_id: str) -> dict[str, Any]:
    """Internal helper for MCP config/SKILL sync & turn permission check (does NOT decrypt key)."""
    clean_uid = _require_openrouter_user_id(user_id)
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM user_openrouter_configs WHERE user_id = ? LIMIT 1",
            (clean_uid,),
        )
        row = await cur.fetchone()
        pub = _row_to_public_openrouter_config(row, clean_uid)
        pub["key_nonce"] = str(row["key_nonce"] or "") if row and pub["has_key"] else ""
        return pub


async def list_all_openrouter_bridge_metas() -> list[dict[str, Any]]:
    """Internal startup helper to sync isolated CLI MCP configs for all users without decrypting keys."""
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur_u = await db.execute("SELECT id FROM users;")
        user_ids = [str(r["id"]) for r in await cur_u.fetchall() if str(r["id"] or "").strip()]
        cur_c = await db.execute("SELECT * FROM user_openrouter_configs;")
        cfg_by_uid = {str(r["user_id"]): r for r in await cur_c.fetchall()}
    out: list[dict[str, Any]] = []
    for uid in user_ids:
        row = cfg_by_uid.get(uid)
        pub = _row_to_public_openrouter_config(row, uid)
        pub["key_nonce"] = str(row["key_nonce"] or "") if row and pub["has_key"] else ""
        out.append(pub)
    return out


async def get_user_openrouter_secret_bundle(user_id: str) -> Optional[dict[str, Any]]:
    """In-memory-only bundle for FastAPI MCP/REST execution. Raw key lives strictly in RAM."""
    clean_uid = _require_openrouter_user_id(user_id)
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur_u = await db.execute(
            "SELECT id, username, is_active FROM users WHERE id = ? LIMIT 1",
            (clean_uid,),
        )
        u_row = await cur_u.fetchone()
        if not u_row or not bool(int(u_row["is_active"] or 0)):
            return None
        cur = await db.execute(
            "SELECT * FROM user_openrouter_configs WHERE user_id = ? LIMIT 1",
            (clean_uid,),
        )
        row = await cur.fetchone()
        if not row:
            return None
    enc = str(row["api_key_encrypted"] or "").strip()
    nonce = str(row["key_nonce"] or "").strip()
    raw_key = decrypt_openrouter_api_key(clean_uid, enc) if enc else ""
    pub = _row_to_public_openrouter_config(row, clean_uid)
    pub["api_key"] = raw_key
    pub["key_nonce"] = nonce
    pub["has_key"] = bool(raw_key and nonce)
    pub["user_is_active"] = True
    return pub


async def upsert_user_openrouter_config(
    user_id: str,
    *,
    new_api_key: Optional[str] = None,
    is_enabled: Optional[bool] = None,
    default_model: Optional[str] = None,
    custom_rules: Optional[str] = None,
    max_price_per_m: Optional[float] = None,
    daily_spend_limit_usd: Optional[float] = None,
    smart_titles_enabled: Optional[bool] = None,
    smart_titles_model: Optional[str] = None,
    cached_status: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create or update user's personal OpenRouter config. Rotates key_nonce when replacing key."""
    clean_uid = _require_openrouter_user_id(user_id)

    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM user_openrouter_configs WHERE user_id = ? LIMIT 1",
            (clean_uid,),
        )
        existing = await cur.fetchone()

        enc_key = str(existing["api_key_encrypted"] or "") if existing else ""
        key_mask = str(existing["key_mask"] or "") if existing else ""
        key_nonce = str(existing["key_nonce"] or "") if existing else ""
        next_enabled = int(existing["is_enabled"]) if existing else 1
        next_model = str(existing["default_model"] or "") if existing else ""
        next_rules = normalize_legacy_openrouter_rules(str(existing["custom_rules"] or "")) if existing else ""
        next_max_price = float(existing["max_price_per_m"] or 0.0) if existing else 0.0
        next_daily_limit = float(existing["daily_spend_limit_usd"] or 0.0) if existing else 0.0
        next_smart_titles_enabled = int(existing["smart_titles_enabled"] or 0) if existing else 0
        next_smart_titles_model = (
            str(existing["smart_titles_model"] or DEFAULT_OPENROUTER_SMART_TITLES_MODEL).strip()
            or DEFAULT_OPENROUTER_SMART_TITLES_MODEL
            if existing
            else DEFAULT_OPENROUTER_SMART_TITLES_MODEL
        )
        next_cached_json = str(existing["cached_status_json"] or "{}") if existing else "{}"
        next_cached_at = str(existing["cached_at"] or "") if existing else ""

        if new_api_key is not None:
            clean_key = new_api_key.strip()
            if not clean_key:
                raise ValueError("API-ключ OpenRouter не может быть пустым")
            enc_key = encrypt_openrouter_api_key(clean_uid, clean_key)
            key_mask = mask_openrouter_api_key(clean_key)
            key_nonce = secrets.token_hex(16)

        if is_enabled is not None:
            next_enabled = 1 if bool(is_enabled) else 0

        if default_model is not None:
            next_model = default_model.strip()[:160]

        if custom_rules is not None:
            clean_rules = normalize_legacy_openrouter_rules(
                custom_rules.replace("\r\n", "\n").replace("\r", "\n").strip()
            )
            byte_len = len(clean_rules.encode("utf-8"))
            if byte_len > MAX_OPENROUTER_RULES_BYTES:
                raise ValueError(
                    f"Правила OpenRouter превышают лимит 12 КБ ({byte_len:,} из {MAX_OPENROUTER_RULES_BYTES:,} байт)".replace(
                        ",", " "
                    )
                )
            next_rules = clean_rules

        if max_price_per_m is not None:
            val_price = float(max_price_per_m)
            if val_price < 0:
                raise ValueError("Максимальная цена за 1M токенов не может быть отрицательной")
            next_max_price = round(val_price, 6)

        if daily_spend_limit_usd is not None:
            val_daily = float(daily_spend_limit_usd)
            if val_daily < 0:
                raise ValueError("Суточный лимит расходов не может быть отрицательным")
            next_daily_limit = round(val_daily, 6)

        if smart_titles_enabled is not None:
            next_smart_titles_enabled = 1 if bool(smart_titles_enabled) else 0

        if smart_titles_model is not None:
            cleaned_st_model = str(smart_titles_model).strip()[:160]
            next_smart_titles_model = cleaned_st_model or DEFAULT_OPENROUTER_SMART_TITLES_MODEL

        now = utc_now_iso()
        if cached_status is not None and isinstance(cached_status, dict):
            next_cached_json = json.dumps(cached_status, ensure_ascii=False)
            next_cached_at = now

        await db.execute(
            """
            INSERT INTO user_openrouter_configs (
                user_id, api_key_encrypted, key_mask, key_nonce, is_enabled,
                default_model, custom_rules, max_price_per_m, daily_spend_limit_usd,
                smart_titles_enabled, smart_titles_model,
                cached_status_json, cached_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                api_key_encrypted = excluded.api_key_encrypted,
                key_mask = excluded.key_mask,
                key_nonce = excluded.key_nonce,
                is_enabled = excluded.is_enabled,
                default_model = excluded.default_model,
                custom_rules = excluded.custom_rules,
                max_price_per_m = excluded.max_price_per_m,
                daily_spend_limit_usd = excluded.daily_spend_limit_usd,
                smart_titles_enabled = excluded.smart_titles_enabled,
                smart_titles_model = excluded.smart_titles_model,
                cached_status_json = excluded.cached_status_json,
                cached_at = excluded.cached_at,
                updated_at = excluded.updated_at
            """,
            (
                clean_uid,
                enc_key,
                key_mask,
                key_nonce,
                next_enabled,
                next_model,
                next_rules,
                next_max_price,
                next_daily_limit,
                next_smart_titles_enabled,
                next_smart_titles_model,
                next_cached_json,
                next_cached_at,
                now,
            ),
        )
        await db.commit()
        cur2 = await db.execute(
            "SELECT * FROM user_openrouter_configs WHERE user_id = ? LIMIT 1",
            (clean_uid,),
        )
        row2 = await cur2.fetchone()
        return _row_to_public_openrouter_config(row2, clean_uid)


async def update_user_openrouter_cached_status(
    user_id: str,
    cached_status: dict[str, Any],
) -> dict[str, Any]:
    clean_uid = _require_openrouter_user_id(user_id)
    now = utc_now_iso()
    payload = json.dumps(cached_status or {}, ensure_ascii=False)
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """
            UPDATE user_openrouter_configs
            SET cached_status_json = ?, cached_at = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (payload, now, now, clean_uid),
        )
        await db.commit()
        cur = await db.execute(
            "SELECT * FROM user_openrouter_configs WHERE user_id = ? LIMIT 1",
            (clean_uid,),
        )
        row = await cur.fetchone()
        return _row_to_public_openrouter_config(row, clean_uid)


async def delete_user_openrouter_key(user_id: str) -> dict[str, Any]:
    """Erase encrypted API key & cached balance and rotate key_nonce to immediately revoke MCP tokens."""
    clean_uid = _require_openrouter_user_id(user_id)
    now = utc_now_iso()
    revoked_nonce = secrets.token_hex(16)
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT user_id FROM user_openrouter_configs WHERE user_id = ? LIMIT 1",
            (clean_uid,),
        )
        if await cur.fetchone():
            await db.execute(
                """
                UPDATE user_openrouter_configs
                SET api_key_encrypted = '',
                    key_mask = '',
                    key_nonce = ?,
                    cached_status_json = '{}',
                    cached_at = '',
                    updated_at = ?
                WHERE user_id = ?
                """,
                (revoked_nonce, now, clean_uid),
            )
            await db.commit()
        cur2 = await db.execute(
            "SELECT * FROM user_openrouter_configs WHERE user_id = ? LIMIT 1",
            (clean_uid,),
        )
        row2 = await cur2.fetchone()
        return _row_to_public_openrouter_config(row2, clean_uid)


def _row_to_openrouter_log(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    raw_purpose = str(d.get("purpose") or "mcp_chat").strip() or "mcp_chat"
    raw_items = d.get("items_count")
    items_cnt = max(0, int(raw_items)) if raw_items is not None else 1
    return {
        "id": int(d.get("id") or 0),
        "user_id": str(d.get("user_id") or ""),
        "chat_id": str(d.get("chat_id") or ""),
        "chat_title": str(d.get("chat_title") or ""),
        "project_name": str(d.get("project_name") or ""),
        "requested_model": str(d.get("requested_model") or ""),
        "resolved_model": str(d.get("resolved_model") or ""),
        "provider_name": str(d.get("provider_name") or ""),
        "generation_id": str(d.get("generation_id") or ""),
        "prompt_tokens": int(d.get("prompt_tokens") or 0),
        "completion_tokens": int(d.get("completion_tokens") or 0),
        "reasoning_tokens": int(d.get("reasoning_tokens") or 0),
        "cached_tokens": int(d.get("cached_tokens") or 0),
        "cost_usd": float(d.get("cost_usd") or 0.0),
        "latency_ms": int(d.get("latency_ms") or 0),
        "status": str(d.get("status") or "ok"),
        "finish_reason": str(d.get("finish_reason") or ""),
        "prompt_preview": str(d.get("prompt_preview") or ""),
        "response_preview": str(d.get("response_preview") or ""),
        "error_text": str(d.get("error_text") or ""),
        "purpose": raw_purpose,
        "items_count": items_cnt,
        "created_at": str(d.get("created_at") or ""),
    }


async def add_openrouter_log(
    user_id: str,
    *,
    chat_id: str = "",
    chat_title: str = "",
    project_name: str = "",
    requested_model: str = "",
    resolved_model: str = "",
    provider_name: str = "",
    generation_id: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    reasoning_tokens: int = 0,
    cached_tokens: int = 0,
    cost_usd: float = 0.0,
    latency_ms: int = 0,
    status: str = "ok",
    finish_reason: str = "",
    prompt_preview: str = "",
    response_preview: str = "",
    error_text: str = "",
    purpose: str = "mcp_chat",
    items_count: int = 1,
) -> dict[str, Any]:
    """Append a personal OpenRouter call log entry and auto-trim beyond 500 records per user."""
    clean_uid = _require_openrouter_user_id(user_id)
    clean_status = status if status in ("ok", "error", "blocked") else "error"
    clean_prompt = (prompt_preview or "").strip()[:500]
    clean_response = (response_preview or "").strip()[:600]
    clean_err = (error_text or "").strip()[:1000]
    clean_purpose = re.sub(r"[^a-zA-Z0-9_.\-]", "", str(purpose or "").strip())[:64] or "mcp_chat"
    clean_items_count = max(0, int(items_count or 0))
    now = utc_now_iso()

    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            INSERT INTO openrouter_logs (
                user_id, chat_id, chat_title, project_name,
                requested_model, resolved_model, provider_name, generation_id,
                prompt_tokens, completion_tokens, reasoning_tokens, cached_tokens,
                cost_usd, latency_ms, status, finish_reason,
                prompt_preview, response_preview, error_text,
                purpose, items_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clean_uid,
                (chat_id or "").strip()[:120],
                (chat_title or "").strip()[:180],
                (project_name or "").strip()[:120],
                (requested_model or "").strip()[:160],
                (resolved_model or "").strip()[:160],
                (provider_name or "").strip()[:120],
                (generation_id or "").strip()[:160],
                max(0, int(prompt_tokens or 0)),
                max(0, int(completion_tokens or 0)),
                max(0, int(reasoning_tokens or 0)),
                max(0, int(cached_tokens or 0)),
                max(0.0, float(cost_usd or 0.0)),
                max(0, int(latency_ms or 0)),
                clean_status,
                (finish_reason or "").strip()[:80],
                clean_prompt,
                clean_response,
                clean_err,
                clean_purpose,
                clean_items_count,
                now,
            ),
        )
        new_id = int(cur.lastrowid or 0)
        await db.execute(
            """
            DELETE FROM openrouter_logs
            WHERE user_id = ?
              AND id NOT IN (
                  SELECT id FROM openrouter_logs
                  WHERE user_id = ?
                  ORDER BY id DESC
                  LIMIT ?
              )
            """,
            (clean_uid, clean_uid, OPENROUTER_MAX_LOGS_PER_USER),
        )
        await db.commit()
        cur_row = await db.execute(
            "SELECT * FROM openrouter_logs WHERE id = ? AND user_id = ? LIMIT 1",
            (new_id, clean_uid),
        )
        row = await cur_row.fetchone()
        return _row_to_openrouter_log(row) if row else {"id": new_id, "user_id": clean_uid}


async def list_openrouter_logs(user_id: str, *, limit: int = 60) -> list[dict[str, Any]]:
    """Return strictly user-owned OpenRouter call logs ordered newest first."""
    clean_uid = _require_openrouter_user_id(user_id)
    cap = max(1, min(int(limit or 60), OPENROUTER_MAX_LOGS_PER_USER))
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT * FROM openrouter_logs
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (clean_uid, cap),
        )
        rows = await cur.fetchall()
        return [_row_to_openrouter_log(r) for r in rows]


async def clear_openrouter_logs(user_id: str) -> int:
    """Clear strictly the requesting user's personal OpenRouter call log."""
    clean_uid = _require_openrouter_user_id(user_id)
    async with connect_db() as db:
        cur = await db.execute(
            "DELETE FROM openrouter_logs WHERE user_id = ?",
            (clean_uid,),
        )
        await db.commit()
        return int(cur.rowcount or 0)


async def get_openrouter_spend_stats(user_id: str) -> dict[str, Any]:
    """Compute strictly user-scoped local Hub spend & token stats (Today UTC / 7d / 30d / Total)."""
    clean_uid = _require_openrouter_user_id(user_id)
    now_utc = datetime.now(timezone.utc)
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    week_start = (now_utc - timedelta(days=7)).isoformat()
    month_start = (now_utc - timedelta(days=30)).isoformat()

    empty_smart_titles = {
        "calls_count": 0,
        "ok_calls": 0,
        "titles_generated": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "today_usd": 0.0,
        "total_usd": 0.0,
    }

    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
                COUNT(*) AS total_calls,
                SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok_calls,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_calls,
                SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) AS blocked_calls,
                SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS today_calls,
                COALESCE(SUM(CASE WHEN status = 'ok' AND created_at >= ? THEN cost_usd ELSE 0.0 END), 0.0) AS today_usd,
                COALESCE(SUM(CASE WHEN status = 'ok' AND created_at >= ? THEN cost_usd ELSE 0.0 END), 0.0) AS week_usd,
                COALESCE(SUM(CASE WHEN status = 'ok' AND created_at >= ? THEN cost_usd ELSE 0.0 END), 0.0) AS month_usd,
                COALESCE(SUM(CASE WHEN status = 'ok' THEN cost_usd ELSE 0.0 END), 0.0) AS total_usd,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                COALESCE(SUM(CASE WHEN created_at >= ? THEN prompt_tokens + completion_tokens ELSE 0 END), 0) AS today_tokens,
                COALESCE(AVG(CASE WHEN status = 'ok' AND latency_ms > 0 THEN latency_ms END), 0) AS avg_latency_ms
            FROM openrouter_logs
            WHERE user_id = ?
            """,
            (today_start, today_start, week_start, month_start, today_start, clean_uid),
        )
        row = await cur.fetchone()

        cur_purpose = await db.execute(
            """
            SELECT
                COALESCE(NULLIF(TRIM(purpose), ''), 'mcp_chat') AS purpose_key,
                COUNT(*) AS calls_count,
                COALESCE(SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END), 0) AS ok_calls,
                COALESCE(SUM(CASE WHEN status = 'ok' THEN items_count ELSE 0 END), 0) AS ok_items_count,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(CASE WHEN status = 'ok' AND created_at >= ? THEN cost_usd ELSE 0.0 END), 0.0) AS today_usd,
                COALESCE(SUM(CASE WHEN status = 'ok' THEN cost_usd ELSE 0.0 END), 0.0) AS total_usd
            FROM openrouter_logs
            WHERE user_id = ?
            GROUP BY purpose_key
            """,
            (today_start, clean_uid),
        )
        purpose_rows = await cur_purpose.fetchall()

        by_purpose: dict[str, dict[str, Any]] = {}
        st_calls = 0
        st_ok_calls = 0
        st_titles = 0
        st_prompt_tok = 0
        st_comp_tok = 0
        st_today_usd = 0.0
        st_total_usd = 0.0
        for pr in purpose_rows:
            pkey = str(pr["purpose_key"] or "mcp_chat")
            p_calls = int(pr["calls_count"] or 0)
            p_ok = int(pr["ok_calls"] or 0)
            p_items = int(pr["ok_items_count"] or 0)
            p_ptok = int(pr["prompt_tokens"] or 0)
            p_ctok = int(pr["completion_tokens"] or 0)
            p_today = float(pr["today_usd"] or 0.0)
            p_total = float(pr["total_usd"] or 0.0)
            by_purpose[pkey] = {
                "calls_count": p_calls,
                "ok_calls": p_ok,
                "items_count": p_items,
                "prompt_tokens": p_ptok,
                "completion_tokens": p_ctok,
                "today_usd": round(p_today, 6),
                "total_usd": round(p_total, 6),
                "cost_usd": round(p_total, 6),
            }
            if pkey in ("outline_title", "outline_title_batch"):
                st_calls += p_calls
                st_ok_calls += p_ok
                st_titles += p_items
                st_prompt_tok += p_ptok
                st_comp_tok += p_ctok
                st_today_usd += p_today
                st_total_usd += p_total

        smart_titles_summary = {
            "calls_count": st_calls,
            "ok_calls": st_ok_calls,
            "titles_generated": st_titles,
            "prompt_tokens": st_prompt_tok,
            "completion_tokens": st_comp_tok,
            "today_usd": round(st_today_usd, 6),
            "total_usd": round(st_total_usd, 6),
        }

        if not row:
            return {
                "total_calls": 0,
                "ok_calls": 0,
                "error_calls": 0,
                "blocked_calls": 0,
                "today_calls": 0,
                "today_usd": 0.0,
                "week_usd": 0.0,
                "month_usd": 0.0,
                "total_usd": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "today_tokens": 0,
                "total_tokens": 0,
                "avg_latency_ms": 0,
                "by_purpose": by_purpose,
                "smart_titles": empty_smart_titles,
            }
        prompt_tok = int(row["prompt_tokens"] or 0)
        comp_tok = int(row["completion_tokens"] or 0)
        return {
            "total_calls": int(row["total_calls"] or 0),
            "ok_calls": int(row["ok_calls"] or 0),
            "error_calls": int(row["error_calls"] or 0),
            "blocked_calls": int(row["blocked_calls"] or 0),
            "today_calls": int(row["today_calls"] or 0),
            "today_usd": round(float(row["today_usd"] or 0.0), 6),
            "week_usd": round(float(row["week_usd"] or 0.0), 6),
            "month_usd": round(float(row["month_usd"] or 0.0), 6),
            "total_usd": round(float(row["total_usd"] or 0.0), 6),
            "prompt_tokens": prompt_tok,
            "completion_tokens": comp_tok,
            "reasoning_tokens": int(row["reasoning_tokens"] or 0),
            "cached_tokens": int(row["cached_tokens"] or 0),
            "today_tokens": int(row["today_tokens"] or 0),
            "total_tokens": prompt_tok + comp_tok,
            "avg_latency_ms": int(round(float(row["avg_latency_ms"] or 0.0))),
            "by_purpose": by_purpose,
            "smart_titles": smart_titles_summary,
        }


async def get_openrouter_models_summary(user_id: str) -> list[dict[str, Any]]:
    """Aggregate per-model usage summary strictly for `user_id`."""
    clean_uid = _require_openrouter_user_id(user_id)
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
                COALESCE(NULLIF(resolved_model, ''), NULLIF(requested_model, ''), 'openrouter/auto') AS model_key,
                COUNT(*) AS calls_count,
                SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok_count,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count,
                SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) AS blocked_count,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                COALESCE(SUM(cost_usd), 0.0) AS total_cost_usd,
                COALESCE(AVG(CASE WHEN status = 'ok' AND latency_ms > 0 THEN latency_ms END), 0) AS avg_latency_ms,
                MAX(created_at) AS last_used_at,
                GROUP_CONCAT(DISTINCT CASE WHEN provider_name != '' THEN provider_name END) AS providers_csv
            FROM openrouter_logs
            WHERE user_id = ?
            GROUP BY model_key
            ORDER BY calls_count DESC, total_cost_usd DESC
            """,
            (clean_uid,),
        )
        rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            providers_raw = str(r["providers_csv"] or "")
            providers = [p.strip() for p in providers_raw.split(",") if p.strip()]
            p_tok = int(r["prompt_tokens"] or 0)
            c_tok = int(r["completion_tokens"] or 0)
            out.append(
                {
                    "model": str(r["model_key"] or "openrouter/auto"),
                    "calls_count": int(r["calls_count"] or 0),
                    "ok_count": int(r["ok_count"] or 0),
                    "error_count": int(r["error_count"] or 0),
                    "blocked_count": int(r["blocked_count"] or 0),
                    "prompt_tokens": p_tok,
                    "completion_tokens": c_tok,
                    "reasoning_tokens": int(r["reasoning_tokens"] or 0),
                    "cached_tokens": int(r["cached_tokens"] or 0),
                    "total_tokens": p_tok + c_tok,
                    "total_cost_usd": round(float(r["total_cost_usd"] or 0.0), 6),
                    "avg_latency_ms": int(round(float(r["avg_latency_ms"] or 0.0))),
                    "last_used_at": str(r["last_used_at"] or ""),
                    "providers": providers,
                }
            )
        return out


async def deduct_user_openrouter_cached_spend(user_id: str, cost_usd: float) -> None:
    """Optimistically deduct `cost_usd` from cached OpenRouter balances without resetting `cached_at` TTL."""
    clean_uid = _require_openrouter_user_id(user_id)
    try:
        spend = float(cost_usd or 0.0)
    except (TypeError, ValueError):
        return
    if spend <= 0.0:
        return

    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT cached_status_json FROM user_openrouter_configs WHERE user_id = ? LIMIT 1",
            (clean_uid,),
        )
        row = await cur.fetchone()
        if not row:
            return
        raw_json = str(row["cached_status_json"] or "")
        if not raw_json:
            return
        try:
            cached = json.loads(raw_json)
        except Exception:
            return
        if not isinstance(cached, dict) or not cached:
            return

        if cached.get("credits_remaining") is not None:
            cached["credits_remaining"] = round(
                max(0.0, float(cached["credits_remaining"]) - spend), 6
            )
        if cached.get("total_credits_usage") is not None:
            cached["total_credits_usage"] = round(
                float(cached["total_credits_usage"]) + spend, 6
            )
        if cached.get("limit_remaining") is not None:
            cached["limit_remaining"] = round(
                max(0.0, float(cached["limit_remaining"]) - spend), 6
            )
        cached["usage"] = round(float(cached.get("usage") or 0.0) + spend, 6)
        cached["usage_daily"] = round(float(cached.get("usage_daily") or 0.0) + spend, 6)

        await db.execute(
            """
            UPDATE user_openrouter_configs
            SET cached_status_json = ?
            WHERE user_id = ?
            """,
            (json.dumps(cached, ensure_ascii=False), clean_uid),
        )
        await db.commit()


async def get_openrouter_sidebar_summary(user_id: str) -> dict[str, Any]:
    """Enriched strictly-personal OpenRouter card summary for `/api/system/info`, `/api/quotas` & WS push."""
    clean_uid = _require_openrouter_user_id(user_id)
    cfg = await get_user_openrouter_config(clean_uid)
    stats = await get_openrouter_spend_stats(clean_uid)
    cached = cfg.get("cached_status") or {}
    now_utc = datetime.now(timezone.utc)
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    cached_at = str(cfg.get("cached_at") or "")
    upstream_daily = (
        float(cached.get("usage_daily") or 0.0)
        if cached_at >= today_start and cached.get("usage_daily") is not None
        else 0.0
    )
    today_spend = max(float(stats.get("today_usd") or 0.0), upstream_daily)
    total_spend = max(
        float(stats.get("total_usd") or 0.0),
        float(cached.get("usage") or 0.0),
    )
    return {
        "has_key": bool(cfg.get("has_key")),
        "is_enabled": bool(cfg.get("is_enabled")),
        "key_mask": str(cfg.get("key_mask") or ""),
        "default_model": str(cfg.get("default_model") or ""),
        "label": str(cached.get("label") or ""),
        "is_free_tier": cached.get("is_free_tier"),
        "cached_at": cached_at,
        "total_credits": cached.get("total_credits"),
        "total_credits_usage": cached.get("total_credits_usage"),
        "credits_remaining": cached.get("credits_remaining"),
        "limit": cached.get("limit"),
        "limit_remaining": cached.get("limit_remaining"),
        "limit_reset": str(cached.get("limit_reset") or ""),
        "usage": cached.get("usage"),
        "usage_daily": cached.get("usage_daily"),
        "usage_monthly": cached.get("usage_monthly"),
        "today_spend_usd": round(today_spend, 6),
        "total_spend_usd": round(total_spend, 6),
        "daily_spend_limit_usd": float(cfg.get("daily_spend_limit_usd") or 0.0),
        "max_price_per_m": float(cfg.get("max_price_per_m") or 0.0),
        "smart_titles_enabled": bool(cfg.get("smart_titles_enabled")),
        "smart_titles_model": str(
            cfg.get("smart_titles_model") or DEFAULT_OPENROUTER_SMART_TITLES_MODEL
        ),
        "smart_titles_spend": stats.get("smart_titles") or {},
        "today_tokens": int(stats.get("today_tokens") or 0),
        "total_tokens": int(stats.get("total_tokens") or 0),
        "today_calls": int(stats.get("today_calls") or 0),
        "total_calls": int(stats.get("total_calls") or 0),
    }


_OUTLINE_XML_BLOCK_PATTERNS = (
    re.compile(r"(?<!`)<CONTEXT_SUMMARY>[\s\S]*?</CONTEXT_SUMMARY>(?!`)", re.IGNORECASE),
    re.compile(r"(?<!`)<PROJECT_PULSE\b[^>]*>[\s\S]*?</PROJECT_PULSE>(?!`)", re.IGNORECASE),
    re.compile(r"(?<!`)<REFERENCED_CHAT\b[^>]*>[\s\S]*?</REFERENCED_CHAT>(?!`)", re.IGNORECASE),
    re.compile(r"(?<!`)<TASK_PROFILE\b[^>]*>[\s\S]*?</TASK_PROFILE>(?!`)", re.IGNORECASE),
    re.compile(r"(?<!`)<ADDITIONAL_METADATA>[\s\S]*?</ADDITIONAL_METADATA>(?!`)", re.IGNORECASE),
    re.compile(r"(?<!`)<USER_SETTINGS_CHANGE>[\s\S]*?</USER_SETTINGS_CHANGE>(?!`)", re.IGNORECASE),
    re.compile(r"(?<!`)</?USER_REQUEST>(?!`)", re.IGNORECASE),
)

_OUTLINE_LEADING_BANNER_PATTERNS = (
    re.compile(r"^\s*\[КОНТЕКСТ ПРЕРВАННОГО ХОДА:[^\]]*\]\s*", re.IGNORECASE),
    re.compile(
        r"^\s*---\s*\n+(?=\s*\[(?:Новое сообщение пользователя|Контур выполнения Antigravity Hub))",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*\[Новое сообщение пользователя[^\]]*\]\s*", re.IGNORECASE),
    re.compile(r"^\s*\[Контур выполнения Antigravity Hub[\s\S]*?\]\s*", re.IGNORECASE),
    re.compile(
        r"^\s*\[(?:Предыдущий ход был (?:прерван|остановлен)|Внимание:\s*предыдущий ход)[^\]]*\]\s*",
        re.IGNORECASE,
    ),
)

_OUTLINE_COMMENTED_REPORT_RE = re.compile(
    r"^([\s\S]+?)\n+\[?\s*↩?\s*Отчёт\s+(?:о\s+выполнении|в\s+Штаб)\s+из\s+рабочей\s+вкладки\s+«([^»\n]+)»",
    re.IGNORECASE,
)

_OUTLINE_ENVELOPE_FIRST_LINE_PATTERNS = (
    re.compile(r"^\[?\s*↩?\s*Отчёт\s+(?:о\s+выполнении|в\s+Штаб)(?=$|[\s:—–\-\]])", re.IGNORECASE),
    re.compile(r"^#{0,6}\s*↩\s*Отчёт(?:\s+в\s+Штаб|\s+о\s+выполнении)?(?=$|[\s:—–\-])", re.IGNORECASE),
    re.compile(r"^#{0,6}\s*Отчёт\s+(?:в\s+Штаб|о\s+выполнении)(?=$|[\s:—–\-])", re.IGNORECASE),
    re.compile(r"^Реализуй\s+утверждённый\s+план(?:\s+из\s+диалога\s+«[^»]+»)?\s*:?", re.IGNORECASE),
    re.compile(r"^#{0,6}\s*🚀\s*ТЗ(?=$|[\s(:—–\-])", re.IGNORECASE),
    re.compile(r"^#{1,6}\s*(?:🚀\s*)?(?:ТЗ|Техническое\s+задание)(?=$|[\s(:—–\-])", re.IGNORECASE),
    re.compile(
        r"^(?:🚀\s*)?(?:Техническое\s+задание|ТЗ\s*(?:\([^)]+\))?\s*[:—–\-])",
        re.IGNORECASE,
    ),
)

_OUTLINE_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~")


def extract_outline_title_source_text(raw_content: str) -> str:
    """Extract clean user prompt text suitable for AI outline title generation (or '' if ineligible)."""
    if not raw_content:
        return ""
    cleaned = str(raw_content).replace("\r\n", "\n").replace("\r", "\n")
    for pat in _OUTLINE_XML_BLOCK_PATTERNS:
        cleaned = pat.sub("\n", cleaned)

    for _ in range(6):
        prev = cleaned
        for bpat in _OUTLINE_LEADING_BANNER_PATTERNS:
            cleaned = bpat.sub("", cleaned)
        if cleaned == prev:
            break

    cleaned = cleaned.strip()
    if not cleaned:
        return ""

    fwd_match = _FWD_BLOCK_DB_RE.search(cleaned)
    if fwd_match:
        before_fwd = cleaned[: fwd_match.start()].strip()
        if before_fwd and not _FWD_AUTO_FRAME_DB_RE.match(before_fwd):
            cleaned = before_fwd
        else:
            inner_fwd = _FWD_ITEM_HEADER_DB_RE.sub("", fwd_match.group(1) or "")
            inner_fwd = re.sub(r"^\s*---\s*$", "", inner_fwd, flags=re.MULTILINE).strip()
            cleaned = inner_fwd
        if not cleaned:
            return ""

    if _OUTLINE_COMMENTED_REPORT_RE.match(cleaned):
        return ""

    without_fences = _OUTLINE_FENCED_CODE_RE.sub("\n", cleaned).strip()
    check_text = without_fences if without_fences else cleaned
    lines = [ln.strip() for ln in check_text.split("\n") if ln.strip()]
    if lines:
        first_line = lines[0]
        first_no_md = re.sub(r"(\*\*|__|\*|_|~~|`)", "", re.sub(r"^#{1,6}\s+", "", first_line)).strip()
        for epat in _OUTLINE_ENVELOPE_FIRST_LINE_PATTERNS:
            if epat.match(first_line) or epat.match(first_no_md):
                return ""

    collapsed = re.sub(r"\s+", " ", without_fences).strip()
    if len(collapsed) < 24:
        return ""
    if len(collapsed) > 480:
        collapsed = f"{collapsed[:340].rstrip()} … {collapsed[-120:].lstrip()}"
    return collapsed


async def list_user_outline_title_candidates(
    user_id: str,
    *,
    include_existing_ai: bool = False,
) -> dict[str, Any]:
    """List strictly RBAC-scoped user messages eligible for AI outline title generation."""
    clean_uid = _require_openrouter_user_id(user_id)

    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur_u = await db.execute(
            "SELECT role FROM users WHERE id = ? LIMIT 1",
            (clean_uid,),
        )
        u_row = await cur_u.fetchone()
        is_admin = bool(u_row and str(u_row["role"] or "") == "admin")

        cur_sp = await db.execute("SELECT name, member_ids_json FROM shared_projects;")
        shared_by_name_lower: dict[str, set[str]] = {}
        for sp_row in await cur_sp.fetchall():
            sp_name = str(sp_row["name"] or "").strip().lower()
            if not sp_name:
                continue
            try:
                mids = json.loads(str(sp_row["member_ids_json"] or "[]"))
                member_set = {str(x) for x in mids if x} if isinstance(mids, list) else set()
            except Exception:
                member_set = set()
            shared_by_name_lower[sp_name] = member_set

        cur = await db.execute(
            """
            SELECT m.id, m.chat_id, m.user_id, m.content, m.custom_title, m.auto_title,
                   c.owner_id AS chat_owner_id, c.kind AS chat_kind,
                   c.title AS chat_title, c.project_name AS project_name
            FROM messages m
            JOIN chats c ON c.id = m.chat_id
            WHERE m.role = 'user'
              AND m.status != 'compacted'
              AND (
                    (m.user_id != '' AND m.user_id = ?)
                    OR c.owner_id = ?
                  )
            ORDER BY m.id ASC
            """,
            (clean_uid, clean_uid),
        )
        rows = await cur.fetchall()

    manual_custom_count = 0
    missing_ai_items: list[dict[str, Any]] = []
    existing_ai_items: list[dict[str, Any]] = []

    for r in rows:
        chat_kind = str(r["chat_kind"] or "chat")
        chat_owner = str(r["chat_owner_id"] or "admin")
        msg_uid = str(r["user_id"] or "").strip()
        proj_name = str(r["project_name"] or "").strip()
        sp_members = (
            shared_by_name_lower.get(proj_name.lower())
            if chat_kind == "project" and proj_name
            else None
        )
        if sp_members is not None:
            if not (is_admin or clean_uid in sp_members):
                continue
            if not ((msg_uid != "" and msg_uid == clean_uid) or (msg_uid == "" and chat_owner == clean_uid)):
                continue
        else:
            if chat_owner != clean_uid:
                continue
            if msg_uid not in ("", clean_uid, "admin"):
                continue

        if str(r["custom_title"] or "").strip() != "":
            manual_custom_count += 1
            continue

        snippet = extract_outline_title_source_text(str(r["content"] or ""))
        if not snippet:
            continue

        has_existing_ai = str(r["auto_title"] or "").strip() != ""
        item_dict = {
            "id": int(r["id"]),
            "chat_id": str(r["chat_id"]),
            "chat_title": str(r["chat_title"] or ""),
            "project_name": proj_name,
            "chat_kind": chat_kind,
            "snippet": snippet,
            "has_existing_ai": has_existing_ai,
        }
        if has_existing_ai:
            existing_ai_items.append(item_dict)
        else:
            missing_ai_items.append(item_dict)

    selected_items = (
        (missing_ai_items + existing_ai_items) if include_existing_ai else missing_ai_items
    )
    return {
        "eligible_count": len(selected_items),
        "existing_ai_count": len(existing_ai_items),
        "manual_custom_count": manual_custom_count,
        "items": selected_items,
    }


# ---------------------------------------------------------------------------
# Personal ChatGPT (Codex CLI) Multi-Account Crypto Vault & Telemetry (v79)
# ---------------------------------------------------------------------------

CHATGPT_PBKDF2_ITERATIONS = 200_000


def _require_chatgpt_user_id(user_id: str) -> str:
    clean_uid = (user_id or "").strip()
    if not clean_uid:
        raise ValueError("Не указан идентификатор пользователя ChatGPT")
    return clean_uid


def _derive_chatgpt_fernet(user_id: str) -> Fernet:
    """Derive a deterministic per-user Fernet key via PBKDF2-HMAC-SHA256 bound to `user_id`."""
    clean_uid = _require_chatgpt_user_id(user_id)
    salt = b"agy-hub-chatgpt-v1:" + clean_uid.encode("utf-8")
    secret = get_or_create_hub_secret()
    return _pbkdf2_fernet_cached(secret, salt, int(CHATGPT_PBKDF2_ITERATIONS))


def encrypt_chatgpt_auth_json(user_id: str, raw_auth_json: str) -> str:
    clean = (raw_auth_json or "").strip()
    if not clean:
        return ""
    fernet = _derive_chatgpt_fernet(user_id)
    return fernet.encrypt(clean.encode("utf-8")).decode("ascii")


def decrypt_chatgpt_auth_json(user_id: str, encrypted_auth_json: str) -> str:
    token = (encrypted_auth_json or "").strip()
    if not token:
        return ""
    try:
        fernet = _derive_chatgpt_fernet(user_id)
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, Exception):
        return ""


def _row_to_public_chatgpt_account(
    row: aiosqlite.Row,
    *,
    include_decrypted_proxy: bool = False,
) -> dict[str, Any]:
    """Convert SQLite row to a strictly public account dict (NEVER exposes `auth_json_encrypted`, `proxy_spec_encrypted`, or tokens)."""
    d = dict(row)
    d.pop("auth_json_encrypted", None)
    p_enc = str(d.pop("proxy_spec_encrypted", "") or "").strip()

    raw_quotas = str(d.pop("cached_quotas_json", "{}") or "{}")
    try:
        quotas = json.loads(raw_quotas) if raw_quotas else {}
        if not isinstance(quotas, dict):
            quotas = {}
    except Exception:
        quotas = {}

    raw_models = str(d.pop("cached_models_json", "[]") or "[]")
    try:
        models = json.loads(raw_models) if raw_models else []
        if not isinstance(models, list):
            models = []
    except Exception:
        models = []

    uid = str(d.get("user_id") or "")
    p_mode = "custom" if str(d.get("proxy_mode") or "global").strip().lower() == "custom" else "global"
    p_lbl = str(d.get("proxy_label") or "") if p_mode == "custom" else ""

    plan_raw = str(d.get("plan_type") or quotas.get("plan_type") or "").strip().lower()
    out: dict[str, Any] = {
        "id": str(d.get("id") or ""),
        "user_id": uid,
        "chatgpt_account_id": str(d.get("chatgpt_account_id") or ""),
        "email": str(d.get("email") or ""),
        "label": str(d.get("label") or ""),
        "plan_type": plan_raw,
        "is_active": bool(int(d.get("is_active", 0) or 0)),
        "is_enabled": bool(int(d.get("is_enabled", 1) or 0)),
        "auto_failover": bool(int(d.get("auto_failover", 1) or 0)),
        "proxy_mode": p_mode,
        "proxy_label": p_lbl,
        "has_custom_proxy": bool(p_mode == "custom" and p_enc),
        "has_proxy_spec": bool(p_mode == "custom" and p_enc),
        "quotas": quotas,
        "models": models,
        "cached_at": str(d.get("cached_at") or ""),
        "last_error": str(d.get("last_error") or ""),
        "sort_order": int(d.get("sort_order", 0) or 0),
        "created_at": str(d.get("created_at") or ""),
        "updated_at": str(d.get("updated_at") or ""),
    }
    if include_decrypted_proxy:
        if p_mode == "custom":
            dec = decrypt_scoped_proxy_spec(f"chatgpt:{uid}", p_enc) if (uid and p_enc) else ""
            out["proxy_spec"] = dec
            out["proxy_spec_corrupted"] = bool(not p_enc or not dec)
        else:
            out["proxy_spec"] = ""
            out["proxy_spec_corrupted"] = False
    return out


async def list_user_chatgpt_accounts(
    user_id: str,
    *,
    include_decrypted_proxy: bool = False,
) -> list[dict[str, Any]]:
    """List all connected personal ChatGPT accounts belonging strictly to `user_id`."""
    clean_uid = _require_chatgpt_user_id(user_id)
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT *
            FROM user_chatgpt_accounts
            WHERE user_id = ?
            ORDER BY sort_order ASC, created_at ASC
            """,
            (clean_uid,),
        )
        rows = await cur.fetchall()
        return [
            _row_to_public_chatgpt_account(r, include_decrypted_proxy=include_decrypted_proxy)
            for r in rows
        ]


async def get_user_chatgpt_account(
    user_id: str,
    account_id: str,
    *,
    include_decrypted_proxy: bool = False,
) -> Optional[dict[str, Any]]:
    clean_uid = _require_chatgpt_user_id(user_id)
    clean_aid = (account_id or "").strip()
    if not clean_aid:
        return None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM user_chatgpt_accounts WHERE id = ? AND user_id = ? LIMIT 1",
            (clean_aid, clean_uid),
        )
        row = await cur.fetchone()
        return (
            _row_to_public_chatgpt_account(row, include_decrypted_proxy=include_decrypted_proxy)
            if row
            else None
        )


async def get_active_user_chatgpt_account(
    user_id: str,
    *,
    require_enabled: bool = False,
    include_decrypted_proxy: bool = False,
) -> Optional[dict[str, Any]]:
    clean_uid = _require_chatgpt_user_id(user_id)
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        sql = """
            SELECT *
            FROM user_chatgpt_accounts
            WHERE user_id = ?
        """
        if require_enabled:
            sql += " AND is_enabled = 1"
        sql += " ORDER BY is_active DESC, is_enabled DESC, sort_order ASC, created_at ASC LIMIT 1"
        cur = await db.execute(sql, (clean_uid,))
        row = await cur.fetchone()
        if not row:
            return None
        # Only return if user actually has an active account (or promote if orphaned)
        pub = _row_to_public_chatgpt_account(row, include_decrypted_proxy=include_decrypted_proxy)
        if require_enabled and not pub["is_active"]:
            # If the explicitly active account is paused (`is_enabled=0`), do NOT silently bypass pause!
            cur_act = await db.execute(
                "SELECT is_enabled FROM user_chatgpt_accounts WHERE user_id = ? AND is_active = 1 LIMIT 1",
                (clean_uid,),
            )
            act_row = await cur_act.fetchone()
            if act_row and int(act_row[0] or 0) == 0:
                return None
        return pub


async def get_user_chatgpt_account_secret(
    user_id: str,
    account_id: str,
    *,
    include_decrypted_proxy: bool = False,
) -> Optional[dict[str, Any]]:
    """Internal backend-only accessor: decrypts `auth.json` in RAM strictly for `user_id`."""
    clean_uid = _require_chatgpt_user_id(user_id)
    clean_aid = (account_id or "").strip()
    if not clean_aid:
        return None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM user_chatgpt_accounts WHERE id = ? AND user_id = ? LIMIT 1",
            (clean_aid, clean_uid),
        )
        row = await cur.fetchone()
        if not row:
            return None
        enc = str(row["auth_json_encrypted"] or "")
        raw_auth = decrypt_chatgpt_auth_json(clean_uid, enc)
        if not raw_auth:
            return None
        pub = _row_to_public_chatgpt_account(row, include_decrypted_proxy=include_decrypted_proxy)
        pub["auth_json"] = raw_auth
        return pub


async def upsert_user_chatgpt_account(
    user_id: str,
    *,
    raw_auth_json: str,
    chatgpt_account_id: str = "",
    email: str = "",
    label: str = "",
    plan_type: str = "",
    quotas: Optional[dict[str, Any]] = None,
    models: Optional[list[dict[str, Any]]] = None,
    make_active: bool = True,
    proxy_mode: Optional[str] = None,
    proxy_spec: Optional[str] = None,
    proxy_label: Optional[str] = None,
) -> dict[str, Any]:
    """Create or re-authenticate a personal ChatGPT account for `user_id`.
    Deduplicates by `(user_id, chatgpt_account_id)` when non-empty, else `(user_id, lower(email))`."""
    clean_uid = _require_chatgpt_user_id(user_id)
    clean_auth = (raw_auth_json or "").strip()
    if not clean_auth:
        raise ValueError("Пустой файл авторизации auth.json")
    try:
        parsed_auth = json.loads(clean_auth)
        if not isinstance(parsed_auth, dict):
            raise ValueError("Некорректная структура JSON")
    except Exception as exc:
        raise ValueError(f"Некорректный JSON авторизации ChatGPT: {exc}") from exc

    enc_auth = encrypt_chatgpt_auth_json(clean_uid, clean_auth)
    clean_cgpt_id = (chatgpt_account_id or "").strip()[:160]
    clean_email = (email or "").strip()[:160]
    clean_label = (label or "").strip()[:80]
    clean_plan = (plan_type or "").strip().lower()[:40]
    now = utc_now_iso()

    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        existing = None
        if clean_cgpt_id:
            cur = await db.execute(
                "SELECT * FROM user_chatgpt_accounts WHERE user_id = ? AND chatgpt_account_id = ? LIMIT 1",
                (clean_uid, clean_cgpt_id),
            )
            existing = await cur.fetchone()
        if existing is None and clean_email:
            cur = await db.execute(
                """
                SELECT * FROM user_chatgpt_accounts
                WHERE user_id = ? AND LOWER(email) = LOWER(?)
                  AND (chatgpt_account_id = '' OR ? = '')
                LIMIT 1
                """,
                (clean_uid, clean_email, clean_cgpt_id),
            )
            existing = await cur.fetchone()

        p_mode, p_enc, p_lbl = _prepare_account_proxy_columns(
            scope_id=f"chatgpt:{clean_uid}",
            existing_row=dict(existing) if existing else None,
            proxy_mode=proxy_mode,
            proxy_spec=proxy_spec,
            proxy_label=proxy_label,
        )

        cur_all = await db.execute(
            "SELECT id, is_active, auto_failover, sort_order FROM user_chatgpt_accounts WHERE user_id = ? ORDER BY sort_order ASC",
            (clean_uid,),
        )
        existing_rows = await cur_all.fetchall()
        has_any_active = any(int(r["is_active"] or 0) == 1 for r in existing_rows)
        default_failover = (
            int(existing_rows[0]["auto_failover"])
            if existing_rows
            else 1
        )
        max_sort = max((int(r["sort_order"] or 0) for r in existing_rows), default=-1)

        should_activate = bool(make_active or not has_any_active)
        if should_activate:
            await db.execute(
                "UPDATE user_chatgpt_accounts SET is_active = 0 WHERE user_id = ?",
                (clean_uid,),
            )

        if existing is not None:
            acc_id = str(existing["id"])
            next_label = clean_label if clean_label else str(existing["label"] or "")
            next_email = clean_email if clean_email else str(existing["email"] or "")
            next_cgpt_id = clean_cgpt_id if clean_cgpt_id else str(existing["chatgpt_account_id"] or "")
            next_plan = clean_plan if clean_plan else str(existing["plan_type"] or "")
            next_quotas_json = (
                json.dumps(quotas, ensure_ascii=False)
                if isinstance(quotas, dict)
                else str(existing["cached_quotas_json"] or "{}")
            )
            next_models_json = (
                json.dumps(models, ensure_ascii=False)
                if isinstance(models, list)
                else str(existing["cached_models_json"] or "[]")
            )
            next_cached_at = (
                now
                if (quotas is not None or models is not None)
                else str(existing["cached_at"] or "")
            )
            next_active = 1 if should_activate else int(existing["is_active"] or 0)
            await db.execute(
                """
                UPDATE user_chatgpt_accounts
                SET chatgpt_account_id = ?,
                    email = ?,
                    label = ?,
                    plan_type = ?,
                    auth_json_encrypted = ?,
                    is_active = ?,
                    is_enabled = 1,
                    proxy_mode = ?,
                    proxy_spec_encrypted = ?,
                    proxy_label = ?,
                    cached_quotas_json = ?,
                    cached_models_json = ?,
                    cached_at = ?,
                    last_error = '',
                    updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    next_cgpt_id,
                    next_email,
                    next_label,
                    next_plan,
                    enc_auth,
                    next_active,
                    p_mode,
                    p_enc,
                    p_lbl,
                    next_quotas_json,
                    next_models_json,
                    next_cached_at,
                    now,
                    acc_id,
                    clean_uid,
                ),
            )
        else:
            acc_id = f"cgpt_{secrets.token_hex(6)}"
            quotas_json = json.dumps(quotas, ensure_ascii=False) if isinstance(quotas, dict) else "{}"
            models_json = json.dumps(models, ensure_ascii=False) if isinstance(models, list) else "[]"
            cached_at_val = now if (quotas is not None or models is not None) else ""
            await db.execute(
                """
                INSERT INTO user_chatgpt_accounts (
                    id, user_id, chatgpt_account_id, email, label, plan_type,
                    auth_json_encrypted, is_active, is_enabled, auto_failover,
                    proxy_mode, proxy_spec_encrypted, proxy_label,
                    cached_quotas_json, cached_models_json, cached_at, last_error,
                    sort_order, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
                """,
                (
                    acc_id,
                    clean_uid,
                    clean_cgpt_id,
                    clean_email,
                    clean_label,
                    clean_plan,
                    enc_auth,
                    1 if should_activate else 0,
                    default_failover,
                    p_mode,
                    p_enc,
                    p_lbl,
                    quotas_json,
                    models_json,
                    cached_at_val,
                    max_sort + 1,
                    now,
                    now,
                ),
            )

        await db.commit()
        cur_out = await db.execute(
            "SELECT * FROM user_chatgpt_accounts WHERE id = ? AND user_id = ? LIMIT 1",
            (acc_id, clean_uid),
        )
        row_out = await cur_out.fetchone()
        return _row_to_public_chatgpt_account(row_out)


async def sync_user_chatgpt_account_auth_json(
    user_id: str,
    account_id: str,
    raw_auth_json: str,
) -> bool:
    """Re-encrypt updated `auth.json` back into SQLite after Codex CLI refreshes OAuth tokens."""
    clean_uid = _require_chatgpt_user_id(user_id)
    clean_aid = (account_id or "").strip()
    clean_auth = (raw_auth_json or "").strip()
    if not clean_aid or not clean_auth:
        return False
    try:
        parsed = json.loads(clean_auth)
        if not isinstance(parsed, dict):
            return False
    except Exception:
        return False

    enc = encrypt_chatgpt_auth_json(clean_uid, clean_auth)
    async with connect_db() as db:
        cur = await db.execute(
            """
            UPDATE user_chatgpt_accounts
            SET auth_json_encrypted = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (enc, utc_now_iso(), clean_aid, clean_uid),
        )
        await db.commit()
        return cur.rowcount > 0


async def activate_user_chatgpt_account(
    user_id: str,
    account_id: str,
) -> Optional[dict[str, Any]]:
    """Switch active ChatGPT account for `user_id`. Returns `None` if account does not belong to `user_id`."""
    clean_uid = _require_chatgpt_user_id(user_id)
    clean_aid = (account_id or "").strip()
    if not clean_aid:
        return None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM user_chatgpt_accounts WHERE id = ? AND user_id = ? LIMIT 1",
            (clean_aid, clean_uid),
        )
        target = await cur.fetchone()
        if not target:
            return None
        now = utc_now_iso()
        await db.execute(
            "UPDATE user_chatgpt_accounts SET is_active = 0 WHERE user_id = ?",
            (clean_uid,),
        )
        await db.execute(
            "UPDATE user_chatgpt_accounts SET is_active = 1, updated_at = ? WHERE id = ? AND user_id = ?",
            (now, clean_aid, clean_uid),
        )
        await db.commit()
        cur_after = await db.execute(
            "SELECT * FROM user_chatgpt_accounts WHERE id = ? AND user_id = ? LIMIT 1",
            (clean_aid, clean_uid),
        )
        row_after = await cur_after.fetchone()
        return _row_to_public_chatgpt_account(row_after) if row_after else None


async def patch_user_chatgpt_account(
    user_id: str,
    account_id: str,
    *,
    label: Optional[str] = None,
    is_enabled: Optional[bool] = None,
    auto_failover: Optional[bool] = None,
    proxy_mode: Optional[str] = None,
    proxy_spec: Optional[str] = None,
    proxy_label: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Update label, pause state (`is_enabled`), `auto_failover`, or proxy route strictly for `user_id`."""
    clean_uid = _require_chatgpt_user_id(user_id)
    clean_aid = (account_id or "").strip()
    if not clean_aid:
        return None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM user_chatgpt_accounts WHERE id = ? AND user_id = ? LIMIT 1",
            (clean_aid, clean_uid),
        )
        existing = await cur.fetchone()
        if not existing:
            return None

        fields: list[str] = []
        params: list[Any] = []
        now = utc_now_iso()
        if label is not None:
            fields.append("label = ?")
            params.append(label.strip()[:80])
        if is_enabled is not None:
            fields.append("is_enabled = ?")
            params.append(1 if bool(is_enabled) else 0)
        if proxy_mode is not None or proxy_spec is not None:
            p_mode, p_enc, p_lbl = _prepare_account_proxy_columns(
                scope_id=f"chatgpt:{clean_uid}",
                existing_row=dict(existing),
                proxy_mode=proxy_mode,
                proxy_spec=proxy_spec,
                proxy_label=proxy_label,
            )
            fields.extend(["proxy_mode = ?", "proxy_spec_encrypted = ?", "proxy_label = ?"])
            params.extend([p_mode, p_enc, p_lbl])
        if auto_failover is not None:
            fo_val = 1 if bool(auto_failover) else 0
            await db.execute(
                "UPDATE user_chatgpt_accounts SET auto_failover = ?, updated_at = ? WHERE user_id = ?",
                (fo_val, now, clean_uid),
            )

        if fields:
            fields.append("updated_at = ?")
            params.append(now)
            params.extend([clean_aid, clean_uid])
            await db.execute(
                f"UPDATE user_chatgpt_accounts SET {', '.join(fields)} WHERE id = ? AND user_id = ?",
                params,
            )

        await db.commit()
        cur_after = await db.execute(
            "SELECT * FROM user_chatgpt_accounts WHERE id = ? AND user_id = ? LIMIT 1",
            (clean_aid, clean_uid),
        )
        row_after = await cur_after.fetchone()
        return _row_to_public_chatgpt_account(row_after) if row_after else None


async def update_user_chatgpt_account_cache(
    user_id: str,
    account_id: str,
    *,
    quotas: Optional[dict[str, Any]] = None,
    models: Optional[list[dict[str, Any]]] = None,
    plan_type: Optional[str] = None,
    email: Optional[str] = None,
    chatgpt_account_id: Optional[str] = None,
    last_error: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    clean_uid = _require_chatgpt_user_id(user_id)
    clean_aid = (account_id or "").strip()
    if not clean_aid:
        return None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM user_chatgpt_accounts WHERE id = ? AND user_id = ? LIMIT 1",
            (clean_aid, clean_uid),
        )
        existing = await cur.fetchone()
        if not existing:
            return None

        now = utc_now_iso()
        fields: list[str] = ["updated_at = ?"]
        params: list[Any] = [now]

        if quotas is not None and isinstance(quotas, dict):
            fields.append("cached_quotas_json = ?")
            params.append(json.dumps(quotas, ensure_ascii=False))
            fields.append("cached_at = ?")
            params.append(now)
        if models is not None and isinstance(models, list) and len(models) > 0:
            fields.append("cached_models_json = ?")
            params.append(json.dumps(models, ensure_ascii=False))
        if plan_type is not None and plan_type.strip():
            fields.append("plan_type = ?")
            params.append(plan_type.strip().lower()[:40])
        if email is not None and email.strip():
            fields.append("email = ?")
            params.append(email.strip()[:160])
        if chatgpt_account_id is not None and chatgpt_account_id.strip():
            fields.append("chatgpt_account_id = ?")
            params.append(chatgpt_account_id.strip()[:160])
        if last_error is not None:
            fields.append("last_error = ?")
            params.append(last_error.strip()[:400])

        params.extend([clean_aid, clean_uid])
        await db.execute(
            f"UPDATE user_chatgpt_accounts SET {', '.join(fields)} WHERE id = ? AND user_id = ?",
            params,
        )
        await db.commit()
        cur_after = await db.execute(
            "SELECT * FROM user_chatgpt_accounts WHERE id = ? AND user_id = ? LIMIT 1",
            (clean_aid, clean_uid),
        )
        row_after = await cur_after.fetchone()
        return _row_to_public_chatgpt_account(row_after) if row_after else None


async def delete_user_chatgpt_account(user_id: str, account_id: str) -> bool:
    """Delete a ChatGPT account strictly owned by `user_id`, promote next account if active,
    and wipe `data/cli_homes/<safe_uid>/.codex/accounts/<account_id>/` from disk."""
    clean_uid = _require_chatgpt_user_id(user_id)
    clean_aid = (account_id or "").strip()
    if not clean_aid:
        return False

    was_active = False
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, is_active FROM user_chatgpt_accounts WHERE id = ? AND user_id = ? LIMIT 1",
            (clean_aid, clean_uid),
        )
        row = await cur.fetchone()
        if not row:
            return False
        was_active = bool(int(row["is_active"] or 0))
        await db.execute(
            "DELETE FROM user_chatgpt_accounts WHERE id = ? AND user_id = ?",
            (clean_aid, clean_uid),
        )
        if was_active:
            cur_next = await db.execute(
                """
                SELECT id FROM user_chatgpt_accounts
                WHERE user_id = ?
                ORDER BY is_enabled DESC, sort_order ASC, created_at ASC
                LIMIT 1
                """,
                (clean_uid,),
            )
            nxt = await cur_next.fetchone()
            if nxt:
                await db.execute(
                    "UPDATE user_chatgpt_accounts SET is_active = 1, updated_at = ? WHERE id = ? AND user_id = ?",
                    (utc_now_iso(), str(nxt["id"]), clean_uid),
                )
        await db.execute(
            "UPDATE chats SET codex_thread_id = '', codex_account_id = '' WHERE owner_id = ? AND codex_account_id = ?",
            (clean_uid, clean_aid),
        )
        await db.execute(
            "UPDATE chats SET preferred_codex_account_id = '' WHERE owner_id = ? AND preferred_codex_account_id = ?",
            (clean_uid, clean_aid),
        )
        await db.commit()
        invalidate_chat_list_cache()

    # Wipe isolated CODEX_HOME directory for this account
    try:
        codex_dir = ensure_user_codex_home(clean_uid)
        accounts_root = (codex_dir / "accounts").resolve()
        safe_acc = re.sub(r"[^a-zA-Z0-9_.\-]+", "_", clean_aid).strip(".-")[:80]
        if safe_acc and safe_acc not in {".", ".."}:
            acc_dir = (accounts_root / safe_acc).resolve()
            acc_dir.relative_to(accounts_root)
            from . import config as _config
            if not acc_dir.is_relative_to(_config.CLI_HOMES_DIR.resolve()):
                raise ValueError("Account directory outside CLI_HOMES_DIR")
            if acc_dir.exists():
                shutil.rmtree(acc_dir, ignore_errors=True)
    except Exception:
        pass

    return True


async def get_chatgpt_token_usage_stats(user_id: str) -> dict[str, Any]:
    """Compute personal ChatGPT (`codex:*` / `provider == "chatgpt_codex"`) token telemetry
    strictly for `user_id` across 5h, 24h, 7d, and all-time windows."""
    clean_uid = _require_chatgpt_user_id(user_id)
    now = datetime.now(timezone.utc)
    cut_5h = now - timedelta(hours=5)
    cut_24h = now - timedelta(hours=24)
    cut_7d = now - timedelta(days=7)

    win_5h = _empty_token_bucket()
    win_24h = _empty_token_bucket()
    win_7d = _empty_token_bucket()
    all_time = _empty_token_bucket()
    by_model_map: dict[str, dict[str, Any]] = {}

    # v110: агрегация по in-memory ledger (строки с пустым usage_json дают 0 токенов и
    # раньше всё равно отбрасывались по `tot_check <= 0`).
    for _cid, r_user_id, r_model, u, dt, tot_check in await _get_usage_ledger():
        if r_user_id != clean_uid:
            continue
        m_id = r_model.strip()
        is_cgpt = m_id.startswith("codex:") or (
            str(u.get("provider") or "").strip().lower() == "chatgpt_codex"
        )
        if not is_cgpt:
            continue

        _accumulate_bucket(all_time, u)
        in_7d = bool(dt and dt >= cut_7d)
        in_24h = bool(dt and dt >= cut_24h)
        in_5h = bool(dt and dt >= cut_5h)

        if in_7d:
            _accumulate_bucket(win_7d, u)
        if in_24h:
            _accumulate_bucket(win_24h, u)
        if in_5h:
            _accumulate_bucket(win_5h, u)

        model_key = m_id or "codex:unknown"
        if model_key not in by_model_map:
            entry = _empty_token_bucket()
            entry["model"] = model_key
            entry["tokens_24h"] = 0
            entry["tokens_7d"] = 0
            by_model_map[model_key] = entry
        m_entry = by_model_map[model_key]
        _accumulate_bucket(m_entry, u)
        if in_24h:
            m_entry["tokens_24h"] += tot_check
        if in_7d:
            m_entry["tokens_7d"] += tot_check

    by_model_list = sorted(
        by_model_map.values(),
        key=lambda x: (x["total_tokens"], x["turns"]),
        reverse=True,
    )
    return {
        "window_5h": win_5h,
        "window_24h": win_24h,
        "window_7d": win_7d,
        "all_time": all_time,
        "by_model": by_model_list,
    }


async def get_chatgpt_sidebar_summary(user_id: str) -> dict[str, Any]:
    """Strictly user-scoped ChatGPT summary for `/api/system/info`, `/api/quotas`,
    `#sidebarChatGptBtn`, `#mobChatGptPillBtn`, `#settingsChatGptBlock` & WS updates."""
    clean_uid = _require_chatgpt_user_id(user_id)
    accounts = await list_user_chatgpt_accounts(clean_uid)
    tok_stats = await get_chatgpt_token_usage_stats(clean_uid)
    cli_info = get_codex_cli_info(probe_version=False)

    active_acc: Optional[dict[str, Any]] = None
    active_index = 0
    for idx, acc in enumerate(accounts):
        if acc.get("is_active"):
            active_acc = acc
            active_index = idx + 1
            break
    if active_acc is None and accounts:
        active_acc = accounts[0]
        active_index = 1

    quotas = (active_acc.get("quotas") or {}) if active_acc else {}
    auto_failover = bool(active_acc.get("auto_failover", True)) if active_acc else True

    return {
        "has_account": bool(accounts),
        "account_count": len(accounts),
        "active_index": active_index,
        "active_account_id": str(active_acc["id"]) if active_acc else "",
        "email": str(active_acc.get("email") or "") if active_acc else "",
        "label": str(active_acc.get("label") or "") if active_acc else "",
        "plan_type": str(active_acc.get("plan_type") or quotas.get("plan_type") or "") if active_acc else "",
        "is_enabled": bool(active_acc.get("is_enabled", True)) if active_acc else False,
        "auto_failover": auto_failover,
        "proxy_mode": str(active_acc.get("proxy_mode") or "global") if active_acc else "global",
        "proxy_label": str(active_acc.get("proxy_label") or "") if active_acc else "",
        "has_custom_proxy": bool(active_acc.get("has_custom_proxy")) if active_acc else False,
        "quotas": quotas,
        "cached_at": str(active_acc.get("cached_at") or "") if active_acc else "",
        "last_error": str(active_acc.get("last_error") or "") if active_acc else "",
        "accounts_brief": [
            {
                "id": a["id"],
                "email": a["email"],
                "label": a["label"],
                "plan_type": a["plan_type"],
                "is_active": a["is_active"],
                "is_enabled": a["is_enabled"],
                "proxy_mode": a.get("proxy_mode", "global"),
                "proxy_label": a.get("proxy_label", ""),
                "has_custom_proxy": bool(a.get("has_custom_proxy")),
                "quotas": a["quotas"],
            }
            for a in accounts
        ],
        "tokens_5h": int(tok_stats["window_5h"]["total_tokens"]),
        "tokens_24h": int(tok_stats["window_24h"]["total_tokens"]),
        "tokens_7d": int(tok_stats["window_7d"]["total_tokens"]),
        "tokens_total": int(tok_stats["all_time"]["total_tokens"]),
        "cli_installed": bool(cli_info.get("installed")),
        "cli_version": str(cli_info.get("version") or ""),
    }



# ---------------------------------------------------------------------------
# Personal Claude Code CLI (Pro / Max / Team) Multi-Account Vault & Telemetry (v87)
# ---------------------------------------------------------------------------

CLAUDE_PBKDF2_ITERATIONS = 120_000
_CLAUDE_SECRET_KEYS = ("accessToken", "refreshToken", "oauthToken", "access_token", "refresh_token")


def _require_claude_user_id(user_id: str) -> str:
    clean_uid = (user_id or "").strip()
    if not clean_uid:
        raise ValueError("Не указан идентификатор пользователя Claude")
    return clean_uid


def _derive_user_claude_fernet(user_id: str) -> Fernet:
    """Per-user Fernet key: PBKDF2-HMAC-SHA256 (120k), salt = `claude_credentials_json:<uid>`."""
    clean_uid = _require_claude_user_id(user_id)
    salt = f"claude_credentials_json:{clean_uid}".encode("utf-8")
    return _pbkdf2_fernet_cached(get_or_create_hub_secret(), salt, int(CLAUDE_PBKDF2_ITERATIONS))


def encrypt_claude_credentials_json(user_id: str, raw_json: str) -> str:
    clean = (raw_json or "").strip()
    if not clean:
        return ""
    return _derive_user_claude_fernet(user_id).encrypt(clean.encode("utf-8")).decode("ascii")


def decrypt_claude_credentials_json(user_id: str, token: str) -> str:
    tok = (token or "").strip()
    if not tok:
        return ""
    try:
        return _derive_user_claude_fernet(user_id).decrypt(tok.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, Exception):
        return ""


def _mask_claude_secret(val: str) -> str:
    s = str(val or "")
    if len(s) <= 12:
        return "•••"
    return f"{s[:10]}…{s[-4:]}"


def _row_to_public_claude_account(
    row: aiosqlite.Row,
    *,
    include_decrypted_credentials: bool = False,
    include_decrypted_proxy: bool = False,
) -> dict[str, Any]:
    d = dict(row)
    enc = str(d.pop("credentials_json_encrypted", "") or "")
    p_enc = str(d.pop("proxy_spec_encrypted", "") or "").strip()
    try:
        quotas = json.loads(str(d.get("cached_quotas_json") or "{}"))
        if not isinstance(quotas, dict):
            quotas = {}
    except Exception:
        quotas = {}
    try:
        models = json.loads(str(d.get("cached_models_json") or "[]"))
        if not isinstance(models, list):
            models = []
    except Exception:
        models = []
    uid = str(d.get("user_id") or "")
    p_mode = "custom" if str(d.get("proxy_mode") or "global").strip().lower() == "custom" else "global"
    p_lbl = str(d.get("proxy_label") or "") if p_mode == "custom" else ""
    out: dict[str, Any] = {
        "id": str(d.get("id") or ""),
        "user_id": uid,
        "claude_account_id": str(d.get("claude_account_id") or ""),
        "email": str(d.get("email") or ""),
        "label": str(d.get("label") or ""),
        "plan_type": str(d.get("plan_type") or quotas.get("plan_type") or "").strip().lower(),
        "auth_method": str(d.get("auth_method") or "oauth"),
        "is_active": bool(int(d.get("is_active", 0) or 0)),
        "is_enabled": bool(int(d.get("is_enabled", 1) or 0)),
        "auto_failover": bool(int(d.get("auto_failover", 1) or 0)),
        "proxy_mode": p_mode,
        "proxy_label": p_lbl,
        "has_custom_proxy": bool(p_mode == "custom" and p_enc),
        "has_proxy_spec": bool(p_mode == "custom" and p_enc),
        "quotas": quotas,
        "models": models,
        "cached_at": str(d.get("cached_at") or ""),
        "last_error": str(d.get("last_error") or ""),
        "sort_order": int(d.get("sort_order", 0) or 0),
        "created_at": str(d.get("created_at") or ""),
        "updated_at": str(d.get("updated_at") or ""),
        "has_credentials": bool(enc),
    }
    if include_decrypted_credentials:
        out["credentials_json"] = decrypt_claude_credentials_json(uid, enc) if enc else ""
    if include_decrypted_proxy:
        if p_mode == "custom":
            dec = decrypt_scoped_proxy_spec(f"claude:{uid}", p_enc) if (uid and p_enc) else ""
            out["proxy_spec"] = dec
            out["proxy_spec_corrupted"] = bool(not p_enc or not dec)
        else:
            out["proxy_spec"] = ""
            out["proxy_spec_corrupted"] = False
    return out


async def _fetch_claude_row(db: aiosqlite.Connection, uid: str, aid: str) -> Optional[aiosqlite.Row]:
    cur = await db.execute(
        "SELECT * FROM user_claude_accounts WHERE id = ? AND user_id = ? LIMIT 1",
        (aid, uid),
    )
    return await cur.fetchone()


async def list_user_claude_accounts(
    user_id: str,
    include_decrypted_credentials: bool = False,
    *,
    include_decrypted_proxy: bool = False,
) -> list[dict[str, Any]]:
    clean_uid = _require_claude_user_id(user_id)
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM user_claude_accounts WHERE user_id = ? ORDER BY sort_order ASC, created_at ASC",
            (clean_uid,),
        )
        rows = await cur.fetchall()
    return [
        _row_to_public_claude_account(
            r,
            include_decrypted_credentials=include_decrypted_credentials,
            include_decrypted_proxy=include_decrypted_proxy,
        )
        for r in rows
    ]


async def get_user_claude_account(
    user_id: str,
    account_id: str,
    include_decrypted_credentials: bool = False,
    *,
    include_decrypted_proxy: bool = False,
) -> Optional[dict[str, Any]]:
    clean_uid = _require_claude_user_id(user_id)
    clean_aid = (account_id or "").strip()
    if not clean_aid:
        return None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        row = await _fetch_claude_row(db, clean_uid, clean_aid)
    return (
        _row_to_public_claude_account(
            row,
            include_decrypted_credentials=include_decrypted_credentials,
            include_decrypted_proxy=include_decrypted_proxy,
        )
        if row
        else None
    )


async def get_active_claude_account(
    user_id: str,
    include_decrypted_credentials: bool = False,
    *,
    require_enabled: bool = False,
    include_decrypted_proxy: bool = False,
) -> Optional[dict[str, Any]]:
    clean_uid = _require_claude_user_id(user_id)
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT * FROM user_claude_accounts WHERE user_id = ?
            ORDER BY is_active DESC, is_enabled DESC, sort_order ASC, created_at ASC LIMIT 1
            """,
            (clean_uid,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    acc = _row_to_public_claude_account(
        row,
        include_decrypted_credentials=include_decrypted_credentials,
        include_decrypted_proxy=include_decrypted_proxy,
    )
    if require_enabled and not acc["is_enabled"]:
        return None
    return acc


async def upsert_user_claude_account(
    user_id: str,
    *,
    raw_credentials_json: str,
    claude_account_id: str = "",
    email: str = "",
    label: str = "",
    plan_type: str = "",
    auth_method: str = "oauth",
    quotas: Optional[dict[str, Any]] = None,
    models: Optional[list[dict[str, Any]]] = None,
    make_active: bool = True,
    proxy_mode: Optional[str] = None,
    proxy_spec: Optional[str] = None,
    proxy_label: Optional[str] = None,
) -> dict[str, Any]:
    """Create or re-authenticate a Claude account (`cld_<hex6>`), deduplicated by account id / email."""
    clean_uid = _require_claude_user_id(user_id)
    clean_raw = (raw_credentials_json or "").strip()
    if not clean_raw:
        raise ValueError("Пустые учётные данные Claude")
    try:
        if not isinstance(json.loads(clean_raw), dict):
            raise ValueError("Некорректная структура JSON")
    except Exception as exc:
        raise ValueError(f"Некорректный JSON учётных данных Claude: {exc}") from exc

    enc = encrypt_claude_credentials_json(clean_uid, clean_raw)
    c_acc = (claude_account_id or "").strip()[:160]
    c_email = (email or "").strip()[:160]
    c_label = (label or "").strip()[:80]
    c_plan = (plan_type or "").strip().lower()[:40]
    c_method = (auth_method or "oauth").strip().lower()[:40] or "oauth"
    now = utc_now_iso()

    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        existing = None
        if c_acc:
            cur = await db.execute(
                "SELECT * FROM user_claude_accounts WHERE user_id = ? AND claude_account_id = ? LIMIT 1",
                (clean_uid, c_acc),
            )
            existing = await cur.fetchone()
        if existing is None and c_email:
            cur = await db.execute(
                "SELECT * FROM user_claude_accounts WHERE user_id = ? AND LOWER(email) = LOWER(?) LIMIT 1",
                (clean_uid, c_email),
            )
            existing = await cur.fetchone()

        p_mode, p_enc, p_lbl = _prepare_account_proxy_columns(
            scope_id=f"claude:{clean_uid}",
            existing_row=dict(existing) if existing else None,
            proxy_mode=proxy_mode,
            proxy_spec=proxy_spec,
            proxy_label=proxy_label,
        )

        cur_all = await db.execute(
            "SELECT id, is_active, auto_failover, sort_order FROM user_claude_accounts WHERE user_id = ? ORDER BY sort_order ASC",
            (clean_uid,),
        )
        rows = await cur_all.fetchall()
        has_active = any(int(r["is_active"] or 0) == 1 for r in rows)
        default_fo = int(rows[0]["auto_failover"]) if rows else 1
        max_sort = max((int(r["sort_order"] or 0) for r in rows), default=-1)
        activate = bool(make_active or not has_active)
        if activate:
            await db.execute("UPDATE user_claude_accounts SET is_active = 0 WHERE user_id = ?", (clean_uid,))

        q_json = json.dumps(quotas, ensure_ascii=False) if isinstance(quotas, dict) else None
        m_json = json.dumps(models, ensure_ascii=False) if isinstance(models, list) else None
        if existing is not None:
            acc_id = str(existing["id"])
            await db.execute(
                """
                UPDATE user_claude_accounts
                SET claude_account_id = ?, email = ?, label = ?, plan_type = ?, auth_method = ?,
                    credentials_json_encrypted = ?, is_active = ?, is_enabled = 1,
                    proxy_mode = ?, proxy_spec_encrypted = ?, proxy_label = ?,
                    cached_quotas_json = ?, cached_models_json = ?, cached_at = ?,
                    last_error = '', updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    c_acc or str(existing["claude_account_id"] or ""),
                    c_email or str(existing["email"] or ""),
                    c_label or str(existing["label"] or ""),
                    c_plan or str(existing["plan_type"] or ""),
                    c_method,
                    enc,
                    1 if activate else int(existing["is_active"] or 0),
                    p_mode,
                    p_enc,
                    p_lbl,
                    q_json if q_json is not None else str(existing["cached_quotas_json"] or "{}"),
                    m_json if m_json is not None else str(existing["cached_models_json"] or "[]"),
                    now if (q_json or m_json) else str(existing["cached_at"] or ""),
                    now,
                    acc_id,
                    clean_uid,
                ),
            )
        else:
            acc_id = f"cld_{secrets.token_hex(3)}"
            await db.execute(
                """
                INSERT INTO user_claude_accounts (
                    id, user_id, claude_account_id, email, label, plan_type, auth_method,
                    credentials_json_encrypted, is_active, is_enabled, auto_failover,
                    proxy_mode, proxy_spec_encrypted, proxy_label,
                    cached_quotas_json, cached_models_json, cached_at, last_error,
                    sort_order, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
                """,
                (
                    acc_id, clean_uid, c_acc, c_email, c_label, c_plan, c_method, enc,
                    1 if activate else 0, default_fo,
                    p_mode, p_enc, p_lbl,
                    q_json or "{}", m_json or "[]", now if (q_json or m_json) else "",
                    max_sort + 1, now, now,
                ),
            )
        await db.commit()
        row = await _fetch_claude_row(db, clean_uid, acc_id)
    return _row_to_public_claude_account(row)


async def sync_user_claude_credentials_json(user_id: str, account_id: str, raw_json: str) -> bool:
    """Re-encrypt refreshed `.credentials.json` back into SQLite."""
    clean_uid = _require_claude_user_id(user_id)
    clean_aid = (account_id or "").strip()
    clean_raw = (raw_json or "").strip()
    if not clean_aid or not clean_raw:
        return False
    try:
        if not isinstance(json.loads(clean_raw), dict):
            return False
    except Exception:
        return False
    enc = encrypt_claude_credentials_json(clean_uid, clean_raw)
    async with connect_db() as db:
        cur = await db.execute(
            "UPDATE user_claude_accounts SET credentials_json_encrypted = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (enc, utc_now_iso(), clean_aid, clean_uid),
        )
        await db.commit()
        return cur.rowcount > 0


async def activate_user_claude_account(user_id: str, account_id: str) -> Optional[dict[str, Any]]:
    clean_uid = _require_claude_user_id(user_id)
    clean_aid = (account_id or "").strip()
    if not clean_aid:
        return None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        if not await _fetch_claude_row(db, clean_uid, clean_aid):
            return None
        await db.execute("UPDATE user_claude_accounts SET is_active = 0 WHERE user_id = ?", (clean_uid,))
        await db.execute(
            "UPDATE user_claude_accounts SET is_active = 1, updated_at = ? WHERE id = ? AND user_id = ?",
            (utc_now_iso(), clean_aid, clean_uid),
        )
        await db.commit()
        row = await _fetch_claude_row(db, clean_uid, clean_aid)
    return _row_to_public_claude_account(row) if row else None


async def patch_user_claude_account(
    user_id: str,
    account_id: str,
    *,
    label: Optional[str] = None,
    is_enabled: Optional[bool] = None,
    auto_failover: Optional[bool] = None,
    proxy_mode: Optional[str] = None,
    proxy_spec: Optional[str] = None,
    proxy_label: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    clean_uid = _require_claude_user_id(user_id)
    clean_aid = (account_id or "").strip()
    if not clean_aid:
        return None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        existing = await _fetch_claude_row(db, clean_uid, clean_aid)
        if not existing:
            return None
        now = utc_now_iso()
        fields: list[str] = []
        params: list[Any] = []
        if label is not None:
            fields.append("label = ?")
            params.append(str(label).strip()[:80])
        if is_enabled is not None:
            fields.append("is_enabled = ?")
            params.append(1 if is_enabled else 0)
        if proxy_mode is not None or proxy_spec is not None:
            p_mode, p_enc, p_lbl = _prepare_account_proxy_columns(
                scope_id=f"claude:{clean_uid}",
                existing_row=dict(existing),
                proxy_mode=proxy_mode,
                proxy_spec=proxy_spec,
                proxy_label=proxy_label,
            )
            fields.extend(["proxy_mode = ?", "proxy_spec_encrypted = ?", "proxy_label = ?"])
            params.extend([p_mode, p_enc, p_lbl])
        if auto_failover is not None:
            # auto_failover is a per-user preference: keep all accounts in sync
            await db.execute(
                "UPDATE user_claude_accounts SET auto_failover = ?, updated_at = ? WHERE user_id = ?",
                (1 if auto_failover else 0, now, clean_uid),
            )
        if fields:
            fields.append("updated_at = ?")
            params.extend([now, clean_aid, clean_uid])
            await db.execute(
                f"UPDATE user_claude_accounts SET {', '.join(fields)} WHERE id = ? AND user_id = ?",
                params,
            )
        await db.commit()
        row = await _fetch_claude_row(db, clean_uid, clean_aid)
    return _row_to_public_claude_account(row) if row else None


async def update_claude_account_telemetry(
    user_id: str,
    account_id: str,
    *,
    quotas: Optional[dict[str, Any]] = None,
    models: Optional[list[dict[str, Any]]] = None,
    plan_type: Optional[str] = None,
    email: Optional[str] = None,
    last_error: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    clean_uid = _require_claude_user_id(user_id)
    clean_aid = (account_id or "").strip()
    if not clean_aid:
        return None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        if not await _fetch_claude_row(db, clean_uid, clean_aid):
            return None
        now = utc_now_iso()
        fields = ["updated_at = ?"]
        params: list[Any] = [now]
        if isinstance(quotas, dict):
            fields += ["cached_quotas_json = ?", "cached_at = ?"]
            params += [json.dumps(quotas, ensure_ascii=False), now]
        if isinstance(models, list) and models:
            fields.append("cached_models_json = ?")
            params.append(json.dumps(models, ensure_ascii=False))
        if plan_type is not None and str(plan_type).strip():
            fields.append("plan_type = ?")
            params.append(str(plan_type).strip().lower()[:40])
        if email is not None and str(email).strip():
            fields.append("email = ?")
            params.append(str(email).strip()[:160])
        if last_error is not None:
            fields.append("last_error = ?")
            params.append(str(last_error).strip()[:400])
        params += [clean_aid, clean_uid]
        await db.execute(
            f"UPDATE user_claude_accounts SET {', '.join(fields)} WHERE id = ? AND user_id = ?",
            params,
        )
        await db.commit()
        row = await _fetch_claude_row(db, clean_uid, clean_aid)
    return _row_to_public_claude_account(row) if row else None


async def delete_user_claude_account(user_id: str, account_id: str) -> bool:
    clean_uid = _require_claude_user_id(user_id)
    clean_aid = (account_id or "").strip()
    if not clean_aid:
        return False
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        row = await _fetch_claude_row(db, clean_uid, clean_aid)
        if not row:
            return False
        was_active = bool(int(row["is_active"] or 0))
        await db.execute(
            "DELETE FROM user_claude_accounts WHERE id = ? AND user_id = ?", (clean_aid, clean_uid)
        )
        if was_active:
            cur = await db.execute(
                """
                SELECT id FROM user_claude_accounts WHERE user_id = ?
                ORDER BY is_enabled DESC, sort_order ASC, created_at ASC LIMIT 1
                """,
                (clean_uid,),
            )
            nxt = await cur.fetchone()
            if nxt:
                await db.execute(
                    "UPDATE user_claude_accounts SET is_active = 1, updated_at = ? WHERE id = ? AND user_id = ?",
                    (utc_now_iso(), str(nxt["id"]), clean_uid),
                )
        await db.execute(
            "UPDATE chats SET claude_session_id = '', claude_account_id = '' WHERE claude_account_id = ?",
            (clean_aid,),
        )
        await db.execute(
            "UPDATE chats SET preferred_claude_account_id = '' WHERE preferred_claude_account_id = ?",
            (clean_aid,),
        )
        await db.commit()
        invalidate_chat_list_cache()
    try:
        accounts_root = (ensure_user_claude_home(clean_uid) / "accounts").resolve()
        safe_acc = re.sub(r"[^a-zA-Z0-9_.-]+", "_", clean_aid).strip(".-")[:80]
        if safe_acc and safe_acc not in {".", ".."}:
            acc_dir = (accounts_root / safe_acc).resolve()
            acc_dir.relative_to(accounts_root)
            from . import config as _config
            if not acc_dir.is_relative_to(_config.CLI_HOMES_DIR.resolve()):
                raise ValueError("Account directory outside CLI_HOMES_DIR")
            if acc_dir.exists():
                shutil.rmtree(acc_dir, ignore_errors=True)
    except Exception:
        pass
    return True


async def get_claude_token_usage_stats(user_id: str) -> dict[str, Any]:
    """Token telemetry for `claude-code:*` turns strictly of `user_id` (5h / 24h / 7d / all-time)."""
    clean_uid = _require_claude_user_id(user_id)
    now = datetime.now(timezone.utc)
    cuts = {
        "window_5h": now - timedelta(hours=5),
        "window_24h": now - timedelta(hours=24),
        "window_7d": now - timedelta(days=7),
    }
    buckets = {k: _empty_token_bucket() for k in cuts}
    all_time = _empty_token_bucket()
    by_model: dict[str, dict[str, Any]] = {}
    for _cid, r_user_id, r_model, u, dt, _tot in await _get_usage_ledger():
        if r_user_id != clean_uid:
            continue
        m_id = r_model.strip()
        if not (
            m_id.startswith("claude-code:")
            or str(u.get("provider") or "").strip().lower() == "claude_code"
        ):
            continue
        _accumulate_bucket(all_time, u)
        for k, cut in cuts.items():
            if dt and dt >= cut:
                _accumulate_bucket(buckets[k], u)
        mk = m_id or "claude-code:unknown"
        if mk not in by_model:
            e = _empty_token_bucket()
            e["model"] = mk
            by_model[mk] = e
        _accumulate_bucket(by_model[mk], u)
    return {
        **buckets,
        "all_time": all_time,
        "by_model": sorted(by_model.values(), key=lambda x: x["total_tokens"], reverse=True),
    }


async def get_claude_sidebar_summary(user_id: str) -> dict[str, Any]:
    """Compact user-scoped Claude Code summary for the sidebar card and `/api/system/info`."""
    clean_uid = _require_claude_user_id(user_id)
    accounts = await list_user_claude_accounts(clean_uid)
    tok = await get_claude_token_usage_stats(clean_uid)
    try:
        cli = get_claude_cli_info()
    except Exception:
        cli = {}
    active: Optional[dict[str, Any]] = None
    active_index = 0
    for idx, a in enumerate(accounts):
        if a.get("is_active"):
            active, active_index = a, idx + 1
            break
    if active is None and accounts:
        active, active_index = accounts[0], 1
    quotas = (active or {}).get("quotas") or {}
    return {
        "has_account": bool(accounts),
        "account_count": len(accounts),
        "enabled_count": sum(1 for a in accounts if a.get("is_enabled")),
        "active_account_id": str(active["id"]) if active else "",
        "active_index": active_index,
        "email": str((active or {}).get("email") or ""),
        "label": str((active or {}).get("label") or ""),
        "plan_type": str((active or {}).get("plan_type") or quotas.get("plan_type") or ""),
        "is_enabled": bool(active.get("is_enabled", True)) if active else False,
        "auto_failover": bool(active.get("auto_failover", True)) if active else True,
        "proxy_mode": str(active.get("proxy_mode") or "global") if active else "global",
        "proxy_label": str(active.get("proxy_label") or "") if active else "",
        "has_custom_proxy": bool(active.get("has_custom_proxy")) if active else False,
        "quotas": quotas,
        "cached_at": str((active or {}).get("cached_at") or ""),
        "last_error": str((active or {}).get("last_error") or ""),
        "accounts_brief": [
            {
                "id": a["id"],
                "email": a["email"],
                "label": a["label"],
                "plan_type": a["plan_type"],
                "is_active": a["is_active"],
                "is_enabled": a["is_enabled"],
                "proxy_mode": a.get("proxy_mode", "global"),
                "proxy_label": a.get("proxy_label", ""),
                "has_custom_proxy": bool(a.get("has_custom_proxy")),
                "quotas": a["quotas"],
            }
            for a in accounts
        ],
        "tokens_5h": int(tok["window_5h"]["total_tokens"]),
        "tokens_24h": int(tok["window_24h"]["total_tokens"]),
        "tokens_7d": int(tok["window_7d"]["total_tokens"]),
        "tokens_total": int(tok["all_time"]["total_tokens"]),
        "cli_installed": bool(cli.get("installed")),
        "cli_version": str(cli.get("version") or ""),
    }


async def _managed_project_chats(db, project_name, owner_id, is_shared):
    sql = "SELECT * FROM chats WHERE kind='project' AND LOWER(project_name)=LOWER(?)"
    args = [project_name]
    if not is_shared:
        sql += " AND owner_id=?"
        args.append(owner_id)
    return await (await db.execute(sql, args)).fetchall()


def _project_mirror_path(name, owner_id, is_shared, username=""):
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")[:80] or "Project"
    root = (_eff_shared_projects_dir() / "_context" if is_shared else
            _ensure_user_space(config.sanitize_user_folder_slug(username or owner_id))["context"])
    return root / (stem + ".md")


async def rename_project_records(*, old_name: str, new_name: str, old_dir: Path, new_dir: Path,
        owner_id: str, is_shared: bool, shared_id: str = "") -> dict[str, Any]:
    from .host_service_manager import _rewrite_relocated_path
    old_scope = build_project_memory_scope_key(old_name, owner_id, is_shared=is_shared)
    new_scope = build_project_memory_scope_key(new_name, owner_id, is_shared=is_shared)
    memory = None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.execute("BEGIN IMMEDIATE")
        try:
            if is_shared:
                conflict = await (await db.execute("SELECT id FROM shared_projects WHERE LOWER(name)=LOWER(?) AND id!=?", (new_name, shared_id))).fetchone()
                if conflict:
                    raise ValueError("Проект с таким именем уже существует")
                await db.execute("UPDATE shared_projects SET name=?, path=?, updated_at=? WHERE id=?", (new_name, str(new_dir), utc_now_iso(), shared_id))
            chats = await _managed_project_chats(db, old_name, owner_id, is_shared)
            for chat in chats:
                await db.execute("UPDATE chats SET project_name=?, project_path=? WHERE id=?", (new_name, str(new_dir), chat["id"]))
                messages = await (await db.execute("SELECT id, attachments_json, usage_json FROM messages WHERE chat_id=?", (chat["id"],))).fetchall()
                for msg in messages:
                    att_changed = False
                    try:
                        attachments = json.loads(msg["attachments_json"] or "[]")
                    except Exception:
                        attachments = []
                    if isinstance(attachments, list):
                        for a in attachments:
                            if isinstance(a, dict) and a.get("disk_path"):
                                new_dp, dp_changed = _rewrite_relocated_path(a["disk_path"], old_dir, new_dir)
                                if dp_changed or new_dp != a["disk_path"]:
                                    a["disk_path"] = new_dp
                                    att_changed = True
                    usage_changed = False
                    try:
                        usage = json.loads(msg["usage_json"] or "{}")
                    except Exception:
                        usage = {}
                    if isinstance(usage, dict):
                        for field in ("code_handoffs", "hq_returns"):
                            items = usage.get(field)
                            if not isinstance(items, list):
                                continue
                            for item in items:
                                if isinstance(item, dict) and str(item.get("project_name", "")).lower() == old_name.lower():
                                    if item.get("project_name") != new_name:
                                        item["project_name"] = new_name
                                        usage_changed = True
                    elif not att_changed:
                        continue
                    if att_changed and usage_changed:
                        await db.execute(
                            "UPDATE messages SET attachments_json=?, usage_json=? WHERE id=?",
                            (json.dumps(attachments, ensure_ascii=False), json.dumps(usage, ensure_ascii=False), msg["id"]),
                        )
                    elif att_changed:
                        await db.execute(
                            "UPDATE messages SET attachments_json=? WHERE id=?",
                            (json.dumps(attachments, ensure_ascii=False), msg["id"]),
                        )
                    elif usage_changed:
                        await db.execute(
                            "UPDATE messages SET usage_json=? WHERE id=?",
                            (json.dumps(usage, ensure_ascii=False), msg["id"]),
                        )
            row = await (await db.execute("SELECT * FROM project_memories WHERE scope_key=?", (old_scope,))).fetchone()
            if row or owner_id == "admin" or is_shared:
                if not row:
                    await db.execute("INSERT INTO project_memories(scope_key,owner_id,project_name,updated_at) VALUES(?,?,?,?)", (old_scope, owner_id, old_name, utc_now_iso()))
                paths = json.loads(row["migrated_from_paths_json"] or "[]") if row else []
                if (owner_id == "admin" or is_shared) and str(old_dir) not in paths:
                    paths.append(str(old_dir))
                await db.execute("UPDATE project_memories SET scope_key=?, project_name=?, migrated_from_paths_json=?, updated_at=? WHERE scope_key=?", (new_scope,new_name,json.dumps(paths,ensure_ascii=False),utc_now_iso(),old_scope))
                row = await (await db.execute("SELECT * FROM project_memories WHERE scope_key=?", (new_scope,))).fetchone()
                memory = _row_to_project_memory(row, scope_key=new_scope)
            users = await (await db.execute("SELECT id, username, projects_order_json FROM users")).fetchall()
            username = next((u["username"] for u in users if u["id"] == owner_id), "")
            for u in users:
                if is_shared or u["id"] == owner_id:
                    order = [new_name if str(n).lower() == old_name.lower() else n for n in json.loads(u["projects_order_json"] or "[]")]
                    await db.execute("UPDATE users SET projects_order_json=? WHERE id=?", (json.dumps(order,ensure_ascii=False),u["id"]))
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    old_mirror = _project_mirror_path(old_name,owner_id,is_shared,username)
    old_mirror.unlink(missing_ok=True)
    if memory:
        _sync_project_context_mirror(memory, owner_username=username)
        sync_project_rules_file(new_dir, memory)
    invalidate_chat_list_cache(None)
    invalidate_user_cache(None)
    return {"updated_chats_count": len(chats)}


async def delete_project_records(*, project_name: str, owner_id: str, is_shared: bool,
        shared_id: str = "", chats_action: str = "delete") -> dict[str, Any]:
    if chats_action not in {"delete", "move_to_regular"}:
        raise ValueError("Некорректное действие с диалогами")
    folder = project_name.strip()[:60]
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.execute("BEGIN IMMEDIATE")
        try:
            chats = await _managed_project_chats(db,project_name,owner_id,is_shared)
            for chat in chats:
                if chats_action == "move_to_regular":
                    await db.execute("UPDATE chats SET kind='chat', project_name='', project_path='', folder=? WHERE id=?", (folder,chat["id"]))
                else:
                    await db.execute("DELETE FROM chats WHERE id=?", (chat["id"],))
            if is_shared and shared_id:
                await db.execute("DELETE FROM shared_projects WHERE id=?", (shared_id,))
            await db.execute("DELETE FROM project_memories WHERE scope_key=?", (build_project_memory_scope_key(project_name,owner_id,is_shared=is_shared),))
            users = await (await db.execute("SELECT id,username,projects_order_json FROM users")).fetchall()
            username = next((u["username"] for u in users if u["id"] == owner_id), "")
            for u in users:
                if is_shared or u["id"] == owner_id:
                    order = [n for n in json.loads(u["projects_order_json"] or "[]") if str(n).lower()!=project_name.lower()]
                    await db.execute("UPDATE users SET projects_order_json=? WHERE id=?", (json.dumps(order,ensure_ascii=False),u["id"]))
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    _project_mirror_path(project_name,owner_id,is_shared,username).unlink(missing_ok=True)
    _reset_usage_ledger_cache()
    invalidate_chat_list_cache(None)
    invalidate_user_cache(None)
    return {"deleted_chats": [_row_to_chat(c) for c in chats] if chats_action=="delete" else [],
            "moved_chat_ids": [c["id"] for c in chats] if chats_action=="move_to_regular" else [], "folder": folder}


async def _relocate_chat_attachments(db, chat_id, old_dir, new_dir):
    if old_dir == new_dir:
        return
    from .host_service_manager import _rewrite_relocated_path
    rows = await (await db.execute("SELECT id,attachments_json FROM messages WHERE chat_id=?", (chat_id,))).fetchall()
    for row in rows:
        attachments = json.loads(row["attachments_json"] or "[]")
        for item in attachments:
            if isinstance(item, dict) and item.get("disk_path"):
                item["disk_path"] = _rewrite_relocated_path(item["disk_path"], old_dir, new_dir)[0]
        await db.execute("UPDATE messages SET attachments_json=? WHERE id=?",
                         (json.dumps(attachments, ensure_ascii=False), row["id"]))


async def _transfer_project_memory(db, name, owner_id, old_shared, new_shared, old_dir, new_dir):
    old_scope = build_project_memory_scope_key(name, owner_id, is_shared=old_shared)
    new_scope = build_project_memory_scope_key(name, owner_id, is_shared=new_shared)
    row = await (await db.execute("SELECT * FROM project_memories WHERE scope_key=?", (old_scope,))).fetchone()
    if not row:
        await db.execute("INSERT INTO project_memories(scope_key,owner_id,project_name,updated_at) VALUES(?,?,?,?)",
                         (old_scope, owner_id, name, utc_now_iso()))
    paths = json.loads(row["migrated_from_paths_json"] or "[]") if row else []
    if old_dir != new_dir and str(old_dir) not in paths:
        paths.append(str(old_dir))
    await db.execute("UPDATE project_memories SET scope_key=?,owner_id=?,migrated_from_paths_json=?,updated_at=? WHERE scope_key=?",
                     (new_scope, owner_id, json.dumps(paths, ensure_ascii=False), utc_now_iso(), old_scope))
    row = await (await db.execute("SELECT * FROM project_memories WHERE scope_key=?", (new_scope,))).fetchone()
    return _row_to_project_memory(row, scope_key=new_scope)


def _sync_sharing_memory(name, owner_id, username, was_shared, is_shared, memory, new_dir):
    # The database has committed: a mirror failure must never roll the directory back.
    try:
        if was_shared != is_shared:
            _project_mirror_path(name, owner_id, was_shared, username).unlink(missing_ok=True)
        _sync_project_context_mirror(memory, owner_username=username)
        sync_project_rules_file(new_dir, memory)
    except OSError:
        logging.getLogger(__name__).exception("Cannot sync project sharing memory mirror")


async def promote_or_update_shared_project_records(*, project_name: str, old_dir: Path, new_dir: Path,
        owner_id: str = "admin", was_shared: bool = False, shared_id: str = "", description: str = "",
        execution_mode: str = "docker", member_ids: Optional[list[str]] = None,
        chats_action: str = "keep_in_project") -> dict[str, Any]:
    if chats_action not in {"keep_in_project", "move_to_regular"}:
        raise ValueError("Некорректное действие с диалогами")
    mode = execution_mode if execution_mode in VALID_SHARED_EXEC_MODES else "docker"
    members = sorted({str(m).strip() for m in (member_ids or []) if str(m).strip() and str(m).strip() != "admin"})
    folder, moved = project_name.strip()[:60], []
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            now = utc_now_iso()
            if not was_shared:
                if await (await db.execute("SELECT id FROM shared_projects WHERE LOWER(name)=LOWER(?)", (project_name,))).fetchone():
                    raise ValueError("Общий проект с таким именем уже существует")
                shared_id = str(uuid.uuid4())
                await db.execute("INSERT INTO shared_projects(id,name,path,description,execution_mode,member_ids_json,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (shared_id,project_name,str(new_dir),(description or "").strip()[:240],mode,json.dumps(members),owner_id,now,now))
            else:
                cur = await db.execute("UPDATE shared_projects SET path=?,description=?,execution_mode=?,member_ids_json=?,updated_at=? WHERE id=?",
                    (str(new_dir),(description or "").strip()[:240],mode,json.dumps(members),now,shared_id))
                if cur.rowcount != 1:
                    raise ValueError("Общий проект не найден")
            chats = await _managed_project_chats(db,project_name,owner_id,was_shared)
            for chat in chats:
                if not was_shared and chats_action == "move_to_regular":
                    await db.execute("UPDATE chats SET kind='chat',project_name='',project_path='',folder=? WHERE id=?", (folder,chat["id"]))
                    moved.append(chat["id"])
                else:
                    await db.execute("UPDATE chats SET project_path=? WHERE id=?", (str(new_dir),chat["id"]))
                await _relocate_chat_attachments(db,chat["id"],old_dir,new_dir)
            memory = await _transfer_project_memory(db,project_name,owner_id,was_shared,True,old_dir,new_dir)
            user = await (await db.execute("SELECT username FROM users WHERE id=?", (owner_id,))).fetchone()
            username = user[0] if user else owner_id
            row = await (await db.execute("SELECT * FROM shared_projects WHERE id=?", (shared_id,))).fetchone()
            shared = _row_to_shared_project(row)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    _sync_sharing_memory(project_name,owner_id,username,was_shared,True,memory,new_dir)
    invalidate_chat_list_cache(None)
    return {"shared_project": shared,"moved_chat_ids": moved,"folder": folder}


async def demote_shared_project_records(*, project_name: str, shared_id: str, old_dir: Path,
        new_dir: Path, target_owner_id: str = "admin") -> dict[str, Any]:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute("DELETE FROM shared_projects WHERE id=?", (shared_id,))
            if cur.rowcount != 1:
                raise ValueError("Общий проект не найден")
            chats = await _managed_project_chats(db,project_name,target_owner_id,True)
            for chat in chats:
                await db.execute("UPDATE chats SET owner_id=?,project_path=? WHERE id=?", (target_owner_id,str(new_dir),chat["id"]))
                if chat["owner_id"] != target_owner_id:
                    await db.execute("UPDATE chats SET claude_session_id='',claude_account_id='',codex_thread_id='',codex_account_id='' WHERE id=?", (chat["id"],))
                await _relocate_chat_attachments(db,chat["id"],old_dir,new_dir)
            memory = await _transfer_project_memory(db,project_name,target_owner_id,True,False,old_dir,new_dir)
            users = await (await db.execute("SELECT id,username,projects_order_json FROM users")).fetchall()
            username = next((u["username"] for u in users if u["id"] == target_owner_id), target_owner_id)
            for user in users:
                if user["id"] != target_owner_id:
                    order = [n for n in json.loads(user["projects_order_json"] or "[]") if str(n).lower() != project_name.lower()]
                    await db.execute("UPDATE users SET projects_order_json=? WHERE id=?", (json.dumps(order,ensure_ascii=False),user["id"]))
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    _sync_sharing_memory(project_name,target_owner_id,username,True,False,memory,new_dir)
    invalidate_chat_list_cache(None)
    invalidate_user_cache(None)
    return {"moved_chat_ids": [], "folder": project_name.strip()[:60]}


# ---------------------------------------------------------------------------
# Google Drive Unidirectional Cloud Mirror Config & Sync Index (v151)
# ---------------------------------------------------------------------------

DEFAULT_GDRIVE_ROOT_FOLDER_NAME = "ZeroHub"


def _require_gdrive_user_id(user_id: str) -> str:
    clean_uid = str(user_id or "").strip()
    if not clean_uid:
        raise ValueError("Не указан идентификатор пользователя Google Drive")
    return clean_uid


def _derive_gdrive_fernet(user_id: str) -> Fernet:
    """Derive a domain-isolated per-user Fernet key (`gdrive_oauth:<user_id>`) via PBKDF2-HMAC-SHA256."""
    clean_uid = _require_gdrive_user_id(user_id)
    salt = f"gdrive_oauth:{clean_uid}".encode("utf-8")
    return _pbkdf2_fernet_cached(get_or_create_hub_secret(), salt, PBKDF2_ITERATIONS)


def encrypt_gdrive_secret(user_id: str, plaintext: str) -> str:
    clean = str(plaintext or "")
    if not clean:
        return ""
    return _derive_gdrive_fernet(user_id).encrypt(clean.encode("utf-8")).decode("ascii")


def decrypt_gdrive_secret(user_id: str, ciphertext: str) -> str:
    token = str(ciphertext or "").strip()
    if not token:
        return ""
    try:
        return _derive_gdrive_fernet(user_id).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, Exception):
        return ""


def encrypt_gdrive_json(user_id: str, payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict) or not payload:
        return ""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return encrypt_gdrive_secret(user_id, raw)


def decrypt_gdrive_json(user_id: str, ciphertext: str) -> Optional[dict[str, Any]]:
    raw = decrypt_gdrive_secret(user_id, ciphertext)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _mask_gdrive_client_id(client_id: str) -> str:
    clean = str(client_id or "").strip()
    if not clean:
        return ""
    if len(clean) <= 16:
        return f"{clean[:4]}••••"
    return f"{clean[:10]}••••{clean[-8:]}"


def _normalize_gdrive_excluded_projects(raw: Any) -> list[str]:
    parsed: Any = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw) if raw.strip() else []
        except Exception:
            parsed = []
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        name = str(item or "").strip()
        if not name:
            continue
        low = name.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(name[:120])
    return out


def _default_gdrive_config_dict(user_id: str, *, include_secrets: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "user_id": user_id,
        "has_oauth_client": False,
        "oauth_client_id_mask": "",
        "connected": False,
        "account_email": "",
        "root_folder_id": "",
        "root_folder_name": DEFAULT_GDRIVE_ROOT_FOLDER_NAME,
        "is_enabled": False,
        "sync_self_hub": True,
        "excluded_projects": [],
        "include_env_vault": False,
        "has_vault_passphrase": False,
        "last_sync_at": "",
        "last_sync_status": "",
        "last_sync_error": "",
        "last_sync_summary": {},
        "updated_at": "",
    }
    if include_secrets:
        out["oauth_client"] = None
        out["tokens"] = None
        out["vault_passphrase"] = ""
    return out


def _row_to_gdrive_config(
    row: Optional[aiosqlite.Row],
    user_id: str,
    *,
    include_secrets: bool = False,
) -> dict[str, Any]:
    if row is None:
        return _default_gdrive_config_dict(user_id, include_secrets=include_secrets)
    d = dict(row)
    uid = str(d.get("user_id") or user_id)
    oauth_client = decrypt_gdrive_json(uid, str(d.get("oauth_client_encrypted") or ""))
    has_oauth_client = bool(
        isinstance(oauth_client, dict)
        and str(oauth_client.get("client_id") or "").strip()
        and str(oauth_client.get("client_secret") or "").strip()
    )
    tokens = decrypt_gdrive_json(uid, str(d.get("tokens_encrypted") or ""))
    connected = bool(
        isinstance(tokens, dict)
        and (
            str(tokens.get("refresh_token") or "").strip()
            or str(tokens.get("access_token") or "").strip()
        )
    )
    vault_passphrase = decrypt_gdrive_secret(uid, str(d.get("vault_passphrase_encrypted") or ""))
    has_vault_passphrase = bool(vault_passphrase)

    raw_summary = str(d.get("last_sync_summary_json") or "{}")
    try:
        parsed_summary = json.loads(raw_summary) if raw_summary.strip() else {}
        if not isinstance(parsed_summary, dict):
            parsed_summary = {}
    except Exception:
        parsed_summary = {}

    root_name = str(d.get("root_folder_name") or "").strip() or DEFAULT_GDRIVE_ROOT_FOLDER_NAME
    out: dict[str, Any] = {
        "user_id": uid,
        "has_oauth_client": has_oauth_client,
        "oauth_client_id_mask": _mask_gdrive_client_id(str(oauth_client.get("client_id") or "")) if has_oauth_client else "",
        "connected": connected,
        "account_email": str(d.get("account_email") or "").strip(),
        "root_folder_id": str(d.get("root_folder_id") or "").strip(),
        "root_folder_name": root_name,
        "is_enabled": bool(int(d.get("is_enabled", 0) or 0)),
        "sync_self_hub": bool(int(d.get("sync_self_hub", 1) or 0)),
        "excluded_projects": _normalize_gdrive_excluded_projects(d.get("excluded_projects_json", "[]")),
        "include_env_vault": bool(int(d.get("include_env_vault", 0) or 0)),
        "has_vault_passphrase": has_vault_passphrase,
        "last_sync_at": str(d.get("last_sync_at") or ""),
        "last_sync_status": str(d.get("last_sync_status") or ""),
        "last_sync_error": str(d.get("last_sync_error") or ""),
        "last_sync_summary": parsed_summary,
        "updated_at": str(d.get("updated_at") or ""),
    }
    if include_secrets:
        out["oauth_client"] = oauth_client if has_oauth_client else None
        out["tokens"] = tokens if connected else None
        out["vault_passphrase"] = vault_passphrase
    return out


async def get_gdrive_config(
    user_id: str,
    *,
    include_secrets: bool = False,
) -> dict[str, Any]:
    clean_uid = _require_gdrive_user_id(user_id)
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM gdrive_configs WHERE user_id = ? LIMIT 1",
            (clean_uid,),
        )
        row = await cur.fetchone()
        return _row_to_gdrive_config(row, clean_uid, include_secrets=include_secrets)


async def list_enabled_gdrive_configs(
    *,
    include_secrets: bool = False,
) -> list[dict[str, Any]]:
    """Return Google Drive configs with auto-sync enabled (`is_enabled = 1`) and stored tokens."""
    out: list[dict[str, Any]] = []
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT * FROM gdrive_configs
            WHERE is_enabled = 1 AND tokens_encrypted != ''
            ORDER BY user_id ASC
            """
        )
        for row in await cur.fetchall():
            uid = str(row["user_id"] or "").strip()
            if not uid:
                continue
            cfg = _row_to_gdrive_config(row, uid, include_secrets=include_secrets)
            if cfg.get("is_enabled") and cfg.get("connected"):
                out.append(cfg)
    return out


async def save_gdrive_config(
    user_id: str,
    *,
    oauth_client: Optional[dict[str, Any]] = None,
    clear_oauth_client: bool = False,
    tokens: Optional[dict[str, Any]] = None,
    clear_tokens: bool = False,
    account_email: Optional[str] = None,
    root_folder_id: Optional[str] = None,
    root_folder_name: Optional[str] = None,
    is_enabled: Optional[bool] = None,
    sync_self_hub: Optional[bool] = None,
    excluded_projects: Optional[list[str]] = None,
    include_env_vault: Optional[bool] = None,
    vault_passphrase: Optional[str] = None,
    clear_vault_passphrase: bool = False,
    last_sync_at: Optional[str] = None,
    last_sync_status: Optional[str] = None,
    last_sync_error: Optional[str] = None,
    last_sync_summary: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    clean_uid = _require_gdrive_user_id(user_id)
    now = utc_now_iso()
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM gdrive_configs WHERE user_id = ? LIMIT 1",
            (clean_uid,),
        )
        existing_row = await cur.fetchone()
        ex = dict(existing_row) if existing_row else {}

        if clear_oauth_client:
            new_oauth_enc = ""
        elif oauth_client is not None:
            new_oauth_enc = encrypt_gdrive_json(clean_uid, oauth_client)
        else:
            new_oauth_enc = str(ex.get("oauth_client_encrypted") or "")

        if clear_tokens:
            new_tokens_enc = ""
        elif tokens is not None:
            new_tokens_enc = encrypt_gdrive_json(clean_uid, tokens)
        else:
            new_tokens_enc = str(ex.get("tokens_encrypted") or "")

        new_email = (
            str(account_email).strip()[:240]
            if account_email is not None
            else str(ex.get("account_email") or "")
        )
        new_root_id = (
            str(root_folder_id).strip()[:200]
            if root_folder_id is not None
            else str(ex.get("root_folder_id") or "")
        )
        if root_folder_name is not None:
            cleaned_rname = re.sub(r"[\x00-\x1f/\\]+", " ", str(root_folder_name)).strip()[:120]
            new_root_name = cleaned_rname or DEFAULT_GDRIVE_ROOT_FOLDER_NAME
        else:
            new_root_name = str(ex.get("root_folder_name") or "").strip() or DEFAULT_GDRIVE_ROOT_FOLDER_NAME

        new_is_enabled = (
            int(bool(is_enabled))
            if is_enabled is not None
            else int(ex.get("is_enabled", 0) or 0)
        )
        new_sync_self = (
            int(bool(sync_self_hub))
            if sync_self_hub is not None
            else int(ex.get("sync_self_hub", 1) if ex else 1)
        )
        new_excluded_json = (
            json.dumps(_normalize_gdrive_excluded_projects(excluded_projects), ensure_ascii=False)
            if excluded_projects is not None
            else str(ex.get("excluded_projects_json") or "[]")
        )
        new_include_vault = (
            int(bool(include_env_vault))
            if include_env_vault is not None
            else int(ex.get("include_env_vault", 0) or 0)
        )

        if clear_vault_passphrase:
            new_vault_pass_enc = ""
        elif vault_passphrase is not None:
            new_vault_pass_enc = encrypt_gdrive_secret(clean_uid, vault_passphrase)
        else:
            new_vault_pass_enc = str(ex.get("vault_passphrase_encrypted") or "")

        new_last_at = (
            str(last_sync_at)
            if last_sync_at is not None
            else str(ex.get("last_sync_at") or "")
        )
        new_last_status = (
            str(last_sync_status)[:64]
            if last_sync_status is not None
            else str(ex.get("last_sync_status") or "")
        )
        new_last_err = (
            str(last_sync_error)[:1200]
            if last_sync_error is not None
            else str(ex.get("last_sync_error") or "")
        )
        new_last_summary_json = (
            json.dumps(last_sync_summary, ensure_ascii=False)
            if last_sync_summary is not None
            else str(ex.get("last_sync_summary_json") or "{}")
        )

        await db.execute(
            """
            INSERT INTO gdrive_configs (
                user_id, oauth_client_encrypted, tokens_encrypted, account_email,
                root_folder_id, root_folder_name, is_enabled, sync_self_hub,
                excluded_projects_json, include_env_vault, vault_passphrase_encrypted,
                last_sync_at, last_sync_status, last_sync_error, last_sync_summary_json,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                oauth_client_encrypted = excluded.oauth_client_encrypted,
                tokens_encrypted = excluded.tokens_encrypted,
                account_email = excluded.account_email,
                root_folder_id = excluded.root_folder_id,
                root_folder_name = excluded.root_folder_name,
                is_enabled = excluded.is_enabled,
                sync_self_hub = excluded.sync_self_hub,
                excluded_projects_json = excluded.excluded_projects_json,
                include_env_vault = excluded.include_env_vault,
                vault_passphrase_encrypted = excluded.vault_passphrase_encrypted,
                last_sync_at = excluded.last_sync_at,
                last_sync_status = excluded.last_sync_status,
                last_sync_error = excluded.last_sync_error,
                last_sync_summary_json = excluded.last_sync_summary_json,
                updated_at = excluded.updated_at
            """,
            (
                clean_uid,
                new_oauth_enc,
                new_tokens_enc,
                new_email,
                new_root_id,
                new_root_name,
                new_is_enabled,
                new_sync_self,
                new_excluded_json,
                new_include_vault,
                new_vault_pass_enc,
                new_last_at,
                new_last_status,
                new_last_err,
                new_last_summary_json,
                now,
            ),
        )
        await db.commit()
        cur2 = await db.execute(
            "SELECT * FROM gdrive_configs WHERE user_id = ? LIMIT 1",
            (clean_uid,),
        )
        saved_row = await cur2.fetchone()
        return _row_to_gdrive_config(saved_row, clean_uid, include_secrets=False)


async def update_gdrive_sync_status(
    user_id: str,
    *,
    status: str,
    error: str = "",
    summary: Optional[dict[str, Any]] = None,
    root_folder_id: Optional[str] = None,
    last_sync_at: Optional[str] = None,
) -> dict[str, Any]:
    return await save_gdrive_config(
        user_id,
        root_folder_id=root_folder_id,
        last_sync_at=last_sync_at if last_sync_at is not None else utc_now_iso(),
        last_sync_status=status,
        last_sync_error=error,
        last_sync_summary=summary,
    )


async def disconnect_gdrive_account(
    user_id: str,
    *,
    clear_index: bool = False,
    clear_root_folder: bool = False,
) -> dict[str, Any]:
    clean_uid = _require_gdrive_user_id(user_id)
    if clear_index:
        await clear_gdrive_sync_items(clean_uid)
    return await save_gdrive_config(
        clean_uid,
        clear_tokens=True,
        account_email="",
        root_folder_id="" if clear_root_folder else None,
        is_enabled=False,
    )


async def list_gdrive_sync_items(user_id: str) -> dict[str, dict[str, Any]]:
    clean_uid = _require_gdrive_user_id(user_id)
    out: dict[str, dict[str, Any]] = {}
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT user_id, logical_path, item_kind, sha256, size_bytes,
                   drive_file_id, drive_parent_id, synced_at
            FROM gdrive_sync_items
            WHERE user_id = ?
            """,
            (clean_uid,),
        )
        for row in await cur.fetchall():
            d = dict(row)
            lp = str(d.get("logical_path") or "")
            if lp:
                out[lp] = {
                    "user_id": str(d.get("user_id") or clean_uid),
                    "logical_path": lp,
                    "item_kind": str(d.get("item_kind") or ""),
                    "sha256": str(d.get("sha256") or ""),
                    "size_bytes": int(d.get("size_bytes") or 0),
                    "drive_file_id": str(d.get("drive_file_id") or ""),
                    "drive_parent_id": str(d.get("drive_parent_id") or ""),
                    "synced_at": str(d.get("synced_at") or ""),
                }
    return out


async def upsert_gdrive_sync_item(
    user_id: str,
    *,
    logical_path: str,
    item_kind: str,
    sha256: str,
    size_bytes: int,
    drive_file_id: str,
    drive_parent_id: str,
    synced_at: Optional[str] = None,
) -> None:
    clean_uid = _require_gdrive_user_id(user_id)
    ts = synced_at or utc_now_iso()
    async with connect_db() as db:
        await db.execute(
            """
            INSERT INTO gdrive_sync_items (
                user_id, logical_path, item_kind, sha256, size_bytes,
                drive_file_id, drive_parent_id, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, logical_path) DO UPDATE SET
                item_kind = excluded.item_kind,
                sha256 = excluded.sha256,
                size_bytes = excluded.size_bytes,
                drive_file_id = excluded.drive_file_id,
                drive_parent_id = excluded.drive_parent_id,
                synced_at = excluded.synced_at
            """,
            (
                clean_uid,
                str(logical_path),
                str(item_kind),
                str(sha256),
                int(size_bytes),
                str(drive_file_id),
                str(drive_parent_id),
                ts,
            ),
        )
        await db.commit()


async def delete_gdrive_sync_items(user_id: str, logical_paths: list[str]) -> int:
    clean_uid = _require_gdrive_user_id(user_id)
    paths = [str(p) for p in (logical_paths or []) if str(p)]
    if not paths:
        return 0
    removed = 0
    async with connect_db() as db:
        # Chunk to stay comfortably below SQLite bind-parameter limit
        for idx in range(0, len(paths), 200):
            chunk = paths[idx : idx + 200]
            placeholders = ", ".join("?" for _ in chunk)
            cur = await db.execute(
                f"DELETE FROM gdrive_sync_items WHERE user_id = ? AND logical_path IN ({placeholders})",
                [clean_uid, *chunk],
            )
            removed += max(0, int(cur.rowcount or 0))
        await db.commit()
    return removed


async def clear_gdrive_sync_items(user_id: str) -> int:
    clean_uid = _require_gdrive_user_id(user_id)
    async with connect_db() as db:
        cur = await db.execute(
            "DELETE FROM gdrive_sync_items WHERE user_id = ?",
            (clean_uid,),
        )
        await db.commit()
        return max(0, int(cur.rowcount or 0))


_VALID_ADVISOR_SOURCES = ("seed", "cohort", "openrouter", "auto_ai")


def _row_to_model_advisor_entry(row: Any) -> dict[str, Any]:
    d = dict(row)
    raw_json = str(d.get("passport_json") or "{}")
    try:
        parsed_passport = json.loads(raw_json)
        if not isinstance(parsed_passport, dict):
            parsed_passport = {}
    except Exception:
        parsed_passport = {}
    src = str(d.get("source") or "cohort").strip().lower()
    if src not in _VALID_ADVISOR_SOURCES:
        src = "cohort"
    return {
        "series_key": str(d.get("series_key") or "").strip().lower(),
        "vendor": str(d.get("vendor") or "").strip().lower(),
        "display_name": str(d.get("display_name") or "").strip(),
        "source": src,
        "passport": parsed_passport,
        "passport_json": raw_json,
        "calibrated_at": str(d.get("calibrated_at") or ""),
        "retry_after": str(d.get("retry_after") or ""),
        "last_error": str(d.get("last_error") or ""),
        "updated_at": str(d.get("updated_at") or ""),
    }


async def list_model_advisor_catalog() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT series_key, vendor, display_name, source, passport_json,
                   calibrated_at, retry_after, last_error, updated_at
            FROM model_advisor_catalog
            ORDER BY series_key ASC
            """
        )
        for row in await cur.fetchall():
            item = _row_to_model_advisor_entry(row)
            if item["series_key"]:
                out.append(item)
    return out


async def get_model_advisor_entry(series_key: str) -> Optional[dict[str, Any]]:
    clean_key = str(series_key or "").strip().lower()[:120]
    if not clean_key:
        return None
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT series_key, vendor, display_name, source, passport_json,
                   calibrated_at, retry_after, last_error, updated_at
            FROM model_advisor_catalog
            WHERE series_key = ?
            LIMIT 1
            """,
            (clean_key,),
        )
        row = await cur.fetchone()
        return _row_to_model_advisor_entry(row) if row is not None else None


async def upsert_model_advisor_entry(
    series_key: str,
    *,
    vendor: Optional[str] = None,
    display_name: Optional[str] = None,
    source: Optional[str] = None,
    passport: Optional[dict[str, Any]] = None,
    passport_json: Optional[str] = None,
    calibrated_at: Optional[str] = None,
    retry_after: Optional[str] = None,
    last_error: Optional[str] = None,
    updated_at: Optional[str] = None,
) -> dict[str, Any]:
    clean_key = str(series_key or "").strip().lower()[:120]
    if not clean_key:
        raise ValueError("series_key обязателен для сохранения паспорта модели")
    now = updated_at or utc_now_iso()
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM model_advisor_catalog WHERE series_key = ? LIMIT 1",
            (clean_key,),
        )
        existing = await cur.fetchone()
        ex = dict(existing) if existing is not None else {}

        new_vendor = (
            str(vendor).strip().lower()[:40]
            if vendor is not None
            else str(ex.get("vendor") or "")
        )
        new_display = (
            str(display_name).strip()[:120]
            if display_name is not None
            else str(ex.get("display_name") or "")
        )
        raw_src = (
            str(source).strip().lower()
            if source is not None
            else str(ex.get("source") or "cohort")
        )
        new_source = raw_src if raw_src in _VALID_ADVISOR_SOURCES else "cohort"
        if passport_json is not None:
            new_passport_json = str(passport_json)
        elif passport is not None:
            new_passport_json = json.dumps(passport, ensure_ascii=False)
        else:
            new_passport_json = str(ex.get("passport_json") or "{}")
        new_calibrated_at = (
            str(calibrated_at)[:64]
            if calibrated_at is not None
            else str(ex.get("calibrated_at") or "")
        )
        new_retry_after = (
            str(retry_after)[:64]
            if retry_after is not None
            else str(ex.get("retry_after") or "")
        )
        new_last_error = (
            str(last_error)[:600]
            if last_error is not None
            else str(ex.get("last_error") or "")
        )

        await db.execute(
            """
            INSERT INTO model_advisor_catalog (
                series_key, vendor, display_name, source, passport_json,
                calibrated_at, retry_after, last_error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(series_key) DO UPDATE SET
                vendor = excluded.vendor,
                display_name = excluded.display_name,
                source = excluded.source,
                passport_json = excluded.passport_json,
                calibrated_at = excluded.calibrated_at,
                retry_after = excluded.retry_after,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (
                clean_key,
                new_vendor,
                new_display,
                new_source,
                new_passport_json,
                new_calibrated_at,
                new_retry_after,
                new_last_error,
                now,
            ),
        )
        await db.commit()
        cur2 = await db.execute(
            "SELECT * FROM model_advisor_catalog WHERE series_key = ? LIMIT 1",
            (clean_key,),
        )
        saved = await cur2.fetchone()
        return _row_to_model_advisor_entry(saved)


async def clear_stale_advisor_errors(
    now_iso: Optional[str] = None,
    *,
    force_all: bool = False,
) -> int:
    ts = now_iso or utc_now_iso()
    async with connect_db() as db:
        if force_all:
            cur = await db.execute(
                """
                UPDATE model_advisor_catalog
                SET retry_after = '', last_error = '', updated_at = ?
                WHERE retry_after != '' OR last_error != ''
                """,
                (ts,),
            )
        else:
            cur = await db.execute(
                """
                UPDATE model_advisor_catalog
                SET retry_after = '', last_error = '', updated_at = ?
                WHERE retry_after != '' AND retry_after <= ?
                """,
                (ts, ts),
            )
        await db.commit()
        return max(0, int(cur.rowcount or 0))
