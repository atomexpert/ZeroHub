"""Offline Chromium coverage for the four settings tabs and their shortcuts."""
from test_frontend_contracts import STATIC
from test_ui_workspace_minimal_v142 import run_workspace_chromium


BROWSER_CHECKS = r"""
function check(ok, message) { if (!ok) throw new Error(message); }

async function measure(width, height) {
  const frame = document.createElement("iframe");
  frame.style.cssText = "position:absolute;border:0;width:" + width + "px;height:" + height + "px";
  const ready = new Promise(resolve => { frame.onload = resolve; });
  frame.srcdoc = workspacePage;
  document.body.append(frame);
  await ready;
  let profile = width + "x" + height;
  try {
    const w = frame.contentWindow, d = frame.contentDocument;
    const api = w.__AGY_ADVISOR_API__;
    const get = id => d.getElementById(id);
    const settings = get("settingsModal");
    const card = settings.querySelector(".settings-modal-card");
    const bar = get("settingsTabsBar");
    const body = settings.querySelector(".modal-body");
    const keys = ["Appearance", "Ai", "Server", "Account"];
    const visible = node => !node.classList.contains("hidden");
    const tab = key => get("settingsTab" + key + "Btn");
    const pane = key => get("settingsPane" + key);
    const press = (key, keyName) => tab(keyName).dispatchEvent(new w.KeyboardEvent(
      "keydown", { key, bubbles: true, cancelable: true },
    ));
    const near = (a, b) => Math.abs(a - b) <= 1;
    const flush = () => new Promise(resolve => setTimeout(resolve, 25));

    const fixtureUrl = "http://127.0.0.1:8765/";
    const calls = [];
    w.fetch = async url => {
      calls.push(url);
      if (url === "/api/system/info") {
        return new Response(JSON.stringify({network: {localhost_url: fixtureUrl}}),
          {headers: {"Content-Type": "application/json"}});
      }
      if (url.startsWith("/api/system/qr?")) return new Response('<svg viewBox="0 0 2 2"></svg>');
      if (url.startsWith("/api/quotas?")) {
        return new Response(JSON.stringify({quotas: {gemini_5h_pct: 65, claude_5h_pct: 34}}),
          {headers: {"Content-Type": "application/json"}});
      }
      return new Response("{}", {headers: {"Content-Type": "application/json"}});
    };
    api.state.token = "offline-fixture";
    api.state.currentUser = {username: "owner", display_name: "Fixture Owner", role: "admin"};
    api.bindSettingsTabs();
    api.applyRoleUI();
    card.style.animation = "none";
    settings.classList.remove("hidden");
    get("updatesSettingsCard").classList.remove("hidden");
    get("quickGdriveSyncBtn").classList.remove("hidden");
    check(bar.parentElement === card && bar.previousElementSibling.classList.contains("modal-header") &&
      bar.nextElementSibling === body, "tabs must sit outside the scroller");
    check(get("paletteGrid").querySelectorAll("[data-palette]").length === 8, "palette options lost");
    check(get("userRulesTextarea").rows === 4, "AI rules field height");
    check(get("settingsProvidersList").querySelectorAll(":scope > .settings-or-card").length === 4, "provider rows");

    function selected(key) {
      check(api.state.settingsActiveTab === key.toLowerCase(), "state differs for " + key);
      for (const candidate of keys) {
        const active = candidate === key;
        check(visible(pane(candidate)) === active && tab(candidate).classList.contains("active") === active
          && tab(candidate).getAttribute("aria-selected") === String(active) && tab(candidate).tabIndex === (active ? 0 : -1),
          "visibility or ARIA wrong for " + candidate + " (expected " + key + ")");
      }
    }
    for (const theme of ["dark", "light"]) {
      profile = width + "x" + height + " " + theme;
      d.documentElement.dataset.theme = theme;
      for (const key of keys) {
        tab(key).click();
        selected(key);
        check(body.scrollTop === 0, "scroll position not reset");
        check(body.scrollWidth <= body.clientWidth + 1, key + " scroll body overflow");
        check(pane(key).scrollWidth <= pane(key).clientWidth + 1, key + " panel overflow");
        check(d.documentElement.scrollWidth <= w.innerWidth, key + " document overflow");
        if (key === "Server") {
          check(get("updatesSettingsCard").getBoundingClientRect().top >= body.getBoundingClientRect().top
            && get("gdriveSettingsCard").getBoundingClientRect().bottom <= body.getBoundingClientRect().bottom + 1,
            "GitHub or Google Drive requires scrolling");
          const top = bar.getBoundingClientRect().top;
          body.scrollTop = body.scrollHeight;
          if (width <= 560) check(body.scrollTop > 0, "server panel did not scroll");
          check(near(top, bar.getBoundingClientRect().top), "tabs moved with content");
        }
      }
      tab("Appearance").click();
      const boxes = keys.map(key => tab(key).getBoundingClientRect());
      for (const [i, box] of boxes.entries()) {
        check(box.width > 0 && box.height >= 36 && box.left >= bar.getBoundingClientRect().left &&
          box.right <= bar.getBoundingClientRect().right, "tab bounds " + keys[i]);
        check(tab(keys[i]).scrollWidth <= tab(keys[i]).clientWidth + 1, "clipped tab " + keys[i]);
      }
      if (width <= 560) {
        check(near(boxes[0].top, boxes[1].top) && near(boxes[2].top, boxes[3].top) &&
          boxes[0].bottom <= boxes[2].top && near(boxes[0].left, boxes[2].left), "admin tabs are not 2x2");
      } else {
        check(boxes.every(box => near(box.top, boxes[0].top)), "desktop tabs are not one row");
      }
    }

    press("ArrowRight", "Appearance"); selected("Ai");
    check(d.activeElement === tab("Ai"), "ArrowRight did not move focus");
    press("ArrowLeft", "Ai"); selected("Appearance");
    press("End", "Appearance"); selected("Account");
    press("Home", "Account"); selected("Appearance");
    press("ArrowLeft", "Appearance"); selected("Account");
    check(w.localStorage.getItem("agy_hub_settings_tab") === "account", "tab preference missing");
    check(get("settingsProfileIdentity").textContent.includes("Fixture Owner · @owner"), "profile identity");
    get("openQrFromSettingsBtn").click();
    await flush();
    check(!visible(settings) && visible(get("qrModal")) && get("qrUrlText").value === fixtureUrl, "QR shortcut failed");
    check(calls.some(url => url.startsWith("/api/system/qr?")), "QR was not generated");
    get("qrModal").classList.add("hidden");
    settings.classList.remove("hidden");
    api.switchSettingsTab("ai");
    get("openQuotaFromSettingsBtn").click();
    await flush();
    check(!visible(settings) && visible(get("quotaModal")), "quota shortcut failed");
    check(get("settingsGoogleSubHint").textContent.includes("Gem 65% · Cld 34%"), "live Google quota hint");
    get("quotaModal").classList.add("hidden");

    settings.classList.remove("hidden");
    api.switchSettingsTab("server");
    api.state.currentUser = {username: "dev", display_name: "Developer", role: "developer"};
    api.applyRoleUI();
    selected("Appearance");
    check(!visible(tab("Server")) && !visible(pane("Server")), "server section visible for developer");
    api.switchSettingsTab("server");
    selected("Appearance");
    api.switchSettingsTab("unknown");
    selected("Appearance");
    press("ArrowRight", "Ai");
    selected("Account");
    const columns = w.getComputedStyle(bar).gridTemplateColumns.trim().split(/\s+/).length;
    check(columns === (width < 380 ? 2 : 3), "wrong guest tab grid");
    check(get("settingsProfileIdentity").textContent.includes("@dev") && get("settingsProfileRoleBadge").classList.contains("is-muted"),
      "developer profile");
    w.localStorage.removeItem("agy_hub_settings_tab");
    return [width, height];
  } catch (error) {
    throw new Error(profile + ": " + error.message);
  } finally {
    frame.remove();
  }
}

(async () => {
  const result = [];
  for (const [width, height] of [[1440, 900], [390, 844], [320, 568]]) {
    result.push(await measure(width, height));
  }
  document.getElementById("report").textContent = JSON.stringify({result});
})().catch(error => {
  document.getElementById("report").textContent = JSON.stringify({error: error.message});
});
"""


def test_settings_tabs_on_desktop_and_mobile(tmp_path):
    assert run_workspace_chromium(tmp_path, BROWSER_CHECKS) == [
        [1440, 900], [390, 844], [320, 568],
    ]
    assert (STATIC / "ui-system.css").read_text(encoding="utf-8").count("!important") == 1
