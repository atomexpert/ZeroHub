"""Offline API, browser and export contracts for the ZeroHub release center."""
import re

from fastapi.testclient import TestClient

from app import auth, config, main, release_service
from test_frontend_contracts import LINKED_IDS, STATIC, ShellParser
from test_ui_workspace_minimal_v142 import run_workspace_chromium

UPDATE_IDS = set("""
sidebarVersionBadge settingsModalVerBadge
sidebarUpdateBannerBtn updatesSettingsCard updatesCurrentVerBadge updatesSettingsStatusPill
updatesSettingsHint openUpdatesModalBtn quickCheckUpdatesBtn updatesModal
updModalVerBadge updRepoExternalLink updTabsBar updTabSystemBtn
updTabPublishBtn updPaneSystem updCurrentVerText updLatestVerText
updLastCheckedText updCheckBtn updReleaseInfoBox updReleaseStatusText
updFirstPublishBtn updReleaseDetails updReleaseTitle updReleaseDate
updReleaseNotes updApplyBtn updApplyProgressBox updApplyProgressText
updPanePublish pubOwnerInput pubRepoInput pubTokenMaskPill
pubTokenInput pubToggleTokenBtn pubSaveConfigBtn pubVersionInput
pubTitleInput pubNotesTextarea pubFillDraftBtn pubShieldNotice
pubSubmitReleaseBtn pubResultBanner
""".split())


def test_update_assets_and_export():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    parser = ShellParser()
    parser.feed(html)
    assert UPDATE_IDS <= parser.ids.keys()
    assert UPDATE_IDS <= LINKED_IDS
    assert "Users" + chr(92) + "atome" not in html
    assert "Users" + chr(47) + "atome" not in html
    assert "window.confirm(" not in (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "ui-system.css").read_text(encoding="utf-8")
    assert css.count("!important") == 1  # Existing shared hidden utility.
    assert "!important" not in css.split("  .updates-settings-card {", 1)[1].split("  #agyConfirmModal", 1)[0]
    worker = (STATIC / "sw.js").read_text(encoding="utf-8")
    cache = re.search(r'CACHE_NAME = "agy-hub-v(\d+)"', worker)
    assert cache and int(cache[1]) >= 159
    for asset in ("styles.css", "ui-system.css", "ui-shell.js", "app.js"):
        assert f"{asset}?v={cache[1]}" in html and f"{asset}?v={cache[1]}" in worker
    files, _ = release_service._collect_export(config.BASE_DIR, "1.0.0")
    release_service.scan_export_tree_for_leaks(files)


def test_update_http_contract_masks_token(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    token = "ghp_" + "A" * 32 + "abcd"
    monkeypatch.setattr(config, "ENV_PATH", env)
    for key in ("GITHUB_REPO_OWNER", "GITHUB_REPO_NAME", "GITHUB_PUBLISH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(release_service, "build_draft_release_notes", lambda: "- Первый релиз")
    monkeypatch.setattr(release_service, "get_cached_update", lambda: None)
    user = {"role": "admin"}
    client = TestClient(main.app)
    health = client.get("/api/health").json()
    assert health["ok"] and health["version"] == config.get_app_version() and health["boot_id"]
    monkeypatch.setitem(main.app.dependency_overrides, auth.get_current_user, lambda: user)
    saved = client.put("/api/updates/publisher-config", json={
        "owner": "atomexpert", "repo": "ZeroHub", "new_token": token,
    })
    assert saved.status_code == 200 and token not in saved.text
    assert saved.json()["token_mask"].endswith("abcd")
    assert token in env.read_text(encoding="utf-8")
    invalid = client.put("/api/updates/publisher-config", json={"new_token": token})
    assert invalid.status_code == 422 and token not in invalid.text
    status = client.get("/api/updates/status")
    assert status.status_code == 200 and token not in status.text
    assert status.json()["publisher"]["draft_notes"] == "- Первый релиз"
    monkeypatch.setattr(main, "COMPACTING_CHATS", {"fixture"})
    assert client.get("/api/updates/status").json()["active_generations"] == 1
    assert client.post("/api/updates/apply", json={"target_version": "1.0.1"}).status_code == 409
    monkeypatch.setattr(release_service, "apply_one_click_update", lambda target_version: {"updated_version": target_version})
    monkeypatch.setattr(main, "_spawn_detached_server_restart", lambda: None)
    restarted = client.post("/api/updates/apply", json={"target_version": "1.0.1", "force": True}).json()
    assert restarted["restarting"] and restarted["previous_boot_id"] == health["boot_id"]
    user["role"] = "developer"
    assert "publisher" not in client.get("/api/updates/status").json()
    assert client.put("/api/updates/publisher-config", json={}).status_code == 403


BROWSER_CHECKS = r"""
function check(value, message) { if (!value) throw new Error(message); }

async function exercise(width, height) {
  const frame = document.createElement("iframe");
  frame.style.cssText = "position:absolute;border:0;width:" + width + "px;height:" + height + "px";
  const loaded = new Promise(resolve => { frame.onload = resolve; });
  frame.srcdoc = workspacePage;
  document.body.append(frame);
  await loaded;
  try {
    const w = frame.contentWindow, d = frame.contentDocument;
    const api = w.__AGY_ADVISOR_API__;
    const get = id => d.getElementById(id);
    const visible = id => !get(id).classList.contains("hidden");
    const flush = () => new Promise(resolve => setTimeout(resolve, 40));
    const versionBadge = get("sidebarVersionBadge");
    const sidebar = get("sidebar");
    const brand = sidebar.querySelector(".sidebar-brand");
    const brandTitle = brand.querySelector(".brand-title");
    const roleBadge = get("sidebarRoleBadge");
    const closeSidebar = get("closeSidebarBtn");
    const themes = ["light", "dark"];
    const versionIds = ["sidebarVersionBadge", "settingsModalVerBadge", "updatesCurrentVerBadge",
      "updModalVerBadge", "updCurrentVerText"];
    const labelsMatch = label => versionIds.every(id => get(id).textContent === label);
    const cacheKey = "agy_hub_cached_app_ver";
    sidebar.style.transition = "none";
    function sidebarGeometry(label, update) {
      sidebar.classList.add("mobile-open");
      for (const theme of themes) {
        d.documentElement.dataset.theme = theme;
        const bounds = brand.getBoundingClientRect();
        check(bounds.height === 48, "sidebar brand height changed");
        check(versionBadge.textContent === label, "sidebar version differs");
        check(versionBadge.classList.contains("has-update") === update, "sidebar update state differs");
        if (update) {
          const dot = w.getComputedStyle(versionBadge, "::before");
          check(dot.width === "5px" && dot.height === "5px", "update dot is missing");
        }
        const items = [brandTitle, versionBadge, roleBadge];
        if (width <= 860) items.push(closeSidebar);
        for (const item of items) {
          const rect = item.getBoundingClientRect();
          check(rect.width > 0 && rect.height > 0 && rect.left >= bounds.left && rect.right <= bounds.right &&
            rect.top >= bounds.top && rect.bottom <= bounds.bottom,
          "sidebar brand item escaped " + item.id + " at " + width + " " + JSON.stringify({ rect, bounds }));
        }
        check(brandTitle.scrollWidth <= brandTitle.clientWidth + 1, "brand name was truncated");
        check(brand.scrollWidth <= brand.clientWidth && sidebar.scrollWidth <= sidebar.clientWidth &&
          d.documentElement.scrollWidth <= width, "sidebar horizontal overflow at " + width + " in " + theme);
      }
    }
    const requests = [];
    const token = "ghp_" + "z".repeat(36);
    const publisher = { owner: "atomexpert", repo: "ZeroHub", has_token: true, token_mask: "masked-abcd", draft_notes: "- Draft" };
    const first = { current_version: "1.0.0", latest_version: "1.0.0", repo_slug: "atomexpert/ZeroHub", published_yet: false, update_available: false };
    const next = { ...first, latest_version: "1.0.1", published_yet: true, update_available: true,
      release_title: "Title ".repeat(70), release_notes: "- New feature\n\n" +
      "\x60\x60\x60\n" + "x".repeat(250) + "\n\x60\x60\x60\n\x3cscript>alert(1)\x3c/script>" };
    const status = { current_version: "1.0.0", repo_slug: first.repo_slug, latest: first, active_generations: 2, publisher };
    let versionAtPublishRefresh;
    w.fetch = async (url, options) => {
      const data = options?.body && JSON.parse(options.body);
      requests.push([url, data]);
      let response, code = 200;
      if (url === "/api/updates/status") response = status;
      else if (url === "/api/updates/check") {
        if (status.current_version !== "1.0.0") {
          versionAtPublishRefresh = labelsMatch("v" + status.current_version);
        } else {
          status.latest = next;
        }
        response = status.latest;
      }
      else if (url === "/api/updates/publisher-config") {
        check(data.new_token === token, "new token not sent");
        publisher.token_mask = "masked-zzzz";
        response = publisher;
      } else if (url === "/api/updates/apply") {
        code = 409;
        response = { detail: "The requested version is unavailable." };
      } else if (url === "/api/updates/publish") {
        status.current_version = data.version;
        status.latest = { ...status.latest, current_version: data.version, latest_version: data.version, update_available: false };
        response = { version: data.version, tag_name: "v" + data.version,
          html_url: "https://github.com/atomexpert/ZeroHub/releases/tag/v" + data.version };
      } else throw new Error("Unexpected fetch " + url);
      return new Response(JSON.stringify(response), { status: code, headers: { "Content-Type": "application/json" } });
    };
    api.state.token = "fixture";
    api.state.currentUser = { role: "admin" };
    api.syncAppVersionBadges(" v2.3.4 ");
    api.syncAppVersionBadges("invalid");
    check(labelsMatch("v2.3.4") && w.localStorage.getItem(cacheKey) === "2.3.4", "version normalization or cache failed");
    api.setupMarkdown();
    api.bindUpdatesEvents();
    await api.loadUpdatesStatus();
    check(labelsMatch("v1.0.0"), "initial installed version differs across badges");
    sidebarGeometry("v1.0.0", false);
    const settings = get("settingsModal");
    settings.classList.remove("hidden");
    settings.querySelector(".modal-card").style.animation = "none";
    for (const theme of themes) {
      d.documentElement.dataset.theme = theme;
      const badge = get("settingsModalVerBadge").getBoundingClientRect();
      const header = settings.querySelector(".modal-header").getBoundingClientRect();
      check(badge.width > 0 && badge.left >= header.left && badge.right <= header.right,
        "settings version is outside the header");
    }
    check(!visible("sidebarUpdateBannerBtn"), "pre-release sidebar indicator is visible");
    check(get("pubVersionInput").value === "1.0.0", "first release version");
    get("openUpdatesModalBtn").click();
    await flush();
    check(visible("updFirstPublishBtn"), "first release call to action is missing");
    get("updCheckBtn").click();
    get("quickCheckUpdatesBtn").click();
    await flush();
    check(requests.filter(([url]) => url === "/api/updates/check").length === 1, "duplicate check ran");
    check(visible("sidebarUpdateBannerBtn") && visible("updApplyBtn"), "update actions missing");
    sidebarGeometry("v1.0.0", true);
    check(versionBadge.title.includes("v1.0.0") && versionBadge.title.includes("v1.0.1"), "update tooltip differs");
    get("updatesModal").classList.add("hidden");
    versionBadge.click();
    check(visible("updatesModal"), "sidebar version did not open the release center");
    if (width <= 860) check(!sidebar.classList.contains("mobile-open"), "mobile sidebar stayed open");
    await flush();
    check(get("pubVersionInput").value === "1.0.2", "published latest patch suggestion is wrong");
    const notes = get("updReleaseNotes");
    check(notes.querySelectorAll("li").length === 1 && notes.querySelector("pre"), "release notes lost formatting");
    check(!notes.querySelector("script, img, a"), "release notes inserted interactive content");
    const card = get("updatesModal").querySelector(".modal-card");
    card.style.animation = "none";
    function geometry() {
      const rect = card.getBoundingClientRect();
      check(rect.left >= -1 && rect.right <= width + 1 && rect.top >= -1 && rect.bottom <= height + 1, "modal escaped " + width + "x" + height + " " + JSON.stringify(rect));
      check(d.documentElement.scrollWidth <= width, "document horizontal overflow");
      const body = card.querySelector(".modal-body");
      check(body.scrollWidth <= body.clientWidth + 1, "modal body horizontal overflow");
    }
    for (const theme of themes) { d.documentElement.dataset.theme = theme; geometry(); }

    get("updApplyBtn").click();
    await flush();
    check(visible("agyConfirmModal"), "confirmation did not open");
    get("agyConfirmCancelBtn").click();
    await flush();
    check(!requests.some(([url]) => url === "/api/updates/apply"), "cancel still applied update");
    get("updApplyBtn").click();
    await flush();
    get("agyConfirmOkBtn").click();
    await flush();
    const apply = requests.find(([url]) => url === "/api/updates/apply")?.[1];
    check(apply?.force && apply.target_version === "1.0.1", "forced update payload is wrong");

    get("updTabPublishBtn").click();
    check(visible("updPanePublish") && !visible("updPaneSystem"), "publisher tab did not switch");
    for (const theme of themes) { d.documentElement.dataset.theme = theme; geometry(); }
    get("pubTokenInput").value = token;
    get("pubToggleTokenBtn").click();
    check(get("pubTokenInput").type === "text", "token reveal did not work");
    get("pubSaveConfigBtn").click();
    await flush();
    check(get("pubTokenInput").value === "" && get("pubTokenInput").type === "password", "token remained in the input");
    check(get("pubTokenMaskPill").textContent.endsWith("masked-zzzz"), "saved mask is missing");
    get("pubNotesTextarea").value = "Manual";
    get("pubFillDraftBtn").click();
    await flush();
    get("agyConfirmCancelBtn").click();
    await flush();
    check(get("pubNotesTextarea").value === "Manual", "draft overwrote manual text");
    get("pubSubmitReleaseBtn").click();
    await flush();
    get("agyConfirmOkBtn").click();
    await flush();
    check(get("pubResultBanner").querySelector("a")?.origin === "https://github.com", "release link missing");
    check(requests.find(([url]) => url === "/api/updates/publish")?.[1]?.release_notes === "Manual", "publish payload differs");
    check(status.current_version === "1.0.2" && versionAtPublishRefresh && labelsMatch("v1.0.2") &&
      w.localStorage.getItem(cacheKey) === "1.0.2", "published version was not shown and cached immediately");
    check(!versionBadge.title.includes("v1.0.1"), "published tooltip still advertises an older release");

    api.state.currentUser = { role: "developer" };
    roleBadge.textContent = "Пользователь с длинным именем";
    get("wsDot").classList.replace("online", "offline");
    get("wsStatusText").textContent = "Переподключение…";
    sidebarGeometry("v1.0.2", false);
    get("updatesModal").classList.add("hidden");
    versionBadge.click();
    await flush();
    check(visible("updatesModal") && !visible("updTabPublishBtn") && !visible("updPanePublish") &&
      !visible("updApplyBtn") && !visible("updCheckBtn"), "developer release center exposes admin controls");
    check(labelsMatch("v1.0.2") && visible("updReleaseDetails"), "developer release information is missing");

    if (width === 1440) {
      let healthCalls = 0;
      w.fetch = async url => {
        check(url === "/api/health", "poll queried a different endpoint");
        return new Response(JSON.stringify({ok: true, version: "1.0.1",
          boot_id: ++healthCalls === 1 ? "old-process" : "new-process"}));
      };
      await api.waitForUpdatedServer("1.0.1", "old-process");
      check(healthCalls === 2, "restart poll accepted the old process");
    }
    return [width, height];
  } finally {
    frame.remove();
  }
}

(async () => {
  const result = [];
  for (const [width, height] of [[1440, 900], [390, 844], [320, 568]]) result.push(await exercise(width, height));
  document.getElementById("report").textContent = JSON.stringify({ result });
})().catch(error => {
  document.getElementById("report").textContent = JSON.stringify({ error: error.message });
});
"""


def test_release_center_in_chromium(tmp_path):
    assert run_workspace_chromium(tmp_path, BROWSER_CHECKS, vendor_scripts=("vendor/marked.min.js",)) == [
        [1440, 900], [390, 844], [320, 568],
    ]
