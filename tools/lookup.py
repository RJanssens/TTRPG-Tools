"""Monster lookup, spell lookup, class lookup, and treasure generation tools."""
import json
import sqlite3
import subprocess
from pathlib import Path
import _campaign as _c

BASE_DIR = Path(__file__).parent.parent

_MONSTERS_DB_CANDIDATES = [
    BASE_DIR / "global" / "monsters.db",
    BASE_DIR.parent / "ttrpg" / "global" / "monsters.db",
]

_2E_DB_CANDIDATES = [
    BASE_DIR / "global" / "2e.db",
]


def _find_db(candidates) -> Path | None:
    for p in candidates:
        if p.exists():
            return p
    return None


def register(mcp):

    @mcp.tool()
    def monster_lookup(name: str) -> dict:
        """Look up a monster by name in the AD&D 2e monster database.
        Returns stats including HD, THAC0, AC, damage, treasure type, morale, and XP value.
        Supports partial/case-insensitive name matching."""
        db_path = _find_db(_MONSTERS_DB_CANDIDATES)
        if db_path is None:
            return {"error": "monsters.db not found."}

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM monsters WHERE name LIKE ? COLLATE NOCASE",
                (f"%{name}%",),
            )
            row = cur.fetchone()
            if row is None:
                return {"error": f"No monster matching '{name}' found."}

            return {
                "name":              row["name"],
                "climate_terrain":   row["climate_terrain"],
                "frequency":         row["frequency"],
                "no_appearing":      row["no_appearing"],
                "ac":                row["armor_class"],
                "move":              row["move"],
                "hit_dice":          row["hit_dice"],
                "thac0":             row["thac0"],
                "pct_in_lair":       row["pct_in_lair"],
                "treasure_type":     row["treasure_type"],
                "no_of_attacks":     row["no_of_attacks"],
                "damage":            row["damage_attack"],
                "special_attacks":   row["special_attacks"],
                "special_defenses":  row["special_defenses"],
                "magic_resistance":  row["magic_resistance"],
                "intelligence":      row["intelligence"],
                "alignment":         row["alignment"],
                "size":              row["size"],
                "psionic_ability":   row["psionic_ability"],
                "attack_defense_modes": row["attack_defense_modes"],
                "morale":            row["morale"],
                "xp_value":          row["xp_value"],
                "description":       row["description"],
            }
        finally:
            conn.close()

    @mcp.tool()
    def spell_lookup(name: str, caster: str = "") -> dict:
        """Look up a spell by name in the AD&D 2e spell database.
        Returns level, school, caster type, casting time (and its initiative value),
        range, area of effect, duration, saving throw, components, and description.
        caster: optional filter — 'wizard', 'priest', or blank for either.
        Supports partial/case-insensitive name matching.
        casting_time_init is the integer used for initiative (1–10; 10 = full round or longer)."""
        db_path = _find_db(_2E_DB_CANDIDATES)
        if db_path is None:
            return {"error": "2e.db not found. Run tools/build_2e_db.py first."}

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            if caster:
                cur = conn.execute(
                    "SELECT * FROM spells WHERE name LIKE ? COLLATE NOCASE AND caster LIKE ? COLLATE NOCASE LIMIT 1",
                    (f"%{name}%", f"%{caster}%"),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM spells WHERE name LIKE ? COLLATE NOCASE LIMIT 1",
                    (f"%{name}%",),
                )
            row = cur.fetchone()
            if row is None:
                return {"error": f"No spell matching '{name}'" + (f" for caster '{caster}'" if caster else "") + " found."}

            return {
                "name":             row["name"],
                "level":            row["level"],
                "caster":           row["caster"],
                "school":           row["school"],
                "casting_time":     row["casting_time"],
                "casting_time_init": row["casting_time_init"],
                "range":            row["range"],
                "aoe":              row["aoe"],
                "duration":         row["duration"],
                "save":             row["save"],
                "damage":           row["damage"] or None,
                "components": {
                    "verbal":   bool(row["verbal"]),
                    "somatic":  bool(row["somatic"]),
                    "material": bool(row["material"]),
                    "details":  row["materials"],
                },
                "reversible":       bool(row["reversible"]),
                "source":           row["source"],
                "page":             row["page"],
                "description":      row["description"],
            }
        finally:
            conn.close()

    @mcp.tool()
    def class_lookup(class_name: str, level: int = 0) -> dict:
        """Look up AD&D 2e class progression data.
        If level > 0: returns that level's stats (THAC0, saves, spell slots, XP required,
        XP to next level). Also returns special abilities gained at that level.
        If level == 0: returns class overview (hit die, prime requisite, XP table summary,
        and all special abilities).
        class_name: Fighter, Mage, Cleric, Thief, Ranger, Paladin, Druid, Bard, etc."""
        db_path = _find_db(_2E_DB_CANDIDATES)
        if db_path is None:
            return {"error": "2e.db not found. Run tools/build_2e_db.py first."}

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            cls_row = conn.execute(
                "SELECT * FROM classes WHERE name LIKE ? COLLATE NOCASE LIMIT 1",
                (f"%{class_name}%",),
            ).fetchone()
            if cls_row is None:
                return {"error": f"No class matching '{class_name}' found."}

            class_id = cls_row["id"]
            special_abilities = json.loads(cls_row["special_abilities"] or "[]")
            prime_req = cls_row["prime_requisite"]
            try:
                prime_req = json.loads(prime_req) if prime_req else []
            except (json.JSONDecodeError, TypeError):
                pass

            if level > 0:
                lvl_row = conn.execute(
                    "SELECT * FROM class_levels WHERE class_id=? AND level=?",
                    (class_id, level),
                ).fetchone()
                if lvl_row is None:
                    return {"error": f"No data for {cls_row['name']} level {level}."}

                next_row = conn.execute(
                    "SELECT xp_required FROM class_levels WHERE class_id=? AND level=?",
                    (class_id, level + 1),
                ).fetchone()

                spell_slots = json.loads(lvl_row["spell_slots"]) if lvl_row["spell_slots"] else None
                turn_undead = json.loads(lvl_row["turn_undead"]) if lvl_row["turn_undead"] else None

                return {
                    "class":        cls_row["name"],
                    "level":        level,
                    "xp_required":  lvl_row["xp_required"],
                    "xp_next_level": next_row["xp_required"] if next_row else "max level",
                    "hit_dice":     lvl_row["hit_dice"],
                    "attacks":      lvl_row["attacks"],
                    "thac0":        lvl_row["thac0"],
                    "saving_throws": {
                        "paralysis_poison_death": lvl_row["save_paralysis"],
                        "rod_staff_wand":         lvl_row["save_rsw"],
                        "petrify_polymorph":      lvl_row["save_petrify"],
                        "breath_weapon":          lvl_row["save_breath"],
                        "spell":                  lvl_row["save_spell"],
                    },
                    "spell_slots":  spell_slots,
                    "turn_undead":  turn_undead,
                    "special_abilities": special_abilities,
                }
            else:
                # Overview: XP table
                level_rows = conn.execute(
                    "SELECT level, xp_required, hit_dice, thac0 FROM class_levels WHERE class_id=? ORDER BY level",
                    (class_id,),
                ).fetchall()
                xp_table = [
                    {"level": r["level"], "xp": r["xp_required"], "hd": r["hit_dice"], "thac0": r["thac0"]}
                    for r in level_rows
                ]
                return {
                    "class":             cls_row["name"],
                    "hit_die":           cls_row["hit_die"],
                    "prime_requisite":   prime_req,
                    "special_abilities": special_abilities,
                    "xp_table":          xp_table,
                }
        finally:
            conn.close()

    @mcp.tool()
    def generate_treasure(treasure_types: str) -> dict:
        """Generate treasure for one or more AD&D 2e treasure types.
        treasure_types is a space-separated string such as 'A', 'A B', or 'Qx5 S'.
        Uses DMG Tables 84-110: coins, gems, art objects, and magic items."""
        types_list = treasure_types.split()
        if not types_list:
            return {"error": "No treasure types provided."}

        treasure_candidates = [
            BASE_DIR / "treasure.py",
            BASE_DIR.parent / "ttrpg" / "treasure.py",
        ]
        treasure_py = next((p for p in treasure_candidates if p.exists()), None)
        if treasure_py is None:
            return {"error": "treasure.py not found."}

        result = subprocess.run(
            ["python3", str(treasure_py)] + types_list,
            capture_output=True,
            text=True,
            cwd=str(treasure_py.parent),
        )

        return {
            "types":  treasure_types,
            "result": result.stdout,
            "error":  result.stderr if result.stderr else None,
        }
