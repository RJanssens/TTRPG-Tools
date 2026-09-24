"""Where the party is.

One record, state.json["position"], answers it for every surface — the
primer, the /play sidebar, introduce_npc's auto-tag, move_party. It has two
halves, either of which may be empty: a ttrpg2 location (a town, a stretch of
road, a dungeon as a whole) and a node on a dungml map (a room).

Campaigns from before this record existed fall back once to the last
`location_visited` event; the first move writes the record.
"""
from __future__ import annotations

import _campaign as _c

_KEYS = ("location", "area", "name", "map_id", "map_name", "node", "label")


def _title(path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                return line.lstrip("#").strip()
    except OSError:
        pass
    return path.stem


def find_location(cfg: dict, slug: str) -> dict | None:
    """A location file by 'area/slug' or bare slug. None if there is none."""
    locs = cfg["_data_dir"] / "locations"
    slug = (slug or "").strip().strip("/")
    if not slug:
        return None
    direct = locs / f"{slug}.md"
    hits = [direct] if direct.exists() else sorted(locs.rglob(f"{slug.split('/')[-1]}.md"))
    if not hits:
        return None
    p = hits[0]
    area = "" if p.parent == locs else p.parent.relative_to(locs).as_posix()
    return {"slug": p.stem, "area": area, "name": _title(p), "path": p}


def current_position(cfg: dict, state: dict | None = None,
                     events: list | None = None) -> dict:
    state = state if state is not None else _c.load_state(cfg)
    pos = state.get("position")
    if isinstance(pos, dict) and pos:
        return dict(pos)
    for e in reversed(events if events is not None else _c.load_events(cfg)):
        if e.get("type") == "location_visited":
            return {"location": e.get("slug", ""), "area": e.get("area", "") or "",
                    "name": e.get("name") or e.get("slug", "")}
    return {}


def set_position(cfg: dict, **changes) -> dict:
    state = _c.load_state(cfg)
    pos = current_position(cfg, state)
    pos.update({k: v for k, v in changes.items() if k in _KEYS})
    h, m = state.get("current_hour", 0), state.get("current_minute", 0)
    pos["since"] = {"day": state.get("current_day", 1), "time": f"{h:02d}:{m:02d}"}
    state["position"] = pos
    _c.save_state(cfg, state)
    return pos


def position_label(pos: dict) -> str:
    if pos.get("label"):
        return f"{pos['label']} · {pos['map_name']}" if pos.get("map_name") else pos["label"]
    return pos.get("name") or pos.get("location") or ""
