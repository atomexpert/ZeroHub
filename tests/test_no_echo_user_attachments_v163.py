"""Keep user attachments inline while preserving generated media previews."""
from app.agy_engine import _augment_prompt_for_mode
from test_ui_workspace_minimal_v142 import run_workspace_chromium


def test_attachment_prompt_asks_the_model_not_to_echo_uploads(tmp_path):
    screenshot = tmp_path / "screenshot.png"
    prompt = _augment_prompt_for_mode(
        kind="chat",
        prompt="Опиши изображение.",
        attachments=[{"disk_path": str(screenshot), "name": screenshot.name}],
        is_first_turn=False,
    )
    assert (
        "Прикреплённые пользователем файлы (прочитай/просмотри их через read_file; "
        "не дублируй картинки и file://-ссылки на эти временные вложения в своём ответе):"
    ) in prompt
    assert f"- {screenshot} (screenshot.png)" in prompt
    assert prompt.endswith("Опиши изображение.")


BROWSER_CHECKS = r"""
function check(ok, message) { if (!ok) throw new Error(message); }

(async () => {
  const frame = document.createElement("iframe");
  const ready = new Promise(resolve => { frame.onload = resolve; });
  frame.srcdoc = workspacePage;
  document.body.append(frame);
  await ready;
  const d = frame.contentDocument;
  const api = frame.contentWindow.__AGY_ADVISOR_API__;
  api.setupMarkdown();

  function render(markdown) {
    const template = d.createElement("template");
    template.innerHTML = api.renderMarkdownSafe(markdown);
    return template.content;
  }

  const hub = "file:///D:/Internet/Antigravity/Antigravity_Hub/";
  const uploads = render([
    "[image.png](" + hub + "users/admin/uploads/cd94218c-9d09/ec355031_image.png)",
    "![скрин](" + hub + "shared_projects/_uploads/chat1/a.png)",
    "[b.png](/api/uploads/chat1/b.png?x=1#view)",
    "![аудио](/data/sandbox_uploads/chat1/Voice%20Note.mp3)",
  ].join("\n\n"));
  check(!uploads.querySelector(".gen-media-auto-gallery, .gen-media-figure, .gen-media-audio-card"), "uploads have media cards");
  const links = uploads.querySelectorAll("a.hub-media-link");
  check(links.length === 4, "upload links are missing");
  check([...links].slice(0, 3).every(link => link.dataset.lightboxSrc && link.dataset.lightboxDl), "image lightbox was lost");
  check(links[2].getAttribute("href").startsWith("/api/uploads/chat1/b.png"), "upload route changed");
  check(links[3].getAttribute("href").startsWith("/api/media?"), "POSIX upload path is not proxied");
  const audioPath = new URLSearchParams(links[3].getAttribute("href").split("?")[1]).get("path");
  check(audioPath === "/data/sandbox_uploads/chat1/Voice Note.mp3", "encoded POSIX upload path was double encoded");
  check(links[3].getAttribute("download") === "Voice Note.mp3", "audio download filename changed");
  check(!render("/api/uploads/chat1/b.png").querySelector(".gen-media-auto-gallery, .gen-media-figure"), "bare upload path created a card");

  for (const path of [
    String.raw`d:\Hub\USERS\ADMIN\UPLOADS\chat\One.PNG?view=1#preview`,
    "/data/uploads/c/b.png",
    "/_uploads/c/c.png",
    "/sandbox_uploads/c/d.png",
    "/api/uploads/c/e.png",
  ]) check(api.isUserChatUploadMediaRef(null, path), "upload path was missed: " + path);

  const custom = "file:///D:/Custom%20Staging/Attachment.PNG";
  api.state.messages = [
    {role: "user", attachments: [{disk_path: "D:\\Custom Staging\\Attachment.PNG"}]},
    {role: "assistant", attachments: [{disk_path: "D:/artifacts/assistant.png"}]},
  ];
  api.state.pendingAttachments = [{url: "/incoming/c/Pending.PNG?draft=1"}];
  check(api.isUserChatUploadMediaRef(null, custom + "?raw=1#ref"), "user attachment metadata did not match normalized path");
  check(api.isUserChatUploadMediaRef(null, "/incoming/c/pending.png#preview"), "pending attachment URL did not match");
  check(!api.isUserChatUploadMediaRef(null, "file:///D:/artifacts/assistant.png"), "assistant metadata suppressed its own artifact");
  const metadata = render("[Reference](" + custom + ")\n\n![Pending](/incoming/c/Pending.PNG)");
  check(metadata.querySelectorAll("a.hub-media-link").length === 2 && !metadata.querySelector("img"), "metadata-only uploads were echoed");

  const project = hub + "users/admin/projects/Demo/plot.png";
  const brain = hub + "antigravity-cli/brain/11111111-2222/result.png";
  const generated = render([
    "[plot](" + project + ")",
    "[result](" + brain + ")",
    "![workspace chart](/workspace/plot.png)",
    "[video](/workspace/demo.mp4)",
    "[audio](/workspace/voice.mp3)",
    "[attachment](/api/uploads/chat1/b.png)",
  ].join("\n\n"));
  const gallery = generated.querySelector(".gen-media-auto-gallery");
  check(gallery?.querySelectorAll(".gen-media-img").length === 2, "local generated image links lost previews or are duplicated");
  check(generated.querySelectorAll(".gen-media-img").length === 3, "explicit generated image lost its inline card");
  check(gallery.querySelector(".gen-media-video") && gallery.querySelector(".gen-media-audio"), "generated video or audio is missing");
  check(!gallery.innerHTML.includes("b.png"), "user attachment leaked into a mixed gallery");
  check(generated.querySelector('a.hub-media-link[data-lightbox-src^="/api/uploads/"]'), "mixed reply lost the upload link");
  document.getElementById("report").textContent = JSON.stringify({result: "ok"});
})().catch(error => {
  document.getElementById("report").textContent = JSON.stringify({error: error.message});
});
"""


def test_chromium_only_generates_cards_for_artifacts(tmp_path):
    assert run_workspace_chromium(tmp_path, BROWSER_CHECKS, vendor_scripts=("vendor/marked.min.js",)) == "ok"
