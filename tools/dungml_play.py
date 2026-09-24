"""dungml play-session bridge — fog-of-war for the dashboard's `/map` tab.

Two contracts, both served by `dmap-server` at `DUNGML_API_BASE`:

  `/maps/{external_id}/…`       Automatic. ttrpg2 owns the DSL: every
                                `create_map` push lands here, so fog works with
                                no setup at all. `external_id` is the campaign
                                slug, which keeps campaigns from colliding.

  `/campaigns/{external_id}/…`  Opt-in. Maps are authored in the dungml editor
                                and owned there. The campaign is linked to a
                                dungml project once (from the dungml side, by
                                the GM's own token); ttrpg2 then picks which of
                                that project's maps is the current locale.

The `/map` tab prefers a selected campaign locale and falls back to whatever
`create_map` last pushed. It draws that locale in or out of combat — the tab
is the party's current location, not a combat-only view. Fog state (which nodes the party has discovered,
where it stands) lives on dungml's PlaySession, keyed per campaign — ttrpg2
stores only the link: which contract, which map, which view scope.

Every call degrades gracefully. If `dmap-server` is unreachable the bridge
reports `connected: False` and the tab falls back to the plain local render
rather than erroring; the DM's game is never blocked on the map service.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

import _campaign as _c

DUNGML_API_BASE = os.environ.get("DUNGML_API_BASE", "http://127.0.0.1:8000").rstrip("/")
# Static-mode service principal (dungml's StaticIdentityProvider). In a
# Keycloak deployment this is the M2M client's access token instead.
DUNGML_SERVICE_TOKEN = os.environ.get("DUNGML_SERVICE_TOKEN", "dev-service")
_TIMEOUT_S = 10.0

# Scopes the render token can carry. "fog" draws only what the party has
# discovered; "gm" draws the whole map with the party marker on it.
SCOPES = ("fog", "gm")


class DungmlUnavailable(Exception):
    """dmap-server is unreachable, or refused the service credential.

    Callers treat this as "no session connected" — never as a hard failure.
    """


class DungmlNotFound(DungmlUnavailable):
    """dungml answered 404 for something the caller named — an unknown room
    or door. `detail` carries dungml's body (candidates for a room)."""

    def __init__(self, message: str, detail=None):
        super().__init__(message)
        self.detail = detail


class DungmlConflict(DungmlUnavailable):
    """409: the map's source does not parse (being edited in dungml)."""


def describe_error(e: DungmlUnavailable) -> dict:
    """A tool-result dict for a dungml failure the DM can act on."""
    if isinstance(e, DungmlConflict):
        return {"error": "the map is mid-edit in the dungml editor and does not "
                         "parse right now — narrate without it, try again later"}
    if isinstance(e, DungmlNotFound):
        d = e.detail if isinstance(e.detail, dict) else {}
        out = {"error": d.get("error") or str(e)}
        if d.get("candidates"):
            out["candidates"] = d["candidates"]
        return out
    return {"error": str(e)}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _request(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    auth: bool = True,
    raw: bool = False,
    strict: bool = False,
) -> dict | bytes:
    """One call against dmap-server. Returns parsed JSON, or raw bytes when
    `raw` (the render endpoints answer `image/svg+xml`, not JSON).

    Raises DungmlUnavailable for anything the caller can't act on: server
    down, bad credential, unexpected status. A 404 is returned as a normal
    `{}` for GETs, because "not linked yet" is an ordinary state here, not an
    error worth surfacing to the DM — unless `strict`, which a caller passes
    when a 404 means something it named (a room, a door) doesn't exist and
    must be surfaced rather than swallowed.
    """
    url = f"{DUNGML_API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {}
    if data is not None:
        headers["content-type"] = "application/json"
    if auth:
        headers["Authorization"] = f"Bearer {DUNGML_SERVICE_TOKEN}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404 and method == "GET" and not strict:
            return b"" if raw else {}
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:2000]
        except Exception:
            pass
        parsed = None
        try:
            parsed = json.loads(detail).get("detail")
        except Exception:
            pass
        msg = f"dungml {method} {path} → HTTP {e.code} {detail[:300]}".strip()
        if e.code == 404:
            raise DungmlNotFound(msg, parsed) from e
        if e.code == 409:
            raise DungmlConflict(msg) from e
        raise DungmlUnavailable(msg) from e
    except urllib.error.URLError as e:
        raise DungmlUnavailable(
            f"dungml backend unreachable at {DUNGML_API_BASE} ({e.reason}). "
            f"Start it with `uv run dmap-server` from the dungml workspace, "
            f"or set DUNGML_API_BASE."
        ) from e
    if raw:
        return payload
    if not payload:
        return {}
    try:
        return json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise DungmlUnavailable(f"dungml {method} {path} → unparseable response") from e


def health() -> bool:
    try:
        return bool(_request("GET", "/health", auth=False))
    except DungmlUnavailable:
        return False


# ---------------------------------------------------------------------------
# Link state — which contract / map / scope this campaign is pointed at
# ---------------------------------------------------------------------------

def external_id(cfg: dict | None = None) -> str:
    """The campaign's identity on the dungml side.

    The campaign slug (its directory name), not its display name — stable
    across renames of the human-facing title, and already unique per campaign.
    """
    return _c.get_active_name()


_DEFAULT_LINK = {
    # "combat"  → the DSL create_map pushed to /maps/{external_id}
    # "campaign" → an authored map under /campaigns/{external_id}/maps/{id}
    "source": "combat",
    "map_id": None,
    "map_name": None,
    "scope": "fog",
    "party_node": None,
    "width": None,
    "height": None,
    "scale": None,
}


def link_state(cfg: dict) -> dict:
    """The campaign's dungml link, from state.json. Always a full dict."""
    state = _c.load_state(cfg)
    link = dict(_DEFAULT_LINK)
    stored = state.get("dungml")
    if isinstance(stored, dict):
        link.update({k: v for k, v in stored.items() if k in _DEFAULT_LINK})
    if link["scope"] not in SCOPES:
        link["scope"] = "fog"
    return link


def save_link_state(cfg: dict, **changes) -> dict:
    """Merge `changes` into the stored link and persist. Returns the new link."""
    state = _c.load_state(cfg)
    link = link_state(cfg)
    link.update({k: v for k, v in changes.items() if k in _DEFAULT_LINK})
    if link["scope"] not in SCOPES:
        link["scope"] = "fog"
    state["dungml"] = link
    _c.save_state(cfg, state)
    return link


# ---------------------------------------------------------------------------
# /maps/{external_id} contract — ttrpg2 owns the DSL
# ---------------------------------------------------------------------------

def push_source(ext: str, dsl: str) -> None:
    """Replace the contract map's source with `dsl`.

    Replace, not append: ttrpg2 rewrites its combatant marker block on every
    token move, so appending would stack duplicate geometry. Discovery lives
    on the PlaySession and survives this.
    """
    _request("PUT", f"/maps/{urllib.parse.quote(ext)}/source", {"dungml": dsl})


def map_info(ext: str) -> dict:
    """{map_id, session_id} for the contract map — idempotent get-or-create."""
    return _request("GET", f"/maps/{urllib.parse.quote(ext)}/info") or {}


def reveal(ext: str, node: str) -> dict:
    return _request("POST", f"/maps/{urllib.parse.quote(ext)}/reveal", {"feature_id": node})


def set_party(ext: str, node: str) -> dict:
    return _request("POST", f"/maps/{urllib.parse.quote(ext)}/party", {"room_id": node})


def mint_token(ext: str, scope: str = "fog") -> str:
    r = _request("POST", f"/maps/{urllib.parse.quote(ext)}/tokens", {"scope": scope})
    return r.get("token", "")


def render(ext: str, token: str) -> bytes:
    q = urllib.parse.urlencode({"token": token})
    return _request("GET", f"/maps/{urllib.parse.quote(ext)}/render?{q}", auth=False, raw=True)


# ---------------------------------------------------------------------------
# /campaigns/{external_id} contract — maps authored in the dungml editor
# ---------------------------------------------------------------------------

def campaign_maps(ext: str) -> list[dict]:
    """Renderable maps in the linked dungml project, newest first.

    Empty when the campaign isn't linked — linking is a one-time action the
    GM performs from the dungml side with their own token, so "not linked" is
    an ordinary state, not an error.
    """
    r = _request("GET", f"/campaigns/{urllib.parse.quote(ext)}/maps")
    return r if isinstance(r, list) else []


def campaign_mint_token(ext: str, map_id: str, scope: str = "fog") -> str:
    r = _request(
        "POST",
        f"/campaigns/{urllib.parse.quote(ext)}/maps/{urllib.parse.quote(map_id)}/tokens",
        {"scope": scope},
    )
    return r.get("token", "")


def campaign_render(ext: str, map_id: str, token: str) -> bytes:
    q = urllib.parse.urlencode({"token": token})
    return _request(
        "GET",
        f"/campaigns/{urllib.parse.quote(ext)}/maps/{urllib.parse.quote(map_id)}/render?{q}",
        auth=False,
        raw=True,
    )


def set_active_map(ext: str, map_id: str) -> None:
    """Tell dungml which of the linked project's maps the party is on.

    dungml can't work this out for itself — the choice of locale lives in
    ttrpg2's own state — and without it the web GUI has no way to point the
    GM at the session this campaign is actually driving. Advisory only:
    nothing here depends on it landing.
    """
    _request(
        "PUT",
        f"/campaigns/{urllib.parse.quote(ext)}/active",
        {"map_id": map_id},
    )


def _cm(ext: str, map_id: str) -> str:
    return f"/campaigns/{urllib.parse.quote(ext)}/maps/{urllib.parse.quote(map_id)}"


def campaign_reveal(ext: str, map_id: str, node: str) -> dict:
    return _request("POST", f"{_cm(ext, map_id)}/reveal", {"feature_id": node})


def campaign_set_party(ext: str, map_id: str, node: str) -> dict:
    return _request("POST", f"{_cm(ext, map_id)}/party", {"room_id": node})


def campaign_room(ext: str, map_id: str, query: str, scope: str = "gm") -> dict:
    """Look up a room by id or free-text query on an authored locale.

    A 404 here means the caller named something that doesn't exist — raised
    as `DungmlNotFound` (with candidates, when dungml offers them) rather than
    swallowed like an ordinary GET 404.
    """
    q = urllib.parse.quote(query, safe="")
    return _request("GET", f"{_cm(ext, map_id)}/rooms/{q}?scope={scope}", strict=True)


def campaign_known(ext: str, map_id: str) -> dict:
    """What the party has discovered so far on this authored locale."""
    return _request("GET", f"{_cm(ext, map_id)}/known", strict=True)


def campaign_door(ext: str, map_id: str, *, door: str | None = None,
                  between: list[str] | None = None, discovered: bool = True,
                  state: str | None = None) -> dict:
    """Mark a door discovered and/or change its state.

    Identify the door either by `door` (its own id) or `between` (the two
    rooms it connects) — dungml resolves whichever is given.
    """
    body: dict = {"discovered": discovered}
    if door:
        body["door"] = door
    if between:
        body["between"] = between
    if state:
        body["state"] = state
    return _request("POST", f"{_cm(ext, map_id)}/doors", body)


def active_map_id(cfg: dict) -> str | None:
    """The dungml map id the party's current locale is on, on either
    contract — the authored map id for a campaign locale, else the pushed
    combat map's id."""
    link = link_state(cfg)
    if link["source"] == "campaign" and link["map_id"]:
        return link["map_id"]
    return map_info(external_id(cfg)).get("map_id")


def map_rooms(map_id: str) -> list[dict]:
    """The map's room/corridor graph: [{id, name, kind, exits}].

    Authored truth, session-independent — the node ids here are what /reveal
    and /party take.
    """
    r = _request("GET", f"/maps/{urllib.parse.quote(map_id)}/rooms")
    return r.get("rooms", []) if isinstance(r, dict) else []


# ---------------------------------------------------------------------------
# Resolution — what the `/map` tab should actually draw
# ---------------------------------------------------------------------------

_VIEWBOX_RX = re.compile(r'viewBox="0\s+0\s+([\d.]+)\s+([\d.]+)"')
_PARTY_NODE_RX = re.compile(r'data-party-node="([^"]*)"')


def render_current(cfg: dict, scope: str | None = None) -> tuple[bytes, dict]:
    """Fetch the SVG the `/map` tab should show, plus its metadata.

    Returns `(svg_bytes, meta)` where meta carries `width`/`height` in grid
    cells (from the SVG's viewBox, which dungml sets from `grid { bounds }`)
    and the node the party marker was drawn at. Raises DungmlUnavailable.
    """
    ext = external_id(cfg)
    link = link_state(cfg)
    use_scope = scope or link["scope"]
    if link["source"] == "campaign" and link["map_id"]:
        token = campaign_mint_token(ext, link["map_id"], use_scope)
        svg = campaign_render(ext, link["map_id"], token)
    else:
        token = mint_token(ext, use_scope)
        svg = render(ext, token)
    text = svg.decode("utf-8", "replace") if isinstance(svg, bytes) else str(svg)
    meta: dict = {"width": None, "height": None, "party_node": None}
    m = _VIEWBOX_RX.search(text)
    if m:
        meta["width"] = int(float(m.group(1)))
        meta["height"] = int(float(m.group(2)))
    pm = _PARTY_NODE_RX.search(text)
    if pm:
        meta["party_node"] = pm.group(1)
    meta["placeholder"] = "map not ready" in text
    return svg, meta


def use_locale(cfg: dict, name_or_id: str) -> dict:
    """Point the campaign's dungml link at an authored map as the current
    locale. Body of the `dungml_use_locale` tool, callable directly (e.g. by
    `move_party`'s `map=` argument) without going through the registered
    tool function.

    `name_or_id` is a map id or a case-insensitive substring of its name,
    from `dungml_locales`/`campaign_maps`. Pass "combat" to go back to
    whatever `create_map` last pushed.
    """
    ext = external_id(cfg)
    if name_or_id.strip().lower() == "combat":
        save_link_state(cfg, source="combat", map_id=None, map_name=None)
        probe(cfg)
        return {"source": "combat", "note": "showing the pushed combat map"}
    try:
        maps = campaign_maps(ext)
    except DungmlUnavailable as e:
        return {"error": str(e)}
    if not maps:
        return {"error": f"campaign '{ext}' has no authored maps linked in dungml."}
    low = name_or_id.strip().lower()
    hit = next((m for m in maps if str(m.get("id")) == name_or_id), None)
    if hit is None:
        hits = [m for m in maps if low in str(m.get("name", "")).lower()]
        if not hits:
            return {
                "error": f"no locale matching '{name_or_id}'",
                "available": [m.get("name") for m in maps],
            }
        if len(hits) > 1:
            return {
                "error": f"'{name_or_id}' matches {len(hits)} locales — be more specific",
                "matches": [m.get("name") for m in hits],
            }
        hit = hits[0]
    save_link_state(cfg, source="campaign", map_id=hit["id"], map_name=hit.get("name"))
    link = probe(cfg)
    return {
        "source": "campaign",
        "map_id": hit["id"],
        "map_name": hit.get("name"),
        "size": f"{link.get('width')}×{link.get('height')}" if link.get("width") else None,
        "scope": link.get("scope"),
    }


def probe(cfg: dict) -> dict:
    """Refresh the cached geometry on the stored link by rendering once.

    The `/map` tab needs `width`/`height` before it can lay out tokens, and it
    learns them from `/combat-state` — which is served before the image
    loads. Geometry doesn't change as fog burns off, so caching it on the link
    is enough; this is called whenever the map itself changes.
    """
    try:
        _svg, meta = render_current(cfg)
    except DungmlUnavailable:
        return link_state(cfg)
    if meta.get("placeholder"):
        return link_state(cfg)
    link = link_state(cfg)
    if link["source"] == "campaign" and link["map_id"]:
        # Re-reported on every probe rather than only on a locale switch, so
        # a campaign that chose its locale before dungml tracked this still
        # shows up in the GUI. Swallowed like every other call here.
        try:
            set_active_map(external_id(cfg), link["map_id"])
        except DungmlUnavailable:
            pass
    return save_link_state(
        cfg,
        width=meta.get("width"),
        height=meta.get("height"),
        party_node=meta.get("party_node"),
    )


def connection_block(cfg: dict) -> dict:
    """The `dungml` block for combat_state.json / `/combat-state`.

    This is what tells the `/map` tab whether a session is connected and
    therefore whether to draw the fogged render instead of the plain local
    one. Deliberately cheap — no render, no token mint — because the tab polls
    it every couple of seconds; it reports the *stored* link plus a liveness
    check, and the SVG itself comes from the `/combat-fog.svg` proxy.
    """
    link = link_state(cfg)
    alive = health()
    block = {
        "connected": False,
        "reachable": alive,
        "source": link["source"],
        "scope": link["scope"],
        "map_id": link["map_id"],
        "map_name": link["map_name"],
        "party_node": link["party_node"],
        "width": link["width"],
        "height": link["height"],
        "api_base": DUNGML_API_BASE,
        "render_url": "/combat-fog.svg",
        "detail": None,
    }
    if not alive:
        block["detail"] = f"dmap-server unreachable at {DUNGML_API_BASE}"
        return block
    try:
        if link["source"] == "campaign" and link["map_id"]:
            block["connected"] = True
        else:
            info = map_info(external_id(cfg))
            # A session row exists as soon as the contract map is provisioned,
            # but there's nothing to draw until some DSL has been pushed —
            # width is only known after a successful probe.
            block["connected"] = bool(info.get("session_id")) and bool(link["width"])
            block["session_id"] = info.get("session_id")
            block["map_id"] = info.get("map_id")
            if not block["connected"]:
                block["detail"] = "no map pushed to dungml yet"
    except DungmlUnavailable as e:
        block["reachable"] = False
        block["detail"] = str(e)
    return block


def sync_combat_map(cfg: dict, dsl: str) -> dict:
    """Push the current combat DSL to dungml and refresh the cached geometry.

    Called by combat_map whenever the map or a token changes. Returns the
    stored link. Swallows DungmlUnavailable: a token still moves and the local
    render still updates when the map service is down.
    """
    if not (dsl or "").strip():
        return link_state(cfg)
    try:
        push_source(external_id(cfg), dsl)
    except DungmlUnavailable:
        return link_state(cfg)
    link = link_state(cfg)
    # Pushing a combat map makes it the thing to show, unless the DM has
    # deliberately pointed the tab at an authored locale.
    if link["source"] != "campaign":
        save_link_state(cfg, source="combat", map_id=None, map_name=None)
    return probe(cfg)


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

def register(mcp):

    @mcp.tool()
    def dungml_status() -> dict:
        """Report whether a dungml play session is connected to this campaign.

        Shows which map the `/map` tab is drawing (the pushed combat map, or a
        selected authored locale), the current view scope (`fog` = players'
        view, `gm` = whole map), the party's node, and how many nodes have
        been discovered. Use it when the `/map` tab looks wrong or empty.
        """
        cfg = _c.load_campaign()
        block = connection_block(cfg)
        ext = external_id(cfg)
        out = {
            "external_id": ext,
            "api_base": DUNGML_API_BASE,
            **{k: block[k] for k in
               ("connected", "reachable", "source", "scope", "map_id",
                "map_name", "party_node", "width", "height", "detail")},
        }
        if block["reachable"]:
            try:
                out["authored_locales"] = len(campaign_maps(ext))
            except DungmlUnavailable:
                out["authored_locales"] = 0
        return out

    @mcp.tool()
    def dungml_locales() -> dict:
        """List the authored maps available as the party's current locale.

        These come from the dungml project linked to this campaign — maps the
        GM drew in the dungml editor. Empty if the campaign isn't linked, in
        which case the `/map` tab uses whatever `create_map` last pushed.
        Select one with `dungml_use_locale`.
        """
        cfg = _c.load_campaign()
        ext = external_id(cfg)
        try:
            maps = campaign_maps(ext)
        except DungmlUnavailable as e:
            return {"error": str(e)}
        if not maps:
            return {
                "locales": [],
                "note": f"campaign '{ext}' is not linked to a dungml project, "
                        f"or the project has no renderable maps. Link it from "
                        f"the dungml UI, then re-run.",
            }
        link = link_state(cfg)
        return {
            "locales": [
                {
                    "map_id": m.get("id"),
                    "name": m.get("name"),
                    "is_default": m.get("is_default", False),
                    "current": m.get("id") == link["map_id"],
                }
                for m in maps
            ]
        }

    @mcp.tool()
    def dungml_use_locale(name_or_id: str) -> dict:
        """Point the `/map` tab at an authored map as the current locale.

        `name_or_id` is a map id or a case-insensitive substring of its name,
        from `dungml_locales`. The map's own fog is per-campaign, so each
        campaign keeps its own discovery on a shared authored map.

        Pass "combat" to go back to whatever `create_map` last pushed.
        """
        return use_locale(_c.load_campaign(), name_or_id)

    @mcp.tool()
    def dungml_rooms() -> dict:
        """List the node ids of the map the `/map` tab is showing.

        These ids (`room.antechamber`, `corridor.spine`, …) are what
        `move_party` and `dungml_reveal` take; labels work too. `exits`
        gives the nodes each one connects to, so you can follow the party's
        route without re-reading the DSL.
        """
        cfg = _c.load_campaign()
        link = link_state(cfg)
        try:
            map_id = link["map_id"] or map_info(external_id(cfg)).get("map_id")
            if not map_id:
                return {"error": "no dungml map for this campaign yet"}
            rooms = map_rooms(map_id)
            party_node = (campaign_known(external_id(cfg), map_id).get("party_location")
                          if link["source"] == "campaign" and link["map_id"]
                          else link["party_node"])
        except DungmlUnavailable as e:
            return {"error": str(e)}
        return {
            "map_id": map_id,
            "party_node": party_node,
            "rooms": rooms,
        }

    @mcp.tool()
    def dungml_reveal(room: str) -> dict:
        """Reveal a node without moving the party there.

        For what the party can see into but hasn't entered — the room beyond
        an opened door, a chamber lit by a spell, the corridor a thief scouted
        ahead. Unknown rooms return an error with candidates. The returned
        room's `dm_only` is never narrated.
        """
        cfg = _c.load_campaign()
        ext = external_id(cfg)
        link = link_state(cfg)
        try:
            if link["source"] == "campaign" and link["map_id"]:
                res = campaign_reveal(ext, link["map_id"], room)
            else:
                res = reveal(ext, room)
        except DungmlUnavailable as e:
            return describe_error(e)
        return {"revealed": res.get("revealed", room), "room": res.get("room")}

    @mcp.tool()
    def dungml_view(scope: str = "fog") -> dict:
        """Set what the `/map` tab draws: `fog` or `gm`.

        `fog` is the players' view — only discovered geometry, with unexplored
        corridors fading out at the boundary. `gm` is the whole map with the
        party marker on it.

        This is a display setting for the dashboard, which the DM may be
        sharing on a second screen. It does not change what the party has
        discovered, and switching to `gm` does not reveal anything.
        """
        cfg = _c.load_campaign()
        want = (scope or "").strip().lower()
        if want not in SCOPES:
            return {"error": f"scope must be one of {', '.join(SCOPES)}"}
        link = save_link_state(cfg, scope=want)
        return {
            "scope": link["scope"],
            "showing": "players' view — discovered geometry only"
                       if want == "fog" else "GM view — the whole map",
        }
