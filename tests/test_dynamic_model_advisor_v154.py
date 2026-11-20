"""Regression suite v154: Dynamic Model Advisor 2.0 & background auto-passports.

Covers:
1. `Gemini 4 Neon (Low / Medium / High / Extra High)` parsing into `gemini-4-neon` and Top-1/Top-3 ranking
   on `backend`, `fullstack`, and `debug` in `balanced` mode.
2. Layer 1 (0ms cohort scoring) for brand-new fictional series (`gemini-4.5-xenon-high` / `(Extra High)`,
   `claude-6-titan-high`, `codex:gpt-6.5-nova@high`) without code changes + trait-based `domainBonus`.
3. Layer 2 background auto-calibration on isolated temp SQLite DB with mocked probe:
   `source="auto_ai"` persistence, range clamping, skip when `ACTIVE_RUNS` active, `TelemetryDeferred` deferral,
   2-hour backoff on errors, `/api/models`, `/api/advisor/catalog`, admin-only `/api/advisor/recalibrate`,
   and WS `models_updated` broadcast.
4. Layer 3 live turn health (`record_model_turn_health`): temporary penalty after 2+ transient failures (`-12`, `-25`)
   and instant reset to `0` on success, plus `⚠️ перебои ответа` in JS advisor reasons.
5. DOM render of `#advisorCatalogDetails` in Node VM and Chromium geometry gate (`1440x900`, `390x844`, `320x568`).

Strictly isolated in temporary directories/SQLite databases: 0 real network calls, 0 touches to live `data/hub.db`.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

_TESTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = _TESTS_DIR.parent
for _p in (str(ROOT_DIR), str(_TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_frontend_contracts import STATIC, ShellParser, run_node
from test_gdrive_autosync_and_restore_v152 import (
    _apply_env_patch,
    _make_isolated_env,
    _restore_env_patch,
)
from test_gdrive_sync_core_v151 import _asgi_request
from test_ui_workspace_minimal_v142 import run_workspace_chromium


# ---------------------------------------------------------------------------
# 1. Node VM Scoring & DOM Tests (Layers 1, 3, Neon, Fictional Series, Settings Catalog)
# ---------------------------------------------------------------------------

NODE_V154_ADVISOR_CHECKS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const ids = JSON.parse(fs.readFileSync(0, "utf8"));
const noop = () => {};

function makeNode(id) {
  const classes = new Set();
  const listeners = new Map();
  const attrs = new Map();
  return {
    id,
    value: "",
    checked: false,
    disabled: false,
    open: false,
    textContent: "",
    innerHTML: "",
    title: "",
    options: [],
    dataset: {},
    style: { width: "", setProperty: noop, removeProperty: noop, getPropertyValue: () => "" },
    classList: {
      add: (...c) => c.forEach(x => classes.add(x)),
      remove: (...c) => c.forEach(x => classes.delete(x)),
      toggle: (c, force) => {
        if (force === undefined) {
          if (classes.has(c)) { classes.delete(c); return false; }
          classes.add(c); return true;
        }
        if (force) classes.add(c); else classes.delete(c);
        return Boolean(force);
      },
      contains: c => classes.has(c),
    },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    removeEventListener(type, fn) {
      const list = listeners.get(type);
      if (!list) return;
      const idx = list.indexOf(fn);
      if (idx >= 0) list.splice(idx, 1);
    },
    dispatchEvent(evt) {
      for (const fn of (listeners.get(evt.type) || []).slice()) fn(evt);
    },
    click() { this.dispatchEvent({ type: "click", target: this, preventDefault: noop, stopPropagation: noop }); },
    focus: noop,
    blur: noop,
    setAttribute(k, v) { attrs.set(k, String(v)); },
    getAttribute(k) { return attrs.has(k) ? attrs.get(k) : null; },
    removeAttribute(k) { attrs.delete(k); },
    appendChild: noop,
    replaceChildren: noop,
    querySelector: () => null,
    querySelectorAll: () => [],
    closest: () => null,
  };
}

const nodes = new Map(ids.map(id => [id, makeNode(id)]));
const document = {
  title: "ZeroHub",
  documentElement: makeNode("html"),
  body: makeNode("body"),
  getElementById: id => nodes.get(id) ?? null,
  querySelector: s => s.startsWith("#") ? nodes.get(s.slice(1)) ?? null : null,
  querySelectorAll: () => [],
  createElement: () => makeNode("el"),
  addEventListener: noop,
  removeEventListener: noop,
};

const fetchCalls = [];
async function mockFetch(url, opts = {}) {
  const method = String(opts.method || "GET").toUpperCase();
  fetchCalls.push({ method, url: String(url) });
  const reply = (payload, status = 200) => ({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    text: async () => JSON.stringify(payload),
    json: async () => payload,
  });
  if (url === "/api/advisor/catalog") {
    return reply({
      ok: true,
      is_calibrating: false,
      calibrated_count: 1,
      items: [
        {
          series_key: "gemini-4-neon",
          display_name: "Gemini 4 Neon",
          vendor: "gemini",
          source: "seed",
          capability: 93,
          burn_class: 2,
          burn_score: 1.65,
          coding: 1.0,
          debug: 0.98,
          ui: 0.86,
          docs: 0.85,
          role_ru: "Кодинг и агентные задачи (−32% расхода)",
          health_penalty: 0,
        },
        {
          series_key: "gemini-4.5-xenon",
          display_name: "Gemini 4.5 Xenon",
          vendor: "gemini",
          source: "auto_ai",
          capability: 94,
          burn_class: 2,
          burn_score: 1.9,
          coding: 0.96,
          debug: 0.95,
          ui: 0.91,
          docs: 0.84,
          role_ru: "Флагманская серия нового поколения",
          health_penalty: -12,
        },
      ],
    });
  }
  if (url === "/api/advisor/recalibrate" && method === "POST") {
    return reply({ ok: true, triggered: true, is_calibrating: true, calibrated_count: 1, items: [] });
  }
  return reply({ ok: true });
}

const storage = () => {
  const m = new Map();
  return { getItem: k => m.get(k) ?? null, setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k) };
};

const context = {
  __AGY_EXPORT_ADVISOR_FOR_TESTS__: true,
  __AGY_SKIP_AUTO_INIT__: true,
  document,
  fetch: mockFetch,
  localStorage: storage(),
  sessionStorage: storage(),
  location: new URL("https://hub.invalid/"),
  URL,
  URLSearchParams,
  CSS: { supports: () => true, escape: String },
  innerWidth: 1440,
  innerHeight: 900,
  navigator: { userAgent: "Desktop" },
  matchMedia: () => ({ matches: false, addEventListener: noop, removeEventListener: noop }),
  requestAnimationFrame: noop,
  cancelAnimationFrame: noop,
  setTimeout: noop,
  clearTimeout: noop,
  setInterval: noop,
  clearInterval: noop,
  addEventListener: noop,
  removeEventListener: noop,
};
context.window = context;
const sandbox = vm.createContext(context);
for (const name of ["ui-shell.js", "app.js"]) {
  vm.runInContext(fs.readFileSync("app/static/" + name, "utf8"), sandbox, { filename: name, timeout: 6000 });
}

(async () => {
  const api = context.__AGY_ADVISOR_API__;

  // 1. Parse Gemini 4 Neon (Low / Medium / High / Extra High) into single series `gemini-4-neon`
  const neonEntries = [
    { id: "gemini-4-neon-low", label: "Gemini 4 Neon (Low)" },
    { id: "gemini-4-neon-medium", label: "Gemini 4 Neon (Medium)" },
    { id: "gemini-4-neon-high", label: "Gemini 4 Neon (High)" },
    { id: "gemini-4-neon-xhigh", label: "Gemini 4 Neon (Extra High)" },
    { id: "gemini-4-neon-extra-high", label: "Gemini 4 Neon (Extra High)" },
  ];
  const neonParsed = neonEntries.map((m, i) => api.parseModelEntry(m, i));
  assert.equal(neonParsed[3].tier, "XHigh");
  assert.equal(neonParsed[3].tierKey, "xhigh");
  assert.equal(neonParsed[3].tierRank, 0);
  assert.equal(neonParsed[3].vendor, "gemini");
  assert.equal(neonParsed[4].tierKey, "xhigh");

  const neonProfiles = neonEntries.map((m, i) => api.buildModelAdvisorProfile(m, i));
  assert.ok(neonProfiles.every(p => p.baseSeries === "gemini-4-neon"));
  assert.equal(neonProfiles[0].capability, 86);
  assert.equal(neonProfiles[1].capability, 90);
  assert.equal(neonProfiles[2].capability, 93);
  assert.equal(neonProfiles[2].burnClass, 2);
  assert.equal(neonProfiles[2].burnScore, 1.65);
  assert.equal(neonProfiles[2].codingAffinity, 1.0);
  assert.equal(neonProfiles[3].effort, "xhigh");
  assert.equal(neonProfiles[3].capability, 95);
  assert.equal(neonProfiles[3].burnClass, 3);

  // 2. Neon (High) wins #1 (and enters Top-3) on backend, fullstack, debug in balanced mode
  const fullPool = [
    { id: "gemini-4-neon-xhigh", label: "Gemini 4 Neon (Extra High)" },
    { id: "gemini-4-neon-high", label: "Gemini 4 Neon (High)" },
    { id: "gemini-4-neon-medium", label: "Gemini 4 Neon (Medium)" },
    { id: "gemini-4-neon-low", label: "Gemini 4 Neon (Low)" },
    { id: "gemini-4-carbon-high", label: "Gemini 4 Carbon (High)" },
    { id: "gemini-4-carbon-medium", label: "Gemini 4 Carbon (Medium)" },
    { id: "gemini-4-argon-high", label: "Gemini 4 Argon (High)" },
    { id: "claude-opus-5-5-high", label: "Claude Opus 5.5 (High)" },
    { id: "claude-sonnet-5-5-high", label: "Claude Sonnet 5.5 (High)" },
    { id: "codex:gpt-6-astra@xhigh", label: "GPT-6 Astra (XHigh)" },
    { id: "codex:gpt-6.1-sol@high", label: "GPT-6.1 Sol (High)" },
    { id: "codex:gpt-6.1-terra@high", label: "GPT-6.1 Terra (High)" },
  ];
  const eqQuotas = { gemini_5h_pct: 90, claude_5h_pct: 90 };
  for (const taskType of ["backend", "fullstack", "debug"]) {
    const rec = api.recommendModelsForTask(
      { complexity: "heavy", type: taskType, files: 4, risk: "medium", needs_terminal: true, needsTerminal: true },
      { models: fullPool, strategy: "balanced", quotas: eqQuotas, chatgpt_5h_pct: 90 }
    );
    assert.equal(rec.top3.length, 3);
    assert.ok(rec.top3.some(m => m.id === "gemini-4-neon-high"), `Neon High must enter Top-3 for ${taskType}`);
    assert.equal(rec.top3[0].id, "gemini-4-neon-high", `Neon High must lead balanced ${taskType}, got ${rec.top3[0].id}`);
  }

  // 3. Brand-new fictional series WITHOUT code changes (Layer 1 cohort + trait-based domainBonus)
  const fictionalPool = [
    { id: "gemini-4.5-xenon-high", label: "Gemini 4.5 Xenon (High)" },
    { id: "gemini-4.5-xenon-extra-high", label: "Gemini 4.5 Xenon (Extra High)" },
    { id: "claude-6-titan-high", label: "Claude 6 Titan (High)" },
    { id: "codex:gpt-6.5-nova@high", label: "GPT-6.5 Nova (High)" },
  ];
  const pXenonH = api.buildModelAdvisorProfile(fictionalPool[0]);
  const pXenonX = api.buildModelAdvisorProfile(fictionalPool[1]);
  const pTitanH = api.buildModelAdvisorProfile(fictionalPool[2]);
  const pNovaH = api.buildModelAdvisorProfile(fictionalPool[3]);
  assert.equal(pXenonH.baseSeries, "gemini-4.5-xenon");
  assert.equal(pXenonX.baseSeries, "gemini-4.5-xenon");
  assert.equal(pXenonX.effort, "xhigh");
  assert.equal(pTitanH.baseSeries, "claude-6-titan");
  assert.equal(pNovaH.baseSeries, "gpt-6.5-nova");
  assert.ok(pXenonH.capability >= 90 && pXenonX.capability >= 93);
  assert.ok(pTitanH.capability >= 90 && pNovaH.capability >= 90);
  assert.equal(pXenonH.advisorSource, "cohort");
  assert.equal(pNovaH.codingAffinity, 0.85);

  // Server passport overrides affinities and drives domainBonus dynamically
  const withServerTraits = [
    {
      id: "gemini-4.5-xenon-high",
      label: "Gemini 4.5 Xenon (High)",
      advisor: {
        series_key: "gemini-4.5-xenon",
        base_title: "Gemini 4.5 Xenon",
        effort: "high",
        capability: 92,
        burn_class: 2,
        burn_score: 2.0,
        coding: 0.70,
        debug: 0.70,
        ui: 1.0,
        docs: 0.80,
        role_ru: "UI-специалист",
        source: "auto_ai",
        health_penalty: 0,
      },
    },
    {
      id: "codex:gpt-6.5-nova@high",
      label: "GPT-6.5 Nova (High)",
      advisor: {
        series_key: "gpt-6.5-nova",
        base_title: "GPT-6.5 Nova",
        effort: "high",
        capability: 92,
        burn_class: 2,
        burn_score: 2.0,
        coding: 1.0,
        debug: 1.0,
        ui: 0.65,
        docs: 0.75,
        role_ru: "Backend-инженер",
        source: "auto_ai",
        health_penalty: 0,
      },
    },
  ];
  const uiScores = api.scoreModelsForTask(
    { complexity: "heavy", type: "ui", files: 2, risk: "low" },
    { models: withServerTraits, strategy: "balanced", quotas: eqQuotas, chatgpt_5h_pct: 90 }
  );
  assert.equal(uiScores[0].id, "gemini-4.5-xenon-high", "Higher uiAffinity must win UI task");
  const backendScores = api.scoreModelsForTask(
    { complexity: "heavy", type: "backend", files: 2, risk: "low" },
    { models: withServerTraits, strategy: "balanced", quotas: eqQuotas, chatgpt_5h_pct: 90 }
  );
  assert.equal(backendScores[0].id, "codex:gpt-6.5-nova@high", "Higher codingAffinity must win backend task");

  // 4. Layer 3 health penalty reduces score & adds `⚠️ перебои ответа`
  const degradedPool = [
    {
      id: "gemini-4-neon-high",
      label: "Gemini 4 Neon (High)",
      advisor: {
        series_key: "gemini-4-neon",
        effort: "high",
        capability: 93,
        burn_class: 2,
        burn_score: 1.65,
        coding: 1.0,
        debug: 0.98,
        ui: 0.86,
        docs: 0.85,
        role_ru: "Кодинг",
        source: "seed",
        health_penalty: -25,
      },
    },
    { id: "codex:gpt-6.1-sol@high", label: "GPT-6.1 Sol (High)" },
  ];
  const scoredDeg = api.scoreModelsForTask(
    { complexity: "heavy", type: "backend", files: 3, risk: "medium" },
    { models: degradedPool, strategy: "balanced", quotas: eqQuotas, chatgpt_5h_pct: 90 }
  );
  assert.equal(scoredDeg[0].id, "codex:gpt-6.1-sol@high", "Degraded Neon (-25) must drop below healthy Sol");
  const degNeon = scoredDeg.find(x => x.id === "gemini-4-neon-high");
  assert.match(degNeon.reason, /⚠️ перебои ответа/);

  // 5. DOM render of #advisorCatalogDetails (admin vs guest, WS live update, recalibrate)
  api.state.currentUser = { id: "u_admin", username: "admin", role: "admin" };
  api.state.models = withServerTraits;
  api.renderAdvisorCatalogUI();
  assert.equal(nodes.get("advisorCatalogCountBadge").textContent, "2");
  assert.match(nodes.get("advisorCatalogList").innerHTML, /Gemini 4\.5 Xenon/);
  assert.match(nodes.get("advisorCatalogList").innerHTML, /Авто ИИ/);
  assert.match(nodes.get("advisorCatalogList").innerHTML, /Код ★/);
  assert.match(nodes.get("advisorCatalogList").innerHTML, /UI ★/);
  assert.ok(!nodes.get("advisorRecalibrateBtn").classList.contains("hidden"));

  await api.loadAdvisorCatalog({ silent: true });
  assert.match(nodes.get("advisorCatalogList").innerHTML, /Gemini 4 Neon/);
  assert.match(nodes.get("advisorCatalogList").innerHTML, /Эталон/);
  assert.match(nodes.get("advisorCatalogList").innerHTML, /⚠️ перебои ответа/);

  await api.triggerAdvisorRecalibrate();
  assert.ok(fetchCalls.some(c => c.url === "/api/advisor/recalibrate" && c.method === "POST"));

  // Guest hides admin recalibrate button
  api.state.currentUser = { id: "u_guest", username: "guest", role: "user" };
  api.applyRoleUI();
  assert.ok(nodes.get("advisorRecalibrateBtn").classList.contains("hidden"));

  // WS `models_updated` refreshes catalog automatically
  api.handleWsEvent({
    type: "models_updated",
    models: [
      { id: "gemini-4-neon-high", label: "Gemini 4 Neon (High)" },
      { id: "claude-6-titan-high", label: "Claude 6 Titan (High)" },
    ],
  });
  assert.equal(nodes.get("advisorCatalogCountBadge").textContent, "2");
  assert.match(nodes.get("advisorCatalogList").innerHTML, /Claude 6 Titan/);
  assert.match(nodes.get("advisorCatalogList").innerHTML, /Авто-пул/);
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""


def test_neon_fictional_series_and_catalog_dom_in_node_vm() -> None:
    parser = ShellParser()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    parser.close()
    for req_id in (
        "advisorCatalogDetails",
        "advisorCatalogCountBadge",
        "advisorCatalogSyncMeta",
        "advisorRecalibrateBtn",
        "advisorCatalogList",
    ):
        assert req_id in parser.ids, f"Missing #{req_id} in index.html"

    # Zero new !important in ui-system.css (strictly 1 in @layer utilities)
    css = (STATIC / "ui-system.css").read_text(encoding="utf-8")
    stripped = re.sub(r"/\*[\s\S]*?\*/", "", css)
    important_hits = re.findall(r"!\s*important\b", stripped, flags=re.IGNORECASE)
    assert len(important_hits) == 1, f"Expected strictly 1 !important in ui-system.css, got {len(important_hits)}"
    assert ".advisor-catalog-details" in stripped

    run_node("dynamic-advisor-v154-vm", parser.ids, script=NODE_V154_ADVISOR_CHECKS)


# ---------------------------------------------------------------------------
# 2. Backend Unit & Integration Tests (DB, Cohort, Auto-Calibration, Health, API)
# ---------------------------------------------------------------------------


class DynamicModelAdvisorV154BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app import (
            agy_engine,
            auth,
            codex_engine,
            database,
            gdrive_service,
            main as main_mod,
            model_advisor_service,
        )

        self.agy_engine = agy_engine
        self.auth = auth
        self.codex_engine = codex_engine
        self.database = database
        self.gdrive_service = gdrive_service
        self.main_mod = main_mod
        self.advisor = model_advisor_service

        self._tmp = tempfile.TemporaryDirectory(prefix="hub_v154_advisor_", ignore_cleanup_errors=True)
        self.tmp_root = Path(self._tmp.name).resolve()
        self.env_map = _make_isolated_env(self.tmp_root / "hub_pc")

        await database.close_db_pool()
        self._saved_attrs = _apply_env_patch(self.env_map)
        self._orig_cached_models = agy_engine._cached_models
        self._orig_cached_models_ts = agy_engine._cached_models_ts
        self._orig_ws_notifier = model_advisor_service._MODELS_UPDATED_NOTIFIER
        model_advisor_service.clear_discovered_models()
        model_advisor_service.reset_health_state()
        gdrive_service.clear_gdrive_dirty_state(None)
        await database.init_db()

    async def asyncTearDown(self) -> None:
        self.agy_engine._cached_models = self._orig_cached_models
        self.agy_engine._cached_models_ts = self._orig_cached_models_ts
        self.agy_engine.ACTIVE_RUNS.clear()
        self.advisor.set_advisor_ws_notifier(self._orig_ws_notifier)
        self.advisor.clear_discovered_models()
        self.advisor.reset_health_state()
        self.gdrive_service.clear_gdrive_dirty_state(None)
        await self.database.close_db_pool()
        _restore_env_patch(self._saved_attrs)
        self._tmp.cleanup()

    async def test_engines_normalization_and_cohort_layer1(self) -> None:
        # 1. agy_engine DEFAULT_MODELS, family, tier, default/resolve
        default_ids = {m["id"] for m in self.agy_engine.DEFAULT_MODELS}
        assert {
            "gemini-4-neon-xhigh",
            "gemini-4-neon-high",
            "gemini-4-neon-medium",
            "gemini-4-neon-low",
        } <= default_ids
        assert self.agy_engine.classify_model_family("gemini-4-neon-high") == "gemini"
        assert self.agy_engine.classify_model_family("neon-preview-high") == "gemini"
        assert self.agy_engine.classify_model_family("gemini-4-hydrogen-low") == "gemini"
        assert self.agy_engine._extract_model_tier("gemini-4-neon-extra-high") == "xhigh"
        assert self.agy_engine._extract_model_tier("codex:gpt-6.5-nova@xhigh") == "xhigh"
        assert self.agy_engine.pick_default_model_id(
            [{"id": "gemini-4-neon-high", "label": "Gemini 4 Neon (High)"}]
        ) == "gemini-4-neon-high"
        assert self.agy_engine.resolve_active_model_id(
            "gemini-4-neon-high",
            [
                {"id": "gemini-4-carbon-high", "label": "Gemini 4 Carbon (High)"},
                {"id": "gemini-4-argon-high", "label": "Gemini 4 Argon (High)"},
            ],
        ) == "gemini-4-carbon-high"

        # 2. codex_engine build_codex_models_from_rpc preserves description (<=240 chars)
        long_desc = "Flagship agentic coding model with extended reasoning " * 10
        codex_built = self.codex_engine.build_codex_models_from_rpc(
            [
                {
                    "model": "gpt-6.5-nova",
                    "displayName": "GPT-6.5 Nova",
                    "description": long_desc,
                    "contextWindow": 400000,
                    "supportedReasoningEfforts": ["high", "xhigh"],
                }
            ]
        )
        assert len(codex_built) == 2
        assert codex_built[0]["id"] == "codex:gpt-6.5-nova@xhigh"
        assert "description" in codex_built[0]
        assert len(codex_built[0]["description"]) == 240

        # 3. Identity normalization & Layer 1 cohort inference
        neon_x = self.advisor.normalize_model_identity(
            {"id": "gemini-4-neon-extra-high", "label": "Gemini 4 Neon (Extra High)"}
        )
        neon_h = self.advisor.normalize_model_identity(
            {"id": "gemini-4-neon-high", "label": "Gemini 4 Neon (High)"}
        )
        assert neon_x["series_key"] == "gemini-4-neon"
        assert neon_x["base_title"] == "Gemini 4 Neon"
        assert neon_x["effort"] == "xhigh"
        assert neon_h["series_key"] == "gemini-4-neon"
        assert neon_h["effort"] == "high"

        pool = [
            {"id": "gemini-4-carbon-high", "label": "Gemini 4 Carbon (High)", "family": "gemini"},
            {"id": "gemini-4.5-xenon-high", "label": "Gemini 4.5 Xenon (High)", "family": "gemini"},
            {"id": "gemini-4.5-xenon-extra-high", "label": "Gemini 4.5 Xenon (Extra High)", "family": "gemini"},
            {"id": "claude-6-titan-high", "label": "Claude 6 Titan (High)", "family": "claude"},
            {
                "id": "codex:gpt-6.5-nova@high",
                "label": "GPT-6.5 Nova (High)",
                "family": "gpt",
                "description": "Agentic coding flagship",
                "context_window": 512000,
            },
        ]
        enriched = await self.advisor.enrich_models_with_advisor(pool)
        by_id = {m["id"]: m["advisor"] for m in enriched}
        assert by_id["gemini-4.5-xenon-high"]["series_key"] == "gemini-4.5-xenon"
        assert by_id["gemini-4.5-xenon-high"]["source"] == "cohort"
        assert by_id["gemini-4.5-xenon-high"]["capability"] >= 92
        assert by_id["gemini-4.5-xenon-extra-high"]["effort"] == "xhigh"
        assert by_id["gemini-4.5-xenon-extra-high"]["capability"] >= 94
        assert by_id["claude-6-titan-high"]["series_key"] == "claude-6-titan"
        assert by_id["claude-6-titan-high"]["capability"] >= 92
        assert by_id["codex:gpt-6.5-nova@high"]["series_key"] == "gpt-6.5-nova"
        assert by_id["codex:gpt-6.5-nova@high"]["coding"] >= 0.94

    async def test_layer2_auto_calibration_active_runs_backoff_and_api_ws(self) -> None:
        from app.agy_credentials import TelemetryDeferred

        fictional_models = [
            {"id": "gemini-4-neon-high", "label": "Gemini 4 Neon (High)", "family": "gemini"},
            {"id": "gemini-4.5-xenon-high", "label": "Gemini 4.5 Xenon (High)", "family": "gemini"},
            {"id": "gemini-4.5-xenon-extra-high", "label": "Gemini 4.5 Xenon (Extra High)", "family": "gemini"},
            {"id": "claude-6-titan-high", "label": "Claude 6 Titan (High)", "family": "claude"},
        ]
        self.agy_engine._cached_models = list(fictional_models)
        self.agy_engine._cached_models_ts = time.monotonic()
        self.advisor.notify_models_discovered(fictional_models)

        probe_calls: list[list[dict[str, Any]]] = []
        ws_broadcasts: list[str] = []

        async def _ws_spy() -> None:
            ws_broadcasts.append("models_updated")

        self.advisor.set_advisor_ws_notifier(_ws_spy)

        async def _empty_or_catalog() -> list[dict[str, Any]]:
            return []

        # 1. Skip when ACTIVE_RUNS is non-empty
        self.agy_engine.ACTIVE_RUNS["chat-busy"] = object()  # type: ignore[assignment]
        res_busy = await self.advisor.run_auto_calibration_once(
            probe_fn=lambda items: asyncio.sleep(0, result={}),  # type: ignore[arg-type]
            openrouter_catalog_fn=_empty_or_catalog,
        )
        assert res_busy["status"] == "skipped_active_runs"
        self.agy_engine.ACTIVE_RUNS.clear()

        # 2. TelemetryDeferred defers quietly without writing backoff error
        async def _deferred_probe(items: list[dict[str, Any]]) -> dict[str, Any]:
            probe_calls.append(items)
            raise TelemetryDeferred()

        res_def = await self.advisor.run_auto_calibration_once(
            probe_fn=_deferred_probe,
            openrouter_catalog_fn=_empty_or_catalog,
        )
        assert res_def["status"] == "deferred"
        assert await self.database.get_model_advisor_entry("gemini-4.5-xenon") is None

        # 3. Error triggers 2h retry_after backoff; immediate second call skips probe
        async def _failing_probe(items: list[dict[str, Any]]) -> dict[str, Any]:
            probe_calls.append(items)
            raise RuntimeError("Upstream 503 during probe")

        calls_before_err = len(probe_calls)
        res_err = await self.advisor.run_auto_calibration_once(
            probe_fn=_failing_probe,
            openrouter_catalog_fn=_empty_or_catalog,
        )
        assert res_err["status"] == "error"
        assert len(probe_calls) == calls_before_err + 1
        err_row = await self.database.get_model_advisor_entry("gemini-4.5-xenon")
        assert err_row is not None
        assert err_row["retry_after"] != ""
        assert "503" in err_row["last_error"]

        res_backoff = await self.advisor.run_auto_calibration_once(
            probe_fn=_failing_probe,
            openrouter_catalog_fn=_empty_or_catalog,
        )
        assert res_backoff["status"] == "noop"
        assert len(probe_calls) == calls_before_err + 1

        # 4. Successful probe with force=True clamps extreme values, persists auto_ai, notifies GDrive & WS
        async def _ok_probe(items: list[dict[str, Any]]) -> dict[str, Any]:
            probe_calls.append(items)
            return {
                "series": {
                    "gemini-4.5-xenon": {
                        "tiers": {
                            "low": {"capability": 88, "burn_class": 1, "burn_score": 0.4},
                            "medium": {"capability": 91, "burn_class": 2, "burn_score": 1.4},
                            "high": {"capability": 140, "burn_class": 9, "burn_score": 12.0},
                            "xhigh": {"capability": 99, "burn_class": 4, "burn_score": 3.5},
                        },
                        "coding": 1.8,
                        "debug": 0.96,
                        "ui": 0.91,
                        "docs": 0.01,
                        "role_ru": "Очень длинная строка описания роли новой модели " * 3,
                    },
                    "claude-6-titan": {
                        "tiers": {
                            "low": {"capability": 87, "burn_class": 2, "burn_score": 1.5},
                            "medium": {"capability": 91, "burn_class": 2, "burn_score": 2.1},
                            "high": {"capability": 95, "burn_class": 3, "burn_score": 3.1},
                            "xhigh": {"capability": 97, "burn_class": 5, "burn_score": 4.9},
                        },
                        "coding": 0.97,
                        "debug": 0.99,
                        "ui": 0.92,
                        "docs": 0.80,
                        "role_ru": "Архитектурный флагман Anthropic 6",
                    },
                }
            }

        res_ok = await self.advisor.run_auto_calibration_once(
            force=True,
            probe_fn=_ok_probe,
            openrouter_catalog_fn=_empty_or_catalog,
        )
        assert res_ok["status"] == "ok"
        assert set(res_ok["calibrated"]) == {"gemini-4.5-xenon", "claude-6-titan"}
        assert ws_broadcasts == ["models_updated"]

        xenon_row = await self.database.get_model_advisor_entry("gemini-4.5-xenon")
        assert xenon_row is not None
        assert xenon_row["source"] == "auto_ai"
        assert xenon_row["retry_after"] == ""
        assert xenon_row["last_error"] == ""
        xp = xenon_row["passport"]
        assert xp["tiers"]["high"]["capability"] == 97
        assert xp["tiers"]["high"]["burn_class"] == 5
        assert xp["tiers"]["high"]["burn_score"] == 5.0
        assert xp["tiers"]["low"]["burn_score"] == 0.8
        assert xp["coding"] == 1.0
        assert xp["docs"] == 0.1
        assert len(xp["role_ru"]) <= 70

        # 5. REST API checks: /api/models, /api/advisor/catalog, /api/advisor/recalibrate RBAC
        admin = await self.database.get_user_by_id("admin")
        assert admin is not None
        admin_cookie = (
            f"{self.auth.COOKIE_NAME}="
            f"{self.auth.create_user_session_token('admin', int(admin.get('session_version') or 1))}"
        )
        guest = await self.database.create_user(
            username="dev_v154",
            password="dev_password_154",
            display_name="Dev",
            role="user",
            max_access_mode="write_files",
            allowed_models=["gemini-4-neon-high"],
        )
        guest_cookie = (
            f"{self.auth.COOKIE_NAME}="
            f"{self.auth.create_user_session_token(guest['id'], int(guest.get('session_version') or 1))}"
        )

        st_m, d_m, _ = await _asgi_request(self.main_mod.app, "GET", "/api/models", cookie_header=admin_cookie)
        assert st_m == 200
        models_by_id = {m["id"]: m for m in d_m["models"]}
        assert models_by_id["gemini-4.5-xenon-high"]["advisor"]["source"] == "auto_ai"
        assert models_by_id["gemini-4-neon-high"]["advisor"]["source"] == "seed"
        assert models_by_id["gemini-4-neon-high"]["advisor"]["capability"] == 93

        st_cat, d_cat, _ = await _asgi_request(
            self.main_mod.app, "GET", "/api/advisor/catalog", cookie_header=admin_cookie
        )
        assert st_cat == 200
        assert d_cat["ok"] is True
        assert d_cat["calibrated_count"] == 2
        caps = [it["capability"] for it in d_cat["items"]]
        assert caps == sorted(caps, reverse=True)

        # Guest catalog only exposes allowed models and cannot POST recalibrate
        st_gcat, d_gcat, _ = await _asgi_request(
            self.main_mod.app, "GET", "/api/advisor/catalog", cookie_header=guest_cookie
        )
        assert st_gcat == 200
        assert [it["series_key"] for it in d_gcat["items"]] == ["gemini-4-neon"]

        st_grec, _, _ = await _asgi_request(
            self.main_mod.app, "POST", "/api/advisor/recalibrate", cookie_header=guest_cookie
        )
        assert st_grec == 403

        st_arec, d_arec, _ = await _asgi_request(
            self.main_mod.app, "POST", "/api/advisor/recalibrate", cookie_header=admin_cookie
        )
        assert st_arec == 200 and d_arec["triggered"] is True
        await asyncio.sleep(0.05)

    async def test_layer3_turn_health_penalty_and_recovery(self) -> None:
        t0 = 1000.0
        # Non-transient error does not penalize
        assert self.advisor.record_model_turn_health(
            "gemini-4-neon-high", ok=False, error_text="Invalid JSON argument", now_mono=t0
        ) == 0
        # 1st transient 503 -> 0 (still tolerated)
        assert self.advisor.record_model_turn_health(
            "gemini-4-neon-high", ok=False, error_text="HTTP 503 Service Unavailable", now_mono=t0 + 1
        ) == 0
        # 2nd transient overload -> -12 across the whole series (including xhigh/medium)
        assert self.advisor.record_model_turn_health(
            "gemini-4-neon-medium", ok=False, error_text="Model overloaded / capacity", now_mono=t0 + 2
        ) == -12
        assert self.advisor.get_series_health_penalty("gemini-4-neon", now_mono=t0 + 3) == -12
        # 3rd timeout -> -25
        assert self.advisor.record_model_turn_health(
            "gemini-4-neon-xhigh", ok=False, error_text="Stream timeout без прогресса", now_mono=t0 + 4
        ) == -25

        # Reflected in live model enrichment
        enriched_bad = await self.advisor.enrich_models_with_advisor(
            [{"id": "gemini-4-neon-high", "label": "Gemini 4 Neon (High)", "family": "gemini"}]
        )
        # Note: enrich uses real time.monotonic(), so also test with default clock
        self.advisor.reset_health_state()
        self.advisor.record_model_turn_health("gemini-4-neon-high", ok=False, error_text="503 overloaded")
        self.advisor.record_model_turn_health("gemini-4-neon-high", ok=False, error_text="503 overloaded")
        enriched_live = await self.advisor.enrich_models_with_advisor(
            [{"id": "gemini-4-neon-high", "label": "Gemini 4 Neon (High)", "family": "gemini"}]
        )
        assert enriched_live[0]["advisor"]["health_penalty"] == -12
        assert enriched_bad[0]["advisor"]["series_key"] == "gemini-4-neon"

        # Single successful turn immediately clears the penalty back to 0
        assert self.advisor.record_model_turn_health("gemini-4-neon-high", ok=True) == 0
        assert self.advisor.get_series_health_penalty("gemini-4-neon") == 0


# ---------------------------------------------------------------------------
# 3. Headless Chromium Geometry Gate (`1440x900`, `390x844`, `320x568`)
# ---------------------------------------------------------------------------

CHROMIUM_V154_CHECKS = r"""
const viewports = [[1440, 900], [390, 844], [320, 568]];
function check(ok, message) {
  if (!ok) throw new Error(message);
}

function measureViewport(width, height) {
  return new Promise((resolve, reject) => {
    const frame = document.createElement("iframe");
    frame.style.cssText = `position:fixed;left:0;top:0;width:${width}px;height:${height}px;border:0`;
    frame.onload = async () => {
      try {
        const w = frame.contentWindow;
        const d = frame.contentDocument;
        const api = w.__AGY_ADVISOR_API__;
        const get = id => d.getElementById(id);
        const rect = el => (typeof el === "string" ? get(el) : el).getBoundingClientRect();
        const eps = 1.5;

        api.state.currentUser = { id: "u_admin", username: "admin", role: "admin", max_access_mode: "full_access" };
        api.state.models = [
          {
            id: "gemini-4-neon-high",
            label: "Gemini 4 Neon (High)",
            advisor: {
              series_key: "gemini-4-neon",
              base_title: "Gemini 4 Neon",
              effort: "high",
              capability: 93,
              burn_class: 2,
              burn_score: 1.65,
              coding: 1.0,
              debug: 0.98,
              ui: 0.86,
              docs: 0.85,
              role_ru: "Кодинг и агентные задачи (−32% расхода)",
              source: "seed",
              health_penalty: 0,
            },
          },
          {
            id: "gemini-4.5-xenon-extra-high",
            label: "Gemini 4.5 XenonUltraLongSeriesNameWithoutSpacesTestV154 (Extra High)",
            advisor: {
              series_key: "gemini-4.5-xenon",
              base_title: "Gemini 4.5 XenonUltraLongSeriesNameWithoutSpacesTestV154",
              effort: "xhigh",
              capability: 96,
              burn_class: 3,
              burn_score: 2.9,
              coding: 0.98,
              debug: 0.95,
              ui: 0.92,
              docs: 0.88,
              role_ru: "Флагман нового поколения с длинным описанием специализации",
              source: "auto_ai",
              health_penalty: -12,
            },
          },
          {
            id: "codex:gpt-6.5-nova@high",
            label: "GPT-6.5 Nova (High)",
          },
        ];

        get("settingsModal").classList.remove("hidden");
        const details = get("advisorCatalogDetails");
        details.open = true;
        api.renderAdvisorCatalogUI();

        const modal = get("settingsModal");
        const card = modal.querySelector(".modal-card");
        const list = get("advisorCatalogList");
        const items = list.querySelectorAll(".advisor-catalog-item");

        check(items.length === 3, "Expected 3 series cards in #advisorCatalogList, got " + items.length);
        check(get("advisorCatalogCountBadge").textContent.trim() === "3", "Count badge must be 3");
        const cardR = rect(card);
        const detR = rect(details);
        check(detR.right <= cardR.right + eps, "#advisorCatalogDetails overflows modal card right edge");
        check(d.documentElement.scrollWidth <= d.documentElement.clientWidth + 1, "Root horizontal overflow");
        check(modal.scrollWidth <= modal.clientWidth + 1, "#settingsModal horizontal overflow");
        check(card.scrollWidth <= card.clientWidth + 1, "Settings .modal-card horizontal overflow");
        check(details.scrollWidth <= details.clientWidth + 1, "#advisorCatalogDetails horizontal overflow");
        check(list.scrollWidth <= list.clientWidth + 1, "#advisorCatalogList horizontal overflow");

        resolve({
          viewport: width + "x" + height,
          cards: items.length,
          detailsWidth: Math.round(detR.width),
        });
      } catch (err) {
        reject(new Error(width + "x" + height + ": " + err.message));
      } finally {
        frame.remove();
      }
    };
    frame.srcdoc = workspacePage;
    document.body.append(frame);
  });
}

(async () => {
  const result = [];
  for (const [width, height] of viewports) {
    result.push(await measureViewport(width, height));
  }
  document.getElementById("report").textContent = JSON.stringify({ result });
})().catch(error => {
  document.getElementById("report").textContent = JSON.stringify({ error: error.message });
});
"""


def test_advisor_catalog_chromium_geometry(tmp_path: Path) -> None:
    measurements = run_workspace_chromium(tmp_path, CHROMIUM_V154_CHECKS)
    assert [row["viewport"] for row in measurements] == ["1440x900", "390x844", "320x568"]
    for row in measurements:
        assert row["cards"] == 3
        assert row["detailsWidth"] > 100
