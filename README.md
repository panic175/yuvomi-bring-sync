# yuvomi-bring-sync

Two-way bridge between the shopping lists of **[Yuvomi](https://yuvomi.app)** and
**Bring!**. Runs as a single container with no published ports, polls both sides
every 10 seconds and keeps them in step.

```
Bring!  <--->  bridge.py  <--->  Yuvomi
        (10 s tick, SQLite state)
```

## What is synchronised

| | Bring → Yuvomi | Yuvomi → Bring |
|---|---|---|
| new item | `POST /shopping/:id/items`, category taken from the Bring product catalogue | `save_item` (quantity as `spec`) |
| name / quantity | `PATCH /shopping/items/:id` (Yuvomi wins on a simultaneous edit) | `update_item` |
| checked off | `items.recently` → `is_checked=1` (Bring wins) | `complete_item`, or `ADD` when un-checking |
| deleted | after being confirmed missing twice, **including checked-off items** (Bring decides) | `DELETE` → `remove_item` |
| lists | Bring list → Yuvomi list, matched by name; a missing one is created | – |

Categories: `https://web.getbring.com/locale/catalog.<locale>.json` is public
and ships 13 sections with ~330 products. The mapping onto Yuvomi's categories
lives in `config.json` (`category_map`); sections without an entry are created
as Yuvomi categories on the fly (`create_missing_categories`).

Conflict resolution is rule-based, not timestamp-based — Bring does not expose
`updated_at`. The full rules are implemented in `plan_list()` and covered by the
test suite.

## Files

| File | Purpose |
|---|---|
| `bridge.py` | The bridge; the planner is pure computation, so it is testable without a network |
| `test_bridge.py` | Offline self-check: `python3 -m unittest -v test_bridge` |
| `config.json` | Lists to skip + category mapping |
| `.env.example` | Template for the credentials (copy to `.env`, never commit `.env`) |
| `Dockerfile`, `docker-compose.yml` | Container deployment |
| `requirements.txt` | Pinned `bring-api` version |

## Configuration

All credentials come from the environment (`.env` via `env_file`), never from
the repository.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `BRING_USERNAME` | yes | – | Bring! e-mail address |
| `BRING_PASSWORD` | yes | – | Bring! password |
| `YUVOMI_URL` | yes | – | Yuvomi base URL, e.g. `http://yuvomi:3000` (Docker-internal) |
| `YUVOMI_TOKEN` | yes | – | Yuvomi API bearer token |
| `POLL_SECONDS` | no | `10` | Seconds between sync cycles |
| `BRIDGE_DATA` | no | `./data` | Directory for `bridge.db` and the `last_ok` health marker (`/data` in the image) |
| `CATALOG_TPL` | no | `https://web.getbring.com/locale/catalog.{locale}.json` | Product catalogue URL template |

`config.json`:

| Key | Default | Meaning |
|---|---|---|
| `skip_lists` | `[]` | Bring list names to leave alone entirely |
| `category_map` | `{}` | Bring section → existing Yuvomi category |
| `create_missing_categories` | `true` | Create unmapped Bring sections as Yuvomi categories |

In `docker-compose.yml`, `environment:` wins over `env_file` — change
`YUVOMI_URL` or `POLL_SECONDS` there, credentials in `.env`.

## Deployment

```bash
mkdir -p yuvomi-bring && cd yuvomi-bring
cp bridge.py config.json Dockerfile docker-compose.yml requirements.txt .
docker network create web          # external network the stack attaches to
cp .env.example .env && chmod 600 .env   # then fill in the four credentials
docker compose up -d --build
```

No `ports:` section — the bridge only talks outward. `restart: unless-stopped`.

```bash
docker logs -f yuvomi-bring                        # running tick
docker compose run --rm yuvomi-bring python bridge.py --once --dry-run   # plan only, changes nothing
docker restart yuvomi-bring                        # reload catalogue and state
```

* State lives in `./data/bridge.db` (mapping `bring_uuid ↔ yuvomi_item_id`
  plus the last known value of every item). Without that file the bridge
  re-pairs both sides by name — nothing is lost and no duplicates appear.
* `./data/last_ok` is the healthcheck marker; older than 60 s → container
  `unhealthy`.
* The product catalogue is fetched once at start-up, so catalogue changes only
  take effect after a restart.
* Yuvomi allows **300 API requests per minute per address**. On `429` the
  bridge stops the cycle and waits 30 s; empty lists are skipped so the budget
  survives a dozen mirrored lists. If you hit it anyway, raise `POLL_SECONDS`.
* Changing `.env` requires `docker compose up -d --force-recreate` — a plain
  `docker restart` does not re-read `env_file`.

## Tests

```bash
python3 -m unittest -v test_bridge
```
The suite is offline and needs no third-party packages — neither `aiohttp` nor
`bring-api` is required, so it runs anywhere (locally, in CI, in the
container). Covered: the planner, the SQLite state store, the Yuvomi client,
the product catalogue, pairing in `reconcile()`, action execution, the
orchestration including the 429 and error paths, and `main()`'s argument
wiring.

Only `run()` — the loop that needs `aiohttp`, `bring-api` and a network —
stays uncovered by unit tests; it is exercised by the CI container smoke test
(`python bridge.py --help` inside the built image) instead of being mocked.
Measure coverage locally:

```bash
pip install coverage
coverage run --source=bridge -m unittest test_bridge && coverage report -m
```

CI (GitHub Actions) runs the suite on Python 3.11 and 3.12, gates coverage at
90 %, builds the image and greps for leaked secrets.

## Known limits

* Bring has no `updated_at`: conflicts are decided by the rules above, never
  by timestamp.
* **Missing means deleted** (decision 2026-10-25): Bring decides what exists.
  An item that disappears there is gone from Yuvomi two ticks later — checked
  off or not. Emptying Bring's "recently bought" list therefore also tidies
  Yuvomi. Exception: a push of ours that never arrived is retried up to
  `MAX_PUSH_ATTEMPTS` and otherwise lives on in Yuvomi alone (deleting it
  would destroy data we caused the loss of).
* Bring sometimes assigns a new UUID to the same item (delete + re-create).
  The stale row then disappears after two ticks; until then the item shows up
  twice in Yuvomi for a moment.
* Renaming an item while it sits in "recently bought" is deferred until it is
  unchecked (the library's ADD path would otherwise pull it back onto the
  shopping list).
* The Bring API is unofficial. Changes there break the bridge, not Yuvomi;
  the package version is pinned in `requirements.txt`.

## License

MIT
