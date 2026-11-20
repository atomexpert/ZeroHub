"""Regression checks for the v141 CSS cascade and local UI helpers."""
import re

from test_frontend_contracts import STATIC, ShellParser, run_node

LAYER_ORDER = "@layer legacy, tokens, primitives, shell, components, utilities;"
CSS_TOKENS = re.compile(
    r"""/\*[\s\S]*?(?:\*/|\Z)|"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*'|\\[\s\S]|[{}"']"""
)


def top_level_blocks(filename):
    source = (STATIC / filename).read_text(encoding="utf-8")
    blocks = []
    depth = prelude_start = 0
    for match in CSS_TOKENS.finditer(source):
        token = match[0]
        line = lambda: source.count("\n", 0, match.start()) + 1
        if token == "{":
            if depth == 0:
                prelude = re.sub(r"/\*[\s\S]*?\*/", "", source[prelude_start:match.start()])
                blocks.append(prelude.strip())
            depth += 1
        elif token == "}":
            depth -= 1
            assert depth >= 0, f"{filename}:{line()}: unexpected closing brace"
            if depth == 0:
                prelude_start = match.end()
        else:
            assert token not in ('"', "'"), f"{filename}:{line()}: unterminated string"
            assert not token.startswith("/*") or token.endswith("*/"), (
                f"{filename}:{line()}: unterminated comment"
            )
    assert depth == 0, f"{filename}: {depth} unclosed block(s)"
    assert not re.sub(r"/\*[\s\S]*?\*/", "", source[prelude_start:]).strip(), filename
    return source, blocks


def test_css_layer_structure_and_palettes():
    legacy, blocks = top_level_blocks("styles.css")
    assert legacy.startswith(LAYER_ORDER + "\n")
    assert blocks == [LAYER_ORDER + "\n\n@layer legacy"]
    palettes = ("emerald", "gemini", "linear", "claude", "nord", "amethyst", "copper", "amoled")
    for palette in palettes:
        for theme in ("light", "dark"):
            selector = f'html[data-palette="{palette}"][data-theme="{theme}"]'
            rule = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", legacy)
            assert rule and "--accent:" in rule[1] and "--focus-ring:" in rule[1], selector

    _, blocks = top_level_blocks("ui-system.css")
    assert blocks == ["@layer tokens", "@layer primitives", "@layer shell", "@layer components", "@layer utilities"]


def test_ui_shell_loads_before_app_with_real_dom_ids():
    shell = ShellParser()
    shell.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    shell.close()
    assets = [url.split("?", 1)[0] for url in shell.assets]
    assert assets.index("/static/ui-system.css") == assets.index("/static/styles.css") + 1
    assert assets.index("/static/ui-shell.js") == assets.index("/static/app.js") - 1
    run_node("smoke-foundation", shell.ids)


GEOMETRY = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const code = fs.readFileSync("app/static/ui-shell.js", "utf8");
const size = { width: 180, height: 120 };
const rect = (left, top, width = 32, height = 28) => ({ left, top, width, height });

for (const [width, height] of [[1440, 900], [390, 844], [320, 568]]) {
  const context = { innerWidth: width, innerHeight: height };
  vm.runInNewContext(code, context);
  const position = context.AgyUI.computeFloatingPosition;
  const right = position(rect(width - 40, height - 40), size);
  assert.equal(right.placement, "top-start");
  assert.equal(right.x, width - size.width - 8);
  assert.equal(right.y, height - 40 - size.height - 6);

  const left = position(rect(-15, 12), size, { placement: "top-end" });
  assert.equal(left.placement, "bottom-end");
  assert.equal(left.x, 8);
  assert.equal(left.y, 46);

  const aligned = position(rect(230, 180), size, { placement: "top-end", offset: 10 });
  assert.equal(aligned.placement, "top-end");
  assert.equal(aligned.x, 82);
  assert.equal(aligned.y, 50);

  const oversized = position(rect(25, height - 70, width), { width: width * 2, height: height * 2 }, {
    matchMinWidth: true, padding: 12,
  });
  assert.equal(oversized.placement, "top-start");
  assert.equal(oversized.x, 12);
  assert.equal(oversized.y, 12);
  assert.equal(oversized.minWidth, width - 24);
  assert.equal(oversized.maxWidth, width - 24);
  assert.equal(oversized.maxHeight, height - 88);
}

function events() {
  const handlers = new Map();
  return {
    addEventListener: (type, callback) => handlers.set(type, callback),
    removeEventListener(type, callback) {
      assert.equal(handlers.get(type), callback);
      handlers.delete(type);
    },
    dispatch: (type, details = {}) => handlers.get(type)?.({ type, ...details }),
    get count() { return handlers.size; },
  };
}

const windowEvents = events();
const viewport = Object.assign(events(), { width: 390, height: 844, offsetTop: 0, offsetLeft: 0 });
const document = events();
const context = {
  ...windowEvents, visualViewport: viewport, document,
  innerWidth: 390, innerHeight: 844,
};
context.window = context;
vm.runInNewContext(code, context);
const ui = context.AgyUI;
assert.equal(context.window.AgyUI, ui);
let focused = false;
const anchor = {
  getBoundingClientRect: () => rect(260, 410, 40),
  focus() { focused = true; },
};
function floating() {
  return { style: {}, hidden: false, getBoundingClientRect: () => size };
}
const popover = floating();
const reasons = [];
const unbind = ui.bindDismissablePopover(anchor, popover, { onDismiss: reason => reasons.push(reason) });
assert.equal(popover.style.position, "fixed");
assert.equal(popover.style.top, "444px");
assert.equal(popover.style.left, "202px");

Object.assign(viewport, { width: 300, height: 436, offsetTop: 36, offsetLeft: 12 });
viewport.dispatch("resize");
assert.equal(popover.hidden, false);
assert.equal(popover.style.top, "284px");
assert.equal(popover.style.left, "124px");
assert.equal(popover.style.maxHeight, "360px");
assert.equal(popover.style.maxWidth, "284px");

const child = floating();
ui.bindDismissablePopover(anchor, child, { onDismiss: reason => reasons.push(reason), matchMinWidth: true });
assert.equal(child.style.minWidth, "40px");
document.dispatch("pointerdown", { target: child, composedPath: () => [child] });
assert.equal(child.hidden, false);
document.dispatch("keydown", { key: "Escape", preventDefault() {} });
assert.equal(child.hidden, true);
assert.equal(popover.hidden, false);
assert.equal(focused, true);
document.dispatch("pointerdown", { target: anchor, composedPath: () => [anchor] });
assert.equal(popover.hidden, false);
document.dispatch("pointerdown", { target: {}, composedPath: () => [] });
assert.equal(popover.hidden, true);
unbind();
assert.deepEqual(reasons, ["escape", "outside"]);
assert.equal(document.count, 0);
assert.equal(windowEvents.count, 0);
assert.equal(viewport.count, 0);

popover.hidden = false;
const cleanup = ui.bindDismissablePopover(anchor, popover);
cleanup();
assert.equal(popover.hidden, false);
ui.bindDismissablePopover(anchor, popover, { onDismiss: reason => reasons.push(reason) });
ui.closeOpenPopovers();
assert.equal(popover.hidden, true);
assert.equal(reasons.at(-1), "programmatic");
assert.equal(document.count + windowEvents.count + viewport.count, 0);

const names = (
  "plus search folder chat pin more settings edit trash archive share terminal cpu check x " +
  "chevronDown chevronRight sparkles layers mic send paperclip refresh gitBranch history shieldCheck cloud lock zeroRing " +
  "eye eyeOff copy key"
).split(" ");
assert.deepEqual(Object.keys(ui.ICO).sort(), names.sort());
for (const name of names) {
  const icon = ui.icon(name, "ui-ico-xs");
  assert.match(icon, /^<svg class="ui-ico ui-ico-xs"/);
  assert.match(icon, /viewBox="0 0 24 24".*stroke="currentColor" stroke-width="1.75"/);
  assert.match(icon, /aria-hidden="true".*<[/a-z]+/);
  assert.ok(icon.endsWith("</svg>"));
}
assert.equal(ui.icon("toString"), "");
assert.doesNotMatch(ui.icon("mic", '" onload="unexpected'), /onload=/);
"""


def test_floating_geometry_dismissal_and_icons():
    run_node("foundation", script=GEOMETRY)
