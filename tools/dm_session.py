"""Headless Claude Code session driver for the dashboard's /play surface.

Spawns ``claude -p`` with the project's MCP server already attached (loaded
automatically from ``.claude/`` config) so each turn runs against the user's
Claude Code subscription rather than consuming API tokens. Persists the
session id between turns so the campaign conversation continues across HTTP
requests.

The CLI emits one JSON object per line under
``--output-format stream-json --verbose``; this module parses each line,
captures the session id from the init event, and yields the events upstream
so the dashboard can render assistant text and tool-use markers as they
arrive.

Usage from a Flask handler::

    from tools.dm_session import stream_turn
    for evt in stream_turn(player_input):
        ...  # forward as SSE
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Iterator

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Module-level so tests can override.
_SESSION_FILE: Path = _REPO_ROOT / ".dm_session"
_CLAUDE_BIN: str = "claude"

# Tools the headless DM may use without prompting. The MCP wildcard covers
# every registered TTRPG tool; Read/Write/Edit/Bash let the DM update
# character files, location files, and DSL maps the way it does in
# interactive sessions.
_ALLOWED_TOOLS = "mcp__ttrpg__*,Read,Write,Edit,Bash"

# Single-flight: the CLI mutates state under ``.claude/projects/`` and
# concurrent ``--resume`` calls on the same session id would race. The
# dashboard is single-player; one turn at a time is correct.
_turn_lock = threading.Lock()


def session_id() -> str | None:
    """Return the persisted session id, or ``None`` for a fresh start."""
    try:
        sid = _SESSION_FILE.read_text().strip()
    except FileNotFoundError:
        return None
    return sid or None


def reset_session() -> None:
    """Forget the current session — the next turn starts a new one."""
    _SESSION_FILE.unlink(missing_ok=True)


def _save_session_id(sid: str) -> None:
    _SESSION_FILE.write_text(sid)


def _build_args(message: str, sid: str | None) -> list[str]:
    args = [
        _CLAUDE_BIN, "-p", message,
        "--output-format", "stream-json",
        "--verbose",
        "--allowedTools", _ALLOWED_TOOLS,
    ]
    if sid:
        args += ["--resume", sid]
    return args


def _process_event_line(line: str) -> dict | None:
    """Parse one JSONL line from the CLI; capture the session id as a side
    effect when the init event arrives. Returns the event dict, or ``None``
    for blank lines. Malformed JSON is wrapped as ``{"type": "raw", ...}``
    so callers can still surface it."""
    line = line.strip()
    if not line:
        return None
    try:
        evt = json.loads(line)
    except json.JSONDecodeError:
        return {"type": "raw", "line": line}
    if evt.get("type") == "system" and isinstance(evt.get("session_id"), str):
        _save_session_id(evt["session_id"])
    return evt


def stream_turn(message: str) -> Iterator[dict]:
    """Run one DM turn against the persisted session.

    Yields parsed JSON events as the CLI emits them. The first event is
    typically a ``system`` init record carrying ``session_id`` — that id is
    persisted automatically so the next call resumes the same conversation.

    On non-zero exit, yields a final ``{"type": "error", ...}`` event with
    the captured stderr (truncated)."""
    sid = session_id()
    args = _build_args(message, sid)

    with _turn_lock:
        proc = subprocess.Popen(
            args,
            cwd=_REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                evt = _process_event_line(line)
                if evt is not None:
                    yield evt
        finally:
            rc = proc.wait()
            if rc != 0:
                err = (proc.stderr.read() if proc.stderr else "").strip()
                yield {"type": "error", "returncode": rc, "stderr": err[-2000:]}
