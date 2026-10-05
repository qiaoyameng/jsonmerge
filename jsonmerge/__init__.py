# vim:ts=4 sw=4 expandtab softtabstop=4
from collections import OrderedDict
from jsonmerge.jsonvalue import JSONValue
from jsonmerge.resolver import LocalRefResolver
from jsonmerge import strategies
from jsonmerge import descenders
from jsonmerge.exceptions import SchemaError, JSONMergeError
from jsonschema.validators import Draft4Validator
import logging
import re
import warnings

log = logging.getLogger(name=__name__)

#logging.basicConfig(level=logging.DEBUG)

class Walk(object):

    DESCENDERS = [
            descenders.Ref,
            descenders.OneOf,
            descenders.AnyOfAllOf,
    ]

    def __init__(self, merger, merge_options):
        self.merger = merger
        self.merge_options = merge_options
        self.resolver = merger.validator.resolver
        self.lvl = -1

        self.descenders = [ cls() for cls in self.DESCENDERS ]

    def _indent(self):
        return "  " * self.lvl

    def is_type(self, instance, type):
        """Check if instance if a specific JSON type."""
        assert isinstance(instance, JSONValue)

        if instance.is_undef():
            return False

        return self.merger.validator.is_type(instance.val, type)

    def descend(self, schema, *args):
        assert isinstance(schema, JSONValue)
        self.lvl += 1

        log.debug("descend: %sschema %s" % (self._indent(), schema.ref,))

        if not schema.is_undef():
            if not schema.synthetic:
                with self.resolver.resolving(schema.ref) as resolved:
                    assert schema.val is resolved

        # backwards compatibility jsonmerge<=1.6.0
        opts = {'meta': None}

        if not schema.is_undef():

            for descender in self.descenders:
                rv = self.call_descender(descender, schema, *args)
                if rv is not None:
                    self.lvl -= 1
                    return rv

            name = schema.val.get("mergeStrategy")

            for v in (
                    self.merge_options.get(name),
                    schema.val.get("mergeOptions")):
                if v is not None:
                    opts.update(v)
        else:
            name = None

        if name is None:
            name = self.default_strategy(schema, *args, **opts)

        log.debug("descend: %sinvoke strategy %s" % (self._indent(), name))

        try:
            strategy = self.merger.strategies[name]
        except KeyError:
            raise SchemaError("Unknown strategy '%s'" % name, schema)

        try:
            rv = self.work(strategy, schema, *args, **opts)
        except JSONMergeError as exc:
            if exc.strategy_name is None:
                exc.strategy_name = name
            raise

        self.lvl -= 1
        return rv

    def get_property_subschema(self, schema, key, *args):
        """Resolve the subschema that applies to an object property.

        schema -- Schema of the object (JSONValue), possibly containing
            'properties', 'patternProperties' and 'additionalProperties'
            keywords.
        key -- Name of the property in the instance.
        args -- Extra arguments for default_strategy() (base and head
            for WalkInstance, none for WalkSchema).

        All rules from 'properties' and 'patternProperties' that match
        the property name are collected. Rules that use the same merge
        strategy with compatible merge options are coalesced into a
        single subschema, so that the result does not depend on the
        declaration order in the schema. If matching rules conflict,
        SchemaError is raised reporting the location of every
        conflicting rule. 'additionalProperties' is only used when no
        rule matches the property name.
        """
        if schema.is_undef():
            return JSONValue(undef=True)

        matches = []

        properties = schema.get('properties')
        if not properties.is_undef():
            subschema = properties.get(key)
            if not subschema.is_undef():
                matches.append(subschema)

        patterns = schema.get('patternProperties')
        if not patterns.is_undef():
            for pattern, subschema in sorted(patterns.items(), key=lambda item: item[0]):
                if re.search(pattern, key):
                    matches.append(subschema)

        if not matches:
            additional = schema.get('additionalProperties')
            # additionalProperties can be boolean in draft 4
            if not additional.is_undef() and self.is_type(additional, "object"):
                return additional
            return JSONValue(undef=True)

        if len(matches) == 1:
            return matches[0]

        return self._coalesce_property_rules(schema, key, matches, *args)

    def _coalesce_property_rules(self, schema, key, matches, *args):
        """Coalesce multiple matching property rules into one subschema.

        Raises SchemaError if the matching rules require conflicting
        merge strategies or incompatible merge options.
        """
        rules = []
        for subschema in matches:
            name = subschema.val.get("mergeStrategy")
            explicit = name is not None
            if not explicit:
                name = self.default_strategy(subschema, *args)
            options = subschema.val.get("mergeOptions")
            if options is None:
                options = {}
            rules.append((subschema, name, options, explicit))

        names = set(name for _, name, _, _ in rules)
        if len(names) > 1:
            details = ", ".join(
                    "'%s' at %s" % (name, subschema.ref)
                    for subschema, name, _, _ in rules)
            raise SchemaError(
                    "Conflicting merge strategies for property '%s': %s" % (key, details),
                    schema)

        name = rules[0][1]

        options = {}
        for subschema, _, sub_options, _ in rules:
            for opt_name, opt_value in sub_options.items():
                if opt_name in options and options[opt_name] != opt_value:
                    details = ", ".join(s.ref for s, _, _, _ in rules)
                    raise SchemaError(
                            "Conflicting merge options for property '%s' ('%s' merge "
                            "strategy): option '%s' differs between rules at %s" % (
                                key, name, opt_name, details),
                            schema)
                options[opt_name] = opt_value

        # Merge the matching subschemas into one. Iteration is ordered
        # by schema location so that the result does not depend on the
        # declaration order in the schema.
        ordered = sorted(matches, key=lambda subschema: subschema.ref)

        merged = {}
        for subschema in ordered:
            merged.update(subschema.val)

        if any(explicit for _, _, _, explicit in rules):
            merged["mergeStrategy"] = name
        else:
            merged.pop("mergeStrategy", None)

        if options:
            merged["mergeOptions"] = options
        else:
            merged.pop("mergeOptions", None)

        return JSONValue(merged, ordered[0].ref, synthetic=True)

class WalkInstance(Walk):

    def __init__(self, merger, base, head, merge_options):
        Walk.__init__(self, merger, merge_options)
        self.base_resolver = LocalRefResolver("", base.val)
        self.head_resolver = LocalRefResolver("", head.val)

    def default_strategy(self, schema, base, head, **kwargs):
        log.debug("       : %sdefault strategy" % (self._indent(),))

        # A different (better?) behavior would be to select default strategy
        # based on head and base like this (see test_merge_default_type_mismatch)
        #
        #if self.is_type(head, "object") and (base.is_undef() or self.is_type(base, "object")):

        if self.is_type(head, "object"):
            return "objectMerge"
        else:
            return "overwrite"

    def call_descender(self, descender, schema, base, head):
        return descender.descend_instance(self, schema, base, head)

    def work(self, strategy, schema, base, head, **kwargs):
        assert isinstance(schema, JSONValue)
        assert isinstance(base, JSONValue)
        assert isinstance(head, JSONValue)

        log.debug("work   : %sbase %s, head %s" % (self._indent(), base.ref, head.ref))

        if not base.is_undef():
            with self.base_resolver.resolving(base.ref) as resolved:
                assert base.val is resolved

        if not head.is_undef():
            with self.head_resolver.resolving(head.ref) as resolved:
                assert head.val is resolved

        rv = strategy.merge(self, base, head, schema, objclass_menu=self.merger.objclass_menu, **kwargs)

        assert isinstance(rv, JSONValue)
        return rv

class WalkSchema(Walk):

    def is_base_context(self):
        return self.resolver.base_uri == self.merger.schema.get('id', '')

    def resolve_refs(self, schema):
        # For backwards compatibility with jsonmerge <= 1.3.0
        return schema

    def resolve_subschema_option_refs(self, subschema):
        # This is kind of ugly - schema for meta data
        # can again contain references to external schemas.
        #
        # Since we already have in place all the machinery
        # to resolve these references in the merge schema,
        # we (ab)use it here to do the same for meta data
        # schema.
        m = Merger(subschema)
        m.validator.resolver.store.update(self.resolver.store)

        w = WalkSchema(m, merge_options={})
        subschema = w._resolve_refs(JSONValue(subschema), resolve_base=True).val

        return subschema

    def _resolve_refs(self, schema, resolve_base=False):
        assert isinstance(schema, JSONValue)

        if (not resolve_base) and self.is_base_context():
            # no need to resolve refs in the context of the original schema - they 
            # are still valid
            return schema
        elif self.is_type(schema, "array"):
            return JSONValue([ self._resolve_refs(v).val for v in schema ], schema.ref)
        elif self.is_type(schema, "object"):
            ref = schema.val.get("$ref")
            if ref is not None:
                with self.resolver.resolving(ref) as resolved:
                    return self._resolve_refs(JSONValue(resolved, ref))
            else:
                return JSONValue(dict( ((k, self._resolve_refs(v).val) for k, v in schema.items()) ), schema.ref)
        else:
            return schema

    def schema_is_object(self, schema):

        objonly = (
                'maxProperties',
                'minProperties',
                'required',
                'additionalProperties',
                'properties',
                'patternProperties',
                'dependencies')

        for k in objonly:
            if k in schema.val:
                return True

        if schema.val.get('type') == 'object':
            return True

        return False

    def default_strategy(self, schema, **kwargs):

        if self.schema_is_object(schema):
            return "objectMerge"
        else:
            return "overwrite"

    def call_descender(self, descender, schema):
        return descender.descend_schema(self, schema)

    def work(self, strategy, schema, **kwargs):
        assert isinstance(schema, JSONValue)

        schema = JSONValue(dict(schema.val), schema.ref)
        schema.val.pop("mergeStrategy", None)
        schema.val.pop("mergeOptions", None)

        rv = strategy.get_schema(self, schema, **kwargs)
        assert isinstance(rv, JSONValue)
        return rv

class Merger(object):

    STRATEGIES = {
        "discard": strategies.Discard(),
        "overwrite": strategies.Overwrite(),
        "version": strategies.Version(),
        "append": strategies.Append(),
        "objectMerge": strategies.ObjectMerge(),
        "arrayMergeById": strategies.ArrayMergeById(),
        "arrayMergeByIndex": strategies.ArrayMergeByIndex(),
    }

    def __init__(self, schema, strategies=(), objclass_def='dict', objclass_menu=None,
            validatorclass=Draft4Validator, strict_strategy_resolution=False):
        """Create a new Merger object.

        schema -- JSON schema to use when merging.
        strategies -- Any additional merge strategies to use during merge.
        objclass_def -- Name of the default class for JSON objects.
        objclass_menu -- Any additional classes for JSON objects.
        validatorclass -- JSON Schema validator class.

        strategies argument should be a dict mapping strategy names to
        instances of Strategy subclasses.

        objclass_def specifies the default class used for JSON objects when one
        is not specified in the schema. It should be 'dict' (dict built-in),
        'OrderedDict' (collections.OrderedDict) or one of the names specified
        in the objclass_menu argument. If not specified, 'dict' is used.

        objclass_menu argument should be a dictionary that maps a string name
        to a function or class that will return an empty dictionary-like object
        to use as a JSON object. The function must accept either no arguments
        or a dictionary-like object.

        validatorclass argument can be used to supply a validator class from
        jsonschema. This can be used for example to specify which JSON Schema
        draft version will be used during merge.

        If strict_strategy_resolution is true, all schema rules that match
        an object property (from 'properties' and 'patternProperties') are
        collected and checked for consistency before merging. Matching
        rules with the same merge strategy and compatible merge options
        are coalesced, so the result does not depend on the declaration
        order in the schema. Conflicting rules raise a SchemaError that
        reports the location of every conflicting rule before anything is
        merged. By default (False), the last matching rule silently wins.
        """

        self.schema = schema
        self.strict_strategy_resolution = strict_strategy_resolution

        if hasattr(validatorclass, 'ID_OF'):
            resolver = LocalRefResolver.from_schema(schema, id_of=validatorclass.ID_OF)
        else:
            # jsonschema<3.0.0
            resolver = LocalRefResolver.from_schema(schema)
        self.validator = validatorclass(schema, resolver=resolver)

        self.strategies = dict(self.STRATEGIES)
        self.strategies.update(strategies)

        self.objclass_menu = { 'dict': dict, 'OrderedDict': OrderedDict }
        if objclass_menu:
            self.objclass_menu.update(objclass_menu)

        self.objclass_menu['_default'] = self.objclass_menu[objclass_def]

    def cache_schema(self, schema, uri=None):
        """Cache an external schema reference.

        schema -- JSON schema to cache
        uri -- Optional URI for the schema

        If the JSON schema for merging contains external references, they will
        be fetched using HTTP from their respective URLs. Alternatively, this
        method can be used to pre-populate the cache with any external schemas
        that are already known.

        If URI is omitted, it is obtained from the schema itself ('id' or '$id'
        keyword, depending on the JSON Schema draft used)
        """

        if uri is None:
            if hasattr(self.validator, 'ID_OF'):
                uri = self.validator.ID_OF(schema)
            else:
                # jsonschema<3.0.0
                uri = schema.get('id', '')

        self.validator.resolver.store.update(((uri, schema),))

    def merge(self, base, head, meta=None, merge_options=None):
        """Merge head into base.

        base -- Old JSON document you are merging into.
        head -- New JSON document for merging into base.
        merge_options -- Optional dictionary with merge options.

        Keys of merge_options must be names of the strategies. Values must be
        dictionaries of merge options as in the mergeOptions schema element.
        Options in merge_options are applied to all instances of a strategy.
        Values in schema override values given in merge_options.

        Returns an updated base document
        """

        schema = JSONValue(self.schema)

        if base is None:
            base = JSONValue(undef=True)
        else:
            base = JSONValue(base)

        head = JSONValue(head)

        if merge_options is None:
            merge_options = {}

        # backwards compatibility jsonmerge<=1.6.0
        if meta is not None:
            warnings.warn("'meta' argument is deprecated. Please use "
                    "merge_options={'version': {'metadata': ...}}.",
                    DeprecationWarning, 2)

            merge_options['version'] = { 'metadata': meta }

        walk = WalkInstance(self, base, head, merge_options)
        return walk.descend(schema, base, head).val

    def get_schema(self, meta=None, merge_options=None):
        """Get JSON schema for the merged document.

        merge_options -- Optional dictionary with merge options.

        Keys of merge_options must be names of the strategies. Values must be
        dictionaries of merge options as in the mergeOptions schema element.
        Options in merge_options are applied to all instances of a strategy.
        Values in schema override values given in merge_options.

        Returns a JSON schema for documents returned by the
        merge() method.
        """

        if merge_options is None:
            merge_options = {}

        # backwards compatibility jsonmerge<=1.6.0
        if meta is not None:
            warnings.warn("'meta' argument is deprecated. Please use "
                    "merge_options={'version': {'metadataSchema': ...}}.",
                    DeprecationWarning, 2)
            merge_options['version'] = { 'metadataSchema': meta }

        schema = JSONValue(self.schema)

        walk = WalkSchema(self, merge_options)
        return walk.descend(schema).val

def merge(base, head, schema={}):
    """Merge two JSON documents using strategies defined in schema.

    base -- Old JSON document you are merging into.
    head -- New JSON document for merging into base.
    schema -- JSON schema to use when merging.

    Merge strategy for each value can be specified in the schema
    using the "mergeStrategy" keyword. If not specified, default
    strategy is to use "objectMerge" for objects and "overwrite"
    for all other types.
    """

    merger = Merger(schema)
    return merger.merge(base, head)
