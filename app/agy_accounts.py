"""Private Google OAuth pool helpers. Credentials never belong in public payloads."""
import asyncio
import base64
import hashlib
import html
import json
import os
import re
import secrets
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from . import config, proxy_router


def parse_agy_import_payload(raw_text, fallback_email='', fallback_label=''):
    raw = str(raw_text or '').strip()
    if '***' in raw:
        raise ValueError('Похоже, вставлен замаскированный пример токена (содержит ***). Вставьте полный refresh_token из экспорта Cockpit.')
    if not raw:
        raise ValueError('Вставьте полный refresh_token или JSON из Cockpit.')
    if raw.startswith(('[', '{', '"')):
        try:
            data = json.loads(raw)
        except ValueError:
            raise ValueError('Некорректный JSON из Cockpit.') from None
        data = data if isinstance(data, list) else [data]
    else:
        data = []
        for line in raw.splitlines():
            email, sep, token = line.strip().partition(':1//')
            data.append({'email': email, 'refresh_token': '1//'+token} if sep else line.strip())
    result = []
    for item in data:
        if isinstance(item, str):
            item = {'refresh_token': item}
        if not isinstance(item, dict):
            raise ValueError('Каждый аккаунт должен быть JSON-объектом.')
        if isinstance(item.get('token'), dict):
            item = {**item, **item['token'], 'email':item.get('email') or item.get('id_token_email') or fallback_email}
        token = item.get('refresh_token') or item.get('refreshToken')
        if not isinstance(token, str) or not token.strip():
            raise ValueError('У аккаунта отсутствует refresh_token.')
        result.append({**item, 'refresh_token': token.strip(),
            'access_token': item.get('access_token') or item.get('accessToken') or '',
            'email': item.get('email') or fallback_email,
            'label': item.get('label') or item.get('name') or item.get('note') or fallback_label,
            'expiry': item.get('expiry') or item.get('expiry_date') or item.get('expires_at') or ''})
    if not result:
        raise ValueError('Список аккаунтов пуст.')
    return result


_oauth_cache = None


class OAuthTemporaryError(OSError):
    """Refresh failed transiently; an unexpired token may still be used."""


def _oauth_pair(payload):
    global _oauth_cache
    local = config.AGY_CLI_DATA_DIR / 'antigravity-oauth-token'
    try:
        metadata = json.loads(local.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        metadata = {}
    cid = os.getenv('AGY_OAUTH_CLIENT_ID') or payload.get('client_id') or metadata.get('client_id')
    secret = os.getenv('AGY_OAUTH_CLIENT_SECRET') or payload.get('client_secret') or metadata.get('client_secret')
    if not cid or not secret:
        binary = Path(config.resolve_agy_bin())
        stamp = (str(binary), binary.stat().st_mtime_ns)
        if not _oauth_cache or _oauth_cache[0] != stamp:
            raw = binary.read_bytes()
            ids = set(re.findall(rb'[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com', raw))
            secrets = set(re.findall(rb'GOCSPX-[A-Za-z0-9_-]+', raw))
            # Go concatenates string constants without delimiters. Match fingerprints,
            # including prefixes, rather than guessing a pair or shipping OAuth secrets.
            # Fingerprints documented by github.com/fikrilal/agy-auth/internal/oauth.
            id_hash = 'bf00c418024ba6bf606ccdc37120976e41bc429dd1d46ecf16a729aa532626ea'
            secret_hashes = {'1d2f041093fd95aa8995a038c711d50a7960da09a505381c09a745d6ad0ecc60','23d89fc5fbce73ed5b9e0cf0ca786bd0eb1bcdff8f65d03dc022d299ad2234b6'}
            known_ids = {v[i:] for v in ids for i in range(len(v)) if hashlib.sha256(v[i:]).hexdigest() == id_hash}
            known_secrets = {v[:i] for v in secrets for i in range(20,len(v)+1) if hashlib.sha256(v[:i]).hexdigest() in secret_hashes}
            if len(known_ids) == 1 and len(known_secrets) == 1:
                ids, secrets = known_ids, known_secrets
            if len(ids) != 1 or len(secrets) != 1:
                raise ValueError('Не удалось однозначно определить OAuth-пару CLI. Задайте AGY_OAUTH_CLIENT_ID и AGY_OAUTH_CLIENT_SECRET.')
            _oauth_cache = (stamp, next(iter(ids)).decode(), next(iter(secrets)).decode())
        cid, secret = cid or _oauth_cache[1], secret or _oauth_cache[2]
    return cid, secret


async def refresh_and_build_agy_token_payload(payload, *, account_id=None, proxy_url=None):
    # Route errors propagate: a custom route must never fall back to a direct request.
    route = proxy_url if proxy_url is not None else await proxy_router.ensure_proxy_for_context('agy', account_id=account_id)
    cid, secret = await asyncio.to_thread(_oauth_pair, payload)
    def request():
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': route, 'https': route} if route else {}))
        body = urllib.parse.urlencode(dict(client_id=cid, client_secret=secret, refresh_token=payload['refresh_token'], grant_type='refresh_token')).encode()
        try:
            with opener.open(urllib.request.Request('https://oauth2.googleapis.com/token', data=body), timeout=12) as response:
                token = json.load(response)
            email = payload.get('email', '')
            if not email or '*' in email:
                req = urllib.request.Request('https://www.googleapis.com/oauth2/v2/userinfo', headers={'Authorization': 'Bearer '+token['access_token']})
                with opener.open(req, timeout=12) as response:
                    email = json.load(response).get('email', '')
            return dict(token=dict(access_token=token['access_token'], refresh_token=token.get('refresh_token') or payload['refresh_token'],
                token_type=token.get('token_type', 'Bearer'),
                **({'scope': token['scope']} if token.get('scope') else {}), expiry=datetime.fromtimestamp(datetime.now(timezone.utc).timestamp()+token.get('expires_in',3600), timezone.utc).isoformat()),
                auth_method=payload.get('auth_method') or 'consumer', id_token=token.get('id_token') or payload.get('id_token',''), id_token_email=email, email=email)
        except urllib.error.HTTPError as exc:
            try:
                if exc.code >= 500 or exc.code == 429:
                    raise OAuthTemporaryError('Google OAuth temporarily unavailable') from None
                raise ValueError('Google OAuth refresh rejected') from None
            finally:
                exc.close()
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            raise OAuthTemporaryError('Google OAuth temporarily unavailable') from None
        except Exception:
            raise ValueError('Google OAuth: обновление токена не удалось. Проверьте токен и маршрут аккаунта.') from None
    return await asyncio.to_thread(request)


_ACCOUNT_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}


def _get_account_refresh_lock(key: str) -> asyncio.Lock:
    clean_k = str(key or '').strip() or '_host'
    lock = _ACCOUNT_REFRESH_LOCKS.get(clean_k)
    try:
        cur_loop = asyncio.get_running_loop()
    except RuntimeError:
        cur_loop = None
    if lock is None or (
        cur_loop is not None
        and getattr(lock, '_loop', None) not in (None, cur_loop)
    ):
        lock = asyncio.Lock()
        _ACCOUNT_REFRESH_LOCKS[clean_k] = lock
    return lock


async def refresh_host_agy_token(home, *, force=False):
    from .agy_credentials import WindowsCredentialStore, _lease_lock
    path = Path(home) / '.gemini/antigravity-cli/antigravity-oauth-token'
    try:
        lock_key = os.path.normcase(str(path.resolve()))
    except OSError:
        lock_key = str(path)

    async with _get_account_refresh_lock(lock_key):
        async with _lease_lock:
            store = WindowsCredentialStore() if os.name == 'nt' else None
            entry = store.read() if store else None
            raw = None
            if entry:
                for encoding in ('utf-8', 'utf-16-le'):
                    try:
                        raw = json.loads(bytes.fromhex(entry['blob']).decode(encoding))
                        break
                    except (ValueError, UnicodeError):
                        pass
            if raw is None:
                try:
                    raw = json.loads(path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    if force:
                        raise ValueError('No refreshable Google OAuth session') from None
                    return
            token = raw.get('token', raw)
            if not token.get('refresh_token'):
                if force:
                    raise ValueError('No Google refresh token')
                return
            remaining = 0
            try:
                dt = datetime.fromisoformat(str(token.get('expiry')).replace('Z', '+00:00'))
                remaining = (dt.replace(tzinfo=dt.tzinfo or timezone.utc) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError):
                pass
            if not (force or not token.get('access_token') or remaining < 900):
                return
            refresh_input = {**raw, **token}
            had_store_entry = entry is not None
            had_file = path.is_file()

        try:
            fresh = await refresh_and_build_agy_token_payload(refresh_input)
        except (OAuthTemporaryError, TimeoutError):
            if force or remaining <= 60:
                raise
            return

        text = json.dumps(fresh, ensure_ascii=False)
        async with _lease_lock:
            cur_entry = store.read() if store else None
            cur_file_exists = path.is_file()
            # Guard against reviving credentials if the account/session was deleted while refreshing.
            if (had_store_entry and cur_entry is None and not cur_file_exists) or (
                not had_store_entry and had_file and not cur_file_exists and cur_entry is None
            ):
                return
            config._atomic_sync_text_file(path, text)
            if store:
                store.write({**(cur_entry or entry or {}), 'blob': text.encode('utf-8').hex()})


async def materialize_agy_account_home(account, user='admin', *, force_refresh=False):
    from . import database
    if not account or not account.get('has_credentials'):
        home = await asyncio.to_thread(config.ensure_user_cli_home, user)
        await refresh_host_agy_token(home, force=force_refresh)
        return home
    account = await database.get_account_profile_by_id(account['id'], include_decrypted_credentials=True)
    raw = account['credentials_json']
    if not raw:
        raise ValueError('Не удалось расшифровать токен Google AI')
    stored_payload = json.loads(raw)
    token = stored_payload.get('token', stored_payload)
    remaining = 0.0
    try:
        expiry = token.get('expiry') or stored_payload.get('expiry')
        expires = datetime.fromisoformat(str(expiry).replace('Z', '+00:00'))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        remaining = (expires - datetime.now(timezone.utc)).total_seconds()
    except (ValueError, TypeError, OverflowError):
        pass
    refreshed = False
    if force_refresh or not token.get('access_token') or remaining < 900:
        try:
            fresh = await refresh_and_build_agy_token_payload(
                {**stored_payload, **token}, account_id=account['id'])
            raw = json.dumps(fresh, ensure_ascii=False)
            await database.sync_agy_account_credentials_json(account['id'], raw)
            refreshed = True
        except proxy_router.ProxyRouteError:
            raise
        except (OAuthTemporaryError, TimeoutError):
            if force_refresh or not token.get('access_token') or remaining <= 60:
                raise
    home = await asyncio.to_thread(config.ensure_user_agy_account_home, user, account['id'])
    dest = config.ensure_agy_account_cli_data_dir(account['id']) / 'antigravity-oauth-token'
    # Do not overwrite a newer token refreshed by another CLI invocation.
    must_write = refreshed or not dest.exists()
    if dest.exists() and not refreshed:
        try:
            on_disk = json.loads(dest.read_text(encoding='utf-8'))
            stored = json.loads(raw)
            on_disk = on_disk.get('token', on_disk)
            stored = stored.get('token', stored)
            must_write = (on_disk.get('refresh_token') or on_disk.get('refreshToken')) != (stored.get('refresh_token') or stored.get('refreshToken'))
            def expires_at(value):
                try:
                    dt = datetime.fromisoformat(str(value.get('expiry')).replace('Z', '+00:00'))
                    return dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
                except (TypeError, ValueError, OverflowError):
                    return 0
            must_write = must_write or expires_at(stored) > expires_at(on_disk)
        except (ValueError, OSError):
            must_write = True
    if must_write:
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix='.oauth-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(raw)
            os.replace(tmp, dest)
        finally:
            Path(tmp).unlink(missing_ok=True)
    return home


async def sync_refreshed_agy_credentials_back(account_id):
    from . import database
    path = config.ensure_agy_account_cli_data_dir(account_id) / 'antigravity-oauth-token'
    if path.is_file():
        raw = path.read_text(encoding='utf-8')
        value = json.loads(raw)
        token = value.get('token', value) if isinstance(value, dict) else {}
        if token.get('refresh_token') or token.get('refreshToken'):
            await database.sync_agy_account_credentials_json(account_id, raw)


def is_agy_quota_exhausted_error(err_text):
    if re.search(r'503|capacity|overload', str(err_text), re.I) and not re.search(r'quota|usage limit|rate limit|429', str(err_text), re.I):
        return False
    return bool(re.search(r'quota exceeded|resource_exhausted|rate limit exceeded|usage limit|quota limit|limit reached|exhausted|429 too many requests', str(err_text), re.I))


def agy_pool_exhausted(account, target_pool):
    quotas = account.get('quotas') or {}
    prefix = 'claude' if target_pool == 'claude_gpt' else 'gemini'
    for group in quotas.get('groups', []):
        if group.get('id') != target_pool:
            continue
        for bucket in group.get('buckets', []):
            if bucket.get('window') not in ('5h', 'weekly'):
                continue
            pct = bucket.get('remaining_pct', quotas.get(prefix+'_'+bucket['window']+'_pct'))
            try:
                reset = datetime.fromisoformat(bucket.get('reset_time', '').replace('Z', '+00:00'))
                if pct is not None and float(pct) <= 0 and reset > datetime.now(timezone.utc):
                    return True
            except (ValueError, TypeError):
                pass
    return False


async def _pick_next_agy_failover_account(tried_ids, target_pool):
    from . import database
    profiles = await database.list_account_profiles()
    prefix = 'claude' if target_pool == 'claude_gpt' else 'gemini'
    candidates = [p for p in profiles if p['id'] not in tried_ids and p['is_enabled'] and p['has_credentials'] and not agy_pool_exhausted(p,target_pool)]
    candidates.sort(key=lambda p: -float((p.get('quotas') or {}).get(prefix+'_5h_pct') if (p.get('quotas') or {}).get(prefix+'_5h_pct') is not None else 101))
    return candidates[0] if candidates else None


def agy_target_pool_for_model(model: str) -> str:
    m = str(model or '').strip().lower()
    if 'gemini' in m:
        return 'gemini'
    if 'claude' in m or any(k in m for k in ('gpt', 'chatgpt', 'openai', 'o1-', 'o3-', 'o4-')) or re.search(r'\b(gpt|o1|o3|o4)\b', m):
        return 'claude_gpt'
    return 'gemini'


async def resolve_agy_account_for_chat_turn(
    model: str,
    *,
    preferred_account_id: str = '',
    current_chat_account_id: str = '',
) -> dict[str, Any]:
    from . import database
    profiles = await database.list_account_profiles()
    active = await database.get_active_account_profile()
    target_pool = agy_target_pool_for_model(model)
    if not profiles:
        return {'account': active, 'target_pool': target_pool, 'switched_from': None, 'emit_failover': False}

    by_id = {str(p['id']): p for p in profiles if p.get('id')}
    pref_id = str(preferred_account_id or '').strip()
    cur_id = str(current_chat_account_id or '').strip()

    # Case A: Explicit per-tab manual account pin
    if pref_id:
        pref_acc = by_id.get(pref_id)
        if pref_acc and pref_acc.get('is_enabled'):
            return {'account': pref_acc, 'target_pool': target_pool, 'switched_from': None, 'emit_failover': False}

    # Case B: Smart Auto Pool (per-tab sticky if bucket has remaining quota)
    bound = by_id.get(cur_id) if cur_id else None
    if bound and bound.get('is_enabled'):
        if not agy_pool_exhausted(bound, target_pool) or not bound.get('auto_failover', True):
            return {'account': bound, 'target_pool': target_pool, 'switched_from': None, 'emit_failover': False}

    if active and active.get('is_enabled'):
        if not agy_pool_exhausted(active, target_pool):
            switched = bound if (bound and bound['id'] != active['id'] and agy_pool_exhausted(bound, target_pool)) else None
            return {
                'account': active,
                'target_pool': target_pool,
                'switched_from': switched,
                'emit_failover': bool(switched),
            }
        if not active.get('auto_failover', True):
            return {'account': active, 'target_pool': target_pool, 'switched_from': None, 'emit_failover': False}

    tried: set[str] = set()
    base_from = bound if (bound and bound.get('is_enabled')) else active
    if bound:
        tried.add(str(bound['id']))
    if active:
        tried.add(str(active['id']))
    nxt = await _pick_next_agy_failover_account(tried, target_pool)
    if nxt:
        did_switch = bool(base_from and base_from.get('id') != nxt['id'])
        return {
            'account': nxt,
            'target_pool': target_pool,
            'switched_from': base_from if did_switch else None,
            'emit_failover': did_switch,
        }

    any_enabled = next((p for p in profiles if p.get('is_enabled')), None)
    return {
        'account': any_enabled or active or profiles[0],
        'target_pool': target_pool,
        'switched_from': None,
        'emit_failover': False,
    }


AGY_OAUTH_SCOPES: tuple[str, ...] = (
    'https://www.googleapis.com/auth/cloud-platform',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    'https://www.googleapis.com/auth/cclog',
    'https://www.googleapis.com/auth/experimentsandconfigs',
    'https://www.googleapis.com/auth/aicode',
)
AGY_OAUTH_FLOW_TTL_SEC = 600


@dataclass
class AgyOAuthFlow:
    flow_id: str
    state: str
    code_verifier: str
    redirect_uri: str
    auth_url: str
    label: str = ''
    proxy_mode: Optional[str] = None
    proxy_spec: Optional[str] = None
    proxy_label: Optional[str] = None
    make_active: bool = True
    status: str = 'pending'
    error: str = ''
    account: Optional[dict[str, Any]] = None
    created_mono: float = field(default_factory=time.monotonic)
    server: Optional[asyncio.AbstractServer] = None
    expiry_task: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    on_completed: Optional[Callable[[], Any]] = None

    def remaining_sec(self) -> int:
        return max(0, int(round(AGY_OAUTH_FLOW_TTL_SEC - (time.monotonic() - self.created_mono))))

    def to_public_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            'flow_id': self.flow_id,
            'status': self.status,
            'auth_url': self.auth_url,
            'redirect_uri': self.redirect_uri,
            'expires_in': self.remaining_sec(),
        }
        if self.error:
            out['error'] = self.error
        if self.account is not None:
            out['account'] = self.account
        return out


_AGY_OAUTH_FLOWS: dict[str, AgyOAuthFlow] = {}
_AGY_OAUTH_EXPIRY_TASKS: set[asyncio.Task] = set()


def _pkce_s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


def extract_google_oauth_code_and_state(raw_input: str) -> tuple[str, Optional[str]]:
    text = str(raw_input or '').strip()
    if not text:
        raise ValueError('Вставьте URL из адресной строки браузера или одноразовый код Google.')
    if '://' in text or text.startswith(('127.0.0.1', 'localhost', '?')) or 'code=' in text or 'error=' in text:
        normalized = text
        if normalized.startswith(('127.0.0.1', 'localhost')):
            normalized = 'http://' + normalized
        elif normalized.startswith('?'):
            normalized = 'http://127.0.0.1/oauth-callback' + normalized
        elif '://' not in normalized and ('code=' in normalized or 'error=' in normalized):
            normalized = 'http://127.0.0.1/oauth-callback?' + normalized.lstrip('?&')
        parsed = urllib.parse.urlsplit(normalized)
        qs = urllib.parse.parse_qs(parsed.query)
        if not qs and parsed.fragment:
            qs = urllib.parse.parse_qs(parsed.fragment)
        if qs.get('error'):
            raise ValueError('Вход через Google отменён или отклонён в браузере.')
        codes = qs.get('code') or []
        if not codes or not str(codes[0]).strip():
            raise ValueError('В ссылке не найден параметр code=. Скопируйте полный адрес страницы 127.0.0.1 после входа.')
        states = qs.get('state') or []
        return str(codes[0]).strip(), (str(states[0]).strip() if states and str(states[0]).strip() else None)
    return text.split()[0], None


async def exchange_agy_oauth_code_for_token(
    code: str,
    *,
    redirect_uri: str,
    code_verifier: str,
    proxy_url: Optional[str],
) -> dict[str, Any]:
    cid, secret = await asyncio.to_thread(_oauth_pair, {})

    def _do_exchange() -> dict[str, Any]:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url} if proxy_url else {})
        )
        body = urllib.parse.urlencode({
            'client_id': cid,
            'client_secret': secret,
            'code': code,
            'code_verifier': code_verifier,
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code',
        }).encode('utf-8')
        try:
            req = urllib.request.Request(
                'https://oauth2.googleapis.com/token',
                data=body,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
            )
            with opener.open(req, timeout=15) as resp:
                tok = json.load(resp)
            access_tok = str(tok.get('access_token') or '').strip()
            refresh_tok = str(tok.get('refresh_token') or '').strip()
            if not access_tok or not refresh_tok:
                raise ValueError('Google OAuth не вернул полный refresh_token. Повторите вход и подтвердите доступ.')
            u_req = urllib.request.Request(
                'https://www.googleapis.com/oauth2/v2/userinfo',
                headers={'Authorization': f'Bearer {access_tok}'},
            )
            email = ''
            with opener.open(u_req, timeout=12) as u_resp:
                email = str(json.load(u_resp).get('email') or '').strip()
            exp_ts = datetime.now(timezone.utc).timestamp() + float(tok.get('expires_in') or 3600)
            return {
                'token': {
                    'access_token': access_tok,
                    'refresh_token': refresh_tok,
                    'token_type': str(tok.get('token_type') or 'Bearer'),
                    **({'scope': tok['scope']} if tok.get('scope') else {}),
                    'expiry': datetime.fromtimestamp(exp_ts, timezone.utc).isoformat(),
                },
                'auth_method': 'consumer',
                'id_token': str(tok.get('id_token') or ''),
                'id_token_email': email,
                'email': email,
            }
        except ValueError:
            raise
        except urllib.error.HTTPError as exc:
            try:
                if exc.code >= 500 or exc.code == 429:
                    raise OAuthTemporaryError('Сервер Google OAuth временно недоступен. Попробуйте через несколько секунд.') from None
                raise ValueError('Код авторизации Google недействителен или уже был использован.') from None
            finally:
                exc.close()
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            raise OAuthTemporaryError('Не удалось связаться с Google OAuth через выбранный маршрут прокси.') from None
        except Exception:
            raise ValueError('Не удалось завершить обмен токена Google OAuth.') from None

    return await asyncio.to_thread(_do_exchange)


async def _close_agy_oauth_flow_resources(flow: AgyOAuthFlow, *, wait_server: bool = True) -> None:
    exp_t, flow.expiry_task = flow.expiry_task, None
    if exp_t is not None and not exp_t.done() and exp_t is not asyncio.current_task():
        exp_t.cancel()
    srv, flow.server = flow.server, None
    if srv is not None:
        try:
            srv.close()
            if wait_server:
                await srv.wait_closed()
        except Exception:
            pass
    try:
        await asyncio.to_thread(proxy_router.release_owner, f'agy:oauth:{flow.flow_id}')
    except Exception:
        pass


async def _auto_expire_agy_oauth_flow(flow_id: str) -> None:
    try:
        await asyncio.sleep(max(0.0, float(AGY_OAUTH_FLOW_TTL_SEC)))
    except asyncio.CancelledError:
        return
    fl = _AGY_OAUTH_FLOWS.get(flow_id)
    if fl is None:
        return
    if fl.status == 'pending':
        fl.status = 'expired'
        fl.error = 'Время ожидания входа истекло (10 минут).'
    await _close_agy_oauth_flow_resources(fl)


async def _cleanup_expired_agy_oauth_flows() -> None:
    for fid, fl in list(_AGY_OAUTH_FLOWS.items()):
        if fl.remaining_sec() <= 0:
            if fl.status == 'pending':
                fl.status = 'expired'
                fl.error = 'Время ожидания входа истекло (10 минут).'
            await _close_agy_oauth_flow_resources(fl)
            if (time.monotonic() - fl.created_mono) > (AGY_OAUTH_FLOW_TTL_SEC + 120):
                _AGY_OAUTH_FLOWS.pop(fid, None)


async def _finalize_agy_oauth_flow_with_code(
    flow: AgyOAuthFlow,
    code: str,
    received_state: Optional[str],
    *,
    from_loopback: bool = False,
) -> dict[str, Any]:
    from . import database, agy_engine
    async with flow.lock:
        if flow.status == 'completed' and flow.account is not None:
            return flow.account
        if flow.remaining_sec() <= 0:
            flow.status = 'expired'
            flow.error = 'Время ожидания OAuth-сессии истекло.'
            await _close_agy_oauth_flow_resources(flow)
            raise ValueError(flow.error)
        if flow.status not in ('pending',):
            raise ValueError(flow.error or 'Сессия OAuth уже закрыта.')
        if received_state is not None and received_state != flow.state:
            raise ValueError('Несовпадение защитного параметра state. Запустите вход заново.')

        # Strictly fail-closed proxy route check before exchanging code with Google!
        route = await proxy_router.ensure_proxy_for_context(
            'agy',
            pending_owner=f'agy:oauth:{flow.flow_id}',
            pending_mode=flow.proxy_mode or 'global',
            pending_spec=flow.proxy_spec,
        )
        token_payload = await exchange_agy_oauth_code_for_token(
            code,
            redirect_uri=flow.redirect_uri,
            code_verifier=flow.code_verifier,
            proxy_url=route or '',
        )
        saved = await database.upsert_agy_account_from_credentials(
            json.dumps(token_payload, ensure_ascii=False),
            email=token_payload.get('email') or '',
            label=(flow.label or '').strip() or None,
            auth_method='oauth',
            proxy_mode=flow.proxy_mode,
            proxy_spec=flow.proxy_spec,
            proxy_label=flow.proxy_label,
            make_active=bool(flow.make_active),
        )
        await materialize_agy_account_home(saved)
        await proxy_router.reload_routes_from_db()
        agy_engine.invalidate_agy_route_caches()
        try:
            await agy_engine.fetch_account_quotas(True, saved['id'])
        except Exception:
            await database.update_agy_account_telemetry(saved['id'], last_error='Не удалось обновить квоты после входа')
        public_acc = await database.get_account_profile_by_id(saved['id'])
        flow.account = public_acc
        flow.status = 'completed'
        flow.error = ''
        await _close_agy_oauth_flow_resources(flow, wait_server=not from_loopback)
        if flow.on_completed is not None:
            try:
                res = flow.on_completed()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass
        return public_acc or saved


def _build_loopback_html_page(ok: bool, title: str, detail: str) -> bytes:
    color = '#34d399' if ok else '#f87171'
    body_html = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{html.escape(title)}</title>'
        '<style>body{margin:0;min-height:100vh;display:grid;place-items:center;'
        'background:#0f1117;color:#e5e7eb;font:15px/1.5 system-ui,sans-serif;padding:20px}'
        '.card{max-width:440px;padding:24px 26px;border-radius:14px;background:#171b26;'
        'border:1px solid #2a3142;text-align:center}'
        f'h2{{margin:0 0 8px;font-size:18px;color:{color}}}p{{margin:0;color:#9ca3af;font-size:13.5px}}</style>'
        f'</head><body><div class="card"><h2>{html.escape(title)}</h2>'
        f'<p>{html.escape(detail)}</p></div></body></html>'
    ).encode('utf-8')
    status_line = 'HTTP/1.1 200 OK\r\n' if ok else 'HTTP/1.1 400 Bad Request\r\n'
    headers = (
        status_line
        + 'Content-Type: text/html; charset=utf-8\r\n'
        + f'Content-Length: {len(body_html)}\r\n'
        + 'Cache-Control: no-store\r\n'
        + 'Connection: close\r\n\r\n'
    ).encode('ascii')
    return headers + body_html


async def start_agy_oauth_flow(
    *,
    label: str = '',
    proxy_mode: Optional[str] = None,
    proxy_spec: Optional[str] = None,
    proxy_label: Optional[str] = None,
    make_active: bool = True,
    on_completed: Optional[Callable[[], Any]] = None,
) -> dict[str, Any]:
    await _cleanup_expired_agy_oauth_flows()
    flow_id = f'agy_oauth_{secrets.token_hex(8)}'
    owner_key = f'agy:oauth:{flow_id}'
    try:
        await proxy_router.ensure_proxy_for_context(
            'agy',
            pending_owner=owner_key,
            pending_mode=proxy_mode or 'global',
            pending_spec=proxy_spec,
        )
        cid, _ = await asyncio.to_thread(_oauth_pair, {})
    except BaseException:
        await asyncio.to_thread(proxy_router.release_owner, owner_key)
        raise

    state_tok = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = _pkce_s256_challenge(verifier)
    flow_holder: dict[str, AgyOAuthFlow] = {}

    async def _handle_loopback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            # Drain remaining headers gently
            for _ in range(40):
                hline = await asyncio.wait_for(reader.readline(), timeout=2.0)
                if not hline or hline in (b'\r\n', b'\n'):
                    break
            parts = req_line.decode('utf-8', errors='replace').strip().split(' ')
            raw_target = parts[1] if len(parts) >= 2 else '/'
            if raw_target.startswith('/favicon'):
                writer.write(b'HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n')
                await writer.drain()
                return
            fl = flow_holder.get('flow')
            if fl is None:
                writer.write(_build_loopback_html_page(False, 'Сессия не найдена', 'Запустите вход заново в окне Antigravity Hub.'))
                await writer.drain()
                return
            try:
                code, rx_state = extract_google_oauth_code_and_state('http://127.0.0.1' + raw_target)
                if rx_state != fl.state:
                    raise ValueError('Несовпадение параметра безопасности state.')
                acc = await _finalize_agy_oauth_flow_with_code(fl, code, rx_state, from_loopback=True)
                email_disp = str((acc or {}).get('email') or (acc or {}).get('label') or 'Google AI')
                writer.write(_build_loopback_html_page(
                    True,
                    '✓ Аккаунт Google AI подключён!',
                    f'Учётка {email_disp} успешно добавлена в Antigravity Hub. Можете закрыть эту вкладку браузера.',
                ))
                await writer.drain()
            except Exception as exc:
                msg = str(exc) if isinstance(exc, (ValueError, OAuthTemporaryError, proxy_router.ProxyRouteError)) else 'Не удалось завершить вход Google OAuth.'
                if fl.status == 'pending':
                    fl.status = 'error'
                    fl.error = msg
                    await _close_agy_oauth_flow_resources(fl, wait_server=False)
                writer.write(_build_loopback_html_page(False, 'Ошибка подключения аккаунта', msg))
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    try:
        server = await asyncio.start_server(_handle_loopback, host='127.0.0.1', port=0)
        port = int(server.sockets[0].getsockname()[1])
    except BaseException:
        await asyncio.to_thread(proxy_router.release_owner, owner_key)
        raise

    redirect_uri = f'http://127.0.0.1:{port}/oauth-callback'
    auth_url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode({
        'client_id': cid,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': ' '.join(AGY_OAUTH_SCOPES),
        'access_type': 'offline',
        'prompt': 'consent',
        'state': state_tok,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    })
    flow = AgyOAuthFlow(
        flow_id=flow_id,
        state=state_tok,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
        auth_url=auth_url,
        label=label,
        proxy_mode=proxy_mode,
        proxy_spec=proxy_spec,
        proxy_label=proxy_label,
        make_active=make_active,
        server=server,
        on_completed=on_completed,
    )
    flow_holder['flow'] = flow
    _AGY_OAUTH_FLOWS[flow_id] = flow
    exp_task = asyncio.create_task(_auto_expire_agy_oauth_flow(flow_id))
    _AGY_OAUTH_EXPIRY_TASKS.add(exp_task)
    exp_task.add_done_callback(_AGY_OAUTH_EXPIRY_TASKS.discard)
    flow.expiry_task = exp_task
    return flow.to_public_dict()


async def get_agy_oauth_flow_status(flow_id: str) -> Optional[dict[str, Any]]:
    await _cleanup_expired_agy_oauth_flows()
    fl = _AGY_OAUTH_FLOWS.get(str(flow_id or '').strip())
    return fl.to_public_dict() if fl is not None else None


async def complete_agy_oauth_flow(flow_id: str, code_or_url: str) -> dict[str, Any]:
    await _cleanup_expired_agy_oauth_flows()
    fl = _AGY_OAUTH_FLOWS.get(str(flow_id or '').strip())
    if fl is None:
        raise KeyError('Сессия входа Google OAuth не найдена или истекла.')
    code, rx_state = extract_google_oauth_code_and_state(code_or_url)
    await _finalize_agy_oauth_flow_with_code(fl, code, rx_state)
    return fl.to_public_dict()


async def cancel_agy_oauth_flow(flow_id: str) -> bool:
    fl = _AGY_OAUTH_FLOWS.pop(str(flow_id or '').strip(), None)
    if fl is None:
        return False
    fl.status = 'cancelled'
    await _close_agy_oauth_flow_resources(fl)
    return True
