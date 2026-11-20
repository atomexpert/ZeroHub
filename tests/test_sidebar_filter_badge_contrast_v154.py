"""Regression suite v154: readable tinted sapphire filter-tab unread badges across all themes and palettes."""
import re

from test_frontend_contracts import STATIC, ShellParser
from test_ui_workspace_minimal_v142 import run_workspace_chromium


BADGE_CONTRAST_CHECKS = r"""
const palettes = ["emerald", "gemini", "linear", "claude", "nord", "amethyst", "copper", "amoled"];
const viewports = [[1440, 900], [390, 844]];

function check(ok, message) {
  if (!ok) throw new Error(message);
}

function parseColor(raw) {
  if (!raw || raw === "transparent") return [0, 0, 0, 0];
  const m = raw.match(/rgba?\(([^)]+)\)/i);
  if (!m) throw new Error("Unsupported color format: " + raw);
  const parts = m[1].split(/[\s,/]+/).filter(Boolean).map(Number);
  return [parts[0], parts[1], parts[2], parts.length >= 4 ? parts[3] : 1];
}

function blendOver(fg, bg) {
  const a = fg[3];
  return [
    fg[0] * a + bg[0] * (1 - a),
    fg[1] * a + bg[1] * (1 - a),
    fg[2] * a + bg[2] * (1 - a),
    1,
  ];
}

function effectiveBackground(el, win) {
  const chain = [];
  for (let cur = el; cur && cur.nodeType === 1; cur = cur.parentElement) {
    chain.push(parseColor(win.getComputedStyle(cur).backgroundColor));
  }
  let acc = [255, 255, 255, 1];
  for (let i = chain.length - 1; i >= 0; i--) {
    if (chain[i][3] > 0) acc = blendOver(chain[i], acc);
  }
  return acc;
}

function relativeLuminance(rgb) {
  const lin = (c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2]);
}

function contrastRatio(fg, bg) {
  const l1 = relativeLuminance(fg);
  const l2 = relativeLuminance(bg);
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
}

function measure(width, height) {
  return new Promise((resolve, reject) => {
    const frame = document.createElement("iframe");
    frame.style.cssText = "position:absolute;border:0;width:" + width + "px;height:" + height + "px";
    frame.onload = () => {
      const d = frame.contentDocument;
      const w = frame.contentWindow;
      const root = d.documentElement;
      const get = id => d.getElementById(id);
      let profile = width + "x" + height;
      try {
        const noAnim = d.createElement("style");
        noAnim.textContent = "*, *::before, *::after { transition: none !important; animation: none !important; }";
        d.head.appendChild(noAnim);
        const api = w.__AGY_ADVISOR_API__;
        const sidebar = get("sidebar");
        sidebar.style.transition = "none";
        if (width <= 860) sidebar.classList.add("mobile-open");

        api.state.projects = [{ name: "Antigravity_Hub" }];
        api.state.chats = [
          { id: "c_proj_done", kind: "project", project_name: "Antigravity_Hub", title: "Worker" },
          { id: "c_reg_stop", kind: "chat", title: "Stopped chat" },
          { id: "c_reg_err", kind: "chat", title: "Error chat" },
        ];
        api.state.unreadCompletedChats.clear();
        api.state.unreadCompletedChats.set("c_proj_done", { status: "done", ts: Date.now(), title: "Worker", runsCount: 1 });
        api.state.unreadCompletedChats.set("c_reg_stop", { status: "stopped", ts: Date.now(), title: "Stopped chat", runsCount: 1 });
        api.syncBackgroundAlertsUI();

        const bar = get("sidebarFilterBar");
        const pillAll = get("treeFilterBadgesAll").querySelector(".tfb-unread-pill.is-done");
        const pillProj = get("treeFilterBadgesProjects").querySelector(".tfb-unread-pill.is-done");
        const pillChatsStopped = get("treeFilterBadgesChats").querySelector(".tfb-unread-pill.is-stopped");
        const actPillDone = get("activitySectionBadges").querySelector(".act-sec-pill.is-done");
        check(pillAll && pillProj && pillChatsStopped && actPillDone, "Expected filter and activity pills to render");
        check(pillAll.textContent.trim() === "✓2", "All tab pill text: " + pillAll.textContent);
        check(pillProj.textContent.trim() === "✓1", "Projects tab pill text: " + pillProj.textContent);
        check(pillChatsStopped.textContent.trim() === "⏹1", "Chats stopped pill text: " + pillChatsStopped.textContent);

        let minDoneContrast = Infinity;
        for (const palette of palettes) {
          for (const theme of ["dark", "light"]) {
            profile = width + "x" + height + " " + palette + "/" + theme;
            Object.assign(root.dataset, { palette, theme });
            const actStyle = w.getComputedStyle(actPillDone);
            for (const pill of [pillAll, pillProj]) {
              const st = w.getComputedStyle(pill);
              check(st.color === actStyle.color, "Done pill color mismatch vs activity pill: " + st.color + " vs " + actStyle.color);
              check(st.backgroundColor === actStyle.backgroundColor, "Done pill bg mismatch vs activity pill: " + st.backgroundColor + " vs " + actStyle.backgroundColor);
              const bg = effectiveBackground(pill, w);
              const fg = blendOver(parseColor(st.color), bg);
              const cr = contrastRatio(fg, bg);
              minDoneContrast = Math.min(minDoneContrast, cr);
              check(cr >= 4.0, "Low contrast for .tfb-unread-pill.is-done (" + cr.toFixed(2) + ")");
            }
            const stopBg = effectiveBackground(pillChatsStopped, w);
            const stopFg = blendOver(parseColor(w.getComputedStyle(pillChatsStopped).color), stopBg);
            check(contrastRatio(stopFg, stopBg) >= 4.0, "Low contrast for .tfb-unread-pill.is-stopped");
          }
        }

        // Also check error tone contrast and pill geometry without label truncation.
        api.state.unreadCompletedChats.set("c_reg_err", { status: "error", ts: Date.now(), title: "Error chat", runsCount: 1 });
        api.syncBackgroundAlertsUI();
        const pillChatsErr = get("treeFilterBadgesChats").querySelector(".tfb-unread-pill.is-error");
        check(pillChatsErr && pillChatsErr.textContent.trim().startsWith("!"), "Error pill did not render");
        for (const theme of ["dark", "light"]) {
          Object.assign(root.dataset, { palette: "emerald", theme });
          const errBg = effectiveBackground(pillChatsErr, w);
          const errFg = blendOver(parseColor(w.getComputedStyle(pillChatsErr).color), errBg);
          check(contrastRatio(errFg, errBg) >= 4.0, "Low contrast for .tfb-unread-pill.is-error in " + theme);
        }

        check(bar.scrollWidth <= bar.clientWidth + 1, "Filter bar horizontal overflow");
        for (const btn of bar.querySelectorAll(".tree-filter-btn")) {
          const br = btn.getBoundingClientRect();
          const lbl = btn.querySelector(".tfb-label");
          const lr = lbl.getBoundingClientRect();
          const pill = btn.querySelector(".tfb-unread-pill");
          const pr = pill.getBoundingClientRect();
          check(pr.width > 0 && pr.height > 0, "Collapsed pill on " + btn.id);
          check(pr.left >= br.left - 0.5 && pr.right <= br.right + 0.5 && pr.top >= br.top - 0.5 && pr.bottom <= br.bottom + 0.5,
            "Pill clipped outside button " + btn.id);
          check(lr.right <= pr.left + 0.5, "Label overlaps pill on " + btn.id);
          check(lbl.scrollWidth <= lbl.clientWidth + 1, "Filter label truncated on " + btn.id);
        }

        resolve({ width, height, minDoneContrast: Math.round(minDoneContrast * 100) / 100 });
      } catch (error) {
        reject(new Error(profile + ": " + error.message));
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


def test_filter_badge_styles_and_cache_sync() -> None:
    ui_css = (STATIC / "ui-system.css").read_text(encoding="utf-8")
    stripped = re.sub(r"/\*[\s\S]*?\*/", "", ui_css)
    important_hits = re.findall(r"!\s*important\b", stripped, flags=re.IGNORECASE)
    assert len(important_hits) == 1, f"Expected strictly 1 !important in ui-system.css, got {len(important_hits)}"
    assert ".tfb-unread-pill.is-done" in stripped
    assert "var(--status-info)" in stripped
    assert "var(--bg-info-subtle)" in stripped

    sw_text = (STATIC / "sw.js").read_text(encoding="utf-8")
    m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_text)
    assert m_sw is not None and int(m_sw.group(1)) >= 154, sw_text[:120]
    ver = m_sw.group(1)
    parser = ShellParser()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    parser.close()
    assert f"/static/ui-system.css?v={ver}" in parser.assets


def test_sidebar_filter_badges_contrast_and_geometry_in_chromium(tmp_path) -> None:
    reports = run_workspace_chromium(tmp_path, BADGE_CONTRAST_CHECKS)
    assert len(reports) == 2
    for rep in reports:
        assert rep["minDoneContrast"] >= 4.0, rep
