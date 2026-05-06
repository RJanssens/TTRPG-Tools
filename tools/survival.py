"""Survival resources — consumable inventory, light sources, encumbrance, ration tracking.

Consumables are stored in state.json under:
    state.consumables.<character_key> = {item_name: qty, ...}
Light sources are a stack in state.light_sources:
    [{type: 'torch'|'lantern'|...,
      minutes_remaining: int,
      holder: char_label,
      lit: bool}, ...]

Lit-time defaults follow the PHB: torch 60 min, lantern 240 min, candle 60 min.
The actual minute-by-minute decrement happens in tools/log.py advance_time
(added alongside time-of-day support).
"""
import sqlite3
from pathlib import Path
import _campaign as _c

BASE_DIR = Path(__file__).parent.parent
_2E_DB = BASE_DIR / "global" / "2e.db"

# STR → (light, moderate, heavy, severe) load thresholds in lbs (PHB Table 1 / quick ref).
# Caller may pass intermediate STR; we use the floor of the bracket.
_STR_BANDS = {
    3:  (5,   10,  15,  20),
    5:  (10,  20,  30,  45),
    7:  (20,  35,  45,  65),
    9:  (30,  45,  65,  85),
    11: (40,  55,  85,  115),
    13: (50,  75,  105, 140),
    15: (65,  90,  130, 170),
    17: (85,  120, 175, 240),
    18: (110, 160, 235, 335),
    19: (485, 535, 635, 785),
}


def _str_thresholds(strength: int) -> tuple[int, int, int, int]:
    """Return (light, moderate, heavy, severe) cap weights for a STR score."""
    keys = sorted(_STR_BANDS.keys())
    # Find largest key ≤ strength
    pick = keys[0]
    for k in keys:
        if k <= strength:
            pick = k
        else:
            break
    return _STR_BANDS[pick]


def _item_weight(name: str) -> float:
    """Look up an item's weight in 2e.db (returns 0 if unknown).
    Best-effort — strips quantity prefixes and matches partially."""
    if not _2E_DB.exists():
        return 0.0
    # Strip leading numbers and common prefixes
    cleaned = name.strip().lstrip("0123456789x ").strip()
    if not cleaned:
        return 0.0
    try:
        conn = sqlite3.connect(str(_2E_DB))
        row = conn.execute(
            "SELECT weight FROM items WHERE name LIKE ? COLLATE NOCASE LIMIT 1",
            (f"%{cleaned}%",),
        ).fetchone()
        conn.close()
        if row and row[0] is not None:
            try:
                # weight may be string ('3', '1/2', '*'); accept numeric only
                return float(str(row[0]).strip())
            except ValueError:
                return 0.0
    except sqlite3.Error:
        return 0.0
    return 0.0


_LIGHT_DEFAULTS = {
    "torch":    60,
    "lantern":  240,
    "candle":   60,
    "continual_light": -1,   # magical, no decay
}


def _resolve_char(cfg: dict, character: str) -> tuple[str | None, dict | None, str]:
    """Resolve a character key (PCs first, then NPC slugs).
    Returns (key, char_dict, display_label) or (None, None, '')."""
    key, char = _c.char_key_for(cfg, character)
    if key is not None:
        return key, char, char.get("label", key)
    npcs = cfg.get("npcs", {})
    low = character.lower()
    for k, c in npcs.items():
        if c.get("label", "").lower() == low or k.lower() == low:
            return k, c, c.get("label", k)
    for k, c in npcs.items():
        if c.get("label", "").lower().startswith(low) or k.lower().startswith(low):
            return k, c, c.get("label", k)
    return None, None, ""


def _consumables(state: dict, key: str) -> dict:
    return state.setdefault("consumables", {}).setdefault(key, {})


def register(mcp):

    @mcp.tool()
    def add_inventory(character: str, item: str, qty: int = 1) -> dict:
        """Add a quantity of a consumable to a character's tracked inventory.
        Use for torches, rations, arrows, oil flasks, holy water, potions, etc.
        For one-of-a-kind items (a magic sword, a key) keep them in the character
        markdown sheet — this tool is for things that get used up."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, _char, label = _resolve_char(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        item_key = item.lower().strip()
        bag = _consumables(state, key)
        bag[item_key] = bag.get(item_key, 0) + int(qty)
        _c.save_state(cfg, state)

        return {"character": label, "item": item_key, "qty_added": qty, "now_holds": bag[item_key]}

    @mcp.tool()
    def consume(character: str, item: str, qty: int = 1) -> dict:
        """Decrement a character's consumable. Errors if they don't have enough.
        Use proactively whenever an in-fiction action depletes supplies:
        firing arrows, lighting a torch, eating a ration, drinking a potion.

        Returns a 'low_supply' warning when stock falls to ≤ 2 (rationing matters)."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, _char, label = _resolve_char(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        item_key = item.lower().strip()
        bag = _consumables(state, key)
        have = bag.get(item_key, 0)
        if have < qty:
            return {"error": f"{label} has only {have} {item_key} (needed {qty})."}

        bag[item_key] = have - qty
        if bag[item_key] == 0:
            del bag[item_key]
        _c.save_state(cfg, state)

        result = {
            "character":  label,
            "item":       item_key,
            "qty_used":   qty,
            "remaining":  bag.get(item_key, 0),
        }
        remaining = bag.get(item_key, 0)
        if remaining == 0:
            result["warning"] = f"{label} is out of {item_key}."
        elif remaining <= 2:
            result["warning"] = f"{label} has only {remaining} {item_key} left."
        return result

    @mcp.tool()
    def inventory(character: str = "") -> dict:
        """List tracked consumables.
        character: empty = all party + named NPCs; named = just that character."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        bag_all = state.get("consumables", {})

        if character:
            key, _char, label = _resolve_char(cfg, character)
            if key is None:
                return {"error": f"Character '{character}' not found."}
            return {"character": label, "items": dict(bag_all.get(key, {}))}

        # Party + NPC view
        out = {}
        for key, items in bag_all.items():
            if not items:
                continue
            char = cfg.get("characters", {}).get(key) or cfg.get("npcs", {}).get(key, {})
            label = char.get("label", key)
            out[label] = dict(items)
        return {"by_character": out}

    @mcp.tool()
    def light_torch(character: str, source_type: str = "torch") -> dict:
        """Light a fresh torch / lantern / candle. Consumes one from inventory.
        For lanterns, also consumes one oil flask (lanterns burn oil, not the lantern itself).
        Pushes a new lit source onto state.light_sources with default duration.
        Use extinguish_light to put it out early; advance_time decrements remaining minutes."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        key, _char, label = _resolve_char(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        stype = source_type.lower().strip().replace(" ", "_")
        if stype not in _LIGHT_DEFAULTS:
            return {"error": f"Unknown light source '{source_type}'. Use: torch, lantern, candle, continual_light."}

        bag = _consumables(state, key)

        # Lanterns burn oil; the lantern itself is durable
        if stype == "lantern":
            if bag.get("oil", 0) < 1:
                return {"error": f"{label} has no oil for the lantern."}
            bag["oil"] -= 1
            if bag["oil"] == 0:
                del bag["oil"]
        elif stype != "continual_light":
            if bag.get(stype, 0) < 1:
                return {"error": f"{label} has no {stype} to light."}
            bag[stype] -= 1
            if bag[stype] == 0:
                del bag[stype]

        sources = state.setdefault("light_sources", [])
        entry = {
            "type":              stype,
            "holder":            label,
            "minutes_remaining": _LIGHT_DEFAULTS[stype],
            "lit":               True,
        }
        sources.append(entry)
        _c.save_state(cfg, state)

        return {
            "lit":          stype,
            "holder":       label,
            "duration_min": _LIGHT_DEFAULTS[stype],
            "active_count": sum(1 for s in sources if s.get("lit")),
        }

    @mcp.tool()
    def extinguish_light(holder: str = "", source_type: str = "") -> dict:
        """Extinguish a lit light source. Filters by holder and/or type;
        empty filters = extinguish the most recently lit one."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        sources = state.get("light_sources", [])

        candidates = [
            (i, s) for i, s in enumerate(sources)
            if s.get("lit")
            and (not holder      or s.get("holder", "").lower().startswith(holder.lower()))
            and (not source_type or s.get("type", "").lower() == source_type.lower())
        ]
        if not candidates:
            return {"error": "No matching lit light source."}

        i, s = candidates[-1]   # most recently lit
        s["lit"] = False
        _c.save_state(cfg, state)
        return {"extinguished": s["type"], "holder": s.get("holder", ""), "remaining_min": s["minutes_remaining"]}

    @mcp.tool()
    def encumbrance(character: str) -> dict:
        """Calculate carry weight from a character's static inventory + tracked
        consumables, vs their STR encumbrance bands.

        Returns total weight, applicable band (light/moderate/heavy/severe),
        movement-rate penalty, and a per-item breakdown. Items with no DB
        match return 0 weight (the DM can override by setting weight on the
        item via item_update).

        Use after major loot pickups, before long marches, and when the player
        argues 'I don't have to drop the chest'."""
        cfg   = _c.load_campaign()
        state = _c.load_state(cfg)

        key, char, label = _resolve_char(cfg, character)
        if key is None:
            return {"error": f"Character '{character}' not found."}

        ab = char.get("ability_scores") or {}
        strength = int(ab.get("str", 10))

        breakdown: list[dict] = []
        total = 0.0

        # Static inventory (free-form strings on the character file)
        for raw in char.get("inventory", []):
            item_str = str(raw)
            w = _item_weight(item_str)
            total += w
            breakdown.append({"item": item_str, "qty": 1, "weight_lb": w})

        # Tracked consumables (state.consumables[key] = {item: qty})
        bag = state.get("consumables", {}).get(key, {})
        for item_name, qty in bag.items():
            w = _item_weight(item_name) * qty
            total += w
            breakdown.append({"item": item_name, "qty": qty, "weight_lb": w})

        light, moderate, heavy, severe = _str_thresholds(strength)
        if total <= light:
            band, penalty = "light", 0
        elif total <= moderate:
            band, penalty = "moderate", -3
        elif total <= heavy:
            band, penalty = "heavy", -6
        elif total <= severe:
            band, penalty = "severe", -9
        else:
            band, penalty = "overloaded", -12

        return {
            "character":      label,
            "strength":       strength,
            "total_weight":   round(total, 1),
            "thresholds":     {"light": light, "moderate": moderate, "heavy": heavy, "severe": severe},
            "band":           band,
            "movement_penalty": penalty,
            "items":          breakdown,
        }

    @mcp.tool()
    def light_state() -> dict:
        """Return all lit and recently-extinguished light sources.
        Useful at any point in a dungeon to check 'is the party still lit?'.
        Sources with minutes_remaining ≤ 5 are flagged as 'guttering'."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        sources = state.get("light_sources", [])

        lit  = []
        used = []
        for s in sources:
            entry = dict(s)
            if s.get("lit"):
                if 0 <= s.get("minutes_remaining", 0) <= 5:
                    entry["status"] = "guttering"
                else:
                    entry["status"] = "burning"
                lit.append(entry)
            else:
                used.append(entry)
        return {
            "any_lit":   bool(lit),
            "lit":       lit,
            "burnt_out": [s for s in used if s.get("minutes_remaining", 1) <= 0],
        }
