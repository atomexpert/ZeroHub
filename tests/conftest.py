"""Session-wide isolation before application imports, including legacy test modules."""
import builtins
import io
import os
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIVE_DATA_DIR = (ROOT / "data").resolve()
LIVE_DB = LIVE_DATA_DIR / "hub.db"
LIVE_STAT = (LIVE_DB.stat().st_mtime_ns, LIVE_DB.stat().st_size) if LIVE_DB.exists() else None
_TEMP = tempfile.TemporaryDirectory(prefix="hub_pytest_v90_", ignore_cleanup_errors=True)
_ENV = {
    "HUB_DATA_DIR": _TEMP.name,
    "AGY_CLI_DATA_DIR": str(Path(_TEMP.name) / "agy_cli"),
    "AGY_APP_DATA_DIR": str(Path(_TEMP.name) / "agy_app"),
    "PROJECTS_ROOT": str(Path(_TEMP.name) / "projects"),
    "CUDA_VISIBLE_DEVICES": "",
    "ORT_DISABLE_ALL_GPU": "1",
    "NVIDIA_VISIBLE_DEVICES": "none",
    "BROWSER": "echo",
    "CI": "1",
}
_PREVIOUS = {k: os.environ.get(k) for k in _ENV}
os.environ.update(_ENV)


def _close_hub_db_pool_quietly() -> None:
    """v110: пул тёплых SQLite-соединений держит файлы БД открытыми. Legacy-тесты удаляют
    временные каталоги без `await database.close_db_pool()` — закрываем пул синхронно,
    иначе на Windows `TemporaryDirectory.cleanup()` падает с WinError 32."""
    import sys as _sys

    db_mod = _sys.modules.get("app.database")
    closer = getattr(db_mod, "close_db_pool_sync", None) if db_mod is not None else None
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


_ORIG_TMPDIR_CLEANUP = tempfile.TemporaryDirectory.cleanup


def _tmpdir_cleanup_with_db_pool_close(self, *args, **kwargs):
    _close_hub_db_pool_quietly()
    return _ORIG_TMPDIR_CLEANUP(self, *args, **kwargs)


tempfile.TemporaryDirectory.cleanup = _tmpdir_cleanup_with_db_pool_close


@pytest.fixture(autouse=True)
def close_hub_db_pool_after_test():
    yield
    _close_hub_db_pool_quietly()


@pytest.fixture(autouse=True)
def isolate_host_credentials(monkeypatch):
    """Legacy host-profile tests must never read or replace the real OAuth key."""
    from app.agy_credentials import WindowsCredentialStore
    original_read, original_write = WindowsCredentialStore.read, WindowsCredentialStore.write
    entries = {}

    def read(store):
        if store.target == 'gemini:antigravity':
            return entries.get(store.target)
        return original_read(store)

    def write(store, value):
        if store.target == 'gemini:antigravity':
            entries[store.target] = value
            return
        return original_write(store, value)

    monkeypatch.setattr(WindowsCredentialStore, 'read', read)
    monkeypatch.setattr(WindowsCredentialStore, 'write', write)
    monkeypatch.setenv('HUB_KEEP_DEFAULT_TERMINAL', '1')


@pytest.fixture(autouse=True)
def protect_live_database(monkeypatch):
    original_connect = sqlite3.connect
    before = (LIVE_DB.stat().st_mtime_ns, LIVE_DB.stat().st_size) if LIVE_DB.exists() else None

    def guarded_connect(database, *args, **kwargs):
        target = os.fsdecode(database)
        if target.startswith("file:"):
            uri = urlsplit(target)
            target = url2pathname(uri.path)
        if target != ":memory:":
            assert not Path(target).resolve().is_relative_to(LIVE_DATA_DIR), (
                f"Test attempted to connect SQLite to production data: {target}"
            )
        return original_connect(database, *args, **kwargs)

    def guard_open(opener):
        def guarded_open(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                assert not Path(os.fsdecode(file)).resolve().is_relative_to(LIVE_DATA_DIR), (
                    f"Test attempted to open production data: {file}"
                )
            return opener(file, *args, **kwargs)
        return guarded_open

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    monkeypatch.setattr(builtins, "open", guard_open(builtins.open))
    monkeypatch.setattr(io, "open", guard_open(io.open))
    yield
    if before is not None and os.environ.get("HUB_ALLOW_EXTERNAL_LIVE_DB_ACTIVITY") != "1":
        assert (LIVE_DB.stat().st_mtime_ns, LIVE_DB.stat().st_size) == before


def pytest_unconfigure(config):
    # Unlink junctions before recursive temporary-directory cleanup on Windows.
    for p in sorted(Path(_TEMP.name).rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if (hasattr(os.path, "isjunction") and os.path.isjunction(p)) or (p.is_symlink() and p.is_dir()):
            os.rmdir(p)
    _TEMP.cleanup()
    for k, v in _PREVIOUS.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
