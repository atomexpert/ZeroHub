"""v124 sharing regressions in the temporary v115 workspace."""
import unittest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
import test_project_rename_delete as mutations


class ProjectSharingTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(mutations.ProjectMutationTests.setUpClass.__func__)
    tearDownClass = classmethod(mutations.ProjectMutationTests.tearDownClass.__func__)
    asyncSetUp = mutations.ProjectMutationTests.asyncSetUp
    asyncTearDown = mutations.ProjectMutationTests.asyncTearDown
    _assert_live_db_untouched = mutations.ProjectMutationTests._assert_live_db_untouched
    chat = mutations.ProjectMutationTests.chat

    async def test_round_trip_preserves_code_memory_chats_and_uploads(self):
        db, main = self.database, self.main_mod
        old = self.root / 'AlphaProj'
        (old / 'code.txt').write_text('preserved', encoding='utf-8')
        chat = await self.chat()
        await db.add_message(chat['id'], role='user', content='history')
        await db.save_project_memory('user:admin:alphaproj', project_name='AlphaProj', passport_overview='passport', passport_rules='rules')
        upload = self.config.resolve_chat_uploads_dir(chat, owner_username='admin')
        upload.mkdir(parents=True, exist_ok=True)
        (upload / 'personal.txt').write_text('personal')
        with patch.object(main.host_service_manager.HOST_SERVICE_MANAGER, 'stop_all_project_services'), patch.object(main.host_service_manager.HOST_SERVICE_MANAGER, 'rename_project_in_registry'):
            result = await main.api_share_project('AlphaProj', main.ShareProjectRequest(execution_mode='host'), self.user)
            self.assertTrue(result['is_shared'])
            shared = self.config.SHARED_PROJECTS_DIR / 'AlphaProj'
            self.assertEqual((shared / 'code.txt').read_text(), 'preserved')
            self.assertFalse(old.exists())
            moved = await db.get_chat(chat['id'])
            self.assertEqual(moved['project_path'], str(shared))
            self.assertEqual(self.config.find_existing_chat_upload_file(chat['id'], 'personal.txt', chat=moved, owner_username='admin', is_shared_project=True), upload / 'personal.txt')
            shared_upload = self.config.SHARED_PROJECTS_DIR / '_uploads' / chat['id']
            shared_upload.mkdir(parents=True, exist_ok=True)
            (shared_upload / 'shared.txt').write_text('shared')
            async with db.connect_db() as conn:
                row = await (await conn.execute('SELECT scope_key,passport_overview,passport_rules FROM project_memories')).fetchone()
                self.assertEqual(tuple(row), ('shared:alphaproj', 'passport', 'rules'))
            await main.api_unshare_project('AlphaProj', self.user)
        self.assertEqual((old / 'code.txt').read_text(), 'preserved')
        self.assertEqual((await db.get_messages(chat['id']))[0]['content'], 'history')
        self.assertEqual(self.config.find_existing_chat_upload_file(chat['id'], 'shared.txt', chat=await db.get_chat(chat['id']), owner_username='admin'), shared_upload / 'shared.txt')
        async with db.connect_db() as conn:
            row = await (await conn.execute('SELECT scope_key,passport_overview FROM project_memories')).fetchone()
            self.assertEqual(tuple(row), ('user:admin:alphaproj', 'passport'))

    async def test_system_and_active_sharing_guards(self):
        main = self.main_mod
        for name in ('Antigravity_Hub', 'AlphaProj'):
            chat = await self.chat() if name == 'AlphaProj' else None
            if chat:
                main.active_generations[chat['id']] = {}
            try:
                for operation in (lambda: main.api_share_project(name, main.ShareProjectRequest(), self.user), lambda: main.api_unshare_project(name, self.user)):
                    with self.assertRaises(HTTPException) as caught:
                        await operation()
                    self.assertEqual(caught.exception.status_code, 409 if chat else 403)
            finally:
                if chat:
                    main.active_generations.pop(chat['id'], None)
