"""Admin-only Google account pool endpoints."""
import asyncio
import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from . import auth, config, database, proxy_router
from . import agy_engine as engine

router = APIRouter()


class ImportRequest(BaseModel):
    payload: str
    label: str = ''
    proxy_mode: Optional[str] = None
    proxy_spec: Optional[str] = None
    make_active: bool = True


class PatchRequest(BaseModel):
    label: Optional[str] = None
    email: Optional[str] = None
    plan_note: Optional[str] = None
    is_enabled: Optional[bool] = None
    auto_failover: Optional[bool] = None
    proxy_mode: Optional[str] = None
    proxy_spec: Optional[str] = None


class StartOAuthRequest(BaseModel):
    label: str = ''
    proxy_mode: Optional[str] = None
    proxy_spec: Optional[str] = None
    make_active: bool = True


class CompleteOAuthRequest(BaseModel):
    code_or_url: str = ''
    url: str = ''
    code: str = ''


async def pool_state(**extra):
    from .main import ws_manager
    payload = dict(ok=True, account_profile=await database.get_active_account_profile(),
        account_profiles=await database.list_account_profiles(), agy_summary=await database.get_agy_accounts_summary(),
        proxy_settings=await asyncio.to_thread(proxy_router.get_proxy_status_payload, is_admin=True), **extra)
    await ws_manager.broadcast_admins({'type':'account_profile_updated', **payload})
    return payload


@router.post('/api/account/profiles/import')
async def import_accounts(body: ImportRequest, _admin=Depends(auth.require_admin)):
    from .main import _validate_account_proxy_input
    try:
        items = engine.parse_agy_import_payload(body.payload, fallback_label=body.label)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    mode, spec, label = await _validate_account_proxy_input(proxy_mode=body.proxy_mode, proxy_spec=body.proxy_spec, is_admin=True)
    imported, errors = [], []
    try:
        route = await proxy_router.ensure_proxy_for_context('agy', pending_owner='agy:import', pending_mode=mode or 'global', pending_spec=spec)
        for index, item in enumerate(items):
            try:
                item_route = route
                if body.proxy_mode is None and body.proxy_spec is None:
                    existing = next((a for a in await database.list_account_profiles() if item.get('email') and '*' not in item['email'] and a['email'].lower() == item['email'].lower()), None)
                    if existing:
                        item_route = await proxy_router.ensure_proxy_for_context('agy', account_id=existing['id'])
                token = await engine.refresh_and_build_agy_token_payload(item, proxy_url=item_route or '')
                saved = await database.upsert_agy_account_from_credentials(json.dumps(token), email=token.get('email',''),
                    label=item.get('label') or None, proxy_mode=mode, proxy_spec=spec, proxy_label=label,
                    make_active=body.make_active and not imported)
                await engine.materialize_agy_account_home(saved)
                imported.append(saved)
            except Exception:
                errors.append({'index':index, 'error':'Не удалось импортировать аккаунт. Проверьте OAuth-токен и маршрут.'})
    finally:
        await asyncio.to_thread(proxy_router.release_owner, 'agy:import')
    await proxy_router.reload_routes_from_db()
    engine.invalidate_agy_route_caches()
    semaphore = asyncio.Semaphore(3)
    async def refresh(acc):
        async with semaphore:
            try:
                await engine.fetch_account_quotas(True, acc['id'])
            except Exception:
                await database.update_agy_account_telemetry(acc['id'], last_error='Не удалось обновить квоты')
    await asyncio.gather(*(refresh(a) for a in imported))
    return await pool_state(imported_count=len(imported), imported_accounts=[await database.get_account_profile_by_id(a['id']) for a in imported], errors=errors,
        quotas=(await database.get_active_account_profile() or {}).get('quotas', {}))


@router.post('/api/account/profiles/adopt-local')
async def adopt_local(_admin=Depends(auth.require_admin)):
    try:
        from .agy_credentials import read_local_cli_credentials, _lease_lock
        async with _lease_lock:
            raw = await asyncio.to_thread(read_local_cli_credentials)
        item = engine.parse_agy_import_payload(raw)[0]
    except (OSError, ValueError):
        raise HTTPException(400, 'Текущий вход CLI не найден или не содержит полного OAuth-токена.') from None
    # Preserve the currently configured account route on first adoption.
    active = await database.get_active_account_profile(include_decrypted_proxy=True)
    route = {k:active.get(k) for k in ('proxy_mode','proxy_spec','proxy_label')} if active else {}
    saved = await database.upsert_agy_account_from_credentials(raw, email=item['email'], auth_method='local_cli', make_active=True, **route)
    await engine.materialize_agy_account_home(saved)
    await proxy_router.reload_routes_from_db()
    engine.invalidate_agy_route_caches()
    return await pool_state()


@router.patch('/api/account/profiles/{profile_id}')
async def patch_account(profile_id: str, body: PatchRequest, _admin=Depends(auth.require_admin)):
    from .main import _validate_account_proxy_input
    values = body.model_dump(exclude_unset=True)
    if 'proxy_mode' in values or 'proxy_spec' in values:
        mode, spec, label = await _validate_account_proxy_input(proxy_mode=body.proxy_mode, proxy_spec=body.proxy_spec, is_admin=True)
        values.update(proxy_mode=mode, proxy_spec=spec, proxy_label=label)
    saved = await database.patch_account_profile(profile_id, **values)
    if not saved:
        raise HTTPException(404, 'Аккаунт не найден')
    await proxy_router.reload_routes_from_db()
    engine.invalidate_agy_route_caches()
    return await pool_state()


@router.post('/api/account/profiles/oauth/start')
async def start_oauth(body: StartOAuthRequest, _admin=Depends(auth.require_admin)):
    from .main import _validate_account_proxy_input
    from . import agy_accounts
    mode, spec, label = await _validate_account_proxy_input(
        proxy_mode=body.proxy_mode, proxy_spec=body.proxy_spec, is_admin=True
    )
    try:
        flow = await agy_accounts.start_agy_oauth_flow(
            label=body.label,
            proxy_mode=mode,
            proxy_spec=spec,
            proxy_label=label,
            make_active=body.make_active,
            on_completed=lambda: pool_state(),
        )
    except proxy_router.ProxyRouteError as exc:
        raise HTTPException(502, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {'ok': True, **flow}


@router.get('/api/account/profiles/oauth/{flow_id}')
async def get_oauth_status(flow_id: str, _admin=Depends(auth.require_admin)):
    from . import agy_accounts
    flow = await agy_accounts.get_agy_oauth_flow_status(flow_id)
    if flow is None:
        raise HTTPException(404, 'Сессия Google OAuth не найдена или истекла')
    if flow.get('status') == 'completed':
        return {
            'ok': True,
            **flow,
            'account_profile': await database.get_active_account_profile(),
            'account_profiles': await database.list_account_profiles(),
            'agy_summary': await database.get_agy_accounts_summary(),
        }
    return {'ok': True, **flow}


@router.post('/api/account/profiles/oauth/{flow_id}/complete')
async def complete_oauth(flow_id: str, body: CompleteOAuthRequest, _admin=Depends(auth.require_admin)):
    from . import agy_accounts
    raw_val = (body.code_or_url or body.url or body.code or '').strip()
    try:
        flow = await agy_accounts.complete_agy_oauth_flow(flow_id, raw_val)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from None
    except proxy_router.ProxyRouteError as exc:
        raise HTTPException(502, str(exc)) from None
    except agy_accounts.OAuthTemporaryError as exc:
        raise HTTPException(502, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return await pool_state(flow=flow, account=flow.get('account'))


@router.delete('/api/account/profiles/oauth/{flow_id}')
async def cancel_oauth(flow_id: str, _admin=Depends(auth.require_admin)):
    from . import agy_accounts
    cancelled = await agy_accounts.cancel_agy_oauth_flow(flow_id)
    return {'ok': True, 'cancelled': cancelled}


@router.post('/api/account/profiles/refresh-quotas')
async def refresh_quotas(_admin=Depends(auth.require_admin)):
    quotas = await engine.refresh_all_agy_accounts_quotas()
    return await pool_state(quotas=quotas)
