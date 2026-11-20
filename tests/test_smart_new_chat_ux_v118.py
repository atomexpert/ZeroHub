"""Isolated v118 behavior tests. No server or production database connections."""
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / 'app/static/app.js'

def function_source(name):
    source = JS.read_text(encoding='utf-8')
    start = source.index('  function ' + name + '(')
    end = source.index('\n  }', start) + 4
    return source[start:end]

def test_smart_draft_behavior():
    names = ['rememberLastProjectName', 'resolveCanonicalProjectName', 'resolveRecentOrActiveProjectName',
             'setNewDraftContext', 'startRegularChatDraft', 'startProjectBranchDraft',
             'syncPrimaryNewButtonUI', 'switchWelcomeDraftContext', 'getComposerDraftKey',
             'saveCurrentComposerDraft', 'restoreComposerDraft', 'getEffectivePendingHqReturns', 'syncBranchTopButton', 'renderWelcomeContextSwitch']
    setup = r'''
const assert = require('node:assert/strict');
const storage = new Map();
const localStorage = {setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
const state = {projects:[{name:'Antigravity_Hub'},{name:'Other'}],activeTab:'project',selectedProject:'Antigravity_Hub',
activeChatId:null,activeChat:null,viewingDesktopConv:null,lastProjectName:'',expandedProjects:new Set(),treeFilter:'all',
chatDrafts:new Map(),draftActivityTs:new Map(),pendingAttachments:[],pendingReferencedChats:[],pendingHqReturns:[],pendingHqReturn:null,voice:{}};
function element(){const classes=new Set();return {value:'',dataset:{},style:{setProperty(){}},classList:{toggle(k,on){on?classes.add(k):classes.delete(k)},contains:k=>classes.has(k)},focus(){},selectionStart:0,selectionEnd:0,setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b}}}
const els={promptInput:element(),newChatBtn:element(),newChatBtnLabel:element(),newProjectBranchTopBtn:element(),welcomeContextSwitch:element()};
const clonePendingForward=x=>x?{...x}:null,clonePendingReply=clonePendingForward;
const closeNewContextMenus=()=>{},clearTypingActivityTimer=()=>{};
const saveExpandedProjectsState=()=>{},saveTreeFilterState=()=>{},applyTabUI=()=>{},closeMobileSidebar=()=>{};
const clearDraftActivity=k=>state.draftActivityTs.delete(k);
const handleComposerActivityInput=()=>state.draftActivityTs.set(getComposerDraftKey(),{ts:1});
const getProjectAccentColor=()=> '#55aa77',escapeHtml=s=>String(s).replaceAll('"','&quot;');
const closeChatMentionMenu=()=>{},resizePromptInput=()=>{},renderPendingAttachments=()=>{},renderPendingReferencedChats=()=>{},renderPendingHqReturnPreview=()=>{},renderPendingForwardPreview=()=>{},renderPendingReplyPreview=()=>{},syncComposerState=()=>{},scheduleComposerModelHintCheck=()=>{};
function resetToNewChatDraft(opts={}) {assert.equal(opts.skipSaveDraft,true);state.activeChatId=null;state.activeChat=null;state.viewingDesktopConv=null;syncBranchTopButton('');restoreComposerDraft();}
'''
    checks = r'''
for(const filter of ['all','chats','projects']){state.treeFilter=filter;syncPrimaryNewButtonUI();assert.equal(els.newChatBtnLabel.textContent,filter==='projects'?'Проект':'Чат');assert.equal(els.newChatBtn.dataset.primaryAction,filter==='projects'?'project':'chat')}
state.treeFilter='projects';els.promptInput.value='project original';startRegularChatDraft();
assert.equal(state.activeTab,'chat');assert.equal(state.selectedProject,'');assert.equal(state.lastProjectName,'Antigravity_Hub');assert.equal(state.treeFilter,'all');
assert.equal(state.chatDrafts.get('draft:project:antigravity_hub').text,'project original');
assert.equal(els.promptInput.value,'');
state.chatDrafts.clear();els.promptInput.value='live';els.promptInput.selectionStart=2;els.promptInput.selectionEnd=3;
state.pendingAttachments=[{id:'file1'}];state.pendingReferencedChats=[{id:'regular',kind:'chat'},{id:'same',kind:'project',project_name:'Antigravity_Hub'},{id:'other',kind:'project',project_name:'Other'}];
state.pendingHqReturn={id:'hq'};state.pendingForward={id:'f'};state.pendingReply={id:'r'};
state.draftActivityTs.set('draft:chat',{ts:1});state.chatDrafts.set('draft:project:antigravity_hub',{text:'target',attachments:[{id:'file1'},{id:'file2'}]});
switchWelcomeDraftContext('project','antigravity_hub');
assert.equal(els.promptInput.value,'target\n\nlive');assert.equal(state.pendingAttachments.length,2);assert.deepEqual(state.pendingReferencedChats.map(x=>x.id),['same']);assert.equal(state.pendingHqReturn,null);
assert.equal(state.pendingForward.id,'f');assert.equal(state.pendingReply.id,'r');assert.equal(els.promptInput.selectionStart,10);
assert.equal(state.chatDrafts.has('draft:chat'),false);assert.equal(state.draftActivityTs.has('draft:chat'),false);
switchWelcomeDraftContext('chat');assert.equal(els.promptInput.value,'target\n\nlive');assert.equal(state.pendingAttachments.length,2);assert.equal(state.pendingReferencedChats.length,0);assert.equal(state.draftActivityTs.size,1);
state.treeFilter='chats';startProjectBranchDraft('Other');assert.equal(state.selectedProject,'Other');assert.equal(state.treeFilter,'all');assert.equal(state.lastProjectName,'Other');
state.activeChat={kind:'project',project_name:'Antigravity_Hub'};assert.equal(resolveRecentOrActiveProjectName(),'Antigravity_Hub');
syncBranchTopButton('Antigravity_Hub');assert.equal(els.newProjectBranchTopBtn.classList.contains('hidden'),false);assert.equal(els.newProjectBranchTopBtn.dataset.branchProj,'Antigravity_Hub');syncBranchTopButton('');assert.equal(els.newProjectBranchTopBtn.classList.contains('hidden'),true);
renderWelcomeContextSwitch();assert.match(els.welcomeContextSwitch.innerHTML,/is-project/);startRegularChatDraft();renderWelcomeContextSwitch();assert.match(els.welcomeContextSwitch.innerHTML,/is-regular/);
state.projects=[];assert.equal(resolveRecentOrActiveProjectName(),'');renderWelcomeContextSwitch();assert.match(els.welcomeContextSwitch.innerHTML,/data-wcs-create-proj/);
'''
    db = ROOT / 'data/hub.db'
    before = (db.stat().st_mtime_ns, db.stat().st_size)
    env = dict(os.environ, CUDA_VISIBLE_DEVICES='', NVIDIA_VISIBLE_DEVICES='none', ORT_DISABLE_ALL_GPU='1')
    hidden = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    with tempfile.TemporaryDirectory(prefix='hub-v118-') as tmp:
        script = Path(tmp) / 'test.cjs'
        script.write_text(setup + '\n'.join(function_source(n) for n in names) + checks, encoding='utf-8')
        result = subprocess.run(['node',str(script)],capture_output=True,text=True,encoding='utf-8',env=env,**hidden)
    assert result.returncode == 0, result.stderr
    assert before == (db.stat().st_mtime_ns, db.stat().st_size)

def test_entry_points_and_accordion():
    s = JS.read_text(encoding='utf-8')
    accordion = s.split('headerEl.addEventListener("click", () => {',1)[1].split('\n          });',1)[0]
    assert 'rememberLastProjectName(pName)' in accordion
    assert 'expandedProjects' in accordion
    assert not any(x in accordion for x in ['selectedProject =','activeTab =','resetToNewChatDraft'])
    for name in ['newRegularChatBtn','mobileNewChatBtn','newTabTopBtn']:
        handler=s.split('els.'+name+'.addEventListener("click", () => {',1)[1].split('});',1)[0]
        assert 'startRegularChatDraft()' in handler
    assert 'syncBranchTopButton(chat.kind === "project" ? chat.project_name : "")' in s
    assert 'syncBranchTopButton("")' in function_source('resetToNewChatDraft')
    assert '!state.activeChatId && !state.viewingDesktopConv && state.activeTab' in s

def test_assets_and_themes():
    html=(ROOT/'app/static/index.html').read_text(encoding='utf-8')
    sw=(ROOT/'app/static/sw.js').read_text(encoding='utf-8')
    css=(ROOT/'app/static/styles.css').read_text(encoding='utf-8')
    css_version = re.search(r'styles\.css\?v=(\d+)', html)
    js_version = re.search(r'app\.js\?v=(\d+)', html)
    assert css_version and js_version
    ver = css_version.group(1)
    assert ver == js_version.group(1) and int(ver) >= 118
    assert f'agy-hub-v{ver}' in sw
    assert html.index('id="newActionPopover"') < html.index(f'/static/app.js?v={ver}')
    for name in ['styles.css','app.js']:
        assert f'{name}?v={ver}' in html and f'{name}?v={ver}' in sw
    assert css.count('{') == css.count('}')
    assert '@media(max-width:760px) {.proj-branch-plus-btn {display:none}}' in css
    v118_css = css.split('/* v118:', 1)[1].split('/* ', 1)[0]
    assert 'var(--bg-surface)' in v118_css and 'var(--text-primary)' in v118_css
    mutation_css = css.split('/* Project rename / safe delete', 1)[1].split('*/', 1)[1].split('/* ', 1)[0]
    for styles in [v118_css, mutation_css]:
        assert not re.search(r'#[0-9a-fA-F]{3,8}\b', styles)
    for f in [JS,ROOT/'app/static/index.html',ROOT/'app/static/styles.css',ROOT/'app/static/sw.js']:
        assert not f.read_bytes().startswith(b'\xef\xbb\xbf')
