"""Chromium tests for dialog geometry, sheet scrolling and Escape priority."""
from test_ui_workspace_minimal_v142 import run_workspace_chromium


DIALOG_CHECKS = r"""
function check(ok, message) {
  if (!ok) throw new Error(message);
}

function measure(width, height) {
  return new Promise((resolve, reject) => {
    const frame = document.createElement("iframe");
    frame.style.cssText = "border:0;position:absolute;width:" + width + "px;height:" + height + "px";
    frame.onload = () => {
      try {
        const w = frame.contentWindow, d = frame.contentDocument;
        const get = id => d.getElementById(id);
        const rect = element => element.getBoundingClientRect();
        const mobile = width <= 768;
        const epsilon = 1;
        const close = (a, b) => Math.abs(a - b) <= epsilon;
        const visible = element => element.getClientRects().length > 0;
        function inside(element, limits = { left: 0, right: width, top: 0, bottom: height }) {
          const r = rect(element);
          check(r.width > 0 && r.height > 0 && r.left >= limits.left - epsilon &&
            r.right <= limits.right + epsilon && r.top >= limits.top - epsilon &&
            r.bottom <= limits.bottom + epsilon, element.id + " " + element.className + ": " + JSON.stringify(r));
          return r;
        }

        const list = get("hostServicesList");
        list.innerHTML = Array.from({ length: 24 }, (_, i) =>
          '<div class="host-svc-card"><strong>Service ' + i + '</strong><code class="host-svc-cmd-line">' +
          'very-long-program-argument'.repeat(16) + '</code></div>').join("");
        const logs = get("hostServiceLogsBox");
        logs.classList.remove("hidden");
        const pre = get("hostServiceLogsPre");
        pre.textContent = Array.from({ length: 50 }, (_, index) => "Log " + index + " " + "x".repeat(200)).join("\n");
        get("clearInactiveProcessesBtn").classList.remove("hidden");
        get("hostServiceCreateDetails").open = true;

        get("shareProjectMembersList").innerHTML = '<label>Project member</label>'.repeat(20);
        get("unshareProjectBtn").classList.remove("hidden");
        get("deleteProjectConfirmWrap").classList.remove("hidden");
        get("deleteProjectConfirmLabel").textContent = "Type the project name to confirm.";

        const tested = [];
        for (const card of d.querySelectorAll(".modal-backdrop > .modal-card")) {
          const modal = card.parentElement;
          card.style.animation = "none";
          modal.classList.remove("hidden");
          const r = inside(card);
          if (mobile) {
            check(close(r.bottom, height) && close(r.left, 0) && close(r.width, width),
              modal.id + " is not a full-width bottom sheet");
            check(card.scrollWidth <= card.clientWidth + 1, modal.id + " has horizontal overflow");
          } else {
            check(close((r.top + r.bottom) / 2, height / 2) && close((r.left + r.right) / 2, width / 2),
              modal.id + " is not centered");
          }
          for (const header of card.querySelectorAll(".modal-header")) {
            if (!visible(header)) continue;
            inside(header, r);
            for (const button of header.querySelectorAll("button.icon-btn")) {
              if (visible(button)) inside(button, r);
            }
          }
          for (const footer of card.querySelectorAll(".modal-footer")) {
            if (!visible(footer)) continue;
            inside(footer, r);
            for (const btn of footer.querySelectorAll("button")) {
              if (visible(btn)) inside(btn, r);
            }
          }
          for (const fieldset of card.querySelectorAll("fieldset")) {
            check(w.getComputedStyle(fieldset).borderTopStyle === "solid", modal.id + " fieldset border");
          }
          if (modal.id === "hostServicesModal") {
            const listBox = inside(list, r);
            const logBox = inside(logs, r);
            inside(get("hostServiceCreateDetails"), r);
            check(listBox.bottom <= logBox.top + epsilon, "Process list overlaps dock");
            check(list.scrollHeight > list.clientHeight && pre.scrollHeight > pre.clientHeight,
              "Process list and logs must scroll independently");
            const originalTop = logBox.top;
            list.scrollTop = list.scrollHeight;
            check(list.scrollTop > 0 && close(rect(logs).top, originalTop), "Log dock moved with the list");
          }
          if (modal.id === "shareProjectModal") {
            const body = modal.querySelector(".modal-body");
            const footer = modal.querySelector(".modal-footer");
            const before = rect(footer).top;
            if (mobile) check(body.scrollHeight > body.clientHeight, "Share form body must scroll");
            body.scrollTop = body.scrollHeight;
            check(close(rect(footer).top, before), "Share footer scrolled away");
          }
          check(d.documentElement.scrollWidth <= w.innerWidth, modal.id + " creates document overflow");
          modal.classList.add("hidden");
          tested.push(modal.id);
        }
        for (const id of ["settingsModal", "quotaModal", "usersAdminModal", "hostServicesModal",
          "modesHelpModal", "deleteProjectModal", "shareProjectModal", "handoffNewTabModal", "projectMemoryModal"]) {
          check(tested.includes(id), "Missing dialog " + id);
        }

        const outline = get("chatOutlinePopover");
        outline.style.transition = "none";
        outline.classList.remove("hidden");
        outline.classList.add("is-open");
        const outlineBox = inside(outline);
        inside(get("chatOutlineSearchInput"), outlineBox);
        if (mobile) {
          check(close(outlineBox.bottom, height) && close(outlineBox.width, width), "Outline is not a bottom sheet");
          inside(get("chatOutlineSheetGrabber"), outlineBox);
        }
        outline.classList.add("hidden");

        const router = get("openrouterModal"), suggestion = get("orModelSuggestList");
        router.classList.remove("hidden");
        suggestion.classList.remove("hidden");
        const pressEscape = () => w.__AGY_ADVISOR_API__.handleWorkspaceEscape(
          new w.KeyboardEvent("keydown", { key: "Escape", cancelable: true }));
        pressEscape();
        check(suggestion.classList.contains("hidden") && visible(router), "First Escape did not close only suggestions");
        pressEscape();
        check(router.classList.contains("hidden"), "Second Escape did not close the dialog");

        resolve({ width, height, dialogs: tested.length });
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


def test_dialogs_and_sheets_in_chromium(tmp_path):
    reports = run_workspace_chromium(tmp_path, DIALOG_CHECKS)
    assert [(report["width"], report["height"]) for report in reports] == [
        (1440, 900), (390, 844), (320, 568),
    ]
    assert all(report["dialogs"] >= 20 for report in reports), reports
