# Rooms: one party position, and room context from dungml

*Sub-project C+D of the continuity overhaul. Spans ttrpg2 and dungml.*

## Problem

The DM misreads rooms because it never sees them. `dungml_rooms` returns node
ids and an exit list; the authored map's labels, boxed text, DM notes, door
types and states never cross the contract, even though the Stonehell maps
carry most of the module key in exactly those fields (Level 1B: 60
descriptions, 49 DM-note blocks). So the DM narrates from memory or invents:

- it described a "processing room with three cells" where the map has the
  Central Passageway, and only found out by curling the dungml API by hand;
- it reads `room.cave_1` as module room 1 when its label is 39 (5 of 12 maps);
- it sees secret doors as ordinary exits (a fourth-wall leak) and gets no
  boundary openings, so exits are both over- and under-reported.

Separately, the party has two unsynchronised positions. ttrpg2's "current
location" is the last `location_visited` event, which only `create_area` /
`create_location` write, so it means "last file created". dungml's party node
only moves when the DM remembers `dungml_party_at` — 26 calls against ~55
movement turns across the transcripts; one campaign went 47 turns without it.
The active campaign currently has live party markers on two maps at once.
Failures are silent: unknown nodes return `{"ok": true}`, `dungml_reveal`
never verifies, and a failed probe returns the stale cached node.

## Goals

1. Every party move goes through **one tool** that updates both systems,
   logs the move, and hands the DM the room it just entered.
2. The DM reads a room's **authored text** — label, boxed text, DM notes,
   features, exits with door state — in one call, partitioned into what the
   characters can perceive and what is DM-only.
3. Node ids stop mattering: the DM may name a room by id, bare name, or
   label ("39").
4. Door state and found secret doors are recorded on the session.
5. Failures are loud: unknown rooms and unreachable maps come back as errors
   or `warning`s the DM can see, never as a quiet success.
6. The party is on one map at a time.
7. What the party *changes* in a room (cleared it, tied the horse here,
   smashed the door) is remembered per room.

## Non-goals

- Editing authored maps from ttrpg2 in any way (unchanged policy).
- Auto-detecting movement from prose (that is the scribe, sub-project B;
  it will call the same `move_party` / `note_room` entry points).
- The per-turn state card (sub-project A) — this spec only provides the
  position block it will include.
- Fixing the Stonehell map data (a GM task; see *Data cleanup*).

## Design

### dungml — one pure implementation of "what is this room"

Exit logic exists three times today (`mcp/server.py:get_exits`,
`routes/sessions.py:_exits`, `routes/campaigns.py:map_rooms`), each slightly
different. Move it into the DSL package as pure functions over
`(DungeonMap, Graph, SessionView)`, and make all three callers use them.

`dungml/play.py` (new functions):

```python
@dataclass(frozen=True)
class SessionView:          # the runtime overlay, detached from the ORM
    discovered_nodes: frozenset[str]
    discovered_doors: frozenset[str]
    door_states: Mapping[str, str]
    party_location: str | None

def resolve_node(dmap, graph, query: str) -> str | None
def room_context(dmap, graph, node: str, view: SessionView) -> dict
def known_map(dmap, graph, view: SessionView) -> dict
```

**`resolve_node`** accepts, in order: an exact node id (`room.cave_1`); a bare
name (`cave_1`); a label text (`"39"`, `"Grand Foyer"`), case-insensitive; a
corridor `display_name`. Ambiguity (two rooms labelled "Cpt") returns None;
the route turns that into a 404 listing the candidates.

**`room_context`** returns the room partitioned by who may know it:

```json
{
  "id": "room.cave_1", "kind": "room", "label": "39",
  "discovered": true, "party_here": true,
  "perceived": {
    "description": "…boxed text…",
    "features": [{"name": "altar", "description": "…"}],
    "annotations": [{"text": "F", "description": "…"}],
    "exits": [{"door": "14,9", "to": "room.cave_2", "to_label": "37",
               "type": "wooden", "state": "closed", "blocked": false,
               "far_side_explored": false, "description": "…"}],
    "map_exits": [{"target_map": "Level 1B: The Quiet Halls", "label": "stairs down"}]
  },
  "dm_only": {
    "notes": "…dm_notes…",
    "secret_exits": [{"door": "20,4", "to": "room.cave_9", "to_label": "44",
                      "type": "secret", "found": false}],
    "trapped_doors": ["14,9"],
    "secret_features": [{"name": "trap", "dm_notes": "…"}],
    "undiscovered_exits": []
  }
}
```

Rules:
- `perceived.exits` = non-hidden doors plus hidden doors the session has
  found, each with the **effective** state (`door_states` override, else
  authored). Boundary openings appear with `"to": null`.
- `dm_only.secret_exits` = hidden doors not yet found. `trapped` never
  appears in `perceived`.
- Features flagged `secret` (per instance or per `feature_def`) go to
  `dm_only.secret_features`; their descriptions never reach `perceived`.
- `annotations` are the node's `texts` — this is where features-key letters
  live; a letter with no description is still listed so the DM knows a key
  exists.
- Cross-map `exits` flagged `secret` go to `dm_only`.

Callers pick what they expose: the campaign route returns both halves to the
service principal (the DM *is* the GM); a future player screen gets
`perceived` only (`?scope=fog`).

**`known_map`** is today's MCP `get_known_map` with labels added and effective
door states: party location, discovered nodes `{id, label}`, connections
between them, and the frontier (found doors leading somewhere unexplored).

### dungml — contract changes (`routes/campaigns.py`)

| Route | Change |
|---|---|
| `GET /campaigns/{ext}/maps/{map}/rooms/{query}` | **new.** `resolve_node` then `room_context`. `?scope=fog` drops `dm_only`. 404 `{detail, candidates}` on unknown/ambiguous. |
| `GET /campaigns/{ext}/maps/{map}/known` | **new.** `known_map`. |
| `POST …/party` `{room_id}` | resolve via `resolve_node`; **404 with candidates** on unknown; **409** if the source doesn't parse; on success returns `{ok, party_location, room}` where `room` is the new room's context. Clears `party_location` on this campaign's sessions for *other* maps in the project (one map at a time) and records this map as the active map. |
| `POST …/reveal` `{feature_id}` | same resolution and error semantics; returns `{ok, revealed, room}`. |
| `POST …/doors` `{door, discovered?, state?}` | **new.** Records a found secret door and/or a runtime state override in `door_states`. `door` is a door key, or `{"between": [a, b]}` which resolves the door joining two nodes. 404 on unknown door. |
| `GET /maps/{map_id}/rooms` | each room gains `label` and `hidden`; `exits` drops hidden doors and a new `secret_exits` lists them. The shape test (`test_campaign_routes.py:181`) is updated deliberately. |

The legacy `/maps/{external_id}/…` contract (combat maps ttrpg2 pushes) gets
the same 404/409 semantics on `party` and `reveal`; its callers are ttrpg2
alone.

`routes/sessions.py:_exits` and the MCP `get_exits` / `get_known_map` /
`mark_door` are rewritten on top of the shared functions; their response
shapes stay as they are, so the web app and MCP clients see no change.

### dungml — map validation

`validate.py` gains two warnings, surfaced through the editor's existing
diagnostics: **disconnected graph** (more than one component, naming the
components' smallest members) and **dead-end corridor** (a corridor with one
door). Surface Level East would show both today. Warnings, not errors: a
cave map can legitimately have an isolated area reached by a map exit.

### ttrpg2 — one position

`state.json["position"]` becomes the single answer to "where is the party":

```json
{"location": "stonehell-gate",
 "map_id": "22110069…", "map_name": "Surface Level East",
 "node": "room.canyon", "label": "The Canyon",
 "since": {"day": 1, "time": "12:42"}}
```

`location` is a ttrpg2 location/area slug (towns, wilderness, a dungeon as a
whole); `map_id`/`node` are set when the party is somewhere on a dungml map.
Either half may be empty.

**`move_party(to, map="", location="")`** — the only way the party moves:
1. If `map` names a different locale, switch to it (`dungml_use_locale` logic).
2. If a map is active and `to` is non-empty, `POST …/party` with `to`
   (resolved server-side). On 404, return the error and the candidates;
   **nothing moves**.
3. If `location` is given (or `to` is a known location slug and no map is
   active), set `position.location`.
4. Write `position`, append one `location_visited` event
   (`{slug, name, map_id, node, label}`).
5. Return `{position, room, room_notes, location_brief, warning?}` — the room
   context from step 2, this campaign's notes on that room, and the opening
   of the linked location file when `location` changed.

If dmap-server is down, the ttrpg2 half still moves and the result carries
`warning: "map service unreachable — party marker not moved"`.

**`room(query="")`** — room context for the current room, or any room on the
active map by id/name/label. Read-only; for "what's through that door" and
for re-reading a room after a reset.

**`door(door, state="", found=False)`** — `door` is a key, or a room the door
leads to from the current room ("the door to 37"). Records state/found on
the session. The DM calls it when a door is opened, forced, locked or a
secret door is found.

**`note_room(text, query="")`** — appends a timestamped note to the current
(or named) room in `campaigns/<slug>/room_notes.json`, keyed by
`map_id:node`. Returned by `move_party` and `room`. This is where "the horse
is tied at the fold", "cleared — 4 troglodytes dead", "barricaded the east
door" live, so they survive a reset. Sub-project B's scribe will write here
too.

**Kept, changed:**
- `dungml_reveal(room)` — reveal without moving; resolves and verifies,
  returns the room context or an error.
- `dungml_rooms()` — adds `label`, drops secret doors from `exits`, adds
  `secret_exits`, and reads `party_node` live instead of from the cache.
- `dungml_status/locales/use_locale/view` — unchanged. (Loud failures are
  delivered where they matter: `move_party`, `room`, `door`,
  `dungml_reveal` and the primer's position block all report dungml
  errors instead of swallowing them.)

**Removed:** `dungml_party_at` (subsumed by `move_party`). Net, this adds
four tools (`move_party`, `room`, `door`, `note_room`, `module_lookup`, less
`dungml_party_at`); they replace most exploration-time calls to
`get_location`, `dungml_rooms` and Bash reads of module files. Sub-project E
reduces the overall tool surface.

**`create_area` / `create_location` stop moving the party.** Creating a file
is world-building, not travel; they no longer log `location_visited`. The
primer, dashboard "At" line and `introduce_npc` read `position` instead of
scanning events. Old campaigns without `position` fall back to the last
`location_visited` event once, and `position` is written on the first move.

**Primer / scene block.** `session_primer`'s `scene` gains a `position`
block: the `position` record, the current room's context (`perceived` and
`dm_only`, read silently), and its room notes — replacing the "last created
location file" guess. `pre_turn_check` adds `position.label` and the
current room's perceived exits.

**Module text.** `module_lookup(query, section="")` — full-text search over
`campaigns/<slug>/reference/*.md`, returning matching paragraphs with their
heading path (e.g. *Level One › 1B The Quiet Halls › Room Key › 5*). For
module material the map doesn't carry (history, NPC motives, restocking).
DM-only; CLAUDE.md says so. Built on SQLite FTS5 in memory per campaign,
rebuilt when a reference file's mtime changes.

### ttrpg2 — CLAUDE.md

Replace the fog-of-war instructions ("call `dungml_party_at` whenever…")
with: *the party moves only through `move_party`; narrate the room from the
`room` it returns — `perceived` is what the characters see, `dm_only` is
never narrated; if the fiction needs a room the map doesn't have, say so
(unchanged policy).* Shorter than what it replaces.

### Error handling

| Situation | Result |
|---|---|
| dmap-server down | ttrpg2 half of the move happens; `warning` in the result; tab shows "map service offline" (unchanged) |
| unknown / ambiguous room | error + candidates (labels and ids); nothing moves |
| map source doesn't parse | 409 from dungml → error "map is mid-edit in the dungml editor" |
| campaign not linked / no locale | `move_party` with only `location` works; with `to` it returns an error naming `dungml_locales` |

### Testing

dungml:
- `dsl`: `resolve_node` (id, bare, label, display name, ambiguity);
  `room_context` partitioning (secret door unfound → `dm_only`; found →
  `perceived`; trapped never perceived; secret feature types; effective door
  state; boundary exits; secret map exits); `known_map` labels.
- `backend`: new routes; 404-with-candidates on `party`/`reveal`; `party`
  clears other maps' marker; `doors`; `/rooms` new shape; validation
  warnings. Existing `test_source_preserves_discovery` must stay green.
- `mcp`: existing `get_exits`/`get_known_map`/`mark_door` tests unchanged and
  green on the shared implementation.

ttrpg2 (HTTP stubbed as in `test_dungml_play.py`, campaign via
`TempCampaign`):
- `move_party` happy path writes `position`, one event, returns room +
  notes; 404 moves nothing; server down still moves the location and warns;
  `map=` switches locale.
- `create_location` no longer moves the party; primer/dashboard read
  `position`; fallback for campaigns without it.
- `note_room` round-trip; `door` resolution by neighbour label.
- `module_lookup` against a fixture reference dir.

### Rollout

1. dungml: shared functions + refactor of the three callers (no behaviour
   change), then new routes and error semantics. Restart `dmap-server`.
2. ttrpg2: new tools against the new routes; CLAUDE.md edit; remove
   `dungml_party_at`.
3. Merge dungml first. The new routes are additive; the changed parts are
   the `/rooms` shape and the 404/409 codes on `party`/`reveal`. Today's
   ttrpg2 turns a non-GET 404 into `DungmlUnavailable` and returns it as an
   `error`, so an old ttrpg2 against a new dungml reports an unknown room
   instead of silently succeeding — a strict improvement, not a crash.

### Data cleanup (GM, in the dungml editor — not part of this work)

- Surface Level East: `corridor_1` dead-ends; rooms 2–5 and the caves are
  unreachable from the canyon (the new validator will flag this).
- A second "Stonehell Dungeon" project duplicates every map (different
  Surface East source); delete one.
- `campaigns/stonehell-descent/reference/maps/*.dmap` and
  `_Surface_Level_East.dmap` diverge from the authored maps; delete them or
  mark them non-authoritative, so the DM never reads stale geometry.
- The active campaign has party markers on East and Gatehouse; the first
  `move_party` clears the stale one.
