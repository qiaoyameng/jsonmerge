# vim:ts=4 sw=4 expandtab softtabstop=4
import unittest

import jsonmerge
from jsonmerge.order_constraints import OrderConstraintError
from jsonmerge.exceptions import SchemaError


SCHEMA = {
    'mergeStrategy': 'arrayMergeById',
    'items': {
        'properties': {
            'id': {'type': 'string'},
            'value': {'mergeStrategy': 'version'},
        }
    }
}


def ids(values):
    return [item['id'] for item in values]


def assert_declared_order(testcase, result, sources):
    position = dict((item['id'], i) for i, item in enumerate(result))

    for source in sources:
        for i in range(len(source) - 1):
            testcase.assertLess(
                    position[source[i]['id']],
                    position[source[i + 1]['id']],
                    '%r must remain before %r' % (
                        source[i]['id'], source[i + 1]['id']))


class TestOrderConstraints(unittest.TestCase):

    def test_batches_preserve_every_declared_constraint(self):
        source_a = [{'id': 'b', 'value': 1}, {'id': 'a', 'value': 2}]
        source_b = [{'id': 'a', 'value': 3}, {'id': 'c', 'value': 4}]
        source_c = [{'id': 'c', 'value': 5}, {'id': 'd', 'value': 6}]

        merger = jsonmerge.Merger(SCHEMA)

        result, state = merger.merge_ordered(None, source_a)
        result, state = merger.merge_ordered(result, source_b, state=state)
        result, state = merger.merge_ordered(result, source_c, state=state)

        self.assertEqual(ids(result), ['b', 'a', 'c', 'd'])
        self.assertEqual(
                [item['value'] for item in result],
                [[{'value': 1}], [{'value': 2}, {'value': 3}],
                 [{'value': 4}, {'value': 5}], [{'value': 6}]])
        assert_declared_order(self, result, (source_a, source_b, source_c))

        # The returned state is self-contained and can be serialized.  It
        # does not need to be reverse-engineered from the sorted result.
        self.assertIn('edges', state)
        self.assertEqual(state['first_seen'], ['b', 'a', 'c', 'd'])

    def test_state_survives_batching_and_normal_merge_api(self):
        source_a = [{'id': 'b'}, {'id': 'a'}]
        source_b = [{'id': 'a'}, {'id': 'c'}]

        merger = jsonmerge.Merger(SCHEMA)
        result, state = merger.merge_ordered(None, source_a, source='a')
        result, state = merger.merge_ordered(
                result, source_b, state=state, source='b')

        merger2 = jsonmerge.Merger(SCHEMA)
        result2 = merger2.merge(
                None, source_a,
                merge_options={'arrayMergeById': {
                    'order_constraints': True,
                    'source': 'a'}})
        result2 = merger2.merge(
                result2, source_b,
                merge_options={'arrayMergeById': {
                    'order_constraints': merger2.last_order_constraint_state,
                    'source': 'b'}})

        self.assertEqual(result, result2)
        self.assertEqual(
                state, merger2.last_order_constraint_state)

    def test_unrelated_items_use_first_occurrence(self):
        source_a = [{'id': 'z'}, {'id': 'a'}]
        source_b = [{'id': 'm'}, {'id': 'b'}]

        merger = jsonmerge.Merger(SCHEMA)
        result, state = merger.merge_ordered(None, source_a)
        result, state = merger.merge_ordered(result, source_b, state=state)

        self.assertEqual(ids(result), ['z', 'a', 'm', 'b'])

    def test_state_does_not_invent_edges_from_sorted_base(self):
        source_a = [{'id': 'a'}, {'id': 'b'}]
        source_b = [{'id': 'c'}, {'id': 'd'}]
        source_c = [{'id': 'c'}, {'id': 'b'}]

        merger = jsonmerge.Merger(SCHEMA)
        result, state = merger.merge_ordered(None, source_a, source='a')
        result, state = merger.merge_ordered(
                result, source_b, state=state, source='b')
        result, state = merger.merge_ordered(
                result, source_c, state=state, source='c')

        # The sorted intermediate result puts b before c, but that is only a
        # first-occurrence tie-break, not a declared constraint.  Carrying
        # the state keeps c -> b legal instead of manufacturing b -> c.
        self.assertEqual(ids(result), ['a', 'c', 'b', 'd'])
        self.assertNotIn(
                {'before': 'b', 'after': 'c', 'sources': ['base']},
                state['edges'])

    def test_minimal_cycle_names_ids_and_sources(self):
        source_a = [{'id': 'a'}, {'id': 'b'}]
        source_b = [{'id': 'b'}, {'id': 'c'}]
        source_c = [{'id': 'c'}, {'id': 'a'}]

        merger = jsonmerge.Merger(SCHEMA)
        result, state = merger.merge_ordered(None, source_a, source='a')
        result, state = merger.merge_ordered(
                result, source_b, state=state, source='b')

        with self.assertRaises(OrderConstraintError) as cm:
            merger.merge_ordered(
                    result, source_c, state=state, source='c')

        message = str(cm.exception)
        self.assertIn('"a" -> "b" -> "c" -> "a"', message)
        for source in ('a', 'b', 'c'):
            self.assertIn(source, message)

    def test_removing_member_does_not_join_neighbours(self):
        source_a = [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}, {'id': 'd'}]

        merger = jsonmerge.Merger(SCHEMA)
        result, state = merger.merge_ordered(None, source_a)
        result, state = merger.merge_ordered(
                result, [], state=state,
                schema_options={'removeIds': ['b']})

        self.assertEqual(ids(result), ['a', 'c', 'd'])
        for edge in state['edges']:
            self.assertNotEqual(edge['before'], 'b')
            self.assertNotEqual(edge['after'], 'b')
            # Removing b only removed a -> b and b -> c.  It did not invent
            # an a -> c relationship.
            self.assertFalse(
                    edge['before'] == 'a' and edge['after'] == 'c')

        result, state = merger.merge_ordered(
                result, [{'id': 'c'}, {'id': 'a'}], state=state,
                source='new')
        self.assertEqual(ids(result), ['c', 'a', 'd'])

    def test_mixing_sort_options_is_rejected(self):
        schema = {
            'mergeStrategy': 'arrayMergeById',
            'mergeOptions': {
                'order_constraints': True,
                'sortByRef': 'id',
            },
        }

        merger = jsonmerge.Merger(schema)
        with self.assertRaises(SchemaError) as cm:
            merger.merge(None, [{'id': 'a'}])

        self.assertIn('cannot be combined', str(cm.exception))

    def test_id_types_are_distinct(self):
        merger = jsonmerge.Merger({
            'mergeStrategy': 'arrayMergeById',
            'mergeOptions': {'order_constraints': True},
        })

        result = merger.merge(
                None, [{'id': 1}, {'id': True}])

        self.assertEqual(result, [{'id': 1}, {'id': True}])


if __name__ == '__main__':
    unittest.main()
