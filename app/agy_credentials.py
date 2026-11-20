"""Serialized Windows keyring leases for CLI versions that prefer CredReadW.

Only isolated token-backed invocations lease the host entry. The original entry
is encrypted before replacement and restored before releasing the process lock.
The host token file is never written.
"""
import asyncio
import ctypes
import json
import os
from pathlib import Path
from . import config

_lease_lock = asyncio.Lock()
_pending_turn_launches = 0
active_working_turns = 0


class TelemetryDeferred(RuntimeError):
    """A working turn owns the credential store; use cached telemetry."""
_turn_queue_clear = asyncio.Event()
_turn_queue_clear.set()


def has_pending_turn_launches() -> bool:
    return _pending_turn_launches > 0


async def wait_for_turn_launches() -> None:
    if _pending_turn_launches > 0:
        await _turn_queue_clear.wait()


async def _acquire_lease_with_priority(priority: str = 'turn') -> None:
    global _pending_turn_launches
    if priority == 'turn':
        _pending_turn_launches += 1
        _turn_queue_clear.clear()
        try:
            await _lease_lock.acquire()
        finally:
            _pending_turn_launches = max(0, _pending_turn_launches - 1)
            if _pending_turn_launches == 0:
                _turn_queue_clear.set()
        return

    while True:
        if _pending_turn_launches > 0:
            await _turn_queue_clear.wait()
        await _lease_lock.acquire()
        if _pending_turn_launches > 0:
            _lease_lock.release()
            await asyncio.sleep(0)
            continue
        return


class WindowsCredentialStore:
    target = 'gemini:antigravity'

    def __init__(self):
        from ctypes import wintypes as w
        class Credential(ctypes.Structure):
            _fields_ = [('Flags',w.DWORD),('Type',w.DWORD),('TargetName',w.LPWSTR),('Comment',w.LPWSTR),
                ('LastWritten',w.FILETIME),('CredentialBlobSize',w.DWORD),('CredentialBlob',ctypes.POINTER(ctypes.c_ubyte)),
                ('Persist',w.DWORD),('AttributeCount',w.DWORD),('Attributes',ctypes.c_void_p),
                ('TargetAlias',w.LPWSTR),('UserName',w.LPWSTR)]
        self.ctype = Credential
        self.api = ctypes.WinDLL('Advapi32.dll',use_last_error=True)
        self.api.CredReadW.argtypes = [w.LPCWSTR,w.DWORD,w.DWORD,ctypes.POINTER(ctypes.POINTER(Credential))]
        self.api.CredWriteW.argtypes = [ctypes.POINTER(Credential),w.DWORD]
        self.api.CredDeleteW.argtypes = [w.LPCWSTR,w.DWORD,w.DWORD]
        self.api.CredFree.argtypes = [ctypes.c_void_p]

    def read(self):
        ptr = ctypes.POINTER(self.ctype)()
        if not self.api.CredReadW(self.target,1,0,ctypes.byref(ptr)):
            if ctypes.get_last_error() == 1168:
                return None
            raise OSError('Не удалось прочитать резервную сессию Windows Credential Manager')
        try:
            c = ptr.contents
            if c.AttributeCount:
                raise ValueError('Сессия Windows содержит неподдерживаемые атрибуты; переключение отменено')
            return dict(blob=ctypes.string_at(c.CredentialBlob,c.CredentialBlobSize).hex(), persist=c.Persist,
                username=c.UserName or '', comment=c.Comment or '', alias=c.TargetAlias or '', flags=c.Flags)
        finally:
            self.api.CredFree(ptr)

    def write(self, entry):
        if entry is None:
            if not self.api.CredDeleteW(self.target,1,0) and ctypes.get_last_error() != 1168:
                raise OSError('Не удалось восстановить пустую сессию Windows')
            return
        blob = bytes.fromhex(entry['blob'])
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        c = self.ctype()
        c.Type=1; c.TargetName=self.target; c.CredentialBlobSize=len(blob); c.CredentialBlob=buf
        c.Persist=entry.get('persist',2); c.UserName=entry.get('username','antigravity')
        c.Comment=entry.get('comment',''); c.TargetAlias=entry.get('alias',''); c.Flags=entry.get('flags',0)
        if not self.api.CredWriteW(ctypes.byref(c),0):
            raise OSError('Не удалось записать сессию Windows Credential Manager')


def read_local_cli_credentials():
    if os.name == 'nt':
        saved = WindowsCredentialStore().read()
        if saved:
            blob = bytes.fromhex(saved['blob'])
            for encoding in ('utf-8','utf-16-le'):
                try:
                    value = json.loads(blob.decode(encoding))
                    if isinstance(value,dict):
                        return json.dumps(value)
                except (ValueError,UnicodeError):
                    continue
    return (config.AGY_CLI_DATA_DIR/'antigravity-oauth-token').read_text(encoding='utf-8')


class _LeaseReleasingStdout:
    def __init__(self, reader, leased):
        self.reader, self.leased = reader, leased

    def __getattr__(self, name):
        return getattr(self.reader, name)

    async def _read(self, method, *args):
        value = await getattr(self.reader, method)(*args)
        authorized = False
        try:
            event = json.loads(value)
            authorized = event.get('event') in ('thinking', 'message', 'tool_call', 'command_result', 'result')
            if event.get('event') == 'step_update':
                step = event.get('step_update') or {}
                authorized = step.get('step_type') in ('thinking', 'agent_response', 'tool')
        except (ValueError, TypeError, AttributeError):
            pass
        if authorized or not value or getattr(self.reader, 'at_eof', lambda: False)():
            await self.leased.finish_lease()
        return value

    async def readline(self):
        return await self._read('readline')

    async def read(self, n=-1):
        return await self._read('read', n)

    async def readuntil(self, separator=b'\n'):
        try:
            return await self._read('readuntil', separator)
        except asyncio.IncompleteReadError:
            await self.leased.finish_lease()
            raise

    def at_eof(self):
        return self.reader.at_eof()

    def __aiter__(self):
        return self

    async def __anext__(self):
        line = await self.readline()
        if not line:
            raise StopAsyncIteration
        return line


class LeasedProcess:
    def __init__(self, proc, restore, max_hold_sec=10.0):
        self.proc=proc
        self.restore=restore
        self.stdout = _LeaseReleasingStdout(proc.stdout, self) if proc.stdout is not None else None
        self._guard = asyncio.create_task(self._startup_window(max_hold_sec))

    async def _startup_window(self, timeout):
        waiter = asyncio.create_task(self.proc.wait())
        try:
            await asyncio.wait({waiter}, timeout=timeout)
            await self.finish_lease()
        finally:
            if not waiter.done():
                waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            await self.finish_lease()

    def __getattr__(self, name):
        return getattr(self.proc,name)

    async def finish_lease(self):
        if self.restore is not None:
            restore,self.restore=self.restore,None
            if self._guard is not asyncio.current_task():
                self._guard.cancel()
            try:
                restore()
            except Exception:
                pass
            finally:
                _lease_lock.release()

    async def wait(self):
        try:
            return await self.proc.wait()
        finally:
            if self.proc.returncode is not None:
                await self.finish_lease()

    async def communicate(self, *args, **kwargs):
        try:
            return await self.proc.communicate(*args,**kwargs)
        finally:
            if self.proc.returncode is None:
                self.proc.kill()
                await self.proc.wait()
            await self.finish_lease()

    async def aclose_lease(self):
        if self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        await self.finish_lease()


async def create_agy_subprocess_exec(*args, **kwargs):
    priority = str(kwargs.pop('priority', 'turn') or 'turn')
    if priority == 'telemetry' and active_working_turns:
        raise TelemetryDeferred()
    env=kwargs.get('env') or {}
    home=Path(env.get('USERPROFILE') or env.get('HOME') or '.')
    isolated = '.agy_homes' in home.parts
    if os.name != 'nt':
        return await asyncio.create_subprocess_exec(*args,**kwargs)
    if not isolated:
        await _acquire_lease_with_priority(priority)
        try:
            if priority == 'telemetry' and active_working_turns:
                raise TelemetryDeferred()
            return LeasedProcess(await asyncio.create_subprocess_exec(*args,**kwargs), lambda: None)
        except BaseException:
            _lease_lock.release()
            raise
    from . import database
    raw=(home/'.gemini/antigravity-cli/antigravity-oauth-token').read_text(encoding='utf-8')
    value=json.loads(raw)
    if not isinstance(value.get('token'),dict):
        raise ValueError('OAuth-файл CLI должен содержать объект token')
    await _acquire_lease_with_priority(priority)
    store = None
    original = None
    changed = False
    proc = None
    backup = config.DATA_DIR / 'agy_host_session_backup.encrypted'
    try:
        if priority == 'telemetry' and active_working_turns:
            raise TelemetryDeferred()
        store = WindowsCredentialStore()
        # An interrupted previous lease must be recovered before taking a new backup.
        if backup.exists():
            previous = json.loads(database.decrypt_agy_credentials_json(backup.read_text(encoding='utf-8')))
            store.write(previous)
            backup.unlink(missing_ok=True)
        original = store.read()
        backup.parent.mkdir(parents=True, exist_ok=True)
        config._atomic_sync_text_file(backup, database.encrypt_agy_credentials_json(json.dumps(original)))
        store.write(dict(blob=raw.encode('utf-8').hex(), persist=2, username='antigravity'))
        changed = True

        def restore():
            # CLI may refresh only the keyring copy; persist it in the isolated file.
            # Never raise out of restore() so _LeaseReleasingStdout._read never crashes.
            restore_original = True
            try:
                try:
                    current = store.read()
                except ValueError:
                    # External credential with unsupported attributes -> do not overwrite
                    current = None
                    restore_original = False
                except OSError:
                    current = None
                if current:
                    decoded_obj = None
                    decoded_text = None
                    try:
                        raw_blob = bytes.fromhex(current.get('blob') or '')
                        for encoding in ('utf-8', 'utf-16-le'):
                            try:
                                cand_text = raw_blob.decode(encoding)
                                cand_obj = json.loads(cand_text)
                                if isinstance(cand_obj, dict):
                                    decoded_obj = cand_obj
                                    decoded_text = cand_text
                                    break
                            except (UnicodeError, ValueError):
                                continue
                    except (UnicodeError, ValueError, OSError, TypeError):
                        decoded_obj = None
                    if not isinstance(decoded_obj, dict) or not isinstance(decoded_obj.get('token'), dict):
                        restore_original = False
                    elif decoded_obj['token'].get('refresh_token') != value['token'].get('refresh_token'):
                        # An external Desktop/Cockpit login wins over our saved snapshot.
                        restore_original = False
                    else:
                        try:
                            config._atomic_sync_text_file(
                                home / '.gemini/antigravity-cli/antigravity-oauth-token',
                                decoded_text or json.dumps(decoded_obj, ensure_ascii=False),
                            )
                        except OSError:
                            pass
            except Exception:
                pass
            finally:
                if restore_original:
                    try:
                        store.write(original)
                    except OSError:
                        pass
                try:
                    backup.unlink(missing_ok=True)
                except OSError:
                    pass

        proc = await asyncio.create_subprocess_exec(*args, **kwargs)
        return LeasedProcess(proc, restore)
    except BaseException:
        try:
            if proc is not None and getattr(proc, 'returncode', None) is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            if changed and store is not None:
                try:
                    store.write(original)
                except OSError:
                    pass
                try:
                    backup.unlink(missing_ok=True)
                except OSError:
                    pass
        finally:
            _lease_lock.release()
        raise
