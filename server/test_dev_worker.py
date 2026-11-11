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
