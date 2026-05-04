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
    def reaction(modifier: int = 0) -> dict:
        """2d10 reaction roll for NPC first encounters.
        Table: 2=attacks, 3-5=hostile, 6-8=uncertain, 9-11=neutral, 12+=friendly."""
        r1 = random.randint(1, 10)
        r2 = random.randint(1, 10)
        raw = r1 + r2
        modified = raw + modifier

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

        return {
            "roll": raw,
            "modified": modified,
            "result": result,
            "description": description,
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
