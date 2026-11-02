# yuvomi-bring-sync

Two-way sync between the shopping lists of **Yuvomi** and **Bring!**. One
container, no published ports, polls both sides every 10 seconds.

```
Bring!  <--->  bridge.py  <--->  Yuvomi
        (10 s tick, SQLite state)
```

## What syncs what

| | Bring → Yuvomi | Yuvomi → Bring |
|---|---|---|
| new item | `POST /shopping/:id/items`, category from the Bring product catalogue | `save_item` (quantity as `spec`) |
| name / quantity | `PATCH /shopping/items/:id` (Yuvomi wins ties) | `update_item` |
| checked off | `items.recently` → `is_checked=1` (Bring wins ties) | `complete_item`, or `ADD` on un-check |
| deleted | gone in Bring = gone in Yuvomi two ticks later, checked or not | `DELETE` → `remove_item` |
| lists | Bring list → Yuvomi list by name, missing ones created | – |

No timestamps: Bring does not expose `updated_at`, so conflicts follow the
table above. The rules are `plan_list()` and have tests.

Categories come from the public catalogue
`https://web.getbring.com/locale/catalog.<locale>.json` (~330 products).
`config.json` maps Bring sections onto existing Yuvomi categories; unmapped
ones get created unless `create_missing_categories` is off.

## Files

| File | Purpose |
|---|---|
| `bridge.py` | the bridge (planner is pure computation, hence testable offline) |
| `test_bridge.py` | offline tests |
| `config.json` | skipped lists + category map |
| `.env.example` | credential template → copy to `.env`, never commit `.env` |
| `Dockerfile`, `docker-compose.yml` | container |
| `requirements.txt` | pinned `bring-api` |

## Configuration

`.env` (via `env_file`, never in git):

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `BRING_USERNAME` | yes | – | Bring! e-mail |
| `BRING_PASSWORD` | yes | – | Bring! password |
| `YUVOMI_URL` | yes | – | e.g. `http://yuvomi:3000` (container name, see `web` below) |
| `YUVOMI_TOKEN` | yes | – | Yuvomi API token |
| `POLL_SECONDS` | no | `10` | seconds between cycles |
| `BRIDGE_DATA` | no | `./data` | holds `bridge.db` and `last_ok` (`/data` in the image) |
| `CATALOG_TPL` | no | catalogue URL | `{locale}` gets substituted |

`config.json`: `skip_lists` (Bring list names to ignore), `category_map`
(Bring section → Yuvomi category), `create_missing_categories` (default on).

`environment:` in the compose file beats `env_file` — change `YUVOMI_URL` and
`POLL_SECONDS` there, credentials in `.env`.

## Run

```bash
mkdir -p yuvomi-bring && cd yuvomi-bring
cp bridge.py config.json Dockerfile docker-compose.yml requirements.txt .
docker network create web                     # once, see below
cp .env.example .env && chmod 600 .env        # fill in the four values
docker compose up -d --build
```

No `ports:` — the bridge only talks outward.

CI also builds the image and pushes it to
`ghcr.io/panic175/yuvomi-bring-sync` — `latest` and the commit sha on every
push to `main`, plus a version tag when you push one:

```bash
git tag v1.0.0 && git push --tags   # publishes :1.0.0
```

so a host can `docker pull` instead of building.

**About the `web` network.** The compose file joins an *external* Docker
network called `web` (`networks: web: external: true`). Docker networks are
per compose project, so a container of another stack is not reachable by name
from here. `web` is the shared network you create once with
`docker network create web` and every stack that should see each other joins —
that is how this bridge reaches Yuvomi as `http://yuvomi:3000`. If your Yuvomi
sits on a differently named network, create that name instead and edit the
compose file, or drop `networks:` and point `YUVOMI_URL` at whatever address
this container can reach.

```bash
docker logs -f yuvomi-bring                                      # running tick
docker compose run --rm yuvomi-bring python bridge.py --once --dry-run   # plan only
docker restart yuvomi-bring                                      # reload catalogue + state
```

* State in `./data/bridge.db` (pairing + last known values). Delete it and the
  bridge re-pairs both sides by name — no data lost, no duplicates.
* `./data/last_ok` is the health marker; older than 60 s → `unhealthy`.
* Catalogue is fetched once per start — restart after it changes.
* Yuvomi allows 300 requests per minute per address. On `429` the bridge waits
  30 s and skips empty lists. Still hitting it? Raise `POLL_SECONDS`.
* After editing `.env`: `docker compose up -d --force-recreate` — a plain
  `docker restart` does not re-read `env_file`.

## Tests

```bash
python3 -m unittest -v test_bridge
```

No network, no third-party packages. Covers the planner, the state store, the
Yuvomi client, the catalogue, pairing, action execution, the orchestration
including the 429 and error paths, and `main()`.

`run()` is not unit-tested (needs `aiohttp`, `bring-api`, network); CI runs
`python bridge.py --help` inside the built image instead.

```bash
pip install coverage
coverage run --source=bridge -m unittest test_bridge && coverage report -m
```

CI runs the suite on Python 3.11 and 3.12 with a 90 % coverage gate, builds
the image, smoke-tests the entrypoint and scans the history with gitleaks.

## Known limits

* No `updated_at` in Bring: conflicts follow the table, not timestamps.
* Gone in Bring = gone in Yuvomi two ticks later, even when checked off —
  emptying "recently bought" also tidies Yuvomi. Exception: a push of ours
  that never arrived is retried up to `MAX_PUSH_ATTEMPTS` and then kept in
  Yuvomi alone (deleting it would be data loss we caused ourselves).
* Bring sometimes hands the same item a new UUID (delete + re-create). The
  stale row clears after two ticks; until then the item shows twice in Yuvomi.
* Renaming while an item sits in "recently bought" waits for the un-check —
  the library's ADD path would otherwise pull it back onto the list.
* Bring's API is unofficial; `bring-api` is pinned in `requirements.txt`.

## License

MIT
