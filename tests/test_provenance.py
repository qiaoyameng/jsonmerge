# vim:ts=4 sw=4 expandtab softtabstop=4
import copy
import json
import unittest

import jsonmerge
import jsonmerge.strategies
from jsonmerge.jsonvalue import JSONValue


def find_node(tree, ref):
    """Return the provenance node at JSON pointer ``ref`` (or None)."""
    if not tree:
        return None
    if tree.get('ref') == ref:
        return tree
    for child in tree.get('children', ()):
        found = find_node(child, ref)
        if found is not None:
            return found
    return None


def all_sources(node):
    sources = []

    def visit(current):
        for source in current.get('sources', ()):
            if source not in sources:
                sources.append(source)
        for child in current.get('children', ()):
            visit(child)

    visit(node)
    return sources


# Schema exercising every built-in strategy at least once.
SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"mergeStrategy": "overwrite"},
        "owner": {"mergeStrategy": "discard"},
        "tags": {"mergeStrategy": "append"},
        "items": {
            "mergeStrategy": "arrayMergeById",
            "mergeOptions": {"idRef": "id"},
            "items": {
                "type": "object",
                "properties": {
                    "id": {"mergeStrategy": "discard"},
                    "status": {"mergeStrategy": "overwrite"},
                    "note": {"mergeStrategy": "version"},
                },
            },
        },
    },
}

BATCH_1 = {
    "title": "initial",
    "owner": "alice",
    "tags": ["raw"],
    "items": [
        {"id": 1, "status": "new", "note": "created"},
        {"id": 2, "status": "new", "note": "todo"},
    ],
}

BATCH_2 = {
    "title": "renamed",
    "owner": "bob",
    "tags": ["fresh"],
    "items": [
        {"id": 1, "status": "in-progress", "note": "started"},
        {"id": 3, "status": "new", "note": "added"},
    ],
}

BATCH_3 = {
    "title": "renamed",
    "tags": ["fresh"],
    "items": [
        {"id": 1, "status": "done", "note": "started"},
        {"id": 2, "status": "new", "note": "todo"},
    ],
}


class TestMergeWithProvenance(unittest.TestCase):

    def _three_batches(self, history_limit=10):
        merger = jsonmerge.Merger(SCHEMA)
        result = merger.merge_with_provenance(
                None, BATCH_1, source_id="batch-1", history_limit=history_limit)
        result = merger.merge_with_provenance(
                result.merged, BATCH_2, source_id="batch-2",
                provenance=result.provenance, history_limit=history_limit)
        result = merger.merge_with_provenance(
                result.merged, BATCH_3, source_id="batch-3",
                provenance=result.provenance, history_limit=history_limit)
        return result

    def test_merged_matches_plain_merge(self):
        plain = None
        for head in (BATCH_1, BATCH_2, BATCH_3):
            plain = jsonmerge.merge(plain, copy.deepcopy(head), SCHEMA)

        result = self._three_batches()
        self.assertEqual(result.merged, plain)

    def test_provenance_is_plain_json_serializable_data(self):
        result = self._three_batches()
        encoded = json.dumps(result.provenance)
        self.assertEqual(json.loads(encoded), result.provenance)

    def test_object_overwrite_field_level_sources(self):
        result = self._three_batches()
        tree = result.provenance

        # title was overwritten by batch-2; batch-3 carries the same value,
        # so its provenance stays with the batch that first supplied it.
        title = find_node(tree, "#/title")
        self.assertEqual(title['sources'], ["batch-2"])
        self.assertEqual(
                [entry['sources'] for entry in title['history']],
                [["batch-1"]])

        # discard keeps the first value and never adopts later heads.
        self.assertNotIn("owner", result.merged)

    def test_overwrite_history_rolls_forward_on_each_real_change(self):
        merger = jsonmerge.Merger({
            "type": "object",
            "properties": {"title": {"mergeStrategy": "overwrite"}},
        })
        result = merger.merge_with_provenance(
                None, {"title": "v1"}, source_id="b1")
        result = merger.merge_with_provenance(
                result.merged, {"title": "v2"}, source_id="b2",
                provenance=result.provenance)
        result = merger.merge_with_provenance(
                result.merged, {"title": "v3"}, source_id="b3",
                provenance=result.provenance)

        title = find_node(result.provenance, "#/title")
        self.assertEqual(title['sources'], ["b3"])
        self.assertEqual(
                [entry['sources'] for entry in title['history']],
                [["b1"], ["b2"]])

    def test_append_element_level_sources(self):
        result = self._three_batches()
        tree = result.provenance

        self.assertEqual(
                find_node(tree, "#/tags/0")['sources'], ["batch-1"])
        self.assertEqual(
                find_node(tree, "#/tags/1")['sources'], ["batch-2"])
        self.assertEqual(
                find_node(tree, "#/tags/2")['sources'], ["batch-3"])

    def test_array_merge_by_id_update_and_insertion(self):
        # Keep the ids in the document so the library can match elements,
        # mirroring the documented arrayMergeById precondition.
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "mergeStrategy": "arrayMergeById",
                    "mergeOptions": {"idRef": "id"},
                    "items": {
                        "type": "object",
                        "properties": {
                            "status": {"mergeStrategy": "overwrite"},
                            "note": {"mergeStrategy": "version"},
                        },
                    },
                },
            },
        }

        merger = jsonmerge.Merger(schema)
        r1 = merger.merge_with_provenance(None, BATCH_1, source_id="batch-1")
        r2 = merger.merge_with_provenance(
                r1.merged, BATCH_2, source_id="batch-2", provenance=r1.provenance)
        r3 = merger.merge_with_provenance(
                r2.merged, BATCH_3, source_id="batch-3", provenance=r2.provenance)

        plain = None
        for head in (BATCH_1, BATCH_2, BATCH_3):
            plain = jsonmerge.merge(plain, copy.deepcopy(head), schema)
        self.assertEqual(r3.merged, plain)

        tree = r3.provenance

        # id=1 stays at index 0 and is updated in place by batch-3.
        self.assertEqual(
                find_node(tree, "#/items/0/status")['sources'], ["batch-3"])

        # id=2 is untouched after batch-1.
        self.assertEqual(
                find_node(tree, "#/items/1/status")['sources'], ["batch-1"])

        # id=3 was inserted by batch-2.
        self.assertEqual(
                find_node(tree, "#/items/2/status")['sources'], ["batch-2"])

    def test_version_dedup_records_only_real_changes(self):
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "mergeStrategy": "arrayMergeById",
                    "mergeOptions": {"idRef": "id"},
                    "items": {
                        "type": "object",
                        "properties": {
                            "note": {"mergeStrategy": "version"},
                        },
                    },
                },
            },
        }

        merger = jsonmerge.Merger(schema)
        r1 = merger.merge_with_provenance(None, BATCH_1, source_id="batch-1")
        r2 = merger.merge_with_provenance(
                r1.merged, BATCH_2, source_id="batch-2", provenance=r1.provenance)
        r3 = merger.merge_with_provenance(
                r2.merged, BATCH_3, source_id="batch-3", provenance=r2.provenance)

        item_one_note = find_node(r3.provenance, "#/items/0/note")
        # created in batch-1, changed once in batch-2, unchanged in batch-3.
        self.assertEqual(len(item_one_note['children']), 2)
        self.assertEqual(
                find_node(item_one_note, "#/items/0/note/0/value")['sources'],
                ["batch-1"])
        self.assertEqual(
                find_node(item_one_note, "#/items/0/note/1/value")['sources'],
                ["batch-2"])

        item_two_note = find_node(r3.provenance, "#/items/1/note")
        self.assertEqual(len(item_two_note['children']), 1)
        self.assertEqual(
                find_node(item_two_note, "#/items/1/note/0/value")['sources'],
                ["batch-1"])

    def test_array_merge_by_id_ignore_id_skips_matching_head(self):
        schema = {
            "mergeStrategy": "arrayMergeById",
            "mergeOptions": {"idRef": "id", "ignoreId": 9},
            "items": {"type": "object"},
        }
        merger = jsonmerge.Merger(schema)
        first = merger.merge_with_provenance(
                None, [{"id": 1, "v": "a"}], source_id="g1")
        second = merger.merge_with_provenance(
                first.merged,
                [{"id": 9, "v": "ignored"}, {"id": 1, "v": "b"}],
                source_id="g2", provenance=first.provenance)

        self.assertEqual(second.merged, [{"id": 1, "v": "b"}])
        self.assertEqual(
                find_node(second.provenance, "#/0/v")['sources'], ["g2"])

    def test_every_leaf_points_to_a_batch_that_supplied_its_value(self):
        result = self._three_batches()

        def check(node, document):
            if node['type'] == 'leaf':
                self.assertTrue(node['sources'])
            if 'children' in node:
                for child in node['children']:
                    check(child, document)

        check(result.provenance, result.merged)


class TestProvenanceHistory(unittest.TestCase):

    def test_history_limit_keeps_most_recent_sources(self):
        merger = jsonmerge.Merger({"mergeStrategy": "overwrite"})
        result = None
        provenance = None
        for index in range(5):
            result = merger.merge_with_provenance(
                    result.merged if result else None,
                    index, source_id="h%d" % index,
                    provenance=provenance, history_limit=2)
            provenance = result.provenance

        self.assertEqual(result.provenance['sources'], ["h4"])
        self.assertEqual(
                [entry['sources'] for entry in result.provenance['history']],
                [["h2"], ["h3"]])
        for entry in result.provenance['history']:
            self.assertNotIn('history', entry)

    def test_history_disabled_matches_plain_merge_values(self):
        heads = [{"a": index, "b": index} for index in range(6)]
        schema = {
            "type": "object",
            "properties": {
                "a": {"mergeStrategy": "overwrite"},
                "b": {"mergeStrategy": "overwrite"},
            },
        }

        plain = None
        for head in heads:
            plain = jsonmerge.merge(plain, copy.deepcopy(head), schema)

        merger = jsonmerge.Merger(schema)
        merged = None
        provenance = None
        for index, head in enumerate(heads):
            result = merger.merge_with_provenance(
                    merged, copy.deepcopy(head), source_id="h%d" % index,
                    provenance=provenance, history_limit=0)
            merged, provenance = result

        self.assertEqual(merged, plain)
        self.assertNotIn("history", json.dumps(provenance))

    def test_version_limit_trims_provenance_entries_with_values(self):
        merger = jsonmerge.Merger({
            "mergeStrategy": "version",
            "mergeOptions": {"limit": 2},
        })
        merged = None
        provenance = None
        for index in range(5):
            result = merger.merge_with_provenance(
                    merged, index, source_id="v%d" % index,
                    provenance=provenance)
            merged, provenance = result

        self.assertEqual(merged, [{"value": 3}, {"value": 4}])
        self.assertEqual(
                [child['sources'] for child in provenance['children']],
                [["v3"], ["v4"]])
        self.assertEqual(provenance['sources'], ["v3", "v4"])

    def test_version_limit_with_duplicates(self):
        merger = jsonmerge.Merger({
            "mergeStrategy": "version",
            "mergeOptions": {"limit": 2},
        })
        merged = None
        provenance = None
        for index, value in enumerate([1, 1, 2, 2, 3, 3]):
            result = merger.merge_with_provenance(
                    merged, value, source_id="s%d" % index,
                    provenance=provenance)
            merged, provenance = result

        self.assertEqual(merged, [{"value": 2}, {"value": 3}])
        self.assertEqual(
                [child['sources'] for child in provenance['children']],
                [["s2"], ["s4"]])


class TestMissingAndNull(unittest.TestCase):

    def test_missing_key_and_explicit_null_are_distinct(self):
        schema = {
            "type": "object",
            "properties": {
                "a": {"mergeStrategy": "overwrite"},
                "b": {"mergeStrategy": "overwrite"},
            },
        }
        merger = jsonmerge.Merger(schema)

        first = merger.merge_with_provenance(
                None, {"a": None, "b": 1}, source_id="s1")
        second = merger.merge_with_provenance(
                first.merged, {"a": 5}, source_id="s2",
                provenance=first.provenance)

        # Explicit null is a recorded value, later overwritten and traceable.
        node_a = find_node(second.provenance, "#/a")
        self.assertEqual(node_a['sources'], ["s2"])
        self.assertEqual(node_a['history'][0]['sources'], ["s1"])

        # Missing key leaves the previous value and its source untouched.
        node_b = find_node(second.provenance, "#/b")
        self.assertEqual(second.merged['b'], 1)
        self.assertEqual(node_b['sources'], ["s1"])


class TestInputIsolation(unittest.TestCase):

    def test_mutating_head_after_merge_does_not_change_provenance(self):
        merger = jsonmerge.Merger({
            "type": "object",
            "properties": {"nested": {"mergeStrategy": "overwrite"}},
        })
        head = {"nested": {"values": [1, 2, 3]}}
        snapshot = copy.deepcopy(head)

        result = merger.merge_with_provenance(None, head, source_id="s1")
        head["nested"]["values"].append(999)

        self.assertEqual(json.loads(json.dumps(result.provenance)),
                         result.provenance)
        self.assertNotIn("999", json.dumps(result.provenance))
        self.assertNotEqual(head, snapshot)


class CountingStrategy(jsonmerge.strategies.Strategy):
    invocations = 0

    def merge(self, walk, base, head, schema, **kwargs):
        CountingStrategy.invocations += 1
        total = 0 if base.is_undef() else base.val
        return JSONValue(total + head.val, head.ref)

    def get_schema(self, walk, schema, **kwargs):
        return schema


class TestCustomStrategy(unittest.TestCase):

    def test_custom_strategy_invoked_once_and_marked_coarse(self):
        merger = jsonmerge.Merger(
                {"mergeStrategy": "count"},
                strategies={"count": CountingStrategy()})

        before = CountingStrategy.invocations
        first = merger.merge_with_provenance(None, 5, source_id="s1")
        second = merger.merge_with_provenance(
                first.merged, 7, source_id="s2", provenance=first.provenance)

        self.assertEqual(CountingStrategy.invocations - before, 2)
        self.assertEqual(second.merged, 12)
        self.assertEqual(second.provenance['type'], "leaf")
        self.assertEqual(set(second.provenance['sources']), {"s1", "s2"})
        self.assertNotIn("children", second.provenance)


class TestConvenienceApi(unittest.TestCase):

    def test_module_level_function_and_auto_source_ids(self):
        schema = {"mergeStrategy": "overwrite"}

        first = jsonmerge.merge_with_provenance(
                None, "a", schema, source_id="s1")
        self.assertEqual(first.merged, jsonmerge.merge(None, "a", schema))
        self.assertEqual(first.provenance['sources'], ["s1"])

        second = jsonmerge.merge_with_provenance(
                first.merged, "b", schema, source_id="s2",
                provenance=first.provenance)
        merged, provenance = second
        self.assertEqual(merged, "b")
        self.assertEqual(provenance['sources'], ["s2"])
        self.assertEqual(provenance['history'][0]['sources'], ["s1"])

    def test_merger_generates_sequential_ids_when_omitted(self):
        merger = jsonmerge.Merger({"mergeStrategy": "overwrite"})
        first = merger.merge_with_provenance(None, "a")
        second = merger.merge_with_provenance(
                first.merged, "b", provenance=first.provenance)
        self.assertEqual(first.provenance['sources'], ["head-0"])
        self.assertEqual(second.provenance['sources'], ["head-1"])


if __name__ == '__main__':
    unittest.main()
