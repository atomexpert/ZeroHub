"""Project mutations, isolated using the existing temporary workspace fixture."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException

_TESTS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _TESTS_DIR.parent
for _p in (str(_ROOT_DIR), str(_TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_smart_hq_return_v115 as isolation
import unittest


class ProjectMutationTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(isolation.SmartHqReturnV115Tests.setUpClass.__func__)
    asyncSetUp = isolation.SmartHqReturnV115Tests.asyncSetUp
    asyncTearDown = isolation.SmartHqReturnV115Tests.asyncTearDown
    _assert_live_db_untouched = isolation.SmartHqReturnV115Tests._assert_live_db_untouched

    @classmethod
    def tearDownClass(cls):
        from app import database
        database.close_db_pool_sync()
        if os.environ.get("HUB_ALLOW_EXTERNAL_LIVE_DB_ACTIVITY") != "1":
            assert isolation._stat_snapshot(isolation.LIVE_HUB_DB) == cls._live_db_before

    async def asyncSetUp(self):
        await isolation.SmartHqReturnV115Tests.asyncSetUp(self)
        await self.database.init_db()
        self.user = {"id": "admin", "username": "admin", "role": "admin"}
        self.root = self.config.PROJECTS_ROOT
        self.stop = patch.object(self.main_mod.sandbox_service, 'stop_project_sandboxes', AsyncMock(return_value=0))
        self.stop.start()
        self.addCleanup(self.stop.stop)
        self.sync = patch.object(self.main_mod.sandbox_service, 'sync_all_users_sandbox_cli_files', AsyncMock())
        self.sync.start()
        self.addCleanup(self.sync.stop)
        self.brain_reader.invalidate_projects_list_cache()

    async def chat(self, name='AlphaProj', owner='admin'):
        return await self.database.create_chat(kind='project', project_name=name, owner_id=owner, title='history')

    async def test_personal_rename_memory_order_and_isolation(self):
        db = self.database
        old = self.root / 'AlphaProj'
        chat = await self.chat()
        other = await self.chat(owner='someone_else')
        msg = await db.add_message(chat['id'], role='user', content='keep', attachments=[{'disk_path':str(old/'a.txt')}], usage={'code_handoffs':[{'project_name':'AlphaProj'}]})
        await db.save_project_memory(db.build_project_memory_scope_key('AlphaProj'), project_name='AlphaProj', passport_overview='memory')
        async with db.connect_db() as conn:
            await conn.execute("UPDATE users SET projects_order_json=? WHERE id='admin'", (json.dumps(['Before','AlphaProj','After']),))
            await conn.commit()
        result = await self.main_mod.api_rename_project('AlphaProj',self.main_mod.RenameProjectRequest(new_name='Renamed'),self.user)
        self.assertEqual(result['updated_chats_count'],1)
        self.assertFalse(old.exists())
        self.assertTrue((self.root/'Renamed').is_dir())
        renamed = await db.get_chat(chat['id'])
        self.assertEqual(renamed['project_name'],'Renamed')
        self.assertEqual(renamed['project_path'],str(self.root/'Renamed'))
        self.assertEqual((await db.get_chat(other['id']))['project_name'],'AlphaProj')
        async with db.connect_db() as conn:
            row = await (await conn.execute('SELECT project_name,migrated_from_paths_json FROM project_memories')).fetchone()
            self.assertEqual(row[0],'Renamed'); self.assertIn(str(old),json.loads(row[1]))
            order = await (await conn.execute("SELECT projects_order_json FROM users WHERE id='admin'")).fetchone()
            self.assertEqual(json.loads(order[0]),['Before','Renamed','After'])
        self.assertEqual((await db.get_messages(chat['id']))[0]['content'],'keep')
        messages = await db.get_messages(chat['id'])
        self.assertEqual(messages[0]['attachments'][0]['disk_path'], str(self.root/'Renamed'/'a.txt'))
        self.assertEqual(messages[0]['usage']['code_handoffs'][0]['project_name'], 'Renamed')

    async def test_os_lock_leaves_database_and_directory_unchanged(self):
        chat = await self.chat()
        main = self.main_mod
        with patch.object(Path, 'rename', side_effect=PermissionError('locked')):
            with self.assertRaises(HTTPException) as caught:
                await main.api_rename_project('AlphaProj',main.RenameProjectRequest(new_name='Locked'),self.user)
        self.assertEqual(caught.exception.status_code,409)
        self.assertTrue((self.root/'AlphaProj').exists())
        self.assertEqual((await self.database.get_chat(chat['id']))['project_name'],'AlphaProj')

    async def test_rename_transaction_failure_rolls_back_disk(self):
        main = self.main_mod
        with patch.object(self.database, 'rename_project_records', AsyncMock(side_effect=ValueError('conflict'))):
            with self.assertRaises(HTTPException):
                await main.api_rename_project('AlphaProj',main.RenameProjectRequest(new_name='RolledBack'),self.user)
        self.assertTrue((self.root/'AlphaProj').exists())
        self.assertFalse((self.root/'RolledBack').exists())

    async def test_host_registry_rename_purge_scoped(self):
        from app.host_service_manager import HOST_SERVICE_MANAGER as manager, _compute_scope_hash
        old = self.root/'AlphaProj';new = self.root/'Renamed'
        other = self.root/'Other';other.mkdir()
        with manager._lock:
            manager._ensure_loaded_locked()
            for path in (old,other):
                key = manager._make_key('AlphaProj','worker',path)
                manager._services[key] = {'name':'worker','project_name':'AlphaProj','project_dir':str(path),
                    'workdir':str(path), 'command':str(path/'job.py'),'scope_hash':_compute_scope_hash(path), 'status':'stopped'}
        self.assertEqual(manager.rename_project_in_registry('AlphaProj',old,'Renamed',new),1)
        with patch.object(manager,'control_service',return_value={}):
            self.assertEqual(manager.purge_project_services('Renamed',new),1)
        self.assertEqual(len(manager._project_entries('AlphaProj',other)),1)

    async def test_case_only_and_validation(self):
        old=self.root/'myproj';old.mkdir();(old/'README.md').write_text('# myproj\n',encoding='utf-8')
        new=self.brain_reader.rename_project_directory_on_disk(old,'MyProj',allowed_parent=self.root)
        self.assertEqual(new.name,'MyProj')
        self.assertEqual((new/'README.md').read_text(encoding='utf-8'),'# MyProj\n')
        for name in ['../bad','_bad','.bad','bad.','CON','Antigravity_Hub']:
            with self.assertRaises(ValueError): self.brain_reader.validate_new_project_name(name)
        with self.assertRaises(ValueError):
            self.brain_reader.remove_or_archive_project_directory(self.hub_base,allowed_parent=self.hub_base.parent,delete_files=True)

    def test_client_events_idempotent(self):
        script = r'''
const fs=require('fs'), assert=require('assert');
const source=fs.readFileSync('app/static/app.js','utf8');
const functions=source.slice(source.indexOf('  function projectStateObjects()'),source.indexOf('  function openProjectMutationModal('));
const state={chats:[{id:'c',project_name:'Old'}],openTabs:[{id:'c',project_name:'Old'}],
  activeChat:{id:'c',project_name:'Old'},projects:[{name:'Old'}],selectedProject:'Old',
  projectOrder:['Before','Old','After'], expandedProjects:new Set(['Old']),desktopSpoilerOpen:new Set(['Old']),archivedBranchSpoilersOpen:new Set(['old']),
  chatDrafts:new Map([['draft:project:old','draft']]),draftActivityTs:new Map([['draft:project:old',42]]),projectChatsCache:new Map(),
  projectMemory:{dataByProject:new Map()},hostServices:{itemsByProject:new Map()}};
const tabViewCache=new Map([['c',{activeChat:{id:'c',project_name:'Old'}}]]);
const localStorage={data:new Map([['agy_hub_project','Old']]),getItem(k){return this.data.get(k)},setItem(k,v){this.data.set(k,v)},removeItem(k){this.data.delete(k)}};
function saveOpenTabs(){} function saveExpandedProjectsState(){} function saveLocalProjectOrder(){}
function applyChatHeaderState(){} function updateWelcomeCopy(){} function renderWorkspaceTabs(){}
function loadProjects(){return Promise.resolve()} function refreshSidebarLists(){}
function clearDraftActivity(k){state.draftActivityTs.delete(k)} function purgeDeletedChatState(){}
function resetToNewChatDraft(){}
function applyTabUI(){}
eval(functions+`
applyProjectRenamedClientState('Old','New'); applyProjectRenamedClientState('Old','New');
assert.equal(state.activeChat.project_name,'New');
assert.equal(tabViewCache.get('c').activeChat.project_name,'New');
assert.deepEqual(state.projectOrder,['Before','New','After']);
assert(state.archivedBranchSpoilersOpen.has('new'));
assert.equal(state.chatDrafts.get('draft:project:new'),'draft');
const payload={project_name:'New',chats_action:'move_to_regular',moved_chat_ids:['c'],deleted_chat_ids:[],folder:'New'};
applyProjectDeletedClientState(payload); applyProjectDeletedClientState(payload);
assert.equal(state.activeChat.kind,'chat'); assert.equal(state.activeChat.folder,'New');
assert.equal(state.openTabs[0].project_name,'');
assert.equal(tabViewCache.get('c').activeChat.kind,'chat');
assert(!state.chatDrafts.has('draft:project:new'));
assert.deepEqual(state.projectOrder,['Before','After']);
`);
'''
        result = subprocess.run(['node','-e',script],capture_output=True,text=True,
            cwd=isolation.ROOT_DIR,**isolation._subprocess_hidden_kwargs())
        self.assertEqual(result.returncode,0,result.stderr)

    async def test_system_and_active_guards(self):
        main=self.main_mod
        for operation, body in [(main.api_rename_project,main.RenameProjectRequest(new_name='Nope')),(main.api_delete_project,main.DeleteProjectRequest())]:
            with self.assertRaises(HTTPException) as caught: await operation('Antigravity_Hub',body,self.user)
            self.assertEqual(caught.exception.status_code,403)
        chat=await self.chat()
        main.active_generations[chat['id']]={}
        try:
            with self.assertRaises(HTTPException) as caught:
                await main.api_rename_project('AlphaProj',main.RenameProjectRequest(new_name='Busy'),self.user)
            self.assertEqual(caught.exception.status_code,409)
        finally: main.active_generations.pop(chat['id'],None)

    async def test_delete_empty_skeleton_readonly(self):
        main=self.main_mod
        git=self.root/'AlphaProj'/'.git';git.mkdir(exist_ok=True)
        ro=git/'packed-refs';ro.write_text('ref');ro.chmod(stat.S_IREAD)
        chat=await self.chat();await self.database.add_message(chat['id'],role='user',content='delete')
        result=await main.api_delete_project('AlphaProj',main.DeleteProjectRequest(delete_files=True),self.user)
        self.assertIn(chat['id'],result['deleted_chat_ids'])
        self.assertFalse((self.root/'AlphaProj').exists())
        self.assertIsNone(await self.database.get_chat(chat['id']))
        self.assertEqual(await self.database.get_messages(chat['id']),[])

    async def test_code_confirmation_archive_and_move_history(self):
        main=self.main_mod;old=self.root/'AlphaProj';(old/'code.py').write_text('important')
        chat=await self.chat();await self.database.add_message(chat['id'],role='user',content='preserved')
        with self.assertRaises(HTTPException) as caught:
            await main.api_delete_project('AlphaProj',main.DeleteProjectRequest(delete_files=True,confirm_name='wrong'),self.user)
        self.assertEqual(caught.exception.status_code,400)
        self.assertTrue((old/'code.py').exists())
        result=await main.api_delete_project('AlphaProj',main.DeleteProjectRequest(chats_action='move_to_regular'),self.user)
        self.assertFalse(old.exists());self.assertEqual(Path(result['archived_disk_path'],'code.py').read_text(),'important')
        moved=await self.database.get_chat(chat['id'])
        self.assertEqual((moved['kind'],moved['project_name'],moved['folder']),('chat','','AlphaProj'))
        self.assertEqual((await self.database.get_messages(chat['id']))[0]['content'],'preserved')
        projects=self.brain_reader.list_projects_for_user(self.user,[])
        self.assertNotIn('AlphaProj',[p['name'] for p in projects])

    async def test_shared_admin_and_member(self):
        main=self.main_mod
        sp=await self.database.create_shared_project(name='TeamProject',created_by='admin',member_ids=['member'])
        user={'id':'member','username':'member','role':'user'}
        with self.assertRaises(HTTPException) as caught:
            await main.api_rename_project('TeamProject',main.RenameProjectRequest(new_name='RenamedTeam'),user)
        self.assertEqual(caught.exception.status_code,403)
        result=await main.api_rename_project('TeamProject',main.RenameProjectRequest(new_name='RenamedTeam'),self.user)
        self.assertTrue(result['is_shared'])
        self.assertEqual((await self.database.list_shared_projects(is_admin=True))[0]['name'],'RenamedTeam')

        # Shared project full delete with hyphenated name ("Photo-1"), member chat, scripts dir & read-only .git
        sp_photo = await self.database.create_shared_project(
            name='Photo-1', created_by='admin', member_ids=['member-1']
        )
        photo_dir = Path(sp_photo['path'])
        (photo_dir / 'scripts').mkdir(parents=True, exist_ok=True)
        (photo_dir / 'scripts' / 'run.py').write_text('print(1)\n', encoding='utf-8')
        git_dir = photo_dir / '.git'
        git_dir.mkdir(exist_ok=True)
        ro_pack = git_dir / 'pack.idx'
        ro_pack.write_text('idx', encoding='utf-8')
        ro_pack.chmod(stat.S_IREAD)
        member_chat = await self.chat(name='Photo-1', owner='member-1')
        await self.database.add_message(member_chat['id'], role='user', content='member turn')

        del_res = await main.api_delete_project(
            'Photo-1',
            main.DeleteProjectRequest(
                chats_action='delete', delete_files=True, confirm_name='Photo-1'
            ),
            self.user,
        )
        self.assertTrue(del_res['ok'])
        self.assertIn(member_chat['id'], del_res['deleted_chat_ids'])
        self.assertFalse(photo_dir.exists())
        self.assertIsNone(await self.database.get_chat(member_chat['id']))
        self.assertEqual(await self.database.get_messages(member_chat['id']), [])
        self.assertNotIn(
            'Photo-1',
            [p['name'] for p in await self.database.list_shared_projects(is_admin=True)],
        )


if __name__ == '__main__':
    unittest.main()
