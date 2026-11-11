#!/usr/bin/env python3
"""Workstation pull-worker bridging Watch Bridge voice turns to AGY Gemini 4 Carbon.

Polls `POST /dev/poll` over outbound HTTPS with the operator's BRIDGE_TOKEN,
executes each turn in `/home/ian/Code/watch-llm-bridge` using `agy` with
persistent `--conversation <uuid>`, and posts spoken-friendly summaries back to
`POST /dev/turns/<id>/complete`.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "gemini-4-carbon-medium"

FIRST_TURN_PREAMBLE = """[Watch Bridge Voice Dev Session]
The operator (Ian) is speaking to you from his Apple Watch via https://bridge.upperpeas.com.
Workspace: {repo_root} (live server at /opt/bridge/, deployed via {repo_root}/dev/canary_deploy.sh).
Guidelines:
1. Your final response is read aloud on an Apple Watch or iPhone notification: keep your final output to 1-3 short, plain-spoken sentences. Do not include markdown links, code blocks, file URIs, or UUIDs in your final answer.
2. Before claiming any code change works, run pytest in a clean env from /tmp:
   cd /tmp && env -i PATH="$PATH" HOME=/tmp BRIDGE_DB=/tmp/btest.db BRIDGE_TOKEN=t TRELLO_KEY=k TRELLO_TOKEN=t {repo_root}/.venv/bin/pytest -q {repo_root}/server/
3. Do NOT deploy to production unless Ian explicitly asks to deploy. When asked to deploy, ALWAYS run `{repo_root}/dev/canary_deploy.sh` (runs clean tests, snapshots /opt/bridge.prev, deploys, checks /health, and auto-rolls back on failure). Never add AI attribution to git commits.

Spoken request:
{prompt}"""

FOLLOWUP_PREAMBLE = """[Spoken follow-up from Apple Watch — reply in 1-3 short plain-spoken sentences, no markdown links or code blocks]
{prompt}"""


def clean_for_speech(text: str) -> str:
    """Strip markdown links, code fences, inline backticks, and emphasis for TTS."""
    out = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    out = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", out)
    out = out.replace("`", "").replace("**", "").replace("__", "")
    out = re.sub(r"(?m)^\s*[-*#>]+\s*", "", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out or "Done."


def _ensure_symlink(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if dst.is_symlink():
        if dst.resolve() == src.resolve():
            return
        dst.unlink()
    elif dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.symlink_to(src)


def prepare_agy_home(real_home: Path, agy_home: Path) -> None:
    """Create an isolated HOME for headless agy so hardware/broken MCP servers
    do not stall cold-start, while preserving OAuth, SSH, git, and user rules."""
    (agy_home / ".gemini" / "antigravity-cli").mkdir(parents=True, exist_ok=True)
    (agy_home / ".gemini" / "config").mkdir(parents=True, exist_ok=True)

    for rel in (
        Path(".ssh"),
        Path(".gitconfig"),
        Path(".agents"),
        Path(".claude"),
        Path(".local"),
        Path(".gemini/antigravity-cli/antigravity-oauth-token"),
        Path(".gemini/antigravity-cli/settings.json"),
        Path(".gemini/config/GEMINI.md"),
        Path(".gemini/config/rules"),
        Path(".gemini/config/skills"),
        Path(".gemini/config/hooks.json"),
    ):
        _ensure_symlink(real_home / rel, agy_home / rel)

    mcp_path = agy_home / ".gemini" / "config" / "mcp_config.json"
    mcp_path.write_text('{"mcpServers": {}}\n')


def format_prompt(prompt: str, conversation_id: str, repo_root: Path) -> str:
    if conversation_id:
        return FOLLOWUP_PREAMBLE.format(prompt=prompt.strip())
    return FIRST_TURN_PREAMBLE.format(repo_root=repo_root, prompt=prompt.strip())


def run_agy_turn(
    prompt: str,
    conversation_id: str,
    repo_root: Path,
    agy_home: Path,
    agy_bin: str,
    model: str = DEFAULT_MODEL,
    timeout: int = 600,
) -> tuple[str, str, bool]:
    """Run one turn of `agy` and return (spoken_reply, conversation_id, is_error)."""
    framed = format_prompt(prompt, conversation_id, repo_root)
    cmd = [
        agy_bin,
        "--model",
        model,
        "--dangerously-skip-permissions",
        "--output-format",
        "json",
    ]
    if conversation_id:
        cmd += ["--conversation", conversation_id]
    cmd.append(f"--print={framed}")

    env = os.environ.copy()
    env["HOME"] = str(agy_home)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ("Carbon timed out after ten minutes on that task.", conversation_id, True)

    stdout = (proc.stdout or "").strip()
    if stdout:
        # Take the last JSON object line in case hooks emitted text earlier.
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    data = json.loads(line)
                    new_conv = data.get("conversation_id") or conversation_id
                    status = data.get("status", "")
                    resp = clean_for_speech(data.get("response") or "")
                    is_err = proc.returncode != 0 or status not in ("SUCCESS", "")
                    return (resp, new_conv, is_err)
                except json.JSONDecodeError:
                    break

    if proc.returncode != 0:
        err_tail = clean_for_speech((proc.stderr or stdout or "unknown error")[-300:])
        return (f"Carbon hit an error: {err_tail}", conversation_id, True)
    return (clean_for_speech(stdout), conversation_id, False)


def post_complete_with_retry(
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    turn_id: int,
    reply: str,
    conversation_id: str,
    is_err: bool,
) -> None:
    url = f"{base_url}/dev/turns/{turn_id}/complete"
    payload = {
        "reply": reply,
        "conversation_id": conversation_id,
        "error": is_err,
    }
    delays = [1, 2, 3, 5, 8, 12]
    for idx, delay in enumerate(delays):
        try:
            r = session.post(url, headers=headers, json=payload, timeout=15)
            r.raise_for_status()
            return
        except requests.RequestException as e:
            if idx == len(delays) - 1:
                raise
            logging.warning("complete turn %s attempt %s failed (%s); retrying in %ss",
                            turn_id, idx + 1, e, delay)
            time.sleep(delay)


def load_config(repo_root: Path) -> tuple[str, str, str, str]:
    file_env = dotenv_values(repo_root / ".env")
    token = os.environ.get("BRIDGE_TOKEN") or file_env.get("BRIDGE_TOKEN") or ""
    if not token:
        raise RuntimeError("BRIDGE_TOKEN is missing from environment and .env")
    base_url = os.environ.get("BRIDGE_URL") or file_env.get("BRIDGE_URL") or ""
    if not base_url:
        cmd_url = os.environ.get("COMMAND_URL") or file_env.get("COMMAND_URL") or "https://bridge.upperpeas.com/command"
        base_url = cmd_url.removesuffix("/command").rstrip("/")
    model = os.environ.get("AGY_MODEL") or DEFAULT_MODEL
    agy_bin = os.environ.get("AGY_BIN") or shutil.which("agy") or str(Path.home() / ".local/bin/agy")
    return base_url, token, model, agy_bin


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch Bridge AGY Carbon worker")
    parser.add_argument("--once", action="store_true", help="Poll once (wait=0) and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    real_home = Path(os.environ.get("REAL_HOME") or Path.home())
    agy_home = Path(os.environ.get("AGY_WORK_HOME") or (real_home / ".cache" / "watch-bridge-agy-home"))
    base_url, token, model, agy_bin = load_config(REPO_ROOT)
    host = socket.gethostname()
    headers = {"Authorization": f"Bearer {token}"}
    session = requests.Session()

    prepare_agy_home(real_home, agy_home)
    logging.info("agy_worker ready: base=%s model=%s host=%s agy_home=%s",
                 base_url, model, host, agy_home)

    while True:
        try:
            r = session.post(
                f"{base_url}/dev/poll",
                headers=headers,
                json={"host": host, "wait": 0.0 if args.once else 15.0},
                timeout=35,
            )
            r.raise_for_status()
            turn = r.json().get("turn")
        except requests.RequestException as e:
            logging.warning("poll failed: %s", e)
            if args.once:
                return 1
            time.sleep(3)
            continue

        if not turn:
            if args.once:
                return 0
            continue

        turn_id = turn["id"]
        conv_id = turn.get("conversation_id") or ""
        logging.info("claimed turn id=%s conv=%s prompt=%r",
                     turn_id, conv_id or "(new)", turn["prompt"][:120])
        prepare_agy_home(real_home, agy_home)
        t0 = time.monotonic()
        reply, new_conv, is_err = run_agy_turn(
            prompt=turn["prompt"],
            conversation_id=conv_id,
            repo_root=REPO_ROOT,
            agy_home=agy_home,
            agy_bin=agy_bin,
            model=model,
        )
        elapsed = time.monotonic() - t0
        logging.info("finished turn id=%s in %.1fs err=%s conv=%s reply=%r",
                     turn_id, elapsed, is_err, new_conv, reply[:160])
        post_complete_with_retry(
            session, base_url, headers, turn_id, reply, new_conv, is_err
        )
        if args.once:
            return 0


if __name__ == "__main__":
    sys.exit(main())
