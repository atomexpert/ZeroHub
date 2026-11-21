"""Storage diagnostics and explicit maintenance; no work runs at import time."""
import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from . import config, database

maintenance_lock = asyncio.Lock()


def upload_roots():
    roots = [config.UPLOADS_DIR, config.SHARED_PROJECTS_DIR / '_uploads']
    users = config.get_effective_users_dir()
    if users.is_dir():
        roots.extend(p / 'uploads' for p in users.iterdir() if p.is_dir() and not p.is_symlink())
    return list(dict.fromkeys(Path(p).absolute() for p in roots))


def safe_remove_upload(path, root):
    path, root = Path(path), Path(root)
    if root.is_symlink():
        return False
    root = root.resolve()
    if path.is_symlink() or not path.is_dir():
        return False
    resolved = path.resolve()
    if resolved.parent != root or root.name not in ('uploads', '_uploads'):
        return False
    shutil.rmtree(resolved)
    return True


def tree_bytes(path):
    if path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(tree_bytes(p) for p in path.iterdir())
    return 0


def orphan_uploads(ids):
    return [(p, root) for root in upload_roots() if root.is_dir() and not root.is_symlink()
            for p in root.iterdir() if p.name not in ids and p.is_dir() and not p.is_symlink()]


async def cleanup_deleted_chat(chat, shared=False):
    cid = str(chat.get('id') or '')
    if not cid or config.sanitize_chat_folder_id(cid) != cid:
        return
    owner = await database.get_user_by_id(str(chat.get('owner_id') or 'admin'))
    username = str((owner or {}).get('username') or chat.get('owner_username') or chat.get('owner_id') or 'admin')
    uploads = config.resolve_chat_uploads_dir(chat, owner_username=username, is_shared_project=shared)
    targets = [(uploads, uploads.parent)] + [(config.UPLOADS_DIR / cid, config.UPLOADS_DIR)]
    # Only the owner's personal sandbox contains this chat's mirrored summary.
    summary = config.resolve_user_chat_sandbox_dir(username, cid, create=False) / 'CONTEXT_SUMMARY.md'
    conv = str(chat.get('agy_conversation_id') or '')
    removable = False
    if conv and not chat.get('source_desktop_id') and re.fullmatch(r'[0-9a-fA-F-]{8,64}', conv):
        async with database.connect_db() as db:
            cur = await db.execute('SELECT 1 FROM chats WHERE agy_conversation_id = ? LIMIT 1', (conv,))
            removable = await cur.fetchone() is None
    def cleanup():
        for path, root in targets:
            safe_remove_upload(path, root)
        sandbox_dir = summary.parent
        if summary.is_file() and not summary.is_symlink():
            summary.unlink()
        try:
            users_root = config.get_effective_users_dir().resolve()
            if (
                sandbox_dir.is_dir()
                and not config._is_junction_or_symlink(sandbox_dir)
                and sandbox_dir.parent.name == 'chats'
                and not config._is_junction_or_symlink(sandbox_dir.parent)
                and sandbox_dir.resolve().parent == sandbox_dir.parent.resolve()
                and sandbox_dir.resolve().is_relative_to(users_root)
                and not any(sandbox_dir.iterdir())
            ):
                sandbox_dir.rmdir()
        except OSError:
            pass
        if removable:
            roots = [config.AGY_CLI_DATA_DIR / 'conversations']
            roots.extend((config.CLI_HOMES_DIR / '_agy_accounts').glob('*/antigravity-cli/conversations'))
            for candidate in roots:
                if config._is_junction_or_symlink(candidate):
                    continue
                root = candidate.resolve()
                for suffix in ('.db', '.db-wal', '.db-shm'):
                    path = root / (conv + suffix)
                    if path.resolve().parent == root and path.is_file() and not path.is_symlink():
                        path.unlink()
    await asyncio.to_thread(cleanup)


def cleanup_stale_large_prompt_files(ttl_sec: float = 7200.0) -> int:
    from . import agy_engine
    now_ts = time.time()
    active_cids = {
        config.sanitize_chat_folder_id(str(cid))
        for cid in list(agy_engine.ACTIVE_RUNS.keys())
    } | set(agy_engine.ACTIVE_RUNS.keys())
    removed_files = 0
    for root in upload_roots():
        if not root.is_dir() or config._is_junction_or_symlink(root):
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for cid_dir in children:
            if not cid_dir.is_dir() or config._is_junction_or_symlink(cid_dir):
                continue
            if cid_dir.name in active_cids:
                continue
            try:
                entries = list(cid_dir.iterdir())
            except OSError:
                continue
            deleted_in_dir = False
            for fp in entries:
                fname = fp.name
                if not (
                    fname == 'large_prompt_input.md'
                    or (fname.startswith('_large_prompt_input_') and fname.endswith('.md'))
                ):
                    continue
                if config._is_junction_or_symlink(fp) or not fp.is_file():
                    continue
                try:
                    age = now_ts - fp.stat().st_mtime
                    if age >= float(ttl_sec):
                        fp.unlink(missing_ok=True)
                        removed_files += 1
                        deleted_in_dir = True
                except OSError:
                    continue
            if deleted_in_dir:
                try:
                    if cid_dir.is_dir() and not any(cid_dir.iterdir()):
                        cid_dir.rmdir()
                except OSError:
                    pass
    return removed_files


async def storage_health():
    def _batch_excess(raw_rows):
        delta = 0
        for (raw,) in raw_rows:
            try:
                slim = json.dumps(database.compact_tool_calls_payload(json.loads(raw)), ensure_ascii=False)
                delta += max(0, len(raw.encode('utf-8')) - len(slim.encode('utf-8')))
            except (ValueError, TypeError):
                pass
        return delta

    async with database.connect_db() as db:
        cur = await db.execute('SELECT id FROM chats')
        ids = {str(r[0]) for r in await cur.fetchall()}
        cur = await db.execute('SELECT COUNT(*) FROM messages')
        messages = (await cur.fetchone())[0]
        excess = 0
        cur = await db.execute(
            "SELECT tool_calls_json FROM messages WHERE tool_calls_json IS NOT NULL AND tool_calls_json NOT IN ('', '[]')"
        )
        while rows := await cur.fetchmany(100):
            excess += await asyncio.to_thread(_batch_excess, rows)
    db_path = Path(database._resolved_db_path())
    def sizes():
        db_size = tree_bytes(db_path)
        wal = tree_bytes(Path(str(db_path) + '-wal'))
        log = tree_bytes(config.DATA_DIR / 'hub_server.log')
        orphan = sum(tree_bytes(p) for p, _ in orphan_uploads(ids))
        return dict(db_bytes=db_size, wal_bytes=wal, db_mb=round(db_size / 1048576, 2),
                    log_bytes=log, orphan_uploads_bytes=orphan, excess_tool_bytes=excess,
                    chats_count=len(ids), messages_count=messages,
                    total_bytes=db_size + wal + log + orphan)
    return await asyncio.to_thread(sizes)


async def run_maintenance(can_vacuum):
    def _compact_batch(batch_rows):
        max_id = 0
        updates = []
        for mid, raw in batch_rows:
            max_id = mid
            raw_str = raw or ''
            try:
                slim = json.dumps(
                    database.compact_tool_calls_payload(json.loads(raw_str or '[]')),
                    ensure_ascii=False,
                )
            except (ValueError, TypeError):
                continue
            if len(slim.encode('utf-8')) < len(raw_str.encode('utf-8')):
                updates.append((slim, mid, raw_str))
        return max_id, updates

    async with maintenance_lock:
        before = await storage_health()
        slimmed = 0
        last_id = 0
        async with database.connect_db() as db:
            while True:
                cur = await db.execute(
                    "SELECT id, tool_calls_json FROM messages "
                    "WHERE id > ? AND status != 'streaming' "
                    "AND tool_calls_json IS NOT NULL AND tool_calls_json NOT IN ('', '[]') "
                    "ORDER BY id LIMIT 100",
                    (last_id,),
                )
                rows = await cur.fetchall()
                if not rows:
                    break
                last_id, updates = await asyncio.to_thread(_compact_batch, rows)
                if updates:
                    upd_cur = await db.executemany(
                        "UPDATE messages SET tool_calls_json = ? "
                        "WHERE id = ? AND status != 'streaming' AND tool_calls_json = ?",
                        updates,
                    )
                    await db.commit()
                    if upd_cur.rowcount and upd_cur.rowcount > 0:
                        slimmed += int(upd_cur.rowcount)
            cur = await db.execute('SELECT id FROM chats')
            ids = {str(r[0]) for r in await cur.fetchall()}

        def clean_files():
            cleanup_stale_large_prompt_files()
            now_ts = time.time()
            removed_cnt = 0
            for p, root in orphan_uploads(ids):
                try:
                    if now_ts - p.stat().st_mtime < 600.0:
                        continue
                except OSError:
                    continue
                if safe_remove_upload(p, root):
                    removed_cnt += 1
            log = config.DATA_DIR / 'hub_server.log'
            try:
                if log.is_file() and log.stat().st_size > 10 * 1048576:
                    with log.open('r+b') as f:
                        f.seek(-2 * 1048576, 2)
                        tail = f.read()
                        f.seek(0)
                        f.write(tail)
                        f.truncate()
            except OSError:
                pass
            return removed_cnt

        removed = await asyncio.to_thread(clean_files)
        async with database.connect_db() as db:
            await db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            await db.execute('PRAGMA optimize')
            vacuumed = bool(can_vacuum())
            if vacuumed:
                await db.execute('VACUUM')
                await db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        database._reset_usage_ledger_cache()
        database.invalidate_chat_list_cache()
        after = await storage_health()
        return dict(before_bytes=before['total_bytes'], after_bytes=after['total_bytes'],
                    freed_bytes=max(0, before['total_bytes'] - after['total_bytes']),
                    slimmed_messages=slimmed, removed_orphan_uploads=removed, vacuumed=vacuumed)
