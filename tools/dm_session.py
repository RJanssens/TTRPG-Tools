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
import os
import re
import queue
import subprocess
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _audit_after_turn() -> None:
    """Background-fire after each turn: run the procedural audit against the
    current session's events.jsonl plus the latest DM response prose, and
    write a fresh snapshot the dashboard can poll. Daemon thread, never
    blocks the SSE close, never raises.

    Architecture C from the discussion: zero LLM cost (findings are surfaced
    in the dashboard UI for the human, not injected into Claude's prompt).
    Claude only sees the findings at next ``session_primer()`` time.

    The prose audit catches fourth-wall and railroading lapses (module
    room IDs in narrative, hidden features named in OOC prompts, directive
    language, DM-omniscience leaks) that the event-stream audit can't see."""
    try:
        import _campaign as _c
        from tools.audit import _run_audit

        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        current = int(state.get("current_session", 1))

        # Pull the latest DM response prose so the prose-pattern checks
        # in _run_audit fire. last_dm_text() reads the JSONL transcript
        # for the active session and returns the most recent assistant
        # text block. Best-effort: any failure here is silently absorbed.
        try:
            dm_text = last_dm_text()
        except Exception:
            dm_text = None

        result = _run_audit(current, dm_text=dm_text)
        result["timestamp"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

        out_path = cfg["_data_dir"] / "_session_audit.json"
        _c.atomic_write_text(out_path, json.dumps(result, indent=2))
    except Exception:
        # Audit is advisory — never let a failure here disturb the turn flow.
        pass


def latest_audit() -> dict | None:
    """Read the most recent audit snapshot for the active campaign, or
    ``None`` if no audit has been written yet. Used by the dashboard's
    ``/api/audit_live.json`` endpoint."""
    slug = _active_campaign_slug()
    if not slug:
        return None
    p = _REPO_ROOT / "campaigns" / slug / "_session_audit.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _audit_settings_path() -> Path | None:
    slug = _active_campaign_slug()
    if not slug:
        return None
    return _REPO_ROOT / "campaigns" / slug / "_audit_settings.json"


# Default config. Both knobs are user-toggleable from the dashboard's
# audit dialog. ``inject_addendum`` controls whether
# ``_recent_violations_addendum`` returns a string (gates B4 — recent
# lapses appearing in the DM's next system prompt).
_AUDIT_SETTINGS_DEFAULTS = {
    "inject_addendum": False,
}


def audit_settings() -> dict:
    """Return the audit settings for the active campaign, merged with
    defaults. Safe to call when no campaign is active (returns defaults)."""
    p = _audit_settings_path()
    out = dict(_AUDIT_SETTINGS_DEFAULTS)
    if p is None:
        return out
    try:
        loaded = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            out.update({k: v for k, v in loaded.items() if k in _AUDIT_SETTINGS_DEFAULTS})
    except (OSError, json.JSONDecodeError):
        pass
    return out


def set_audit_settings(**kwargs) -> dict:
    """Update one or more audit settings for the active campaign. Unknown
    keys are silently dropped. Returns the merged result."""
    p = _audit_settings_path()
    if p is None or not p.parent.is_dir():
        return audit_settings()
    current = audit_settings()
    for k, v in kwargs.items():
        if k in _AUDIT_SETTINGS_DEFAULTS:
            current[k] = bool(v)
    try:
        p.write_text(json.dumps(current, indent=2), encoding="utf-8")
    except OSError:
        pass
    return current


def clear_shown_findings() -> None:
    """Reset the shown-findings tracker so the next turn re-surfaces every
    current lapse via the addendum. Useful when the user wants to retry the
    nudge — e.g. after toggling injection back on, or after the DM
    repeatedly ignored a particular violation."""
    p = _audit_shown_path()
    if p is not None and p.exists():
        try:
            p.unlink()
        except OSError:
            pass


def _finding_key(f: dict) -> str:
    """Stable identifier for a finding so we can dedupe across turns.
    A violation surfaces to the model exactly once via the system-prompt
    addendum; after that it lives in the dashboard panel only."""
    return f"{f.get('kind', '')}:{f.get('slug', '')}"


def _audit_shown_path() -> Path | None:
    slug = _active_campaign_slug()
    if not slug:
        return None
    return _REPO_ROOT / "campaigns" / slug / "_session_audit_shown.json"


def _load_shown_findings() -> set[str]:
    p = _audit_shown_path()
    if p is None:
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return set()


def _save_shown_findings(shown: set[str]) -> None:
    p = _audit_shown_path()
    if p is None or not p.parent.is_dir():
        return
    try:
        p.write_text(json.dumps(sorted(shown)), encoding="utf-8")
    except OSError:
        pass


# --- DM tone / voice ------------------------------------------------------
# A per-campaign voice directive injected into every turn's system prompt via
# --append-system-prompt. Tone shapes texture/phrasing only and is framed so it
# can never override the Hard Procedural Constraints. ``classic`` injects
# nothing (the CLAUDE.md baseline). ``custom`` injects the user's own text.
_TONE_PREFIX = (
    "TONE & VOICE DIRECTION — shapes texture, phrasing, pacing, and imagery "
    "only. It NEVER overrides the Hard Procedural Constraints, dice results, "
    "monster intelligence, NPC interests, or the fourth-wall rules in "
    "CLAUDE.md; resolve every mechanic exactly as always and apply this purely "
    "to HOW you narrate the outcome.\n\n"
)

_TONE_PRESETS = {
    "grimdark": (
        "Narrate in a grim, gritty register. The world is dangerous, weary, and "
        "morally grey; survival costs something and violence carries weight. Keep "
        "prose terse and sensory — guttering torches, cold, blood, exhaustion. "
        "NPCs are self-interested and unsentimental. Avoid winking heroism and "
        "tidy comfort; let hard outcomes land without softening."
    ),
    "heroic": (
        "Narrate in a high-heroic, sword-and-sorcery register. Deeds are larger "
        "than life and the world is vivid and colourful — banners, oaths, last "
        "stands, monstrous evil and shining courage. Use sweeping, romantic "
        "imagery and a cinematic eye. Stakes feel epic and boldness is worth "
        "celebrating, even as the dice stay honest."
    ),
    "horror": (
        "Narrate in a register of slow dread. Favour atmosphere over spectacle: "
        "hushed pacing, wrongness at the edges, the unseen implied rather than "
        "shown. Light is scarce, sounds are off, safety is always provisional. "
        "Withhold and let silence stretch; unsettle before you reveal. Dread "
        "over gore."
    ),
    "comedic": (
        "Narrate with warmth and wry, character-driven humour — quick banter, "
        "comic NPC quirks, ironic mishaps in a Discworld vein. Keep it light and "
        "fun, but don't undercut real danger: when a blade is drawn or a save is "
        "failed, let the moment chill as it should."
    ),
}

# Selectable keys surfaced to the dashboard, in display order. ``classic`` and
# ``custom`` are handled specially (no preset body); the rest map into PRESETS.
_TONE_CHOICES = ["classic", "grimdark", "heroic", "horror", "comedic", "custom"]

_TONE_DEFAULTS = {"preset": "classic", "custom": ""}


def _tone_path() -> Path | None:
    slug = _active_campaign_slug()
    if not slug:
        return None
    return _REPO_ROOT / "campaigns" / slug / "_tone.json"


def tone_setting() -> dict:
    """Return the active campaign's tone setting merged with defaults. Safe
    when no campaign is active (returns defaults)."""
    out = dict(_TONE_DEFAULTS)
    p = _tone_path()
    if p is None:
        return out
    try:
        loaded = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            if loaded.get("preset") in _TONE_CHOICES:
                out["preset"] = loaded["preset"]
            if isinstance(loaded.get("custom"), str):
                out["custom"] = loaded["custom"]
    except (OSError, json.JSONDecodeError):
        pass
    return out


def set_tone(preset: str | None = None, custom: str | None = None) -> dict:
    """Persist the active campaign's tone. ``preset`` must be one of
    ``_TONE_CHOICES``; ``custom`` is the free-text used when preset=='custom'.
    Returns the merged setting."""
    current = tone_setting()
    if preset in _TONE_CHOICES:
        current["preset"] = preset
    if custom is not None:
        current["custom"] = str(custom)[:2000]
    p = _tone_path()
    if p is not None and p.parent.is_dir():
        try:
            p.write_text(json.dumps(current, indent=2), encoding="utf-8")
        except OSError:
            pass
    return current


def tone_choices() -> list[str]:
    """Selectable tone keys, in display order (for the dashboard selector)."""
    return list(_TONE_CHOICES)


def _tone_addendum() -> str | None:
    """The voice directive to inject this turn, or ``None`` for the classic
    baseline (nothing appended)."""
    s = tone_setting()
    preset = s.get("preset", "classic")
    if preset == "classic":
        return None
    if preset == "custom":
        body = (s.get("custom") or "").strip()
    else:
        body = _TONE_PRESETS.get(preset, "")
    if not body:
        return None
    return _TONE_PREFIX + body


# --- Narrative detail level ----------------------------------------------
# A per-campaign knob, SEPARATE from tone, controlling how much raw
# mechanical detail (exact coin counts, ability scores, AC/THAC0, stat-block
# values, DM-side background facts) the DM exposes in player-facing prose.
# Like tone it shapes only HOW results are narrated — never the dice, the
# rules, monster intelligence, NPC interests, or the fourth wall. Levels run
# 0 (immersive — abstract every number) → 3 (open table — show the numbers).
# Level 2 is the default and matches long-standing behaviour, so existing
# campaigns see no change. Stored in campaigns/<slug>/_detail_level.json.
_DETAIL_PREFIX = (
    "NARRATIVE DETAIL LEVEL — controls how much raw mechanical detail (exact "
    "coin counts, ability scores, AC, THAC0, HP totals, stat-block values, "
    "DM-side background facts) you surface in PLAYER-FACING prose. It changes "
    "only HOW you report results the tools already produced; it NEVER changes "
    "the dice, the rules, monster intelligence, NPC interests, or the "
    "fourth-wall constraints in CLAUDE.md. Resolve every mechanic exactly as "
    "always, then narrate to this level:\n\n"
)

# level -> (label, directive body). An empty body means "inject nothing".
_DETAIL_LEVELS: dict[int, tuple[str, str]] = {
    0: (
        "Immersive",
        "Expose NO raw numbers in player-facing prose. Render coin as "
        "impressions ('a heavy purse', 'a few tarnished silvers'), never "
        "totals. Never state ability scores, AC, THAC0, HP totals, or "
        "stat-block values — convey them as description ('powerfully built', "
        "'the wound looks grave'). Reveal DM-side background only as the "
        "characters discover it in fiction. The numbers still exist in the "
        "tools; you simply don't read them aloud.",
    ),
    1: (
        "Light",
        "Prefer impressions, but you MAY give rounded or approximate figures "
        "when the characters would plausibly know them ('about 1,700 gold', "
        "'badly hurt — maybe a third of his strength left'). Avoid bare "
        "stat-block values (exact AC, THAC0, ability scores) unless the "
        "player asks directly.",
    ),
    2: ("Standard", ""),  # default — the CLAUDE.md baseline, nothing injected
    3: (
        "Open table",
        "Play with the screen down. Freely state exact coin counts, HP "
        "totals, AC, THAC0, ability scores, and stat-block values in "
        "narration when relevant, and summarise mechanical facts plainly. "
        "This relaxes only the PRESENTATION of numbers the party could "
        "reasonably audit — it does NOT lift the fourth wall on hidden, "
        "DM-only knowledge (module canon, unscouted rooms, secret motives, "
        "concealed creatures), which stays hidden per CLAUDE.md regardless "
        "of level.",
    ),
}

_DETAIL_DEFAULT_LEVEL = 2
_DETAIL_DEFAULTS = {"level": _DETAIL_DEFAULT_LEVEL}


def _detail_path() -> Path | None:
    slug = _active_campaign_slug()
    if not slug:
        return None
    return _REPO_ROOT / "campaigns" / slug / "_detail_level.json"


def detail_setting() -> dict:
    """Return the active campaign's narrative-detail setting merged with
    defaults: ``{"level": int, "label": str}``. Safe when no campaign is
    active (returns the default)."""
    level = _DETAIL_DEFAULT_LEVEL
    p = _detail_path()
    if p is not None:
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("level") in _DETAIL_LEVELS:
                level = int(loaded["level"])
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            pass
    return {"level": level, "label": _DETAIL_LEVELS[level][0]}


def set_detail_level(level: int) -> dict:
    """Persist the active campaign's narrative-detail level (0–3). Takes
    effect on the next DM turn. Returns the merged setting."""
    try:
        level = int(level)
    except (ValueError, TypeError):
        level = _DETAIL_DEFAULT_LEVEL
    if level not in _DETAIL_LEVELS:
        level = _DETAIL_DEFAULT_LEVEL
    p = _detail_path()
    if p is not None and p.parent.is_dir():
        try:
            p.write_text(json.dumps({"level": level}, indent=2), encoding="utf-8")
        except OSError:
            pass
    return {"level": level, "label": _DETAIL_LEVELS[level][0]}


def detail_choices() -> list[dict]:
    """Selectable detail levels, in order, for the dashboard slider."""
    return [{"level": lvl, "label": label}
            for lvl, (label, _body) in sorted(_DETAIL_LEVELS.items())]


def _detail_addendum() -> str | None:
    """The narrative-detail directive to inject this turn, or ``None`` for
    the Standard baseline (level 2 — nothing appended)."""
    level = detail_setting()["level"]
    body = _DETAIL_LEVELS.get(level, ("", ""))[1].strip()
    if not body:
        return None
    return _DETAIL_PREFIX + body


# --- Reasoning effort ----------------------------------------------------
# Measured on three identical turns through this pipeline: default effort ran
# 252 s / 191 s / 124 s, `--effort low` ran 47 s / 58 s / 36 s — a 4x cut,
# because ~88% of a turn's generated tokens are thinking, not prose. But low
# effort also skipped the combat pipeline on one of those turns, so it is not
# a free win: it buys speed by spending procedural diligence.
#
# Hence "auto": cheap effort for the many routine turns, full effort for the
# ones where the Hard Constraints actually bite (a live fight, a multi-NPC
# council, a multi-step plan). The composite tools carry the pipeline now
# (setup_combat, combat_round, scout, council), so a low-effort turn has far
# less procedure left to reason its way through.
_EFFORT_CHOICES = ("auto", "low", "medium", "high", "xhigh", "max")
_EFFORT_DEFAULT = "auto"

# Length above which a player message reads as a multi-step plan rather than
# a single action. Tuned from the campaign logs: routine turns run well under
# this, plans and negotiations run over it.
_PLAN_CHARS = 300

# Words that mark a turn where getting the procedure right matters: staged
# plans, social manoeuvring, and anything that owes a spell or a save.
_HIGH_EFFORT_MARKERS = (
    "ambush", "argue", "bargain", "barter", "bluff", "cast", "convince",
    "council", "deceive", "disguise", "distract", "haggle", "intimidate",
    "lie to", "negotiate", "persuade", "plan", "pretend", "ritual", "sneak",
    "surrender", "threaten", "trap", "trick",
)


def _effort_path() -> Path | None:
    slug = _active_campaign_slug()
    if not slug:
        return None
    return _REPO_ROOT / "campaigns" / slug / "_effort.json"


def effort_setting() -> dict:
    """Return the active campaign's effort setting, ``{"effort": str}``.
    Safe when no campaign is active (returns the default)."""
    effort = _EFFORT_DEFAULT
    p = _effort_path()
    if p is not None:
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("effort") in _EFFORT_CHOICES:
                effort = loaded["effort"]
        except (OSError, json.JSONDecodeError):
            pass
    return {"effort": effort}


def set_effort(effort: str) -> dict:
    """Persist the active campaign's reasoning effort. ``auto`` lets
    ``_effort_for_turn`` pick per turn; anything else pins it."""
    effort = str(effort or "").strip().lower()
    if effort not in _EFFORT_CHOICES:
        effort = _EFFORT_DEFAULT
    p = _effort_path()
    if p is not None and p.parent.is_dir():
        try:
            p.write_text(json.dumps({"effort": effort}, indent=2), encoding="utf-8")
        except OSError:
            pass
    return {"effort": effort}


def effort_choices() -> list[str]:
    """Selectable effort levels, in order, for the dashboard control."""
    return list(_EFFORT_CHOICES)


def _npc_labels() -> list[str]:
    """Lower-cased slugs and display names of every NPC the campaign knows.
    Used to spot a scene where several named NPCs are being addressed at once
    — the council case that CLAUDE.md Hard Constraint #4 governs."""
    try:
        import _campaign as _c
        cfg = _c.load_campaign()
    except Exception:
        return []
    out = []
    for slug, npc in (cfg.get("npcs") or {}).items():
        out.append(slug.lower())
        label = (npc.get("label") or "").strip().lower()
        if label:
            out.append(label)
            first = label.split()[0]
            if len(first) > 3:
                out.append(first)
    return out


def _auto_effort(message: str) -> str:
    """Pick an effort level for this turn. See the note above for why.

    Errs high: a wrongly-cheap turn can railroad, a wrongly-expensive one
    only costs seconds."""
    if _combat_active():
        return "high"

    low = (message or "").lower()
    if len(low) >= _PLAN_CHARS:
        return "high"
    if any(marker in low for marker in _HIGH_EFFORT_MARKERS):
        return "high"

    labels = _npc_labels()
    if labels:
        named = {lab for lab in labels if lab and lab in low}
        # Two distinct NPCs in one message is a scene with competing
        # interests, not a single exchange.
        if len(named) >= 2:
            return "high"
    return "low"


def _effort_for_turn(message: str) -> str | None:
    """The ``--effort`` value for this turn, or None to leave the CLI default."""
    setting = effort_setting()["effort"]
    if setting == "auto":
        return _auto_effort(message)
    return setting or None


def _recent_violations_addendum() -> str | None:
    """Build a system-prompt addendum listing recent procedural lapses the
    model should NOT repeat in its next reply. Returns ``None`` when there
    are no new violations since the last injection — keeping the addendum
    stable across quiet turns lets Anthropic's prompt cache stay warm.

    A violation is injected exactly once. The shown-finding key set
    persists in ``campaigns/<slug>/_session_audit_shown.json`` so the
    same lapse doesn't keep being re-surfaced turn after turn (which
    would force a cache miss every turn for marginal value — after the
    first injection the dashboard panel already shows it).

    Disabled by default — opt in via the dashboard audit dialog's
    "Inject into DM" toggle, which writes ``inject_addendum: true`` to
    ``campaigns/<slug>/_audit_settings.json``.
    """
    if not audit_settings().get("inject_addendum", False):
        return None
    snap = latest_audit()
    if not snap:
        return None

    # Inject lapses and the prose-audit warnings (directive language /
    # negative-knowledge / hidden-feature). Info notes (old-NPC backlog,
    # urgent clocks, etc.) stay in the dashboard panel only — those are
    # background catch-up, not urgent enough to spend system-prompt
    # tokens on every turn.
    actionable = [
        f for f in snap.get("findings", [])
        if f.get("severity") in ("lapse", "warning")
    ]
    if not actionable:
        return None

    shown = _load_shown_findings()
    new_findings = [f for f in actionable if _finding_key(f) not in shown]
    if not new_findings:
        return None

    # Cap at 8 findings so the addendum never balloons. If there's a
    # bigger backlog the dashboard panel still shows it.
    new_findings = new_findings[:8]

    lines = [
        "RECENT PROCEDURAL LAPSES — DO NOT REPEAT THESE IN YOUR NEXT REPLY:",
        "",
    ]
    for f in new_findings:
        kind = (f.get("kind") or "").replace("_", " ")
        slug = f.get("slug") or ""
        fix = (f.get("fix") or "")[:200]
        marker = f"  - [{f.get('severity', 'lapse').upper()}] {kind}"
        if slug:
            marker += f": {slug}"
        lines.append(marker)
        if fix:
            lines.append(f"    {fix}")
    lines.append("")
    lines.append(
        "These are violations of CLAUDE.md Hard Procedural Constraints from "
        "your previous turn. Address them by not repeating the pattern — "
        "specifically, do not name DM-only knowledge (module room IDs, "
        "hidden features, internal mechanisms) in player-facing narrative "
        "or OOC choice prompts."
    )

    # Mark as shown so the next quiet turn doesn't re-inject and bust
    # the cache for no new value.
    shown.update(_finding_key(f) for f in new_findings)
    _save_shown_findings(shown)

    return "\n".join(lines)

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

# Auto-reset: the resumed ``--resume`` conversation grows monotonically, so a
# long campaign session drifts past the point where every turn ships 150k+
# tokens of context (caching hides the price but not the bytes). When the live
# context crosses a threshold AND we're at a safe boundary (not mid-combat),
# the next turn silently starts a fresh session and re-primes from the state
# files via ``session_primer()`` — the system is designed to rehydrate that
# way. ``DM_AUTO_RESET=0`` disables; ``DM_AUTO_RESET_TOKENS`` tunes the trigger.
_AUTO_RESET_ENABLED = os.environ.get("DM_AUTO_RESET", "1") != "0"
# 90k, not 150k: measured round-trip cost is ~2.6 s at 42-66k of context, 3.0 s
# at 107k and 3.5 s at 150k, and a turn spends that on every one of its 5-20
# round trips. Resetting earlier keeps the whole back half of a session on the
# cheap end of that curve, and rehydration is now a single session_primer()
# call (it returns the scene, so the DM no longer reads campaign files by hand).
_AUTO_RESET_TOKENS = int(os.environ.get("DM_AUTO_RESET_TOKENS", "90000"))

# One-shot system-prompt note injected on the first turn after an auto-reset.
# It tells the (now context-free) DM that this is the SAME ongoing scene and to
# rehydrate silently — the player must never see a seam (CLAUDE.md fourth-wall
# + Harness Reminders).
_REHYDRATE_ADDENDUM = (
    "SESSION CONTINUATION — INTERNAL NOTE (never reveal this to the player):\n"
    "Your prior in-context conversation was cleared to keep context small. "
    "This is the SAME ongoing campaign and the SAME scene already in progress, "
    "NOT a new game. Before you respond to the player's message:\n"
    "  1. Call session_primer() to reload day, time, weather, party HP, open "
    "quests, active clocks, recent events, and the last-session summary.\n"
    "  2. If in-game days have passed since the last recorded event, call "
    "tick_world() as usual.\n"
    "Then continue the scene seamlessly, treating the player's message as their "
    "next action. Do NOT announce a reset, do NOT greet the player, do NOT "
    "re-introduce the setting or summarise 'previously' — just carry on the "
    "narrative exactly as if nothing changed."
)

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


# Flags for the two stateless side-passes (image-prompt rewrite, action
# suggestions). Both are short text transforms that need no campaign context,
# yet each was spawning a full-weight turn: the default model plus every MCP
# schema, measured at a ~72k-token prefix to produce four chip labels. Haiku
# with no tools and no MCP does the same job on a fraction of the latency.
_AUX_ARGS = [
    "--model", "haiku",
    "--tools", "",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--setting-sources", "",
]


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
        *_AUX_ARGS,
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
        *_AUX_ARGS,
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
    new one. Sessions in other campaigns are untouched.

    Drops the resident process too: it is bound to the old session id."""
    p = _active_session_file()
    if p is not None:
        p.unlink(missing_ok=True)
    _kill_resident()


def set_session(sid: str) -> None:
    """Pin a specific session id as the active one for the current
    campaign. Public counterpart to the implicit save inside ``stream_turn``;
    used by the dashboard's 'resume this session' button."""
    _save_session_id(sid)
    _kill_resident()


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
    slug = _active_campaign_slug()
    if slug:
        record_session_in_manifest(slug, sid)


def session_manifest_for(slug: str) -> Path:
    """Per-campaign session manifest path. Sibling to ``state.json``.

    The manifest is the source of truth for which Claude Code JSONL
    transcripts belong to this campaign — the JSONLs themselves stay in
    Claude's shared pool dir so ``--resume`` keeps working."""
    return _REPO_ROOT / "campaigns" / slug / "sessions.json"


def _load_manifest(slug: str) -> dict:
    p = session_manifest_for(slug)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sessions": []}
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        return {"sessions": []}
    return data


def _save_manifest(slug: str, data: dict) -> None:
    p = session_manifest_for(slug)
    if not p.parent.is_dir():
        return
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def record_session_in_manifest(slug: str, sid: str, created_at: str | None = None) -> None:
    """Idempotently add ``sid`` to ``campaigns/<slug>/sessions.json``.

    Existing sessions keep their original ``created_at``; new ones get
    the current UTC timestamp (or the supplied one for backfills)."""
    data = _load_manifest(slug)
    for entry in data["sessions"]:
        if isinstance(entry, dict) and entry.get("sid") == sid:
            return
    data["sessions"].append({
        "sid": sid,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    _save_manifest(slug, data)


def remove_session_from_manifest(slug: str, sid: str) -> bool:
    """Strip ``sid`` from one campaign's manifest. Returns True if removed."""
    data = _load_manifest(slug)
    before = len(data["sessions"])
    data["sessions"] = [e for e in data["sessions"]
                        if not (isinstance(e, dict) and e.get("sid") == sid)]
    if len(data["sessions"]) == before:
        return False
    _save_manifest(slug, data)
    return True


def manifest_sids(slug: str) -> list[str]:
    """Return the sids listed in this campaign's manifest, in stored order."""
    return [e.get("sid") for e in _load_manifest(slug)["sessions"]
            if isinstance(e, dict) and e.get("sid")]


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


def _system_prompt_parts(rehydrate: bool) -> list[str]:
    """The --append-system-prompt blocks for this turn, in order:

      - rehydration note when the turn follows an auto-reset (fresh session,
        re-prime via session_primer() and continue the scene seamlessly);
      - tone/voice direction (per-campaign, stable until the user edits it);
      - narrative detail level, likewise stable;
      - the audit nudge listing recent lapses not to repeat — None on quiet
        turns, which keeps the prompt cache warm.

    Also the resident process's respawn trigger: when this text changes the
    process is replaced so the new addenda actually reach the model.
    """
    parts = []
    if rehydrate:
        parts.append(_REHYDRATE_ADDENDUM)
    tone = _tone_addendum()
    if tone:
        parts.append(tone)
    detail = _detail_addendum()
    if detail:
        parts.append(detail)
    violations = _recent_violations_addendum()
    if violations:
        parts.append(violations)
    return parts


def _build_args(
    message: str | None,
    sid: str | None,
    rehydrate: bool = False,
    effort: str | None = None,
) -> list[str]:
    """Assemble the CLI argv for one turn.

    ``message=None`` builds the resident-process form: the turn arrives on
    stdin as stream-json instead of as a positional prompt, so one process can
    serve many turns (see ``_Resident``). Everything else is identical, which
    matters because the argv is what decides whether the resident can be
    reused or has to be respawned."""
    args = [_CLAUDE_BIN, "-p"]
    if message is None:
        args += ["--input-format", "stream-json"]
    else:
        args += [message]
    args += [
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--allowedTools", _ALLOWED_TOOLS,
        "--fallback-model", "sonnet",
        "--disable-slash-commands",
        "--exclude-dynamic-system-prompt-sections",
        # Default is `on`, which records the system prompt on a conversation's
        # first request and replays it verbatim on every later --resume, even
        # when a launch passes different text. Every turn here passes fresh
        # --append-system-prompt content (tone, narrative detail, the audit
        # nudge), so `on` silently discarded all of it from turn 2 onward.
        "--system-prompt-snapshot", "off",
    ]
    if effort:
        args += ["--effort", effort]
    parts = _system_prompt_parts(rehydrate)
    if parts:
        args += ["--append-system-prompt", "\n\n".join(parts)]
    if sid:
        args += ["--resume", sid]
    return args


def _session_context_tokens(sid: str) -> int | None:
    """Best-effort current context size, in tokens, for a session transcript.

    Reads the tail of the Claude Code JSONL and parses the most recent ``usage``
    record, summing the three input components (fresh + cache-creation +
    cache-read) — that total is the real context the model saw last turn, which
    the next ``--resume`` turn will build on. The raw file size is a poor proxy
    (streaming deltas inflate it ~100x), so we read the token counts directly.

    Returns ``None`` when the transcript is missing or carries no usage yet."""
    p = _project_jsonl_dir() / f"{sid}.jsonl"
    try:
        with open(p, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 256 * 1024))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    idx = tail.rfind('"usage"')
    if idx == -1:
        return None
    seg = tail[idx:idx + 600]

    def _field(name: str) -> int:
        m = re.search(r'"%s":\s*(\d+)' % name, seg)
        return int(m.group(1)) if m else 0

    total = (
        _field("input_tokens")
        + _field("cache_creation_input_tokens")
        + _field("cache_read_input_tokens")
    )
    return total or None


def _combat_active() -> bool:
    """True when the active campaign is genuinely mid-combat, so an auto-reset
    can be held until the fight ends and the DM never loses initiative order /
    tracker context mid-fight.

    A live fight needs BOTH ``active: true`` AND at least one hostile combatant
    still standing (an ``enemy``-side member above -10 HP). The second test
    matters because a forgotten ``end_combat`` leaves ``active: true`` behind
    indefinitely — without it, one stale flag would block every future reset
    and silently defeat cost control. Fails safe to True on a read error —
    better to skip a reset than to drop a real combat."""
    slug = _active_campaign_slug()
    if not slug:
        return False
    p = _REPO_ROOT / "campaigns" / slug / "combat_state.json"
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, json.JSONDecodeError):
        return True
    if not state.get("active"):
        return False
    for c in state.get("combatants", []):
        if c.get("side") != "enemy":
            continue
        hp = c.get("hp")
        if not isinstance(hp, (int, float)) or hp > -10:
            return True
    return False


def _maybe_auto_reset() -> bool:
    """Decide whether to silently start a fresh DM session before this turn.

    Fires when auto-reset is enabled, a session exists, its live context has
    crossed ``_AUTO_RESET_TOKENS``, and we're not mid-combat. On a fire it
    clears the session id (so the turn starts fresh, no ``--resume``) and
    returns True so the caller injects the rehydration primer. Stateless — the
    trigger is re-derived from the transcript each turn, so a combat simply
    holds the reset until it ends."""
    if not _AUTO_RESET_ENABLED:
        return False
    sid = session_id()
    if not sid:
        return False
    tokens = _session_context_tokens(sid)
    if tokens is None or tokens < _AUTO_RESET_TOKENS:
        return False
    if _combat_active():
        return False
    reset_session()
    return True


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


# --- Resident CLI process -------------------------------------------------
# Spawning `claude -p` per turn costs a flat ~3.0 s before the model sees a
# token: node boot, MCP server spawn, transcript replay. Measured 5.81 s of
# wall time against 2.74 s of API time on a small session. Feeding turns to
# one long-lived process over stream-json stdin instead measured 4.78 s for
# the first turn and 2.18 s wall against 2.18 s of API time for every turn
# after — i.e. the overhead goes to zero once the process is warm.
#
# The process is respawned whenever the argv would change (a new session id
# after an auto-reset, a tone/detail edit, a different effort level), which is
# also what makes the system-prompt addenda take effect: with
# --system-prompt-snapshot off, a respawn re-renders them.
_RESIDENT_ENABLED = os.environ.get("DM_PERSIST", "1") != "0"

# Ceiling on how long one turn may take before we give up on the resident and
# tear it down. Generous — a long combat turn legitimately runs minutes.
_TURN_TIMEOUT = float(os.environ.get("DM_TURN_TIMEOUT", "900"))


class _Resident:
    """A long-lived ``claude -p --input-format stream-json`` process.

    stdout and stderr are drained by daemon threads so neither can fill its
    pipe buffer and deadlock the child between turns; stdout lines land on a
    queue that ``stream_turn`` consumes with a deadline."""

    def __init__(self, args: list[str], signature: tuple, sid: str | None):
        self.args = list(args)
        self.signature = signature
        # The session this process was launched against, and the one the CLI
        # actually reported. They differ on the first turn of a new session:
        # we launch with no --resume and learn the id from the init event.
        self.sid_at_spawn = sid
        self.observed_sid: str | None = None
        self.proc = subprocess.Popen(
            args,
            cwd=_REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._lines: "queue.Queue[str | None]" = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=80)
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        try:
            for line in self.proc.stdout:  # type: ignore[union-attr]
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._lines.put(None)  # EOF sentinel

    def _pump_stderr(self) -> None:
        try:
            for line in self.proc.stderr:  # type: ignore[union-attr]
                self._stderr.append(line)
        except (OSError, ValueError):
            pass

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stderr_tail(self) -> str:
        return "".join(self._stderr)[-2000:]

    def send(self, message: str) -> None:
        payload = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": message}]},
        })
        self.proc.stdin.write(payload + "\n")  # type: ignore[union-attr]
        self.proc.stdin.flush()                # type: ignore[union-attr]

    def next_line(self, timeout: float) -> str | None:
        """Next stdout line, or None on EOF. Raises queue.Empty on timeout."""
        return self._lines.get(timeout=timeout)

    def kill(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        if self.alive():
            try:
                self.proc.kill()
            except OSError:
                pass


_resident: _Resident | None = None


def _turn_signature(rehydrate: bool, effort: str | None) -> tuple:
    """Everything except the session id that a resident process bakes in at
    spawn. A change here means the process must be replaced, not reused.

    The session id is compared separately: on the first turn of a new session
    we launch without --resume and only learn the id from the init event, so a
    naive comparison would respawn on turn 2 for no reason."""
    return (bool(rehydrate), effort, "\n\n".join(_system_prompt_parts(rehydrate)))


def _kill_resident() -> None:
    global _resident
    res, _resident = _resident, None
    if res is not None:
        res.kill()


def _ensure_resident(sid: str | None, rehydrate: bool, effort: str | None) -> _Resident | None:
    """Return a usable resident process, respawning it if the turn's argv
    changed or the old one died. None when the spawn itself failed, so the
    caller can fall back to the per-turn spawn path."""
    global _resident
    sig = _turn_signature(rehydrate, effort)
    if (_resident is not None
            and _resident.alive()
            and _resident.signature == sig
            and sid in (_resident.sid_at_spawn, _resident.observed_sid)):
        return _resident

    _kill_resident()
    args = _build_args(None, sid, rehydrate=rehydrate, effort=effort)
    try:
        _resident = _Resident(args, sig, sid)
    except OSError:
        _resident = None
    return _resident


def _stream_turn_resident(message: str, sid: str | None,
                          rehydrate: bool, effort: str | None) -> Iterator[dict]:
    """One turn against the resident process. Falls back to a fresh spawn if
    the process can't be started or dies before answering."""
    res = _ensure_resident(sid, rehydrate, effort)
    if res is None:
        yield from _spawn_turn_events(message, sid, rehydrate, effort)
        return

    with _active_proc_lock:
        global _active_proc
        _active_proc = res.proc
    _set_turn_state(in_flight=True, started_at=time.time(), tools_used=0)

    deadline = time.time() + _TURN_TIMEOUT
    try:
        res.send(message)
    except (OSError, ValueError):
        # Process died between the liveness check and the write — start over
        # once with a fresh spawn rather than dropping the player's turn.
        _kill_resident()
        _set_turn_state(in_flight=False, started_at=None, tools_used=0)
        yield from _spawn_turn_events(message, sid, rehydrate, effort)
        return

    completed = False
    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                _kill_resident()
                completed = True  # nothing left to drain
                yield {"type": "error", "returncode": None,
                       "stderr": f"DM turn exceeded {_TURN_TIMEOUT:.0f}s — resident process killed."}
                return
            try:
                line = res.next_line(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:  # the process exited
                stderr = res.stderr_tail()
                rc = res.proc.poll()
                _kill_resident()
                completed = True
                yield {"type": "error", "returncode": rc, "stderr": stderr}
                return

            evt = _consume_event(res, line)
            if evt is None:
                continue
            yield evt
            if evt.get("type") == "result":
                completed = True
                return
    finally:
        if not completed:
            # The consumer went away mid-turn — a page reload drops the SSE
            # stream and Werkzeug closes this generator. The turn is still
            # running, and its unread events would otherwise be read by the
            # *next* turn, which would then stop at this turn's result event.
            # Drain to the end, exactly as the old per-turn path blocked on
            # proc.wait(), so the transcript is written and /api/last_narrative
            # can replay the reply on reload.
            _drain_turn(res, deadline)
        with _active_proc_lock:
            if _active_proc is res.proc:
                _active_proc = None
        _set_turn_state(in_flight=False, started_at=None, tools_used=0)
        threading.Thread(target=_audit_after_turn, daemon=True).start()


def _consume_event(res: _Resident, line: str) -> dict | None:
    """Parse one stdout line and fold in its side effects: persist the session
    id, note it on the resident, and tally tool uses for the live poll."""
    evt = _process_event_line(line)
    if evt is None:
        return None
    if evt.get("type") == "system" and isinstance(evt.get("session_id"), str):
        res.observed_sid = evt["session_id"]
    _tally_tool_uses(evt)
    return evt


def _drain_turn(res: _Resident, deadline: float) -> None:
    """Read the rest of an abandoned turn's output and discard it, so the
    resident is clean for the next turn. Kills the process if the turn never
    finishes within the deadline."""
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            _kill_resident()
            return
        try:
            line = res.next_line(timeout=remaining)
        except queue.Empty:
            continue
        if line is None:
            _kill_resident()
            return
        evt = _consume_event(res, line)
        if evt is not None and evt.get("type") == "result":
            return


def _tally_tool_uses(evt: dict) -> None:
    """Count tool_use blocks for the live turn-state poll."""
    if evt.get("type") != "assistant":
        return
    content = evt.get("message", {}).get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            _bump_tools_used()


def stream_turn(message: str) -> Iterator[dict]:
    """Run one DM turn against the persisted session.

    Yields parsed JSON events as the CLI emits them. The first event is
    typically a ``system`` init record carrying ``session_id`` — that id is
    persisted automatically so the next call resumes the same conversation.

    On non-zero exit, yields a final ``{"type": "error", ...}`` event with
    the captured stderr (truncated)."""
    did_auto_reset = _maybe_auto_reset()
    if did_auto_reset:
        # The session id is gone; the resident is bound to the old one.
        _kill_resident()
    sid = session_id()
    effort = _effort_for_turn(message)

    with _turn_lock:
        if _RESIDENT_ENABLED:
            yield from _stream_turn_resident(message, sid, did_auto_reset, effort)
            return
    yield from _stream_turn_spawn(message, sid, did_auto_reset, effort)


def _stream_turn_spawn(message: str, sid: str | None,
                       rehydrate: bool = False,
                       effort: str | None = None) -> Iterator[dict]:
    """One turn in its own short-lived process, taking ``_turn_lock`` — the
    original path, used when ``DM_PERSIST=0``."""
    with _turn_lock:
        yield from _spawn_turn_events(message, sid, rehydrate, effort)


def _spawn_turn_events(message: str, sid: str | None,
                       rehydrate: bool = False,
                       effort: str | None = None) -> Iterator[dict]:
    """The per-turn spawn itself, without touching ``_turn_lock``.

    Called directly by the resident path when its process won't start or dies
    mid-turn — that path already holds the lock, and ``_turn_lock`` is not
    reentrant."""
    args = _build_args(message, sid, rehydrate=rehydrate, effort=effort)

    if True:
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
            # Fire post-turn audit on a daemon thread — never blocks SSE
            # close, never raises into the turn flow. The dashboard polls
            # the resulting snapshot via /api/audit_live.json.
            threading.Thread(target=_audit_after_turn, daemon=True).start()
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
    # Drop the resident first: a wedged turn's process IS the resident, and
    # leaving it registered would hand the next turn the same stuck process.
    _kill_resident()
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
