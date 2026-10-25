"""Offline self-check for the bridge - no network, no credentials.

Run with:  python3 -m unittest -v test_bridge
"""

import unittest

import bridge
from bridge import (FALLBACK_CATEGORY, MAX_PUSH_ATTEMPTS, client_uuid,
                    norm, parse_catalog, plan_list, resolve_category)

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


class CatalogueTests(unittest.TestCase):
    def test_parses_names_and_ids(self):
        mapping = parse_catalog(CATALOGUE)
        self.assertEqual(mapping[norm("Milch")], "Milch & Käse")
        self.assertEqual(mapping[norm("Milk")], "Milch & Käse")
        self.assertEqual(mapping[norm("Wasser")], "Getränke")

    def test_empty_payload_is_safe(self):
        self.assertEqual(parse_catalog(None), {})


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


class PlanTests(unittest.TestCase):
    def test_new_bring_item_is_created_in_yuvomi_with_category(self):
        actions = plan(
            bring=[b(name="Milch")], cats={"Sonstiges"},
            cfg={"category_map": {"Milch & Käse": "Milchprodukte"}},
            sections={"milch": "Milch & Käse"})
        self.assertEqual(kinds(actions), ["cat_create", "y_create"])
        self.assertEqual(actions[1]["category"], "Milchprodukte")
        self.assertEqual(actions[1]["quantity"], "1 L")

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

    def test_missing_once_only_waits(self):
        actions = plan(bring=[], yuvomi=[y()], rows=[row(missing=0)])
        self.assertEqual(kinds(actions), ["row_set"])
        self.assertEqual(actions[0]["fields"], {"missing": 1})

    def test_confirmed_missing_open_item_is_deleted(self):
        actions = plan(bring=[], yuvomi=[y()], rows=[row(missing=1)])
        self.assertEqual(kinds(actions), ["y_delete", "row_drop"])

    def test_confirmed_missing_bought_item_is_kept(self):
        actions = plan(bring=[], yuvomi=[y(checked=1)],
                       rows=[row(missing=1, b_checked=1, y_checked=1)])
        self.assertEqual(kinds(actions), ["row_set"])
        self.assertEqual(actions[0]["fields"], {"gone": 1, "missing": 0})

    def test_gone_item_is_repushed_when_unchecked(self):
        actions = plan(bring=[], yuvomi=[y(checked=0)],
                       rows=[row(gone=1, b_checked=1, y_checked=1, attempts=0)])
        self.assertEqual(kinds(actions), ["b_add", "row_set"])
        self.assertEqual(actions[1]["fields"]["attempts"], 1)
        self.assertEqual(actions[1]["fields"]["pending"], 1)

    def test_gone_item_stays_put_while_bought(self):
        actions = plan(bring=[], yuvomi=[y(checked=1)],
                       rows=[row(gone=1, b_checked=1, y_checked=1)])
        self.assertEqual(actions, [])

    def test_unchecked_push_failure_gives_up(self):
        actions = plan(bring=[], yuvomi=[y()],
                       rows=[row(gone=1, attempts=MAX_PUSH_ATTEMPTS)])
        self.assertEqual(actions, [])

    def test_deleted_in_yuvomi_is_removed_from_bring(self):
        actions = plan(bring=[b()], yuvomi=[], rows=[row()])
        self.assertEqual(kinds(actions), ["b_remove", "row_drop"])

    def test_deleted_on_both_sides_only_drops_the_row(self):
        actions = plan(bring=[], yuvomi=[], rows=[row()])
        self.assertEqual(kinds(actions), ["row_drop"])


if __name__ == "__main__":
    unittest.main()
