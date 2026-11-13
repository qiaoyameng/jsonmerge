# vim:ts=4 sw=4 expandtab softtabstop=4
import copy
import unittest

import jsonmerge
from jsonmerge import (
    MISSING,
    ThreeWayMerger,
    merge_three_way,
    three_way_merge,
)
from jsonmerge.strategies import Strategy
from jsonmerge.threeway_exceptions import (
    ThreeWayConflictResolutionError,
    ThreeWayInputError,
    ThreeWayStrategyError,
)


def conflicts_by_path(result):
    return dict((conflict.path, conflict) for conflict in result.conflicts)


class TestThreeWayLeafMerge(unittest.TestCase):

    def test_independent_fields_combine(self):
        ancestor = {"a": 1, "b": 2}
        left = {"a": 1, "b": 2, "c": 3}
        right = {"a": 1, "b": 2, "d": 4}

        result = three_way_merge(ancestor, left, right)

        self.assertFalse(result.has_conflicts)
        self.assertEqual(result.merged, {"a": 1, "b": 2, "c": 3, "d": 4})

    def test_same_modification_kept_once(self):
        ancestor = {"k": 1}
        left = {"k": 9}
        right = {"k": 9}

        result = three_way_merge(ancestor, left, right)

        self.assertFalse(result.has_conflicts)
        self.assertEqual(result.merged, {"k": 9})

    def test_same_deletion_kept_once(self):
        ancestor = {"k": 1, "keep": 2}
        left = {"keep": 2}
        right = {"keep": 2}

        result = three_way_merge(ancestor, left, right)

        self.assertFalse(result.has_conflicts)
        self.assertEqual(result.merged, {"keep": 2})

    def test_different_modification_conflict_has_three_values(self):
        ancestor = {"k": 1}
        left = {"k": 8}
        right = {"k": 9}

        result = three_way_merge(ancestor, left, right)

        self.assertTrue(result.has_conflicts)
        conflict = result.conflicts[0]
        self.assertEqual(conflict.path, "/k")
        self.assertEqual(conflict.conflict_type, "different_modification")
        self.assertEqual(conflict.ancestor, 1)
        self.assertEqual(conflict.left, 8)
        self.assertEqual(conflict.right, 9)
        self.assertNotIn("k", result.merged)

    def test_delete_vs_modify_conflict(self):
        ancestor = {"k": 1}

        result_left_delete = three_way_merge(
            ancestor, {}, {"k": 2})
        result_right_delete = three_way_merge(
            ancestor, {"k": 2}, {})

        for result in (result_left_delete, result_right_delete):
            self.assertEqual(len(result.conflicts), 1)
            conflict = result.conflicts[0]
            self.assertEqual(conflict.conflict_type, "delete_modify")
            self.assertEqual(conflict.ancestor, 1)
            self.assertNotIn("k", result.merged)

    def test_one_sided_delete_is_accepted(self):
        ancestor = {"k": 1, "other": 2}

        result = three_way_merge(
            ancestor, {"other": 2}, {"k": 1, "other": 2})

        self.assertFalse(result.has_conflicts)
        self.assertEqual(result.merged, {"other": 2})

    def test_one_sided_addition_is_accepted(self):
        result = three_way_merge({}, {"k": 1}, {})
        self.assertFalse(result.has_conflicts)
        self.assertEqual(result.merged, {"k": 1})

        result_swapped = three_way_merge({}, {}, {"k": 1})
        self.assertEqual(result_swapped.merged, {"k": 1})

    def test_distinct_additions_conflict(self):
        result = three_way_merge(
            {}, {"k": 1}, {"k": 2})

        self.assertEqual(len(result.conflicts), 1)
        self.assertEqual(result.conflicts[0].conflict_type, "add_add")

    def test_explicit_null_is_a_value_not_a_deletion(self):
        result = three_way_merge(
            {"k": 1}, {"k": None}, {"k": None})
        self.assertFalse(result.has_conflicts)
        self.assertIn("k", result.merged)
        self.assertIsNone(result.merged["k"])

        conflict_result = three_way_merge(
            {"k": 1}, {"k": None}, {"k": 2})
        self.assertEqual(
            conflict_result.conflicts[0].conflict_type,
            "different_modification")

    def test_present_flags_distinguish_null_from_absence(self):
        result = three_way_merge(
            {"k": 1}, {}, {"k": 2})
        record = result.conflicts[0].as_dict()
        self.assertFalse(record["left_present"])
        self.assertTrue(record["right_present"])
        self.assertTrue(record["ancestor_present"])


class TestThreeWayNestedObjects(unittest.TestCase):

    def test_nested_disjoint_edits_merge(self):
        ancestor = {"o": {"a": 1, "b": 2}}
        left = {"o": {"a": 9, "b": 2}}
        right = {"o": {"a": 1, "b": 8}}

        result = three_way_merge(ancestor, left, right)

        self.assertFalse(result.has_conflicts)
        self.assertEqual(result.merged, {"o": {"a": 9, "b": 8}})

    def test_nested_conflict_path_is_json_pointer(self):
        schema = {
            "mergeStrategy": "objectMerge",
            "properties": {
                "o": {
                    "mergeStrategy": "objectMerge",
                    "properties": {
                        "leaf": {"mergeStrategy": "overwrite"},
                    },
                },
            },
        }
        merger = ThreeWayMerger(schema)
        result = merger.merge(
            {"o": {"leaf": 1}},
            {"o": {"leaf": 2}},
            {"o": {"leaf": 3}})

        self.assertEqual(result.conflicts[0].path, "/o/leaf")

    def test_conflict_path_escapes_special_tokens(self):
        result = three_way_merge(
            {"a/b": 1}, {"a/b": 2}, {"a/b": 3})
        self.assertEqual(result.conflicts[0].path, "/a~1b")


class TestThreeWayArrays(unittest.TestCase):

    ID_SCHEMA = {
        "properties": {
            "items": {
                "mergeStrategy": "arrayMergeById",
                "mergeOptions": {"idRef": "/id"},
                "items": {"mergeStrategy": "objectMerge"},
            },
        },
    }

    def test_array_without_idref_is_whole_value(self):
        schema = {
            "properties": {
                "tags": {},
            },
        }
        merger = ThreeWayMerger(schema)

        same = merger.merge(
            {"tags": [1]}, {"tags": [1, 2]}, {"tags": [1, 2]})
        self.assertFalse(same.has_conflicts)
        self.assertEqual(same.merged, {"tags": [1, 2]})

        divergent = merger.merge(
            {"tags": [1]}, {"tags": [1, 2]}, {"tags": [1, 3]})
        self.assertEqual(
            [conflict.path for conflict in divergent.conflicts],
            ["/tags"])
        self.assertEqual(divergent.conflicts[0].ancestor, [1])

    def test_array_merge_by_id_without_declared_idref_is_atomic(self):
        schema = {
            "properties": {
                "items": {
                    "mergeStrategy": "arrayMergeById",
                    "items": {"mergeStrategy": "objectMerge"},
                },
            },
        }
        merger = ThreeWayMerger(schema)
        result = merger.merge(
            {"items": [{"id": 1, "v": 0}]},
            {"items": [{"id": 1, "v": 1}]},
            {"items": [{"id": 1, "v": 2}]})

        self.assertEqual(
            [conflict.path for conflict in result.conflicts], ["/items"])

    def test_index_strategy_is_rejected(self):
        schema = {
            "properties": {
                "items": {"mergeStrategy": "arrayMergeByIndex"},
            },
        }
        with self.assertRaises(ThreeWayStrategyError):
            ThreeWayMerger(schema)

    def test_identity_array_disjoint_item_edits(self):
        merger = ThreeWayMerger(self.ID_SCHEMA)
        ancestor = {
            "items": [
                {"id": 1, "name": "a"},
                {"id": 2, "name": "b"},
            ],
        }
        left = {
            "items": [
                {"id": 1, "name": "a2"},
                {"id": 2, "name": "b"},
            ],
        }
        right = {
            "items": [
                {"id": 1, "name": "a"},
                {"id": 2, "name": "b2"},
            ],
        }

        result = merger.merge(ancestor, left, right)

        self.assertFalse(result.has_conflicts)
        self.assertEqual(
            result.merged,
            {"items": [
                {"id": 1, "name": "a2"},
                {"id": 2, "name": "b2"},
            ]})

    def test_identity_array_rename_is_delete_plus_add(self):
        merger = ThreeWayMerger(self.ID_SCHEMA)
        ancestor = {
            "items": [
                {"id": 1, "name": "a"},
                {"id": 2, "name": "b"},
            ],
        }
        # Left renames id 2 -> 3 (delete old identity, add a new one).
        left = {
            "items": [
                {"id": 1, "name": "a"},
                {"id": 3, "name": "renamed"},
            ],
        }
        # Right edits a field of the unchanged identity.
        right = {
            "items": [
                {"id": 1, "name": "a"},
                {"id": 2, "name": "b2"},
            ],
        }

        result = merger.merge(ancestor, left, right)

        # The rename deletes identity 2 while the other side modifies it:
        # this is a delete/modify conflict carrying all three values.
        conflicts = conflicts_by_path(result)
        self.assertIn("/items/1", conflicts)
        conflict = conflicts["/items/1"]
        self.assertEqual(conflict.conflict_type, "delete_modify")
        self.assertEqual(conflict.ancestor, {"id": 2, "name": "b"})
        self.assertEqual(conflict.left, MISSING)
        self.assertEqual(conflict.right, {"id": 2, "name": "b2"})

        # The newly added identity from the rename is still merged cleanly.
        self.assertIn({"id": 3, "name": "renamed"}, result.merged["items"])

    def test_identity_array_pure_rename_without_other_edit(self):
        merger = ThreeWayMerger(self.ID_SCHEMA)
        ancestor = {"items": [{"id": 1, "name": "a"},
                              {"id": 2, "name": "b"}]}
        left = {"items": [{"id": 1, "name": "a"},
                          {"id": 3, "name": "renamed"}]}
        # Right does not touch identity 2 at all.
        right = {"items": [{"id": 1, "name": "a"},
                           {"id": 2, "name": "b"}]}

        result = merger.merge(ancestor, left, right)

        self.assertFalse(result.has_conflicts)
        ids = [item["id"] for item in result.merged["items"]]
        self.assertEqual(ids, [1, 2, 3])

    def test_identity_array_same_rename_on_both_sides_kept_once(self):
        merger = ThreeWayMerger(self.ID_SCHEMA)
        ancestor = {"items": [{"id": 1, "name": "a"},
                              {"id": 2, "name": "b"}]}
        renamed = {"items": [{"id": 1, "name": "a"},
                             {"id": 3, "name": "x"}]}

        result = merger.merge(ancestor, renamed, copy.deepcopy(renamed))

        self.assertFalse(result.has_conflicts)
        self.assertEqual(
            result.merged["items"],
            [{"id": 1, "name": "a"}, {"id": 3, "name": "x"}])

    def test_identity_array_field_conflict_keeps_item_with_three_values(self):
        merger = ThreeWayMerger(self.ID_SCHEMA)
        ancestor = {"items": [{"id": 1, "name": "a"}]}
        left = {"items": [{"id": 1, "name": "L"}]}
        right = {"items": [{"id": 1, "name": "R"}]}

        result = merger.merge(ancestor, left, right)

        self.assertEqual(len(result.conflicts), 1)
        conflict = result.conflicts[0]
        self.assertEqual(conflict.path, "/items/0/name")
        self.assertEqual(conflict.ancestor, "a")
        self.assertEqual(conflict.left, "L")
        self.assertEqual(conflict.right, "R")
        # The conflicting field is omitted, but the item itself survives.
        self.assertEqual(result.merged, {"items": [{"id": 1}]})

    def test_duplicate_id_is_a_conflict_and_not_silently_preferred(self):
        merger = ThreeWayMerger(self.ID_SCHEMA)
        ancestor = {"items": [{"id": 1, "name": "a"},
                              {"id": 2, "name": "b"}]}
        left = {
            "items": [
                {"id": 1, "name": "a"},
                {"id": 1, "name": "duplicate"},
            ],
        }
        right = {"items": [{"id": 1, "name": "a"},
                           {"id": 2, "name": "b2"}]}

        result = merger.merge(ancestor, left, right)

        duplicate = [
            conflict for conflict in result.conflicts
            if conflict.conflict_type == "duplicate_id"]
        self.assertEqual(len(duplicate), 1)
        self.assertEqual(duplicate[0].path, "/items/0")
        # Neither copy of the duplicated identity is written to the result,
        # but unrelated identities are still merged.
        self.assertEqual(
            result.merged["items"], [{"id": 2, "name": "b2"}])
        self.assertEqual(
            duplicate[0].left,
            [{"id": 1, "name": "a"}, {"id": 1, "name": "duplicate"}])

    def test_custom_id_ref_pointer(self):
        schema = {
            "properties": {
                "items": {
                    "mergeStrategy": "arrayMergeById",
                    "mergeOptions": {"idRef": "/key"},
                    "items": {"mergeStrategy": "objectMerge"},
                },
            },
        }
        merger = ThreeWayMerger(schema)
        result = merger.merge(
            {"items": [{"key": "a", "v": 0}]},
            {"items": [{"key": "a", "v": 1}]},
            {"items": [{"key": "a", "v": 1},
                       {"key": "b", "v": 2}]})

        self.assertFalse(result.has_conflicts)
        self.assertEqual(
            result.merged,
            {"items": [{"key": "a", "v": 1}, {"key": "b", "v": 2}]})

    def test_root_id_ref_treats_scalar_array_as_identity_set(self):
        schema = {
            "properties": {
                "items": {
                    "mergeStrategy": "arrayMergeById",
                    "mergeOptions": {"idRef": "/"},
                    "items": {},
                },
            },
        }
        merger = ThreeWayMerger(schema)

        result = merger.merge(
            {"items": ["a", "b"]},
            {"items": ["a", "b", "c"]},
            {"items": ["a", "b"]})

        self.assertFalse(result.has_conflicts)
        self.assertEqual(result.merged, {"items": ["a", "b", "c"]})

    def test_duplicate_scalar_identity_is_reported(self):
        schema = {
            "properties": {
                "items": {
                    "mergeStrategy": "arrayMergeById",
                    "mergeOptions": {"idRef": "/"},
                    "items": {},
                },
            },
        }
        merger = ThreeWayMerger(schema)
        result = merger.merge(
            {"items": ["a"]},
            {"items": ["a", "a"]},
            {"items": ["a"]})

        self.assertEqual(
            [conflict.conflict_type for conflict in result.conflicts],
            ["duplicate_id"])

    def test_item_without_resolvable_id_is_whole_addition(self):
        merger = ThreeWayMerger(self.ID_SCHEMA)
        ancestor = {"items": [{"id": 1, "name": "a"}]}
        left = {"items": [{"id": 1, "name": "a"},
                          {"name": "no-identity"}]}
        right = {"items": [{"id": 1, "name": "a"}]}

        result = merger.merge(ancestor, left, right)

        self.assertFalse(result.has_conflicts)
        self.assertIn({"name": "no-identity"}, result.merged["items"])

    def test_empty_arrays_merge_cleanly(self):
        merger = ThreeWayMerger(self.ID_SCHEMA)
        result = merger.merge(
            {"items": []}, {"items": []}, {"items": []})
        self.assertFalse(result.has_conflicts)
        self.assertEqual(result.merged, {"items": []})

    def test_array_merge_is_symmetric_under_left_right_swap(self):
        import random

        merger = ThreeWayMerger(self.ID_SCHEMA)

        def random_items():
            count = random.randint(0, 4)
            ids = random.sample(range(1, 6), count)
            return {"items": [
                {"id": item_id, "v": random.choice([0, 1, 2])}
                for item_id in ids]}

        random.seed(7)
        for _ in range(200):
            ancestor = random_items()
            left = random_items()
            right = random_items()

            forward = merger.merge(ancestor, left, right)
            swapped = merger.merge(ancestor, right, left)

            self.assertEqual(forward.merged, swapped.merged)

            forward_sig = sorted(
                (c.path, c.conflict_type, c.ancestor, c.left, c.right)
                for c in forward.conflicts)
            swapped_sig = sorted(
                (c.path, c.conflict_type, c.ancestor, c.right, c.left)
                for c in swapped.conflicts)
            self.assertEqual(forward_sig, swapped_sig)


class TestThreeWayStrategies(unittest.TestCase):

    def test_overwrite_leaf_returns_accepted_value(self):
        # Regression: a non-conflicting overwrite leaf must return the
        # accepted value rather than None.
        schema = {
            "mergeStrategy": "objectMerge",
            "properties": {
                "name": {"mergeStrategy": "overwrite"},
            },
        }
        merger = ThreeWayMerger(schema)
        result = merger.merge(
            {"name": "acme"},
            {"name": "acme-corp"},
            {"name": "acme"})

        self.assertFalse(result.has_conflicts)
        self.assertEqual(result.merged, {"name": "acme-corp"})

    def test_default_strategy_three_way_returns_leaf_value(self):

        class DefaultLeafStrategy(Strategy):
            three_way_capable = True

        merger = ThreeWayMerger(
            {"mergeStrategy": "default-leaf"},
            strategies={"default-leaf": DefaultLeafStrategy()})

        result = merger.merge(1, 2, 1)
        self.assertEqual(result.merged, 2)

    def test_unknown_strategy_is_rejected(self):
        with self.assertRaises(ThreeWayStrategyError):
            ThreeWayMerger({"mergeStrategy": "does-not-exist"})

    def test_builtin_non_three_way_strategies_rejected(self):
        for name in ("discard", "version", "append", "arrayMergeByIndex"):
            with self.assertRaises(ThreeWayStrategyError):
                ThreeWayMerger({"mergeStrategy": name})

    def test_nested_unknown_strategy_is_rejected_early(self):
        schema = {
            "properties": {
                "nested": {"mergeStrategy": "append"},
            },
        }
        with self.assertRaises(ThreeWayStrategyError):
            ThreeWayMerger(schema)

    def test_custom_strategy_must_declare_capability(self):

        class LegacyStrategy(Strategy):
            def merge(self, walk, base, head, schema, **kwargs):
                return head

        with self.assertRaises(ThreeWayStrategyError):
            ThreeWayMerger(
                {"mergeStrategy": "legacy"},
                strategies={"legacy": LegacyStrategy()})

    def test_capable_custom_strategy_is_used(self):

        class CustomLeaf(Strategy):
            three_way_capable = True

            def merge_three_way(self, walk, ancestor, left, right, schema,
                                path=(), **kwargs):
                return walk.merge_leaf(ancestor, left, right, path)

        merger = ThreeWayMerger(
            {"mergeStrategy": "custom"},
            strategies={"custom": CustomLeaf()})
        result = merger.merge(1, 2, 3)

        self.assertEqual(result.conflicts[0].path, "")
        self.assertEqual(
            (result.conflicts[0].ancestor,
             result.conflicts[0].left,
             result.conflicts[0].right),
            (1, 2, 3))

    def test_ref_strategy_is_resolved(self):
        schema = {
            "definitions": {"leaf": {"mergeStrategy": "overwrite"}},
            "mergeStrategy": "objectMerge",
            "properties": {
                "k": {"$ref": "#/definitions/leaf"},
            },
        }
        result = ThreeWayMerger(schema).merge(
            {"k": 1}, {"k": 2}, {"k": 3})
        self.assertEqual(result.conflicts[0].path, "/k")


class TestThreeWaySymmetryAndImmutability(unittest.TestCase):

    def test_left_right_swap_is_equivalent_without_conflicts(self):
        ancestor = {"a": 1, "b": {"x": 1}, "c": [1]}
        left = {"a": 1, "b": {"x": 2}, "c": [1], "d": 4}
        right = {"a": 7, "b": {"x": 1, "y": 2}, "c": [1]}

        forward = three_way_merge(ancestor, left, right)
        swapped = three_way_merge(ancestor, right, left)

        self.assertEqual(forward.merged, swapped.merged)

    def test_left_right_swap_keeps_equivalent_conflicts(self):
        ancestor = {"a": 1, "b": 2}
        left = {"a": 8, "b": 2}
        right = {"a": 1, "b": 9}

        forward = three_way_merge(ancestor, left, right)
        swapped = three_way_merge(ancestor, right, left)

        def signature(result):
            return sorted(
                (conflict.path, conflict.conflict_type, conflict.ancestor,
                 conflict.left, conflict.right)
                for conflict in result.conflicts)

        forward_sig = signature(forward)
        swapped_sig = [
            (path, conflict_type, anc, right_value, left_value)
            for path, conflict_type, anc, left_value, right_value
            in signature(swapped)]
        self.assertEqual(forward_sig, swapped_sig)

    def test_inputs_are_not_modified(self):
        ancestor = {"o": {"x": [1, 2]}, "k": 1}
        left = copy.deepcopy(ancestor)
        right = copy.deepcopy(ancestor)
        left["o"]["x"].append(3)
        right["k"] = 9

        ancestor_copy = copy.deepcopy(ancestor)
        left_copy = copy.deepcopy(left)
        right_copy = copy.deepcopy(right)

        three_way_merge(ancestor, left, right)

        self.assertEqual(ancestor, ancestor_copy)
        self.assertEqual(left, left_copy)
        self.assertEqual(right, right_copy)

    def test_conflicting_merge_does_not_mutate_inputs(self):
        ancestor = {"k": 1}
        left = {"k": 2}
        right = {"k": 3}

        ancestor_copy = copy.deepcopy(ancestor)
        left_copy = copy.deepcopy(left)
        right_copy = copy.deepcopy(right)

        result = three_way_merge(ancestor, left, right)
        self.assertTrue(result.has_conflicts)

        self.assertEqual(ancestor, ancestor_copy)
        self.assertEqual(left, left_copy)
        self.assertEqual(right, right_copy)


class TestThreeWayResolution(unittest.TestCase):

    def setUp(self):
        self.merger = ThreeWayMerger({})
        self.ancestor = {"a": 1, "b": 1, "c": 1}
        self.left = {"a": 2, "b": 7, "c": 8}
        self.right = {"a": 3, "b": 9, "c": 8}

    def test_resolution_revalidates_and_clears_solved_conflicts(self):
        initial = self.merger.merge(
            self.ancestor, self.left, self.right)
        ids = dict(
            (conflict.path, conflict.conflict_id)
            for conflict in initial.conflicts)

        result = self.merger.resolve(
            self.ancestor, self.left, self.right,
            {ids["/a"]: "solved"})

        self.assertEqual([c.path for c in result.conflicts], ["/b"])
        self.assertEqual(result.merged["a"], "solved")
        self.assertEqual(result.merged["c"], 8)

    def test_full_resolution_produces_clean_document(self):
        initial = self.merger.merge(
            self.ancestor, self.left, self.right)
        resolutions = dict(
            (conflict.conflict_id, conflict.right)
            for conflict in initial.conflicts)

        result = self.merger.resolve(
            self.ancestor, self.left, self.right, resolutions)

        self.assertFalse(result.has_conflicts)
        self.assertEqual(result.merged, {"a": 3, "b": 9, "c": 8})

    def test_missing_sentinel_deletes_path(self):
        initial = self.merger.merge(
            self.ancestor, self.left, self.right)
        conflict_id = initial.conflicts[0].conflict_id

        result = self.merger.resolve(
            self.ancestor, self.left, self.right,
            {conflict_id: MISSING})

        self.assertNotIn(initial.conflicts[0].path.lstrip("/"),
                         result.merged)

    def test_explicit_null_resolution_is_kept(self):
        initial = self.merger.merge(
            self.ancestor, self.left, self.right)
        conflict_id = initial.conflicts[0].conflict_id

        result = self.merger.resolve(
            self.ancestor, self.left, self.right,
            {conflict_id: None})

        self.assertIsNone(
            result.merged[initial.conflicts[0].path.lstrip("/")])

    def test_unknown_conflict_id_is_rejected(self):
        with self.assertRaises(ThreeWayConflictResolutionError):
            self.merger.resolve(
                self.ancestor, self.left, self.right,
                {"conflict-999": 1})

    def test_resolution_inputs_remain_unchanged(self):
        initial = self.merger.merge(
            self.ancestor, self.left, self.right)
        conflict_id = initial.conflicts[0].conflict_id

        ancestor_copy = copy.deepcopy(self.ancestor)
        self.merger.resolve(
            self.ancestor, self.left, self.right,
            {conflict_id: {"nested": []}})

        self.assertEqual(self.ancestor, ancestor_copy)


class TestThreeWayArrayResolution(unittest.TestCase):

    SCHEMA = {
        "properties": {
            "items": {
                "mergeStrategy": "arrayMergeById",
                "mergeOptions": {"idRef": "/id"},
                "items": {"mergeStrategy": "objectMerge"},
            },
        },
    }

    def test_item_level_conflict_can_be_resolved_with_value(self):
        merger = ThreeWayMerger(self.SCHEMA)
        ancestor = {
            "items": [
                {"id": 1, "name": "a"},
                {"id": 2, "name": "b"},
            ],
        }
        left = {
            "items": [
                {"id": 1, "name": "a"},
                {"id": 3, "name": "renamed"},
            ],
        }
        right = {
            "items": [
                {"id": 1, "name": "a"},
                {"id": 2, "name": "b2"},
            ],
        }

        initial = merger.merge(ancestor, left, right)
        conflict_id = initial.conflicts[0].conflict_id

        result = merger.resolve(
            ancestor, left, right,
            {conflict_id: {"id": 2, "name": "chosen"}})

        self.assertFalse(result.has_conflicts)
        ids = [item["id"] for item in result.merged["items"]]
        self.assertEqual(ids, [1, 2, 3])
        chosen = [item for item in result.merged["items"]
                  if item["id"] == 2][0]
        self.assertEqual(chosen["name"], "chosen")

    def test_item_level_conflict_can_be_resolved_by_deletion(self):
        merger = ThreeWayMerger(self.SCHEMA)
        ancestor = {"items": [{"id": 1, "name": "a"},
                              {"id": 2, "name": "b"}]}
        left = {"items": [{"id": 1, "name": "a"},
                          {"id": 3, "name": "renamed"}]}
        right = {"items": [{"id": 1, "name": "a"},
                           {"id": 2, "name": "b2"}]}

        initial = merger.merge(ancestor, left, right)
        conflict_id = initial.conflicts[0].conflict_id

        result = merger.resolve(
            ancestor, left, right, {conflict_id: MISSING})

        self.assertFalse(result.has_conflicts)
        self.assertEqual(
            result.merged["items"],
            [{"id": 1, "name": "a"}, {"id": 3, "name": "renamed"}])

    def test_field_level_array_conflict_can_be_resolved(self):
        merger = ThreeWayMerger(self.SCHEMA)
        ancestor = {"items": [{"id": 1, "name": "a"}]}
        left = {"items": [{"id": 1, "name": "L"}]}
        right = {"items": [{"id": 1, "name": "R"}]}

        initial = merger.merge(ancestor, left, right)
        conflict_id = initial.conflicts[0].conflict_id

        result = merger.resolve(
            ancestor, left, right, {conflict_id: "chosen"})

        self.assertFalse(result.has_conflicts)
        self.assertEqual(
            result.merged["items"], [{"id": 1, "name": "chosen"}])


class TestThreeWayPublicApi(unittest.TestCase):

    def test_module_level_function_and_method_agree(self):
        ancestor, left, right = {"k": 1}, {"k": 2}, {"k": 3}

        function_result = three_way_merge(ancestor, left, right)
        method_result = merge_three_way(ancestor, left, right, schema={})

        self.assertEqual(
            function_result.conflicts_as_dict(),
            method_result.conflicts_as_dict())
        self.assertEqual(function_result.merged, method_result.merged)

    def test_result_dict_is_json_serializable(self):
        import json

        result = three_way_merge(
            {"k": 1}, {}, {"k": 2})
        encoded = json.dumps(result.as_dict())
        decoded = json.loads(encoded)

        self.assertEqual(decoded["conflicts"][0]["type"], "delete_modify")
        self.assertFalse(decoded["conflicts"][0]["left_present"])

    def test_invalid_schema_type_raises(self):
        with self.assertRaises(ThreeWayInputError):
            ThreeWayMerger([])

    def test_conflict_ids_are_stable_strings(self):
        result = three_way_merge(
            {"a": 1, "b": 1}, {"a": 2, "b": 2}, {"a": 3, "b": 4})
        ids = [conflict.conflict_id for conflict in result.conflicts]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(isinstance(conflict_id, str)
                            for conflict_id in ids))

    def test_jsonmerge_namespace_exports(self):
        self.assertTrue(hasattr(jsonmerge, "three_way_merge"))
        self.assertTrue(hasattr(jsonmerge, "ThreeWayMerger"))
        self.assertTrue(hasattr(jsonmerge, "MISSING"))


class TestThreeWaySchemaValidation(unittest.TestCase):

    SCHEMA = {
        "type": "object",
        "properties": {
            "a": {"type": "integer"},
            "b": {"type": "integer"},
        },
        "required": ["a", "b"],
        "additionalProperties": False,
    }

    def test_clean_merge_can_be_validated(self):
        merger = ThreeWayMerger(self.SCHEMA)
        result = merger.merge(
            {"a": 1, "b": 1}, {"a": 2, "b": 1}, {"a": 2, "b": 1},
            validate=True)
        self.assertFalse(result.has_conflicts)
        self.assertEqual(result.merged, {"a": 2, "b": 1})

    def test_validate_document_reports_failing_path(self):
        merger = ThreeWayMerger(self.SCHEMA)
        with self.assertRaises(ThreeWayInputError) as cm:
            merger.validate_document({"a": "no", "b": 1})
        self.assertIn("/a", str(cm.exception))

    def test_resolve_validates_fully_resolved_document(self):
        merger = ThreeWayMerger(self.SCHEMA)
        ancestor = {"a": 1, "b": 1}
        left = {"a": 2, "b": 1}
        right = {"a": 3, "b": 9}
        initial = merger.merge(ancestor, left, right)
        resolutions = dict(
            (conflict.conflict_id, conflict.right)
            for conflict in initial.conflicts)

        result = merger.resolve(ancestor, left, right, resolutions)
        self.assertFalse(result.has_conflicts)
        self.assertEqual(result.merged, {"a": 3, "b": 9})

    def test_invalid_resolution_value_is_rejected(self):
        merger = ThreeWayMerger(self.SCHEMA)
        ancestor = {"a": 1, "b": 1}
        left = {"a": 2, "b": 1}
        right = {"a": 3, "b": 9}
        initial = merger.merge(ancestor, left, right)
        bad_conflict = initial.conflicts[0]

        with self.assertRaises(ThreeWayInputError):
            merger.resolve(
                ancestor, left, right,
                {bad_conflict.conflict_id: "not-an-integer"})

    def test_validation_can_be_disabled(self):
        merger = ThreeWayMerger(self.SCHEMA)
        ancestor = {"a": 1, "b": 1}
        initial = merger.merge(
            ancestor, {"a": 2, "b": 1}, {"a": 3, "b": 1})
        conflict_id = initial.conflicts[0].conflict_id

        result = merger.resolve(
            ancestor, {"a": 2, "b": 1}, {"a": 3, "b": 1},
            {conflict_id: "string"}, validate=False)
        self.assertFalse(result.has_conflicts)
        self.assertEqual(result.merged["a"], "string")


class TestThreeWayObjectClass(unittest.TestCase):

    def test_unknown_objclass_is_rejected_early(self):
        with self.assertRaises(ThreeWayStrategyError):
            ThreeWayMerger({
                "mergeStrategy": "objectMerge",
                "mergeOptions": {"objClass": "does-not-exist"},
            })

    def test_custom_object_class_menu_is_used(self):

        class MyDict(dict):
            pass

        merger = ThreeWayMerger(
            {}, objclass_def="mine",
            objclass_menu={"mine": MyDict})
        result = merger.merge({}, {"k": 1}, {})

        self.assertIsInstance(result.merged, MyDict)

    def test_builtin_ordered_dict_option(self):
        from collections import OrderedDict

        merger = ThreeWayMerger({
            "mergeStrategy": "objectMerge",
            "mergeOptions": {"objClass": "OrderedDict"},
        })
        result = merger.merge(
            OrderedDict([("a", 1)]),
            OrderedDict([("a", 1), ("b", 2)]),
            OrderedDict([("a", 1)]))

        self.assertIsInstance(result.merged, OrderedDict)
        self.assertEqual(list(result.merged.keys()), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
