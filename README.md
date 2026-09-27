# ttrpg-tools

An AD&D 2nd Edition campaign manager built as an **MCP server** for use with
Claude Code, plus a **Flask web dashboard** for viewing campaign state. The MCP
server exposes ~175 tools covering dice, party and combat tracking, world and
dungeon maps, lore, factions, survival, travel, treasure, rules lookups and
more; the dashboard renders the same data as browsable web pages (the /play
transcript, party status, session chronicle, character sheets, maps, scene
galleries). `CLAUDE.md` is the DM procedure the tools are built around.

Dungeon/combat maps and fog of war come from the companion
[`dungml`](https://github.com/RJanssens/DungML) service — run it alongside this
if you want map rendering. Play is never blocked on it.

## Requirements

- Python 3.11 or newer
- An [OpenAI](https://platform.openai.com/) API key (scene/portrait prompts, narration audio)
- A [Replicate](https://replicate.com/) API token (portrait/scene image generation)
- *(optional)* a running `dmap-server` from `dungml` for dungeon/combat maps

## Install

```bash
git clone git@github.com:RJanssens/TTRPG-Tools.git ttrpg2
cd ttrpg2

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configure

Create a `.env` file in the project root (it is git-ignored — never commit it):

```bash
OPENAI_API_KEY=sk-...
REPLICATE_API_TOKEN=r8_...

# Optional — dungml map integration. The defaults suit a stock local
# dmap-server, so these are only needed when yours differs.
DUNGML_API_BASE=http://127.0.0.1:8000
DUNGML_SERVICE_TOKEN=dev-service
# For an https dmap-server signed by a private CA:
# DUNGML_CA_CERT=/path/to/ca.crt
```

## Run the MCP server

Register the server with Claude Code in a `.mcp.json` at the project root,
with absolute paths to where you cloned the repo:

```json
{
  "mcpServers": {
    "ttrpg": {
      "command": "/path/to/ttrpg2/.venv/bin/python3",
      "args": ["/path/to/ttrpg2/server.py"],
      "cwd": "/path/to/ttrpg2"
    }
  }
}
```

You can also run it directly to check it starts:

```bash
python3 server.py
```

## Run the dashboard

```bash
python3 dashboard.py                  # http://127.0.0.1:5000
python3 dashboard.py --port 8080
python3 dashboard.py --campaign my-campaign
python3 dashboard.py --host 0.0.0.0   # reachable from the network
```

`/` lands on `/play`. Other routes include `/party`, `/log`, `/characters`,
`/locations`, `/map`, `/maps`, `/reference` and `/sheets/<slug>`.

## Tests

```bash
python3 -m unittest discover -s tests
```

Run them under the venv: the dashboard tests skip when Flask is missing.

## Notes

- Campaign data lives under `campaigns/` and is tracked, apart from generated
  images and audio and each campaign's `reference/` module text.
- Copyrighted reference material (rulebook text, module text) is git-ignored.
  Databases under `global/*.db` are too; most are rebuilt by the scripts in
  `tools/`. The exception is `global/lore_facts.db`, the curated "did you
  know" pool — a fresh one holds only a small seed, so back it up.

## License

MIT — see [LICENSE](LICENSE).
