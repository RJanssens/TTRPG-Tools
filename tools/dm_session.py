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
import tempfile
import threading
import time
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

# Live-state introspection so a reloaded /play page can discover that a
# turn is still cooking server-side (browser dropped the SSE stream but
# the subprocess kept running). Updated by stream_turn around the
# subprocess lifetime; read by the /api/turn_state endpoint.
_turn_state: dict = {"in_flight": False, "started_at": None, "tools_used": 0}
_turn_state_lock = threading.Lock()


def turn_state() -> dict:
    """Snapshot of the in-flight turn state for the dashboard to poll."""
    with _turn_state_lock:
        return dict(_turn_state)


def _set_turn_state(**fields) -> None:
    with _turn_state_lock:
        _turn_state.update(fields)


def _bump_tools_used() -> None:
    with _turn_state_lock:
        _turn_state["tools_used"] += 1


def session_id() -> str | None:
    """Return the persisted session id, or ``None`` for a fresh start."""
    try:
        sid = _SESSION_FILE.read_text().strip()
    except FileNotFoundError:
        return None
    return sid or None


def _project_jsonl_dir() -> Path:
    """Path to Claude Code's per-project transcript directory.

    Claude Code stores session JSONLs under
    ``~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`` where the encoding
    replaces ``/`` with ``-`` in the absolute project path."""
    encoded = str(_REPO_ROOT).replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded


_IMAGE_PROMPT_SYSTEM = (
    "You translate fantasy RPG narrative prose into a focused "
    "image-generation prompt for a Flux model.\n\n"
    "The user's input is either:\n"
    "  - a NARRATIVE on its own, OR\n"
    "  - a NARRATIVE section followed by a HINT line. The HINT is the "
    "player's direction about what the picture should emphasise (subject, "
    "framing, mood). When a HINT is present, use it as the primary anchor "
    "for the prompt and pull supporting visual detail from the narrative.\n\n"
    "Extract only the *visual* scene: subject(s), action, setting, lighting, "
    "time of day, mood, palette. Drop dialogue, internal thoughts, dice "
    "results, NPC stat blocks, exposition, and anything an image cannot "
    "show.\n\n"
    "Return one English paragraph of 60 to 120 words describing the scene "
    "the way a painter's brief would. No preface, no bullets, no "
    "explanation, no quotation marks around the output — just the prompt "
    "itself."
)


def rewrite_for_image(
    narrative: str,
    hint: str | None = None,
    timeout_seconds: int = 90,
) -> str | None:
    """One-shot Claude pre-pass: convert raw narrative prose to a focused
    image-generation prompt. Stateless — runs ``claude -p`` from a temp
    directory so the project's CLAUDE.md and MCP config don't bias the
    rewrite, no session is resumed, and nothing is persisted.

    If ``hint`` is provided, the player's optional extra direction (e.g.
    "focus on the treasure", "show Korrhast in the foreground") is
    forwarded alongside the narrative; the system prompt instructs the
    rewriter to give the hint priority when shaping the visual.

    Returns the rewritten prompt, or ``None`` on any failure (caller should
    fall back to the raw narrative)."""
    text = (narrative or "").strip()
    if not text:
        return None

    hint_clean = (hint or "").strip()
    if hint_clean:
        user_msg = (
            f"NARRATIVE:\n{text}\n\n"
            f"HINT (player's direction for the illustrator — prioritise this "
            f"when shaping the prompt): {hint_clean}"
        )
    else:
        user_msg = text

    args = [
        _CLAUDE_BIN, "-p", user_msg,
        "--append-system-prompt", _IMAGE_PROMPT_SYSTEM,
    ]
    try:
        proc = subprocess.run(
            args,
            cwd=tempfile.gettempdir(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    return out or None


def last_dm_text() -> str | None:
    """Return the most recent assistant text block from the persisted
    session's JSONL transcript, or ``None`` if there's no session yet, no
    transcript file, or no DM prose recorded.

    Used by the dashboard's /play surface to replay the last narrative line
    when a player reloads the page mid-session."""
    sid = session_id()
    if not sid:
        return None
    p = _project_jsonl_dir() / f"{sid}.jsonl"
    if not p.exists():
        return None
    last: str | None = None
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                content = rec.get("message", {}).get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = (block.get("text") or "").strip()
                        if text:
                            last = text
    except OSError:
        return None
    return last


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
        _set_turn_state(in_flight=True, started_at=time.time(), tools_used=0)
        try:
            for line in proc.stdout:
                evt = _process_event_line(line)
                if evt is None:
                    continue
                # Tally tool_use blocks for the live turn-state poll.
                if evt.get("type") == "assistant":
                    content = evt.get("message", {}).get("content")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                _bump_tools_used()
                yield evt
        finally:
            rc = proc.wait()
            _set_turn_state(in_flight=False, started_at=None, tools_used=0)
            if rc != 0:
                err = (proc.stderr.read() if proc.stderr else "").strip()
                yield {"type": "error", "returncode": rc, "stderr": err[-2000:]}
