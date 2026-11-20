"""Chromium regression coverage for the sidebar with all quota cards expanded."""
from test_ui_workspace_minimal_v142 import run_workspace_chromium


SIDEBAR_CHECKS = r"""
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
        const bounds = el => el.getBoundingClientRect();
        const sidebar = get("sidebar");
        const tree = get("sidebarScroll");
        const footer = sidebar.querySelector(".sidebar-footer");
        const utilities = footer.querySelector(".sidebar-utility-bar");
        const quotas = footer.querySelector(".sidebar-telemetry-pod");

        sidebar.style.transition = "none";
        sidebar.classList.add("mobile-open");
        footer.classList.add("is-mob-telemetry-expanded");
        const providers = [
          ["sidebarChatGptBtn", "scgCompactView", "scgCardView"],
          ["sidebarClaudeBtn", "sclCompactView", "sclCardView"],
          ["sidebarOpenRouterBtn", "sorCompactView", "sorCardView"],
        ];
        for (const [card, compact, expanded] of providers) {
          get(card).classList.replace("is-unconfigured", "is-configured");
          get(compact).classList.add("hidden");
          get(expanded).classList.remove("hidden");
          check(bounds(get(expanded)).height > 0, expanded + " did not open");
        }

        // The earlier rules still live in @layer legacy, so measure the same cards
        // against those rules before enabling the release's component styles.
        const uiStyles = d.querySelector('style[data-asset="ui-system.css"]').sheet;
        const compactHeight = bounds(quotas).height;
        uiStyles.disabled = true;
        const previousHeight = bounds(quotas).height;
        uiStyles.disabled = false;

        api.state.projects = [{ name: "Workspace" }, { name: "Another project" }];
        api.state.chats = [
          { id: "folder-chat", kind: "chat", folder: "Planning", title: "A folder conversation" },
          ...Array.from({ length: 24 }, (_, index) => ({
            id: "chat-" + index, kind: "chat", title: "Long conversation title ".repeat(6) + index,
          })),
        ];
        api.renderUnifiedSidebar();
        get("sidebarActivitySection").classList.remove("hidden");
        get("sidebarProcessesStrip").classList.remove("hidden");
        const treeHeight = tree.clientHeight;
        check(tree.scrollHeight > treeHeight, "Expected a scrollable tree");
        const dragRow = tree.querySelector(".nav-item");
        dragRow.classList.add("drop-above");
        check(w.getComputedStyle(dragRow).boxShadow !== "none", "Drop indicator is missing");
        dragRow.classList.remove("drop-above");
        for (const el of [sidebar, tree]) {
          check(el.scrollWidth <= el.clientWidth + 1, el.id + " horizontal overflow");
        }

        const sidebarBox = bounds(sidebar);
        const utilityBox = bounds(utilities);
        check(utilityBox.height >= 30 && utilityBox.bottom <= Math.min(height, sidebarBox.bottom) &&
          utilityBox.left >= sidebarBox.left && utilityBox.right <= sidebarBox.right, "Utility bar clipped");
        check(bounds(tree).bottom <= bounds(footer).top + 1, "Footer overlaps the tree");
        check(bounds(sidebar.querySelector(".sidebar-brand")).height === 48, "Sidebar header height");
        check(bounds(get("searchInput")).height === 30, "Search input height");

        const project = tree.querySelector(".proj-tree-group");
        const name = project.querySelector(".proj-tree-name");
        const idleWidth = bounds(name).width;
        project.classList.add("menu-open");
        check(bounds(name).width === idleWidth, "Project name moved when actions appeared");
        project.classList.remove("menu-open");

        const x = width - 2;
        const y = height - 2;
        function checkMenu(menu) {
          const r = bounds(menu);
          check(r.width > 0 && r.height > 0 && r.left >= 0 && r.right <= width &&
            r.top >= 0 && r.bottom <= height, menu.id + ": " + JSON.stringify(r));
          check(r.bottom <= y + .1, menu.id + " did not flip above the pointer");
        }

        api.openProjectContextMenuAtPoint("Workspace", x, y);
        checkMenu(get("projectContextMenu"));
        check(get("ctxProjRenameBtn").querySelector("svg"), "Project edit icon missing");
        api.closeProjectContextMenu();
        api.openChatContextMenuAtPoint(api.state.chats[0], x, y);
        checkMenu(get("chatContextMenu"));
        check(get("chatContextMenu").querySelector('[data-ctx-action="reveal_folder"]'), "Folder reveal action lost");
        api.closeChatContextMenu();

        const group = tree.querySelector(".folder-group");
        const header = group.querySelector(".folder-header-btn");
        const folderMore = group.querySelector(".folder-more-btn");
        check(bounds(header).height === 32, "Folder row height: " + bounds(header).height);
        const before = group.classList.contains("collapsed");
        folderMore.click();
        check(folderMore.getAttribute("aria-expanded") === "true", "Folder menu did not open");
        const folderToggle = get("chatContextMenu").querySelector(".ctx-item");
        check(folderToggle?.querySelector("svg"), "Folder menu icon missing");
        folderToggle.click();
        check(group.classList.contains("collapsed") !== before, "Folder action did not toggle");
        check(folderMore.getAttribute("aria-expanded") === "false", "Folder menu did not reset");
        header.click();
        check(group.classList.contains("collapsed") === before, "Folder header click no longer toggles");

        for (const [card, compact, expanded] of providers) {
          get(card).classList.replace("is-configured", "is-unconfigured");
          get(compact).classList.remove("hidden");
          get(expanded).classList.add("hidden");
          check(bounds(get(card)).height === 27, card + " unconfigured row height");
        }
        resolve({ width, height, treeHeight, compactHeight, previousHeight });
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
  for (const [width, height] of [[1440, 900], [390, 844], [320, 568]]) {
    result.push(await measure(width, height));
  }
  document.getElementById("report").textContent = JSON.stringify({ result });
})().catch(error => {
  document.getElementById("report").textContent = JSON.stringify({ error: error.message });
});
"""


def test_sidebar_geometry_and_context_menus_in_chromium(tmp_path):
    reports = run_workspace_chromium(tmp_path, SIDEBAR_CHECKS)
    assert [(report["width"], report["height"]) for report in reports] == [
        (1440, 900), (390, 844), (320, 568),
    ]
    desktop = reports[0]
    assert desktop["treeHeight"] >= 320, desktop
    # Allow small font metric differences between local Chromium builds.
    assert desktop["compactHeight"] <= desktop["previousHeight"] * 0.73, desktop
