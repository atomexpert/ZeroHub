"""Isolated HTTP, prompt and browser coverage for chat file delivery."""
import os
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app import agy_engine, auth, claude_engine, codex_engine, config, database, main
from test_ui_workspace_minimal_v142 import run_workspace_chromium


def test_media_delivery_headers_and_revalidation(tmp_path, monkeypatch):
    users = tmp_path / "users"
    project = users / "developer" / "projects" / "ZeroNote"
    project.mkdir(parents=True)
    for key, path in {
        "BASE_DIR": tmp_path,
        "DATA_DIR": tmp_path / "data",
        "CERTS_DIR": tmp_path / "data" / "certs",
        "SESSIONS_DIR": tmp_path / "data" / "sessions",
        "CLI_HOMES_DIR": tmp_path / "data" / "cli_homes",
        "AGY_CLI_DATA_DIR": tmp_path / "agy_cli",
        "AGY_APP_DATA_DIR": tmp_path / "agy_app",
        "CHAT_SANDBOX_DIR": tmp_path / "sandbox",
        "SHARED_PROJECTS_DIR": tmp_path / "shared",
        "PROJECTS_ROOT": tmp_path / "projects",
    }.items():
        monkeypatch.setattr(config, key, path)
    monkeypatch.setattr(config, "get_effective_users_dir", lambda: users)
    monkeypatch.setattr(config, "get_effective_legacy_uploads_dir", lambda: tmp_path / "data" / "uploads")
    user = {"id": "dev-id", "username": "developer", "role": "developer"}
    monkeypatch.setattr(auth, "resolve_token_user", AsyncMock(return_value=user))
    monkeypatch.setattr(database, "list_shared_projects", AsyncMock(return_value=[]))

    apk = project / "ZeroNote.apk"
    apk.write_bytes(b"version one")
    pdf = project / "report.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    picture = project / "plot.png"
    picture.write_bytes(b"png fixture")
    archive = project / "Сборка APK.7z"
    archive.write_bytes(b"archive fixture")
    secret = project / ".env"
    secret.write_text("FIXTURE=1", encoding="utf-8")
    foreign = users / "another" / "projects" / "other.apk"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"private fixture")

    client = TestClient(main.app)

    def fetch(path, download=0, **headers):
        return client.get(
            "/api/media",
            params={"path": str(path), "download": download, "token": "fixture-token"},
            headers=headers,
        )

    for download in (0, 1):
        response = fetch(apk, download)
        assert response.status_code == 200 and response.content == b"version one"
        assert response.headers["content-type"] == "application/vnd.android.package-archive"
        assert response.headers["content-disposition"] == 'attachment; filename="ZeroNote.apk"'
        assert response.headers["cache-control"] == "private, no-cache"

    saved_etag = response.headers["etag"]
    first_mtime = apk.stat().st_mtime_ns
    apk.write_bytes(b"version two")  # Same size, different build.
    os.utime(apk, ns=(first_mtime, first_mtime + 2_000_000_000))
    rebuilt = fetch(apk, **{"If-None-Match": saved_etag})
    assert rebuilt.status_code == 200 and rebuilt.content == b"version two"
    assert rebuilt.headers["etag"] != saved_etag

    preview = fetch(picture)
    cached = fetch(picture, **{"If-None-Match": "W/" + preview.headers["etag"]})
    assert cached.status_code == 304 and not cached.content
    assert cached.headers["etag"] == preview.headers["etag"]
    assert cached.headers["cache-control"] == "private, no-cache"

    for download, disposition in ((0, "inline"), (1, "attachment")):
        response = fetch(pdf, download)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"] == f'{disposition}; filename="report.pdf"'
    response = fetch(archive)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-7z-compressed"
    assert response.headers["content-disposition"].startswith("attachment; filename*=utf-8''")
    assert response.headers["content-disposition"].endswith("%20APK.7z")
    for forbidden in (secret, foreign):
        assert fetch(forbidden, **{"If-None-Match": "*"}).status_code == 403


def test_shared_file_delivery_instruction(tmp_path):
    augment = agy_engine._augment_prompt_for_mode
    assert claude_engine._augment_prompt_for_mode is augment
    assert codex_engine._augment_prompt_for_mode is augment
    instruction = agy_engine.HUB_FILE_DELIVERY_INSTRUCTION
    assert "[ИмяФайла.ext](file:///" in instruction and "![Краткая подпись](file:///" in instruction

    common = dict(prompt="Спасибо", attachments=[], project_dir=tmp_path)
    for kind, access_mode, first_turn in (
        ("chat", "read_only", True),
        ("project", "read_only", True),
        ("project", "write_files", False),
        ("project", "full_access", False),
    ):
        mode = dict(kind=kind, access_mode=access_mode, is_first_turn=first_turn)
        assert augment(**common, **mode).count(instruction) == 1
        assert instruction not in augment(**common, **mode, live_voice=True)
    assert augment(**common, kind="chat", is_first_turn=False) == "Спасибо"


BROWSER_CHECKS = r"""
function check(condition, message) { if (!condition) throw new Error(message); }

async function exercise(width, height, theme) {
  const frame = document.createElement("iframe");
  frame.style.cssText = "position:absolute;border:0;width:" + width + "px;height:" + height + "px";
  const loaded = new Promise(resolve => { frame.onload = resolve; });
  frame.srcdoc = workspacePage;
  document.body.append(frame);
  await loaded;

  try {
    const w = frame.contentWindow;
    const d = frame.contentDocument;
    d.documentElement.dataset.theme = theme;
    const api = w.__AGY_ADVISOR_API__;
    api.setupMarkdown();
    const messages = d.getElementById("messagesList");
    let body;
    const path = "file:///D:/Projects/ZeroNote/";
    function render(markdown) {
      messages.replaceChildren(api.buildMessageDom({
        id: "fixture", role: "assistant", status: "completed", content: markdown,
      }));
      body = messages.querySelector(".markdown-body");
    }
    function query(selector) { return body.querySelector(selector); }
    function count(selector) { return body.querySelectorAll(selector).length; }
    function params(link) { return new URLSearchParams(link.getAttribute("href").split("?")[1]); }

    const files = [
      ["ZeroNote.apk", "ZeroNote.apk"],
      ["app/build/ZeroNote.apk", "ZeroNote.apk"],
      ["target/release/zeronote.exe", "zeronote.exe"],
      ["report.pdf", "report.pdf"],
    ];
    render(files.map(([relative, name]) => "[" + name + "](" + path + relative + ")").join("\n\n"));
    const links = body.querySelectorAll("a.hub-media-link");
    check(links.length === files.length, "missing inline download links");
    files.forEach(([relative, name], i) => {
      const link = links[i];
      check(link.download === name && params(link).get("download") === "1", "incorrect download for " + relative);
      check(params(link).get("path") === "D:/Projects/ZeroNote/" + relative, "incorrect target for " + relative);
    });
    check(!query(".gen-file-card, .gen-media-auto-gallery"), "plain links created cards or a gallery");

    render([
      "![Экспорт](" + path + "dump.json)",
      "[ZeroNote.apk](" + path + "ZeroNote.apk)",
      "![ZeroNote APK](" + path + "ZeroNote.apk)",
      "![Повтор APK](" + path + "ZeroNote%2Eapk)",
    ].join("\n\n"));
    check(count("p .gen-file-card") === 2 && !query("img, .gen-media-auto-gallery"), "explicit cards missing or duplicated");
    check(query(".gen-file-sub").textContent === "Экспорт", "explicit card subtitle");
    check(query(".gen-file-dl-btn").download === "dump.json", "JSON download name");
    const apk = body.querySelectorAll(".gen-file-card")[1];
    check(apk.querySelector(".gen-file-ext").textContent === "APK", "APK badge");
    check(apk.querySelector(".gen-file-sub").textContent === "ZeroNote APK", "APK caption");
    const repeated = body.querySelectorAll("a.hub-media-link")[1];
    check(repeated?.textContent === "Повтор APK" && repeated.download === "ZeroNote.apk"
      && params(repeated).get("download") === "1", "repeated attachment should be a download link");

    render('![report.pdf](' + path + 'Quarter%20One/report.pdf "Отчёт за квартал")');
    check(query("p .gen-file-card") && !query(".gen-media-auto-gallery"), "PDF card should render in place");
    const open = query(".gen-file-open-btn");
    const download = query(".gen-file-dl-btn");
    check(open?.target === "_blank" && !params(open).has("download"), "PDF inline open");
    check(download?.download === "report.pdf" && params(download).get("download") === "1", "PDF forced download");
    check(params(open).get("path") === "D:/Projects/ZeroNote/Quarter One/report.pdf", "PDF path with spaces");
    check(query(".gen-file-sub").textContent === "Отчёт за квартал", "Markdown title subtitle");

    render("[main.py](" + path + "main.py#L10-L20)\n\n[app.js](" + path + "app.js)\n\n[pdf anchor](" + path + "report.pdf#L10-L20)");
    check(!query(".gen-file-card") && count(".hub-file-link") === 3, "source or line anchor became a card");
    check(query(".hub-file-link").getAttribute("href").endsWith("#L10-L20"), "line anchor lost");

    api.state.pendingAttachments = [{disk_path: "D:/Projects/ZeroNote/staged.zip"}];
    render([
      "[upload](file:///D:/Hub/users/admin/uploads/chat/sample.apk)",
      "![upload](/api/uploads/chat/sample.pdf)",
      "[archive](/api/uploads/chat/sample.zip)",
      "[staged](" + path + "staged.zip)",
    ].join("\n\n"));
    check(!query(".gen-file-card, .gen-media-auto-gallery, img") && count(".hub-media-link") === 4, "upload echoed in gallery");
    check(query('[href^="/api/uploads/chat/sample.pdf"]').download === "sample.pdf", "upload download route");

    render("![Внешнее изображение](data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7)");
    check(query("img.gen-media-img") && !query(".gen-file-card"), "external image changed");

    const longName = "ОченьДлинноеИмяОтчёта".repeat(8) + ".pdf";
    render("![" + longName + "](" + path + longName + ")");
    const card = query(".gen-file-card");
    const cardRect = card.getBoundingClientRect();
    check(cardRect.width > 0 && cardRect.left >= 0 && cardRect.right <= width, "card left viewport");
    check(d.documentElement.scrollWidth <= width && body.scrollWidth <= body.clientWidth &&
          card.scrollWidth <= card.clientWidth, "horizontal overflow");
    for (const button of card.querySelectorAll(".gen-file-dl-btn, .gen-file-open-btn")) {
      const rect = button.getBoundingClientRect();
      check(rect.height >= 32 && rect.right <= cardRect.right, "PDF actions do not fit");
    }
    check(w.getComputedStyle(query(".gen-file-name")).textDecorationLine === "none", "name is underlined");
    return [width, height, theme];
  } finally {
    frame.remove();
  }
}

(async () => {
  const result = [];
  for (const [width, height] of [[1440, 900], [390, 844], [320, 568]]) {
    for (const theme of ["dark", "light"]) {
      try {
        result.push(await exercise(width, height, theme));
      } catch (error) {
        throw new Error(width + "x" + height + " " + theme + ": " + error.message);
      }
    }
  }
  document.getElementById("report").textContent = JSON.stringify({result});
})().catch(error => {
  document.getElementById("report").textContent = JSON.stringify({error: error.message});
});
"""


def test_file_delivery_cards_in_chromium(tmp_path):
    assert run_workspace_chromium(
        tmp_path, BROWSER_CHECKS, vendor_scripts=("vendor/marked.min.js",),
    ) == [
        [width, height, theme]
        for width, height in ((1440, 900), (390, 844), (320, 568))
        for theme in ("dark", "light")
    ]
