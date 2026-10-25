"""Offline self-check for the bridge - no network, no credentials.

Run with:  python3 -m unittest -v test_bridge
"""

import unittest

import bridge
from bridge import (FALLBACK_CATEGORY, MAX_PUSH_ATTEMPTS, client_uuid,
                    first_pairing, norm, parse_catalog, plan_list,
                    resolve_category, skip_reason, unpaired)

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


if __name__ == "__main__":
    unittest.main()
