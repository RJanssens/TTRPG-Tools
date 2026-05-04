"""Combat map — DSL parser, grid state, combatant positioning."""
import json
from pathlib import Path

import _campaign as _c
from tools.combat import get_session

BASE_DIR = Path(__file__).parent.parent

# Built-in terrain styles — override or extend with STYLE declarations in DSL
_DEFAULT_STYLES: dict[str, dict] = {
    "WALL":   {"color": "#5C3D2E", "symbol": "█", "passable": "no"},
    "DOOR":   {"color": "#8B6914", "symbol": "D", "passable": "yes"},
    "ROCK":   {"color": "#696969", "symbol": "◆", "passable": "no"},
    "PILLAR": {"color": "#808080", "symbol": "O", "passable": "no"},
    "COLUMN": {"color": "#808080", "symbol": "O", "passable": "no"},
    "TABLE":  {"color": "#7B5C3A", "symbol": "T", "passable": "no"},
    "WATER":  {"color": "#4169E1", "symbol": "~", "passable": "slow"},
    "PIT":    {"color": "#1a1a1a", "symbol": "○", "passable": "fall"},
    "TREE":   {"color": "#228B22", "symbol": "♠", "passable": "no"},
    "CHEST":  {"color": "#DAA520", "symbol": "C", "passable": "no"},
    "FIRE":   {"color": "#FF4500", "symbol": "*", "passable": "no"},
    "STATUE": {"color": "#9090A0", "symbol": "S", "passable": "no"},
    "ALTAR":  {"color": "#8B0000", "symbol": "A", "passable": "no"},
    "BARREL": {"color": "#7B5C3A", "symbol": "b", "passable": "no"},
    "BED":    {"color": "#6B4C9A", "symbol": "B", "passable": "no"},
    "STAIRS": {"color": "#B8860B", "symbol": "^", "passable": "yes"},
    "POOL":   {"color": "#1E90FF", "symbol": "P", "passable": "no"},
    "RUBBLE": {"color": "#808080", "symbol": "r", "passable": "slow"},
    "TRAP":   {"color": "#CC0000", "symbol": "!", "passable": "yes"},
    "WINDOW": {"color": "#87CEEB", "symbol": "w", "passable": "no"},
    "THRONE": {"color": "#DAA520", "symbol": "H", "passable": "no"},
    "FORGE":  {"color": "#FF4500", "symbol": "F", "passable": "no"},
}


def _parse_dsl(dsl: str) -> dict:
    """Parse map DSL → grid dict. Unknown terrain types without a prior STYLE are an error."""
    width  = 20
    height = 15
    scale  = "5ft"
    styles: dict[str, dict] = {k: dict(v) for k, v in _DEFAULT_STYLES.items()}
    cells:  dict[str, str]  = {}
    errors: list[str]       = []

    for lineno, raw in enumerate(dsl.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        tokens = line.split()
        cmd    = tokens[0].upper()

        if cmd == "MAP":
            try:
                w_str, h_str = tokens[1].lower().split("x")
                width, height = int(w_str), int(h_str)
            except Exception:
                errors.append(f"L{lineno}: invalid MAP — use 'MAP WxH'")

        elif cmd == "SCALE":
            scale = tokens[1] if len(tokens) > 1 else "5ft"

        elif cmd == "STYLE":
            if len(tokens) < 2:
                errors.append(f"L{lineno}: STYLE requires a type name")
                continue
            stype = tokens[1].upper()
            attrs: dict = {}
            for tok in tokens[2:]:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    attrs[k.lower()] = v
            merged = dict(styles.get(stype, {}))
            merged.update(attrs)
            styles[stype] = merged

        else:
            terrain = cmd
            if terrain not in styles:
                errors.append(f"L{lineno}: unknown terrain '{terrain}' — declare with STYLE first")
                continue
            try:
                x1, y1 = (int(v) for v in tokens[1].split(","))
                if len(tokens) >= 4 and tokens[2].upper() == "TO":
                    x2, y2 = (int(v) for v in tokens[3].split(","))
                else:
                    x2, y2 = x1, y1
                for x in range(min(x1, x2), max(x1, x2) + 1):
                    for y in range(min(y1, y2), max(y1, y2) + 1):
                        cells[f"{x},{y}"] = terrain
            except Exception:
                errors.append(f"L{lineno}: bad placement — use 'TYPE x,y' or 'TYPE x1,y1 TO x2,y2'")

    return {
        "width":  width,
        "height": height,
        "scale":  scale,
        "styles": styles,
        "cells":  cells,
        "errors": errors,
    }


def _portrait_url(key: str | None, name: str, side: str) -> str | None:
    """Return a dashboard-relative portrait URL, or None."""
    camp_dir = _c.campaign_dir()

    if side == "party" and key:
        idx_path = camp_dir / "images" / "index.json"
        if idx_path.exists():
            try:
                idx = json.loads(idx_path.read_text())
                matches = [e for e in idx if e.get("type") == "portrait" and e.get("slug") == key]
                if matches:
                    return f"/images/{matches[-1]['filename']}"
            except Exception:
                pass

    # Monster portrait: global/monsters/<slug>.<ext>
    slug = name.lower().replace(" ", "_").replace("'", "").replace("-", "_")
    for ext in ("png", "jpg", "webp"):
        if (BASE_DIR / "global" / "monsters" / f"{slug}.{ext}").exists():
            return f"/monsters/portraits/{slug}.{ext}"

    return None


def save_combat_state(cfg: dict, session: dict) -> None:
    """Write combat_state.json for the dashboard. Called by map tools and re-exported
    so that combat.py can clear it on end_combat without importing this whole module."""
    camp_dir = cfg["_dir"]
    idx      = min(session.get("current_idx", 0), max(len(session["combatants"]) - 1, 0))

    combatants_out = []
    for i, c in enumerate(session["combatants"]):
        combatants_out.append({
            "name":         c["name"],
            "side":         c["side"],
            "key":          c.get("_key"),
            "hp":           c["hp"],
            "hp_max":       c["hp_max"],
            "x":            c.get("x"),
            "y":            c.get("y"),
            "init":         c.get("init"),
            "conditions":   list(c["conditions"]),
            "current":      i == idx,
            "portrait_url": _portrait_url(c.get("_key"), c["name"], c["side"]),
        })

    out = {
        "active":     True,
        "round":      session["round"],
        "current":    session["combatants"][idx]["name"] if session["combatants"] else None,
        "grid":       session.get("grid"),
        "combatants": combatants_out,
    }
    (camp_dir / "combat_state.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2)
    )


def register(mcp):

    @mcp.tool()
    def create_map(dsl: str) -> dict:
        """Parse a map DSL and attach the grid to the active combat session.
        The map is immediately visible on the dashboard at /combat.

        DSL syntax:
          MAP WxH                          — required; map dimensions in cells
          SCALE Nft                        — optional label, default 5ft
          STYLE TYPE color=#HEX symbol=X passable=yes|no|slow|fall
          TYPE x,y                         — place one cell
          TYPE x1,y1 TO x2,y2             — fill rectangle between two corners
          # comment lines are ignored

        Built-in types (no STYLE needed): WALL DOOR ROCK PILLAR COLUMN TABLE
          WATER PIT TREE CHEST FIRE STATUE ALTAR BARREL BED STAIRS POOL
          RUBBLE TRAP WINDOW THRONE FORGE

        Example — a 20×12 room with stone walls, a door, and two pillars:
          MAP 20x12
          WALL 0,0 TO 19,0
          WALL 0,0 TO 0,11
          WALL 19,0 TO 19,11
          WALL 0,11 TO 19,11
          DOOR 9,0
          PILLAR 3,3
          PILLAR 16,3
        """
        session = get_session()
        if session is None:
            return {"error": "No active combat session. Call start_combat first."}

        grid             = _parse_dsl(dsl)
        session["grid"]  = grid
        cfg              = _c.load_campaign()
        save_combat_state(cfg, session)

        return {
            "size":   f"{grid['width']}×{grid['height']}",
            "scale":  grid["scale"],
            "cells":  len(grid["cells"]),
            "styles": list(grid["styles"].keys()),
            "errors": grid["errors"],
        }

    @mcp.tool()
    def place_combatant(name: str, x: int, y: int) -> dict:
        """Place or move a combatant to grid cell (x, y).
        No movement-rate validation — DM positions tokens freely.
        Call after create_map to set initial positions for each combatant."""
        session = get_session()
        if session is None:
            return {"error": "No active combat session. Call start_combat first."}

        target = None
        low    = name.lower()
        for c in session["combatants"]:
            if c["name"].lower() == low or c["name"].lower().startswith(low):
                target = c
                break
        if target is None:
            return {"error": f"Combatant '{name}' not found."}

        grid = session.get("grid")
        if grid and not (0 <= x < grid["width"] and 0 <= y < grid["height"]):
            return {
                "error": f"({x},{y}) is outside map bounds "
                         f"({grid['width']}×{grid['height']})."
            }

        target["x"] = x
        target["y"] = y

        cfg = _c.load_campaign()
        save_combat_state(cfg, session)

        return {"name": target["name"], "x": x, "y": y}

    @mcp.tool()
    def get_map_state() -> dict:
        """Return map dimensions, scale, and current grid positions of all combatants."""
        session = get_session()
        if session is None:
            return {"error": "No active combat session."}

        grid = session.get("grid")
        return {
            "has_map":   grid is not None,
            "grid_size": f"{grid['width']}×{grid['height']}" if grid else None,
            "scale":     grid["scale"] if grid else None,
            "positions": [
                {
                    "name": c["name"],
                    "side": c["side"],
                    "x":    c.get("x"),
                    "y":    c.get("y"),
                }
                for c in session["combatants"]
            ],
        }
