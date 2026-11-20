"""Exercise an actual rendered conversation across sizes, palettes and themes."""
from test_ui_workspace_minimal_v142 import run_workspace_chromium


CONVERSATION_CHECKS = r"""
const sizes = [[1440, 900], [390, 844], [320, 568]];
const palettes = ["emerald", "gemini", "linear", "claude", "nord", "amethyst", "copper", "amoled"];
function check(ok, message) {
  if (!ok) throw new Error(message);
}

function measure(width, height) {
  return new Promise((resolve, reject) => {
    const frame = document.createElement("iframe");
    frame.style.cssText = "position:absolute;border:0;width:" + width + "px;height:" + height + "px";
    frame.onload = () => {
      const d = frame.contentDocument, w = frame.contentWindow;
      const get = id => d.getElementById(id);
      const root = d.documentElement;
      let profile = width + "x" + height;
      try {
        const api = w.__AGY_ADVISOR_API__;
        api.setupMarkdown();
        get("workspaceTabbar").classList.add("has-project-mode");
        api.syncMobileHeaderToolsSlot();
        const title = "Long project branch title ".repeat(12);
        get("activeChatTitle").textContent = title;
        get("chatContextBadge").textContent = title;
        get("chatContextBadge").classList.remove("hidden");
        get("welcomeState").classList.add("hidden");
        get("composerSection").classList.remove("hidden");
        const bar = get("chatActiveProcessesBar");
        bar.classList.remove("hidden");
        bar.innerHTML = '<span class="cap-bar-prefix">Processes</span><button type="button" class="process-capsule">Running service</button>';

        const quote = api.buildReplyQuoteXmlBlock({
          mode: "selection", msgId: "a0", turnNumber: 1, role: "assistant",
          authorLabel: "Assistant", text: "An excerpt from the previous answer.",
        });
        const header = "| " + Array.from({length: 8}, (_, i) => "Column " + (i + 1)).join(" | ") + " |";
        const separator = "|" + "---|".repeat(8);
        const data = "| " + Array(8).fill("A long table cell").join(" | ") + " |";
        const code = "x".repeat(200);
        const fence = String.fromCharCode(96).repeat(3);
        const assistant = {
          id: "a1", role: "assistant", status: "done", thinking: "Read the source first.",
          tool_calls: Array.from({ length: 20 }, () => ({ name: "read_file", parameters: {Path: "fixture.js"}, state: "DONE" })),
          content: ["## Technical answer", "", "- First item", "- Second item", "", fence + "js", code, fence, "",
            header, separator, data].join("\n"),
        };
        const user = { id: "u1", role: "user", content: "Please review this excerpt.\n\n" + quote };
        api.state.messages = [user, assistant];
        const list = get("messagesList");
        list.append(api.buildMessageDom(user), api.buildMessageDom(assistant));
        check(list.querySelector(".user-reply-quote"), "Reply quote did not render");
        for (const selector of [".thinking-box", ".tools-spoiler"]) {
          const details = list.querySelector(selector);
          check(details && !details.open, selector + " is missing or expanded");
        }
        const pre = list.querySelector(".code-block-wrap pre");
        const table = list.querySelector(".markdown-body table");
        check(pre?.textContent.trim() === code, "The 200-character line did not render");
        check(table?.querySelectorAll("th").length === 8, "The eight-column table did not render");

        const stream = get("messagesContainer");
        const composer = get("composerBox");
        for (const palette of palettes) {
          for (const theme of ["dark", "light"]) {
            profile = width + "x" + height + " " + palette + "/" + theme;
            Object.assign(root.dataset, { palette, theme });
            check(root.scrollWidth <= w.innerWidth, "Document overflow");
            check(stream.scrollWidth <= stream.clientWidth + 1, "Message stream overflow");
            const header = get("workspaceTabbar").getBoundingClientRect();
            const mobile = get("mobileAccessBar").getBoundingClientRect();
            const prompt = composer.getBoundingClientRect();
            const messages = stream.getBoundingClientRect();
            const processes = bar.getBoundingClientRect();
            check(header.top >= 0 && messages.top >= Math.max(header.bottom, mobile.bottom) - 1,
              "Header overlaps messages");
            check(messages.bottom <= processes.top + 1 && processes.bottom <= prompt.top + 1 &&
              prompt.bottom <= height && prompt.width > 0, "Messages, process bar and composer overlap");
            for (const block of [pre, table]) {
              const r = block.getBoundingClientRect();
              check(r.left >= messages.left && r.right <= messages.right + 1 && block.clientWidth > 0, "Wide block escaped stream");
              check(w.getComputedStyle(block).overflowX === "auto" && block.scrollWidth > block.clientWidth,
                block.tagName + " must overflow locally");
              block.scrollLeft = 50;
              check(block.scrollLeft > 0 && stream.scrollLeft === 0, block.tagName + " is not independently scrollable");
              block.scrollLeft = 0;
            }
          }
        }
        resolve({ width, height, profiles: palettes.length * 2 });
      } catch (error) {
        reject(new Error(profile + " " + error.message));
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
  for (const [width, height] of sizes) result.push(await measure(width, height));
  document.getElementById("report").textContent = JSON.stringify({ result });
})().catch(error => {
  document.getElementById("report").textContent = JSON.stringify({ error: error.message });
});
"""


def test_conversation_geometry_in_chromium(tmp_path):
    reports = run_workspace_chromium(
        tmp_path, CONVERSATION_CHECKS, vendor_scripts=("vendor/marked.min.js",),
    )
    assert reports == [
        {"width": 1440, "height": 900, "profiles": 16},
        {"width": 390, "height": 844, "profiles": 16},
        {"width": 320, "height": 568, "profiles": 16},
    ]
