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

# Session ids are stored per-campaign under ``campaigns/<slug>/.dm_session``
# so each campaign keeps its own /play conversation thread. Switching
# campaigns transparently swaps which session resumes on the next turn.
#
# The legacy single file at the repo root is kept as a one-shot migration
# source — see ``_migrate_legacy_session()`` at the bottom of this module.
_LEGACY_SESSION_FILE: Path = _REPO_ROOT / ".dm_session"
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

# Tracks the currently-running claude subprocess so ``force_reset`` can
# terminate it from a different request thread when the dashboard's GUI
# escape hatch is invoked. ``None`` whenever no turn is in flight.
_active_proc: subprocess.Popen | None = None
_active_proc_lock = threading.Lock()

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


def _active_campaign_slug() -> str | None:
    """Read the slug of the currently-active campaign from ``.active``.
    Returns ``None`` when no campaign is active (fresh checkout)."""
    f = _REPO_ROOT / ".active"
    try:
        slug = f.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return slug or None


def _session_file_for(slug: str) -> Path:
    """Per-campaign session-id file path. Sibling to ``state.json``."""
    return _REPO_ROOT / "campaigns" / slug / ".dm_session"


def _active_session_file() -> Path | None:
    """Resolve the session-id file for the currently-active campaign,
    or ``None`` if no campaign is active. Tests override this function
    to redirect reads/writes into a tmpdir."""
    slug = _active_campaign_slug()
    return _session_file_for(slug) if slug else None


def session_id() -> str | None:
    """Return the persisted session id for the active campaign, or
    ``None`` for a fresh start (no campaign, no file, or empty file)."""
    p = _active_session_file()
    if p is None:
        return None
    try:
        sid = p.read_text().strip()
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


_SUGGEST_ACTIONS_SYSTEM = (
    "You read the last DM reply from a tabletop RPG session and propose "
    "2 to 4 plausible next actions a player might take.\n\n"
    "Each action MUST:\n"
    "  - be short (5 to 15 words)\n"
    "  - be a concrete action the player can declare to the DM "
    "(e.g. 'Search the body for a key', not 'consider searching')\n"
    "  - be distinct from the others — different approaches, not "
    "rephrasings of the same idea\n"
    "  - rest only on what the DM has actually told the players; never "
    "assume facts the player has not learned\n\n"
    "If the DM is presenting explicit choices (e.g. 'left passage or "
    "right?'), surface those choices verbatim. If the DM asks an "
    "open-ended 'what do you do?', propose the most consequential or "
    "obvious moves.\n\n"
    "Output each action on its own line. No numbering, no bullets, no "
    "markdown, no preamble, no commentary. Just the action lines."
)


def suggest_actions(
    narrative: str,
    timeout_seconds: int = 60,
    limit: int = 4,
) -> list[str]:
    """One-shot Claude pre-pass that returns 2-4 plausible next player
    actions for a given DM reply. Stateless — runs ``claude -p`` from a
    temp directory so CLAUDE.md and MCP config don't bias the suggestions
    and no session is resumed.

    Returns an empty list on any failure or when the DM reply is empty;
    the caller silently hides the chip row in that case."""
    text = (narrative or "").strip()
    if not text:
        return []
    args = [
        _CLAUDE_BIN, "-p", text[:4000],
        "--append-system-prompt", _SUGGEST_ACTIONS_SYSTEM,
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
        return []
    if proc.returncode != 0:
        return []
    out: list[str] = []
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        # Defensive stripping for the cases where the model slips in a
        # bullet or a "1. " prefix despite the system prompt.
        line = line.lstrip("-*•").lstrip()
        while line[:2].isdigit() and len(line) > 2 and line[2] in ".)":
            line = line[3:].lstrip()
        if len(line) <= 1 and line[:1].isdigit():
            line = line[1:].lstrip(".) ").lstrip()
        if not line:
            continue
        out.append(line)
        if len(out) >= limit:
            break
    return out if len(out) >= 2 else []


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
    """Forget the active campaign's session — the next turn starts a
    new one. Sessions in other campaigns are untouched."""
    p = _active_session_file()
    if p is not None:
        p.unlink(missing_ok=True)


def set_session(sid: str) -> None:
    """Pin a specific session id as the active one for the current
    campaign. Public counterpart to the implicit save inside ``stream_turn``;
    used by the dashboard's 'resume this session' button."""
    _save_session_id(sid)


def _save_session_id(sid: str) -> None:
    p = _active_session_file()
    if p is None:
        return
    # Don't materialise a campaigns/<slug>/ directory that doesn't exist —
    # if .active points at a missing campaign, silently drop the save
    # rather than fabricating a phantom directory.
    if not p.parent.is_dir():
        return
    p.write_text(sid)


def _migrate_legacy_session() -> None:
    """One-shot: fold the old repo-root ``.dm_session`` into the active
    campaign's per-campaign file the first time this module loads after
    the per-campaign change. Idempotent — once the legacy file is gone
    nothing happens. Does nothing when no campaign is active."""
    if not _LEGACY_SESSION_FILE.is_file():
        return
    p = _active_session_file()
    if p is None or not p.parent.is_dir():
        return  # No active campaign or its directory is missing — bail.
    try:
        sid = _LEGACY_SESSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if sid and not p.exists():
        p.write_text(sid)
    _LEGACY_SESSION_FILE.unlink(missing_ok=True)


_migrate_legacy_session()


def _build_args(message: str, sid: str | None) -> list[str]:
    args = [
        _CLAUDE_BIN, "-p", message,
        "--output-format", "stream-json",
        "--include-partial-messages",
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
        with _active_proc_lock:
            global _active_proc
            _active_proc = proc
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
            with _active_proc_lock:
                if _active_proc is proc:
                    _active_proc = None
            _set_turn_state(in_flight=False, started_at=None, tools_used=0)
            if rc != 0:
                err = (proc.stderr.read() if proc.stderr else "").strip()
                yield {"type": "error", "returncode": rc, "stderr": err[-2000:]}


def force_reset() -> dict:
    """GUI escape hatch for a wedged DM turn. Kills any in-flight claude
    subprocess, clears the in-flight turn-state flag, and replaces the
    module-level ``_turn_lock`` so a thread still wedged inside the old
    lock can't keep blocking new requests. The wedged thread retains a
    reference to the original lock and will release it on its own exit;
    fresh ``stream_turn`` calls bind the new lock at entry.

    Returns a small dict describing what happened, suitable for echoing
    back to the dashboard."""
    global _turn_lock, _active_proc
    killed = False
    with _active_proc_lock:
        proc = _active_proc
        _active_proc = None
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
            killed = True
        except OSError:
            pass
    _turn_lock = threading.Lock()
    _set_turn_state(in_flight=False, started_at=None, tools_used=0)
    return {"killed_subprocess": killed}
