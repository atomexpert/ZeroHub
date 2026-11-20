
"""Offline release and update coverage using temporary repositories and SQLite."""
import ast
import json
import os
import sqlite3
import subprocess
import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, config, env_service, main, openrouter_service
from app import release_service as release

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "ghp_" + "a" * 36
FINE_TOKEN = "github_" + "pat_" + "b" * 30


def git(root, *args):
    return release._git(root, *args, isolated=True).stdout.decode().strip()


def commit(root, message, *paths):
    git(root, "add", "--", *paths)
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", message)


def test_semver_check_and_cache(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.setattr(config, "get_app_version", lambda: "1.0.0")
    assert release.parse_semver("v1.0.2") == (1, 0, 2)
    assert release.is_newer_version("v1.0.2", "1.0.10")
    with pytest.raises(ValueError):
        release.parse_semver("1.0.2-beta")
    calls = []

    def transport(request, timeout):
        assert request.full_url == "https://api.github.com/repos/atomexpert/ZeroHub/releases/latest"
        assert timeout == 12
        assert request.get_header("Authorization") is None
        calls.append(request)
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
        return {"tag_name": "v1.0.1", "name": "Patch", "body": "Release notes"}

    absent = release.check_for_updates(force=True, env_path=env, github_api_transport=transport)
    assert absent["ok"] and not absent["published_yet"] and not absent["update_available"]
    assert absent["latest_version"] == absent["current_version"] == "1.0.0"
    present = release.check_for_updates(force=True, env_path=env, github_api_transport=transport)
    assert present["published_yet"] and present["update_available"]
    assert present["release_notes"] == "Release notes"
    assert release.check_for_updates(env_path=env, github_api_transport=transport) == present
    assert len(calls) == 2
    release._invalidate_cache()


@pytest.mark.parametrize("path,value", [
    ("file.txt", TOKEN),
    ("tests/sample.py", FINE_TOKEN),
    ("file.txt", "sk-or-v1-" + "z" * 36),
    ("file.txt", "GOCSPX-" + "x" * 23),
    ("file.txt", "-----BEGIN " + "RSA PRIVATE KEY-----"),
    (".env", ""),
    ("file.pem", ""),
    (".secrets/auth", ""),
    ("folder/hub.db-wal", ""),
    ("file.txt", r"C:\Users\atome\Desktop"),
])
def test_scanner_blocks_and_redacts(path, value):
    with pytest.raises(release.ReleaseSecurityError) as error:
        release.scan_export_tree_for_leaks({path: ("\n" + value).encode()})
    if value:
        assert value not in str(error.value)
        assert f"{path}:2:" in str(error.value)


def test_public_export_from_real_tracked_tree():
    files, _ = release._collect_export(ROOT, "1.0.0")
    release.scan_export_tree_for_leaks(files)
    assert ".env" not in files
    assert files["VERSION"] == b"1.0.0\n"
    for path in release._FIXTURE_DIGESTS:
        assert ast.dump(ast.parse(files[path])) == ast.dump(ast.parse((ROOT / path).read_bytes()))


def test_local_config_and_secret_guard(tmp_path):
    env = tmp_path / ".env"
    initial = b"# Existing comment\r\nHUB_SECRET_KEY='untouched' # keep\r\nHUB_PIN=4321\r\n"
    env.write_bytes(initial)
    settings = release.save_publisher_settings(owner="atomexpert", repo="ZeroHub", new_token=TOKEN, env_path=env)
    assert env.read_bytes().startswith(initial)
    assert settings == {"owner": "atomexpert", "repo": "ZeroHub", "has_token": True,
                        "token_mask": release.mask_github_token(TOKEN)}
    assert settings["token_mask"].endswith("aaaa") and TOKEN not in str(settings)
    assert release.mask_github_token(FINE_TOKEN).startswith("github_pat_")
    for value in (TOKEN, FINE_TOKEN):
        outbound, count = openrouter_service.sanitize_outbound_openrouter_text(f"GITHUB_PUBLISH_TOKEN={value}")
        assert count and value not in outbound
    assert "GITHUB_PUBLISH_TOKEN" in env_service.PROTECTED_SELF_HUB_KEYS
    with pytest.raises(ValueError, match="GITHUB_PUBLISH_TOKEN"):
        env_service._enforce_self_hub_protected_keys(ROOT, env.read_bytes().decode(), initial.decode())
    assert not release.save_publisher_settings(owner="atomexpert", repo="ZeroHub", clear_token=True,
                                               env_path=env)["has_token"]


def test_first_run_password(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HUB_INITIAL_PIN", raising=False)
    config.ensure_first_run_config(tmp_path)
    env = tmp_path / ".env"
    credentials = tmp_path / "data" / "FIRST_RUN_CREDENTIALS.txt"
    previous = env.read_bytes()
    pin = next(entry["value"] for entry in env_service.parse_dotenv_text(previous.decode())["entries"]
               if entry["key"] == "HUB_PIN")
    assert len(pin) == 8 and pin.isalnum() and pin in credentials.read_text()
    assert pin not in capsys.readouterr().err
    if os.name != "nt":
        assert credentials.stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("HUB_INITIAL_PIN", "AnotherPin")
    config.ensure_first_run_config(tmp_path)
    assert env.read_bytes() == previous
    custom = tmp_path / "custom"
    custom.mkdir()
    config.ensure_first_run_config(custom)
    assert "AnotherPin" in (custom / "data" / credentials.name).read_text()


def test_root_shell_files_use_lf():
    files = list(ROOT.glob("*.sh"))
    assert {p.name for p in files} >= {"start_hub.sh", "stop_hub.sh", "setup_sandbox_linux.sh", "install_linux.sh"}
    for path in files:
        source = path.read_bytes()
        assert b"\r" not in source and not source.startswith(b"\xef\xbb\xbf")
        source.decode("utf-8")


@pytest.fixture
def public_repositories(tmp_path):
    source = tmp_path / "publisher"
    remote = tmp_path / "public.git"
    source.mkdir()
    remote.mkdir()
    git(source, "init", "--template=", "-b", "main")
    git(remote, "init", "--bare", "--template=", "-b", "main")
    passport = source / ".agents" / "rules" / "hub_project.md"
    passport.parent.mkdir(parents=True)
    passport.write_text("Local generated passport with " + r"C:\Users\atome", encoding="utf-8")
    (source / "VERSION").write_bytes(b"1.0.0\n")
    (source / "requirements.txt").write_bytes(b"package==1\n")
    (source / "obsolete.txt").write_text("old", encoding="utf-8")
    (source / ".gitignore").write_bytes(b".env\ndata/\nusers/\nshared_projects/\n")
    commit(source, "Old internal commit", ".gitignore", "VERSION", "requirements.txt",
           "obsolete.txt", ".agents/rules/hub_project.md")
    (source / "draft.txt").write_text("public", encoding="utf-8")
    commit(source, "Internal work", "draft.txt")
    release.save_publisher_settings(owner="atomexpert", repo="ZeroHub", new_token=TOKEN, env_path=source / ".env")
    return source, remote


def test_publish_then_update(public_repositories, tmp_path, monkeypatch):
    source, remote = public_repositories
    remote_url = remote.as_uri()
    calls = []
    releases = {}

    def transport(request, timeout):
        assert request.get_header("Authorization") == "Bearer " + TOKEN and timeout == 12
        path = request.selector
        method = request.get_method()
        calls.append((method, path))
        payload = json.loads(request.data) if request.data else {}
        if path == "/repos/atomexpert/ZeroHub":
            if ("POST", "/user/repos") not in calls:
                raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
            return {"private": False, "full_name": "atomexpert/ZeroHub"}
        if path == "/user/repos":
            assert payload["name"] == "ZeroHub" and payload["private"] is False
            return {"private": False, "full_name": "atomexpert/ZeroHub"}
        if method == "GET":
            return releases[path.rsplit("/", 1)[-1]]
        if method == "PATCH":
            assert path.endswith("/releases/2")
            releases[payload["tag_name"]]["body"] = payload["body"]
            return releases[payload["tag_name"]]
        tag = payload["tag_name"]
        if tag in releases:
            raise urllib.error.HTTPError(request.full_url, 422, "duplicate", {}, None)
        releases[tag] = {"id": len(releases) + 1, "body": payload["body"], "html_url": f"https://example.com/{tag}"}
        return releases[tag]

    def publish(version):
        return release.publish_release_to_github(version=version, title="Release", release_notes="changes",
                                                 source_root=source, remote_git_url=remote_url,
                                                 github_api_transport=transport)

    local_head = git(source, "rev-parse", "HEAD")
    (source / "draft.txt").write_text(TOKEN, encoding="utf-8")
    with pytest.raises(release.ReleaseSecurityError):
        publish("1.0.0")
    assert not calls and not git(remote, "for-each-ref")
    (source / "draft.txt").write_text("public", encoding="utf-8")

    first = publish("1.0.0")
    assert first["clean_initial_history"] and first["tag_name"] == "v1.0.0"
    assert git(remote, "rev-list", "--count", "main") == "1"
    assert git(remote, "show", "main:.agents/rules/hub_project.md") == release.PASSPORT.strip()
    assert ".env" not in git(remote, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert git(source, "rev-parse", "HEAD") == local_head

    client = tmp_path / "client"
    git(tmp_path, "clone", "--template=", "--", remote_url, str(client))
    failing_client = tmp_path / "failing-client"
    git(tmp_path, "clone", "--template=", "--", remote_url, str(failing_client))
    client_env = client / ".env"
    client_env.write_bytes(b"HUB_PIN=9876\n")
    db = client / "data" / "hub.db"
    db.parent.mkdir()
    with sqlite3.connect(db) as conn:
        conn.execute("create table rows (value text)")
        conn.execute("insert into rows values ('preserved')")

    git(source, "rm", "--", "obsolete.txt")
    (source / "requirements.txt").write_bytes(b"package==2\n")
    second = publish("1.0.1")
    assert not second["clean_initial_history"]
    assert git(remote, "rev-list", "--count", "main") == "2"
    assert git(remote, "rev-parse", "v1.0.1^") == git(remote, "rev-parse", "v1.0.0")
    assert "obsolete.txt" not in git(remote, "ls-tree", "main")
    assert (source / "VERSION").read_bytes() == b"1.0.1\n"
    assert publish("1.0.1")["tag_name"] == "v1.0.1"
    assert ("PATCH", "/repos/atomexpert/ZeroHub/releases/2") in calls
    assert git(remote, "rev-list", "--count", "main") == "2"

    # The updater must not call a real pip or modify the publisher's history.
    ran_pip = []
    real_run = subprocess.run

    def run(command, **kwargs):
        if command[1:3] == ["-m", "pip"]:
            assert kwargs["env"]["CUDA_VISIBLE_DEVICES"] == ""
            assert kwargs["env"]["NVIDIA_VISIBLE_DEVICES"] == "none"
            assert kwargs["env"]["ORT_DISABLE_ALL_GPU"] == "1"
            assert kwargs["cwd"] in {client, failing_client}
            ran_pip.append(kwargs["cwd"])
            if kwargs["cwd"] == failing_client:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0)
        return real_run(command, **kwargs)

    monkeypatch.setattr(release.subprocess, "run", run)
    with pytest.raises(release.ReleaseError) as blocked:
        release.apply_one_click_update(source_root=source, remote_git_url=remote_url)
    assert blocked.value.status_code == 409
    assert git(source, "rev-parse", "HEAD") == local_head

    (client / "draft.txt").write_text("client edit", encoding="utf-8")
    result = release.apply_one_click_update(target_version="v1.0.1", source_root=client, remote_git_url=remote_url)
    assert result["previous_version"] == "1.0.0" and result["updated_version"] == "1.0.1"
    assert result["pip_updated"] and result["stashed_local_changes"]
    assert "zerohub-pre-update-backup" in git(client, "stash", "list")
    assert client_env.read_bytes() == b"HUB_PIN=9876\n"
    assert db.is_file() and (client / "VERSION").read_bytes() == b"1.0.1\n"
    backup = Path(result["backup_file"])
    assert backup.name.startswith("hub_pre_update_v1.0.0_") and backup.parent == db.parent / "backups"
    with sqlite3.connect(backup) as conn:
        assert conn.execute("select value from rows").fetchone() == ("preserved",)

    git(failing_client, "switch", "-c", "client-branch")
    (failing_client / "draft.txt").write_text("keep this staged", encoding="utf-8")
    git(failing_client, "add", "--", "draft.txt")
    old = git(failing_client, "rev-parse", "HEAD")
    with pytest.raises(release.ReleaseError, match="dependency installation failed"):
        release.apply_one_click_update(source_root=failing_client, remote_git_url=remote_url)
    assert git(failing_client, "rev-parse", "HEAD") == old
    assert git(failing_client, "rev-parse", "main") == old
    assert git(failing_client, "branch", "--show-current") == "client-branch"
    assert git(failing_client, "diff", "--cached", "--name-only") == "draft.txt"
    assert (failing_client / "draft.txt").read_text() == "keep this staged"
    assert ran_pip == [client, failing_client]


def test_release_routes_rbac(monkeypatch):
    def publisher():
        return {"owner": "atomexpert", "repo": "ZeroHub", "has_token": True, "token_mask": "redacted"}

    monkeypatch.setattr(release, "get_publisher_settings", publisher)
    monkeypatch.setattr(release, "get_cached_update", lambda: None)
    monkeypatch.setattr(release, "build_draft_release_notes", lambda: "- example")
    user = {"role": "developer"}
    monkeypatch.setitem(main.app.dependency_overrides, auth.get_current_user, lambda: user)
    client = TestClient(main.app, raise_server_exceptions=True)
    assert client.get("/api/health").json()["version"] == config.get_app_version()
    status = client.get("/api/updates/status")
    assert status.status_code == 200 and "publisher" not in status.json() and "redacted" not in status.text
    for method, path, body in [
        ("POST", "check", None),
        ("POST", "apply", {"force": True}),
        ("PUT", "publisher-config", {"owner": "atomexpert", "repo": "ZeroHub", "clear_token": True}),
        ("POST", "publish", {"version": "1.0.0", "title": "Release", "release_notes": ""}),
    ]:
        response = client.request(method, "/api/updates/" + path, json=body)
        assert response.status_code == 403
    user["role"] = "admin"
    assert client.get("/api/updates/status").json()["publisher"]["draft_notes"] == "- example"
