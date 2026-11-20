"""Fast HQ acceptance: isolated VM, git and turn integration."""
import ast
import subprocess
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import test_project_rename_delete as mutations
import test_smart_hq_return_v115 as isolation


class FastHqTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(mutations.ProjectMutationTests.setUpClass.__func__)
    tearDownClass = classmethod(mutations.ProjectMutationTests.tearDownClass.__func__)
    asyncSetUp = mutations.ProjectMutationTests.asyncSetUp
    asyncTearDown = mutations.ProjectMutationTests.asyncTearDown
    _assert_live_db_untouched = mutations.ProjectMutationTests._assert_live_db_untouched

    async def test_git_filter_untracked_and_cap(self):
        root = self.root / 'AlphaProj'
        def git(*args):
            result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, **isolation._subprocess_hidden_kwargs())
            self.assertEqual(result.returncode, 0, result.stderr)
        git('init')
        (root / 'tracked.txt').write_text('old\n')
        git('add', 'tracked.txt')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-m', 'initial')
        (root / 'tracked.txt').write_text('new\n')
        (root / 'new.txt').write_text('new-untracked\n')
        (root / '.env').write_text('SECRET_MARKER')
        msg = {'tool_calls':[{'name':'write_file', 'parameters':{'Path':str(root / name)}} for name in ('tracked.txt', 'new.txt', '.env', '../escape.txt')]}
        block = await self.main_mod._build_hq_return_git_diff_block(root, msg)
        self.assertIn('<HQ_RETURN_GIT_DIFF files="2">', block)
        self.assertIn('+new', block)
        self.assertIn('(new untracked file)', block)
        self.assertNotIn('SECRET_MARKER', block)
        self.assertNotIn('escape', block)
        (root / 'tracked.txt').write_text('x' * 20000)
        block = await self.main_mod._build_hq_return_git_diff_block(root, msg)
        self.assertLessEqual(len(block), 14000)
        self.assertTrue(block.endswith('</HQ_RETURN_GIT_DIFF>'))

    async def test_review_injection_only_in_model_prompt(self):
        main, db = self.main_mod, self.database
        src = await db.create_chat(kind='project', project_name='AlphaProj', owner_id='admin', title='Worker')
        msg = await db.add_message(src['id'], role='assistant', content='report')
        admin = await db.get_user_by_id('admin')
        captured = []
        async def stream(**kwargs):
            captured.append(kwargs['prompt'])
            yield {'type':'done', 'content':'accepted', 'tool_calls':[], 'usage':{}}
        for mode, allowed in [('fast', True), ('review', True), ('review', False)]:
            hq = await db.create_chat(kind='project', project_name='AlphaProj', owner_id='admin', title='HQ')
            async def access(chat, user):
                return (allowed if chat['id'] == src['id'] else True), None
            diff = '<HQ_RETURN_GIT_DIFF files="1">\nDIFF_MARKER\n</HQ_RETURN_GIT_DIFF>'
            with patch.object(main,'fetch_available_models',AsyncMock(return_value=[{'id':'gemini-4-carbon-high','label':'Gemini'}])), patch.object(main,'get_cli_auth_status',return_value={'ok':True}), patch.object(main.openrouter_service,'is_user_openrouter_bridge_active',AsyncMock(return_value=False)), patch.object(main.sandbox_service,'is_user_sandbox_eligible',return_value=False), patch.object(main.host_service_manager,'is_user_host_eligible',return_value=False), patch.object(main,'_refresh_and_broadcast_quotas',AsyncMock()), patch.object(main,'stream_agy_turn',side_effect=stream), patch.object(main.ws_manager,'broadcast_for_chat',AsyncMock()), patch.object(main,'_build_hq_return_git_diff_block',AsyncMock(return_value=diff)) as build, patch.object(main,'_get_chat_access_context',AsyncMock(side_effect=access)):
                await main._run_turn_task_inner(chat_id=hq['id'], prompt='report text', attachments=[], override_model='gemini-4-carbon-high', override_access_mode='read_only', trigger_user=admin, hq_return_source={'chat_id':src['id'], 'message_id':msg['id'], 'review_mode':mode})
            self.assertEqual('DIFF_MARKER' in captured[-1], mode == 'review' and allowed)
            self.assertEqual(build.await_count, int(mode == 'review' and allowed))
            user = [m for m in await db.get_messages(hq['id']) if m['role']=='user'][0]
            self.assertEqual(user['content'], 'report text')

    def test_frontend_modes_toggle_and_encoding(self):
        html = (isolation.ROOT_DIR/'app/static/index.html').read_text(encoding='utf-8')
        self.assertIn('↻ Обновить квоты', html.split('id="refreshQuotasBtn"',1)[1].split('</button>',1)[0])
        self.assertNotIn('????', html[html.index('id="refreshQuotasBtn"')-1000:html.index('id="refreshQuotasBtn"')+2500])
        tree = ast.parse(Path(isolation.__file__).read_text(encoding='utf-8'))
        harness = next(n.value.value for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='node_harness' for t in n.targets))
        harness += r'''
const msg126 = {content:"Finished", tool_calls:[]};
const chat126 = {id:"worker126",title:"Worker"};
const fast126 = api.buildReturnToHqPrompt(msg126,chat126);
assert.ok(!fast126.includes("⚡ [Режим быстрой приёмки Штаба"));
assert.ok(api.buildReturnToHqPrompt(msg126,chat126,{includeDirective:true}).includes("⚡ [Режим быстрой приёмки Штаба"));
const error126 = {content:"Failed", tool_calls:[{name:"edit_file",state:"FAILED"}]};
assert.ok(api.buildReturnToHqPrompt(error126,chat126,{includeDirective:true}).includes("🔍 [Режим код-ревью Штаба"));
assert.ok(api.buildReturnToHqPrompt(error126,chat126,{reviewMode:"fast",includeDirective:true}).includes("⚡ [Режим"));
api.state.pendingHqReturn = {sourceChatId:"worker126",sourceTitle:"Worker",reviewMode:"fast",reportFastText:fast126,reportReviewText:api.buildReturnToHqPrompt(msg126,chat126,{reviewMode:"review"}),reportBodyText:fast126};
api.togglePendingHqReturnMode();
assert.equal(api.state.pendingHqReturn.reviewMode,"review");
assert.ok(!api.state.pendingHqReturn.reportBodyText.includes("🔍 [Режим"));
assert.ok(api.els.pendingHqReturnPreview.innerHTML.includes('data-toggle-pending-hq-mode="1"'));
api.togglePendingHqReturnMode();
assert.equal(api.state.pendingHqReturn.reportBodyText, fast126);
console.log("V126_OK");
'''
        script = self.tmp_root/'v126.js'
        script.write_text(harness, encoding='utf-8')
        result = subprocess.run(['node',str(script),str(isolation.ROOT_DIR/'app/static/app.js')], capture_output=True,text=True,encoding='utf-8',timeout=25,**isolation._subprocess_hidden_kwargs())
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('V126_OK',result.stdout)
