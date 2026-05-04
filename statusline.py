#!/usr/bin/env python3
"""Claude Code status line — live TTRPG campaign state."""
import json
import sys
from pathlib import Path

BASE = Path(__file__).parent
ACTIVE_FILE = BASE / ".active"
CAMPAIGNS_DIR = BASE / "campaigns"


def _hp_str(cur: int, max_hp: int) -> str:
    if max_hp == 0:
        return str(cur)
    if cur <= 0:
        return f"0/{max_hp}†"
    ratio = cur / max_hp
    if ratio <= 0.25:
        return f"!{cur}/{max_hp}"
    return f"{cur}/{max_hp}"


def main():
    try:
        ctx = json.loads(sys.stdin.read())
    except Exception:
        ctx = {}

    ctx_pct = ctx.get("context_window", {}).get("used_percentage", 0)

    try:
        if not ACTIVE_FILE.exists():
            print("No active campaign")
            return

        slug = ACTIVE_FILE.read_text().strip()
        camp_dir = CAMPAIGNS_DIR / slug

        camp_json = camp_dir / "campaign.json"
        if not camp_json.exists():
            print(f"[{slug}]")
            return

        cfg = json.loads(camp_json.read_text())
        name = cfg.get("name", slug)
        status = cfg.get("status", "active")

        if status == "closed":
            reason = cfg.get("closed_reason", "closed")
            print(f"[{name}] CLOSED ({reason})")
            return

        state_path = camp_dir / "state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}

        day = state.get("current_day", 1)
        session = state.get("current_session", 1)

        # Party HP
        chars = cfg.get("characters", {})
        char_states = state.get("characters", {})
        hp_parts = []
        for key, char in chars.items():
            max_hp = char.get("hp_max", 0)
            cur_hp = char_states.get(key, {}).get("hp", max_hp)
            label = char.get("label", key).split()[0]
            conds = char_states.get(key, {}).get("conditions", [])
            cond_str = f"({','.join(c[:3] for c in conds)})" if conds else ""
            hp_parts.append(f"{label} {_hp_str(cur_hp, max_hp)}{cond_str}")
        hp_str = " · ".join(hp_parts)

        # Current location — last location_visited event
        location_str = ""
        events_path = camp_dir / "events.json"
        if events_path.exists():
            events = json.loads(events_path.read_text())
            for event in reversed(events):
                if event.get("type") == "location_visited":
                    loc_name = event.get("name") or event.get("slug", "")
                    if loc_name:
                        location_str = f" @ {loc_name}"
                    break

        # Alerts
        in_combat = bool(state.get("combat"))
        combat_str = " [COMBAT]" if in_combat else ""
        ctx_str = f" [{ctx_pct:.0f}%ctx]" if ctx_pct >= 75 else ""

        print(f"[{name}] S{session} Day {day}{location_str}{combat_str} | {hp_str}{ctx_str}")

    except Exception as e:
        print(f"[ttrpg] err: {e}")


if __name__ == "__main__":
    main()
