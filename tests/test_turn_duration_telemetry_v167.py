# -*- coding: utf-8 -*-
"""Isolated regression suite for v167 turn duration telemetry & live stopwatch.

Covers:
1. Backend `merge_assistant_usage` summing `duration_ms` and updating `last_run_duration_ms`
   for single runs and resumed (`continue_turn`) runs.
2. Full turn lifecycle (`done`, `cancelled`, resumed `continue_turn`, `error`, unexpected exception):
   recording positive `duration_ms` & `last_run_duration_ms` in `messages.usage_json`,
   broadcasting `gen_started_at_ms` on `assistant_start`, exposing `gen_started_at_ms` via
   `api_get_chat` while `streaming`, and cleaning up `_CHAT_GEN_STARTED_TS_MS` in `finally`.
3. Frontend & Headless Chromium (`1440x900`, `390x844`, `320x568` in `dark` and `light`):
   `formatTurnDuration` / `formatLiveStreamElapsed` edge cases, desktop vs mobile `.usage-hint`
   visibility without horizontal overflow, duration-only and legacy token-only messages,
   interrupted empty-body `.msg-continue-bar` hint, live `.msg-live-elapsed` ticking without
   mutating header slot `data-sig` across `text_delta`, and exclusion from copied text.

Strict invariants:
- 0% GPU (`CUDA_VISIBLE_DEVICES=""`, `ORT_DISABLE_ALL_GPU="1"`).
- 0 touches to live `data/hub.db`, `.env`, `users/` or the running server.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["ORT_DISABLE_ALL_GPU"] = "1"

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from test_ui_workspace_minimal_v142 import run_workspace_chromium


BROWSER_CHECKS = r"""
function check(ok, message) {
  if (!ok) throw new Error(message);
}

function normSpaces(str) {
  return String(str || "").replace(/[\u00a0\u202f]/g, " ");
}

function visibleTextOf(root) {
  let out = "";
  const w = root.ownerDocument.defaultView;
  function walk(node) {
    if (node.nodeType === 3) {
      out += node.nodeValue || "";
      return;
    }
    if (node.nodeType !== 1) return;
    if (w.getComputedStyle(node).display === "none") return;
    for (const child of node.childNodes) walk(child);
  }
  walk(root);
  return normSpaces(out);
}

async function measure(width, height) {
  const frame = document.createElement("iframe");
  frame.style.cssText = "border:0;position:absolute;width:" + width + "px;height:" + height + "px";
  const loaded = new Promise(resolve => { frame.onload = resolve; });
  frame.srcdoc = workspacePage;
  document.body.append(frame);
  try {
    await loaded;
    const w = frame.contentWindow;
    const d = frame.contentDocument;
    const api = w.__AGY_ADVISOR_API__;
    const isMobileCompact = width <= 480;

    // 1. Pure formatting boundary checks (once per frame)
    check(api.formatTurnDuration(0) === "", "0 ms should format empty");
    check(api.formatTurnDuration(-500) === "", "negative ms should format empty");
    check(api.formatTurnDuration(null) === "", "null should format empty");
    check(api.formatTurnDuration(50) === "0.1 с" && api.formatTurnDuration(50, { compact: true }) === "0.1с", "50ms min clamp");
    check(api.formatTurnDuration(4200) === "4.2 с" && api.formatTurnDuration(4200, { compact: true }) === "4.2с", "4200ms formatting");
    check(api.formatTurnDuration(9950) === "10 с" && api.formatTurnDuration(9950, { compact: true }) === "10с", "9950ms rollover to 10s");
    check(api.formatTurnDuration(38000) === "38 с" && api.formatTurnDuration(38000, { compact: true }) === "38с", "38s formatting");
    check(api.formatTurnDuration(59600) === "1 мин" && api.formatTurnDuration(59600, { compact: true }) === "1м", "59600ms rollover to 1 min");
    check(api.formatTurnDuration(74000) === "1 мин 14 с" && api.formatTurnDuration(74000, { compact: true }) === "1м 14с", "74000ms formatting");
    check(api.formatTurnDuration(120000) === "2 мин" && api.formatTurnDuration(120000, { compact: true }) === "2м", "120000ms exact minutes");
    check(api.formatTurnDuration(3900000) === "1 ч 05 мин" && api.formatTurnDuration(3900000, { compact: true }) === "1ч 05м", "3900000ms hours formatting");
    check(api.formatTurnDuration(3600000) === "1 ч" && api.formatTurnDuration(3600000, { compact: true }) === "1ч", "3600000ms exact hour");

    check(api.formatLiveStreamElapsed(0) === "0:00", "0ms live elapsed");
    check(api.formatLiveStreamElapsed(7800) === "0:07", "7.8s live elapsed");
    check(api.formatLiveStreamElapsed(84000) === "1:24", "84s live elapsed");
    check(api.formatLiveStreamElapsed(845000) === "14:05", "845s live elapsed");
    check(api.formatLiveStreamElapsed(3905000) === "1:05:05", "1h5m5s live elapsed");

    // 2. Render full set of assistant cards inside the real chat workspace
    const t0 = 1760000000000;
    const chatObj = { id: "chat-v167", kind: "chat", title: "Telemetry v167", is_generating: false };
    const msgBoth = {
      id: "m-both",
      chat_id: "chat-v167",
      role: "assistant",
      status: "done",
      model: "gemini-3-pro",
      created_at: "2026-11-21T10:00:00Z",
      content: "Готовый ответ с длительностью и токенами.",
      usage: {
        total_tokens: 29998,
        input_tokens: 20000,
        output_tokens: 9998,
        duration_ms: 74000,
        last_run_duration_ms: 24000,
        proc_runs: 2,
      },
    };
    const msgDurOnly = {
      id: "m-dur-only",
      chat_id: "chat-v167",
      role: "assistant",
      status: "done",
      model: "gemini-3-pro",
      created_at: "2026-11-21T10:02:00Z",
      content: "Ответ только с длительностью без токенов.",
      usage: { duration_ms: 38000 },
    };
    const msgLegacy = {
      id: "m-legacy",
      chat_id: "chat-v167",
      role: "assistant",
      status: "done",
      model: "gemini-3-pro",
      created_at: "2026-11-21T10:03:00Z",
      content: "Старое сообщение только с токенами.",
      usage: { total_tokens: 29998, input_tokens: 20000, output_tokens: 9998 },
    };
    const msgInterruptedEmpty = {
      id: "m-empty-cancel",
      chat_id: "chat-v167",
      role: "assistant",
      status: "cancelled",
      model: "gemini-3-pro",
      created_at: "2026-11-21T10:04:00Z",
      content: "",
      tool_calls: [1, 2, 3, 4, 5].map(i => ({ step_index: i, name: "read_file", state: "done" })),
      usage: { duration_ms: 38000 },
    };
    const msgStreaming = {
      id: "m-stream",
      chat_id: "chat-v167",
      role: "assistant",
      status: "streaming",
      model: "gemini-3-pro",
      created_at: "2026-11-21T10:05:00Z",
      gen_started_at_ms: t0,
      content: "Привет",
      usage: { duration_ms: 10000 },
    };

    api.state.activeChatId = chatObj.id;
    api.state.activeChat = chatObj;
    api.state.viewingDesktopConv = null;
    api.state.generatingChats.clear();
    // First mount with msgInterruptedEmpty as the tail non-generating message to verify .msg-continue-bar
    api.state.messages = [msgBoth, msgDurOnly, msgLegacy, msgInterruptedEmpty];
    d.getElementById("welcomeState").classList.add("hidden");
    const listEl = d.getElementById("messagesList");
    listEl.innerHTML = "";
    for (const m of api.state.messages) {
      listEl.appendChild(api.buildMessageDom(m));
    }
    const contHintEarly = listEl.querySelector('[data-msg-id="m-empty-cancel"] .msg-continue-hint');
    check(contHintEarly && contHintEarly.textContent.includes("5 шаг. · 38 с"),
      "Interrupted empty continue hint mismatch: " + (contHintEarly && contHintEarly.textContent));

    // Now append the active streaming message for live stopwatch & slot stability tests
    chatObj.is_generating = true;
    api.state.messages.push(msgStreaming);
    listEl.appendChild(api.buildMessageDom(msgStreaming));
    api.syncLiveAssistantElapsedDom(t0 + 7000);

    const mc = d.getElementById("messagesContainer");
    for (const theme of ["dark", "light"]) {
      d.documentElement.dataset.theme = theme;

      // 2a. Combined duration + tokens card
      const bothRow = listEl.querySelector('[data-msg-id="m-both"]');
      const bothHint = bothRow.querySelector(".usage-hint");
      check(bothHint, "Missing .usage-hint on m-both");
      const durFullEl = bothHint.querySelector(".usage-dur-full");
      const durShortEl = bothHint.querySelector(".usage-dur-short");
      const tokFullEl = bothHint.querySelector(".usage-tok-full");
      const tokShortEl = bothHint.querySelector(".usage-tok-short");
      const sepEl = bothHint.querySelector(".usage-sep");
      check(durFullEl && durShortEl && tokFullEl && tokShortEl && sepEl, "Missing usage sub-elements on m-both");
      check(normSpaces(tokFullEl.textContent) === "29 998 tok", "tokFull text mismatch: " + tokFullEl.textContent);
      check(tokShortEl.textContent === "30.0k", "tokShort text mismatch: " + tokShortEl.textContent);
      check(normSpaces(bothHint.getAttribute("title")).includes("Время генерации: 1 мин 14 с (посл. прогон: 24 с)"),
        "Unexpected title: " + bothHint.getAttribute("title"));

      const visBoth = visibleTextOf(bothHint);
      if (isMobileCompact) {
        check(w.getComputedStyle(durFullEl).display === "none", "dur-full should be hidden on <=480px");
        check(w.getComputedStyle(durShortEl).display !== "none", "dur-short should be visible on <=480px");
        check(w.getComputedStyle(tokFullEl).display === "none", "tok-full should be hidden on <=480px");
        check(w.getComputedStyle(tokShortEl).display !== "none", "tok-short should be visible on <=480px");
        check(visBoth === "1м 14с · 30.0k", "Mobile visible usage mismatch: " + visBoth);
      } else {
        check(w.getComputedStyle(durFullEl).display !== "none", "dur-full should be visible on desktop");
        check(w.getComputedStyle(durShortEl).display === "none", "dur-short should be hidden on desktop");
        check(w.getComputedStyle(tokFullEl).display !== "none", "tok-full should be visible on desktop");
        check(w.getComputedStyle(tokShortEl).display === "none", "tok-short should be hidden on desktop");
        check(visBoth === "1 мин 14 с · 29 998 tok", "Desktop visible usage mismatch: " + visBoth);
      }
      check(w.getComputedStyle(bothHint).fontVariantNumeric.includes("tabular-nums"),
        ".usage-hint should use tabular-nums");

      // 2b. Duration-only message (no tokens, no stray separator)
      const durOnlyHint = listEl.querySelector('[data-msg-id="m-dur-only"] .usage-hint');
      check(durOnlyHint, "Missing .usage-hint on m-dur-only");
      check(!durOnlyHint.querySelector(".usage-sep"), "Stray .usage-sep on duration-only message");
      check(!durOnlyHint.querySelector(".usage-tok-full"), "Stray token span on duration-only message");
      const visDurOnly = visibleTextOf(durOnlyHint);
      check(visDurOnly === (isMobileCompact ? "38с" : "38 с"), "Duration-only visible text mismatch: " + visDurOnly);
      check(!visDurOnly.includes("·"), "Duration-only text must not contain dot separator");
      check(durOnlyHint.getAttribute("title") === "Время генерации: 38 с", "Duration-only title mismatch");

      // 2c. Legacy token-only message (no duration_ms)
      const legacyHint = listEl.querySelector('[data-msg-id="m-legacy"] .usage-hint');
      check(legacyHint, "Missing .usage-hint on m-legacy");
      check(!legacyHint.querySelector(".usage-dur"), "Legacy message must not render .usage-dur");
      check(!legacyHint.querySelector(".usage-sep"), "Legacy message must not render .usage-sep");
      const visLegacy = visibleTextOf(legacyHint);
      check(visLegacy === (isMobileCompact ? "30.0k" : "29 998 tok"), "Legacy visible text mismatch: " + visLegacy);

      // 2d. Interrupted empty-body message continue hint
      const contHint = listEl.querySelector('[data-msg-id="m-empty-cancel"] .msg-continue-hint');
      check(contHint && contHint.textContent.includes("5 шаг. · 38 с"),
        "Interrupted empty continue hint mismatch: " + (contHint && contHint.textContent));

      // 2e. Live streaming message stopwatch + slot sig stability + clipboard stripping
      const streamRow = listEl.querySelector('[data-msg-id="m-stream"]');
      const streamCard = streamRow.querySelector(".assistant-card");
      const headerSlot = streamCard.querySelector(".assistant-header-slot");
      const liveEl = streamCard.querySelector(".assistant-header > .msg-live-elapsed");
      check(liveEl, "Missing .msg-live-elapsed in streaming header");
      check(liveEl.dataset.genStartMs === String(t0) && liveEl.dataset.genBaseMs === "10000",
        "Unexpected live elapsed dataset: " + JSON.stringify(liveEl.dataset));
      check(w.getComputedStyle(liveEl).fontVariantNumeric.includes("tabular-nums"),
        ".msg-live-elapsed must use tabular-nums");

      api.syncLiveAssistantElapsedDom(t0 + 7000);
      check(liveEl.textContent === "0:17", "Expected 0:17 live elapsed, got: " + liveEl.textContent);

      const sigBeforeDelta = headerSlot.dataset.sig;
      msgStreaming.content = "Привет, мир! Тест потокового обновления.";
      api.updateSingleAssistantDom(msgStreaming);
      check(headerSlot.dataset.sig === sigBeforeDelta,
        "Header slot data-sig mutated on text_delta: " + sigBeforeDelta + " -> " + headerSlot.dataset.sig);
      check(streamCard.querySelector(".assistant-header > .msg-live-elapsed") === liveEl,
        "Live elapsed node was unnecessarily recreated on text_delta");

      api.syncLiveAssistantElapsedDom(t0 + 74000);
      check(liveEl.textContent === "1:24", "Expected 1:24 live elapsed, got: " + liveEl.textContent);

      check(liveEl.matches(api.COPY_STRIP_SELECTOR), ".msg-live-elapsed must match COPY_STRIP_SELECTOR");
      check(bothHint.matches(api.COPY_STRIP_SELECTOR), ".usage-hint must match COPY_STRIP_SELECTOR");
      const copiedStream = api.buildClipboardPayloadFromElement(streamCard);
      check(copiedStream.text.includes("Привет, мир!") && !copiedStream.text.includes("1:24"),
        "Live stopwatch leaked into copied text: " + copiedStream.text);
      const copiedBoth = api.buildClipboardPayloadFromElement(bothRow.querySelector(".assistant-card"));
      check(copiedBoth.text.includes("Готовый ответ") && !copiedBoth.text.includes("1 мин 14 с") && !copiedBoth.text.includes("29 998"),
        "Usage telemetry leaked into copied text: " + copiedBoth.text);

      // Geometry / zero horizontal overflow checks
      check(mc.scrollWidth <= mc.clientWidth + 1,
        "messagesContainer horizontal overflow: " + mc.scrollWidth + " > " + mc.clientWidth);
      for (const row of listEl.querySelectorAll(".msg-row")) {
        const card = row.querySelector(".assistant-card");
        if (card) {
          check(card.scrollWidth <= card.clientWidth + 1,
            row.dataset.msgId + " card overflow: " + card.scrollWidth + " > " + card.clientWidth);
        }
      }
      const bothCardRect = bothRow.querySelector(".assistant-card").getBoundingClientRect();
      const bothHintRect = bothHint.getBoundingClientRect();
      check(bothHintRect.width > 0 && bothHintRect.right <= bothCardRect.right + 1,
        "usage-hint clipped/overflowed card right edge");
    }

    // 3. Finishing the streaming card removes .msg-live-elapsed and shows .usage-hint
    msgStreaming.status = "done";
    msgStreaming.usage = { duration_ms: 84000, total_tokens: 5120 };
    api.updateSingleAssistantDom(msgStreaming);
    const finishedRow = listEl.querySelector('[data-msg-id="m-stream"]');
    check(!finishedRow.querySelector(".msg-live-elapsed"), "Live stopwatch remained after status=done");
    const finHint = finishedRow.querySelector(".usage-hint");
    check(finHint && visibleTextOf(finHint) === (isMobileCompact ? "1м 24с · 5.1k" : "1 мин 24 с · 5 120 tok"),
      "Finished streaming usage mismatch: " + (finHint && visibleTextOf(finHint)));

    api.stopLiveAssistantElapsedTimer();
    return [width, height];
  } catch (error) {
    throw new Error(width + "x" + height + " " + frame.contentDocument.documentElement.dataset.theme + " " + error.message);
  } finally {
    frame.remove();
  }
}

(async () => {
  const result = [];
  for (const [width, height] of [[1440, 900], [390, 844], [320, 568]]) result.push(await measure(width, height));
  document.getElementById("report").textContent = JSON.stringify({ result });
})().catch(error => {
  document.getElementById("report").textContent = JSON.stringify({ error: error.message });
});
"""


class TurnDurationBackendV167Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.config as config
        import app.database as database
        import app.main as main

        self.config = config
        self.database = database
        self.main = main

        self._live_db = (ROOT_DIR / "data" / "hub.db").resolve()
        self._live_db_before = (
            (self._live_db.stat().st_size, self._live_db.stat().st_mtime_ns)
            if self._live_db.exists()
            else None
        )

        self._tmp = tempfile.TemporaryDirectory(
            prefix="agy_v167_turn_dur_", ignore_cleanup_errors=True
        )
        self.tmp_root = Path(self._tmp.name).resolve()
        self.tmp_db = self.tmp_root / "hub.db"

        self._orig_cfg_db = config.DB_PATH
        self._orig_db_db = database.DB_PATH
        config.DB_PATH = self.tmp_db
        database.DB_PATH = self.tmp_db

        await database.init_db()
        self.user = await database.create_user(
            username="v167_tester",
            password="V167TestPassword!",
            display_name="V167 Tester",
            role="admin",
        )
        self.chat = await database.create_chat(
            owner_id=self.user["id"],
            kind="chat",
            title="Duration Test Chat",
            model="gemini-3-pro",
        )
        main._CHAT_GEN_STARTED_TS_MS.clear()

    async def asyncTearDown(self) -> None:
        self.main._CHAT_GEN_STARTED_TS_MS.clear()
        self.config.DB_PATH = self._orig_cfg_db
        self.database.DB_PATH = self._orig_db_db
        self._tmp.cleanup()
        after = (
            (self._live_db.stat().st_size, self._live_db.stat().st_mtime_ns)
            if self._live_db.exists()
            else None
        )
        self.assertEqual(self._live_db_before, after, "Live data/hub.db must remain untouched")

    async def test_merge_assistant_usage_duration_single_and_continue(self) -> None:
        m = self.main

        # Single run with duration_ms only -> derives last_run_duration_ms
        u_single = m.merge_assistant_usage(
            {},
            {"input_tokens": 10000, "output_tokens": 2000, "total_tokens": 12000, "duration_ms": 42000},
        )
        self.assertEqual(u_single["duration_ms"], 42000)
        self.assertEqual(u_single["last_run_duration_ms"], 42000)
        self.assertEqual(u_single["proc_runs"], 1)

        # Preserve code_handoffs and sum duration_ms across continue_turn while updating last_run_duration_ms
        u_single["code_handoffs"] = [{"target_chat_id": "c-code", "sent_at": "2026-11-21T10:00:00Z"}]
        u_resumed = m.merge_assistant_usage(
            u_single,
            {
                "input_tokens": 12000,
                "output_tokens": 3000,
                "total_tokens": 15000,
                "duration_ms": 32000,
                "last_run_duration_ms": 32000,
            },
            clear_last_error=True,
        )
        self.assertEqual(u_resumed["duration_ms"], 74000)
        self.assertEqual(u_resumed["last_run_duration_ms"], 32000)
        self.assertEqual(u_resumed["proc_runs"], 2)
        self.assertEqual(len(u_resumed["code_handoffs"]), 1)

        # Error merge with empty incoming keeps previous duration telemetry intact
        u_err = m.merge_assistant_usage(u_resumed, None, last_error="boom")
        self.assertEqual(u_err["duration_ms"], 74000)
        self.assertEqual(u_err["last_run_duration_ms"], 32000)
        self.assertEqual(u_err["last_error"], "boom")

    async def test_turn_lifecycle_done_cancelled_continue_error_and_exception(self) -> None:
        m = self.main
        db = self.database
        chat_id = self.chat["id"]
        broadcasts: list[dict[str, Any]] = []

        async def _fake_broadcast_for_chat(_chat: dict[str, Any], payload: dict[str, Any], **_kw: Any) -> None:
            broadcasts.append(payload)

        mid_stream_checks: dict[str, Any] = {}

        # 1) Done turn + mid-stream api_get_chat gen_started_at_ms check
        async def _stream_done(**_kw: Any):
            mid_stream_checks["in_map"] = chat_id in m._CHAT_GEN_STARTED_TS_MS
            mid_stream_checks["map_val"] = m._CHAT_GEN_STARTED_TS_MS.get(chat_id)
            chat_resp = await m.api_get_chat(chat_id, include_thinking=True, current_user=self.user)
            streaming_msgs = [
                msg for msg in chat_resp["messages"]
                if msg.get("role") == "assistant" and msg.get("status") == "streaming"
            ]
            mid_stream_checks["api_gen_started"] = (
                streaming_msgs[-1].get("gen_started_at_ms") if streaming_msgs else None
            )
            yield {"type": "text_delta", "delta": "Готово", "seq": 1}
            await asyncio.sleep(0.025)
            yield {
                "type": "done",
                "content": "Готово",
                "thinking": "",
                "tool_calls": [],
                "usage": {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500},
            }

        with (
            patch.object(m.ws_manager, "broadcast_for_chat", side_effect=_fake_broadcast_for_chat),
            patch.object(m, "stream_agy_turn", side_effect=_stream_done),
            patch.object(m, "_spawn_telemetry_task", side_effect=lambda coro: coro.close()),
            patch.object(m, "_maybe_generate_live_outline_title_task", new=AsyncMock(return_value=None)),
            patch.object(m, "_notify_gdrive_change_for_user", return_value=None),
            patch.object(m.openrouter_service, "is_user_openrouter_bridge_active", new=AsyncMock(return_value=False)),
            patch.object(m.sandbox_service, "is_user_sandbox_eligible", return_value=False),
            patch.object(m.host_service_manager, "is_user_host_eligible", return_value=False),
            patch.object(m.model_advisor_service, "record_model_turn_health", return_value=None),
        ):
            await m._run_turn_task_inner(
                chat_id=chat_id,
                prompt="Тест done",
                attachments=[],
                override_model="gemini-3-pro",
                override_access_mode=None,
                trigger_user=self.user,
            )

        self.assertTrue(mid_stream_checks["in_map"])
        self.assertIsInstance(mid_stream_checks["map_val"], int)
        self.assertEqual(mid_stream_checks["api_gen_started"], mid_stream_checks["map_val"])
        self.assertNotIn(chat_id, m._CHAT_GEN_STARTED_TS_MS)

        start_evts = [e for e in broadcasts if e.get("type") == "assistant_start"]
        done_evts = [e for e in broadcasts if e.get("type") == "turn_done"]
        self.assertEqual(len(start_evts), 1)
        self.assertEqual(start_evts[0].get("gen_started_at_ms"), mid_stream_checks["map_val"])
        self.assertEqual(len(done_evts), 1)
        done_usage = done_evts[0]["message"]["usage"]
        self.assertGreaterEqual(done_usage["duration_ms"], 15)
        self.assertEqual(done_usage["last_run_duration_ms"], done_usage["duration_ms"])

        # 2) Cancelled turn -> then continue_turn summing duration_ms
        broadcasts.clear()

        async def _stream_cancelled(**_kw: Any):
            await asyncio.sleep(0.025)
            yield {
                "type": "cancelled",
                "content": "Часть 1",
                "thinking": "",
                "tool_calls": [],
                "usage": {"input_tokens": 500, "output_tokens": 100, "total_tokens": 600},
            }

        with (
            patch.object(m.ws_manager, "broadcast_for_chat", side_effect=_fake_broadcast_for_chat),
            patch.object(m, "stream_agy_turn", side_effect=_stream_cancelled),
            patch.object(m, "_spawn_telemetry_task", side_effect=lambda coro: coro.close()),
            patch.object(m, "_maybe_generate_live_outline_title_task", new=AsyncMock(return_value=None)),
            patch.object(m, "_notify_gdrive_change_for_user", return_value=None),
            patch.object(m.openrouter_service, "is_user_openrouter_bridge_active", new=AsyncMock(return_value=False)),
            patch.object(m.sandbox_service, "is_user_sandbox_eligible", return_value=False),
            patch.object(m.host_service_manager, "is_user_host_eligible", return_value=False),
            patch.object(m.model_advisor_service, "record_model_turn_health", return_value=None),
        ):
            await m._run_turn_task_inner(
                chat_id=chat_id,
                prompt="Тест cancel",
                attachments=[],
                override_model="gemini-3-pro",
                override_access_mode=None,
                trigger_user=self.user,
            )

        self.assertNotIn(chat_id, m._CHAT_GEN_STARTED_TS_MS)
        cancel_evts = [e for e in broadcasts if e.get("type") == "turn_cancelled"]
        self.assertEqual(len(cancel_evts), 1)
        cancelled_msg = cancel_evts[0]["message"]
        first_dur = cancelled_msg["usage"]["duration_ms"]
        self.assertGreaterEqual(first_dur, 15)
        self.assertEqual(cancelled_msg["usage"]["last_run_duration_ms"], first_dur)

        # Resume the cancelled message via continue_turn
        broadcasts.clear()
        resume_api_start: dict[str, Any] = {}

        async def _stream_resume_done(**_kw: Any):
            resume_api_start["map_val"] = m._CHAT_GEN_STARTED_TS_MS.get(chat_id)
            chat_resp = await m.api_get_chat(chat_id, include_thinking=True, current_user=self.user)
            resumed_m = next(x for x in chat_resp["messages"] if int(x["id"]) == int(cancelled_msg["id"]))
            resume_api_start["api_val"] = resumed_m.get("gen_started_at_ms")
            await asyncio.sleep(0.025)
            yield {
                "type": "done",
                "content": "Часть 1 + Часть 2",
                "thinking": "",
                "tool_calls": [],
                "usage": {"input_tokens": 700, "output_tokens": 200, "total_tokens": 900},
            }

        with (
            patch.object(m.ws_manager, "broadcast_for_chat", side_effect=_fake_broadcast_for_chat),
            patch.object(m, "stream_agy_turn", side_effect=_stream_resume_done),
            patch.object(m, "_spawn_telemetry_task", side_effect=lambda coro: coro.close()),
            patch.object(m, "_maybe_generate_live_outline_title_task", new=AsyncMock(return_value=None)),
            patch.object(m, "_notify_gdrive_change_for_user", return_value=None),
            patch.object(m.openrouter_service, "is_user_openrouter_bridge_active", new=AsyncMock(return_value=False)),
            patch.object(m.sandbox_service, "is_user_sandbox_eligible", return_value=False),
            patch.object(m.host_service_manager, "is_user_host_eligible", return_value=False),
            patch.object(m.model_advisor_service, "record_model_turn_health", return_value=None),
        ):
            await m._run_turn_task_inner(
                chat_id=chat_id,
                prompt="",
                attachments=[],
                override_model="gemini-3-pro",
                override_access_mode=None,
                trigger_user=self.user,
                resume_message_id=int(cancelled_msg["id"]),
            )

        self.assertNotIn(chat_id, m._CHAT_GEN_STARTED_TS_MS)
        self.assertIsInstance(resume_api_start["map_val"], int)
        self.assertEqual(resume_api_start["api_val"], resume_api_start["map_val"])
        resume_start_evts = [e for e in broadcasts if e.get("type") == "assistant_start"]
        self.assertTrue(resume_start_evts and resume_start_evts[0].get("resumed"))
        self.assertEqual(resume_start_evts[0].get("gen_started_at_ms"), resume_api_start["map_val"])

        resumed_db = await db.get_message_by_id(chat_id, int(cancelled_msg["id"]), include_thinking=False)
        self.assertIsNotNone(resumed_db)
        assert resumed_db is not None
        self.assertEqual(resumed_db["status"], "done")
        second_last_dur = resumed_db["usage"]["last_run_duration_ms"]
        self.assertGreaterEqual(second_last_dur, 15)
        self.assertEqual(resumed_db["usage"]["duration_ms"], first_dur + second_last_dur)
        self.assertEqual(resumed_db["usage"]["proc_runs"], 2)

        # 3) Explicit error event branch
        broadcasts.clear()

        async def _stream_error(**_kw: Any):
            await asyncio.sleep(0.02)
            yield {"type": "error", "error": "Synthetic backend error", "usage": {}}

        with (
            patch.object(m.ws_manager, "broadcast_for_chat", side_effect=_fake_broadcast_for_chat),
            patch.object(m, "stream_agy_turn", side_effect=_stream_error),
            patch.object(m, "_spawn_telemetry_task", side_effect=lambda coro: coro.close()),
            patch.object(m, "_maybe_generate_live_outline_title_task", new=AsyncMock(return_value=None)),
            patch.object(m, "_notify_gdrive_change_for_user", return_value=None),
            patch.object(m.openrouter_service, "is_user_openrouter_bridge_active", new=AsyncMock(return_value=False)),
            patch.object(m.sandbox_service, "is_user_sandbox_eligible", return_value=False),
            patch.object(m.host_service_manager, "is_user_host_eligible", return_value=False),
            patch.object(m.model_advisor_service, "record_model_turn_health", return_value=None),
        ):
            await m._run_turn_task_inner(
                chat_id=chat_id,
                prompt="Тест error",
                attachments=[],
                override_model="gemini-3-pro",
                override_access_mode=None,
                trigger_user=self.user,
            )

        self.assertNotIn(chat_id, m._CHAT_GEN_STARTED_TS_MS)
        err_evts = [e for e in broadcasts if e.get("type") == "turn_error"]
        self.assertEqual(len(err_evts), 1)
        self.assertGreater(err_evts[0]["message"]["usage"]["duration_ms"], 0)
        self.assertEqual(
            err_evts[0]["message"]["usage"]["last_run_duration_ms"],
            err_evts[0]["message"]["usage"]["duration_ms"],
        )

        # 4) Unexpected exception branch
        broadcasts.clear()

        async def _stream_raises(**_kw: Any):
            await asyncio.sleep(0.02)
            if True:
                raise RuntimeError("unexpected stream crash")
            yield {}

        with (
            patch.object(m.ws_manager, "broadcast_for_chat", side_effect=_fake_broadcast_for_chat),
            patch.object(m, "stream_agy_turn", side_effect=_stream_raises),
            patch.object(m, "_spawn_telemetry_task", side_effect=lambda coro: coro.close()),
            patch.object(m, "_maybe_generate_live_outline_title_task", new=AsyncMock(return_value=None)),
            patch.object(m, "_notify_gdrive_change_for_user", return_value=None),
            patch.object(m.openrouter_service, "is_user_openrouter_bridge_active", new=AsyncMock(return_value=False)),
            patch.object(m.sandbox_service, "is_user_sandbox_eligible", return_value=False),
            patch.object(m.host_service_manager, "is_user_host_eligible", return_value=False),
            patch.object(m.model_advisor_service, "record_model_turn_health", return_value=None),
        ):
            await m._run_turn_task_inner(
                chat_id=chat_id,
                prompt="Тест exception",
                attachments=[],
                override_model="gemini-3-pro",
                override_access_mode=None,
                trigger_user=self.user,
            )

        self.assertNotIn(chat_id, m._CHAT_GEN_STARTED_TS_MS)
        exc_evts = [e for e in broadcasts if e.get("type") == "turn_error"]
        self.assertEqual(len(exc_evts), 1)
        self.assertGreater(exc_evts[0]["message"]["usage"]["duration_ms"], 0)
        self.assertEqual(
            exc_evts[0]["message"]["usage"]["last_run_duration_ms"],
            exc_evts[0]["message"]["usage"]["duration_ms"],
        )


def test_turn_duration_telemetry_in_chromium(tmp_path: Path) -> None:
    assert run_workspace_chromium(tmp_path, BROWSER_CHECKS) == [
        [1440, 900],
        [390, 844],
        [320, 568],
    ]
