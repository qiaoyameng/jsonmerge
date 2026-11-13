# vim:ts=4 sw=4 expandtab softtabstop=4
import unittest

import jsonmerge
from jsonmerge.migration import (
    plan_migration,
    MigrationChange,
    MigrationError,
    PlanNotApprovedError,
    PlanExpiredError,
)


OLD_SCHEMA_NESTED_VERSION = {
    'type': 'object',
    'properties': {
        'name': {'type': 'string'},
        'config': {
            'type': 'object',
            'mergeStrategy': 'version',
        },
    },
}

NEW_SCHEMA_NESTED_VERSION = {
    'type': 'object',
    'properties': {
        'name': {'type': 'string'},
        'config': {
            'type': 'object',
            'mergeStrategy': 'objectMerge',
            'properties': {
                'a': {'type': 'integer'},
                'b': {'type': 'integer'},
                'c': {'type': 'integer'},
            },
        },
    },
}


def make_versioned_document():
    merger = jsonmerge.Merger(OLD_SCHEMA_NESTED_VERSION)
    base = merger.merge(None, {'name': 'x', 'config': {'a': 1}})
    base = merger.merge(base, {'config': {'a': 2, 'b': 3}})
    return base


class TestNestedVersionMigration(unittest.TestCase):

    def test_explicit_selection_allows_merge_to_continue(self):
        document = make_versioned_document()
        self.assertEqual(document['config'],
                         [{'value': {'a': 1}}, {'value': {'a': 2, 'b': 3}}])

        plan = plan_migration(
                OLD_SCHEMA_NESTED_VERSION, NEW_SCHEMA_NESTED_VERSION,
                document, select_versions={'/config': 0})

        self.assertEqual(plan.pending, [])
        self.assertEqual(len(plan.changes), 1)
        change = plan.changes[0]
        self.assertEqual(change.path, '/config')
        self.assertEqual(change.old_strategy, 'version')
        self.assertEqual(change.new_strategy, 'objectMerge')
        self.assertEqual(change.kind, MigrationChange.KIND_AUTOMATIC)
        self.assertIn('1 of 2 version entries', change.discarded)

        plan.approve()
        migrated = plan.apply(document)

        # the explicitly selected version won, not the last one
        self.assertEqual(migrated, {'name': 'x', 'config': {'a': 1}})
        # original document was not modified
        self.assertEqual(document['config'],
                         [{'value': {'a': 1}}, {'value': {'a': 2, 'b': 3}}])

        # the migrated document validates against the new result schema
        result_schema = jsonmerge.Merger(
                NEW_SCHEMA_NESTED_VERSION).get_schema()
        import jsonschema
        jsonschema.Draft4Validator(result_schema).validate(migrated)

        # and the next merge works with the new schema
        merger = jsonmerge.Merger(NEW_SCHEMA_NESTED_VERSION)
        merged = merger.merge(migrated, {'config': {'c': 4}})
        self.assertEqual(merged['config'], {'a': 1, 'c': 4})

    def test_no_automatic_last_version_selection(self):
        document = make_versioned_document()

        plan = plan_migration(
                OLD_SCHEMA_NESTED_VERSION, NEW_SCHEMA_NESTED_VERSION,
                document)

        # without an explicit decision the change stays pending; the
        # plan must not silently pick the last version
        self.assertEqual(len(plan.pending), 1)
        self.assertEqual(plan.pending[0].path, '/config')

        with self.assertRaises(MigrationError):
            plan.approve()

        with self.assertRaises(PlanNotApprovedError):
            plan.apply(document)

    def test_plan_summary_keeps_digests_and_discards(self):
        document = make_versioned_document()

        plan = plan_migration(
                OLD_SCHEMA_NESTED_VERSION, NEW_SCHEMA_NESTED_VERSION,
                document, select_versions={'/config': 0})

        summary = plan.summary()
        self.assertIn(plan.old_schema_digest, summary)
        self.assertIn(plan.new_schema_digest, summary)
        self.assertIn(plan.document_digest, summary)
        self.assertIn('discarded', summary)


class TestArrayMergeByIdMigration(unittest.TestCase):

    OLD_SCHEMA = {
        'type': 'object',
        'properties': {
            'items': {
                'type': 'array',
                'mergeStrategy': 'arrayMergeById',
                'mergeOptions': {'idRef': 'id'},
                'items': {
                    'type': 'object',
                    'properties': {
                        'id': {'type': 'integer'},
                        'v': {'type': 'string'},
                    },
                },
            },
        },
    }

    NEW_SCHEMA = {
        'type': 'object',
        'properties': {
            'items': {
                'type': 'array',
                'mergeStrategy': 'arrayMergeById',
                'mergeOptions': {'idRef': 'key'},
                'items': {
                    'type': 'object',
                    'properties': {
                        'key': {'type': 'integer'},
                        'v': {'type': 'string'},
                    },
                },
            },
        },
    }

    def make_document(self):
        merger = jsonmerge.Merger(self.OLD_SCHEMA)
        return merger.merge(None, {'items': [{'id': 1, 'v': 'a'},
                                             {'id': 2, 'v': 'b'}]})

    @staticmethod
    def rekey(items):
        return [{'key': item['id'], 'v': item['v']} for item in items]

    def test_idref_change_requires_transform(self):
        document = self.make_document()

        plan = plan_migration(self.OLD_SCHEMA, self.NEW_SCHEMA, document)

        self.assertEqual(len(plan.pending), 1)
        self.assertEqual(plan.pending[0].path, '/items')
        self.assertEqual(plan.pending[0].old_options, {'idRef': 'id'})
        self.assertEqual(plan.pending[0].new_options, {'idRef': 'key'})

        with self.assertRaises(MigrationError):
            plan.approve()

    def test_idref_change_with_transform(self):
        document = self.make_document()

        plan = plan_migration(
                self.OLD_SCHEMA, self.NEW_SCHEMA, document,
                transforms={'/items': self.rekey})

        self.assertEqual(plan.pending, [])
        self.assertEqual(plan.changes[0].kind, MigrationChange.KIND_TRANSFORM)

        plan.approve()
        migrated = plan.apply(document)

        self.assertEqual(migrated, {'items': [{'key': 1, 'v': 'a'},
                                              {'key': 2, 'v': 'b'}]})

        # next merge matches items on the new idRef
        merger = jsonmerge.Merger(self.NEW_SCHEMA)
        merged = merger.merge(migrated, {'items': [{'key': 2, 'v': 'B'},
                                                   {'key': 3, 'v': 'c'}]})
        self.assertEqual(merged, {'items': [{'key': 1, 'v': 'a'},
                                            {'key': 2, 'v': 'B'},
                                            {'key': 3, 'v': 'c'}]})


class TestPlanLifecycle(unittest.TestCase):

    def test_expired_plan_is_rejected(self):
        document = make_versioned_document()

        plan = plan_migration(
                OLD_SCHEMA_NESTED_VERSION, NEW_SCHEMA_NESTED_VERSION,
                document, select_versions={'/config': 0},
                ttl_seconds=0)

        plan.approve()
        with self.assertRaises(PlanExpiredError):
            plan.apply(document)

    def test_expiry_with_clock(self):
        document = make_versioned_document()
        now = [1000.0]

        plan = plan_migration(
                OLD_SCHEMA_NESTED_VERSION, NEW_SCHEMA_NESTED_VERSION,
                document, select_versions={'/config': 0},
                ttl_seconds=10, clock=lambda: now[0])

        plan.approve()
        now[0] = 1011.0
        with self.assertRaises(PlanExpiredError):
            plan.apply(document)

    def test_unapproved_plan_is_rejected(self):
        document = make_versioned_document()

        plan = plan_migration(
                OLD_SCHEMA_NESTED_VERSION, NEW_SCHEMA_NESTED_VERSION,
                document, select_versions={'/config': 0})

        with self.assertRaises(PlanNotApprovedError):
            plan.apply(document)

    def test_document_mismatch_is_rejected(self):
        document = make_versioned_document()

        plan = plan_migration(
                OLD_SCHEMA_NESTED_VERSION, NEW_SCHEMA_NESTED_VERSION,
                document, select_versions={'/config': 0})
        plan.approve()

        other = dict(document)
        other['name'] = 'changed'
        with self.assertRaises(MigrationError):
            plan.apply(other)

    def test_decision_for_unknown_path_is_rejected(self):
        document = make_versioned_document()

        with self.assertRaises(MigrationError):
            plan_migration(
                    OLD_SCHEMA_NESTED_VERSION, NEW_SCHEMA_NESTED_VERSION,
                    document, select_versions={'/nonexistent': 0})


class TestMigrationFailures(unittest.TestCase):

    def test_failed_transform_returns_no_partial_result(self):
        document = make_versioned_document()

        def bad_transform(value):
            raise ValueError('boom')

        plan = plan_migration(
                OLD_SCHEMA_NESTED_VERSION, NEW_SCHEMA_NESTED_VERSION,
                document, transforms={'/config': bad_transform})
        plan.approve()

        with self.assertRaises(MigrationError):
            plan.apply(document)

        # the document passed in was not half-migrated in place
        self.assertEqual(document['config'],
                         [{'value': {'a': 1}}, {'value': {'a': 2, 'b': 3}}])

    def test_selected_version_failing_new_schema_is_rejected(self):
        old_schema = {
            'type': 'object',
            'properties': {
                'config': {'mergeStrategy': 'version'},
            },
        }
        new_schema = {
            'type': 'object',
            'properties': {
                'config': {
                    'type': 'object',
                    'mergeStrategy': 'objectMerge',
                },
            },
        }

        merger = jsonmerge.Merger(old_schema)
        document = merger.merge(None, {'config': 'not-an-object'})

        plan = plan_migration(
                old_schema, new_schema, document,
                select_versions={'/config': 0})
        plan.approve()

        # the selected version is a string, which does not validate
        # against the new object schema
        with self.assertRaises(MigrationError):
            plan.apply(document)

    def test_selector_out_of_range_is_rejected(self):
        document = make_versioned_document()

        plan = plan_migration(
                OLD_SCHEMA_NESTED_VERSION, NEW_SCHEMA_NESTED_VERSION,
                document, select_versions={'/config': 5})
        plan.approve()

        with self.assertRaises(MigrationError):
            plan.apply(document)


if __name__ == '__main__':
    unittest.main()
