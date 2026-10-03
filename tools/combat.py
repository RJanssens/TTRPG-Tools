"""Combat tracker — initiative, attack resolution, HP management, spell declaration."""
import json
import random
import re
import sqlite3
from pathlib import Path
import _campaign as _c
from tools.composites import call_tool as _call_tool

_session: dict | None = None

BASE_DIR = Path(__file__).parent.parent
_2E_DB = BASE_DIR / "global" / "2e.db"


# The tracker used to live only in this process's memory, so a fight could not
# outlive the process. The /play driver spawned one `claude -p` per turn, which
# meant a new MCP server per turn, which meant every combat past round one hit
# "No active combat session" and had to be re-initialised mid-fight (visible as
# "Combat session dropped — re-initializing" in the session logs). The full
# session is now mirrored to disk after every mutation and reloaded on demand.
#
# combat_state.json can't serve for this: it is the dashboard's view and drops
# thac0, ac, damage dice and effects. This file is the authoritative one.
_SESSION_FILENAME = "combat_session.json"


def _session_file() -> Path | None:
    try:
        return _c.load_campaign()["_dir"] / _SESSION_FILENAME
    except Exception:
        return None


def _ensure_session() -> dict | None:
    """Return the active combat session, reloading it from disk if this
    process hasn't got it in memory yet. None when no fight is in progress."""
    global _session
    if _session is not None:
        return _session
    p = _session_file()
    if p is None:
        return None
    try:
        loaded = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(loaded, dict) and isinstance(loaded.get("combatants"), list):
        _session = loaded
    return _session


def get_session() -> dict | None:
    """Return the active combat session (used by party tools for heal sync)."""
    return _ensure_session()


def _clear_session_file() -> None:
    p = _session_file()
    if p is not None:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _persist_session() -> None:
    """Persist the session after a state mutation: the authoritative copy for
    the next turn, plus combat_state.json so the dashboard and status line can
    show live HP/conditions. Deferred import avoids a circular dep with
    tools.combat_map."""
    if _session is None:
        return
    p = _session_file()
    if p is not None:
        try:
            _c.atomic_write_text(p, json.dumps(_session, ensure_ascii=False))
        except Exception:
            pass  # a tracker write must never break a tool response
    try:
        from tools.combat_map import save_combat_state
        cfg = _c.load_campaign()
        save_combat_state(cfg, _session)
    except Exception:
        pass  # never break a tool response on a dashboard write


def _sum_modifier(combatant: dict, key: str) -> int:
    """Total a numeric effect modifier across all active effects on a combatant."""
    total = 0
    for eff in combatant.get("effects", []):
        v = eff.get(key)
        if isinstance(v, int):
            total += v
    return total


def _check_morale_trigger(side: str) -> dict | None:
    """Return a one-shot morale-due notice when `side` first crosses 50% casualties.
    Tracks fired sides on the session so each threshold fires only once."""
    if _session is None or side == "party":
        return None
    members = [c for c in _session["combatants"] if c.get("side") == side]
    if not members:
        return None
    down = sum(1 for c in members if c.get("hp", 0) <= 0)
    if down * 2 < len(members):
        return None
    fired = _session.setdefault("morale_triggered_sides", [])
    if side in fired:
        return None
    fired.append(side)
    return {
        "side":     side,
        "down":     down,
        "total":    len(members),
        "message":  f"Morale check due: {down}/{len(members)} of side '{side}' down. "
                    f"Call morale_check(rating) using the group's morale rating "
                    f"(monster_lookup → 'morale').",
    }


# --- Weapon attack blocks ----------------------------------------------------
# Every PC sheet in campaign.json already carries a complete `attacks` array:
#   {"name": "Sunsweep long sword +1 (vs normal)", "speed": 5, "attacks": 1.5,
#    "thac0": 13, "damage_sm": "1d8+6", "damage_l": "1d12+6"}
# — per-weapon THAC0, attacks per round, weapon speed and BOTH damage dice, with
# STR / magic / specialisation already folded in. The tracker used to ignore all
# of it and read `char["weapon"]` as dice notation instead. That field holds
# prose ("Maragbur — bastard sword +1, Flame Tongue"), so _roll_dice's regex
# failed and silently returned 1: every PC in five of six campaigns has been
# hitting for 1 plus their damage bonus. Hence the strict parsing below and the
# warnings surfaced out of start_combat.
#
# Because a block's THAC0 and damage already include the character's bonuses,
# a combatant built from one carries bonus_hit/bonus_dmg of 0 — adding them
# again would swing the error the other way.

_DICE_RE = re.compile(r'^\s*(\d*)d(\d+)((?:\s*[+-]\s*\d+)*)\s*$', re.IGNORECASE)

# Damage-vs-Large applies to targets of size Large or bigger (PHB Table 44's
# second damage column).
_LARGE_SIZES = ("l", "h", "g")


def _parse_dice(notation) -> tuple[int, int, int] | None:
    """Parse 'NdX+Y' into (count, sides, modifier), or None if unparseable.

    Also accepts a bare integer ('1', '3') for the handful of stat lines that
    give flat damage."""
    if isinstance(notation, (int, float)):
        return (0, 0, int(notation))
    s = str(notation or "").strip()
    if not s:
        return None
    if re.fullmatch(r'-?\d+', s):
        return (0, 0, int(s))
    m = _DICE_RE.match(s)
    if not m:
        return None
    count = int(m.group(1)) if m.group(1) else 1
    sides = int(m.group(2))
    if count < 1 or sides < 1:
        return None
    mod = sum(int(p.replace(" ", "")) for p in re.findall(r'[+-]\s*\d+', m.group(3) or ""))
    return (count, sides, mod)


def _roll_dice(notation: str) -> int:
    """Roll a dice expression. Raises ValueError on anything unparseable.

    It used to return 1 on a parse failure, which is how a prose weapon name in
    the damage field turned into a lifetime of 1-point hits without anyone
    noticing. Notation is validated when a combatant is built, so a raise here
    means a genuine bug rather than bad campaign data."""
    parsed = _parse_dice(notation)
    if parsed is None:
        raise ValueError(f"Unparseable dice notation: {notation!r}")
    count, sides, mod = parsed
    total = sum(random.randint(1, sides) for _ in range(count)) + mod
    return max(0, total)


def _size_code(raw) -> str:
    """First letter of a monster size line: 'Large (9' tall)' → 'L'.

    Returns 'M' when the line says nothing usable — man-sized is the safe
    default, since it selects the S/M damage die."""
    s = str(raw or "").strip()
    if not s:
        return "M"
    first = s[0].upper()
    return first if first in ("T", "S", "M", "L", "H", "G") else "M"


def _takes_large_damage(size_code: str) -> bool:
    return str(size_code or "M").lower() in _LARGE_SIZES


def _attack_rate(entry: dict) -> float:
    """Attacks per round from an attack block. Accepts 1.5, '3/2', '2'."""
    raw = entry.get("attacks", 1)
    if isinstance(raw, (int, float)):
        return max(0.5, float(raw))
    s = str(raw).strip()
    m = re.fullmatch(r'(\d+)\s*/\s*(\d+)', s)
    if m and int(m.group(2)):
        return max(0.5, int(m.group(1)) / int(m.group(2)))
    try:
        return max(0.5, float(s))
    except ValueError:
        return 1.0


def _normalize_attacks(char: dict) -> tuple[list[dict], list[str]]:
    """Turn a sheet's `attacks` array into validated attack blocks.

    Returns (blocks, warnings). A block whose S/M damage won't parse is dropped
    and reported — better a loud warning at setup than a silent 1-point hit."""
    blocks: list[dict] = []
    warnings: list[str] = []
    raw_list = char.get("attacks")
    if not isinstance(raw_list, list):
        return blocks, warnings

    label = char.get("label", "?")
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "attack").strip()
        dmg_sm = entry.get("damage_sm") or entry.get("damage") or ""
        if _parse_dice(dmg_sm) is None:
            warnings.append(
                f"{label}: attack '{name}' has unusable S/M damage {dmg_sm!r} — skipped. "
                f"Fix the sheet's attacks[] entry."
            )
            continue
        dmg_l = entry.get("damage_l") or ""
        if dmg_l and _parse_dice(dmg_l) is None:
            warnings.append(
                f"{label}: attack '{name}' has unusable L damage {dmg_l!r} — "
                f"falling back to the S/M die against large targets."
            )
            dmg_l = ""
        blocks.append({
            "name":       name,
            "thac0":      int(entry["thac0"]) if entry.get("thac0") is not None else None,
            "damage_sm":  str(dmg_sm),
            "damage_l":   str(dmg_l or dmg_sm),
            "speed":      int(entry.get("speed") or 5),
            "rate":       _attack_rate(entry),
        })
    return blocks, warnings


def _apply_attack_block(combatant: dict, block: dict) -> None:
    """Point a combatant at one of its attack blocks."""
    combatant["weapon"]            = block["name"]
    combatant["dmg"]               = block["damage_sm"]
    combatant["dmg_large"]         = block["damage_l"]
    combatant["weapon_speed"]      = block["speed"]
    combatant["attacks_per_round"] = block["rate"]
    if block.get("thac0") is not None:
        combatant["thac0"] = block["thac0"]
    # The block's numbers already include STR, magic and specialisation.
    combatant["bonus_hit"] = 0
    combatant["bonus_dmg"] = 0


def _select_attack(combatant: dict, wanted: str) -> tuple[dict | None, str]:
    """Find one of a combatant's attack blocks by (partial) name.
    Returns (block, note). Matching is case-insensitive substring, longest
    match first, so 'undead' picks 'Sunsweep long sword +3 (vs undead)'."""
    blocks = combatant.get("attack_blocks") or []
    if not blocks:
        return None, f"{combatant['name']} has no attack blocks on their sheet — using their current weapon."
    needle = str(wanted or "").strip().lower()
    if not needle:
        return None, ""
    exact = [b for b in blocks if b["name"].lower() == needle]
    if exact:
        return exact[0], ""
    hits = sorted(
        (b for b in blocks if needle in b["name"].lower()),
        key=lambda b: len(b["name"]),
    )
    if hits:
        return hits[0], ""
    return None, (
        f"No attack matching {wanted!r} for {combatant['name']}; "
        f"available: {[b['name'] for b in blocks]}. Kept {combatant.get('weapon', 'current weapon')}."
    )


def _attacks_allowed_in_round(combatant: dict, round_no: int) -> int:
    """How many attacks this combatant is entitled to in this round alone.

    floor(rate x round) - floor(rate x (round-1)), which gives a 3/2 fighter
    one swing in round 1, two in round 2, one in round 3 — the standard reading
    of "3 attacks every 2 rounds" — with no DM bookkeeping.

    Deliberately stateless in the round number: an entitlement is spent in its
    own round or lost. Deriving it from a lifetime counter instead would let a
    fighter who spent a round unconscious bank swings and come back with three."""
    rate = float(combatant.get("attacks_per_round") or 1)
    r = max(1, int(round_no))
    return max(1, int(rate * r) - int(rate * (r - 1)))


def _attacks_remaining_this_round(combatant: dict, round_no: int) -> int:
    """Entitlement for this round minus what has already been swung in it."""
    used = int(combatant.get("attacks_this_round") or 0)
    return max(0, _attacks_allowed_in_round(combatant, round_no) - used)


def _tally(counter: str, n: int = 1) -> None:
    """Count pipeline work on the session so end_combat can report it.

    The audit's Hard Constraint #8 check needs to tell a fight that went
    through the tracker from one narrated freehand. It used to look for
    `roll_initiative` / `attack` / `apply_combat_damage` events — which no
    tool has ever emitted, so the check fired on every single combat and told
    the DM nothing. Counters here travel with the combat event instead."""
    if _session is None:
        return
    pipeline = _session.setdefault("pipeline", {})
    pipeline[counter] = int(pipeline.get(counter, 0)) + n


def _find_combatant(name: str) -> dict | None:
    if _session is None:
        return None
    low = name.lower()
    for c in _session["combatants"]:
        if c["name"].lower() == low:
            return c
    for c in _session["combatants"]:
        if c["name"].lower().startswith(low):
            return c
    return None


_spell_ct_cache: dict[str, tuple | None] = {}


def _spell_casting_time(spell_name: str) -> tuple | None:
    """Return (casting_time, casting_time_init) from 2e.db, or None if not found.
    Cached: spell DB is read-only at runtime."""
    key = spell_name.lower().strip()
    if key in _spell_ct_cache:
        return _spell_ct_cache[key]
    if not _2E_DB.exists():
        _spell_ct_cache[key] = None
        return None
    try:
        conn = sqlite3.connect(str(_2E_DB))
        row = conn.execute(
            "SELECT casting_time, casting_time_init FROM spells "
            "WHERE name = ? COLLATE NOCASE "
            "ORDER BY CASE WHEN caster='wizard' THEN 0 ELSE 1 END LIMIT 1",
            (spell_name.strip(),),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT casting_time, casting_time_init FROM spells "
                "WHERE name LIKE ? COLLATE NOCASE "
                "ORDER BY CASE WHEN caster='wizard' THEN 0 ELSE 1 END, length(name) LIMIT 1",
                (f"%{spell_name}%",),
            ).fetchone()
        conn.close()
        result = (row[0], row[1]) if row else None
    except Exception:
        result = None
    _spell_ct_cache[key] = result
    return result


# --- Monster stat-line parsing ----------------------------------------------
# The monster DB stores MM prose, not numbers: AC "5 (10)", HD "4+4" / "½" /
# "1-4 hp", morale "Elite (13-14)", damage "2-8" or "By weapon". setup_combat()
# needs integers, so these parse best-effort and always hand the raw string
# back so the DM can override with an explicit value.

def _first_int(text, default=None):
    """First signed integer in a string, or `default`."""
    if isinstance(text, (int, float)):
        return int(text)
    m = re.search(r'-?\d+', str(text or ""))
    return int(m.group()) if m else default


def _parse_ac(raw, default: int = 10) -> int:
    """'5' → 5 · '-2' → -2 · '5 (10)' → 5 · '0 (base)' → 0 · 'Varies' → default."""
    return _first_int(raw, default)


def _parse_morale(raw, default: int = 10) -> int:
    """'Elite (13-14)' → 13 · 'Fearless (20)' → 20 · '11' → 11.

    Takes the low end of a range: the honest reading when the DM hasn't picked
    a specific value, and they can always pass morale= explicitly."""
    s = str(raw or "")
    paren = re.search(r'\(([^)]*)\)', s)
    if paren:
        n = _first_int(paren.group(1))
        if n is not None:
            return n
    return _first_int(s, default)


def _parse_hd(raw) -> dict:
    """Parse an MM Hit Dice line into a rollable shape.

    Returns {dice, sides, mod, fixed, raw} where `fixed` is a literal HP value
    for the 'N hp' creatures and `dice` is 0 when the line carries no usable
    number ('Varies', 'Nil', 'See below') — the caller must then be given an
    explicit hp."""
    s = str(raw or "").strip()
    out = {"dice": 0, "sides": 8, "mod": 0, "fixed": None, "raw": s}
    if not s:
        return out

    low = s.lower()
    if "½" in s or "1/2" in s:
        # Half-HD creatures roll d4 (PHB/MM convention).
        out.update(dice=1, sides=4)
        return out

    # '1 hp', '1 hit point', '1-6 hp' — literal hit points, not dice.
    if re.search(r'h(p|it\s*point)', low):
        nums = [int(n) for n in re.findall(r'\d+', s)]
        if nums:
            out["fixed"] = random.randint(nums[0], nums[1]) if len(nums) > 1 else nums[0]
        return out

    # '8, 12, or 16' — a creature with variant grades; take the first.
    if "," in s:
        n = _first_int(s)
        if n is not None:
            out["dice"] = n
        return out

    m = re.match(r'^(\d+)\s*([+-])\s*(\d+)', s)
    if m:
        dice, sign, mod = int(m.group(1)), m.group(2), int(m.group(3))
        if sign == "-" and mod > 3:
            # '5-6' in the MM is an HD *range*, not a subtraction; only the
            # small forms ('1-1', '2-2') mean "minus". Take the low end.
            out["dice"] = dice
        else:
            out.update(dice=dice, mod=mod if sign == "+" else -mod)
        return out

    n = _first_int(s)
    if n is not None:
        out["dice"] = n
    return out


def _roll_monster_hp(hd: dict) -> int | None:
    """Roll HP from a parsed HD shape. None when the line gave us nothing."""
    if hd.get("fixed") is not None:
        return max(1, int(hd["fixed"]))
    dice = int(hd.get("dice") or 0)
    if dice <= 0:
        return None
    sides = int(hd.get("sides") or 8)
    total = sum(random.randint(1, sides) for _ in range(dice)) + int(hd.get("mod") or 0)
    return max(1, total)


def _parse_damage(raw, default: str = "1d6") -> tuple[str, bool]:
    """Normalise an MM damage line to dice notation.

    '1d8' → unchanged · '1-6' → '1d6' · '2-8' → '1d7+1' (same 2..8 spread) ·
    '1-10 or by weapon' → '1d10' · '5-8/5-8/5-12' → first attack only.

    Returns (notation, parsed) — `parsed` is False for lines carrying no
    numbers at all ('By weapon', 'Nil', 'Special'), where the caller gets
    `default` and should say so rather than pretend it read a stat block."""
    s = str(raw or "").strip()
    if not s:
        return default, False
    first = s.split("/")[0].strip()

    m = re.match(r'^(\d*)d(\d+)\s*([+-]\s*\d+)?', first, re.IGNORECASE)
    if m:
        count = m.group(1) or "1"
        mod = (m.group(3) or "").replace(" ", "")
        return f"{count}d{m.group(2)}{mod}", True

    # Prefix match, not full: MM lines trail prose ('1-10 or by weapon').
    m = re.match(r'^(\d+)\s*-\s*(\d+)', first)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if hi > lo:
            span = hi - lo + 1
            return (f"1d{span}+{lo - 1}" if lo > 1 else f"1d{span}"), True
        return f"1d{max(1, hi)}", True

    return default, False


def _monster_stats(name: str) -> dict | None:
    """Thin read of the monster DB for combat setup — the tactically relevant
    slice of monster_lookup() without the multi-paragraph description, so
    setup_combat() can return every enemy's stats inline."""
    try:
        from tools.lookup import _find_db, _MONSTERS_DB_CANDIDATES
    except Exception:
        return None
    db_path = _find_db(_MONSTERS_DB_CANDIDATES)
    if db_path is None:
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM monsters WHERE name LIKE ? COLLATE NOCASE "
            "ORDER BY length(name) LIMIT 1",
            (f"%{name}%",),
        ).fetchone()
        conn.close()
    except Exception:
        return None
    if row is None:
        return None
    keys = row.keys()
    def g(k):
        return row[k] if k in keys else None
    return {
        "name":             g("name"),
        "ac":               g("armor_class"),
        "hit_dice":         g("hit_dice"),
        "thac0":            g("thac0"),
        "damage":           g("damage_attack"),
        "no_of_attacks":    g("no_of_attacks"),
        "no_appearing":     g("no_appearing"),
        "morale":           g("morale"),
        "xp_value":         g("xp_value"),
        "treasure_type":    g("treasure_type"),
        "intelligence":     g("intelligence"),
        "alignment":        g("alignment"),
        "size":             g("size"),
        "move":             g("move"),
        "special_attacks":  g("special_attacks"),
        "special_defenses": g("special_defenses"),
        "magic_resistance": g("magic_resistance"),
    }


def register(mcp):

    @mcp.tool()
    def start_combat() -> dict:
        """Start a new combat encounter. Loads all party members from the active campaign
        and resets the combat session. Call this before adding enemies or rolling initiative."""
        global _session
        cfg   = _c.load_campaign()
        state = _c.load_state(cfg)

        # A fresh fight never inherits a stale tracker from disk.
        _clear_session_file()
        _session = {
            "round":       1,
            "current_idx": 0,
            "combatants":  [],
            "grid":        None,
        }

        party_loaded = []
        warnings: list[str] = []
        for key, char in cfg.get("characters", {}).items():
            char_state = state.get("characters", {}).get(key, {})
            hp_current = char_state.get("hp", char["hp_max"])
            conditions = list(char_state.get("conditions", []))
            combatant = {
                "name":         char["label"],
                "hp":           hp_current,
                "hp_max":       char["hp_max"],
                "thac0":        char.get("thac0", 20),
                "ac":           char.get("ac", 10),
                "dmg":          "1d6",
                "dmg_large":    "1d6",
                "bonus_hit":    char.get("bonus_hit", 0),
                "bonus_dmg":    char.get("bonus_dmg", 0),
                "weapon":       "",
                "weapon_speed": char.get("weapon_speed", 5),
                "attacks_per_round": 1.0,
                "attacks_made": 0,
                "attacks_this_round": 0,
                "size":         _size_code(char.get("size") or "M"),
                "side":         "party",
                "init":         None,
                "conditions":   conditions,
                "effects":      [],
                "_key":         key,
                "x":            None,
                "y":            None,
                "movement":     char.get("movement_rate", 12) // 2,
            }

            blocks, block_warnings = _normalize_attacks(char)
            warnings.extend(block_warnings)
            combatant["attack_blocks"] = blocks
            if blocks:
                _apply_attack_block(combatant, blocks[0])
            else:
                # No usable attacks[] — fall back to the legacy `weapon` field,
                # but only if it actually parses as dice. It usually doesn't:
                # it holds a weapon's name, which is the bug this replaces.
                legacy = char.get("weapon", "1d6")
                if _parse_dice(legacy) is not None:
                    combatant["dmg"] = combatant["dmg_large"] = str(legacy)
                    combatant["weapon"] = str(legacy)
                else:
                    warnings.append(
                        f"{char['label']}: no usable attacks[] on the sheet and "
                        f"weapon={legacy!r} is not dice notation — defaulted to 1d6. "
                        f"Add an attacks[] entry (name, thac0, damage_sm, damage_l, "
                        f"speed, attacks) before this fight matters."
                    )
                    combatant["weapon"] = str(legacy)

            _session["combatants"].append(combatant)
            party_loaded.append(char["label"])

        _persist_session()
        out = {
            "message":      f"Combat started. {len(party_loaded)} party member(s) loaded.",
            "party_loaded": party_loaded,
            "party_attacks": {
                c["name"]: {
                    "weapon":     c.get("weapon"),
                    "thac0":      c["thac0"],
                    "damage_sm":  c["dmg"],
                    "damage_l":   c["dmg_large"],
                    "speed":      c["weapon_speed"],
                    "per_round":  c["attacks_per_round"],
                    "alternates": [b["name"] for b in (c.get("attack_blocks") or [])[1:]],
                }
                for c in _session["combatants"] if c["side"] == "party"
            },
        }
        if warnings:
            out["warnings"] = warnings
        return out

    @mcp.tool()
    def add_combatant(
        name:              str,
        hp:                int,
        ac:                int,
        thac0:             int,
        dmg:               str = "1d6",
        side:              str = "enemy",
        weapon_speed:      int = 5,
        movement:          int = 6,
        dmg_large:         str = "",
        size:              str = "M",
        attacks_per_round: float = 1.0,
    ) -> dict:
        """Add a combatant (usually an enemy) to the active combat session.
        Call start_combat first. dmg is a dice notation string e.g. '2d6'.

        dmg_large: damage against Large-or-bigger targets (PHB Table 44's
                second column). Defaults to `dmg` — correct for most monsters,
                which have one damage line.
        size:   T/S/M/L/H/G, or a full monster size line ("Large (9' tall)").
                Decides which die attackers roll against this combatant.
        attacks_per_round: 1.5 for 3/2, 2 for two attacks, etc.

        Unparseable damage notation is rejected here rather than silently
        rolling a 1 at the table."""
        if _ensure_session() is None:
            return {"error": "No active combat session. Call start_combat first."}

        if _parse_dice(dmg) is None:
            return {"error": f"dmg={dmg!r} is not dice notation (e.g. '1d8', '2d6+1'). "
                             f"Pass the creature's damage dice, not its weapon's name."}
        if dmg_large and _parse_dice(dmg_large) is None:
            return {"error": f"dmg_large={dmg_large!r} is not dice notation."}

        _session["combatants"].append({
            "name":         name,
            "hp":           hp,
            "hp_max":       hp,
            "thac0":        thac0,
            "ac":           ac,
            "dmg":          dmg,
            "dmg_large":    dmg_large or dmg,
            "bonus_hit":    0,
            "bonus_dmg":    0,
            "weapon":       "",
            "weapon_speed": weapon_speed,
            "attacks_per_round": _attack_rate({"attacks": attacks_per_round}),
            "attacks_made": 0,
            "attacks_this_round": 0,
            "size":         _size_code(size),
            "attack_blocks": [],
            "side":         side,
            "init":         None,
            "conditions":   [],
            "effects":      [],
            "x":            None,
            "y":            None,
            "movement":     movement,
        })

        _persist_session()
        return {"name": name, "hp": hp, "ac": ac, "thac0": thac0, "side": side,
                "damage_sm": dmg, "damage_l": dmg_large or dmg,
                "size": _size_code(size)}

    @mcp.tool()
    def roll_initiative() -> dict:
        """Roll initiative for all combatants. Lower total (d10 + weapon_speed) acts first.
        Spellcasters who have declared a spell via declare_spell() will use casting_time
        instead of weapon_speed. Stores results and returns sorted order."""
        if _ensure_session() is None:
            return {"error": "No active combat session. Call start_combat first."}

        order = []
        for c in _session["combatants"]:
            d10   = random.randint(1, 10)
            total = d10 + c["weapon_speed"]
            c["init"] = total
            order.append({
                "name":   c["name"],
                "roll":   d10,
                "wspeed": c["weapon_speed"],
                "total":  total,
                "side":   c["side"],
            })

        _tally("initiative_rolls")
        order.sort(key=lambda x: x["total"])
        name_to_total = {e["name"]: e["total"] for e in order}
        _session["combatants"].sort(key=lambda c: name_to_total.get(c["name"], 999))
        _session["current_idx"] = 0

        _persist_session()
        return {"round": _session["round"], "order": order}

    @mcp.tool()
    def declare_spell(character: str, spell_name: str) -> dict:
        """Declare a spell before initiative is rolled.
        Looks up the spell's casting time in the 2e DB and sets the caster's
        weapon_speed to that value so roll_initiative() uses it correctly.
        Also adds the 'casting' condition so any damage before their turn disrupts the spell.
        Must be called before roll_initiative() each round the spell is cast."""
        if _ensure_session() is None:
            return {"error": "No active combat session. Call start_combat first."}

        caster = _find_combatant(character)
        if caster is None:
            return {"error": f"Combatant '{character}' not found in combat."}

        result = _spell_casting_time(spell_name)
        if result is None:
            ct_raw, ct_init = "unknown", 5
            db_found = False
        else:
            ct_raw, ct_init = result
            db_found = True

        # Set weapon_speed to casting time for initiative
        caster["weapon_speed"] = ct_init

        # Mark as casting so damage before their turn disrupts
        if "casting" not in caster["conditions"]:
            caster["conditions"].append("casting")

        _persist_session()
        return {
            "caster":           caster["name"],
            "spell":            spell_name,
            "casting_time":     ct_raw if db_found else "not found in DB — defaulting to 5",
            "casting_time_init": ct_init,
            "effect":           f"Initiative will be d10+{ct_init}. Spell disrupted if hit before turn.",
        }

    @mcp.tool()
    def attack(attacker: str, target: str, condition: str = "",
               weapon: str = "") -> dict:
        """Resolve one attack roll. Natural 20 always hits; natural 1 always misses.

        condition options: 'rear' (+2 hit), 'charging' (+2 hit, AC penalty),
        'set_vs_charge' (double damage on hit), 'fleeing' (auto-hit).
        HP floor: 0 = unconscious (stable), -1 to -9 = bleeding (1 HP/round), -10 = dead.

        weapon: pick one of the attacker's sheet attack blocks by name, matched
            case-insensitively on a substring — `weapon="undead"` selects
            "Sunsweep long sword +3 (vs undead)" with its own THAC0 and dice.
            The choice sticks for later swings until changed. Omit to keep the
            current weapon. start_combat lists each PC's alternates.

        Damage uses the S/M die or the Large die depending on the target's
        recorded size (PHB Table 44) — a long sword rolls 1d8 against an orc
        and 1d12 against a troll.

        Multiple attacks per round are tracked: a 3/2 specialist gets one swing
        in round 1 and two in round 2, and the result reports how many remain."""
        if _ensure_session() is None:
            return {"error": "No active combat session. Call start_combat first."}

        atk = _find_combatant(attacker)
        if atk is None:
            return {"error": f"Attacker '{attacker}' not found in combat."}
        tgt = _find_combatant(target)
        if tgt is None:
            return {"error": f"Target '{target}' not found in combat."}

        notes: list[str] = []
        cond = condition.lower().strip()

        if weapon:
            block, sel_note = _select_attack(atk, weapon)
            if block is not None:
                _apply_attack_block(atk, block)
                notes.append(f"{atk['name']} attacks with {block['name']} "
                             f"(THAC0 {atk['thac0']}, {block['damage_sm']}/{block['damage_l']}).")
            elif sel_note:
                notes.append(sel_note)

        if cond == "fleeing":
            roll = 10
            hit  = True
            notes.append("Target is fleeing — automatic hit.")
        else:
            roll      = random.randint(1, 20)
            hit_bonus = atk["bonus_hit"]

            # Active-effect modifiers on attacker (to_hit) and target (ac)
            atk_eff_hit = _sum_modifier(atk, "to_hit")
            tgt_eff_ac  = _sum_modifier(tgt, "ac")
            if atk_eff_hit:
                hit_bonus += atk_eff_hit
                notes.append(f"{atk_eff_hit:+d} to hit (active effects on {atk['name']}).")
            if tgt_eff_ac:
                notes.append(f"{tgt_eff_ac:+d} AC modifier on {tgt['name']} (active effects).")

            if cond == "rear":
                hit_bonus += 2
                notes.append("+2 to hit (rear attack).")
            elif cond == "charging":
                hit_bonus += 2
                if "charging_ac_penalty" not in atk["conditions"]:
                    atk["conditions"].append("charging_ac_penalty")
                notes.append("+2 to hit (charging). Attacker has AC penalty this round.")

            if roll == 20:
                hit = True
                notes.append("Natural 20 — critical hit!")
            elif roll == 1:
                hit = False
                notes.append("Natural 1 — automatic miss!")
            else:
                effective_roll = roll + hit_bonus
                target_ac = tgt["ac"] + tgt_eff_ac
                # No second +2 for a rear attack here: the bonus is already in
                # hit_bonus above, and adding it to the target's AC as well was
                # applying it twice (a net +4 on a documented +2).
                hit = effective_roll >= (atk["thac0"] - target_ac)

        # Spell disruption on target
        spell_disrupted = False
        if "casting" in tgt["conditions"]:
            tgt["conditions"].remove("casting")
            spell_disrupted = True
            notes.append(f"{tgt['name']} was casting — spell disrupted!")

        damage   = None
        hp_after = None
        downed   = False
        dead     = False
        notation = None

        if hit:
            atk_eff_dmg = _sum_modifier(atk, "dmg")
            large = _takes_large_damage(tgt.get("size"))
            notation = (atk.get("dmg_large") or atk["dmg"]) if large else atk["dmg"]
            if large and notation != atk["dmg"]:
                notes.append(f"{tgt['name']} is size {_size_code(tgt.get('size'))} — "
                             f"rolling the Large damage die ({notation}).")
            raw_dmg = _roll_dice(notation) + atk["bonus_dmg"] + atk_eff_dmg
            if atk_eff_dmg:
                notes.append(f"{atk_eff_dmg:+d} damage (active effects).")
            if cond == "set_vs_charge":
                raw_dmg *= 2
                notes.append("Set vs. charge — double damage!")
            damage    = max(0, raw_dmg)
            tgt["hp"] = max(-10, tgt["hp"] - damage)
            hp_after  = tgt["hp"]
            dead      = tgt["hp"] <= -10
            downed    = tgt["hp"] <= 0
            if dead:
                notes.append(f"{tgt['name']} is dead!")
            elif downed:
                notes.append(f"{tgt['name']} is unconscious!")
                if tgt["hp"] < 0:
                    notes.append(f"Bleeding: loses 1 HP/round until stabilised or dead at -10.")

        atk["attacks_made"]      = int(atk.get("attacks_made") or 0) + 1
        atk["attacks_this_round"] = int(atk.get("attacks_this_round") or 0) + 1
        remaining = _attacks_remaining_this_round(atk, _session.get("round", 1))

        result = {
            "attacker":        atk["name"],
            "target":          tgt["name"],
            "weapon":          atk.get("weapon") or None,
            "roll":            roll,
            "hit":             hit,
            "damage":          damage,
            "damage_die":      notation if hit else None,
            "hp_after":        hp_after,
            "downed":          downed,
            "dead":            dead,
            "spell_disrupted": spell_disrupted,
            "attacks_per_round":            atk.get("attacks_per_round", 1),
            "attacks_remaining_this_round": remaining,
            "notes":           notes,
        }
        if remaining:
            notes.append(f"{atk['name']} has {remaining} more attack(s) this round "
                         f"({atk.get('attacks_per_round')}/round).")
        if downed:
            morale = _check_morale_trigger(tgt["side"])
            if morale:
                result["morale_due"] = morale
        _tally("attacks")
        _persist_session()
        return result

    @mcp.tool()
    def apply_combat_damage(name: str, amount: int) -> dict:
        """Apply direct damage to a named combatant (e.g. from spells or traps).
        If the target was casting, their spell is disrupted.
        HP floor: 0 = unconscious, -10 = dead."""
        if _ensure_session() is None:
            return {"error": "No active combat session. Call start_combat first."}

        c = _find_combatant(name)
        if c is None:
            return {"error": f"Combatant '{name}' not found."}

        spell_disrupted = False
        if "casting" in c["conditions"]:
            c["conditions"].remove("casting")
            spell_disrupted = True

        hp_before = c["hp"]
        c["hp"]   = max(-10, c["hp"] - amount)
        downed    = c["hp"] <= 0
        dead      = c["hp"] <= -10

        result = {
            "name":            c["name"],
            "hp_before":       hp_before,
            "hp_after":        c["hp"],
            "downed":          downed,
            "dead":            dead,
            "spell_disrupted": spell_disrupted,
        }
        if downed:
            morale = _check_morale_trigger(c["side"])
            if morale:
                result["morale_due"] = morale
        _tally("damage_applications")
        _persist_session()
        return result

    @mcp.tool()
    def apply_combat_heal(name: str, amount: int) -> dict:
        """Apply healing directly to a combatant in the active session.
        Caps at hp_max. Removes 'unconscious' status if HP rises above 0.
        Use this during combat instead of apply_heal (which writes to the character file
        but does not update the in-session HP tracker)."""
        if _ensure_session() is None:
            return {"error": "No active combat session. Call start_combat first."}

        c = _find_combatant(name)
        if c is None:
            return {"error": f"Combatant '{name}' not found."}

        hp_before = c["hp"]
        hp_max    = c.get("hp_max", hp_before)
        c["hp"]   = min(hp_before + amount, hp_max)
        healed    = c["hp"] - hp_before

        notes = []
        if hp_before <= 0 and c["hp"] > 0:
            notes.append(f"{c['name']} regains consciousness.")

        _persist_session()
        return {
            "name":     c["name"],
            "healed":   healed,
            "hp_before": hp_before,
            "hp_after":  c["hp"],
            "notes":    notes,
        }

    @mcp.tool()
    def add_condition(name: str, condition: str) -> dict:
        """Add a condition string to a combatant (e.g. 'stunned', 'prone', 'casting').
        No-op if condition is already present."""
        if _ensure_session() is None:
            return {"error": "No active combat session. Call start_combat first."}

        c = _find_combatant(name)
        if c is None:
            return {"error": f"Combatant '{name}' not found."}

        if condition not in c["conditions"]:
            c["conditions"].append(condition)

        _persist_session()
        return {"name": c["name"], "conditions": list(c["conditions"])}

    @mcp.tool()
    def remove_condition(name: str, condition: str) -> dict:
        """Remove a condition from a combatant. No-op if condition is not present."""
        if _ensure_session() is None:
            return {"error": "No active combat session. Call start_combat first."}

        c = _find_combatant(name)
        if c is None:
            return {"error": f"Combatant '{name}' not found."}

        if condition in c["conditions"]:
            c["conditions"].remove(condition)

        _persist_session()
        return {"name": c["name"], "conditions": list(c["conditions"])}

    @mcp.tool()
    def apply_effect(
        target:    str,
        name:      str,
        duration:  int,
        to_hit:    int = 0,
        ac:        int = 0,
        dmg:       int = 0,
        save:      int = 0,
        source:    str = "",
    ) -> dict:
        """Attach a timed effect (buff or debuff) to a combatant.
        Auto-decrements at the start of each new round and expires when duration hits 0.
        Numeric modifiers are auto-applied:
          to_hit: delta to attacker's d20 attack roll (e.g. bless +1)
          ac:     delta to AC when targeted (negative = harder to hit; e.g. shield -1)
          dmg:    delta to damage rolls
          save:   not auto-applied — saving_throw() does not consult effects;
                  surfaced for the DM's reference when calling it manually
        duration: rounds remaining; 1 = expires at start of next round.
        Examples:
          apply_effect("Maren", "bless", 6, to_hit=1)
          apply_effect("Goblin #2", "stinking cloud", 3, to_hit=-2)
          apply_effect("Torven", "haste", 9, to_hit=0)  # mark, narrate doubled actions
        """
        if _ensure_session() is None:
            return {"error": "No active combat session. Call start_combat first."}

        c = _find_combatant(target)
        if c is None:
            return {"error": f"Combatant '{target}' not found."}

        eff = {"name": name, "duration": int(duration)}
        if to_hit: eff["to_hit"] = int(to_hit)
        if ac:     eff["ac"]     = int(ac)
        if dmg:    eff["dmg"]    = int(dmg)
        if save:   eff["save"]   = int(save)
        if source: eff["source"] = source

        c.setdefault("effects", []).append(eff)
        _persist_session()
        return {"target": c["name"], "effect": eff, "active_count": len(c["effects"])}

    @mcp.tool()
    def remove_effect(target: str, name: str) -> dict:
        """Remove a named effect from a combatant before its natural expiry
        (e.g. dispel magic, save succeeded on next round, target moved out of AOE).
        Removes only the first matching effect; call again if duplicates exist."""
        if _ensure_session() is None:
            return {"error": "No active combat session. Call start_combat first."}

        c = _find_combatant(target)
        if c is None:
            return {"error": f"Combatant '{target}' not found."}

        effects = c.get("effects", [])
        for i, eff in enumerate(effects):
            if eff.get("name", "").lower() == name.lower():
                removed = effects.pop(i)
                _persist_session()
                return {"target": c["name"], "removed": removed, "remaining": len(effects)}
        return {"error": f"No active effect named '{name}' on {c['name']}."}

    @mcp.tool()
    def next_turn() -> dict:
        """Advance to the next combatant in initiative order, skipping those who cannot act
        (HP <= 0). Wraps to round start and increments the round counter.
        At each new round: party members with HP between -1 and -9 lose 1 HP (bleeding).
        Dead combatants (HP <= -10) are always skipped."""
        global _session
        if _ensure_session() is None:
            return {"error": "No active combat session. Call start_combat first."}

        combatants = _session["combatants"]
        n = len(combatants)
        if n == 0:
            return {"error": "No combatants in session."}

        start_idx = _session["current_idx"]
        idx       = start_idx
        bleeding_report = []

        for _ in range(n):
            idx = (idx + 1) % n

            if idx == 0:
                # New round — increment counter and apply bleeding
                _session["round"] += 1
                # Unspent attacks do not carry over.
                for c in combatants:
                    c["attacks_this_round"] = 0
                for c in combatants:
                    if c["side"] == "party" and -10 < c["hp"] < 0:
                        c["hp"] = max(-10, c["hp"] - 1)
                        status = "dead" if c["hp"] <= -10 else f"hp now {c['hp']}"
                        bleeding_report.append(f"{c['name']} bleeds 1 HP ({status})")
                # Decrement active effects; remove expired
                for c in combatants:
                    surviving = []
                    for eff in c.get("effects", []):
                        d = eff.get("duration", 0)
                        if d <= 1:
                            bleeding_report.append(f"{c['name']}: '{eff.get('name','effect')}' expires.")
                        else:
                            eff["duration"] = d - 1
                            surviving.append(eff)
                    c["effects"] = surviving

            c = combatants[idx]
            if c["hp"] > 0:
                _session["current_idx"] = idx
                result = {
                    "round":   _session["round"],
                    "current": c["name"],
                    "init":    c["init"],
                }
                if bleeding_report:
                    result["bleeding"] = bleeding_report
                _persist_session()
                return result

        return {"error": "All combatants are down."}

    @mcp.tool()
    def combat_status() -> dict:
        """Return full current combat status: round, turn, and all combatants with
        HP, AC, initiative, side, conditions, and alive/dead status."""
        if _ensure_session() is None:
            return {"error": "No active combat session. Call start_combat first."}

        combatants_out = []
        for i, c in enumerate(_session["combatants"]):
            status = "active"
            if c["hp"] <= -10:
                status = "dead"
            elif c["hp"] <= 0:
                status = "unconscious"
            entry = {
                "name":       c["name"],
                "side":       c["side"],
                "hp":         c["hp"],
                "hp_max":     c["hp_max"],
                "ac":         c["ac"],
                "thac0":      c["thac0"],
                "init":       c["init"],
                "size":       c.get("size", "M"),
                "conditions": list(c["conditions"]),
                "effects":    list(c.get("effects", [])),
                "status":     status,
                "current":    i == _session["current_idx"],
            }
            if c.get("weapon"):
                entry["weapon"] = c["weapon"]
            entry["damage_sm"] = c.get("dmg")
            entry["damage_l"]  = c.get("dmg_large", c.get("dmg"))
            if float(c.get("attacks_per_round") or 1) != 1.0:
                entry["attacks_per_round"] = c["attacks_per_round"]
                entry["attacks_remaining_this_round"] = _attacks_remaining_this_round(
                    c, _session.get("round", 1))
            combatants_out.append(entry)

        combatants_out.sort(key=lambda x: (x["init"] is None, x["init"] or 999))
        current = _session["combatants"][_session["current_idx"]]
        return {
            "round":      _session["round"],
            "current":    current["name"],
            "combatants": combatants_out,
        }

    @mcp.tool()
    def end_combat() -> dict:
        """End combat: save all party members' current HP back to state.json,
        record a combat event, and clear the in-memory session."""
        global _session
        if _ensure_session() is None:
            return {"error": "No active combat session."}

        cfg   = _c.load_campaign()
        state = _c.load_state(cfg)

        rounds   = _session["round"]
        party_hp = {}
        saved    = {}

        for c in _session["combatants"]:
            if c["side"] != "party":
                continue
            key = c.get("_key")
            if key and key in state.get("characters", {}):
                state["characters"][key]["hp"] = c["hp"]
                party_hp[c["name"]] = c["hp"]
                saved[c["name"]]    = c["hp"]

        _c.save_state(cfg, state)
        pipeline = dict(_session.get("pipeline") or {})
        _c.append_event(cfg, {
            "type":     "combat",
            "outcome":  "ended",
            "rounds":   rounds,
            "party_hp": party_hp,
            # What the tracker actually resolved. The audit reads this to tell
            # a real fight from one narrated freehand (Hard Constraint #8).
            "pipeline": pipeline,
        })

        # Remove dashboard map state file
        cs_file = cfg["_dir"] / "combat_state.json"
        if cs_file.exists():
            cs_file.unlink()
        _clear_session_file()

        _session = None
        return {"saved": saved, "rounds": rounds}

    # --- Composite pipeline tools -------------------------------------------
    # Each mandated step used to cost one model round trip (~2.6 s of prefill
    # plus a few hundred thinking tokens). A 7-combatant encounter measured 16
    # round trips just to get to the first swing. These tools run the same
    # sequence server-side, in order, and return every result at once — the
    # dice still land before the DM writes a word, which is the point of
    # CLAUDE.md Hard Constraint #1.

    @mcp.tool()
    async def setup_combat(
        enemies:        list[dict],
        party_modifier: int = 0,
        enemy_modifier: int = 0,
        surprise:       bool = True,
        initiative:     bool = True,
    ) -> dict:
        """Stand up a whole encounter in one call: start_combat → monster_lookup
        per enemy type → add_combatant per individual → surprise_check →
        roll_initiative. Replaces the 10-16 separate calls the pipeline used to
        need (CLAUDE.md Hard Constraint #8) with one.

        enemies: list of type specs, e.g.
            [{"name": "goblin", "count": 3},
             {"name": "goblin shaman", "count": 1, "hp": 9, "dmg": "1d6"}]
          - name:  monster name (partial match against the 2e monster DB)
          - count: how many individuals (default 1); each gets its own HP roll
                   and a numbered label ("Goblin 1", "Goblin 2", …)
          - label: display name override (numbered when count > 1)
          - hp / ac / thac0 / dmg / morale / weapon_speed / movement: explicit
            overrides; anything omitted is read from the monster DB
          - side:  defaults to "enemy" (use e.g. "ally" for a third party)

        Returns the party roster, every enemy's rolled HP and combat-relevant
        stats (morale rating, treasure type, intelligence, specials — so no
        follow-up monster_lookup is needed for morale_check or treasure), the
        surprise result, and the initiative order.

        Set surprise=False only when the encounter genuinely can't involve
        surprise (both sides already engaged). Set initiative=False when
        casters still need declare_spell() first — then call roll_initiative()
        yourself once the declarations are in."""
        if not isinstance(enemies, list) or not enemies:
            return {"error": "enemies must be a non-empty list of specs, e.g. [{'name':'goblin','count':3}]"}

        start = start_combat()
        added: list[dict] = []
        notes: list[str] = []

        for spec in enemies:
            if not isinstance(spec, dict):
                notes.append(f"Skipped non-dict enemy spec: {spec!r}")
                continue
            raw_name = str(spec.get("name") or "").strip()
            if not raw_name:
                notes.append("Skipped enemy spec with no name.")
                continue

            count = max(1, int(spec.get("count") or 1))
            stats = _monster_stats(raw_name)
            if stats is None:
                notes.append(f"'{raw_name}' not found in the monster DB — using the supplied/default stats.")
                stats = {}

            hd = _parse_hd(stats.get("hit_dice"))
            ac    = int(spec["ac"])    if spec.get("ac")    is not None else _parse_ac(stats.get("ac"))
            thac0 = int(spec["thac0"]) if spec.get("thac0") is not None else _first_int(stats.get("thac0"), 20)
            if spec.get("dmg"):
                dmg, dmg_parsed = str(spec["dmg"]), True
            else:
                dmg, dmg_parsed = _parse_damage(stats.get("damage"))
            morale = int(spec["morale"]) if spec.get("morale") is not None else _parse_morale(stats.get("morale"))
            wspeed = int(spec.get("weapon_speed") or 5)
            movement = int(spec.get("movement") or 6)
            side = str(spec.get("side") or "enemy")
            if spec.get("label"):
                base_label = str(spec["label"]).strip()
            else:
                # DB names are lower-case ("goblin shaman"); a display label
                # the caller supplied is left exactly as they wrote it.
                base_label = str(stats.get("name") or raw_name).strip().title()

            size_code = _size_code(spec.get("size") or stats.get("size") or "M")
            # A plain integer attack line ("3") is a real attack rate; anything
            # else ("1 or 2", "See below") stays at 1 and the note below tells
            # the DM to resolve the extras by hand.
            atk_raw = str(stats.get("no_of_attacks") or "").strip()
            atk_rate = float(atk_raw) if re.fullmatch(r'\d+', atk_raw) else 1.0
            if spec.get("attacks_per_round") is not None:
                atk_rate = _attack_rate({"attacks": spec["attacks_per_round"]})

            if spec.get("hp") is None and hd.get("dice", 0) <= 0 and hd.get("fixed") is None:
                notes.append(
                    f"'{base_label}' HD reads {hd.get('raw')!r} — no rollable dice. "
                    f"Defaulted to 1d8; pass hp= to set it honestly."
                )
            if not dmg_parsed:
                notes.append(
                    f"'{base_label}' damage reads {stats.get('damage')!r} — no dice in the line. "
                    f"Defaulted to {dmg}; pass dmg= (e.g. from the creature's weapon) before it swings."
                )
            if atk_raw and atk_raw not in ("1", "Nil"):
                if atk_rate > 1:
                    notes.append(
                        f"'{base_label}' gets {atk_rate:g} attacks/round — the tracker holds its "
                        f"segment until they are all spent. Its damage line is "
                        f"{stats.get('damage')!r}: if the attacks differ, pass dmg= per swing."
                    )
                else:
                    notes.append(
                        f"'{base_label}' attack line reads {atk_raw!r} ({stats.get('damage')!r}) — "
                        f"left at 1/round; pass attacks_per_round= if it should be more."
                    )

            group = {
                "type":            base_label,
                "size_code":       size_code,
                "attacks_per_round": atk_rate,
                "matched":         stats.get("name"),
                "count":           count,
                "ac":              ac,
                "thac0":           thac0,
                "damage":          dmg,
                "hit_dice":        hd.get("raw"),
                "morale_rating":   morale,
                "individuals":     [],
            }
            for extra in ("treasure_type", "intelligence", "alignment", "size", "move",
                          "no_of_attacks", "special_attacks", "special_defenses",
                          "magic_resistance", "xp_value"):
                if stats.get(extra) not in (None, ""):
                    group[extra] = stats[extra]

            for i in range(count):
                if spec.get("hp") is not None:
                    hp = int(spec["hp"])
                else:
                    hp = _roll_monster_hp(hd)
                    if hp is None:
                        # HD line carried no usable number; 1d8 keeps the fight
                        # moving and `notes` tells the DM to set it properly.
                        hp = random.randint(1, 8)
                label = f"{base_label} {i + 1}" if count > 1 else base_label
                add_combatant(
                    name=label, hp=hp, ac=ac, thac0=thac0, dmg=dmg,
                    side=side, weapon_speed=wspeed, movement=movement,
                    size=size_code, attacks_per_round=atk_rate,
                )
                group["individuals"].append({"name": label, "hp": hp})
            added.append(group)

        out: dict = {
            "party_loaded": start.get("party_loaded", []),
            "enemies":      added,
        }
        if surprise:
            out["surprise"] = await _call_tool(
                mcp, "surprise_check",
                party_modifier=party_modifier, enemy_modifier=enemy_modifier,
            )
        if initiative:
            out["initiative"] = roll_initiative()
        else:
            out["initiative"] = "not rolled — declare_spell() then call roll_initiative()"
        if notes:
            out["notes"] = notes
        return out

    @mcp.tool()
    def resolve_attack(
        attacker:  str,
        target:    str,
        condition: str = "",
        weapon:    str = "",
        advance:   bool = True,
    ) -> dict:
        """Resolve one attack and advance the turn: attack() (which rolls to
        hit, picks the S/M or Large damage die, rolls damage and applies it)
        followed by next_turn().

        condition: 'rear' | 'charging' | 'set_vs_charge' | 'fleeing' (see attack)
        weapon:    select one of the attacker's sheet attack blocks by name
                   (substring match), e.g. weapon="undead"
        advance:   True (default) advances only once the attacker has used every
                   attack they are entitled to this round, so a 3/2 specialist
                   keeps their segment for the second swing automatically.
                   False never advances.

        Returns the attack result plus `morale_due` when a side has just crossed
        50% casualties (call morale_check with the rating setup_combat returned)
        and, when the turn moved on, `next` describing whose segment it is."""
        result = attack(attacker=attacker, target=target, condition=condition,
                        weapon=weapon)
        if "error" in result:
            return result
        if advance:
            if result.get("attacks_remaining_this_round"):
                result["next"] = (
                    f"{result['attacker']} still has "
                    f"{result['attacks_remaining_this_round']} attack(s) this round — "
                    f"turn not advanced."
                )
            else:
                result["next"] = next_turn()
        return result

    @mcp.tool()
    def combat_round(actions: list[dict], order: str = "initiative") -> dict:
        """Resolve a sequence of attacks in one call — typically a whole combat
        round.

        actions: [{"attacker": "Garrek Drask", "target": "Goblin 1",
                   "condition": "charging", "weapon": "undead",
                   "advance": true}, ...]
          - condition: optional, as per attack()
          - weapon:    optional, selects one of the attacker's sheet attack
                       blocks by name (substring match)
          - advance:   optional, default true — advances once the attacker has
                       spent the attacks they are owed this round
        order:   "initiative" (default) sorts the declared actions by each
                 attacker's rolled initiative, so declaration order can't
                 quietly override the dice; "given" resolves them as listed
                 (for a segment you are deliberately sequencing by hand).

        Actions whose attacker is already down (HP <= 0) are skipped and
        reported rather than rolled, so a round declared up-front stays honest
        when someone drops mid-round. Anything that isn't a plain weapon attack
        — spells, turning undead, saves, fleeing — still needs its own call;
        split the round around it.

        Returns per-action results, any morale checks that came due, and the
        round/turn state afterwards."""
        if _ensure_session() is None:
            return {"error": "No active combat session. Call setup_combat or start_combat first."}
        if not isinstance(actions, list) or not actions:
            return {"error": "actions must be a non-empty list of {attacker, target} dicts."}

        queue = list(actions)
        if str(order).lower().startswith("init"):
            def _init_of(act):
                if not isinstance(act, dict):
                    return (1, 999)
                c = _find_combatant(str(act.get("attacker") or ""))
                if c is None or c.get("init") is None:
                    return (1, 999)
                return (0, c["init"])
            queue.sort(key=_init_of)

        results: list[dict] = []
        morale_due: list[dict] = []
        for act in queue:
            if not isinstance(act, dict):
                results.append({"skipped": f"not a dict: {act!r}"})
                continue
            attacker = str(act.get("attacker") or "")
            target   = str(act.get("target") or "")
            atk = _find_combatant(attacker)
            if atk is None:
                results.append({"attacker": attacker, "skipped": "attacker not found in combat"})
                continue
            if atk["hp"] <= 0:
                results.append({
                    "attacker": atk["name"],
                    "skipped":  "down — cannot act"
                            if atk["hp"] > -10 else "dead — cannot act",
                })
                continue
            r = resolve_attack(
                attacker=attacker,
                target=target,
                condition=str(act.get("condition") or ""),
                weapon=str(act.get("weapon") or ""),
                advance=bool(act.get("advance", True)),
            )
            if r.get("morale_due"):
                morale_due.append(r["morale_due"])
            results.append(r)

        out = {"actions": results, "status": combat_status()}
        if morale_due:
            out["morale_due"] = morale_due
        # A 3/2 fighter entitled to two swings this round who was only given one
        # action has an attack going spare — say so rather than lose it quietly.
        unused = {
            c["name"]: _attacks_remaining_this_round(c, _session.get("round", 1))
            for c in _session["combatants"]
            if c["hp"] > 0 and _attacks_remaining_this_round(c, _session.get("round", 1))
            and any(isinstance(a, dict) and _find_combatant(str(a.get("attacker") or "")) is c
                    for a in queue)
        }
        if unused:
            out["unused_attacks"] = unused
        return out

