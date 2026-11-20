"""v125: isolated Google pool regression; no Google network or live database."""
import asyncio
import json
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_smart_hq_return_v115 as isolation


class AgyMultiAccountTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(isolation.SmartHqReturnV115Tests.setUpClass.__func__)
    @classmethod
    def tearDownClass(cls):
        from app import database
        database.close_db_pool_sync()
        assert isolation._stat_snapshot(isolation.LIVE_HUB_DB) == cls._live_db_before
    _assert_live_db_untouched = isolation.SmartHqReturnV115Tests._assert_live_db_untouched

    async def asyncSetUp(self):
        await isolation.SmartHqReturnV115Tests.asyncSetUp(self)
        self.local = self.tmp_root / 'global_cli'
        self.local.mkdir()
        for name in ('builtin','mcp','brain'):
            (self.local / name).mkdir()
            (self.local / name / 'keep').write_text('keep')
        for mod in (self.config, self.agy_engine):
            p = patch.object(mod, 'AGY_CLI_DATA_DIR', self.local)
            p.start(); self._patches.append(p)
        p = patch.object(self.database, 'get_or_create_hub_secret', return_value='v125-isolated-test-secret')
        p.start(); self._patches.append(p)
        await self.database.init_db()

    async def asyncTearDown(self):
        # Remove junctions before TemporaryDirectory cleanup on Windows.
        for acc in await self.database.list_account_profiles():
            self.config.cleanup_agy_account_dirs(acc['id'])
        await isolation.SmartHqReturnV115Tests.asyncTearDown(self)

    async def account(self, email='a@gmail.com', **kwargs):
        return await self.database.upsert_agy_account_from_credentials(json.dumps({'refresh_token':'1//tokA','access_token':'fake','expiry':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}), email=email, **kwargs)

    async def test_parser_formats_and_mask(self):
        parse = self.agy_engine.parse_agy_import_payload
        self.assertEqual(len(parse('[{"email":"a@gmail.com","refresh_token":"1//tokA"},{"email":"b@gmail.com","refreshToken":"1//tokB"}]')),2)
        self.assertEqual(parse('{"refresh_token":"1//tokA"}')[0]['refresh_token'],'1//tokA')
        self.assertEqual(parse('1//tokA')[0]['refresh_token'],'1//tokA')
        self.assertEqual(parse('a@gmail.com:1//tokA\n1//tokB')[0]['email'],'a@gmail.com')
        with self.assertRaisesRegex(ValueError,'замаскированный'):
            parse('1/***1Y')
        with self.assertRaises(ValueError):
            parse('{}')

    async def test_encryption_dedupe_and_public_api(self):
        a = await self.account(label='Keep', proxy_mode='custom',proxy_spec='http://127.0.0.1:4321')
        again = await self.account('A@GMAIL.COM')
        self.assertEqual(a['id'],again['id'])
        self.assertEqual(again['label'],'Keep')
        self.assertEqual(again['proxy_mode'],'custom')
        profiles = await self.main_mod.api_list_account_profiles({'role':'admin'})
        self.assertNotIn('1//tokA',json.dumps(profiles))
        self.assertNotIn('credentials_json_encrypted',json.dumps(profiles))
        internal = await self.database.get_account_profile_by_id(a['id'],include_decrypted_credentials=True,include_decrypted_proxy=True)
        self.assertIn('1//tokA',internal['credentials_json'])
        self.assertEqual(internal['proxy_spec'],'http://127.0.0.1:4321')
        async with self.database.connect_db() as db:
            row = await (await db.execute('SELECT credentials_json_encrypted FROM account_profiles')).fetchone()
        self.assertNotIn('1//tokA',row[0])

    async def test_pause_activation_and_pool_setting(self):
        a,b = await self.account(), await self.account('b@gmail.com')
        await self.database.patch_account_profile(a['id'],is_enabled=False,auto_failover=False)
        self.assertEqual((await self.database.get_active_account_profile())['id'],b['id'])
        self.assertTrue(all(not p['auto_failover'] for p in await self.database.list_account_profiles()))
        self.assertIsNone(await self.database.activate_account_profile(a['id']))
        summary = await self.database.get_agy_accounts_summary()
        self.assertEqual(summary['enabled_count'],1)

    async def test_filesystem_isolation_and_safe_cleanup(self):
        a,b = await self.account(), await self.account('b@gmail.com')
        home = await self.agy_engine.materialize_agy_account_home(a)
        other = await self.agy_engine.materialize_agy_account_home(b)
        self.assertNotEqual((home/'.gemini/antigravity-cli').resolve(),(other/'.gemini/antigravity-cli').resolve())
        self.assertFalse((self.local/'antigravity-oauth-token').exists())
        self.assertEqual((home/'.gemini/config').resolve(),(self.config.CLI_HOMES_DIR/'admin/.gemini/config').resolve())
        self.config.cleanup_agy_account_dirs(a['id'])
        for name in ('builtin','mcp','brain'):
            self.assertEqual((self.local/name/'keep').read_text(),'keep')
        self.assertTrue((other/'.gemini/antigravity-cli/antigravity-oauth-token').is_file())
        with self.assertRaises(ValueError):
            self.config.ensure_agy_account_cli_data_dir('../escape')

    async def test_family_reset_and_failover_selection(self):
        from app.agy_accounts import agy_pool_exhausted, _pick_next_agy_failover_account
        a,b = await self.account(), await self.account('b@gmail.com')
        future = (datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()
        quotas = dict(gemini_5h_pct=85,claude_5h_pct=0,groups=[dict(id='claude_gpt',buckets=[dict(window='5h',remaining_pct=0,reset_time=future)])])
        await self.database.update_agy_account_telemetry(a['id'],quotas=quotas)
        a = await self.database.get_account_profile_by_id(a['id'])
        self.assertTrue(agy_pool_exhausted(a,'claude_gpt'))
        self.assertFalse(agy_pool_exhausted(a,'gemini'))
        self.assertEqual((await _pick_next_agy_failover_account({a['id']},'claude_gpt'))['id'],b['id'])
        quotas['groups'][0]['buckets'][0]['reset_time'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        self.assertFalse(agy_pool_exhausted({'quotas':quotas},'claude_gpt'))
        self.assertFalse(self.agy_engine.is_agy_quota_exhausted_error('503 capacity temporarily unavailable'))
        self.assertTrue(self.agy_engine.is_agy_quota_exhausted_error('RESOURCE_EXHAUSTED quota exceeded'))

    async def test_delete_detaches_session_preserves_history(self):
        a = await self.account()
        chat = await self.database.create_chat(kind='regular',title='Keep',owner_id='admin')
        await self.database.add_message(chat['id'],role='user',content='history')
        await self.database.update_chat(chat['id'],agy_account_id=a['id'],agy_conversation_id='old')
        await self.database.delete_account_profile(a['id'])
        self.assertEqual((await self.database.get_chat(chat['id']))['agy_conversation_id'],'')
        self.assertEqual((await self.database.get_messages(chat['id']))[0]['content'],'history')

    async def test_oauth_route_fail_closed(self):
        with patch.object(self.agy_engine.proxy_router,'ensure_proxy_for_context',AsyncMock(side_effect=self.agy_engine.proxy_router.ProxyRouteError('closed'))), patch('urllib.request.build_opener') as opener:
            with self.assertRaises(self.agy_engine.proxy_router.ProxyRouteError):
                await self.agy_engine.refresh_and_build_agy_token_payload({'refresh_token':'1//tokA'}, account_id='agy_fake')
            opener.assert_not_called()

    async def test_refresh_exchange_and_credentials_sync(self):
        import io
        from app import agy_accounts
        from unittest.mock import MagicMock
        opener = MagicMock()
        opener.open.side_effect = [io.BytesIO(b'{"access_token":"access-new","expires_in":3600}'),io.BytesIO(b'{"email":"resolved@gmail.com"}')]
        with patch.object(agy_accounts,'_oauth_pair',return_value=('public-id','public-secret')), patch.object(agy_accounts.proxy_router,'ensure_proxy_for_context',AsyncMock(return_value='http://127.0.0.1:1234')), patch('urllib.request.build_opener',return_value=opener):
            token = await agy_accounts.refresh_and_build_agy_token_payload({'refresh_token':'1//fake','email':'x***'},account_id='agy_test')
        self.assertEqual(token['email'],'resolved@gmail.com')
        self.assertEqual(token['token']['access_token'],'access-new')
        self.assertEqual(len(opener.open.call_args_list),2)
        a = await self.account()
        home = await self.agy_engine.materialize_agy_account_home(a)
        (home/'.gemini/antigravity-cli/antigravity-oauth-token').write_text(json.dumps(token))
        await self.agy_engine.sync_refreshed_agy_credentials_back(a['id'])
        saved = await self.database.get_account_profile_by_id(a['id'],include_decrypted_credentials=True)
        self.assertEqual(json.loads(saved['credentials_json'])['token']['access_token'],'access-new')

    async def test_quotas_api_never_returns_credentials(self):
        await self.account()
        with patch.object(self.main_mod,'schedule_stale_telemetry_revalidation'):
            response = await self.main_mod.api_get_quotas(refresh=False,chat_id=None,current_user={'id':'admin','username':'admin','role':'admin'})
        self.assertNotIn('1//tokA',json.dumps(response))
        self.assertNotIn('credentials_json_encrypted',json.dumps(response))

    async def test_windows_keyring_lease_restores_original_on_success_and_failure(self):
        import os
        if os.name != 'nt':
            self.skipTest('Windows keyring lease')
        from app import agy_credentials
        from unittest.mock import MagicMock
        a = await self.account()
        home = await self.agy_engine.materialize_agy_account_home(a)
        path = home/'.gemini/antigravity-cli/antigravity-oauth-token'
        path.write_text(json.dumps({'token':{'refresh_token':'1//fake','access_token':'fake','expiry':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}}))
        original = {'blob':b'original-host-session'.hex(),'persist':2,'username':'antigravity'}
        state = dict(value=original)
        store = MagicMock()
        store.read.side_effect = lambda: state['value']
        store.write.side_effect = lambda v: state.update(value=v)
        proc = MagicMock(returncode=0)
        proc.wait = AsyncMock(return_value=0)
        proc.stdout = asyncio.StreamReader()
        proc.stdout.feed_data(b'{"event":"message"}\n')
        proc.communicate = AsyncMock(return_value=(b'{}',b''))
        kwargs = {'env':{'USERPROFILE':str(home)}}
        with patch.object(agy_credentials,'WindowsCredentialStore',return_value=store), patch.object(agy_credentials.asyncio,'create_subprocess_exec',AsyncMock(return_value=proc)):
            leased = await agy_credentials.create_agy_subprocess_exec('fake',**kwargs)
            self.assertNotEqual(state['value'],original)
            self.assertEqual(await leased.stdout.readline(), b'{"event":"message"}\n')
            self.assertEqual(state['value'], original)
            self.assertFalse(agy_credentials._lease_lock.locked())
            await leased.communicate()
            self.assertEqual(state['value'],original)
        with patch.object(agy_credentials,'WindowsCredentialStore',return_value=store), patch.object(agy_credentials.asyncio,'create_subprocess_exec',AsyncMock(side_effect=OSError('fake launch failure'))):
            with self.assertRaises(OSError):
                await agy_credentials.create_agy_subprocess_exec('fake',**kwargs)
            self.assertEqual(state['value'],original)
        self.assertFalse((self.config.DATA_DIR/'agy_host_session_backup.encrypted').exists())

    async def test_proactive_stream_switches_only_claude_bucket(self):
        a,b = await self.account(), await self.account('b@gmail.com')
        future = (datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()
        await self.database.update_agy_account_telemetry(a['id'], quotas={'gemini_5h_pct':85,'claude_5h_pct':0,
            'groups':[{'id':'claude_gpt','buckets':[{'window':'5h','remaining_pct':0,'reset_time':future}]}]})
        stream = self.agy_engine.stream_agy_turn(chat_id='fake',kind='regular',project_name='',model='claude-sonnet-5-5-high',access_mode='read_only',prompt='hi',attachments=[],agy_conversation_id='old',history_messages=[])
        event = await anext(stream)
        self.assertEqual(event['type'],'agy_failover')
        self.assertEqual(event['account_id'],b['id'])
        await stream.aclose()
        self.assertEqual((await self.database.get_active_account_profile())['id'],a['id'])

        # Stopping at environment construction proves no failover was emitted or activated.
        await self.database.activate_account_profile(a['id'])
        for model, auto in [('gemini-4-carbon-high',True), ('claude-sonnet-5-5-high',False)]:
            await self.database.patch_account_profile(a['id'], auto_failover=auto)
            with patch.object(self.agy_engine.proxy_router,'ensure_proxy_for_context',AsyncMock(return_value=None)), patch.object(self.agy_engine,'_build_subprocess_env',side_effect=RuntimeError('test-stop-before-launch')):
                stream = self.agy_engine.stream_agy_turn(chat_id='fake',kind='regular',project_name='',model=model,access_mode='read_only',prompt='hi',attachments=[],agy_conversation_id='old',history_messages=[])
                with self.assertRaisesRegex(RuntimeError,'test-stop-before-launch'):
                    await anext(stream)
                await stream.aclose()
            self.assertEqual((await self.database.get_active_account_profile())['id'],a['id'])

    async def test_static_contract(self):
        root = Path(__file__).resolve().parents[1]/'app/static'
        html_txt = (root/'index.html').read_text(encoding='utf-8')
        sw_txt = (root/'sw.js').read_text(encoding='utf-8')
        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_txt)
        self.assertIsNotNone(m_sw)
        ver = int(m_sw.group(1))
        self.assertGreaterEqual(ver, 129)
        self.assertIn(f'?v={ver}', html_txt)
        css = (root/'styles.css').read_text(encoding='utf-8').split('/* v125:',1)[1]
        self.assertIsNone(re.search(r'#[0-9a-fA-F]{3,8}\b',css))

    async def test_expiring_token_refresh_and_route_failure(self):
        from app import agy_accounts
        a = await self.account()
        old = {'token': {'access_token':'old', 'refresh_token':'1//fake', 'expiry':(datetime.now(timezone.utc)+timedelta(seconds=300)).isoformat()}}
        fresh = {'token': {**old['token'], 'access_token':'new', 'expiry':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}}
        await self.database.sync_agy_account_credentials_json(a['id'], json.dumps(old))
        with patch.object(agy_accounts, 'refresh_and_build_agy_token_payload', AsyncMock(return_value=fresh)) as refresh:
            home = await agy_accounts.materialize_agy_account_home(a)
            refresh.assert_awaited_once()
        self.assertEqual(json.loads((home/'.gemini/antigravity-cli/antigravity-oauth-token').read_text())['token']['access_token'], 'new')
        saved = await self.database.get_account_profile_by_id(a['id'], include_decrypted_credentials=True)
        self.assertEqual(json.loads(saved['credentials_json']), fresh)
        await self.database.sync_agy_account_credentials_json(a['id'], json.dumps(old))
        with patch.object(agy_accounts, 'refresh_and_build_agy_token_payload', AsyncMock(side_effect=agy_accounts.proxy_router.ProxyRouteError('closed'))):
            with self.assertRaises(agy_accounts.proxy_router.ProxyRouteError):
                await agy_accounts.materialize_agy_account_home(a)

    async def test_startup_lease_stdout_eof_and_silent_timeout(self):
        from app import agy_credentials as creds
        from unittest.mock import MagicMock
        for output in (b'first\n', b''):
            reader = asyncio.StreamReader()
            reader.feed_data(output)
            reader.feed_eof()
            done = asyncio.Event()
            proc = MagicMock(stdout=reader, returncode=None)
            proc.wait = AsyncMock(side_effect=done.wait)
            restore = MagicMock()
            await creds._lease_lock.acquire()
            leased = creds.LeasedProcess(proc, restore)
            self.assertEqual(await leased.stdout.readline(), output)
            self.assertFalse(creds._lease_lock.locked())
            self.assertFalse(done.is_set())
            await leased.finish_lease()
            restore.assert_called_once()
        # A silent, long-running process also releases the lock independently.
        await creds._lease_lock.acquire()
        proc.stdout = asyncio.StreamReader()
        restore = MagicMock()
        leased = creds.LeasedProcess(proc, restore, max_hold_sec=0.02)
        await asyncio.sleep(0.06)
        self.assertFalse(creds._lease_lock.locked())
        restore.assert_called_once()

    async def test_temporary_refresh_failure_only_uses_valid_token(self):
        from app import agy_accounts
        a = await self.account()
        for seconds, allowed in [(300, True), (30, False)]:
            old = {'token':{'access_token':'old', 'refresh_token':'1//fake', 'expiry':(datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()}}
            await self.database.sync_agy_account_credentials_json(a['id'], json.dumps(old))
            with patch.object(agy_accounts, 'refresh_and_build_agy_token_payload', AsyncMock(side_effect=agy_accounts.OAuthTemporaryError('temporary'))):
                if allowed:
                    await agy_accounts.materialize_agy_account_home(a)
                else:
                    with self.assertRaises(agy_accounts.OAuthTemporaryError):
                        await agy_accounts.materialize_agy_account_home(a)
