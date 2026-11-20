"""v129: isolated context, reply, multi-return and local Git regression coverage."""
import ast
import asyncio
import re
import subprocess
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_project_rename_delete as mutations
import test_smart_hq_return_v115 as isolation

ROOT = Path(__file__).resolve().parents[1]


def test_local_git_excludes_private_data():
    assert (ROOT / ".git").is_dir()
    result = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                            cwd=ROOT, capture_output=True, text=True,
                            **isolation._subprocess_hidden_kwargs())
    assert result.returncode == 0
    for line in result.stdout.splitlines():
        assert not re.search(r"^(data|users|shared_projects|\.venv)/|\.db", line[3:])


def test_large_digests_and_stats(tmp_path, monkeypatch):
    from app import database as db, main
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "isolated.db")
    async def run():
        await db.init_db()
        admin = await db.get_user_by_id("admin")
        chats = []
        texts = []
        for i in range(2):
            chat = await db.create_chat(kind="regular", owner_id="admin", title=f"Report{i}")
            text = f"START{i} " + ("complete report text " * 1800) + f" END{i}"
            await db.add_message(chat["id"], role="user", content="Task")
            await db.add_message(chat["id"], role="assistant", content=text)
            chats.append(chat)
            texts.append(text)
            for scope in ("last", "full"):
                digest = await db.build_referenced_chat_digest(chat["id"], scope=scope)
                assert text in digest["block"]
                assert digest["chars"] == len(digest["block"])
                assert not digest["truncated"]
            stats = await main.api_chat_mention_stats(chat["id"], current_user=admin)
            assert stats["last"]["tokens"] > 5000
            assert not stats["full"]["truncated"]
        with patch.object(main, "_get_chat_access_context", AsyncMock(return_value=(False, None))):
            try:
                await main.api_chat_mention_stats(chats[0]["id"], current_user=admin)
                assert False, "RBAC must reject"
            except main.HTTPException as exc:
                assert exc.status_code == 403
        await db.close_db_pool()
    asyncio.run(run())


def test_last_digest_truncation_flags(tmp_path, monkeypatch):
    from app import database as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "last.db")
    async def run():
        await db.init_db()
        chat = await db.create_chat(kind="regular", owner_id="admin", title="Long report")
        text = "START " + "report paragraph " * 10_000 + " END"
        msg = await db.add_message(chat["id"], role="assistant", content=text)
        for cap in (None, 1000, 120_000):
            digest = await db.build_referenced_chat_digest(chat["id"], scope="last", max_chars=cap)
            assert digest["truncated"] is True
            assert " END" not in digest["block"]
            assert digest["chars"] <= (cap or db.MAX_REFERENCED_CHAT_LAST_CHARS)
        await db.update_message(msg["id"], content="Short answer")
        digest = await db.build_referenced_chat_digest(chat["id"], scope="last")
        assert digest["truncated"] is False
        # The fallback to the last user message must report cuts too.
        user_only = await db.create_chat(kind="regular", owner_id="admin", title="User only")
        await db.add_message(user_only["id"], role="user", content=text)
        digest = await db.build_referenced_chat_digest(user_only["id"], scope="last", max_chars=1000)
        assert digest["truncated"] is True
        await db.close_db_pool()
    asyncio.run(run())


def test_bookmark_markers_in_all_scopes(tmp_path, monkeypatch):
    from app import database as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bookmarks.db")
    async def run():
        await db.init_db()
        chat = await db.create_chat(kind="regular", owner_id="admin", title="Bookmarks")
        await db.add_message(chat["id"], role="user", content="Keep the configuration")
        msg = await db.add_message(chat["id"], role="assistant", content="Configuration confirmed. " * 1200)
        await db.set_message_bookmarked(chat["id"], msg["id"], True)
        for scope in ("full", "last", "summary"):
            digest = await db.build_referenced_chat_digest(chat["id"], scope=scope)
            assert "🔖" in digest["block"]
        await db.update_chat(chat["id"], compacted_up_to_msg_id=msg["id"], compacted_summary="Saved summary")
        digest = await db.build_referenced_chat_digest(chat["id"], scope="full")
        assert "🔖" in digest["block"]
        assert "Configuration confirmed. " * 1199 in digest["block"]
        await db.close_db_pool()
    asyncio.run(run())


def test_review_without_diff_limits_reads():
    from app import main
    for status in ("no_git", "no_paths", "clean", "error"):
        directive = main._build_hq_acceptance_directive("review", commands_count=0, diff_status=status)
        assert "3–5" in directive
        assert "НЕ перечитывай все затронутые файлы подряд" in directive


class ContextTurnsV129Tests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(mutations.ProjectMutationTests.setUpClass.__func__)
    tearDownClass = classmethod(mutations.ProjectMutationTests.tearDownClass.__func__)
    asyncSetUp = mutations.ProjectMutationTests.asyncSetUp
    asyncTearDown = mutations.ProjectMutationTests.asyncTearDown
    _assert_live_db_untouched = mutations.ProjectMutationTests._assert_live_db_untouched

    async def test_multi_return_union_diff_reply_and_persistence(self):
        main, db = self.main_mod, self.database
        root = self.root / "AlphaProj"
        def git(*args):
            result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                    **isolation._subprocess_hidden_kwargs())
            self.assertEqual(result.returncode, 0, result.stderr)
        git("init")
        for name in ("one.py", "two.py"):
            (root / name).write_text("old\n")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-m", "base")
        sources = []
        for name in ("one.py", "two.py"):
            (root / name).write_text("new\n")
            chat = await db.create_chat(kind="project", project_name="AlphaProj", owner_id="admin", title=name)
            await db.add_message(chat["id"], role="user", content="Task context")
            msg = await db.add_message(chat["id"], role="assistant", content="report " * 4500,
                tool_calls=[{"name":"write_file", "parameters":{"Path":str(root / name)}}])
            sources.append({"chat_id":chat["id"], "message_id":msg["id"], "return_uid":name, "review_mode":"review"})
        hq = await db.create_chat(kind="project", project_name="AlphaProj", owner_id="admin", title="HQ")
        original = "LONG_REPLY_START " + "full source " * 1500 + " LONG_REPLY_END"
        quoted = await db.add_message(hq["id"], role="assistant", content=original)
        await db.update_chat(hq["id"], compacted_up_to_msg_id=quoted["id"], compacted_summary="summary")
        prompt = f'Accept both\n<REPLY_QUOTE mode="message" msg_id="{quoted["id"]}">preview</REPLY_QUOTE>'
        captured = []
        async def stream(**kw):
            captured.append(kw)
            yield {"type":"done", "content":"accepted", "tool_calls":[], "usage":{}}
        admin = await db.get_user_by_id("admin")
        with ExitStack() as stack:
            for obj, name, replacement in [
                (main,"fetch_available_models",AsyncMock(return_value=self.agy_engine.DEFAULT_MODELS)),
                (main,"get_cli_auth_status",lambda: {"ok":True}),
                (main.openrouter_service,"is_user_openrouter_bridge_active",AsyncMock(return_value=False)),
                (main.sandbox_service,"is_user_sandbox_eligible",lambda user: False),
                (main.host_service_manager,"is_user_host_eligible",lambda user: False),
                (main,"_refresh_and_broadcast_quotas",AsyncMock()),
                (main.ws_manager,"broadcast_for_chat",AsyncMock()),
                (main,"stream_agy_turn",stream)]:
                stack.enter_context(patch.object(obj,name,replacement))
            await main._run_turn_task_inner(chat_id=hq["id"], prompt=prompt, attachments=[],
                override_model="gemini-4-carbon-high", override_access_mode="read_only",
                trigger_user=admin, hq_return_sources=sources,
                referenced_chats=[{"id":s["chat_id"],"scope":"full"} for s in sources])
            self.assertIn(original, captured[0]["prompt"])
            self.assertIn("one.py", captured[0]["prompt"])
            self.assertIn("two.py", captured[0]["prompt"])
            self.assertIn("HQ_RETURN_GIT_DIFF", captured[0]["prompt"])
            self.assertEqual(len(captured[0]["referenced_chat_blocks"]), 2)
            for src in sources:
                self.assertTrue(await db.has_message_hq_return_uid(src["chat_id"],src["message_id"],hq["id"],src["return_uid"]))
            await main._run_turn_task_inner(chat_id=hq["id"], prompt=prompt, attachments=[],
                override_model="gemini-4-carbon-high", override_access_mode="read_only",
                trigger_user=admin, hq_return_sources=sources)
            self.assertEqual(len(captured),1)
            for scope in ("last", "full"):
                explicit = await db.create_chat(kind="project", project_name="AlphaProj", owner_id="admin", title="Explicit")
                await main._run_turn_task_inner(chat_id=explicit["id"], prompt="Compare reports", attachments=[],
                    override_model="gemini-4-carbon-high", override_access_mode="read_only", trigger_user=admin,
                    referenced_chats=[{"id":src["chat_id"],"scope":scope} for src in sources])
                self.assertEqual(len(captured[-1]["referenced_chat_blocks"]), 2)
                for block in captured[-1]["referenced_chat_blocks"]:
                    self.assertIn(("report " * 4500).strip(), block)
        saved = [m for m in await db.get_messages(hq["id"]) if m["role"]=="user"][-1]
        self.assertEqual(saved["content"],prompt)
        self.assertEqual(len(saved["usage"]["hq_return_sources"]),2)

    def test_reply_live_tail_and_recovery_cap(self):
        expand = self.main_mod._expand_reply_quote_for_turn_prompt
        text = ("complete source " * 1200).strip()
        msgs = [{"id":7, "role":"assistant", "content":text}]
        prompt = '<REPLY_QUOTE mode="message" msg_id="7">preview</REPLY_QUOTE>'
        chat = {"last_engine":"agy", "agy_conversation_id":"live", "compacted_up_to_msg_id":0}
        self.assertEqual(expand(prompt,msgs,chat,"agy",100000),prompt)
        self.assertIn(text,expand(prompt,msgs,{**chat,"compacted_up_to_msg_id":7},"agy",100000))
        self.assertIn(text,expand(prompt,msgs,chat,"codex",100000))
        old = msgs + [{"id":8,"role":"user"},{"id":9,"role":"assistant"}]
        self.assertIn(text,expand(prompt,old,chat,"agy",100000))
        self.assertNotIn(text,expand(prompt,msgs,{**chat,"agy_conversation_id":""},"agy",0))
        selection = prompt.replace('mode="message"','mode="selection"')
        self.assertEqual(expand(selection,msgs,chat,"codex",100000),selection)

    def test_node_vm_final_status_and_multi_draft(self):
        tree = ast.parse(Path(isolation.__file__).read_text(encoding="utf-8"))
        harness = next(n.value.value for n in ast.walk(tree) if isinstance(n,ast.Assign)
            and any(isinstance(t,ast.Name) and t.id=="node_harness" for t in n.targets))
        harness += r"""
const done129 = {status:"done",content:"ok",tool_calls:[
  {name:"run_command",state:"ERROR",parameters:{CommandLine:"bad"}},
  {name:"run_command",state:"DONE",parameters:{CommandLine:"pytest"}}]};
assert.equal(api.buildExecutionFactSummaryFromMsg(done129,{}).hasErrors,false);
assert.equal(api.buildExecutionFactSummaryFromMsg({...done129,status:"error"},{}).hasErrors,true);
assert.equal(api.buildExecutionFactSummaryFromMsg({...done129,tool_calls:[{name:"run_command",state:"ERROR",parameters:{CommandLine:"bad"}}]},{}).hasErrors,true);
const first129 = {sourceChatId:"one",sourceMsgId:1,sourceTitle:"One",reportBodyText:"X".repeat(26000),reportFastText:"X".repeat(26000),reportReviewText:"review1",reviewMode:"fast"};
const second129 = {...first129,sourceChatId:"two",sourceMsgId:2,sourceTitle:"Two",reportBodyText:"Y".repeat(27000),reportFastText:"Y".repeat(27000)};
api.state.pendingHqReturn=first129;
api.state.pendingHqReturns=[first129,second129];
assert.equal(api.getEffectivePendingHqReturns().length,2);
api.renderPendingHqReturnPreview();
assert.ok(api.els.pendingHqReturnPreview.innerHTML.includes('data-remove-pending-hq-idx="1"'));
let multiHtml129 = api.els.pendingHqReturnPreview.innerHTML;
assert.ok(multiHtml129.includes("↩ One"));
assert.ok(multiHtml129.includes("⚡ Быстрая приёмка"));
assert.ok(multiHtml129.includes("Отправить все (2) ↵"));
assert.ok(!multiHtml129.includes(">?<") && !multiHtml129.includes(">? ") && !multiHtml129.includes("\ufffd"));
api.togglePendingHqReturnMode();
assert.equal(second129.reviewMode,"review");
assert.ok(api.els.pendingHqReturnPreview.innerHTML.includes("🔍 Ревью с diff"));
const preview129 = api.trimReplyTextForPrompt("full source ".repeat(1500), "message");
assert.ok(!preview129.includes("полный текст находится выше"));
assert.ok(preview129.includes("подгружается сервером при необходимости"));
const selection129 = "S".repeat(12000);
assert.equal(api.trimReplyTextForPrompt(selection129, "selection"), selection129);
api.togglePendingHqReturnMode();
assert.equal(second129.reportBodyText.length,27000);
api.state.activeChatId="hq129";
api.els.promptInput.value="";
api.saveCurrentComposerDraft();
assert.equal(api.state.chatDrafts.get("chat:hq129").pendingHqReturns.length,2);
api.state.pendingHqReturn={sourceChatId:"legacy",reportBodyText:"old"};
assert.equal(api.getEffectivePendingHqReturns()[0].sourceChatId,"legacy");
console.log("V129_OK");
"""
        script = self.tmp_root / "v129_vm.js"
        script.write_text(harness,encoding="utf-8")
        result = subprocess.run(["node",str(script),str(ROOT/"app/static/app.js")],
            capture_output=True,text=True,encoding="utf-8",timeout=25,**isolation._subprocess_hidden_kwargs())
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn("V129_OK",result.stdout)
