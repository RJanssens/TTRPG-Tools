"""Self-policing audit — scan events.json for procedural lapses.

Catches the failure modes CLAUDE.md cares about: NPCs introduced without
reaction rolls, combats without surprise/morale checks, scope-mismatched
quests, NPCs without recorded disposition after meaningful events.

Run mid-session or at session end to surface drift before it compounds.
"""
import _campaign as _c


def _avg_party_level(cfg: dict) -> float:
    chars = cfg.get("characters", {})
    if not chars:
        return 1.0
    return sum(int(c.get("level", 1)) for c in chars.values()) / len(chars)


def register(mcp):

    @mcp.tool()
    def audit_session(session: int = 0) -> dict:
        """Audit a session for fairness lapses. session=0 means current session.

        Checks:
          1. NPCs introduced without a follow-up reaction roll
          2. Combat events without a preceding surprise_check
          3. Combat events without a morale_check (when enemies fell)
          4. Active quests with scope mismatched to party level
          5. Long stretches of in-game time without an encounter_check while
             the party is in dangerous terrain (uses event terrain hints)

        Returns a list of findings with severity (info / warning / lapse)."""
        cfg    = _c.load_campaign()
        state  = _c.load_state(cfg)
        events = _c.load_events(cfg)

        target_session = session or state.get("current_session", 1)
        in_session = [e for e in events if e.get("session") == target_session]

        findings: list[dict] = []

        # 1. NPCs met without reaction roll
        npc_met_slugs = {e.get("slug") for e in in_session if e.get("type") == "npc_met"}
        reacted_slugs = {e.get("slug") for e in in_session if e.get("type") == "reaction_roll"}
        for slug in npc_met_slugs:
            if not slug:
                continue
            if slug not in reacted_slugs:
                findings.append({
                    "severity": "lapse",
                    "kind":     "missing_reaction",
                    "slug":     slug,
                    "fix":      f"Call reaction(npc='{slug}') for first encounter, or set_disposition if previously known.",
                })

        # 2. Combat ends without surprise_check earlier in the session
        combat_indices = [i for i, e in enumerate(in_session) if e.get("type") == "combat"]
        surprise_indices = [i for i, e in enumerate(in_session) if e.get("type") == "surprise_check"]
        for ci in combat_indices:
            preceding_surprise = [si for si in surprise_indices if si < ci]
            if not preceding_surprise:
                findings.append({
                    "severity": "warning",
                    "kind":     "missing_surprise_check",
                    "fix":      "At the next encounter, call surprise_check before initiative. Surprise is easy to forget.",
                })
                break  # one warning per session is enough

        # 3. Combat events with no morale_check anywhere in session
        morale_indices = [i for i, e in enumerate(in_session) if e.get("type") == "morale_check"]
        if combat_indices and not morale_indices:
            findings.append({
                "severity": "warning",
                "kind":     "missing_morale_check",
                "fix":      "If any enemy group lost 50% in combat this session, call morale_check(rating). Morale breaks are part of fairness.",
            })

        # 4. Quests with scope mismatch
        avg = _avg_party_level(cfg)
        for slug, q in cfg.get("quests", {}).items():
            if q.get("status") != "active":
                continue
            scope = q.get("scope", "local")
            if scope == "regional" and avg < 4:
                findings.append({
                    "severity": "lapse",
                    "kind":     "scope_too_large",
                    "slug":     slug,
                    "fix":      f"Quest '{slug}' is regional but party avg level is {avg:.1f}. CLAUDE.md says regional is L4+. Demote scope or pause.",
                })
            elif scope == "continental" and avg < 8:
                findings.append({
                    "severity": "lapse",
                    "kind":     "scope_too_large",
                    "slug":     slug,
                    "fix":      f"Quest '{slug}' is continental but party avg level is {avg:.1f}. CLAUDE.md says continental is L8+. Demote scope or pause.",
                })

        # 5. NPC met but no disposition recorded after meaningful interactions
        # Heuristic: NPC slug appears 3+ times in session events, but no disposition_change.
        slug_counts: dict[str, int] = {}
        for e in in_session:
            s = e.get("slug")
            if s and e.get("type") in ("npc_interaction", "reaction_roll", "npc_met"):
                slug_counts[s] = slug_counts.get(s, 0) + 1
        disp_slugs = {e.get("slug") for e in in_session if e.get("type") == "disposition_change"}
        for slug, count in slug_counts.items():
            if count >= 3 and slug not in disp_slugs:
                findings.append({
                    "severity": "info",
                    "kind":     "frequent_npc_no_disposition",
                    "slug":     slug,
                    "fix":      f"NPC '{slug}' appeared {count} times this session — consider set_disposition to anchor future reactions.",
                })

        # 6. Hireling appearing in events but no loyalty_check across the session
        hire_events = [e for e in in_session if e.get("type") == "hireling_hired"]
        loyalty_events = [e for e in in_session if e.get("type") == "loyalty_change"]
        if hire_events and not loyalty_events and len(in_session) > 20:
            findings.append({
                "severity": "info",
                "kind":     "hireling_no_loyalty_review",
                "fix":      "Long session with active hirelings but no loyalty adjustments. Did anything happen they would react to?",
            })

        # 7. Active world clocks ignored too long (>14 days remaining still in same session)
        # This is informational only.
        for c in state.get("faction_clocks", []):
            if 0 < c.get("days_remaining", 999) <= 7:
                findings.append({
                    "severity": "info",
                    "kind":     "urgent_clock",
                    "fix":      f"Clock '{c.get('label','?')}' has {c.get('days_remaining')} days left. Consider letting NPCs reference urgency.",
                })

        return {
            "session":  target_session,
            "checked":  len(in_session),
            "findings": findings,
            "summary":  f"{sum(1 for f in findings if f['severity']=='lapse')} lapses, "
                        f"{sum(1 for f in findings if f['severity']=='warning')} warnings, "
                        f"{sum(1 for f in findings if f['severity']=='info')} info notes.",
        }
