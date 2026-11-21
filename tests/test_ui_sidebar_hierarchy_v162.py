"""Chromium regression coverage for the sidebar's expanded folder hierarchy."""
from test_ui_workspace_minimal_v142 import run_workspace_chromium


BROWSER_CHECKS = r"""
function check(ok, message) {
  if (!ok) throw new Error(message);
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
    const sidebar = d.getElementById("sidebar");
    const tree = d.getElementById("sidebarScroll");
    const rect = node => node.getBoundingClientRect();
    sidebar.style.transition = "none";
    sidebar.classList.add("mobile-open");

    const names = ["First project with a very long title", "Second project with a very long title", "Collapsed project"];
    api.state.projects = names.map(name => ({ name }));
    api.state.expandedProjects = new Set(names.slice(0, 2));
    api.state.chats = [
      ...names.slice(0, 2).map((name, index) => ({
        id: "branch-" + index, kind: "project", project_name: name, title: "Long branch title ".repeat(8),
      })),
      { id: "folder-chat", kind: "chat", folder: "Planning", title: "Long conversation title ".repeat(8) },
    ];
    api.state.activeChatId = "branch-0";
    api.state.activeChat = api.state.chats[0];
    api.renderUnifiedSidebar();

    const projects = [...tree.querySelectorAll(".proj-tree-group")];
    const [first, second, third] = projects;
    check(first?.matches(".expanded.has-active-chat") && second?.matches(".expanded") &&
      third?.matches(":not(.expanded)"), "Expected two expanded projects and one collapsed project");
    const chatFolder = d.querySelector("#foldersContainer .folder-group:not(.collapsed)");
    const chatList = chatFolder.querySelector(":scope > .nav-list");

    for (const theme of ["dark", "light"]) {
      d.documentElement.dataset.theme = theme;
      for (const project of [first, second]) {
        const header = project.querySelector(":scope > .proj-tree-header");
        const folderIcon = header.querySelector(".proj-tree-folder-ico");
        const name = header.querySelector(".proj-tree-name");
        const body = project.querySelector(":scope > .proj-tree-body");
        const row = body.querySelector(".nav-item");
        const childIcon = row.querySelector(".nav-item-icon-btn");
        check(rect(childIcon).width > 0 && rect(childIcon).left >= rect(folderIcon).left,
          "Branch icon is left of its folder: " + rect(childIcon).left + " < " + rect(folderIcon).left);
        const projectStyle = w.getComputedStyle(project);
        check(["Top", "Right", "Bottom", "Left"].every(side => projectStyle["border" + side + "Width"] === "1px"),
          "Project card does not have a 1px frame");
        check(w.getComputedStyle(header).borderBottomWidth === "1px", "Header separator is missing");
        check(parseFloat(w.getComputedStyle(body).borderInlineStartWidth) > 0, "Project tree guide is missing");
        check(rect(row).height < rect(header).height, "Branch row is as tall as its parent header");
        const idleWidth = rect(name).width;
        project.classList.add("menu-open");
        check(idleWidth > 0 && rect(name).width === idleWidth, "Project label moved when opening the menu");
        project.classList.remove("menu-open");
      }

      check(rect(second).top - rect(first).bottom >= 6 && rect(third).top - rect(second).bottom >= 6,
        "Neighboring project cards are too close");
      check(w.getComputedStyle(second, "::before").display === "none", "Redundant outside separator is visible");
      for (const [project, side, pseudo] of [[second, "above", "::before"], [first, "below", "::after"]]) {
        const className = "proj-drop-" + side;
        project.classList.add(className);
        const style = w.getComputedStyle(project, pseudo);
        check(style.display !== "none" && style.content !== "none" && style.height === "2px",
          "Project drop indicator is missing");
        project.classList.remove(className);
      }

      check(rect(sidebar.querySelector(".sidebar-brand")).height === 48, "Sidebar brand height changed");
      const chatHeader = chatFolder.querySelector(".folder-header-btn");
      check(rect(chatHeader).height === 32, "Chat folder header height changed");
      check(parseFloat(w.getComputedStyle(chatList).borderInlineStartWidth) > 0 &&
        rect(chatList).left > rect(chatHeader).left, "Chat folder guide or indent is missing");
      for (const element of [sidebar, tree]) {
        check(element.scrollWidth <= element.clientWidth + 1, element.id + " has horizontal overflow");
      }
    }
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


def test_expanded_folder_hierarchy_in_chromium(tmp_path):
    assert run_workspace_chromium(tmp_path, BROWSER_CHECKS) == [[1440, 900], [390, 844], [320, 568]]
