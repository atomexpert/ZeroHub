"""Workspace geometry and menu behavior using the deployed CSS, HTML and JS."""
import html
import json
import re
import subprocess

import pytest

from app.config import subprocess_hidden_kwargs
from test_frontend_contracts import STATIC, ShellParser, run_node
from test_sidebar_badges_layout_v113 import _find_browser


BROWSER_CHECKS = r"""
const viewports = [[1440, 900], [390, 844], [320, 568]];
function check(ok, message) {
  if (!ok) throw new Error(message);
}
function measure(width, height) {
  return new Promise((resolve, reject) => {
    const frame = document.createElement("iframe");
    frame.style.cssText = "border:0;position:absolute;width:" + width + "px;height:" + height + "px";
    frame.onload = () => {
      try {
        const w = frame.contentWindow;
        const d = frame.contentDocument;
        const api = w.__AGY_ADVISOR_API__;
        const get = id => d.getElementById(id);
        const show = (...ids) => ids.forEach(id => get(id).classList.remove("hidden"));
        const mobile = width <= 768;
        const rect = id => get(id).getBoundingClientRect();
        const inside = (id, bounds = { left: 0, top: 0, right: width, bottom: height }) => {
          const r = rect(id);
          check(r.width > 0 && r.height > 0 && r.left >= bounds.left - .1 && r.right <= bounds.right + .1 &&
            r.top >= bounds.top - .1 && r.bottom <= bounds.bottom + .1, id + ": " + JSON.stringify(r));
          return r;
        };

        get("workspaceTabbar").classList.add("has-project-mode");
        get("activeChatTitle").textContent = "Long project conversation title ".repeat(7);
        api.syncMobileHeaderToolsSlot();
        get("modelPickerLabel").textContent = "Very long model designation ".repeat(4);
        get("modelPickerAccBadge").textContent = "Long account";
        get("chatMetaOutlineBtn").textContent = "999";
        show("modelPickerAccBadge", "chatMetaOutlineBtn", "chatHeaderActions", "contextMeterBar", "composerSection");
        if (mobile) {
          get("mobileSegFullAccessBtn").querySelector(".seg-full-label").textContent = "Терм.";
          show("mobileResetSandboxBtn", "mobileHostServicesBtn");
        } else {
          show("topAccessSeg", "resetSandboxBtn", "hostServicesBtn", "chatContextBadge", "chatMetaDot1",
            "openProjectMemoryBtn", "newProjectBranchTopBtn");
        }
        check(d.documentElement.scrollWidth <= w.innerWidth, "Horizontal document overflow");
        check(rect("appShell").height === height, "Shell must fill the viewport");
        const header = inside("workspaceTabbar");
        check(header.height === (mobile ? 46 : 48), "Header height: " + header.height);
        const toolbar = mobile ? inside("mobileAccessBar") : header;
        const model = inside("modelPickerBtn", toolbar);
        if (mobile) {
          const left = inside("mobileAccessSeg", toolbar);
          const host = inside("mobileHostServicesBtn", toolbar);
          const outline = inside("chatMetaOutlineBtn", toolbar);
          check(left.right <= host.left && host.right <= model.left && model.right <= outline.left,
            "Mobile toolbar items overlap");
          check(model.width >= 36, "Model picker collapsed: " + model.width);
        }
        const messages = inside("messagesContainer");
        const composer = inside("composerBox");
        check(header.bottom <= toolbar.bottom && toolbar.bottom <= messages.top + .1, "Header overlaps messages");
        check(messages.bottom <= composer.top + .1, "Messages overlap composer");
        inside("promptInput", composer);
        inside("sendBtn", composer);

        api.openModelPickerMenu();
        check(get("modelPickerBtn").getAttribute("aria-expanded") === "true", "Model menu failed to open");
        inside("modelPickerMenu");
        api.closeModelPickerMenu();
        check(get("modelPickerMenu").classList.contains("hidden"), "Model menu failed to close");

        get("sidebar").classList.add("mobile-open");
        get("sidebar").style.transition = "none";
        api.bindNewContextEvents();
        get("newActionMenuBtn").click();
        inside("newActionPopover");
        check(get("newActionPopover").querySelector("[data-nap-action=chat] svg"), "Missing menu icon");
        get("newActionMenuBtn").click();
        check(get("newActionPopover").classList.contains("hidden"), "New action menu failed to close");
        get("sidebar").classList.remove("mobile-open");
        api.state.activeTab = "project";
        api.state.selectedProject = "Workspace";
        api.state.projects = [{ name: "Workspace" }, { name: "Another project" }];
        api.renderWelcomeContextSwitch();
        const welcomePicker = get("welcomeContextSwitch").querySelector("[data-wcs-open-picker]");
        welcomePicker.click();
        inside("welcomeProjPopover");
        check(welcomePicker.getAttribute("aria-expanded") === "true", "Welcome picker not expanded");
        d.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        check(get("welcomeProjPopover").classList.contains("hidden"), "Escape did not close the welcome picker");
        check(welcomePicker.getAttribute("aria-expanded") === "false", "Welcome picker not collapsed");
        resolve(width + "x" + height);
      } catch (error) {
        reject(new Error(width + "x" + height + " " + error.message));
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
  for (const [width, height] of viewports) result.push(await measure(width, height));
  document.getElementById("report").textContent = JSON.stringify({ result });
})().catch(error => {
  document.getElementById("report").textContent = JSON.stringify({ error: error.message });
});
"""


def run_workspace_chromium(tmp_path, checks, *, vendor_scripts=()):
    browser = _find_browser()
    if not browser:
        pytest.skip("Chromium required (set AGY_TEST_BROWSER)")
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    # Keep the production markup and CSS while isolating auth and external assets.
    page = re.sub(r"<script\b[^>]*>.*?</script>|<link\b[^>]*>", "", page, flags=re.S)
    page = page.replace('data-auth="checking"', 'data-auth="authenticated"', 1)
    styles = "".join(
        f'<style data-asset="{name}">{(STATIC / name).read_text(encoding="utf-8")}</style>'
        for name in ("styles.css", "ui-system.css")
    )
    scripts = '<script>window.__AGY_EXPORT_ADVISOR_FOR_TESTS__=true;window.__AGY_SKIP_AUTO_INIT__=true;</script>'
    for name in (*vendor_scripts, "ui-shell.js", "app.js"):
        code = (STATIC / name).read_text(encoding="utf-8").replace("</script", r"<\/script")
        scripts += f"<script>{code}</script>"
    page = page.replace("</head>", styles + "</head>").replace("</body>", scripts + "</body>")
    payload = json.dumps(page).replace("<", r"\u003c")
    fixture = tmp_path / "workspace.html"
    fixture.write_text(
        '<!doctype html><meta charset="utf-8"><pre id="report">PENDING</pre>'
        f"<script>const workspacePage={payload};\n{checks}</script>",
        encoding="utf-8",
    )
    result = subprocess.run(
        [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--disable-extensions",
         "--disable-background-networking", "--no-proxy-server", "--virtual-time-budget=5000",
         f"--user-data-dir={tmp_path / 'profile'}", "--dump-dom", fixture.as_uri()],
        capture_output=True, encoding="utf-8", errors="replace", timeout=35,
        **subprocess_hidden_kwargs(),
    )
    assert result.returncode == 0, result.stderr
    match = re.search(r'<pre id="report">(.*?)</pre>', result.stdout)
    assert match and match[1] != "PENDING", result.stderr[-3000:]
    report = json.loads(html.unescape(match[1]))
    assert "error" not in report, report
    return report["result"]


def test_workspace_geometry_in_chromium(tmp_path):
    assert run_workspace_chromium(tmp_path, BROWSER_CHECKS) == ["1440x900", "390x844", "320x568"]


NODE_CHECKS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const noop = () => {};
const ids = JSON.parse(fs.readFileSync(0, "utf8"));
function element() {
  const classes = new Set(["hidden"]), attrs = new Map(), listeners = new Map();
  return {
    classList: {
      add: name => classes.add(name), remove: name => classes.delete(name),
      toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
      contains: name => classes.has(name),
    },
    options: [], value: "", style: { setProperty: noop, removeProperty: noop }, dataset: {},
    setAttribute: (name, value) => attrs.set(name, value),
    getAttribute: name => attrs.get(name) ?? null,
    addEventListener: (type, callback) => listeners.set(type, callback),
    click() { listeners.get("click")?.(); },
    querySelector: () => null, querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 270, top: 30, right: 310, bottom: 60, width: 40, height: 30 }),
  };
}
const nodes = new Map(ids.map(id => [id, element()]));
const document = {
  getElementById: id => nodes.get(id) ?? null, body: element(), documentElement: element(),
  querySelector: () => null, querySelectorAll: () => [], addEventListener: noop,
};
const storage = { getItem: () => null, setItem: noop, removeItem: noop };
const context = {
  __AGY_EXPORT_ADVISOR_FOR_TESTS__: true, __AGY_SKIP_AUTO_INIT__: true,
  document, localStorage: storage, sessionStorage: storage,
  location: new URL("https://hub.invalid/"), URL, URLSearchParams,
  innerWidth: 320, innerHeight: 568, navigator: { userAgent: "Mobile" },
  matchMedia: () => ({ matches: true }), requestAnimationFrame: noop, addEventListener: noop,
};
context.window = context;
const sandbox = vm.createContext(context);
for (const name of ["ui-shell.js", "app.js"]) {
  vm.runInContext(fs.readFileSync("app/static/" + name, "utf8"), sandbox, { filename: name, timeout: 5000 });
}
const api = context.__AGY_ADVISOR_API__;
api.syncSecurityModeUI();
api.syncModelPickerUI();
api.syncComposerState();
api.openModelPickerMenu();
const model = nodes.get("modelPickerMenu");
assert.ok(!model.classList.contains("hidden"));
assert.equal(model.style.position, "fixed");
assert.equal(nodes.get("modelPickerBtn").getAttribute("aria-expanded"), "true");
api.closeModelPickerMenu();
assert.ok(model.classList.contains("hidden"));
assert.equal(nodes.get("modelPickerBtn").getAttribute("aria-expanded"), "false");

api.bindNewContextEvents();
const menu = nodes.get("newActionPopover"), trigger = nodes.get("newActionMenuBtn");
trigger.click();
assert.ok(!menu.classList.contains("hidden"));
assert.equal(trigger.getAttribute("aria-expanded"), "true");
assert.equal(menu.style.position, "fixed");
context.innerWidth = 300;
api.repositionWorkspaceMenus();
assert.equal(menu.style.maxWidth, "284px");
trigger.click();
assert.ok(menu.classList.contains("hidden"));
assert.equal(trigger.getAttribute("aria-expanded"), "false");

vm.runInContext("globalThis.AgyUI = undefined", sandbox);
api.openModelPickerMenu();
assert.equal(model.style.width, "280px");
api.closeModelPickerMenu();
trigger.click();
assert.ok(!menu.classList.contains("hidden"));
api.closeNewContextMenus();
assert.ok(menu.classList.contains("hidden"));
"""


def test_workspace_menus_in_node_vm():
    parser = ShellParser()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    parser.close()
    run_node("workspace-menus", parser.ids, script=NODE_CHECKS)
