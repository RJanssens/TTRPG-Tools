"""Lore and history tools: search, timelines, quests, and session primers."""
import re
import json
from pathlib import Path
import _campaign as _c


def _read_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _excerpt(text: str, pos: int, radius: int = 100) -> str:
    """Return up to 2*radius chars centred on pos, with ellipsis markers."""
    start = max(0, pos - radius)
    end   = min(len(text), pos + radius)
    snippet = text[start:end].replace("\n", " ")
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + snippet + suffix


# --- Scene rehydration -------------------------------------------------------
# session_primer() used to return day/time/HP/quests and nothing about *where
# the party is*. The DM filled the gap by shelling out to Bash — measured at 5
# reads of adventure_log.md / state.json / locations/*.md / events.jsonl on a
# session-start turn, i.e. 5 extra model round trips before a word was narrated.
# These helpers fold that whole reconstruction into the primer payload.
#
# Everything below is DM-side context, exactly like get_location() output: it
# may contain keyed/hidden material and is never narrated verbatim.

_SCENE_LOCATION_CHARS = 2500
_SCENE_AREA_CHARS     = 1200
_SCENE_LOG_CHARS      = 1800


def _tail(text: str, limit: int) -> str:
    """Last `limit` chars of text, cut back to a paragraph boundary when possible."""
    if len(text) <= limit:
        return text.strip()
    chunk = text[-limit:]
    nl = chunk.find("\n\n")
    if 0 <= nl <= limit // 3:
        chunk = chunk[nl + 2:]
    return "…\n" + chunk.strip()


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n…[truncated]"


def _current_location(cfg: dict, events: list) -> dict | None:
    """Resolve the party's current location from state.json["position"]
    (falling back to the last `location_visited` event for campaigns that
    predate it), and read its file (plus the parent area file when it has
    one)."""
    from tools.position import current_position
    pos = current_position(cfg, events=events)
    slug = pos.get("location", "")
    if not slug:
        return None

    area = pos.get("area", "") or ""
    locs = cfg["_data_dir"] / "locations"
    path = (locs / area / f"{slug}.md") if area else (locs / f"{slug}.md")

    out: dict = {
        "slug":    slug,
        "area":    area,
        "name":    pos.get("name", slug),
        "day":     (pos.get("since") or {}).get("day"),
    }
    if path.exists():
        out["content"] = _clip(_read_safe(path), _SCENE_LOCATION_CHARS)
    else:
        out["content"] = ""
        out["note"] = f"No location file at locations/{area + '/' if area else ''}{slug}.md"

    if area:
        area_file = locs / f"{area}.md"
        if area_file.exists():
            out["area_content"] = _clip(_read_safe(area_file), _SCENE_AREA_CHARS)
    return out


def _light_block(state: dict) -> dict:
    """Same reading as light_state(), inlined so the primer is one file read."""
    lit = []
    spent = 0
    for s in state.get("light_sources", []):
        entry = {
            "holder":            s.get("holder", ""),
            "type":              s.get("type", s.get("source_type", "")),
            "minutes_remaining": s.get("minutes_remaining", 0),
        }
        if s.get("lit"):
            entry["status"] = "guttering" if 0 <= entry["minutes_remaining"] <= 5 else "burning"
            lit.append(entry)
        elif entry["minutes_remaining"] <= 0:
            # A long dungeon crawl accumulates dozens of burnt-out stubs; the
            # DM only needs the count, not the list.
            spent += 1
    return {"any_lit": bool(lit), "lit": lit, "burnt_out_count": spent}


def _combat_block(cfg: dict) -> dict | None:
    """Live combat snapshot from combat_state.json, or None when no fight is up.

    A forgotten end_combat leaves `active: true` behind, so report the flag as
    stored and let the DM see the combatant list rather than guessing."""
    p = cfg["_dir"] / "combat_state.json"
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not st.get("active"):
        return None
    return {
        "active": True,
        "round":  st.get("round"),
        "combatants": [
            {
                "name":       c.get("name"),
                "side":       c.get("side"),
                "hp":         c.get("hp"),
                "hp_max":     c.get("hp_max"),
                "init":       c.get("init"),
                "current":    bool(c.get("current")),
                "conditions": c.get("conditions", []),
            }
            for c in st.get("combatants", [])
        ],
    }


def _position_block(cfg: dict, state: dict, events: list) -> dict:
    """The party's position, plus the authored room and its notes when the
    current locale is a campaign map. Never raises on a dead map service —
    `where` still reports the party's location even when the room lookup
    can't be made."""
    from tools import dungml_play as dp
    from tools.position import current_position
    from tools.rooms import notes_for
    pos = current_position(cfg, state, events)
    block: dict = {"where": pos, "room": None, "room_notes": []}
    link = dp.link_state(cfg)
    if pos.get("node") and pos.get("map_id"):
        block["room_notes"] = notes_for(cfg, pos["map_id"], pos["node"])
        if link["source"] == "campaign" and link["map_id"] == pos["map_id"]:
            try:
                block["room"] = dp.campaign_room(dp.external_id(cfg), pos["map_id"], pos["node"])
            except dp.DungmlUnavailable as e:
                block["map_error"] = dp.describe_error(e)["error"]
    return block


def _scene_block(cfg: dict, state: dict, events: list) -> dict:
    """Everything the DM needs to resume a scene without reading files."""
    log_file = cfg["_data_dir"] / cfg.get("session_log_file", "adventure_log.md")
    scene: dict = {
        "location":         _current_location(cfg, events),
        "recent_narrative": _tail(_read_safe(log_file), _SCENE_LOG_CHARS) if log_file.exists() else "",
        "light":            _light_block(state),
        "position":         _position_block(cfg, state, events),
    }
    combat = _combat_block(cfg)
    if combat:
        scene["combat"] = combat

    conditions = {}
    for key, cs in (state.get("characters") or {}).items():
        conds = cs.get("conditions") or []
        if conds:
            conditions[key] = list(conds)
    if conditions:
        scene["conditions"] = conditions

    # Consumables live at state["consumables"][key], not under characters.
    consumables = {
        key: {k: v for k, v in bag.items() if v}
        for key, bag in (state.get("consumables") or {}).items()
        if any(bag.values())
    }
    if consumables:
        scene["consumables"] = consumables
    return scene


def _open_quests(cfg: dict, events: list | None = None) -> tuple[list, str]:
    """Open quests, oldest first, and which store they came from.

    Prefers the structured campaign.json[quests] schema (add_quest /
    update_quest); falls back to legacy quest_update events for campaigns
    that predate it."""
    structured = cfg.get("quests", {})
    quests = []
    if structured:
        for slug, q in structured.items():
            if q.get("status", "active") != "active":
                continue
            log = q.get("notes_log", [])
            quests.append({
                "slug":   slug,
                "title":  q.get("title", slug),
                "scope":  q.get("scope", ""),
                "stakes": q.get("stakes", ""),
                "giver":  q.get("giver", ""),
                "notes":  log[-1].get("text", "") if log else "",
                "day":    q.get("started_day", 0),
            })
        quests.sort(key=lambda q: q["day"])
        return quests, "structured"

    if events is None:
        events = _c.load_events(cfg)
    quest_events: dict[str, list[dict]] = {}
    for e in events:
        if e.get("type") == "quest_update":
            quest_events.setdefault(e.get("slug", ""), []).append(e)
    for slug, evts in quest_events.items():
        if any(e.get("status") in ("complete", "failed") for e in evts):
            continue
        latest = max(evts, key=lambda e: (e.get("day", 0), e.get("session", 0)))
        quests.append({
            "slug":  slug,
            "notes": latest.get("notes", ""),
            "day":   latest.get("day", 0),
        })
    quests.sort(key=lambda q: q["day"])
    return quests, "events"


def register(mcp):

    @mcp.tool()
    def search_lore(query: str) -> dict:
        """Case-insensitive search across chronicle, character files, location files, and events.
        Returns up to 20 results with source and excerpt for each hit."""
        cfg   = _c.load_campaign()
        q     = query.lower()
        results: list[dict] = []

        def _search_text(text: str, source: str):
            for m in re.finditer(re.escape(q), text, re.IGNORECASE):
                if len(results) >= 20:
                    return
                line_no = text[:m.start()].count("\n") + 1
                results.append({
                    "source":  f"{source}:{line_no}",
                    "excerpt": _excerpt(text, m.start()),
                })

        # 1. Chronicle / session log
        log_file = cfg["_data_dir"] / cfg.get("session_log_file", "adventure_log.md")
        if log_file.exists():
            _search_text(_read_safe(log_file), log_file.name)

        # 2. Character files
        chars_dir = cfg["_data_dir"] / "characters"
        if chars_dir.exists():
            for md in sorted(chars_dir.rglob("*.md")):
                if len(results) >= 20:
                    break
                _search_text(_read_safe(md), f"characters/{md.name}")

        # 3. Location files (recursive)
        locs_dir = cfg["_data_dir"] / "locations"
        if locs_dir.exists():
            for md in sorted(locs_dir.rglob("*.md")):
                if len(results) >= 20:
                    break
                rel = md.relative_to(cfg["_data_dir"])
                _search_text(_read_safe(md), str(rel))

        # 4. Events JSON (search "notes", "result", "name" fields)
        for idx, event in enumerate(_c.load_events(cfg)):
            if len(results) >= 20:
                break
            for field in ("notes", "result", "name"):
                val = event.get(field, "")
                if val and q in str(val).lower():
                    results.append({
                        "source":  f"events.json[{idx}]",
                        "excerpt": str(val)[:200],
                    })
                    break  # one hit per event is enough

        return {"query": query, "results": results}

    @mcp.tool()
    def npc_history(slug: str) -> dict:
        """Return all events associated with an NPC slug, sorted by day then session."""
        cfg    = _c.load_campaign()
        events = [e for e in _c.load_events(cfg) if e.get("slug") == slug]
        events.sort(key=lambda e: (e.get("day", 0), e.get("session", 0)))
        return {"slug": slug, "events": events}

    @mcp.tool()
    def location_history(slug: str, area: str = "") -> dict:
        """Return all events for a location slug (and optional area), sorted by day.
        slug: location slug
        area: parent area slug, or empty for top-level"""
        cfg    = _c.load_campaign()
        events = [
            e for e in _c.load_events(cfg)
            if e.get("slug") == slug and e.get("area", "") == area
        ]
        events.sort(key=lambda e: (e.get("day", 0), e.get("session", 0)))
        return {"slug": slug, "area": area, "events": events}

    @mcp.tool()
    def active_quests() -> dict:
        """Return open quests. Prefers the structured campaign.json[quests]
        schema (use add_quest / update_quest); falls back to legacy
        quest_update events for older campaigns."""
        quests, source = _open_quests(_c.load_campaign())
        return {"quests": quests, "source": source}

    @mcp.tool()
    def party_timeline(session_from: int = 1, session_to: int = 0) -> dict:
        """Return all events within a session range, sorted by day and session.
        session_from: first session to include (default 1)
        session_to: last session to include, 0 means no upper bound"""
        cfg    = _c.load_campaign()
        events = _c.load_events(cfg)

        filtered = []
        for e in events:
            s = e.get("session", 1)
            if s < session_from:
                continue
            if session_to and s > session_to:
                continue
            filtered.append(e)

        filtered.sort(key=lambda e: (e.get("day", 0), e.get("session", 0)))
        return {"events": filtered}

    @mcp.tool()
    def session_primer(scene: bool = True) -> dict:
        """Generate a session-start brief — everything needed to resume play
        without reading a single campaign file by hand.

        Returns current day/time/weather, the full party digest (HP, AC, THAC0,
        class/level, conditions, slots, memorised spells), recent events, active
        quests and world clocks, the last session heading, the prior session's
        audit findings, and the narrative detail level.

        With ``scene=True`` (the default) it also returns a ``scene`` block:
        the party's current location file (and its parent area), the tail of the
        chronicle, light sources, live combat state, conditions and consumables.
        That block is DM-side context — like get_location() it can carry keyed
        or hidden material, so read it silently and never narrate it verbatim.

        Pass ``scene=False`` for the lean brief (no file reads) when you only
        need the numbers."""
        cfg   = _c.load_campaign()
        state = _c.load_state(cfg)

        # --- Party status ---
        # Carries the canonical mechanics too: the DM otherwise follows the
        # primer with a get_character() per PC (~1.8k tokens each) just to
        # learn AC/THAC0/level. See CLAUDE.md "Impartiality Rules" — read
        # stats, never archetype-guess.
        party = {}
        chars_cfg = cfg.get("characters", {})
        chars_state = state.get("characters", {})
        for key, char in chars_cfg.items():
            cs = chars_state.get(key, {})
            entry: dict = {
                "label":   char.get("label", key),
                "hp":      cs.get("hp", char.get("hp_max")),
                "hp_max":  char.get("hp_max"),
            }
            for field, src in (("race", "race"), ("class", "cls"), ("level", "level"),
                               ("ac", "ac"), ("thac0", "thac0"),
                               ("weapon", "weapon")):
                if char.get(src) is not None:
                    entry[field] = char[src]
            conds = cs.get("conditions") or []
            if conds:
                entry["conditions"] = list(conds)
            slots = cs.get("spell_slots")
            if slots:
                entry["spell_slots"] = slots
            # add_character stores bare names; memorize_spells stores dicts.
            memorized = [
                m if isinstance(m, str) else m.get("name")
                for m in (cs.get("memorized_spells") or [])
                if isinstance(m, str) or (isinstance(m, dict) and not m.get("cast"))
            ]
            memorized = [m for m in memorized if m]
            if memorized:
                entry["memorized_uncast"] = memorized
            party[key] = entry

        # --- Recent events ---
        # Eight rather than three: the extra five are a few hundred tokens and
        # they usually spare the DM a party_timeline() round trip.
        events = _c.load_events(cfg)
        last_events = events[-8:]

        # --- Active quests (the same list active_quests() returns) ---
        open_quests, _ = _open_quests(cfg, events)

        # --- Last session heading from chronicle ---
        log_file = cfg["_data_dir"] / cfg.get("session_log_file", "adventure_log.md")
        last_session_title = None
        if log_file.exists():
            text = _read_safe(log_file)
            # Match any ### heading (session entries typically use ### Session N — Title)
            headings = re.findall(r"^#{1,3}\s+(.+)$", text, re.MULTILINE)
            if headings:
                last_session_title = headings[-1].strip()

        # --- Active world clocks (urgent first) ---
        clocks = state.get("faction_clocks", [])
        active_clocks = sorted(
            [c for c in clocks if c.get("days_remaining", 0) > 0],
            key=lambda c: c.get("days_remaining", 999),
        )[:5]

        # --- Time of day ---
        h = state.get("current_hour", 6)
        m = state.get("current_minute", 0)
        time_str = f"{h:02d}:{m:02d}"

        # --- Audit findings from the prior session ---
        # Surfaces procedural lapses (missing reactions, freehand combat,
        # scope creep, etc.) at session start so the DM can address them
        # before adding new content. See tools/audit.py for the full
        # check list and CLAUDE.md "Audit" for usage guidance.
        current_session = state.get("current_session", 1)
        prior_session = max(1, current_session - 1)
        try:
            from tools.audit import _run_audit
            prior_audit = _run_audit(prior_session)
        except Exception as exc:  # pragma: no cover - audit must never break primer
            prior_audit = {"error": str(exc), "findings": []}

        # --- Narrative detail level (how much raw mechanical detail to expose
        # in player-facing prose; set from the dashboard, honoured every turn) ---
        try:
            from tools.dm_session import detail_setting
            narrative_detail = detail_setting()
        except Exception:
            narrative_detail = {"level": 2, "label": "Standard"}

        out = {
            "current_day":         state.get("current_day", 1),
            "current_time":        time_str,
            "current_session":     current_session,
            "current_weather":     state.get("current_weather", ""),
            "party":               party,
            # Front to back; the roster order the /play sidebar shows.
            "marching_order":      [c.get("label", k) for k, c in chars_cfg.items()],
            "last_events":         last_events,
            "active_quests":       open_quests,
            "active_clocks":       active_clocks,
            "last_session_title":  last_session_title,
            "prior_session_audit": prior_audit,
            "narrative_detail":    narrative_detail,
        }
        if scene:
            out["scene"] = _scene_block(cfg, state, events)
        return out
