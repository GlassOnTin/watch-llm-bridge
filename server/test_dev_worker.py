"""Unit tests for the workstation AGY Carbon pull-worker."""
from pathlib import Path
import json
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dev import agy_worker  # noqa: E402


def test_clean_for_speech_strips_markdown_links_and_formatting():
    raw = "Updated [`route`](file:///home/ian/Code/watch-llm-bridge/server/app.py#L1886) in **app.py** and `store.py`."
    assert agy_worker.clean_for_speech(raw) == "Updated route in app.py and store.py."


def test_prepare_agy_home_isolates_mcp_and_symlinks_essentials(tmp_path):
    real_home = tmp_path / "real"
    (real_home / ".ssh").mkdir(parents=True)
    (real_home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    (real_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").write_text("tok")
    (real_home / ".gemini" / "antigravity-cli" / "settings.json").write_text("{}")
    (real_home / ".gemini" / "config").mkdir(parents=True)
    (real_home / ".gemini" / "config" / "GEMINI.md").write_text("# rules")

    agy_home = tmp_path / "agy_home"
    agy_worker.prepare_agy_home(real_home, agy_home)

    mcp_cfg = json.loads((agy_home / ".gemini" / "config" / "mcp_config.json").read_text())
    assert mcp_cfg == {"mcpServers": {}}
    assert (agy_home / ".ssh").is_symlink()
    assert (agy_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").is_symlink()
    assert (agy_home / ".gemini" / "config" / "GEMINI.md").is_symlink()


def test_run_agy_turn_passes_conversation_id_and_parses_json(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env", {})
        seen["stdin"] = kwargs.get("stdin")
        payload = {
            "conversation_id": "conv-999",
            "status": "SUCCESS",
            "response": "Checked [`health`](file:///opt/bridge/app.py): **all good**.\n",
        }
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(agy_worker.subprocess, "run", fake_run)
    reply, conv_id, is_err = agy_worker.run_agy_turn(
        prompt="now check health",
        conversation_id="conv-999",
        repo_root=tmp_path,
        agy_home=tmp_path / "agy_home",
        agy_bin="/usr/local/bin/agy",
        model="gemini-4-carbon-medium",
    )
    assert is_err is False
    assert conv_id == "conv-999"
    assert reply == "Checked health: all good."
    assert "--conversation" in seen["cmd"]
    assert "conv-999" in seen["cmd"]
    assert "--dangerously-skip-permissions" in seen["cmd"]
    assert any(a.startswith("--print=") for a in seen["cmd"])
    assert seen["stdin"] == subprocess.DEVNULL
    assert seen["env"]["HOME"] == str(tmp_path / "agy_home")

    reply2, raw2, conv2, err2 = agy_worker.run_agy_turn(
        prompt="now check health",
        conversation_id="conv-999",
        repo_root=tmp_path,
        agy_home=tmp_path / "agy_home",
        agy_bin="/usr/local/bin/agy",
        model="gemini-4-carbon-medium",
        include_raw=True,
    )
    assert err2 is False
    assert conv2 == "conv-999"
    assert reply2 == "Checked health: all good."
    assert "file:///opt/bridge/app.py" in raw2


def test_ensure_project_dir_creates_git_repo_and_rejects_traversal(tmp_path):
    projects_root = tmp_path / "Code"
    projects_root.mkdir()

    proj = agy_worker.ensure_project_dir(projects_root, "solar-logger", init_git=True)
    assert proj == projects_root / "solar-logger"
    assert (proj / ".git").is_dir()

    import pytest
    with pytest.raises(ValueError):
        agy_worker.ensure_project_dir(projects_root, "../escape", init_git=True)
    with pytest.raises(ValueError):
        agy_worker.ensure_project_dir(projects_root, "bad/slash", init_git=True)


def test_delete_project_dir_removes_folder_and_enforces_containment(tmp_path):
    import pytest

    projects_root = tmp_path / "Code"
    projects_root.mkdir()

    # Default bridge directory is strictly protected
    bridge_dir = projects_root / "watch-llm-bridge"
    bridge_dir.mkdir()
    (bridge_dir / "keep.txt").write_text("important")
    with pytest.raises(ValueError, match="watch-llm-bridge"):
        agy_worker.delete_project_dir(projects_root, "watch-llm-bridge")
    assert (bridge_dir / "keep.txt").exists()

    # Valid watch-started project folder is removed cleanly
    solar = agy_worker.ensure_project_dir(projects_root, "solar-logger", init_git=True)
    (solar / "main.py").write_text("print('hi')\n")
    assert agy_worker.delete_project_dir(projects_root, "solar-logger") is True
    assert not solar.exists()
    assert agy_worker.delete_project_dir(projects_root, "solar-logger") is False

    # Symlinks and path traversal are rejected without touching targets
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    sym = projects_root / "linked-proj"
    sym.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        agy_worker.delete_project_dir(projects_root, "linked-proj")
    assert (outside / "secret.txt").exists()

    with pytest.raises(ValueError):
        agy_worker.delete_project_dir(projects_root, "../outside")


