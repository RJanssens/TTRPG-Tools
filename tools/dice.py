"""Dice rolling, reaction, morale, encounter, saving throw, and skill check tools."""
import re
import random
import _campaign as _c


def register(mcp):

    @mcp.tool()
    def roll(notation: str) -> dict:
        """Parse and roll dice notation: '2d6', '1d20', '4d6dl' (drop lowest), '1d8+2'."""
        original = notation.strip()

        # Parse: optional NdX, optional dl (drop lowest), optional +/-modifier
        pattern = r'^(\d*)d(\d+)(dl)?(([+-]\d+)*)$'
        m = re.match(pattern, original.lower().replace(' ', ''))
        if not m:
            return {"error": f"Cannot parse dice notation: {original}"}

        count_str, sides_str, drop_lowest, mod_str, _ = m.groups()
        count = int(count_str) if count_str else 1
        sides = int(sides_str)
        drop = drop_lowest is not None

        # Sum up modifiers
        modifier = 0
        for part in re.findall(r'[+-]\d+', mod_str or ''):
            modifier += int(part)

        rolls = [random.randint(1, sides) for _ in range(count)]
        dropped = []

        if drop and count > 1:
            min_val = min(rolls)
            idx = rolls.index(min_val)
            dropped = [rolls[idx]]
            kept = rolls[:idx] + rolls[idx+1:]
        else:
            kept = rolls[:]

        total = sum(kept) + modifier

        return {
            "notation": original,
            "rolls": rolls,
            "dropped": dropped,
            "modifier": modifier,
            "total": total,
        }

    @mcp.tool()
    def reaction(modifier: int = 0, npc: str = "") -> dict:
        """2d10 reaction roll. Use for FIRST encounters with an unknown NPC, OR
        when a known NPC's mood is in question (pass `npc` slug to apply their
        stored disposition + faction reputation as a modifier).

        modifier: explicit situational mod (CHA reaction adj, gift offered, etc.)
        npc:      optional NPC slug — adds disposition/20 as a modifier and
                  records the result back to the NPC's history.

        Table: 2=attacks, 3-5=hostile, 6-8=uncertain, 9-11=neutral, 12+=friendly."""
        cfg = _c.load_campaign()
        total_modifier = modifier
        disposition_applied = 0
        npc_label = ""
        npc_slug  = ""

        if npc:
            slug_key = npc.lower().strip()
            npc_data = cfg.get("npcs", {}).get(slug_key)
            if not npc_data:
                # Try prefix match
                for k, v in cfg.get("npcs", {}).items():
                    if k.lower().startswith(slug_key) or v.get("label", "").lower().startswith(slug_key):
                        npc_data = v
                        slug_key = k
                        break
            if npc_data:
                disp = int(npc_data.get("disposition", 0))
                disposition_applied = disp // 20  # ±100 → ±5
                total_modifier += disposition_applied
                npc_label = npc_data.get("label", slug_key)
                npc_slug  = slug_key
                # Faction reputation modifier (added in #16)
                fac = npc_data.get("faction", "")
                if fac:
                    state = _c.load_state(cfg)
                    rep = state.get("faction_state", {}).get(fac, {}).get("reputation", 0)
                    total_modifier += rep // 25  # ±100 → ±4
            else:
                return {"error": f"NPC '{npc}' not found in campaign.json npcs."}

        r1 = random.randint(1, 10)
        r2 = random.randint(1, 10)
        raw = r1 + r2
        modified = raw + total_modifier

        if modified <= 2:
            result = "attacks"
            description = "The NPC attacks immediately."
        elif modified <= 5:
            result = "hostile"
            description = "The NPC is hostile and threatening."
        elif modified <= 8:
            result = "uncertain"
            description = "The NPC is uncertain and wary."
        elif modified <= 11:
            result = "neutral"
            description = "The NPC is neutral and indifferent."
        else:
            result = "friendly"
            description = "The NPC is friendly and well-disposed."

        # Log for traceability when an NPC slug was used
        if npc_slug:
            _c.append_event(cfg, {
                "type":  "reaction_roll",
                "slug":  npc_slug,
                "name":  npc_label,
                "result": result,
                "notes": f"reaction: {result} (raw {raw}, mod {total_modifier:+d})",
            })

        return {
            "roll":        raw,
            "modifier":    total_modifier,
            "disposition_applied": disposition_applied,
            "modified":    modified,
            "result":      result,
            "description": description,
            "npc":         npc_label,
        }

    @mcp.tool()
    def morale_check(rating: int, modifier: int = 0) -> dict:
        """2d10 morale check. If result > rating, morale breaks."""
        r1 = random.randint(1, 10)
        r2 = random.randint(1, 10)
        raw = r1 + r2
        modified = raw + modifier
        breaks = modified > rating

        if breaks:
            description = f"Morale breaks! Roll {modified} exceeds rating {rating}."
        else:
            description = f"Holds firm. Roll {modified} does not exceed rating {rating}."

        cfg = _c.load_campaign()
        _c.append_event(cfg, {
            "type":   "morale_check",
            "rating": rating,
            "modifier": modifier,
            "roll":   modified,
            "breaks": breaks,
            "notes":  description,
        })

        return {
            "roll": modified,
            "rating": rating,
            "breaks": breaks,
            "description": description,
        }

    @mcp.tool()
    def check_encounter(terrain: str) -> dict:
        """Check whether a random encounter is triggered for the given terrain.
        terrain: dungeon / wilderness / road / urban."""
        cfg = _c.load_campaign()
        freq = cfg.get("encounter_frequency", {})
        terrain_key = terrain.lower()

        defaults = {
            "dungeon":    {"die": 6,  "threshold": 1, "interval_minutes": 10},
            "wilderness": {"die": 6,  "threshold": 1, "interval_minutes": 240},
            "road":       {"die": 6,  "threshold": 1, "interval_minutes": 60},
            "urban":      {"die": 20, "threshold": 1, "interval_minutes": 120},
        }

        config = freq.get(terrain_key, defaults.get(terrain_key, {"die": 6, "threshold": 1, "interval_minutes": 60}))
        die = config.get("die", 6)
        threshold = config.get("threshold", 1)
        interval = config.get("interval_minutes", 60)

        roll_result = random.randint(1, die)
        triggered = roll_result <= threshold

        hours = interval // 60
        mins = interval % 60
        if hours and mins:
            interval_str = f"{hours}h {mins}m"
        elif hours:
            interval_str = f"{hours}h"
        else:
            interval_str = f"{mins}m"

        _c.append_event(cfg, {
            "type":      "encounter_check",
            "terrain":   terrain_key,
            "triggered": triggered,
            "notes":     f"d{die} → {roll_result}/{threshold} ({'triggered' if triggered else 'no encounter'})",
        })

        return {
            "terrain": terrain_key,
            "roll": roll_result,
            "threshold": threshold,
            "triggered": triggered,
            "interval": interval_str,
        }

    @mcp.tool()
    def determine_encounter(terrain: str, dungeon_level: int = 1) -> dict:
        """Roll on the campaign's encounter table for the given terrain.
        terrain: dungeon / wilderness / road / urban. dungeon_level: 1-6."""
        cfg = _c.load_campaign()
        tables = cfg.get("encounter_tables", {})

        terrain_key = terrain.lower()
        if terrain_key == "dungeon":
            table_key = f"dungeon_l{dungeon_level}"
        else:
            table_key = terrain_key

        table = tables.get(table_key, [])

        if not table:
            return {
                "roll": 0,
                "result": f"No encounter table defined for '{table_key}'. Add entries to campaign.json encounter_tables.",
                "category": "none",
            }

        # Find max range value to know the die size
        max_val = max(entry["range"][1] for entry in table)
        roll_result = random.randint(1, max_val)

        for entry in table:
            lo, hi = entry["range"]
            if lo <= roll_result <= hi:
                return {
                    "roll": roll_result,
                    "result": entry.get("result", "Unknown"),
                    "category": entry.get("category", ""),
                }

        return {
            "roll": roll_result,
            "result": "No matching table entry.",
            "category": "",
        }

    @mcp.tool()
    def surprise_check(party_modifier: int = 0, enemy_modifier: int = 0) -> dict:
        """Roll surprise for both sides at the start of an encounter (PHB Ch. 9).
        Each side rolls 1d10 separately. A roll of 1–2 = surprised (default threshold).
        Surprised side loses one segment (10 segments = 1 round) of action and may not
        use shields. Modifiers shift the threshold (positive = harder to surprise; e.g.
        Ranger imposes -3 on enemies trying to surprise the party in wilderness).

        party_modifier: +N to enemy roll's surprise threshold for the party (i.e.
                        threshold becomes 2 - N for the enemy; rangers/elves/etc.)
        enemy_modifier: +N to party's surprise threshold (party harder to surprise
                        the enemy when sneaking, etc.)

        Returns who is surprised and by how many segments."""
        # Threshold = 2 by default. A side is surprised if their d10 ≤ threshold.
        # The opposing side's modifier *raises their threshold* (more likely surprised).
        party_d10 = random.randint(1, 10)
        enemy_d10 = random.randint(1, 10)

        party_threshold = max(0, 2 - enemy_modifier)
        enemy_threshold = max(0, 2 - party_modifier)

        party_surprised = party_d10 <= party_threshold
        enemy_surprised = enemy_d10 <= enemy_threshold

        # Segments lost: surprised side loses (threshold - roll + 1) segments per
        # the looser DMG reading; we default to 1 segment (PHB simplified).
        party_segments = max(0, party_threshold - party_d10 + 1) if party_surprised else 0
        enemy_segments = max(0, enemy_threshold - enemy_d10 + 1) if enemy_surprised else 0

        if party_surprised and enemy_surprised:
            description = "Both sides surprised — encounter begins simultaneously, no advantage."
        elif party_surprised:
            description = f"Party is surprised. Enemy acts freely for {party_segments} segment(s)."
        elif enemy_surprised:
            description = f"Enemy is surprised. Party acts freely for {enemy_segments} segment(s)."
        else:
            description = "Neither side surprised — roll initiative normally."

        cfg = _c.load_campaign()
        _c.append_event(cfg, {
            "type":  "surprise_check",
            "party_surprised": party_surprised,
            "enemy_surprised": enemy_surprised,
            "notes": description,
        })

        return {
            "party_roll":       party_d10,
            "enemy_roll":       enemy_d10,
            "party_threshold":  party_threshold,
            "enemy_threshold":  enemy_threshold,
            "party_surprised":  party_surprised,
            "enemy_surprised":  enemy_surprised,
            "party_segments_lost": party_segments,
            "enemy_segments_lost": enemy_segments,
            "description":      description,
        }

    @mcp.tool()
    def saving_throw(character: str, save_type: str) -> dict:
        """Roll a saving throw for a character.
        save_type: para/paralysis/poison/death | rsw/rod/staff/wand | poly/polymorph/petrify | breath/bw | spell/magic"""
        cfg = _c.load_campaign()

        save_aliases = {
            "para": 0, "paralysis": 0, "poison": 0, "death": 0,
            "rsw": 1, "rod": 1, "staff": 1, "wand": 1,
            "poly": 2, "polymorph": 2, "petrify": 2,
            "breath": 3, "bw": 3,
            "spell": 4, "magic": 4,
        }
        save_names = ["Paralysis/Poison/Death", "Rod/Staff/Wand", "Polymorph/Petrify", "Breath Weapon", "Spell/Magic"]

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        idx = save_aliases.get(save_type.lower())
        if idx is None:
            return {"error": f"Unknown save type '{save_type}'. Use: para, rsw, poly, breath, spell."}

        saves = char.get("saves", [16, 18, 17, 20, 19])
        target = saves[idx]
        roll_result = random.randint(1, 20)
        success = roll_result >= target

        return {
            "character": char.get("label", key),
            "save": save_names[idx],
            "roll": roll_result,
            "target": target,
            "success": success,
        }

    @mcp.tool()
    def system_shock(character: str) -> dict:
        """Roll system shock for a character (PHB Table 3, CON-driven).
        Required after polymorph, petrification, raise dead, or any massive
        magical/biological transformation. Failure = die instantly.

        Pulls CON from ability_scores. Returns roll, CON, target %, and pass/fail."""
        # PHB Table 3: System Shock % by CON.
        SHOCK = {3: 35, 4: 40, 5: 45, 6: 50, 7: 55, 8: 60, 9: 65, 10: 70,
                 11: 75, 12: 80, 13: 85, 14: 88, 15: 90, 16: 95, 17: 97,
                 18: 99, 19: 99, 20: 99, 21: 99, 22: 99, 23: 99, 24: 99}

        cfg = _c.load_campaign()
        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        ab = char.get("ability_scores") or {}
        con = int(ab.get("con", 10))
        target = SHOCK.get(con, 70)
        roll = random.randint(1, 100)
        success = roll <= target

        return {
            "character":   char.get("label", key),
            "con":         con,
            "target":      target,
            "roll":        roll,
            "success":     success,
            "description": (
                f"{char.get('label', key)} survives the shock — roll {roll} ≤ {target}." if success
                else f"{char.get('label', key)} dies of system shock — roll {roll} > {target}."
            ),
        }

    @mcp.tool()
    def resurrection_check(character: str) -> dict:
        """Roll resurrection survival (PHB Table 3, CON-driven).
        Each failed or successful attempt reduces the character's CON by 1
        permanently — when CON hits 0 they cannot be raised again.

        Updates ability_scores.con on success or failure (the cost is paid
        regardless). Failure → the character is unrecoverably dead by this method."""
        # PHB Table 3: Resurrection Survival % by CON.
        RESURRECT = {3: 40, 4: 45, 5: 50, 6: 55, 7: 60, 8: 65, 9: 70, 10: 75,
                     11: 80, 12: 85, 13: 90, 14: 92, 15: 94, 16: 96, 17: 98,
                     18: 100, 19: 100, 20: 100}

        cfg = _c.load_campaign()
        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        ab = char.setdefault("ability_scores", {})
        con = int(ab.get("con", 10))
        if con <= 0:
            return {"error": f"{char.get('label', key)} has CON {con} — beyond resurrection."}

        target = RESURRECT.get(con, 75)
        roll = random.randint(1, 100)
        success = roll <= target

        # Permanent CON cost
        ab["con"] = con - 1
        _c.save_campaign(cfg)

        return {
            "character":   char.get("label", key),
            "con_before":  con,
            "con_after":   con - 1,
            "target":      target,
            "roll":        roll,
            "success":     success,
            "description": (
                f"Resurrection succeeds — roll {roll} ≤ {target}. CON {con} → {con-1}." if success
                else f"Resurrection fails — roll {roll} > {target}. The body cannot be recalled. CON {con} → {con-1} (cost paid regardless)."
            ),
        }

    @mcp.tool()
    def skill_check(character: str, skill: str) -> dict:
        """Roll a thieving or special skill check (percentile).
        Partial match on skill key name."""
        cfg = _c.load_campaign()

        key, char = _c.char_key_for(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        skills = char.get("skills", {})
        skill_lower = skill.lower()

        # Partial match
        matched_key = None
        for k in skills:
            if skill_lower in k.lower():
                matched_key = k
                break

        if matched_key is None:
            return {"error": f"Skill '{skill}' not found for {char.get('label', key)}. Available: {list(skills.keys())}"}

        target = skills[matched_key]
        roll_result = random.randint(1, 100)
        success = roll_result <= target

        return {
            "character": char.get("label", key),
            "skill": matched_key,
            "roll": roll_result,
            "target": target,
            "success": success,
        }
