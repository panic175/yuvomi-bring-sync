#!/usr/bin/env python3
"""Two-way bridge between the Yuvomi shopping lists and Bring!.

Both sides are polled on an interval and reconciled against a local state
store (SQLite). Rules:

  * New items travel in both directions, edits as well.
  * "Checked" is mirrored both ways: Bring keeps open items in
    items.purchase and bought ones in items.recently, Yuvomi uses is_checked.
  * Categories come from the public Bring product catalogue and are mapped
    onto the Yuvomi categories through config.json.
  * An item missing on the Bring side is confirmed over several polls before
    anything happens; bought items are never deleted (reason unknown).

Usage: python bridge.py [--once] [--dry-run] [--config FILE]
Runtime needs bring-api and aiohttp, the tests do not (standard library
only), so they run anywhere without installing dependencies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import uuid as uuidlib
from pathlib import Path

log = logging.getLogger("bring-bridge")

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BRIDGE_DATA", str(HERE / "data")))
CATALOG_TPL = os.environ.get(
    "CATALOG_TPL", "https://web.getbring.com/locale/catalog.{locale}.json"
)
DEFAULT_LOCALE = "de-DE"
FALLBACK_CATEGORY = "Sonstiges"   # Yuvomi's own catch-all category

# Bring does not distinguish an item that dropped out of "recently bought"
# from one someone deleted: it is simply absent. So absence only counts once
# it has been seen missing twice in a row.
MIN_MISSING = 2
# An item pushed to Bring that never shows up is retried at most this often -
# otherwise an error would create duplicates.
MAX_PUSH_ATTEMPTS = 3


# ---------------------------------------------------------------- helpers

def norm(value) -> str:
    """Comparison key, case and whitespace insensitive."""
    return (value or "").strip().casefold()


def client_uuid(list_id, item_id) -> str:
    """Deterministic UUID for items this bridge creates on the Bring side.

    Stable across retries so a re-push lands on the same slot instead of
    creating a second item.
    """
    return str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, f"yuvomi/{list_id}/{item_id}"))


def parse_catalog(data: dict) -> dict[str, str]:
    """Catalogue JSON into {item name (lowercase): section}."""
    mapping: dict[str, str] = {}
    for section in (data or {}).get("catalog", {}).get("sections", []):
        name = section.get("name") or section.get("sectionId")
        if not name:
            continue
        for item in section.get("items", []):
            for key in ("name", "itemId"):
                if item.get(key):
                    mapping[norm(item[key])] = name
    return mapping


def resolve_category(section, cfg, existing: set) -> tuple:
    """Bring section -> (Yuvomi category, name to create or None)."""
    if not section:
        return FALLBACK_CATEGORY, None
    mapped = (cfg.get("category_map") or {}).get(section)
    if mapped:
        return mapped, (mapped if mapped not in existing else None)
    if section in existing:
        return section, None
    if cfg.get("create_missing_categories", True):
        return section, section
    return FALLBACK_CATEGORY, None


# ---------------------------------------------------------------- planning

def plan_list(*, blist_uuid: str, y_list_id, bring_items: list, y_items: list,
              rows: list, cfg: dict, y_categories: set, cat_sections: dict,
              attempts: dict) -> list:
    """Work out the actions for one list. Pure computation, no I/O.

    bring_items: uuid, name, spec, checked (0/1)
    y_items:     id, name, quantity, category, is_checked (0/1)
    rows:        state rows (dicts) from SQLite
    """
    actions: list = []
    b_by_uuid = {b["uuid"]: b for b in bring_items}
    y_by_id = {y["id"]: y for y in y_items}
    rows_by_b = {r["bring_uuid"]: r for r in rows}
    free_b = [b for b in bring_items if b["uuid"] not in rows_by_b]
    free_y = [y for y in y_items if y["id"] not in {r["yuvomi_item_id"] for r in rows}]

    # Pair up free items on both sides by name: that is how a lost state file
    # rejoins existing data instead of duplicating it.
    pool: dict = {}
    for y in free_y:
        pool.setdefault(norm(y["name"]), []).append(y["id"])
    matched: dict = {}
    for b in free_b:
        bucket = pool.get(norm(b["name"]))
        if bucket:
            matched[b["uuid"]] = bucket.pop(0)

    # 1) Bring items without a partner: create them in Yuvomi.
    for b in free_b:
        if b["uuid"] in matched:
            continue
        section = cat_sections.get(norm(b["name"]))
        category, new_cat = resolve_category(section, cfg, y_categories)
        if new_cat:
            actions.append({"kind": "cat_create", "name": new_cat})
            y_categories.add(new_cat)
        actions.append({"kind": "y_create", "list_id": y_list_id, "name": b["name"],
                        "quantity": b["spec"], "category": category})

    # 2) Yuvomi items without a partner: push them to Bring.
    for y in free_y:
        if y["id"] in matched.values():
            continue
        key = (y_list_id, y["id"])
        if attempts.get(key, 0) >= MAX_PUSH_ATTEMPTS:
            log.error("Giving up: %r never shows up in Bring", y["name"])
            continue
        attempts[key] = attempts.get(key, 0) + 1
        actions.append({"kind": "b_add", "list_uuid": blist_uuid, "name": y["name"],
                        "spec": y["quantity"] or "",
                        "item_uuid": client_uuid(y_list_id, y["id"])})

    # 3) Reconcile existing pairs.
    for b_uuid, row in rows_by_b.items():
        b = b_by_uuid.get(b_uuid)
        y = y_by_id.get(row["yuvomi_item_id"])

        if b is None and y is None:
            actions.append({"kind": "row_drop", "bring_uuid": b_uuid})
            continue

        if b is None:
            # Gone from the Bring side. Delete only once absence is confirmed
            # and only for open items - bought ones are left alone on purpose.
            if row.get("gone"):
                if int(y["is_checked"]):
                    continue                   # still bought: leave it alone
                if int(row.get("attempts") or 0) >= MAX_PUSH_ATTEMPTS:
                    log.error("Giving up: %r cannot be pushed back to Bring", y["name"])
                    continue
                actions.append({"kind": "b_add", "list_uuid": blist_uuid,
                                "name": y["name"], "spec": y["quantity"] or "",
                                "item_uuid": client_uuid(y_list_id, y["id"])})
                actions.append({"kind": "row_set", "bring_uuid": b_uuid, "fields": {
                    "gone": 0, "pending": 1, "missing": 0,
                    "attempts": int(row.get("attempts") or 0) + 1}})
                continue

            missing = int(row.get("missing") or 0) + 1
            fields = {"missing": missing}
            if row.get("pending") and missing >= MIN_MISSING:
                # Pushed but never appeared: mark as traceless, the item stays
                # alive in Yuvomi.
                fields.update(gone=1, pending=0, missing=0)
            elif not row.get("pending") and missing >= MIN_MISSING:
                if int(row["b_checked"]):
                    # shortcut: Bring prunes "recently bought" itself and a
                    # real deletion is indistinguishable - the item stays
                    # until it is deleted in Yuvomi.
                    fields.update(gone=1, missing=0)
                else:
                    actions.append({"kind": "y_delete", "item_id": row["yuvomi_item_id"]})
                    actions.append({"kind": "row_drop", "bring_uuid": b_uuid})
                    continue
            actions.append({"kind": "row_set", "bring_uuid": b_uuid, "fields": fields})
            continue

        if y is None:
            # Deleted in Yuvomi -> remove it from Bring too.
            actions.append({"kind": "b_remove", "list_uuid": blist_uuid,
                            "name": b["name"], "item_uuid": b_uuid})
            actions.append({"kind": "row_drop", "bring_uuid": b_uuid})
            continue

        if row.get("gone") and int(y["is_checked"]):
            continue

        b_name, b_spec, b_checked = b["name"], b["spec"] or "", int(b["checked"])
        y_name, y_qty, y_checked = y["name"], y["quantity"] or "", int(y["is_checked"])

        # Compute targets. Text: Yuvomi wins when it changed, otherwise Bring.
        # Checked: Bring wins.
        t_y_name, t_y_qty, t_y_checked = y_name, y_qty, y_checked
        t_b_name, t_b_spec, t_b_checked = b_name, b_spec, b_checked
        if y_name != (row["y_name"] or ""):
            t_b_name = y_name
        elif b_name != (row["b_name"] or ""):
            t_y_name = b_name
        if y_qty != (row["y_qty"] or ""):
            t_b_spec = y_qty
        elif b_spec != (row["b_spec"] or ""):
            t_y_qty = b_spec
        if b_checked != int(row["b_checked"]):
            t_y_checked = b_checked
        elif y_checked != int(row["y_checked"]):
            t_b_checked = y_checked

        y_fields = {}
        if t_y_name != y_name:
            y_fields["name"] = t_y_name
        if t_y_qty != y_qty:
            y_fields["quantity"] = t_y_qty
        if t_y_checked != y_checked:
            y_fields["is_checked"] = bool(t_y_checked)
        if y_fields:
            actions.append({"kind": "y_patch", "item_id": y["id"], "fields": y_fields})

        if t_b_checked != b_checked:
            if t_b_checked:
                actions.append({"kind": "b_complete", "list_uuid": blist_uuid,
                                "name": b_name, "spec": b_spec, "item_uuid": b_uuid})
            else:
                # Back onto the shopping list: the library's ADD path also
                # carries the text, so Yuvomi wins on rename/quantity here.
                actions.append({"kind": "b_reopen", "list_uuid": blist_uuid,
                                "name": t_y_name, "spec": t_y_qty, "item_uuid": b_uuid})
        elif t_b_name != b_name or t_b_spec != b_spec:
            # shortcut: renaming while the item sits in "recently bought"
            # would use the ADD path and pull it back onto the shopping list.
            # The edit is applied when the item gets unchecked.
            if not b_checked:
                actions.append({"kind": "b_update", "list_uuid": blist_uuid,
                                "name": t_b_name, "spec": t_b_spec, "item_uuid": b_uuid})

    return actions


# ---------------------------------------------------------------- Yuvomi

class YuvomiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Yuvomi:
    """Thin REST client for Yuvomi's shopping lists."""

    def __init__(self, session, base_url: str, token: str):
        self._session = session
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    async def _call(self, method: str, path: str, body=None):
        url = f"{self.base}/api/v1{path}"
        async with self._session.request(method, url, headers=self.headers,
                                         json=body) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise YuvomiError(resp.status,
                                  f"{method} {path} -> {resp.status}: {text[:300]}")
            return json.loads(text) if text.strip() else {}

    async def lists(self) -> list:
        return (await self._call("GET", "/shopping"))["data"]

    async def create_list(self, name: str) -> dict:
        return (await self._call("POST", "/shopping", {"name": name}))["data"]

    async def items(self, list_id) -> list:
        return (await self._call("GET", f"/shopping/{list_id}/items"))["data"]

    async def categories(self) -> list:
        return (await self._call("GET", "/shopping/categories"))["data"]

    async def create_category(self, name: str) -> None:
        try:
            await self._call("POST", "/shopping/categories", {"name": name})
        except YuvomiError as err:
            if err.status != 409:          # "category_exists" is not an error
                raise

    async def create_item(self, list_id, name: str, quantity: str, category: str) -> dict:
        body = {"name": name}
        if quantity:
            body["quantity"] = quantity
        if category:
            body["category"] = category
        return (await self._call("POST", f"/shopping/{list_id}/items", body))["data"]

    async def patch_item(self, item_id, fields: dict) -> dict:
        return (await self._call("PATCH", f"/shopping/items/{item_id}", fields))["data"]

    async def delete_item(self, item_id) -> None:
        await self._call("DELETE", f"/shopping/items/{item_id}")


# ---------------------------------------------------------------- catalogue

class Catalog:
    """Public Bring product catalogue: item name -> section (category)."""

    def __init__(self, session):
        self._session = session
        self._maps: dict = {}

    async def sections_for(self, locale: str) -> dict:
        if locale in self._maps:
            return self._maps[locale]
        data = await self._fetch(locale) or await self._fetch(DEFAULT_LOCALE)
        mapping = parse_catalog(data)
        if not mapping:
            log.warning("Product catalogue for %s unreadable, categories skipped", locale)
        # shortcut: loaded once per start; catalogue changes only take effect
        # after the container restarts
        self._maps[locale] = mapping
        return mapping

    async def _fetch(self, locale: str):
        try:
            async with self._session.get(CATALOG_TPL.format(locale=locale)) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception as err:            # no catalogue is no reason to stop
            log.warning("Catalogue %s unreachable: %s", locale, err)
            return None


# ---------------------------------------------------------------- state

SCHEMA = """
CREATE TABLE IF NOT EXISTS list_map (
  bring_uuid     TEXT PRIMARY KEY,
  bring_name     TEXT NOT NULL,
  yuvomi_list_id INTEGER NOT NULL,
  yuvomi_name    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_map (
  bring_uuid      TEXT PRIMARY KEY,
  bring_list_uuid TEXT NOT NULL,
  yuvomi_item_id  INTEGER NOT NULL UNIQUE,
  b_name          TEXT NOT NULL DEFAULT '',
  b_spec          TEXT NOT NULL DEFAULT '',
  b_checked       INTEGER NOT NULL DEFAULT 0,
  y_name          TEXT NOT NULL DEFAULT '',
  y_qty           TEXT NOT NULL DEFAULT '',
  y_cat           TEXT NOT NULL DEFAULT '',
  y_checked       INTEGER NOT NULL DEFAULT 0,
  missing         INTEGER NOT NULL DEFAULT 0,
  gone            INTEGER NOT NULL DEFAULT 0,
  pending         INTEGER NOT NULL DEFAULT 0,
  attempts        INTEGER NOT NULL DEFAULT 0
);
"""


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # lists
    def list_map(self) -> dict:
        return {r["bring_uuid"]: dict(r)
                for r in self.conn.execute("SELECT * FROM list_map")}

    def map_list(self, bl: dict, y_list: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO list_map"
            " (bring_uuid, bring_name, yuvomi_list_id, yuvomi_name) VALUES (?, ?, ?, ?)",
            (bl["bring_uuid"], bl["bring_name"], y_list["id"], y_list["name"]))
        self.conn.commit()

    def clear_items(self, blist_uuid: str) -> None:
        self.conn.execute("DELETE FROM item_map WHERE bring_list_uuid = ?", (blist_uuid,))
        self.conn.commit()

    # items
    def rows(self, blist_uuid: str) -> list:
        cur = self.conn.execute("SELECT * FROM item_map WHERE bring_list_uuid = ?"
                                " ORDER BY bring_uuid", (blist_uuid,))
        return [dict(r) for r in cur.fetchall()]

    def insert_row(self, blist_uuid: str, b: dict, y: dict) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO item_map (bring_uuid, bring_list_uuid, yuvomi_item_id,"
            " b_name, b_spec, b_checked, y_name, y_qty, y_cat, y_checked)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (b["uuid"], blist_uuid, y["id"], b["name"], b["spec"] or "", int(b["checked"]),
             y["name"], y["quantity"] or "", y["category"] or "", int(y["is_checked"])))
        self.conn.commit()

    def row_set(self, bring_uuid: str, fields: dict) -> None:
        if not fields:
            return
        cols = ", ".join(f"{key} = ?" for key in fields)
        self.conn.execute(f"UPDATE item_map SET {cols} WHERE bring_uuid = ?",
                          (*fields.values(), bring_uuid))
        self.conn.commit()

    def row_drop(self, bring_uuid: str) -> None:
        self.conn.execute("DELETE FROM item_map WHERE bring_uuid = ?", (bring_uuid,))
        self.conn.commit()


# ---------------------------------------------------------------- execution

async def execute(actions: list, y: Yuvomi, bring, state: State) -> None:
    for act in actions:
        kind = act.get("kind")
        try:
            if kind == "cat_create":
                await y.create_category(act["name"])
            elif kind == "y_create":
                await y.create_item(act["list_id"], act["name"],
                                    act["quantity"], act["category"])
            elif kind == "y_patch":
                await y.patch_item(act["item_id"], act["fields"])
            elif kind == "y_delete":
                await y.delete_item(act["item_id"])
            elif kind == "b_add":
                await bring.save_item(act["list_uuid"], act["name"], act["spec"],
                                      item_uuid=act["item_uuid"])
            elif kind == "b_update":
                await bring.update_item(act["list_uuid"], act["name"], act["spec"],
                                        item_uuid=act["item_uuid"])
            elif kind == "b_complete":
                await bring.complete_item(act["list_uuid"], act["name"], act["spec"],
                                          item_uuid=act["item_uuid"])
            elif kind == "b_reopen":
                await bring.save_item(act["list_uuid"], act["name"], act["spec"],
                                      item_uuid=act["item_uuid"])
            elif kind == "b_remove":
                await bring.remove_item(act["list_uuid"], act["name"],
                                        item_uuid=act["item_uuid"])
            elif kind == "row_set":
                state.row_set(act["bring_uuid"], act["fields"])
            elif kind == "row_drop":
                state.row_drop(act["bring_uuid"])
            else:
                log.warning("Unknown action: %r", act)
        except Exception:
            log.exception("Action failed: %s", act)


async def bring_items(bring, blist_uuid: str) -> list:
    resp = await bring.get_list(blist_uuid)
    items = [{"uuid": i.uuid, "name": i.itemId, "spec": i.specification or "", "checked": 0}
             for i in resp.items.purchase]
    items += [{"uuid": i.uuid, "name": i.itemId, "spec": i.specification or "", "checked": 1}
              for i in resp.items.recently]
    return items


async def reconcile(bring, y: Yuvomi, state: State, blist_uuid: str, y_list_id) -> None:
    """Read the current state, join loose pairs, record what is true now."""
    b_items = await bring_items(bring, blist_uuid)
    y_items = await y.items(y_list_id)
    rows = state.rows(blist_uuid)
    rows_by_b = {r["bring_uuid"]: r for r in rows}
    paired_y = {r["yuvomi_item_id"] for r in rows}

    # Join free items by name - that is how items just created on either side
    # register, even if Bring did not keep the UUID we supplied.
    free_y = {}
    for item in y_items:
        if item["id"] not in paired_y:
            free_y.setdefault(norm(item["name"]), []).append(item)
    for b in b_items:
        if b["uuid"] in rows_by_b:
            continue
        bucket = free_y.get(norm(b["name"]))
        if not bucket:
            continue
        state.insert_row(blist_uuid, b, bucket.pop(0))

    # Record the current values for every pair; counters stay untouched while
    # a side is missing, that is the planner's decision.
    for row in state.rows(blist_uuid):
        b = next((i for i in b_items if i["uuid"] == row["bring_uuid"]), None)
        y = next((i for i in y_items if i["id"] == row["yuvomi_item_id"]), None)
        if y is None:
            continue
        fields = {"y_name": y["name"], "y_qty": y["quantity"] or "",
                  "y_cat": y["category"] or "", "y_checked": int(y["is_checked"])}
        if b is not None:
            fields.update(b_name=b["name"], b_spec=b["spec"] or "",
                          b_checked=int(b["checked"]), missing=0, gone=0,
                          pending=0, attempts=0)
        state.row_set(row["bring_uuid"], fields)


# ---------------------------------------------------------------- orchestration

class Bridge:
    def __init__(self, *, bring, y: Yuvomi, catalog: Catalog, state: State,
                 cfg: dict, dry_run: bool = False):
        self.bring = bring
        self.y = y
        self.catalog = catalog
        self.state = state
        self.cfg = cfg
        self.dry_run = dry_run
        self.skip = {norm(n) for n in cfg.get("skip_lists", [])}
        self.attempts: dict = {}

    async def sync_list(self, bl: dict, y_lists: list, y_categories: set) -> None:
        mapping = self.state.list_map().get(bl["listUuid"])
        if mapping is None or not any(l["id"] == mapping["yuvomi_list_id"] for l in y_lists):
            # The Yuvomi list does not exist (yet): match it by name, else create.
            target = next((l for l in y_lists if norm(l["name"]) == norm(bl["name"])), None)
            if target is None:
                if self.dry_run:
                    log.info("[dry-run] would create Yuvomi list %r", bl["name"])
                    return
                target = await self.y.create_list(bl["name"])
                y_lists.append(target)
                log.info("Created Yuvomi list %r", target["name"])
            if mapping is not None:
                # The previously mapped list was deleted: start over, Bring is
                # the source of truth again.
                log.warning("Yuvomi list %r is gone, recreating it",
                            mapping["yuvomi_name"])
                self.state.clear_items(bl["listUuid"])
            self.state.map_list({"bring_uuid": bl["listUuid"], "bring_name": bl["name"]},
                                target)
            mapping = self.state.list_map()[bl["listUuid"]]

        y_list_id = mapping["yuvomi_list_id"]
        b_items = await bring_items(self.bring, bl["listUuid"])
        y_items = await self.y.items(y_list_id)
        rows = self.state.rows(bl["listUuid"])
        locale = (self.bring.user_list_settings.get(bl["listUuid"], {})
                  .get("listArticleLanguage") or self.bring.user_locale)
        sections = await self.catalog.sections_for(locale)

        actions = plan_list(blist_uuid=bl["listUuid"], y_list_id=y_list_id,
                            bring_items=b_items, y_items=y_items, rows=rows,
                            cfg=self.cfg, y_categories=y_categories,
                            cat_sections=sections, attempts=self.attempts)
        if self.dry_run:
            for act in actions:
                log.info("[dry-run] %s", json.dumps(act, ensure_ascii=False))
            return
        if actions:
            await execute(actions, self.y, self.bring, self.state)
        await reconcile(self.bring, self.y, self.state, bl["listUuid"], y_list_id)

    async def cycle(self) -> None:
        if not self.bring.uuid:
            await self.bring.login()
            log.info("Signed in to Bring")
        lists = await self.bring.load_lists()
        y_lists = await self.y.lists()
        y_categories = {c["name"] for c in await self.y.categories()}
        for bl in lists.lists:
            if norm(bl.name) in self.skip:
                continue
            try:
                await self.sync_list({"listUuid": bl.listUuid, "name": bl.name},
                                     y_lists, y_categories)
            except Exception:
                log.exception("List %r failed", bl.name)


# ---------------------------------------------------------------- start

async def run(args) -> int:
    import aiohttp                      # imported at runtime so tests stay dependency free
    from bring_api import Bring

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        bring = Bring(session, os.environ["BRING_USERNAME"], os.environ["BRING_PASSWORD"])
        y = Yuvomi(session, os.environ["YUVOMI_URL"], os.environ["YUVOMI_TOKEN"])
        bridge = Bridge(bring=bring, y=y, catalog=Catalog(session),
                        state=State(DATA_DIR / "bridge.db"), cfg=cfg,
                        dry_run=args.dry_run)
        interval = int(os.environ.get("POLL_SECONDS", "10"))
        while True:
            try:
                await bridge.cycle()
                (DATA_DIR / "last_ok").touch()
            except Exception:
                log.exception("Sync cycle failed")
            if args.once:
                return 0
            await asyncio.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="Yuvomi <-> Bring! bridge")
    parser.add_argument("--config", default=str(HERE / "config.json"))
    parser.add_argument("--once", action="store_true", help="run a single cycle")
    parser.add_argument("--dry-run", action="store_true",
                        help="log what would happen, change nothing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
