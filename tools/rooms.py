"""The party moves through one tool, and the room it enters comes back.

`move_party` updates both halves of state.json["position"] — the ttrpg2
location and the dungml room — moves the dungml party marker (which reveals
the room), writes one `location_visited` event and returns the room's
context from the authored map, split into `perceived` (what the characters
see) and `dm_only` (never narrated), plus this campaign's notes on it.

Room notes are what the party changed: "horse tied here", "cleared", "east
door barricaded". They live in room_notes.json keyed by map and node, so a
reset or a new session still knows.
"""
from __future__ import annotations

import json
import re

import _campaign as _c
from tools import dungml_play as dp
from tools import position as _pos

_BRIEF_CHARS = 900
_DOOR_KEY_RX = re.compile(r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?(#\d+)?$")


# ----- room notes -----

def room_key(map_id: str, node: str) -> str:
    return f"{map_id}:{node}"


def _notes_path(cfg: dict):
    return cfg["_dir"] / "room_notes.json"


def _load_notes(cfg: dict) -> dict:
    try:
        return json.loads(_notes_path(cfg).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def notes_for(cfg: dict, map_id: str, node: str) -> list[dict]:
    return list(_load_notes(cfg).get(room_key(map_id, node), []))


def add_note(cfg: dict, map_id: str, node: str, text: str) -> dict:
    state = _c.load_state(cfg)
    note = {"day": state.get("current_day", 1),
            "time": f"{state.get('current_hour', 0):02d}:{state.get('current_minute', 0):02d}",
            "text": text.strip()}
    data = _load_notes(cfg)
    data.setdefault(room_key(map_id, node), []).append(note)
    _c.atomic_write_text(_notes_path(cfg), json.dumps(data, indent=2, ensure_ascii=False))
    return note


# ----- helpers -----

def _brief(path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    return text if len(text) <= _BRIEF_CHARS else text[:_BRIEF_CHARS].rstrip() + "\n…"


def _campaign_map(cfg: dict) -> tuple[str | None, str | None]:
    link = dp.link_state(cfg)
    if link["source"] == "campaign" and link["map_id"]:
        return link["map_id"], link.get("map_name")
    return None, None


def _restore_link(cfg: dict, prev_link: dict | None) -> None:
    """Undo a locale switch that `move_party` made on this call, when the
    room move it was for didn't pan out. Nothing else about the link
    (party_node, scope, cached geometry) is worth restoring — only the
    choice of locale, which is what a failed `to` must not leave changed."""
    if prev_link is not None:
        dp.save_link_state(cfg, source=prev_link["source"], map_id=prev_link["map_id"],
                            map_name=prev_link["map_name"])


def register(mcp):

    @mcp.tool()
    def move_party(to: str = "", map: str = "", location: str = "") -> dict:
        """Move the party. The ONLY way the party changes place.

        to:       a room on the current map — its id (room.cave_1), bare name
                  (cave_1) or label ("39", "Grand Foyer"). Moves the map
                  marker and reveals the room.
        map:      switch to another authored map first (name substring, as
                  dungml_locales lists them) — stairs, a map exit. Requires
                  `to` — the room arrived at on that map — in the same call;
                  to only change what the `/map` tab is showing, without
                  moving anyone, use `dungml_use_locale` instead.
        location: a ttrpg2 location slug (a town, the road, a dungeon as a
                  whole) — for moves with no map, or to change both at once.
                  Leaving a room by `location` alone (no `to`) clears the
                  room half of position — the party is no longer anywhere
                  on that map.

        Returns the room just entered: `perceived` is what the characters
        see and may be narrated; `dm_only` never is. Also returns
        `room_notes` (what the party changed there earlier) and, when the
        location changed, the opening of its file. An unknown room returns
        `candidates` and moves nothing — a failed `map`+`to` also undoes the
        locale switch it made. If the map service is down the location half
        still moves and `warning` says so.
        """
        if map and not to:
            return {"error": "pass `to` — the room you arrive in — with `map`; "
                             "use dungml_use_locale to only change the displayed map"}

        cfg = _c.load_campaign()
        ext = dp.external_id(cfg)
        changes: dict = {}
        out: dict = {}

        loc = None
        if location:
            loc = _pos.find_location(cfg, location)
            if loc is None:
                return {"error": f"no location file '{location}' — create_location first, "
                                 f"or pass `to` for a room on the map"}

        prev_link = None
        if map:
            prev_link = dp.link_state(cfg)
            switched = dp.use_locale(cfg, map)
            if "error" in switched:
                return switched

        if to:
            campaign_map_id, campaign_map_name = _campaign_map(cfg)
            is_campaign = bool(campaign_map_id)
            try:
                if is_campaign:
                    map_id, map_name = campaign_map_id, campaign_map_name
                    res = dp.campaign_set_party(ext, map_id, to)
                else:
                    # Resolved before the move, not after: a failure here is
                    # "the marker never moved", not "it moved but we lost
                    # track of which map" — the two must not be confused.
                    map_id = dp.map_info(ext).get("map_id")
                    map_name = "combat map"
                    res = dp.set_party(ext, to)
            except (dp.DungmlNotFound, dp.DungmlConflict) as e:
                _restore_link(cfg, prev_link)
                return dp.describe_error(e)
            except dp.DungmlUnavailable as e:
                if loc is None:
                    _restore_link(cfg, prev_link)
                    return {"error": f"map service unreachable — party marker not moved ({e})"}
                out["warning"] = f"map service unreachable — party marker not moved ({e})"
            else:
                room = res.get("room") or {}
                node = res.get("party_location") or room.get("id")
                if not node:
                    # dungml accepted the call but didn't place anyone —
                    # the old silent no-op. Nothing moves on a non-move.
                    _restore_link(cfg, prev_link)
                    return {"error": f"dungml did not place the party at '{to}' — "
                                     f"check dungml_rooms for the exact room"}
                changes.update(map_id=map_id, map_name=map_name, node=node,
                               label=room.get("label") or node)
                out["room"] = room
                out["room_notes"] = notes_for(cfg, map_id, node)
                dp.save_link_state(cfg, party_node=node)

        if loc is not None:
            changes.update(location=loc["slug"], area=loc["area"], name=loc["name"])
            out["location_brief"] = _brief(loc["path"])
            if not to:
                # Leaving by location alone abandons the room, not just the
                # display of it — position must not go on claiming a node
                # on a map the party has left.
                changes.update(map_id=None, map_name=None, node=None, label=None)

        if not changes:
            if "warning" in out:
                return {"error": out["warning"]}
            return {"error": "nothing to move to — pass `to`, `map` or `location`"}

        pos = _pos.set_position(cfg, **changes)
        _c.append_event(cfg, {
            "type": "location_visited",
            "slug": pos.get("location", ""), "area": pos.get("area", ""),
            "name": _pos.position_label(pos),
            "map_id": pos.get("map_id"), "node": pos.get("node"), "label": pos.get("label"),
        })
        out["position"] = pos
        return out

    @mcp.tool()
    def room(query: str = "") -> dict:
        """The authored room: the current one, or any room on the current map
        by id, bare name or label. `perceived` may be narrated; `dm_only`
        never. Read-only — for "what's through that door", or re-reading
        the room after a reset. Includes the campaign's notes on it."""
        cfg = _c.load_campaign()
        map_id, _ = _campaign_map(cfg)
        if not map_id:
            return {"error": "room context needs an authored locale (dungml_use_locale)"}
        pos = _pos.current_position(cfg)
        q = query or pos.get("node")
        if not q:
            return {"error": "the party isn't in a room yet — move_party first"}
        try:
            ctx = dp.campaign_room(dp.external_id(cfg), map_id, q)
        except dp.DungmlUnavailable as e:
            return dp.describe_error(e)
        return {"room": ctx, "room_notes": notes_for(cfg, map_id, ctx.get("id", q))}

    @mcp.tool()
    def door(door: str, state: str = "", found: bool = False) -> dict:
        """Record what happened to a door: opened, forced, locked, barred —
        or a secret door found (found=True).

        door: a door key ("14,9", from a room's exits) or the room it leads
              to from the current room ("37", "room.cave_2")."""
        cfg = _c.load_campaign()
        map_id, _ = _campaign_map(cfg)
        if not map_id:
            return {"error": "doors are tracked on an authored locale (dungml_use_locale)"}
        is_key = bool(_DOOR_KEY_RX.match(door))
        between = None
        if not is_key:
            here = _pos.current_position(cfg).get("node")
            if not here:
                return {"error": "the party isn't in a room yet — move_party first"}
            between = [here, door]
        try:
            return dp.campaign_door(dp.external_id(cfg), map_id,
                                    door=door if is_key else None, between=between,
                                    discovered=True, state=state or None)
        except dp.DungmlUnavailable as e:
            return dp.describe_error(e)

    @mcp.tool()
    def note_room(text: str, query: str = "") -> dict:
        """Remember what the party changed in a room — cleared it, left the
        horse, barricaded a door, looted the chest. Returned by move_party
        and room() every time the party is there again. Current room by
        default; `query` names another by id or label."""
        cfg = _c.load_campaign()
        pos = _pos.current_position(cfg)
        map_id = pos.get("map_id")
        node = pos.get("node")
        if query:
            mid, _ = _campaign_map(cfg)
            if not mid:
                return {"error": "naming a room needs an authored locale"}
            try:
                node = dp.campaign_room(dp.external_id(cfg), mid, query, scope="fog")["id"]
            except dp.DungmlUnavailable as e:
                return dp.describe_error(e)
            map_id = mid
        else:
            # No query: notes attach to wherever `move_party` last put the
            # party, which is only trustworthy while the campaign's current
            # locale still is the map that node lives on. If the locale has
            # moved on (or there is none), the room is stale, not current.
            cur_map_id, _ = _campaign_map(cfg)
            if not cur_map_id or cur_map_id != map_id:
                return {"error": "the party isn't in a room on the current locale — "
                                 "move_party first, or name the room with `query`"}
        if not (map_id and node):
            return {"error": "the party isn't in a room yet — move_party first"}
        return {"room": node, "note": add_note(cfg, map_id, node, text)}
