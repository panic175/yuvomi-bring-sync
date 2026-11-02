"""Offline self-check for the bridge - no network, no credentials.

Run with:  python3 -m unittest -v test_bridge
"""

import json
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import bridge
from bridge import (DEFAULT_LOCALE, FALLBACK_CATEGORY, MAX_PUSH_ATTEMPTS,
                    Bridge, Catalog, State, Yuvomi, YuvomiError, bring_items,
                    client_uuid, execute, first_pairing, norm, parse_catalog,
                    plan_list, reconcile, resolve_category, skip_reason,
                    unpaired)

# The failure-path tests raise logged exceptions on purpose - keep it quiet.
logging.getLogger("bring-bridge").setLevel(logging.CRITICAL)

CATALOGUE = {
    "catalog": {
        "sections": [
            {"sectionId": "milk", "name": "Milch & Käse",
             "items": [{"itemId": "Milk", "name": "Milch"}]},
            {"sectionId": "drinks", "name": "Getränke",
             "items": [{"itemId": "Water", "name": "Wasser"}]},
        ]
    }
}


def b(uuid="u1", name="Milch", spec="1 L", checked=0):
    return {"uuid": uuid, "name": name, "spec": spec, "checked": checked}


def y(item_id=10, name="Milch", quantity="1 L", category="Milchprodukte", checked=0):
    return {"id": item_id, "name": name, "quantity": quantity,
            "category": category, "is_checked": checked}


def row(**overrides):
    base = {
        "bring_uuid": "u1", "bring_list_uuid": "L", "yuvomi_item_id": 10,
        "b_name": "Milch", "b_spec": "1 L", "b_checked": 0,
        "y_name": "Milch", "y_qty": "1 L", "y_cat": "Milchprodukte", "y_checked": 0,
        "missing": 0, "gone": 0, "pending": 0, "attempts": 0,
    }
    base.update(overrides)
    return base


def plan(bring=(), yuvomi=(), rows=(), cfg=None, sections=None, cats=None,
         attempts=None):
    return plan_list(
        blist_uuid="L", y_list_id=1, bring_items=list(bring),
        y_items=list(yuvomi), rows=list(rows), cfg=cfg or {},
        y_categories=set(cats if cats is not None else {"Sonstiges", "Milchprodukte"}),
        cat_sections=sections or {}, attempts=attempts if attempts is not None else {})


def kinds(actions):
    return [a["kind"] for a in actions]


def called(recorder, name):
    """Every recorded call of one method, in order."""
    return [c for c in recorder.calls if c[0] == name]


# ---------------------------------------------------------------- fakes
# Nothing here talks to the network: the runtime dependencies (aiohttp,
# bring-api) are deliberately absent so the suite runs anywhere.

class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status = status
        self._payload = payload if payload is not None else {}
        self._text = text if text is not None else json.dumps(self._payload)

    async def text(self):
        return self._text

    async def json(self):
        return self._payload


class _Ctx:
    """`async with session.request(...) as resp` around a canned answer."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        if isinstance(self._value, Exception):
            raise self._value
        return self._value

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Stands in for the aiohttp session; answers come from a queue."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.requests = []
        self.gets = []

    def _next(self):
        return self.responses.pop(0) if self.responses else FakeResponse()

    def request(self, method, url, headers=None, json=None):
        self.requests.append({"method": method, "url": url,
                              "headers": headers, "json": json})
        return _Ctx(self._next())

    def get(self, url):
        self.gets.append(url)
        return _Ctx(self._next())


class Recorder:
    """Generic client stand-in: records every call, answers canned values.

    A canned exception is raised (that is how failures are injected), a
    callable is called with the arguments it received, anything else is
    returned as is. Recording happens on call, not on await - the state store
    is used synchronously, the REST clients are awaited.
    """

    def __init__(self, **canned):
        self.calls = []
        self.canned = canned

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            value = self.canned.get(name)
            if isinstance(value, Exception):
                raise value
            return _Awaitable(value(*args, **kwargs) if callable(value)
                              else value)

        return call


class _Awaitable:
    """Return value of a recorded call, so `await y.items(...)` works."""

    def __init__(self, value):
        self._value = value

    def __await__(self):
        async def done():
            return self._value
        return done().__await__()


def bring_list(purchase=(), recently=()):
    """Shaped like the answer of bring-api's get_list()."""
    def entry(item):
        return SimpleNamespace(uuid=item["uuid"], itemId=item["name"],
                               specification=item.get("spec", ""))
    return SimpleNamespace(items=SimpleNamespace(
        purchase=[entry(i) for i in purchase],
        recently=[entry(i) for i in recently]))


class CatalogueTests(unittest.TestCase):
    def test_parses_names_and_ids(self):
        mapping = parse_catalog(CATALOGUE)
        self.assertEqual(mapping[norm("Milch")], "Milch & Käse")
        self.assertEqual(mapping[norm("Milk")], "Milch & Käse")
        self.assertEqual(mapping[norm("Wasser")], "Getränke")

    def test_empty_payload_is_safe(self):
        self.assertEqual(parse_catalog(None), {})

    def test_section_without_a_name_is_skipped(self):
        payload = {"catalog": {"sections": [{"items": [{"itemId": "X"}]}]}}
        self.assertEqual(parse_catalog(payload), {})


class CategoryTests(unittest.TestCase):
    def test_mapped_section_uses_yuvomi_category(self):
        name, create = resolve_category(
            "Milch & Käse", {"category_map": {"Milch & Käse": "Milchprodukte"}},
            {"Sonstiges"})
        self.assertEqual(name, "Milchprodukte")
        self.assertEqual(create, "Milchprodukte")   # not there yet

    def test_exact_match_is_reused(self):
        name, create = resolve_category("Getränke", {}, {"Getränke"})
        self.assertEqual(name, "Getränke")
        self.assertIsNone(create)

    def test_unknown_section_is_created(self):
        name, create = resolve_category("Tierbedarf", {}, {"Sonstiges"})
        self.assertEqual(name, "Tierbedarf")
        self.assertEqual(create, "Tierbedarf")

    def test_creation_disabled_falls_back(self):
        name, create = resolve_category(
            "Tierbedarf", {"create_missing_categories": False}, {"Sonstiges"})
        self.assertEqual(name, FALLBACK_CATEGORY)
        self.assertIsNone(create)

    def test_no_section_at_all(self):
        self.assertEqual(resolve_category(None, {}, {"Sonstiges"}),
                         (FALLBACK_CATEGORY, None))


class FirstPairingTests(unittest.TestCase):
    """A pair without common history must not record a difference as 'never
    changed' - that is how bought items ended up open in Yuvomi."""

    def test_bring_wins_when_the_states_differ(self):
        self.assertEqual(first_pairing(1, 0), {"is_checked": True})
        self.assertEqual(first_pairing(0, 1), {"is_checked": False})

    def test_no_action_when_they_agree(self):
        self.assertIsNone(first_pairing(1, 1))
        self.assertIsNone(first_pairing(0, 0))


class UnpairedTests(unittest.TestCase):
    def test_paired_sides_need_no_join(self):
        self.assertFalse(unpaired([row()], [b()], [y()]))

    def test_lost_state_needs_a_join(self):
        self.assertTrue(unpaired([], [b()], [y()]))

    def test_new_item_on_either_side_needs_a_join(self):
        self.assertTrue(unpaired([row()], [b(uuid="u2", name="Brot")], [y()]))
        self.assertTrue(unpaired([row()], [b()], [y(item_id=11, name="Brot")]))


class SkipReasonTests(unittest.TestCase):
    """Every reason that saves Yuvomi requests (budget: 300/min per IP)."""

    def kw(self, **over):
        base = {"off": False, "mapping": {"yuvomi_list_id": 1},
                "y_list_exists": True, "y_name_exists": True,
                "b_items": [b()], "y_count": 3, "rows": [row()]}
        base.update(over)
        return base

    def test_syncs_by_default(self):
        self.assertIsNone(skip_reason(**self.kw()))

    def test_unsubscribed_list_is_skipped(self):
        self.assertEqual(skip_reason(**self.kw(off=True, y_name_exists=False)),
                         "off")

    def test_recreated_name_resubscribes(self):
        self.assertIsNone(skip_reason(**self.kw(off=True, y_name_exists=True)))

    def test_deleted_yuvomi_list_is_unsubscribed(self):
        self.assertEqual(
            skip_reason(**self.kw(y_list_exists=False, y_name_exists=False)),
            "deleted")

    def test_replaced_yuvomi_list_is_rematched(self):
        self.assertEqual(
            skip_reason(**self.kw(y_list_exists=False, y_name_exists=True)),
            "remap")

    def test_empty_bring_list_gets_no_twin(self):
        self.assertEqual(
            skip_reason(**self.kw(mapping=None, y_list_exists=False,
                                  y_name_exists=False, b_items=[], y_count=0,
                                  rows=[])),
            "empty")

    def test_idle_list_needs_no_requests(self):
        self.assertEqual(skip_reason(**self.kw(b_items=[], y_count=0, rows=[])),
                         "idle")

    def test_item_count_alone_keeps_the_list_alive(self):
        self.assertIsNone(skip_reason(**self.kw(b_items=[], rows=[])))

    def test_stale_rows_are_still_cleaned_up(self):
        self.assertIsNone(
            skip_reason(**self.kw(b_items=[], y_count=0, rows=[row()])))


class PlanTests(unittest.TestCase):
    def test_new_bring_item_is_created_in_yuvomi_with_category(self):
        actions = plan(
            bring=[b(name="Milch")], cats={"Sonstiges"},
            cfg={"category_map": {"Milch & Käse": "Milchprodukte"}},
            sections={"milch": "Milch & Käse"})
        self.assertEqual(kinds(actions), ["cat_create", "y_create"])
        self.assertEqual(actions[1]["category"], "Milchprodukte")
        self.assertEqual(actions[1]["quantity"], "1 L")

    def test_new_bring_item_keeps_its_bought_state(self):
        # Root cause of "bought in Bring, open in Yuvomi": the create call
        # has to carry the state, otherwise the pairing record freezes 0/1.
        bought = plan(bring=[b(checked=1)], cats={"Sonstiges"}, sections={})
        self.assertEqual(kinds(bought), ["y_create"])
        self.assertEqual(bought[0]["checked"], 1)
        open_ = plan(bring=[b(checked=0)], cats={"Sonstiges"}, sections={})
        self.assertEqual(open_[0]["checked"], 0)

    def test_new_yuvomi_item_is_pushed_to_bring(self):
        attempts = {}
        actions = plan(yuvomi=[y(name="Brot")], attempts=attempts)
        self.assertEqual(kinds(actions), ["b_add"])
        self.assertEqual(actions[0]["item_uuid"], client_uuid(1, 10))
        self.assertEqual(attempts[(1, 10)], 1)

    def test_gives_up_after_repeated_push_failures(self):
        attempts = {(1, 10): MAX_PUSH_ATTEMPTS}
        self.assertEqual(plan(yuvomi=[y()], attempts=attempts), [])

    def test_free_items_with_equal_names_are_joined_not_created(self):
        # Same name on both sides without state: pairing happens in reconcile.
        self.assertEqual(plan(bring=[b(name="Milch")], yuvomi=[y(name="Milch")]), [])

    def test_bring_check_mirrors_into_yuvomi(self):
        actions = plan(bring=[b(checked=1)], yuvomi=[y()], rows=[row()])
        self.assertEqual(kinds(actions), ["y_patch"])
        self.assertEqual(actions[0]["fields"], {"is_checked": True})

    def test_yuvomi_check_mirrors_into_bring(self):
        actions = plan(bring=[b()], yuvomi=[y(checked=1)], rows=[row()])
        self.assertEqual(kinds(actions), ["b_complete"])

    def test_uncheck_mirrors_into_bring_with_yuvomi_text(self):
        state = row(b_checked=1, y_checked=1, b_name="Milch alt", y_name="Milch neu")
        actions = plan(bring=[b(name="Milch alt", checked=1)],
                       yuvomi=[y(checked=0, name="Milch neu")], rows=[state])
        self.assertEqual(kinds(actions), ["b_reopen"])
        self.assertEqual(actions[0]["name"], "Milch neu")

    def test_name_conflict_yuvomi_wins(self):
        actions = plan(bring=[b(name="Milch frisch")],
                       yuvomi=[y(name="H-Milch")], rows=[row()])
        self.assertEqual(kinds(actions), ["b_update"])
        self.assertEqual(actions[0]["name"], "H-Milch")   # Yuvomi's name pushed

    def test_bring_rename_reaches_yuvomi_when_yuvomi_is_untouched(self):
        # The other direction of the name rule: only Bring changed.
        actions = plan(bring=[b(name="Milch neu")], yuvomi=[y(name="Milch")],
                       rows=[row()])
        self.assertEqual(kinds(actions), ["y_patch"])
        self.assertEqual(actions[0]["fields"], {"name": "Milch neu"})

    def test_yuvomi_quantity_change_is_pushed_to_bring(self):
        actions = plan(bring=[b(spec="1 L")], yuvomi=[y(quantity="6 Pack")],
                       rows=[row()])
        self.assertEqual(kinds(actions), ["b_update"])
        self.assertEqual(actions[0]["spec"], "6 Pack")

    def test_quantity_conflict_bring_wins_when_yuvomi_untouched(self):
        actions = plan(bring=[b(spec="6 Pack")], yuvomi=[y(quantity="1 L")],
                       rows=[row()])
        self.assertEqual(kinds(actions), ["y_patch"])
        self.assertEqual(actions[0]["fields"], {"quantity": "6 Pack"})

    def test_checked_conflict_bring_wins(self):
        # Both sides flipped since the last sync: Bring says bought.
        state = row(b_checked=0, y_checked=1)
        actions = plan(bring=[b(checked=1)], yuvomi=[y(checked=0)], rows=[state])
        self.assertEqual(kinds(actions), ["y_patch"])
        self.assertEqual(actions[0]["fields"], {"is_checked": True})

    def test_rename_while_bought_is_deferred(self):
        actions = plan(bring=[b(checked=1)], yuvomi=[y(name="Neu", checked=1)],
                       rows=[row(b_checked=1, y_checked=1)])
        self.assertEqual(actions, [])

    def test_pair_marked_gone_but_present_again_is_left_alone(self):
        # A push that has failed before: Bring has the item again while
        # Yuvomi still holds the bought copy - no fight, the next
        # reconcile records reality.
        actions = plan(bring=[b(checked=1)], yuvomi=[y(checked=1)],
                       rows=[row(gone=1, b_checked=1, y_checked=1)])
        self.assertEqual(actions, [])

    def test_missing_once_only_waits(self):
        actions = plan(bring=[], yuvomi=[y()], rows=[row(missing=0)])
        self.assertEqual(kinds(actions), ["row_set"])
        self.assertEqual(actions[0]["fields"], {"missing": 1})

    def test_confirmed_missing_open_item_is_deleted(self):
        actions = plan(bring=[], yuvomi=[y()], rows=[row(missing=1)])
        self.assertEqual(kinds(actions), ["y_delete", "row_drop"])

    def test_confirmed_missing_bought_item_is_deleted(self):
        # Bring decides what still exists - bought items are no longer exempt.
        actions = plan(bring=[], yuvomi=[y(checked=1)],
                       rows=[row(missing=1, b_checked=1, y_checked=1)])
        self.assertEqual(kinds(actions), ["y_delete", "row_drop"])

    def test_failed_push_is_retried_after_the_waiting_period(self):
        actions = plan(bring=[], yuvomi=[y(checked=1)],
                       rows=[row(gone=1, b_checked=1, y_checked=1,
                                 missing=1, attempts=0)])
        self.assertEqual(kinds(actions), ["b_add", "row_set"])
        self.assertEqual(actions[1]["fields"]["attempts"], 1)
        self.assertEqual(actions[1]["fields"]["pending"], 1)

    def test_failed_push_gives_up_instead_of_deleting(self):
        actions = plan(bring=[], yuvomi=[y()],
                       rows=[row(gone=1, attempts=MAX_PUSH_ATTEMPTS, missing=1)])
        self.assertEqual(actions, [])

    def test_push_that_never_appears_is_kept_not_deleted(self):
        # Our own push did not show up - deleting would destroy data we caused.
        actions = plan(bring=[], yuvomi=[y()], rows=[row(pending=1, missing=1)])
        self.assertEqual(kinds(actions), ["row_set"])
        self.assertEqual(actions[0]["fields"],
                         {"gone": 1, "pending": 0, "missing": 0})

    def test_deleted_in_yuvomi_is_removed_from_bring(self):
        actions = plan(bring=[b()], yuvomi=[], rows=[row()])
        self.assertEqual(kinds(actions), ["b_remove", "row_drop"])

    def test_deleted_on_both_sides_only_drops_the_row(self):
        actions = plan(bring=[], yuvomi=[], rows=[row()])
        self.assertEqual(kinds(actions), ["row_drop"])


# ---------------------------------------------------------------- state store

class StateTests(unittest.TestCase):
    """The SQLite store is the only thing that remembers a pair."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = State(Path(tmp.name) / "data" / "bridge.db")

    def test_list_mapping_round_trip(self):
        self.assertEqual(self.state.list_map(), {})
        self.state.map_list({"bring_uuid": "L1", "bring_name": "Lebensmittel"},
                            {"id": 7, "name": "Einkauf"})
        got = self.state.list_map()["L1"]
        self.assertEqual((got["yuvomi_list_id"], got["yuvomi_name"]), (7, "Einkauf"))
        # a replaced Yuvomi list overwrites the mapping in place
        self.state.map_list({"bring_uuid": "L1", "bring_name": "Lebensmittel"},
                            {"id": 9, "name": "Neu"})
        self.assertEqual(self.state.list_map()["L1"]["yuvomi_list_id"], 9)

    def test_off_switch_round_trip(self):
        self.assertFalse(self.state.is_off("L1"))
        self.state.set_off("L1", "yuvomi list deleted")
        self.assertTrue(self.state.is_off("L1"))
        self.state.set_off("L1", "another reason")      # last reason wins
        self.state.clear_off("L1")
        self.assertFalse(self.state.is_off("L1"))

    def test_rows_round_trip_and_clear(self):
        self.state.insert_row("L", b(), y())
        rows = self.state.rows("L")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["y_qty"], "1 L")
        self.state.row_set("u1", {"missing": 2, "gone": 1})
        self.assertEqual((self.state.rows("L")[0]["missing"],
                          self.state.rows("L")[0]["gone"]), (2, 1))
        self.state.row_set("u1", {})                    # no fields – no write
        self.state.clear_items("L")
        self.assertEqual(self.state.rows("L"), [])

    def test_row_drop_removes_exactly_one_pair(self):
        self.state.insert_row("L", b(), y())
        self.state.insert_row("L", b(uuid="u2", name="Brot"),
                              y(item_id=11, name="Brot"))
        self.state.row_drop("u1")
        self.assertEqual([r["bring_uuid"] for r in self.state.rows("L")], ["u2"])

    def test_a_pair_can_only_be_created_once(self):
        # yuvomi_item_id is UNIQUE: a second join for the same item is dropped.
        self.state.insert_row("L", b(), y())
        self.state.insert_row("L", b(uuid="u9"), y())
        self.assertEqual(len(self.state.rows("L")), 1)


# ---------------------------------------------------------------- REST client

class YuvomiClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, *responses):
        session = FakeSession(responses)
        return Yuvomi(session, "http://yuvomi:3000/", "t0k3n"), session

    async def test_url_header_and_data_unwrapped(self):
        y, session = self.client(FakeResponse(payload={"data": [{"id": 1}]}))
        self.assertEqual(await y.lists(), [{"id": 1}])
        call = session.requests[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "http://yuvomi:3000/api/v1/shopping")
        self.assertEqual(call["headers"], {"Authorization": "Bearer t0k3n"})

    async def test_error_status_carries_status_and_path(self):
        y, _ = self.client(FakeResponse(status=500, text="kaputt"))
        with self.assertRaises(YuvomiError) as ctx:
            await y.items(1)
        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("GET /shopping/1/items -> 500", str(ctx.exception))

    async def test_empty_body_is_an_empty_answer(self):
        y, _ = self.client(FakeResponse(text=""))
        self.assertEqual(await y._call("DELETE", "/shopping/items/3"), {})

    async def test_list_and_category_reads_unwrap_their_data(self):
        y, _ = self.client(FakeResponse(payload={"data": {"id": 7}}),
                           FakeResponse(payload={"data": [{"name": "Getränke"}]}))
        self.assertEqual(await y.create_list("Neu"), {"id": 7})
        self.assertEqual(await y.categories(), [{"name": "Getränke"}])

    async def test_create_item_only_sends_what_it_has(self):
        y, session = self.client(FakeResponse(payload={"data": {"id": 5}}),
                                 FakeResponse(payload={"data": {"id": 6}}))
        await y.create_item(1, "Milch", "", "", 0)
        self.assertEqual(session.requests[0]["json"], {"name": "Milch"})
        await y.create_item(1, "Milch", "1 L", "Milchprodukte", 1)
        self.assertEqual(session.requests[1]["json"],
                         {"name": "Milch", "quantity": "1 L",
                          "category": "Milchprodukte", "is_checked": True})

    async def test_only_a_conflict_is_tolerated_when_creating_a_category(self):
        clash, _ = self.client(FakeResponse(status=409, text="{}"))
        await clash.create_category("Getränke")          # must not raise
        broken, _ = self.client(FakeResponse(status=500, text="kaputt"))
        with self.assertRaises(YuvomiError):
            await broken.create_category("Getränke")

    async def test_patch_and_delete_use_the_item_path(self):
        y, session = self.client(FakeResponse(payload={"data": {"id": 3}}),
                                 FakeResponse())
        self.assertEqual(await y.patch_item(3, {"name": "Neu"}), {"id": 3})
        await y.delete_item(3)
        self.assertEqual([c["method"] for c in session.requests],
                         ["PATCH", "DELETE"])
        self.assertEqual(session.requests[0]["url"],
                         "http://yuvomi:3000/api/v1/shopping/items/3")


class CatalogFetcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_mapping_is_cached_per_locale(self):
        session = FakeSession([FakeResponse(payload=CATALOGUE)])
        catalog = Catalog(session)
        first = await catalog.sections_for("fr-FR")
        second = await catalog.sections_for("fr-FR")
        self.assertIs(first, second)                     # one request per start
        self.assertEqual(len(session.gets), 1)

    async def test_unknown_locale_falls_back_to_the_default(self):
        session = FakeSession([FakeResponse(status=404),
                               FakeResponse(payload=CATALOGUE)])
        mapping = await Catalog(session).sections_for("zz-ZZ")
        self.assertEqual(mapping[norm("Milch")], "Milch & Käse")
        self.assertIn(DEFAULT_LOCALE, session.gets[1])

    async def test_unreadable_catalogue_yields_no_categories(self):
        session = FakeSession([FakeResponse(status=404), FakeResponse(status=500)])
        self.assertEqual(await Catalog(session).sections_for("fr-FR"), {})

    async def test_network_failure_is_survived(self):
        session = FakeSession([ConnectionError("down"), ConnectionError("down")])
        self.assertEqual(await Catalog(session).sections_for("fr-FR"), {})


# ---------------------------------------------------------------- adapters

class BringItemsTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_and_bought_items_become_one_list(self):
        bring = Recorder(get_list=bring_list(purchase=[b(uuid="u1")],
                                             recently=[b(uuid="u2", name="Brot")]))
        items = await bring_items(bring, "L")
        self.assertEqual([(i["uuid"], i["checked"]) for i in items],
                         [("u1", 0), ("u2", 1)])


class ReconcileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = State(Path(tmp.name) / "bridge.db")

    async def test_free_items_are_joined_and_bring_decides_the_first_state(self):
        # Bought items live in Bring's "recently" - that is where checked=1
        # comes from.
        bring = Recorder(get_list=bring_list(recently=[b(checked=1)]))
        y_client = Recorder(items=[y(checked=0)])
        await reconcile(bring, y_client, self.state, "L", 1)
        rows = self.state.rows("L")
        self.assertEqual(len(rows), 1)
        self.assertEqual(called(y_client, "patch_item")[0][1],
                         (10, {"is_checked": True}))
        # recorded after the patch, so no phantom difference survives
        self.assertEqual((rows[0]["b_checked"], rows[0]["y_checked"]), (1, 1))

    async def test_current_values_overwrite_the_baseline(self):
        self.state.insert_row("L", b(), y())
        self.state.row_set("u1", {"missing": 2, "gone": 1, "pending": 1,
                                  "attempts": 3})
        bring = Recorder(get_list=bring_list(recently=[b(spec="2 L", checked=1)]))
        y_client = Recorder(items=[y(name="Milch neu", checked=1)])
        await reconcile(bring, y_client, self.state, "L", 1)
        row = self.state.rows("L")[0]
        self.assertEqual((row["b_spec"], row["y_name"], row["y_checked"]),
                         ("2 L", "Milch neu", 1))
        self.assertEqual((row["missing"], row["gone"], row["pending"],
                          row["attempts"]), (0, 0, 0, 0))

    async def test_a_missing_bring_side_leaves_the_counters_alone(self):
        # Deletion is the planner's decision, not the reconciler's.
        self.state.insert_row("L", b(), y())
        self.state.row_set("u1", {"missing": 1})
        bring = Recorder(get_list=bring_list())
        y_client = Recorder(items=[y(name="Neu")])
        await reconcile(bring, y_client, self.state, "L", 1)
        row = self.state.rows("L")[0]
        self.assertEqual(row["y_name"], "Neu")           # Yuvomi side recorded
        self.assertEqual(row["missing"], 1)              # counters untouched
        self.assertEqual(row["b_name"], "Milch")         # still the old truth

    async def test_a_deleted_yuvomi_item_keeps_the_row(self):
        self.state.insert_row("L", b(), y())
        before = self.state.rows("L")[0]
        bring = Recorder(get_list=bring_list(purchase=[b()]))
        await reconcile(bring, Recorder(items=[]), self.state, "L", 1)
        self.assertEqual(self.state.rows("L")[0], before)


class ExecuteTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_action_reaches_the_right_object(self):
        y_client, bring, state = Recorder(), Recorder(), Recorder()
        actions = [
            {"kind": "cat_create", "name": "Getränke"},
            {"kind": "y_create", "list_id": 1, "name": "Milch",
             "quantity": "1 L", "category": "Milchprodukte", "checked": 1},
            {"kind": "y_patch", "item_id": 10, "fields": {"name": "Neu"}},
            {"kind": "y_delete", "item_id": 11},
            {"kind": "b_add", "list_uuid": "L", "name": "Milch", "spec": "1 L",
             "item_uuid": "u1"},
            {"kind": "b_update", "list_uuid": "L", "name": "Neu", "spec": "1 L",
             "item_uuid": "u1"},
            {"kind": "b_complete", "list_uuid": "L", "name": "Milch",
             "spec": "1 L", "item_uuid": "u1"},
            {"kind": "b_reopen", "list_uuid": "L", "name": "Milch",
             "spec": "1 L", "item_uuid": "u1"},
            {"kind": "b_remove", "list_uuid": "L", "name": "Milch",
             "spec": "1 L", "item_uuid": "u1"},
            {"kind": "row_set", "bring_uuid": "u1", "fields": {"missing": 1}},
            {"kind": "row_drop", "bring_uuid": "u1"},
        ]
        await execute(actions, y_client, bring, state)
        self.assertEqual([c[0] for c in y_client.calls],
                         ["create_category", "create_item", "patch_item",
                          "delete_item"])
        self.assertEqual([c[0] for c in bring.calls],
                         ["save_item", "update_item", "complete_item",
                          "save_item", "remove_item"])
        self.assertEqual([c[0] for c in state.calls], ["row_set", "row_drop"])
        # arguments pass through unchanged
        self.assertEqual(y_client.calls[1][1], (1, "Milch", "1 L",
                                               "Milchprodukte", 1))
        self.assertEqual(bring.calls[0][2], {"item_uuid": "u1"})

    async def test_a_failing_action_does_not_stop_the_queue(self):
        y_client = Recorder(patch_item=RuntimeError("kaputt"))
        await execute([{"kind": "y_patch", "item_id": 1, "fields": {}},
                       {"kind": "y_delete", "item_id": 2}],
                      y_client, Recorder(), Recorder())
        self.assertEqual([c[0] for c in y_client.calls],
                         ["patch_item", "delete_item"])

    async def test_unknown_action_is_reported_not_raised(self):
        await execute([{"kind": "mystery"}], Recorder(), Recorder(), Recorder())


# ---------------------------------------------------------------- orchestration

class BridgeSyncTests(unittest.IsolatedAsyncioTestCase):
    def build(self, *, mapped=True, y_lists=None, purchase=(), dry_run=False,
              off=False, sections=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = State(Path(tmp.name) / "bridge.db")
        if mapped:
            state.map_list({"bring_uuid": "L1", "bring_name": "Lebensmittel"},
                           {"id": 7, "name": "Lebensmittel"})
        if off:
            state.set_off("L1", "test")
        bring = Recorder(get_list=bring_list(purchase=purchase))
        bring.uuid = "session"
        bring.user_list_settings = {}
        bring.user_locale = "de-DE"
        y_client = Recorder(
            lists=[{"id": 7, "name": "Lebensmittel", "item_total": 0}],
            items=[], categories=[{"name": "Sonstiges"}],
            create_list={"id": 7, "name": "Lebensmittel"})
        # y_lists=None means "the Yuvomi twin exists under the same name"
        twin = [{"id": 7, "name": "Lebensmittel", "item_total": 0}]
        bridge_obj = Bridge(bring=bring, y=y_client,
                            catalog=Recorder(sections_for=sections or {}),
                            state=state, cfg={}, dry_run=dry_run)
        return SimpleNamespace(bridge=bridge_obj, bring=bring, y=y_client,
                               state=state,
                               y_lists=list(twin if y_lists is None else y_lists),
                               bl={"listUuid": "L1", "name": "Lebensmittel"})

    async def test_new_bring_list_gets_a_yuvomi_twin(self):
        env = self.build(mapped=False, y_lists=[], purchase=[b()])
        await env.bridge.sync_list(env.bl, env.y_lists, {}, {"Sonstiges"})
        self.assertEqual(len(called(env.y, "create_list")), 1)
        self.assertEqual(env.state.list_map()["L1"]["yuvomi_list_id"], 7)
        self.assertEqual(called(env.y, "create_item")[0][1],
                         (7, "Milch", "1 L", FALLBACK_CATEGORY, 0))

    async def test_unsubscribed_list_costs_no_yuvomi_request(self):
        env = self.build(off=True, y_lists=[], purchase=[b()])
        await env.bridge.sync_list(env.bl, env.y_lists, {}, set())
        self.assertEqual(env.y.calls, [])

    async def test_deleted_yuvomi_list_unsubscribes_and_clears_the_pairs(self):
        env = self.build(y_lists=[], purchase=[b()])
        env.state.insert_row("L1", b(), y())
        await env.bridge.sync_list(env.bl, env.y_lists, {}, set())
        self.assertTrue(env.state.is_off("L1"))
        self.assertEqual(env.state.rows("L1"), [])
        self.assertEqual(called(env.y, "create_list"), [])

    async def test_resubscribed_list_starts_over_from_bring(self):
        env = self.build(off=True, purchase=[b()])
        env.state.insert_row("L1", b(), y())
        await env.bridge.sync_list(env.bl, env.y_lists, {7: 1}, set())
        self.assertFalse(env.state.is_off("L1"))
        self.assertTrue(env.state.list_map())            # twin reused, not new
        self.assertEqual(env.state.rows("L1"), [])       # old pairs discarded

    async def test_dry_run_only_reports(self):
        env = self.build(mapped=False, y_lists=[], purchase=[b()], dry_run=True)
        await env.bridge.sync_list(env.bl, env.y_lists, {}, set())
        self.assertEqual(called(env.y, "create_list"), [])
        self.assertEqual(called(env.y, "create_item"), [])
        self.assertEqual(env.state.list_map(), {})

    async def test_dry_run_reports_actions_without_running_them(self):
        env = self.build(dry_run=True, purchase=[b()])
        await env.bridge.sync_list(env.bl, env.y_lists, {7: 0}, set())
        self.assertEqual(called(env.y, "create_item"), [])   # nothing executed
        self.assertEqual(len(called(env.y, "items")), 1)     # no reconcile

    async def test_a_replaced_yuvomi_list_is_rematched_by_name(self):
        env = self.build(y_lists=[{"id": 9, "name": "Lebensmittel",
                                   "item_total": 0}])
        await env.bridge.sync_list(env.bl, env.y_lists, {9: 0}, set())
        self.assertEqual(env.state.list_map()["L1"]["yuvomi_list_id"], 9)
        self.assertEqual(called(env.y, "create_list"), [])   # name matched it

    async def test_a_new_category_drops_the_cached_category_list(self):
        env = self.build(sections={"milch": "Milch & Käse"}, purchase=[b()])
        env.bridge._cat_cache = (time.monotonic(), {"alt"})
        await env.bridge.sync_list(env.bl, env.y_lists, {7: 0}, {"Sonstiges"})
        self.assertEqual(len(called(env.y, "create_category")), 1)
        self.assertIsNone(env.bridge._cat_cache)

    async def test_actions_run_and_are_reconciled_afterwards(self):
        env = self.build(purchase=[b()])
        await env.bridge.sync_list(env.bl, env.y_lists, {7: 0}, set())
        self.assertEqual(len(called(env.y, "create_item")), 1)
        # plan reads the items, reconcile reads them again
        self.assertGreaterEqual(len(called(env.y, "items")), 2)

    async def test_categories_are_fetched_once_within_ten_minutes(self):
        env = self.build()
        await env.bridge.yuvomi_categories()
        await env.bridge.yuvomi_categories()
        self.assertEqual(len(called(env.y, "categories")), 1)
        env.bridge._cat_cache = (time.monotonic() - 601, {"alt"})
        self.assertEqual(await env.bridge.yuvomi_categories(), {"Sonstiges"})
        self.assertEqual(len(called(env.y, "categories")), 2)


class CycleTests(unittest.IsolatedAsyncioTestCase):
    """The ten-second loop: what stops it, what it ignores."""

    def build(self, *, items, cfg=None, extra_lists=()):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = State(Path(tmp.name) / "bridge.db")
        state.map_list({"bring_uuid": "L1", "bring_name": "Lebensmittel"},
                       {"id": 7, "name": "Lebensmittel"})
        lists = [SimpleNamespace(listUuid="L1", name="Lebensmittel")]
        lists += [SimpleNamespace(listUuid=f"L-{n}", name=n) for n in extra_lists]
        bring = Recorder(get_list=bring_list(purchase=[b()]),
                         load_lists=SimpleNamespace(lists=lists))
        bring.uuid = "session"
        bring.user_list_settings = {}
        bring.user_locale = "de-DE"
        y_client = Recorder(
            lists=[{"id": 7, "name": "Lebensmittel", "item_total": 0}],
            items=items, categories=[{"name": "Sonstiges"}])
        bridge_obj = Bridge(bring=bring, y=y_client,
                            catalog=Recorder(sections_for={}), state=state,
                            cfg=cfg or {})
        return SimpleNamespace(bridge=bridge_obj, bring=bring, y=y_client,
                               state=state)

    async def test_rate_limit_aborts_the_cycle(self):
        # Budget: 300 requests/minute - on 429 the loop stops and sleeps.
        env = self.build(items=YuvomiError(429, "budget"))
        with self.assertRaises(YuvomiError):
            await env.bridge.cycle()

    async def test_a_broken_list_does_not_stop_the_loop(self):
        env = self.build(items=RuntimeError("kaputt"))
        env.bring.uuid = None                     # forces the login branch
        await env.bridge.cycle()                  # logged, not raised
        self.assertEqual(len(called(env.bring, "login")), 1)

    async def test_a_yuvomi_error_that_is_not_a_rate_limit_is_logged(self):
        env = self.build(items=YuvomiError(500, "boom"))
        await env.bridge.cycle()                  # logged, loop continues
        self.assertEqual(len(called(env.y, "items")), 1)

    async def test_skipped_lists_are_never_touched(self):
        env = self.build(items=[], cfg={"skip_lists": ["Party"]},
                         extra_lists=["Party"])
        await env.bridge.cycle()
        self.assertEqual(list(env.state.list_map()), ["L1"])


class MainTests(unittest.TestCase):
    """main() is argparse plus a call into run().

    run() needs aiohttp and bring-api, so it is stubbed here and exercised for
    real by the container smoke test (``python bridge.py --help`` in the image).
    """

    def run_main(self, *argv):
        seen = {}

        async def fake_run(args):
            seen["args"] = args
            return 0

        with mock.patch.object(bridge, "run", fake_run), \
                mock.patch.object(sys, "argv", ["bridge.py", *argv]):
            code = bridge.main()
        return code, seen["args"]

    def test_defaults(self):
        code, args = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(args.config, str(bridge.HERE / "config.json"))
        self.assertFalse(args.once)
        self.assertFalse(args.dry_run)

    def test_flags_reach_the_loop(self):
        _, args = self.run_main("--once", "--dry-run", "--config", "/tmp/other.json")
        self.assertTrue(args.once)
        self.assertTrue(args.dry_run)
        self.assertEqual(args.config, "/tmp/other.json")


if __name__ == "__main__":
    unittest.main()
