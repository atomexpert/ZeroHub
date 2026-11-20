"""Isolated long-session regression tests. Never opens the live database."""
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
import test_smart_hq_return_v115 as isolation


class LongSessionTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(isolation.SmartHqReturnV115Tests.setUpClass.__func__)
    # The original classmethod uses super(), so define teardown here.
    @classmethod
    def tearDownClass(cls):
        from app import database
        database.close_db_pool_sync()
        assert isolation._stat_snapshot(isolation.LIVE_HUB_DB) == cls._live_db_before

    asyncSetUp = isolation.SmartHqReturnV115Tests.asyncSetUp
    asyncTearDown = isolation.SmartHqReturnV115Tests.asyncTearDown
    _assert_live_db_untouched = isolation.SmartHqReturnV115Tests._assert_live_db_untouched

    async def test_compact_payload_and_brain_refs(self):
        db = self.database
        await db.init_db()
        chat = await db.create_chat(title='tools')
        brain = '12345678-abcd-abcd-abcd-123456789abc'
        payload = [{'name': 'write_file', 'state': 'DONE', 'step_index': 2,
                    'output': 'x' * 9000 + ' brain/' + brain,
                    'parameters': {'Content': 'SECRET', 'Target': 'SECRET', 'Replacement': 'SECRET',
                                   'Path': '/tmp/file', 'Arguments': {'command': 'echo hello', 'port': 8765, 'Script': 'SECRET'}}}]
        clean = db.compact_tool_calls_payload(payload)
        self.assertNotIn('SECRET', json.dumps(clean))
        self.assertEqual(clean[0]['output_len'], len(payload[0]['output']))
        self.assertEqual(clean[0]['parameters']['Arguments']['port'], 8765)
        self.assertEqual(clean[0]['parameters']['Path'], '/tmp/file')
        self.assertEqual(db.compact_tool_calls_payload(clean), clean)
        msg = await db.add_message(chat['id'], role='assistant', tool_calls=payload)
        self.assertTrue(await db.chat_references_brain_id(chat['id'], brain))
        self.assertEqual(msg['tool_calls'], clean)
        # Historical uncompressed rows are also compacted on read.
        async with db.connect_db() as conn:
            await conn.execute('UPDATE messages SET tool_calls_json = ? WHERE id = ?', (json.dumps(payload), msg['id']))
            await conn.commit()
        self.assertEqual((await db.get_messages(chat['id']))[0]['tool_calls'], clean)

    async def test_incremental_ledger_and_context_stats(self):
        db = self.database
        await db.init_db()
        chat = await db.create_chat(title='stats')
        db._reset_usage_ledger_cache()
        first = await db.add_message(chat['id'], role='assistant', content='answer', usage={'total_tokens': 10})
        await db._get_usage_ledger()
        old = db._USAGE_LEDGER_CACHE['rows_by_id'][first['id']]
        second = await db.add_message(chat['id'], role='assistant', content='next', usage={'total_tokens': 20})
        self.assertEqual(sum(r[-1] for r in await db._get_usage_ledger()), 30)
        self.assertIs(db._USAGE_LEDGER_CACHE['rows_by_id'][first['id']], old)
        await db.update_message(second['id'], usage={'total_tokens': 30})
        self.assertEqual(sum(r[-1] for r in await db._get_usage_ledger()), 40)
        for compact in (False, True):
            if compact:
                chat = await db.update_chat(chat['id'], compacted_summary='saved summary', compacted_up_to_msg_id=first['id'], compacted_count=1)
            expected = db.compute_chat_context_stats(chat, await db.get_messages(chat['id']))
            self.assertEqual(await db.compute_single_chat_context_stats(chat), expected)
        chat = await db.update_chat(chat['id'], compacted_summary='', compacted_up_to_msg_id=second['id'])
        self.assertEqual(await db.compute_single_chat_context_stats(chat), db.compute_chat_context_stats(chat, await db.get_messages(chat['id'])))
        await db.delete_chat(chat['id'])
        self.assertEqual(await db._get_usage_ledger(), [])

    async def test_maintenance_preserves_history(self):
        from app import storage_maintenance as sm
        db = self.database
        await db.init_db()
        chat = await db.create_chat(title='preserved')
        msg = await db.add_message(chat['id'], role='assistant', content='history', thinking='thoughts', usage={'total_tokens': 42})
        chat = await db.update_chat(chat['id'], compacted_summary='summary', compacted_count=1)
        async with db.connect_db() as conn:
            await conn.execute('UPDATE messages SET bookmarked=1, tool_calls_json=? WHERE id=?', (json.dumps([{'name':'read_file','output':'x'*2000000}]), msg['id']))
            await conn.commit()
        before_chat = await db.get_chat(chat['id'])
        orphan = self.config.UPLOADS_DIR / 'orphan-test'
        orphan.mkdir(parents=True)
        (orphan / 'data.txt').write_text('garbage')
        health = await sm.storage_health()
        self.assertGreater(health['excess_tool_bytes'], 1900000)
        result = await self.main_mod.api_system_maintenance({'role':'admin'})
        self.assertEqual(result['slimmed_messages'], 1)
        self.assertGreater(result['freed_bytes'], 1000000)
        self.assertTrue(result['vacuumed'])
        self.assertFalse(orphan.exists())
        self.assertEqual(await db.get_chat(chat['id']), before_chat)
        after = (await db.get_messages(chat['id']))[0]
        self.assertEqual((after['content'], after['thinking'], after['bookmarked'], after['usage']), ('history', 'thoughts', True, {'total_tokens':42}))
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            await self.main_mod.api_system_maintenance({'role':'user'})
        with self.assertRaises(HTTPException):
            await self.main_mod.api_storage_health({'role':'user'})

    async def test_delete_cleanup_protects_desktop_and_shared_trajectory(self):
        from app import storage_maintenance as sm
        db = self.database
        await db.init_db()
        cli = self.tmp_root / 'cli-test'
        (cli / 'conversations').mkdir(parents=True)
        conv = '12345678-abcd-abcd-abcd-123456789abc'
        trajectory = cli / 'conversations' / (conv + '.db')
        trajectory.write_text('IDE history')
        with patch.object(self.config, 'AGY_CLI_DATA_DIR', cli):
            chat = await db.create_chat(title='delete')
            chat = await db.update_chat(chat['id'], agy_conversation_id=conv)
            uploads = self.config.resolve_chat_uploads_dir(chat)
            uploads.mkdir(parents=True)
            (uploads / 'file.txt').write_text('upload')
            with patch.object(self.main_mod, '_get_chat_access_context', AsyncMock(return_value=(True,None))), patch.object(self.main_mod, 'cancel_chat_generation', AsyncMock()), patch.object(self.main_mod.ws_manager, 'broadcast_for_chat', AsyncMock()):
                await self.main_mod.api_delete_chat(chat['id'], {'id':'admin','role':'admin'})
            self.assertFalse(uploads.exists())
            self.assertFalse(trajectory.exists())
            trajectory.write_text('IDE history')
            await sm.cleanup_deleted_chat({**chat, 'source_desktop_id':conv})
            self.assertTrue(trajectory.exists())
            other = await db.create_chat(title='reference')
            await db.update_chat(other['id'], agy_conversation_id=conv)
            await sm.cleanup_deleted_chat(chat)
            self.assertTrue(trajectory.exists())

    async def test_active_generation_defers_vacuum(self):
        from app import storage_maintenance as sm
        await self.database.init_db()
        with patch.object(self.main_mod, 'active_generations', {'busy': object()}):
            report = await self.main_mod.api_system_maintenance({'role':'admin'})
        self.assertFalse(report['vacuumed'])

    async def test_js_history_window_and_fragment_eviction(self):
        source = (isolation.ROOT_DIR / 'app/static/app.js').read_text(encoding='utf-8')
        def function(name):
            start = source.index('  function ' + name + '(')
            end = source.index('\n  function ', start + 12)
            return source[start:end]
        code = """const assert = require('assert');
const HISTORY_WINDOW_INITIAL=30, HISTORY_UNFOLD_STEP=30, MAX_CACHED_DOM_FRAGMENTS=4, MAX_OPEN_TABS=16;
const state={messages:[],renderedStartIdx:70}; const tabViewCache=new Map();
let unfolded=-1; function unfoldHistoryTo(i){unfolded=i;state.renderedStartIdx=i;}
""" + function('getInitialHistoryStart') + function('ensureMessageRenderedInDom') + function('setTabViewCache') + """
const msgs = Array.from({length:100},(_,i)=>({id:i,role:'assistant'}));
assert.equal(getInitialHistoryStart(msgs),70);
assert.equal(getInitialHistoryStart(msgs.slice(0,36)),0);
msgs[80].status='compacted';assert.equal(getInitialHistoryStart(msgs),80);
msgs[80].status='done';msgs[10].role='system';assert.equal(getInitialHistoryStart(msgs),70);
state.messages=msgs;assert(ensureMessageRenderedInDom(12));assert.equal(unfolded,12);
for(let i=0;i<8;i++)setTabViewCache(String(i),{domFragment:{},domMessages:[],messages:[]});
assert.equal([...tabViewCache.values()].filter(e=>e.domFragment).length,4);
assert.equal(tabViewCache.get('0').domMessages,null);assert(tabViewCache.get('0').domDirty);
"""
        result = subprocess.run(['node','-e',code], capture_output=True, text=True, **isolation._subprocess_hidden_kwargs())
        self.assertEqual(result.returncode, 0, result.stderr)
