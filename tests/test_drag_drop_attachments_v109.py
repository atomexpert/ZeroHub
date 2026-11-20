"""
Automated isolated verification suite for Release v109 (Chat Drag & Drop File Attachment):
1. Live `data/hub.db` byte-level immutability guard (`st_mtime_ns`, `st_size`).
2. Static `v>=109` cache sync (`index.html`, `sw.js`, `styles.css`, `app.js`), UTF-8 without BOM,
   CSS brace balance, and DOM/CSS presence of `#mainWorkspace`, `#chatFileDropOverlay`,
   `#chatFileDropTitle`, `#chatFileDropSub`, and `.composer-box.is-file-drag-target`.
3. `node --check` syntax verification on `app/static/app.js` and `app/static/sw.js`.
4. Node VM behavioral & end-to-end event simulation tests:
   - Russian pluralization & live drop counter titles (`formatRuFilesNoun`, `formatRuFilesDropTitle`);
   - External vs internal drag detection (`hasExternalDraggedFiles`, `countDraggedFileItems`);
   - Synchronous `DataTransfer` snapshot before async tick (`extractDroppedFilesFromDataTransfer`);
   - Recursive folder traversal skipping `.git` / `node_modules` and rejecting raw 0-byte directory pseudo-files;
   - Exact disk file deduplication (`isDuplicatePendingFile`, `addFilesToPendingAttachments`);
   - Full window `dragenter` / `dragover` / `dragleave` / `drop` simulation across chat workspace,
     composer box, sidebar cancel zone, open modal block, and Desktop read-only archive mode.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("ORT_DISABLE_ALL_GPU", "1")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

LIVE_HUB_DB = ROOT_DIR / "data" / "hub.db"
_INITIAL_LIVE_DB_SIG: tuple[int, int] | None = None
if LIVE_HUB_DB.exists():
    _st0 = LIVE_HUB_DB.stat()
    _INITIAL_LIVE_DB_SIG = (_st0.st_mtime_ns, _st0.st_size)


class ReleaseV109DragDropAttachmentsTests(unittest.TestCase):
    """Isolated verification of drag-and-drop file attachment in chat (v=109)."""

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        if _INITIAL_LIVE_DB_SIG is not None:
            assert LIVE_HUB_DB.exists(), "Live data/hub.db disappeared!"
            st_now = LIVE_HUB_DB.stat()
            assert (st_now.st_mtime_ns, st_now.st_size) == _INITIAL_LIVE_DB_SIG, (
                f"Live data/hub.db was modified! before={_INITIAL_LIVE_DB_SIG}, "
                f"after={(st_now.st_mtime_ns, st_now.st_size)}"
            )

    def _assert_live_db_untouched(self) -> None:
        if _INITIAL_LIVE_DB_SIG is not None:
            self.assertTrue(LIVE_HUB_DB.exists())
            st = LIVE_HUB_DB.stat()
            self.assertEqual(
                (st.st_mtime_ns, st.st_size),
                _INITIAL_LIVE_DB_SIG,
                "Live data/hub.db must remain untouched!",
            )

    def test_static_v109_assets_dom_css_and_utf8_no_bom(self) -> None:
        index_path = ROOT_DIR / "app" / "static" / "index.html"
        sw_path = ROOT_DIR / "app" / "static" / "sw.js"
        css_path = ROOT_DIR / "app" / "static" / "styles.css"
        js_path = ROOT_DIR / "app" / "static" / "app.js"

        for p in (index_path, sw_path, css_path, js_path):
            raw = p.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{p.name} must not have UTF-8 BOM")

        index_html = index_path.read_text(encoding="utf-8")
        sw_js = sw_path.read_text(encoding="utf-8")
        styles_css = css_path.read_text(encoding="utf-8")

        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_js)
        self.assertIsNotNone(m_sw, "sw.js CACHE_NAME must match agy-hub-v<N>")
        ver = int(m_sw.group(1))
        self.assertGreaterEqual(ver, 109)

        self.assertIn(f'/static/styles.css?v={ver}"', index_html)
        self.assertIn(f'/static/app.js?v={ver}"', index_html)
        self.assertIn(f'"/static/styles.css?v={ver}"', sw_js)
        self.assertIn(f'"/static/app.js?v={ver}"', sw_js)

        for html_id in (
            'id="mainWorkspace"',
            'id="chatFileDropOverlay"',
            'id="chatFileDropTitle"',
            'id="chatFileDropSub"',
        ):
            self.assertIn(html_id, index_html, f"Missing {html_id} in index.html")

        for selector in (
            ".chat-file-drop-overlay",
            ".chat-file-drop-overlay.is-readonly",
            ".chat-file-drop-card",
            ".chat-file-drop-icon",
            ".chat-file-drop-title",
            ".chat-file-drop-sub",
            ".composer-box.is-file-drag-target",
        ):
            self.assertIn(selector, styles_css, f"Missing CSS rule for {selector}")

        # Verify balanced braces in styles.css (after stripping CSS comments)
        css_no_comments = re.sub(r"/\*[\s\S]*?\*/", "", styles_css)
        self.assertEqual(
            css_no_comments.count("{"),
            css_no_comments.count("}"),
            "CSS curly braces must be balanced in styles.css",
        )

        self._assert_live_db_untouched()

    def test_js_syntax_and_node_vm_drag_drop_suite(self) -> None:
        app_js = ROOT_DIR / "app" / "static" / "app.js"
        sw_js = ROOT_DIR / "app" / "static" / "sw.js"

        for target in (app_js, sw_js):
            chk = subprocess.run(
                ["node", "--check", str(target)],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(chk.returncode, 0, f"node --check failed for {target}: {chk.stderr}")

        node_script = r"""
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const code = fs.readFileSync(process.env.APP_JS_PATH, "utf8");
const noop = () => {};

function makeClassList(initial = []) {
  const set = new Set(initial);
  return {
    add: (...cls) => cls.forEach((c) => set.add(c)),
    remove: (...cls) => cls.forEach((c) => set.delete(c)),
    toggle: (c, force) => {
      if (force === undefined) {
        if (set.has(c)) { set.delete(c); return false; }
        set.add(c); return true;
      }
      if (force) set.add(c); else set.delete(c);
      return Boolean(force);
    },
    contains: (c) => set.has(c),
  };
}

const elRegistry = new Map();
function makeEl(id = "", tagName = "DIV", initialClasses = []) {
  const listeners = new Map();
  const children = [];
  const el = {
    id,
    tagName,
    type: "",
    value: "",
    textContent: "",
    innerHTML: "",
    title: "",
    disabled: false,
    parentElement: null,
    style: { setProperty: noop, removeProperty: noop },
    dataset: {},
    classList: makeClassList(initialClasses),
    appendChild(child) {
      if (child && typeof child === "object") {
        child.parentElement = el;
        children.push(child);
      }
      return child;
    },
    contains(target) {
      let cur = target;
      while (cur) {
        if (cur === el) return true;
        cur = cur.parentElement || null;
      }
      return false;
    },
    closest(sel) {
      let cur = el;
      while (cur) {
        if (sel.startsWith("#") && cur.id === sel.slice(1)) return cur;
        if (sel.startsWith(".") && cur.classList && cur.classList.contains(sel.slice(1))) return cur;
        cur = cur.parentElement || null;
      }
      return null;
    },
    addEventListener(ev, fn) {
      if (!listeners.has(ev)) listeners.set(ev, []);
      listeners.get(ev).push(fn);
    },
    removeEventListener: noop,
    querySelector: () => makeEl("", "BUTTON"),
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 260, top: 0, right: 1280, bottom: 800, width: 1020, height: 800 }),
    focus() { el._focused = true; },
    setAttribute: noop,
    getAttribute: () => null,
    _children: children,
    _listeners: listeners,
  };
  if (id) elRegistry.set(id, el);
  return el;
}

// Build realistic workspace hierarchy
const sidebar = makeEl("sidebar", "ASIDE");
const sidebarItem = makeEl("sidebarItem1", "DIV");
sidebar.appendChild(sidebarItem);
const sidebarBackdrop = makeEl("sidebarBackdrop", "DIV", ["hidden"]);

const mainWorkspace = makeEl("mainWorkspace", "MAIN", ["workspace"]);
const chatFileDropOverlay = makeEl("chatFileDropOverlay", "DIV", ["chat-file-drop-overlay", "hidden"]);
const chatFileDropTitle = makeEl("chatFileDropTitle", "DIV");
const chatFileDropSub = makeEl("chatFileDropSub", "DIV");
chatFileDropOverlay.appendChild(chatFileDropTitle);
chatFileDropOverlay.appendChild(chatFileDropSub);
mainWorkspace.appendChild(chatFileDropOverlay);

const messagesContainer = makeEl("messagesContainer", "DIV");
const welcomeState = makeEl("welcomeState", "DIV");
messagesContainer.appendChild(welcomeState);
mainWorkspace.appendChild(messagesContainer);

const composerSection = makeEl("composerSection", "DIV");
const composerBox = makeEl("composerBox", "DIV", ["composer-box"]);
const attachmentsPreview = makeEl("attachmentsPreview", "DIV", ["hidden"]);
const promptInput = makeEl("promptInput", "TEXTAREA");
const sendBtn = makeEl("sendBtn", "BUTTON", ["hidden"]);
const stopBtn = makeEl("stopBtn", "BUTTON", ["hidden"]);
composerBox.appendChild(attachmentsPreview);
composerBox.appendChild(promptInput);
composerBox.appendChild(sendBtn);
composerBox.appendChild(stopBtn);
composerSection.appendChild(composerBox);
mainWorkspace.appendChild(composerSection);

const toastContainer = makeEl("toastContainer", "DIV");
let openModalEl = null;
let authAttr = "authenticated";

const winListeners = new Map();
const docListeners = new Map();

const docElement = makeEl("htmlRoot", "HTML");
docElement.getAttribute = (k) => (k === "data-auth" ? authAttr : "dark");
docElement.setAttribute = (k, v) => { if (k === "data-auth") authAttr = v; };

const sandbox = {
  console,
  Array,
  Object,
  Map,
  Set,
  Promise,
  RegExp,
  Date,
  Error,
  String,
  Number,
  Boolean,
  setTimeout,
  clearTimeout,
  setInterval: () => 1,
  clearInterval: noop,
  requestAnimationFrame: (cb) => { cb(); return 1; },
  cancelAnimationFrame: noop,
  URL: {
    createObjectURL: (f) => `blob:mock/${encodeURIComponent((f && f.name) || "file")}`,
    revokeObjectURL: noop,
  },
  CSS: { escape: (s) => String(s) },
  localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  sessionStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  navigator: { userAgent: "Mozilla/5.0", clipboard: { writeText: async () => {} } },
  location: { protocol: "http:", host: "127.0.0.1:8000", pathname: "/", reload: noop },
  WebSocket: function () {},
  FormData: function () { this.append = noop; },
  __AGY_EXPORT_ADVISOR_FOR_TESTS__: true,
  __AGY_SKIP_AUTO_INIT__: true,
};

sandbox.document = {
  hidden: false,
  documentElement: docElement,
  body: makeEl("body", "BODY"),
  getElementById: (id) => {
    if (!elRegistry.has(id)) makeEl(id, "DIV");
    return elRegistry.get(id);
  },
  createElement: (tag) => makeEl("", String(tag || "DIV").toUpperCase()),
  querySelector: (sel) => {
    if (sel === ".modal-backdrop:not(.hidden)") return openModalEl;
    if (sel === "main.workspace") return mainWorkspace;
    return null;
  },
  querySelectorAll: () => [],
  addEventListener: (ev, fn) => {
    if (!docListeners.has(ev)) docListeners.set(ev, []);
    docListeners.get(ev).push(fn);
  },
  removeEventListener: noop,
};

sandbox.window = Object.assign(sandbox, {
  innerWidth: 1440,
  innerHeight: 900,
  matchMedia: () => ({ matches: false, addEventListener: noop }),
  addEventListener: (ev, fn) => {
    if (!winListeners.has(ev)) winListeners.set(ev, []);
    winListeners.get(ev).push(fn);
  },
  removeEventListener: noop,
  getSelection: () => ({ isCollapsed: true, toString: () => "" }),
});
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(code, sandbox);

const api = sandbox.__AGY_ADVISOR_API__;
assert(api, "Expected __AGY_ADVISOR_API__ to be exported");

(async () => {
  // 1. Pluralization & drop title formatting
  assert.strictEqual(api.formatRuFilesNoun(1), "файл");
  assert.strictEqual(api.formatRuFilesNoun(2), "файла");
  assert.strictEqual(api.formatRuFilesNoun(4), "файла");
  assert.strictEqual(api.formatRuFilesNoun(5), "файлов");
  assert.strictEqual(api.formatRuFilesNoun(11), "файлов");
  assert.strictEqual(api.formatRuFilesNoun(21), "файл");
  assert.strictEqual(api.formatRuFilesDropTitle(0), "Отпустите файлы, чтобы прикрепить");
  assert.strictEqual(api.formatRuFilesDropTitle(1), "Отпустите файл, чтобы прикрепить");
  assert.strictEqual(api.formatRuFilesDropTitle(3), "Отпустите 3 файла, чтобы прикрепить");
  assert.strictEqual(api.formatRuFilesDropTitle(7), "Отпустите 7 файлов, чтобы прикрепить");

  // 2. External vs internal drag detection & countDraggedFileItems during dragover
  assert.strictEqual(api.hasExternalDraggedFiles({ types: ["Files"], files: [] }), true);
  assert.strictEqual(
    api.hasExternalDraggedFiles({
      types: { contains: (t) => t === "Files" },
      files: [],
    }),
    true
  );
  assert.strictEqual(api.hasExternalDraggedFiles({ types: ["text/plain", "text/uri-list"] }), false);
  assert.strictEqual(api.hasExternalDraggedFiles({ types: ["Files"] }, true), false);

  assert.strictEqual(
    api.countDraggedFileItems({
      items: [{ kind: "string" }, { kind: "file" }, { kind: "file" }],
      files: [],
    }),
    2
  );

  // 3. Synchronous snapshot before DataTransfer gets cleared by browser after drop returns
  const f1 = { name: "screen.png", size: 12040, type: "image/png", lastModified: 1700000001 };
  const f2 = { name: "notes.md", size: 840, type: "text/markdown", lastModified: 1700000002 };
  const ephemeralDt = {
    types: ["Files"],
    files: [f1, f2],
    items: [
      { kind: "file", getAsFile: () => f1, webkitGetAsEntry: () => ({ isFile: true, isDirectory: false }) },
      { kind: "file", getAsFile: () => f2, webkitGetAsEntry: () => ({ isFile: true, isDirectory: false }) },
    ],
  };
  const extractPromise = api.extractDroppedFilesFromDataTransfer(ephemeralDt);
  // Simulate browser clearing DataTransfer synchronously as soon as drop listener yields
  ephemeralDt.files = [];
  ephemeralDt.items = [];
  const res1 = await extractPromise;
  assert.strictEqual(res1.files.length, 2);
  assert.strictEqual(res1.files[0].name, "screen.png");
  assert.strictEqual(res1.files[1].name, "notes.md");
  assert.strictEqual(res1.hadDirectory, false);

  // 4. Directory traversal: extracts real files across batches, skips .git & node_modules, rejects empty folder pseudo-file
  const nestedA = { name: "app.py", size: 512, type: "text/x-python", lastModified: 1700000010 };
  const nestedB = { name: "config.json", size: 256, type: "application/json", lastModified: 1700000011 };
  const ignoredSecret = { name: "HEAD", size: 23, type: "text/plain", lastModified: 1700000012 };

  const makeFileEntry = (fileObj) => ({
    isFile: true,
    isDirectory: false,
    name: fileObj.name,
    file: (ok) => setTimeout(() => ok(fileObj), 5),
  });
  const makeDirEntry = (name, batches) => ({
    isFile: false,
    isDirectory: true,
    name,
    createReader: () => {
      let idx = 0;
      return {
        readEntries: (ok) => setTimeout(() => ok(batches[idx++] || []), 5),
      };
    },
  });

  const gitDir = makeDirEntry(".git", [[makeFileEntry(ignoredSecret)], []]);
  const srcDir = makeDirEntry("src", [[makeFileEntry(nestedA)], [makeFileEntry(nestedB)], []]);
  const rootDir = makeDirEntry("my_project", [[gitDir, srcDir], []]);
  const brokenFolderPseudoFile = { name: "my_project", size: 0, type: "", lastModified: 1700000099 };

  const dirDropRes = await api.extractDroppedFilesFromDataTransfer({
    types: ["Files"],
    files: [brokenFolderPseudoFile],
    items: [
      {
        kind: "file",
        getAsFile: () => brokenFolderPseudoFile,
        webkitGetAsEntry: () => rootDir,
      },
    ],
  });
  assert.strictEqual(dirDropRes.hadDirectory, true);
  assert.deepStrictEqual(
    Array.from(dirDropRes.files, (f) => f.name),
    ["app.py", "config.json"],
    "Must extract nested files and skip .git without pushing broken directory pseudo-file"
  );

  // Empty directory should return 0 files and hadDirectory=true (never broken pseudo-file)
  const emptyDir = makeDirEntry("empty_folder", [[]]);
  const emptyDirRes = await api.extractDroppedFilesFromDataTransfer({
    types: ["Files"],
    files: [brokenFolderPseudoFile],
    items: [
      {
        kind: "file",
        getAsFile: () => brokenFolderPseudoFile,
        webkitGetAsEntry: () => emptyDir,
      },
    ],
  });
  assert.strictEqual(emptyDirRes.hadDirectory, true);
  assert.strictEqual(emptyDirRes.files.length, 0);

  const toPlain = (obj) => JSON.parse(JSON.stringify(obj));

  // 5. Zone resolution & addFilesToPendingAttachments deduplication
  assert.deepStrictEqual(
    toPlain(api.resolveChatFileDropZoneState({ target: welcomeState, clientX: 600, clientY: 300 })),
    { mode: "accept", overComposer: false }
  );
  assert.deepStrictEqual(
    toPlain(api.resolveChatFileDropZoneState({ target: promptInput, clientX: 600, clientY: 740 })),
    { mode: "accept", overComposer: true }
  );
  assert.deepStrictEqual(
    toPlain(api.resolveChatFileDropZoneState({ target: sidebarItem, clientX: 120, clientY: 200 })),
    { mode: "outside", overComposer: false }
  );

  openModalEl = makeEl("settingsModal", "DIV");
  assert.strictEqual(
    api.resolveChatFileDropZoneState({ target: welcomeState, clientX: 600, clientY: 300 }).mode,
    "blocked"
  );
  openModalEl = null;

  const add1 = api.addFilesToPendingAttachments([f1, f2], { focusComposer: true, deduplicate: true });
  assert.deepStrictEqual(toPlain(add1), { addedCount: 2, skippedDuplicates: 0 });
  assert.strictEqual(attachmentsPreview.classList.contains("hidden"), false);
  assert.strictEqual(promptInput._focused, true);

  // Re-dropping exact same disk file f1 + new file nestedA skips f1 and adds nestedA
  const add2 = api.addFilesToPendingAttachments([f1, nestedA], { focusComposer: true, deduplicate: true });
  assert.deepStrictEqual(toPlain(add2), { addedCount: 1, skippedDuplicates: 1 });

  // 6. End-to-end window dragenter / dragover / dragleave / drop event simulation
  api.bindChatFileDragAndDrop();
  const fireWin = async (type, ev) => {
    for (const fn of winListeners.get(type) || []) {
      await fn(ev);
    }
  };
  const fireDoc = (type, ev = {}) => {
    for (const fn of docListeners.get(type) || []) {
      fn(ev);
    }
  };

  let prevented = false;
  let stopped = false;
  const dragDt = {
    types: ["Files"],
    dropEffect: "none",
    items: [{ kind: "file" }, { kind: "file" }],
    files: [],
  };
  await fireWin("dragover", {
    target: promptInput,
    clientX: 640,
    clientY: 740,
    dataTransfer: dragDt,
    preventDefault: () => { prevented = true; },
  });
  assert.strictEqual(prevented, true, "dragover with Files must preventDefault");
  assert.strictEqual(dragDt.dropEffect, "copy");
  assert.strictEqual(chatFileDropOverlay.classList.contains("hidden"), false);
  assert.strictEqual(composerBox.classList.contains("is-file-drag-target"), true);
  assert.strictEqual(chatFileDropTitle.textContent, "Отпустите 2 файла, чтобы прикрепить");

  // Moving to sidebar hides overlay and sets dropEffect=none while still preventing browser navigation
  prevented = false;
  await fireWin("dragover", {
    target: sidebarItem,
    clientX: 100,
    clientY: 220,
    dataTransfer: dragDt,
    preventDefault: () => { prevented = true; },
  });
  assert.strictEqual(prevented, true);
  assert.strictEqual(dragDt.dropEffect, "none");
  assert.strictEqual(chatFileDropOverlay.classList.contains("hidden"), true);
  assert.strictEqual(composerBox.classList.contains("is-file-drag-target"), false);

  // Dropping a new file inside workspace attaches it and hides overlay
  const droppedFile = { name: "diagram.webp", size: 4096, type: "image/webp", lastModified: 1700000999 };
  prevented = false;
  stopped = false;
  await fireWin("dragover", {
    target: welcomeState,
    clientX: 600,
    clientY: 320,
    dataTransfer: dragDt,
    preventDefault: () => {},
  });
  assert.strictEqual(chatFileDropOverlay.classList.contains("hidden"), false);

  await fireWin("drop", {
    target: welcomeState,
    clientX: 600,
    clientY: 320,
    dataTransfer: {
      types: ["Files"],
      files: [droppedFile],
      items: [{ kind: "file", getAsFile: () => droppedFile }],
    },
    preventDefault: () => { prevented = true; },
    stopPropagation: () => { stopped = true; },
  });
  assert.strictEqual(prevented, true, "drop must preventDefault");
  assert.strictEqual(stopped, true, "drop must stopPropagation");
  assert.strictEqual(chatFileDropOverlay.classList.contains("hidden"), true);

  // Internal DOM drag must NOT trigger chat file drop overlay
  fireDoc("dragstart");
  await fireWin("dragover", {
    target: welcomeState,
    clientX: 600,
    clientY: 320,
    dataTransfer: dragDt,
    preventDefault: () => {},
  });
  assert.strictEqual(chatFileDropOverlay.classList.contains("hidden"), true);
  fireDoc("dragend");

  console.log("ALL_DRAG_DROP_V109_CHECKS_PASSED");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
        env = os.environ.copy()
        env["APP_JS_PATH"] = str(app_js)
        run = subprocess.run(
            ["node", "-e", node_script],
            capture_output=True,
            text=True,
            env=env,
            timeout=25,
        )
        self.assertEqual(
            run.returncode,
            0,
            f"Node VM drag-drop suite failed:\nSTDOUT:\n{run.stdout}\nSTDERR:\n{run.stderr}",
        )
        self.assertIn("ALL_DRAG_DROP_V109_CHECKS_PASSED", run.stdout)
        self._assert_live_db_untouched()


if __name__ == "__main__":
    unittest.main()
