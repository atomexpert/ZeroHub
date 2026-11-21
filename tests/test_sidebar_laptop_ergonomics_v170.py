"""Chromium regression suite v170: compact laptop/desktop sidebar ergonomics."""
from __future__ import annotations

import re

from test_frontend_contracts import STATIC, ShellParser
from test_ui_workspace_minimal_v142 import run_workspace_chromium


BROWSER_CHECKS = r"""
function check(ok, message) {
  if (!ok) throw new Error(message);
}

function measure(width, height, scenario) {
  return new Promise((resolve, reject) => {
    const frame = document.createElement("iframe");
    frame.style.cssText = "border:0;position:absolute;width:" + width + "px;height:" + height + "px";
    frame.onload = () => {
      const w = frame.contentWindow;
      const d = frame.contentDocument;
      const api = w.__AGY_ADVISOR_API__;
      const get = id => d.getElementById(id);
      const rect = el => el.getBoundingClientRect();
      let stage = width + "x" + height;
      try {
        const noAnim = d.createElement("style");
        noAnim.textContent = "*, *::before, *::after { transition: none !important; animation: none !important; }";
        d.head.appendChild(noAnim);

        const sidebar = get("sidebar");
        const tree = get("sidebarScroll");
        const footer = sidebar.querySelector(".sidebar-footer");
        const bar = get("mobileTelemetryBar");
        const pod = footer.querySelector(".sidebar-telemetry-pod");
        const expandBtn = get("mobTelemetryExpandBtn");
        const chevron = get("mobTelemetryChevron");

        sidebar.style.transition = "none";
        if (width <= 860) sidebar.classList.add("mobile-open");
        if (width > 860) d.documentElement.style.setProperty("--sidebar-width", "272px");

        api.bindTelemetryDockEvents();

        // Populate projects (including self-hub + processes + 5 chat statuses) and chats for scrollability.
        api.state.projects = [
          {
            name: "Antigravity_Hub",
            is_self_hub: true,
            is_shared: true,
            execution_mode: "host",
            processes_summary: { running_count: 2, crashed_count: 1 },
          },
          {
            name: "Сайты",
            processes_summary: { running_count: 1, crashed_count: 0 },
          },
          {
            name: "Очень длинное название проекта для проверки усечения",
          },
        ];
        api.state.expandedProjects = new Set(["Сайты"]);
        api.state.chats = [
          { id: "c-err", kind: "project", project_name: "Antigravity_Hub", title: "Crash tab" },
          { id: "c-run", kind: "project", project_name: "Antigravity_Hub", title: "Running tab", is_generating: true, queue_count: 2 },
          { id: "c-done", kind: "project", project_name: "Antigravity_Hub", title: "Done tab" },
          { id: "c-stop", kind: "project", project_name: "Antigravity_Hub", title: "Stopped tab" },
          { id: "c-site-1", kind: "project", project_name: "Сайты", title: "Landing" },
          ...Array.from({ length: 18 }, (_, i) => ({
            id: "reg-" + i,
            kind: "chat",
            title: "Regular conversation " + i,
          })),
        ];
        api.state.unreadCompletedChats.clear();
        api.state.unreadCompletedChats.set("c-err", { status: "error", ts: Date.now(), title: "Crash tab", runsCount: 1 });
        api.state.unreadCompletedChats.set("c-done", { status: "done", ts: Date.now(), title: "Done tab", runsCount: 1 });
        api.state.unreadCompletedChats.set("c-stop", { status: "stopped", ts: Date.now(), title: "Stopped tab", runsCount: 1 });
        api.state.unreadCompletedChats.set("c-site-1", { status: "done", ts: Date.now(), title: "Landing", runsCount: 1 });
        api.state.chatQueues.clear();
        api.state.activeTab = "project";
        api.state.selectedProject = "Antigravity_Hub";
        api.state.activeChatId = null;

        api.renderUnifiedSidebar();

        // Check search input compacting & height invariant
        const searchInput = get("searchInput");
        check(searchInput.getAttribute("placeholder") === "Поиск…", "Unexpected search placeholder");
        check(searchInput.getAttribute("aria-label") === "Поиск чатов и проектов", "Missing search aria-label");
        check(Math.abs(rect(searchInput).height - 30) <= 0.5, "Search input height must stay 30px, got " + rect(searchInput).height);
        check(Math.abs(rect(sidebar.querySelector(".sidebar-brand")).height - 48) <= 0.5, "Sidebar brand height must stay 48px");
        if (width <= 1440) {
          const kbd = sidebar.querySelector(".sidebar-search-row .kbd-hint");
          check(w.getComputedStyle(kbd).display === "none", ".kbd-hint must be hidden at width <= 1440px");
          check(parseFloat(w.getComputedStyle(searchInput).paddingRight) <= 10.5, "Search padding-inline-end must shrink to 10px");
        }

        // Check single-line .proj-tree-header.has-badges across dark & light themes
        let procClicked = false;
        const hubGroup = tree.querySelector('.proj-tree-group[data-proj-group="Antigravity_Hub"]');
        const hubHeader = hubGroup.querySelector(":scope > .proj-tree-header");
        const hubName = hubHeader.querySelector(".proj-tree-name");
        const hubSelfBadge = hubHeader.querySelector(".proj-self-hub-badge");
        const hubSharedBadge = hubHeader.querySelector(".proj-tree-shared-badge");
        const hubProcPill = hubHeader.querySelector(".proj-proc-pill");
        const hubCounters = hubHeader.querySelector(".proj-tree-counters.proj-tree-badges-row");
        const hubQuickBtns = hubHeader.querySelector(".proj-tree-quick-btns");
        const hubHeadLeft = hubHeader.querySelector(".proj-tree-head-left");
        const hubHeadRight = hubHeader.querySelector(".proj-tree-head-right");

        check(hubGroup.classList.contains("has-badges") && hubHeader.classList.contains("has-badges"), "Expected has-badges on group and header");
        check(hubSelfBadge && hubSelfBadge.textContent.trim() === "⚙️", "Self-hub badge must be compact ⚙️");
        check(hubHeadLeft.contains(hubSelfBadge) && hubSharedBadge.nextElementSibling === hubSelfBadge,
          "Self-hub badge must be inside .proj-tree-head-left right after shared badge");
        check(hubProcPill && !hubHeadRight.contains(hubProcPill) && hubProcPill.nextElementSibling === hubHeadRight,
          "proj-proc-pill must precede .proj-tree-head-right directly inside .proj-tree-head-top");
        check(hubHeadRight.contains(hubCounters), "proj-tree-counters must live inside .proj-tree-head-right");

        // Priority order & max 2 pills + +N counter
        const counterChildren = [...hubCounters.children];
        check(counterChildren.length === 3, "Expected 2 status pills + +N counter, got " + counterChildren.length);
        check(counterChildren[0].classList.contains("proj-count-err"), "1st priority pill must be err");
        check(counterChildren[1].classList.contains("proj-count-running"), "2nd priority pill must be running");
        check(counterChildren[2].classList.contains("proj-count-more") && counterChildren[2].textContent.trim() === "+3",
          "3rd item must be +3 overflow badge, got " + counterChildren[2]?.textContent);
        const countersTitle = hubCounters.getAttribute("title") || "";
        check(countersTitle.includes("Ошибки: 1") && countersTitle.includes("В работе: 1") &&
          countersTitle.includes("Готово: 1") && countersTitle.includes("В очереди: 2") &&
          countersTitle.includes("Остановлено: 1"), "Full summary missing in title: " + countersTitle);

        for (const theme of ["dark", "light"]) {
          stage = width + "x" + height + "/" + theme;
          d.documentElement.dataset.theme = theme;
          const hr = rect(hubHeader);
          check(hr.height <= 34, "proj-tree-header.has-badges height expected <= 34px, got " + hr.height);
          const headerCenterY = hr.top + hr.height / 2;
          for (const [label, el] of [
            ["name", hubName],
            ["selfBadge", hubSelfBadge],
            ["procPill", hubProcPill],
            ["counters", hubCounters],
            ["quickBtns", hubQuickBtns],
          ]) {
            const r = rect(el);
            check(r.width > 0 && r.height > 0, label + " collapsed");
            const cy = r.top + r.height / 2;
            check(Math.abs(cy - headerCenterY) <= 4.5, label + " wrapped to a second row: cy=" + cy + " vs " + headerCenterY);
          }

          // procPill must not overlap quickBtns / headRight
          const pr = rect(hubProcPill);
          const rr = rect(hubHeadRight);
          check(pr.right <= rr.left + 0.5, "proj-proc-pill overlaps .proj-tree-head-right");

          // Selected project (idle, no hover) still keeps counters visible and quickBtns hidden on desktop
          if (width > 860) {
            check(hubGroup.classList.contains("is-selected-proj"), "Expected hubGroup to be selected project");
            check(w.getComputedStyle(hubCounters).opacity === "1", "Counters should stay visible on selected project without hover");
            check(w.getComputedStyle(hubQuickBtns).opacity === "0", "Quick buttons should stay hidden on selected project without hover");
          } else {
            // Mobile: inline-flex without overlap between counters and quickBtns
            check(rect(hubCounters).right <= rect(hubQuickBtns).left + 0.5, "Mobile counters overlap quick buttons");
          }

          // Adding .menu-open must produce 0.0px shift in .proj-tree-name width
          const idleNameW = rect(hubName).width;
          hubGroup.classList.add("menu-open");
          check(rect(hubName).width === idleNameW, "Project name width shifted on .menu-open: " + idleNameW + " -> " + rect(hubName).width);
          if (width > 860) {
            check(w.getComputedStyle(hubQuickBtns).opacity === "1" && w.getComputedStyle(hubQuickBtns).pointerEvents === "auto",
              "Quick buttons should show on .menu-open");
            check(w.getComputedStyle(hubCounters).opacity === "0" && w.getComputedStyle(hubCounters).pointerEvents === "none",
              "Counters should hide on .menu-open");
          }
          // procPill must remain directly hit-testable even with .menu-open
          const hit = d.elementFromPoint(pr.left + pr.width / 2, pr.top + pr.height / 2);
          check(hit === hubProcPill || hubProcPill.contains(hit), "proj-proc-pill is obscured from clicks");
          hubGroup.classList.remove("menu-open");

          // Expanded project child row must stay shorter than parent header
          const siteGroup = tree.querySelector('.proj-tree-group[data-proj-group="Сайты"]');
          const siteHeader = siteGroup.querySelector(":scope > .proj-tree-header");
          const siteRow = siteGroup.querySelector(".proj-tree-body .nav-item");
          check(rect(siteHeader).height <= 34, "Expanded project header with badge taller than 34px: " + rect(siteHeader).height);
          check(rect(siteRow).height < rect(siteHeader).height, "Child row must be shorter than project header");

          check(sidebar.scrollWidth <= sidebar.clientWidth + 1, "Sidebar horizontal overflow");
          check(tree.scrollWidth <= tree.clientWidth + 1, "Sidebar scroll tree horizontal overflow");
        }

        // Verify procPill click fires listener cleanly
        hubProcPill.addEventListener("click", () => { procClicked = true; }, true);
        hubProcPill.click();
        check(procClicked, "proj-proc-pill click event did not fire");

        // Configure 2 active providers (Google + ChatGPT), keep Claude & OR unconfigured
        api.state.quotas = { gemini_5h_pct: 96, claude_5h_pct: 81 };
        api.state.chatgpt.summary = { has_account: true, is_enabled: true, quotas: { window_5h_remaining_pct: 100, window_7d_remaining_pct: 90 } };
        api.state.claude.summary = { has_account: false };
        api.state.openrouter.summary = { has_key: false };
        get("sidebarChatGptBtn").classList.replace("is-unconfigured", "is-configured");
        get("scgCompactView").classList.add("hidden");
        get("scgCardView").classList.remove("hidden");

        // Reset to auto mode and sync
        w.localStorage.clear();
        api.state.telemetryDockMode = api.loadTelemetryDockMode("test_user");
        check(api.state.telemetryDockMode === "auto", "Default telemetry mode must be auto");
        api.syncMobileTelemetryBar();

        check(bar.getAttribute("data-configured-count") === "2", "Expected data-configured-count=2, got " + bar.getAttribute("data-configured-count"));
        check(get("mobChatGptPillBtn").classList.contains("is-configured"), "mobChatGptPillBtn missing is-configured");
        check(!get("mobClaudePillBtn").classList.contains("is-configured"), "mobClaudePillBtn should not have is-configured");
        check(!get("mobOrPillBtn").classList.contains("is-configured"), "mobOrPillBtn should not have is-configured");

        // Verify legacy key soft-migration: mobile -> expanded, desktop -> auto
        w.localStorage.setItem("agy_hub_mob_telem_exp_mig_u", "1");
        const migrated = api.loadTelemetryDockMode("mig_u");
        check(migrated === (width <= 860 ? "expanded" : "auto"),
          "Legacy migration mismatch at width " + width + ": " + migrated);
        w.localStorage.clear();

        if (scenario === "laptop") {
          check(!footer.classList.contains("is-mob-telemetry-expanded"), "Laptop auto mode must collapse telemetry");
          check(expandBtn.getAttribute("aria-expanded") === "false" && chevron.textContent === "▴", "Collapsed chevron/aria mismatch");
          check(w.getComputedStyle(pod).display === "none", "Telemetry pod must be hidden when collapsed");
          check(w.getComputedStyle(get("mobClaudePillBtn")).display === "none", "Unconfigured Claude pill must be hidden");
          check(w.getComputedStyle(get("mobOrPillBtn")).display === "none", "Unconfigured OR pill must be hidden");
          const barRect2 = rect(bar);
          check(barRect2.height <= 32, "2-provider collapsed telemetry bar must be <= 32px, got " + barRect2.height);
          const qRect = rect(get("mobQuotaPillBtn"));
          const gRect = rect(get("mobChatGptPillBtn"));
          check(Math.abs(qRect.top - gRect.top) <= 1, "2-provider pills must sit on a single row");
          const collapsedTreeHeight = tree.clientHeight;

          // Expand and measure tree height gain (comparing collapsed vs expanded)
          expandBtn.click();
          check(footer.classList.contains("is-mob-telemetry-expanded"), "Click must expand telemetry on laptop");
          check(w.localStorage.getItem("agy_hub_telem_mode_admin") === "expanded", "Expanded choice must persist in localStorage");
          const expandedTreeHeight = tree.clientHeight;
          const heightGain = collapsedTreeHeight - expandedTreeHeight;
          check(heightGain >= 120, "Expected >= 120px tree height gain on laptop, got " + heightGain);

          // Unconfigured rows inside expanded pod still 27px
          check(Math.abs(rect(get("sidebarClaudeBtn")).height - 27) <= 0.5, "Unconfigured Claude row must stay 27px");
          check(Math.abs(rect(get("sidebarOpenRouterBtn")).height - 27) <= 0.5, "Unconfigured OR row must stay 27px");

          // Collapse again and verify 1-provider and 4-provider bar heights
          expandBtn.click();
          check(!footer.classList.contains("is-mob-telemetry-expanded"), "Second click must collapse telemetry");
          check(w.localStorage.getItem("agy_hub_telem_mode_admin") === "collapsed", "Collapsed choice must persist");

          api.state.chatgpt.summary = { has_account: false };
          api.syncMobileTelemetryBar();
          check(bar.getAttribute("data-configured-count") === "1" && rect(bar).height <= 32,
            "1-provider collapsed bar must be <= 32px, got " + rect(bar).height);

          api.state.chatgpt.summary = { has_account: true, is_enabled: true };
          api.state.claude.summary = { has_account: true, is_enabled: true };
          api.state.openrouter.summary = { has_key: true, is_enabled: true };
          api.syncMobileTelemetryBar();
          check(bar.getAttribute("data-configured-count") === "4", "Expected 4 configured providers");
          const barRect4 = rect(bar);
          check(barRect4.height >= 48 && barRect4.height <= 60, "4-provider compact 2-row grid expected ~54px, got " + barRect4.height);

          resolve({ scenario, width, height, barHeight2: barRect2.height, barHeight4: barRect4.height, heightGain });
          return;
        }

        if (scenario === "large") {
          check(footer.classList.contains("is-mob-telemetry-expanded"), "1440x1080 auto mode must expand telemetry");
          check(expandBtn.getAttribute("aria-expanded") === "true" && chevron.textContent === "▾", "Expanded chevron/aria mismatch");
          check(w.getComputedStyle(pod).display !== "none", "Telemetry pod must be visible on 1440x1080");
          const barRect = rect(bar);
          const podRect = rect(pod);
          check(Math.abs(barRect.height - 22) <= 0.5, "Expanded spoiler header height expected 22px, got " + barRect.height);
          check(barRect.bottom <= podRect.top + 1, "Spoiler header must sit above .sidebar-telemetry-pod via order:-1");
          check(w.getComputedStyle(get("mobQuotaPillBtn")).display === "none", "Duplicate pills must hide when expanded");

          // Click collapses and saves mode in localStorage
          expandBtn.click();
          check(!footer.classList.contains("is-mob-telemetry-expanded"), "Click on 1440x1080 must collapse telemetry");
          check(expandBtn.getAttribute("aria-expanded") === "false" && chevron.textContent === "▴", "Chevron must flip to ▴");
          check(w.localStorage.getItem("agy_hub_telem_mode_admin") === "collapsed", "Collapsed mode not saved in localStorage");
          check(rect(bar).height <= 32, "Collapsed bar height expected <= 32px on 1440x1080, got " + rect(bar).height);

          resolve({ scenario, width, height, spoilerHeight: barRect.height });
          return;
        }

        // Mobile 390x844
        check(!footer.classList.contains("is-mob-telemetry-expanded"), "Mobile auto mode must collapse telemetry");
        const mobBarRect = rect(bar);
        check(mobBarRect.height <= 32, "Mobile 2-provider bar must be <= 32px, got " + mobBarRect.height);
        expandBtn.click();
        check(footer.classList.contains("is-mob-telemetry-expanded"), "Mobile click must expand telemetry");
        const utilRect = rect(footer.querySelector(".sidebar-utility-bar"));
        check(utilRect.bottom <= height + 0.5, "Mobile utility bar clipped after expand");
        resolve({ scenario, width, height, mobBarHeight: mobBarRect.height });
      } catch (error) {
        reject(new Error(stage + ": " + error.message));
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
  result.push(await measure(1366, 768, "laptop"));
  result.push(await measure(1440, 1080, "large"));
  result.push(await measure(390, 844, "mobile"));
  document.getElementById("report").textContent = JSON.stringify({ result });
})().catch(error => {
  document.getElementById("report").textContent = JSON.stringify({ error: error.message });
});
"""


def test_sidebar_laptop_ergonomics_static_contracts() -> None:
    ui_css = (STATIC / "ui-system.css").read_text(encoding="utf-8")
    stripped = re.sub(r"/\*[\s\S]*?\*/", "", ui_css)
    important_hits = re.findall(r"!\s*important\b", stripped, flags=re.IGNORECASE)
    assert len(important_hits) == 1, f"Expected strictly 1 !important in ui-system.css, got {len(important_hits)}"

    sw_text = (STATIC / "sw.js").read_text(encoding="utf-8")
    m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_text)
    assert m_sw is not None and int(m_sw.group(1)) >= 170, sw_text[:120]
    ver = m_sw.group(1)
    parser = ShellParser()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    parser.close()
    for asset in ("styles.css", "ui-system.css", "ui-shell.js", "app.js"):
        assert f"/static/{asset}?v={ver}" in parser.assets
        assert f"/static/{asset}?v={ver}" in sw_text


def test_sidebar_laptop_ergonomics_in_chromium(tmp_path) -> None:
    reports = run_workspace_chromium(tmp_path, BROWSER_CHECKS)
    assert [r["scenario"] for r in reports] == ["laptop", "large", "mobile"], reports
    laptop = reports[0]
    assert laptop["barHeight2"] <= 32, laptop
    assert laptop["heightGain"] >= 120, laptop
