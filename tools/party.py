"""Party and character management tools: HP, XP, spells, rest, coin."""
import re
import sqlite3
from pathlib import Path

import _campaign as _c
from tools import class_xp as _xp

_2E_DB = Path(__file__).resolve().parent.parent / "global" / "2e.db"

# Priest-side and wizard-side class keywords, for disambiguating a spell name
# that exists on both lists at different levels.
_PRIEST_CLASSES = ("cleric", "priest", "druid", "paladin", "ranger", "shaman")
_WIZARD_CLASSES = ("mage", "wizard", "illusionist", "magic-user", "sorcerer", "bard")

# Warrior classes get the parenthetical (higher) Constitution HP bonus.
_WARRIOR_CLASSES = ("fighter", "paladin", "ranger", "barbarian")

_spell_level_cache: dict[tuple[str, str], int | None] = {}


def _caster_kind(cls: str) -> str:
    """'priest', 'wizard', or '' when the class says nothing useful."""
    low = (cls or "").lower()
    if any(k in low for k in _PRIEST_CLASSES):
        return "priest"
    if any(k in low for k in _WIZARD_CLASSES):
        return "wizard"
    return ""


def _spell_level(name: str, caster_kind: str = "") -> int | None:
    """Look a spell's level up in global/2e.db. None when it isn't found.

    Memorised spells used to be stored with level 0 whenever memorize_spells
    was handed bare names — which it usually is. cast_spell then skipped its
    slot decrement entirely (`if spell_level:` on a 0), so casting cost nothing
    and the whole spell economy was free. Levels are resolved here instead of
    being asked of the caller. Cached: the spell DB is read-only at runtime."""
    key = (name.lower().strip(), caster_kind)
    if key in _spell_level_cache:
        return _spell_level_cache[key]
    level = None
    if _2E_DB.exists() and key[0]:
        try:
            conn = sqlite3.connect(str(_2E_DB))
            params: list = [name.strip()]
            caster_sql = ""
            if caster_kind:
                caster_sql = " AND caster = ?"
                params.append(caster_kind)
            row = conn.execute(
                f"SELECT level FROM spells WHERE name = ? COLLATE NOCASE{caster_sql} LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                # Fall back to a prefix match, shortest name first, so
                # "cure light wounds" doesn't land on "Cure Light Wounds, Mass".
                params = [f"{name.strip()}%"]
                if caster_kind:
                    params.append(caster_kind)
                row = conn.execute(
                    f"SELECT level FROM spells WHERE name LIKE ? COLLATE NOCASE{caster_sql} "
                    f"ORDER BY length(name) LIMIT 1",
                    params,
                ).fetchone()
            if row is None and caster_kind:
                # Known to the books but not on this class's list — take the
                # level anyway rather than leaving it at 0.
                row = conn.execute(
                    "SELECT level FROM spells WHERE name = ? COLLATE NOCASE LIMIT 1",
                    (name.strip(),),
                ).fetchone()
            conn.close()
            if row is not None and row[0] is not None:
                level = int(row[0])
        except Exception:
            level = None
    _spell_level_cache[key] = level
    return level


def _con_hp_bonus(char: dict) -> int:
    """The character's per-level Constitution hit-point bonus, used for the
    weekly bed-rest bonus (PHB: "for each complete week of bed rest, add any
    Constitution hit point bonus to the base of 21"). Warriors get the
    parenthetical value. 0 when CON is unknown."""
    ab = char.get("ability_scores") or char.get("abilities") or {}
    con = ab.get("con") or ab.get("constitution")
    if con is None or not _2E_DB.exists():
        return 0
    try:
        conn = sqlite3.connect(str(_2E_DB))
        row = conn.execute(
            "SELECT data FROM ability_scores WHERE ability='constitution' AND score=? LIMIT 1",
            (str(con),),
        ).fetchone()
        conn.close()
    except Exception:
        return 0
    if not row:
        return 0
    import json as _json
    try:
        adj = str((_json.loads(row[0]) or {}).get("hp_adj", "0"))
    except Exception:
        return 0
    # "+2(+3)" — plain value, then the warrior-only value in brackets.
    warrior = any(k in (char.get("cls") or "").lower() for k in _WARRIOR_CLASSES)
    m = re.search(r'\(([+-]?\d+)\)', adj)
    if warrior and m:
        return int(m.group(1))
    m = re.match(r'\s*([+-]?\d+)', adj)
    return int(m.group(1)) if m else 0


def _normalize_memorized(spells: list, caster_kind: str = "") -> list:
    """Ensure memorized_spells is a list of {name, level, cast} dicts.

    Accepts flat strings (legacy) or existing dicts. A missing or zero level is
    resolved against global/2e.db, so a caller passing bare spell names still
    ends up with levels that cast_spell can charge a slot against. Entries the
    DB doesn't recognise keep level 0 and are reported by memorize_spells."""
    result = []
    for s in spells:
        if isinstance(s, dict):
            name = s.get("name", "")
            level = int(s.get("level") or 0)
            cast = bool(s.get("cast", False))
        else:
            name, level, cast = str(s), 0, False
        if not level and name:
            level = _spell_level(name, caster_kind) or 0
        result.append({"name": name, "level": level, "cast": cast})
    return result


def register(mcp):

    @mcp.tool()
    def party_status() -> dict:
        """Return HP, conditions, spell slots, coin, and current day for all characters."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        result = {
            "campaign": cfg.get("name", ""),
            "current_day": state.get("current_day", 1),
            "current_session": state.get("current_session", 0),
            "coin": state.get("coin", {"pp": 0, "gp": 0, "ep": 0, "sp": 0, "cp": 0}),
            "characters": {},
        }

        for key, char in cfg.get("characters", {}).items():
            cstate = state.get("characters", {}).get(key, {})
            hp_max = char.get("hp_max", 0)
            hp_cur = cstate.get("hp", hp_max)

            # Build spell slot summary
            slot_cfg = char.get("spell_slots") or {}
            slot_state = cstate.get("spell_slots") or {}
            slots_summary = {}
            for lvl, max_count in slot_cfg.items():
                used = max_count - slot_state.get(lvl, max_count)
                slots_summary[f"L{lvl}"] = f"{slot_state.get(lvl, max_count)}/{max_count} remaining"

            result["characters"][key] = {
                "label": char.get("label", key),
                "class": char.get("cls", ""),
                "hp": f"{hp_cur}/{hp_max}",
                "conditions": cstate.get("conditions", []),
                "xp": cstate.get("xp", 0),
                "spell_slots": slots_summary,
            }

        return result

    @mcp.tool()
    def set_marching_order(order: list[str]) -> dict:
        """Set the party's marching order, front to back.

        order: character names or keys. Anyone left out falls in behind, in
        their current order. The marching order is the roster order — the
        same sequence the /play sidebar shows and the player rearranges with
        its ▴/▾ handles — so read it (session_primer's `marching_order`)
        before ruling who is in front, who a trap hits first, or who a rear
        attacker can reach."""
        cfg = _c.load_campaign()
        keys: list[str] = []
        for name in order:
            key, _ = _c.char_key_for(cfg, str(name))
            if key is None:
                return {"error": f"Unknown character '{name}'.",
                        "party": [c["label"] for c in cfg.get("characters", {}).values()]}
            keys.append(key)
        new_order = _c.reorder_characters(cfg, keys)
        _c.save_campaign(cfg)
        labels = [cfg["characters"][k]["label"] for k in new_order]
        _c.append_event(cfg, {"type": "marching_order", "order": labels,
                              "note": "Marching order: " + ", ".join(labels)})
        return {"marching_order": labels}

    @mcp.tool()
    def character_status(character: str) -> dict:
        """Full status for one character: HP, XP, conditions, spell slots, memorized spells, saves, skills."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        cstate = state.get("characters", {}).get(key, {})
        hp_max = char.get("hp_max", 0)
        hp_cur = cstate.get("hp", hp_max)

        slot_cfg = char.get("spell_slots") or {}
        slot_state = cstate.get("spell_slots") or {}
        slots_detail = {}
        for lvl, max_count in slot_cfg.items():
            remaining = slot_state.get(lvl, max_count)
            slots_detail[f"level_{lvl}"] = {"remaining": remaining, "max": max_count}

        save_names = ["Paralysis/Poison/Death", "Rod/Staff/Wand", "Polymorph/Petrify", "Breath Weapon", "Spell/Magic"]
        saves_norm = _c.normalize_saves(char.get("saves"))
        by_type = {s["type"]: s["value"] for s in saves_norm}
        defaults = [16, 18, 17, 20, 19]
        saves_named = {save_names[i]: by_type.get(_c.SAVE_TYPES[i], defaults[i]) for i in range(5)}

        return {
            "key": key,
            "label": char.get("label", key),
            "class": char.get("cls", ""),
            "race": char.get("race", ""),
            "hp": {"current": hp_cur, "max": hp_max},
            "ac": char.get("ac", 10),
            "thac0": char.get("thac0", 20),
            "weapon": char.get("weapon", "1d6"),
            "weapon_speed": char.get("weapon_speed", 5),
            "bonus_hit": char.get("bonus_hit", 0),
            "bonus_dmg": char.get("bonus_dmg", 0),
            "xp": cstate.get("xp", 0),
            "conditions": cstate.get("conditions", []),
            "spell_slots": slots_detail,
            "memorized_spells": cstate.get("memorized_spells", []),
            "saves": saves_named,
            "skills": char.get("skills", {}),
        }

    @mcp.tool()
    def apply_damage(character: str, amount: int) -> dict:
        """Reduce a character's HP outside combat.
        0 = unconscious (stable). -1 to -9 = bleeding (1 HP/round). -10 = dead."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        chars_state = state.setdefault("characters", {})
        cstate = chars_state.setdefault(key, {})
        hp_max    = char.get("hp_max", 0)
        hp_before = cstate.get("hp", hp_max)
        hp_after  = max(hp_before - amount, -10)
        cstate["hp"] = hp_after

        downed = hp_after <= 0
        dead   = hp_after <= -10
        if downed and "downed" not in cstate.get("conditions", []):
            cstate.setdefault("conditions", []).append("downed")

        _c.save_state(cfg, state)

        return {
            "character": char.get("label", key),
            "damage":    amount,
            "hp_before": hp_before,
            "hp_after":  hp_after,
            "downed":    downed,
            "dead":      dead,
        }

    @mcp.tool()
    def apply_heal(character: str, amount: int) -> dict:
        """Increase a character's HP, capped at hp_max.
        Also syncs with the active combat session if one is running."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        chars_state = state.setdefault("characters", {})
        cstate  = chars_state.setdefault(key, {})
        hp_max  = char.get("hp_max", 0)
        hp_before = cstate.get("hp", hp_max)
        hp_after  = min(hp_before + amount, hp_max)
        cstate["hp"] = hp_after

        if hp_after > 0 and "downed" in cstate.get("conditions", []):
            cstate["conditions"].remove("downed")

        _c.save_state(cfg, state)

        # Sync with active combat session — and persist combat_state.json so
        # the dashboard sidebar and statusline (which read live HP from that
        # file) reflect the heal immediately. Without the persist call the
        # in-memory tracker would be correct but the on-disk file stale.
        combat_synced = False
        try:
            from tools import combat as _combat
            sess = _combat.get_session()
            if sess:
                for c in sess["combatants"]:
                    if c.get("_key") == key:
                        c["hp"] = min(c["hp"] + amount, hp_max)
                        combat_synced = True
                        break
                if combat_synced:
                    _combat._persist_session()
        except Exception:
            pass

        return {
            "character":     char.get("label", key),
            "healed":        hp_after - hp_before,
            "hp_before":     hp_before,
            "hp_after":      hp_after,
            "combat_synced": combat_synced,
        }

    @mcp.tool()
    def memorize_spells(character: str, spells: list) -> dict:
        """Declare the spell list a caster has prepared after rest.
        Each entry is a spell name string or {name, level} dict.
        All spells start uncast. Validates against per-level slot counts.

        Examples:
          ["sleep"]
          [{"name": "sleep", "level": 1}, {"name": "magic missile", "level": 1}]
        """
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        slot_cfg = char.get("spell_slots") or {}
        normalized = _normalize_memorized(spells, _caster_kind(char.get("cls", "")))

        # Validate total
        total_capacity = sum(slot_cfg.values()) if slot_cfg else 0
        if len(normalized) > total_capacity:
            return {"error": f"Too many spells: {len(normalized)} given, {total_capacity} total slots."}

        # Validate per-level for spells with known levels
        level_counts: dict[str, int] = {}
        for s in normalized:
            if s["level"]:
                k = str(s["level"])
                level_counts[k] = level_counts.get(k, 0) + 1
        for lvl_key, count in level_counts.items():
            cap = slot_cfg.get(lvl_key, 0)
            if count > cap:
                return {"error": f"Too many level-{lvl_key} spells: {count} given, {cap} slots available."}

        cstate = state.setdefault("characters", {}).setdefault(key, {})
        cstate["memorized_spells"] = normalized
        _c.save_state(cfg, state)

        by_level = {}
        for s in normalized:
            k = str(s["level"]) if s["level"] else "?"
            by_level.setdefault(k, []).append(s["name"])

        out = {
            "character": char.get("label", key),
            "memorized": [s["name"] for s in normalized],
            "by_level": by_level,
        }
        unresolved = [s["name"] for s in normalized if not s["level"]]
        if unresolved:
            out["unresolved_levels"] = unresolved
            out["warning"] = (
                f"No spell level found for {unresolved} — casting these will not "
                f"consume a slot. Pass them as {{'name': ..., 'level': N}} if they "
                f"are homebrew or from a source outside global/2e.db."
            )
        return out

    @mcp.tool()
    def cast_spell(character: str, spell_name: str) -> dict:
        """Cast a memorized spell by name.

        Marks the first uncast instance as spent AND decrements its slot — this
        is the whole bookkeeping for a memorised cast. Do not follow it with
        use_spell_slot(); that would charge the slot twice.

        Returns an error if the spell is not memorized or every instance is
        already cast."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        cstate = state.setdefault("characters", {}).setdefault(key, {})
        memorized = _normalize_memorized(cstate.get("memorized_spells", []),
                                         _caster_kind(char.get("cls", "")))
        needle = spell_name.lower().strip()

        # Find first uncast instance
        target = next(
            (i for i, s in enumerate(memorized) if s["name"].lower() == needle and not s["cast"]),
            None,
        )
        if target is None:
            already_cast = any(s["name"].lower() == needle and s["cast"] for s in memorized)
            if already_cast:
                return {"error": f"'{spell_name}' has already been cast. All memorized instances are spent."}
            return {"error": f"'{spell_name}' is not in {char.get('label', key)}'s memorized spells."}

        spell = memorized[target]
        spell_level = spell["level"]

        # Decrement slot
        slot_cfg = char.get("spell_slots") or {}
        slot_state = cstate.setdefault("spell_slots", dict(slot_cfg))
        if spell_level:
            lvl_key = str(spell_level)
            remaining = slot_state.get(lvl_key, 0)
            if remaining <= 0:
                return {"error": f"No level {spell_level} slots remaining — cannot cast '{spell_name}'."}
            slot_state[lvl_key] = remaining - 1

        memorized[target]["cast"] = True
        cstate["memorized_spells"] = memorized
        _c.save_state(cfg, state)
        slot_note = (
            f"Level {spell_level} slot spent ({slot_state.get(str(spell_level))} left)."
            if spell_level else
            "No level on record for this spell, so no slot was charged — "
            "re-memorise it with an explicit level if that is wrong."
        )

        available = [s["name"] for s in memorized if not s["cast"]]
        return {
            "character": char.get("label", key),
            "cast": spell["name"],
            "level": spell_level or "unknown",
            "slot_note": slot_note,
            "slots_remaining": {k: v for k, v in slot_state.items()},
            "available_spells": available,
        }

    @mcp.tool()
    def spell_status(character: str) -> dict:
        """Return the full spell state: available (uncast) and spent spells, with slot counts."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        cstate = state.get("characters", {}).get(key, {})
        memorized = _normalize_memorized(cstate.get("memorized_spells", []),
                                         _caster_kind(char.get("cls", "")))

        slot_cfg  = char.get("spell_slots") or {}
        slot_state = cstate.get("spell_slots") or {}

        slots = {
            f"level_{lvl}": {"remaining": slot_state.get(str(lvl), max_c), "max": max_c}
            for lvl, max_c in slot_cfg.items()
        }

        return {
            "character": char.get("label", key),
            "available": [s["name"] for s in memorized if not s["cast"]],
            "spent":     [s["name"] for s in memorized if s["cast"]],
            "slots":     slots,
        }

    @mcp.tool()
    def use_spell_slot(character: str, level: int = 1) -> dict:
        """Decrement a slot by level without naming a spell.

        For casts that bypass the memorised list: an innate ability, a scroll
        or wand that burns a slot, a homebrew effect. For an ordinary
        memorised spell use cast_spell(), which marks the spell AND spends the
        slot — calling both charges the slot twice."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        cstate = state.setdefault("characters", {}).setdefault(key, {})
        slot_cfg  = char.get("spell_slots") or {}
        lvl_key   = str(level)
        max_slots = slot_cfg.get(lvl_key, 0)
        if max_slots == 0:
            return {"error": f"{char.get('label', key)} has no level {level} spell slots."}

        slot_state = cstate.setdefault("spell_slots", dict(slot_cfg))
        current = slot_state.get(lvl_key, max_slots)
        if current <= 0:
            return {"error": f"No level {level} slots remaining for {char.get('label', key)}."}

        slot_state[lvl_key] = current - 1

        # Mark first uncast spell of matching level (or any level if unknown)
        memorized = _normalize_memorized(cstate.get("memorized_spells", []),
                                         _caster_kind(char.get("cls", "")))
        removed = None
        for s in memorized:
            if not s["cast"] and (s["level"] == level or s["level"] == 0):
                s["cast"] = True
                removed = s["name"]
                break

        cstate["memorized_spells"] = memorized
        _c.save_state(cfg, state)

        return {
            "character": char.get("label", key),
            "level": level,
            "slots_remaining": slot_state,
            "spell_marked_cast": removed,
            "available_spells": [s["name"] for s in memorized if not s["cast"]],
        }

    @mcp.tool()
    def rest(
        character:  str = "",
        days:       int = 1,
        bed_rest:   bool = False,
        healer:     str = "",
        herbalism:  bool = False,
        provisioned: bool = True,
        full:       bool = False,
    ) -> dict:
        """Rest for a number of days, healing at the by-the-book rate.

        PHB natural healing: **1 hit point per day of rest**, where rest means
        low activity — nothing more strenuous than riding or travelling. With
        **complete bed rest** (doing nothing at all for the day) it is 3 hp per
        day, and for each complete week of bed rest the character also adds
        their Constitution hit point bonus. Nothing is regained on a day
        without adequate food, water and sleep.

        Under the care of someone with the Healing proficiency: 1 hp/day even
        while travelling, 2 hp/day with complete rest, or 3 hp/day if the carer
        has both Healing and Herbalism. Where the attended and unattended rates
        overlap the better of the two applies (the PHB's two passages cross;
        a healer never makes a patient heal slower).

        days:        days of rest (default 1)
        bed_rest:    complete bed rest rather than light activity
        healer:      name of a character with Healing proficiency tending the
                     party — cares for up to six patients
        herbalism:   that carer also has Herbalism
        provisioned: False if food/water/sleep were inadequate — no HP that day
        full:        DM override. Restores everyone to maximum, the pre-2e-rules
                     behaviour. Not the book rule; use it deliberately.

        Spell slots and memorised-spell availability reset on any rest of a day
        or more. Does NOT auto-memorize spells — call memorize_spells after.

        Magical healing is apply_heal(); this is bed and time."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        days = max(0, int(days))
        chars_state = state.setdefault("characters", {})

        target_keys = list(cfg.get("characters", {}).keys()) if not character else None
        if character:
            key, char = _c.char_key_for(cfg, character)
            if key is None:
                return {"error": f"Character '{character}' not found."}
            target_keys = [key]

        # Rate per day, before the weekly Constitution bonus.
        natural = 3 if bed_rest else 1
        if healer:
            care = 3 if (bed_rest and herbalism) else (2 if bed_rest else 1)
            rate = max(natural, care)
        else:
            rate = natural
        if not provisioned:
            rate = 0

        healed_report = {}
        rested = []
        for key in target_keys:
            char = cfg["characters"].get(key, {})
            cstate = chars_state.setdefault(key, {})
            hp_max = char.get("hp_max", 0)
            before = cstate.get("hp", hp_max)

            if full:
                after = hp_max
                gained = after - before
                con_bonus = 0
            else:
                gained = rate * days
                con_bonus = 0
                if bed_rest and provisioned and days >= 7:
                    con_bonus = _con_hp_bonus(char) * (days // 7)
                    gained += con_bonus
                # Healing stops at maximum, and the dead do not convalesce.
                after = before if before <= -10 else min(hp_max, before + gained)
                gained = after - before

            cstate["hp"] = after
            healed_report[char.get("label", key)] = {
                "hp_before": before,
                "hp_after":  after,
                "healed":    gained,
                "at_max":    after >= hp_max,
            }
            if con_bonus:
                healed_report[char.get("label", key)]["con_weekly_bonus"] = con_bonus

            if days >= 1:
                slot_cfg = char.get("spell_slots") or {}
                if slot_cfg:
                    cstate["spell_slots"] = dict(slot_cfg)
                memorized = _normalize_memorized(cstate.get("memorized_spells", []),
                                                 _caster_kind(char.get("cls", "")))
                for sp in memorized:
                    sp["cast"] = False
                cstate["memorized_spells"] = memorized

            # Only someone actually back above 0 stops being down.
            if after > 0:
                cstate["conditions"] = [c for c in cstate.get("conditions", [])
                                       if c != "downed"]
            rested.append(char.get("label", key))

        _c.save_state(cfg, state)

        if full:
            basis = "DM override — restored to maximum (not the PHB rate)."
        elif not provisioned:
            basis = "No food, water or sleep — no hit points regained (PHB)."
        else:
            basis = f"{rate} hp/day x {days} day(s)" + (
                f", tended by {healer}" if healer else ""
            ) + (" (complete bed rest)" if bed_rest else " (light activity)")

        out = {
            "rested":   rested,
            "days":     days,
            "rate_per_day": rate,
            "basis":    basis,
            "healing":  healed_report,
        }
        if days >= 1:
            out["reminder"] = ("Spell slots restored. Call memorize_spells for each "
                               "spellcaster to set their new memorized spell list.")
        still_hurt = [n for n, r in healed_report.items() if not r["at_max"]]
        if still_hurt and not full:
            out["still_wounded"] = still_hurt
        return out

    @mcp.tool()
    def award_xp(character: str, amount: int) -> dict:
        """Add XP to a character. Includes a level-up hint if a threshold is crossed.

        A multi-class character ("Fighter/Thief") has the award split evenly
        between their classes (PHB): `xp_by_class` shows each class's share,
        level and next threshold, and the hint names the class that levels.
        Levelling is still update_stats(levels={...}) — this only tells you."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        chars_state = state.setdefault("characters", {})
        cstate = chars_state.setdefault(key, {})
        xp_before = cstate.get("xp", 0)
        xp_after = xp_before + amount
        cstate["xp"] = xp_after
        _c.save_state(cfg, state)

        hints = _xp.level_up_hints(char, xp_before, xp_after)

        out = {
            "character": char.get("label", key),
            "xp_gained": amount,
            "xp_total": xp_after,
            "level_up_hint": " ".join(hints),
        }
        if _xp.is_multiclass(char):
            out["xp_by_class"] = [
                {f: r[f] for f in ("class", "level", "xp", "xp_next", "ready")}
                for r in _xp.progress(char, xp_after)
            ]
        return out

    @mcp.tool()
    def update_coin(delta: str) -> dict:
        """Parse a coin delta like '+450pp', '+50gp', '-10sp', '+3ep', '+100cp' and update the party coin."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        m = re.match(r'^([+-]?\d+)(pp|gp|ep|sp|cp)$', delta.strip().lower())
        if not m:
            return {"error": f"Cannot parse coin delta '{delta}'. Expected format: +450pp, +50gp, -10sp, +3ep, +100cp."}

        amount = int(m.group(1))
        denom = m.group(2)

        coin = state.setdefault("coin", {"pp": 0, "gp": 0, "ep": 0, "sp": 0, "cp": 0})
        coin[denom] = coin.get(denom, 0) + amount
        _c.save_state(cfg, state)

        return {
            "delta": delta,
            "coin": dict(coin),
        }
