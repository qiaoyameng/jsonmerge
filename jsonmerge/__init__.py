# vim:ts=4 sw=4 expandtab softtabstop=4
from collections import OrderedDict
from jsonmerge.jsonvalue import JSONValue
from jsonmerge.resolver import LocalRefResolver
from jsonmerge import strategies
from jsonmerge import descenders
from jsonmerge.exceptions import SchemaError, JSONMergeError, HeadInstanceError
from jsonschema.validators import Draft4Validator
import logging
import warnings
import re

log = logging.getLogger(name=__name__)

#logging.basicConfig(level=logging.DEBUG)

class Walk(object):

    DESCENDERS = [
            descenders.Ref,
            descenders.OneOf,
            descenders.AnyOfAllOf,
    ]

    def __init__(self, merger, merge_options, strict_strategy_resolution=False):
        self.merger = merger
        self.merge_options = merge_options
        self.strict_strategy_resolution = strict_strategy_resolution
        self.resolver = merger.validator.resolver
        self.lvl = -1

        self.descenders = [ cls() for cls in self.DESCENDERS ]

    def _resolve_ref_chain(self, schema):
        refs = []
        current = schema
        seen = set()

        while self.is_type(current, "object"):
            ref = current.val.get("$ref")
            if ref is None:
                break

            if ref in seen:
                break

            seen.add(ref)
            refs.append((ref, current.ref))

            with self.resolver.resolving(ref) as resolved:
                current = JSONValue(resolved, ref)

        return current, refs

    def _is_one_of_schema(self, schema):
        return self.is_type(schema, "object") and \
                not schema.get("oneOf").is_undef() and \
                schema.get("mergeStrategy").is_undef()

    def _one_of_is_valid(self, value, schema):
        if value.is_undef():
            return True

        validator = self.merger.validator
        if hasattr(validator, 'evolve'):
            errors = validator.evolve(schema=schema).iter_errors(value.val)
        else:
            errors = validator.iter_errors(value.val, schema)
        return not list(errors)

    def _select_one_of(self, schema, base, head):
        one_of = schema.get("oneOf")
        valid = []

        for i, subschema in enumerate(one_of):
            if self._one_of_is_valid(base, subschema) and \
                    self._one_of_is_valid(head, subschema):
                valid.append(i)

        if len(valid) == 0:
            raise HeadInstanceError(
                    "No element of 'oneOf' validates both base and head", head)

        if len(valid) > 1:
            raise HeadInstanceError(
                    "Multiple elements of 'oneOf' validate", head)

        return one_of[valid[0]]

    def _rule_options(self, schema):
        options = schema.val.get("mergeOptions", {})
        if options is None:
            options = {}

        if not self.is_type(JSONValue(options), "object"):
            raise SchemaError("'mergeOptions' does not contain an object",
                    schema)

        return options

    def _rule_location(self, rule):
        location = rule["source"].ref
        if rule["refs"]:
            location = location + " -> " + \
                    " -> ".join(ref for ref, source_ref in rule["refs"])
        return location

    def _describe_rules(self, rules):
        return ", ".join(
                "%s at %s" % (rule["strategy"], self._rule_location(rule))
                for rule in rules)

    def _build_effective_rule(self, rules, strategy, options):
        representative = rules[0]
        target = representative["resolved"]

        if not self.is_type(target, "object"):
            return representative["source"]

        target_strategy = target.val.get("mergeStrategy")
        target_options = target.val.get("mergeOptions", {})
        if target_options is None:
            target_options = {}

        if target_strategy == strategy and target_options == options:
            return representative["source"]

        effective = dict(target.val)
        effective.pop("$ref", None)
        effective["mergeStrategy"] = strategy
        if options:
            effective["mergeOptions"] = options
        else:
            effective.pop("mergeOptions", None)

        return JSONValue(effective, representative["resolved"].ref,
                resolved=True)

    def _combine_rules(self, rules, key):
        strategies = {}
        for rule in rules:
            strategies.setdefault(rule["strategy"], []).append(rule)

        if len(strategies) > 1:
            conflicting = []
            for strategy in sorted(strategies):
                conflicting.extend(strategies[strategy])

            raise SchemaError(
                    "Conflicting merge strategies for property %r: %s" %
                    (key, self._describe_rules(conflicting)),
                    conflicting[0]["source"])

        strategy = rules[0]["strategy"]
        if strategy not in self.merger.strategies:
            raise SchemaError("Unknown strategy '%s'" % strategy,
                    rules[0]["resolved"])

        option_values = {}
        for rule in rules:
            for name, value in rule["options"].items():
                if name in option_values:
                    old_value, old_rule = option_values[name]
                    if old_value != value:
                        raise SchemaError(
                                "Conflicting '%s' values for %s merge "
                                "strategy for property %r: %r at %s, "
                                "%r at %s" %
                                (name, strategy, key,
                                    old_value,
                                    self._rule_location(old_rule),
                                    value,
                                    self._rule_location(rule)),
                                rule["source"])
                else:
                    option_values[name] = (value, rule)

        options = dict(
                (name, value)
                for name, (value, rule) in sorted(option_values.items()))

        return self._build_effective_rule(rules, strategy, options)

    def _collect_property_rules(self, schema, key, base=None, head=None):
        rules = []

        def add_rule(source, kind):
            resolved, refs = self._resolve_ref_chain(source)

            while self._is_one_of_schema(resolved):
                if head is None:
                    return
                selected = self._select_one_of(resolved, base, head)
                resolved, selected_refs = self._resolve_ref_chain(selected)
                refs.extend(selected_refs)

            if not self.is_type(resolved, "object"):
                strategy = "overwrite"
                options = {}
            else:
                if resolved.get("allOf").is_undef() and \
                        resolved.get("anyOf").is_undef():
                    pass
                elif resolved.get("mergeStrategy").is_undef():
                    raise SchemaError(
                            "Can't descend to 'allOf' and 'anyOf' keywords",
                            resolved)

                strategy = resolved.val.get("mergeStrategy")
                options = self._rule_options(resolved)

                if strategy is None:
                    if head is None:
                        strategy = self.default_strategy(resolved)
                    else:
                        strategy = self.default_strategy(
                                resolved,
                                JSONValue(undef=True),
                                head)

            rules.append({
                "source": source,
                "resolved": resolved,
                "refs": refs,
                "strategy": strategy,
                "options": options,
                "kind": kind,
            })

        properties = schema.get("properties")
        if not properties.is_undef():
            property_schema = properties.get(key)
            if not property_schema.is_undef():
                add_rule(property_schema, "property")

        pattern_properties = schema.get("patternProperties")
        if not pattern_properties.is_undef():
            patterns = sorted(pattern_properties.val.keys())
            for pattern in patterns:
                if re.search(pattern, key):
                    add_rule(pattern_properties.get(pattern), "pattern")

        if not rules:
            additional_properties = schema.get("additionalProperties")
            if not additional_properties.is_undef() and \
                    self.is_type(additional_properties, "object"):
                add_rule(additional_properties, "additional")

        return rules

    def resolve_property_subschema(self, schema, key, base=None, head=None):
        if not self.strict_strategy_resolution:
            subschema = JSONValue(undef=True)

            properties = schema.get("properties")
            if not properties.is_undef():
                subschema = properties.get(key)

            if subschema.is_undef():
                pattern_properties = schema.get("patternProperties")
                if not pattern_properties.is_undef():
                    for pattern, pattern_schema in pattern_properties.items():
                        if re.search(pattern, key):
                            subschema = pattern_schema

            if subschema.is_undef():
                additional_properties = schema.get("additionalProperties")
                if not additional_properties.is_undef() and \
                        self.is_type(additional_properties, "object"):
                    subschema = additional_properties

            return subschema

        rules = self._collect_property_rules(schema, key, base, head)
        if not rules:
            return JSONValue(undef=True)

        return self._combine_rules(rules, key)

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
            if schema.resolved:
                self.resolver.push_scope(schema.ref)
                try:
                    rv = self._descend(schema, *args)
                finally:
                    self.resolver.pop_scope()
            else:
                with self.resolver.resolving(schema.ref) as resolved:
                    assert schema.val is resolved
                rv = self._descend(schema, *args)
        else:
            rv = self._descend(schema, *args)

        self.lvl -= 1
        return rv

    def _descend(self, schema, *args):

        # backwards compatibility jsonmerge<=1.6.0
        opts = {'meta': None}

        if not schema.is_undef():

            for descender in self.descenders:
                rv = self.call_descender(descender, schema, *args)
                if rv is not None:
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
        return rv

class WalkInstance(Walk):

    def __init__(self, merger, base, head, merge_options,
            strict_strategy_resolution=False):
        Walk.__init__(self, merger, merge_options,
                strict_strategy_resolution)
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

        self.prepare_merge(schema, base, head)

        rv = strategy.merge(self, base, head, schema, objclass_menu=self.merger.objclass_menu, **kwargs)

        assert isinstance(rv, JSONValue)
        return rv

    def prepare_merge(self, schema, base, head):
        if not self.strict_strategy_resolution or schema.is_undef():
            return

        resolved, refs = self._resolve_ref_chain(schema)

        if self._is_one_of_schema(resolved):
            selected = self._select_one_of(resolved, base, head)
            self.prepare_merge(selected, base, head)
            return

        if not self.is_type(resolved, "object"):
            return

        if not resolved.get("allOf").is_undef() or \
                not resolved.get("anyOf").is_undef():
            if resolved.get("mergeStrategy").is_undef():
                raise SchemaError(
                        "Can't descend to 'allOf' and 'anyOf' keywords",
                        resolved)

        name = resolved.val.get("mergeStrategy")
        if name is None:
            name = self.default_strategy(resolved, base, head)

        if name not in self.merger.strategies:
            raise SchemaError("Unknown strategy '%s'" % name, resolved)

        strategy = self.merger.strategies[name]

        if isinstance(strategy, strategies.ObjectMerge):
            if self.is_type(head, "object"):
                for key in sorted(head.val, key=lambda value: str(value)):
                    subschema = self.resolve_property_subschema(
                            resolved, key,
                            base.get(key) if not base.is_undef() and
                            self.is_type(base, "object") else JSONValue(undef=True),
                            head.get(key))
                    if not subschema.is_undef():
                        child_base = base.get(key) \
                                if not base.is_undef() and \
                                self.is_type(base, "object") and \
                                key in base.val else JSONValue(undef=True)
                        self.prepare_merge(
                                subschema, child_base, head.get(key))
        elif isinstance(strategy, strategies.ArrayMergeById):
            if self.is_type(head, "array"):
                subschema = resolved.get("items")
                if self.is_type(subschema, "array"):
                    raise SchemaError(
                            "This strategy is not supported when 'items' is "
                            "an array", subschema)
                if not subschema.is_undef():
                    for item in head:
                        self.prepare_merge(
                                subschema, JSONValue(undef=True), item)

class WalkSchema(Walk):

    def __init__(self, merger, merge_options, strict_strategy_resolution=False):
        Walk.__init__(self, merger, merge_options,
                strict_strategy_resolution)

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
        m = Merger(subschema,
                strict_strategy_resolution=self.strict_strategy_resolution)
        m.validator.resolver.store.update(self.resolver.store)

        w = WalkSchema(m, merge_options={},
                strict_strategy_resolution=self.strict_strategy_resolution)
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

    def prepare_get_schema(self, schema, active_refs=None):
        if not self.strict_strategy_resolution or schema.is_undef():
            return

        resolved, refs = self._resolve_ref_chain(schema)
        ref = refs[-1][0] if refs else None
        if schema.resolved and ref is None and schema.ref != '#':
            ref = schema.ref

        active_refs = set(active_refs or ())
        if ref is not None and ref in active_refs:
            return
        if ref is not None:
            active_refs.add(ref)

        if self._is_one_of_schema(resolved):
            for branch in resolved.get("oneOf"):
                self.prepare_get_schema(branch, active_refs)
            return

        if not self.is_type(resolved, "object"):
            return

        if not resolved.get("allOf").is_undef() or \
                not resolved.get("anyOf").is_undef():
            if resolved.get("mergeStrategy").is_undef():
                raise SchemaError(
                        "Can't descend to 'allOf' and 'anyOf' keywords",
                        resolved)

        name = resolved.val.get("mergeStrategy")
        if name is None:
            name = self.default_strategy(resolved)

        if name not in self.merger.strategies:
            raise SchemaError("Unknown strategy '%s'" % name, resolved)

        strategy = self.merger.strategies[name]

        if isinstance(strategy, strategies.ObjectMerge):
            properties = resolved.get("properties")
            if not properties.is_undef():
                for key in sorted(properties.val, key=lambda value: str(value)):
                    effective = self.resolve_property_subschema(
                            resolved, key, head=None)
                    if not effective.is_undef():
                        self.prepare_get_schema(effective, active_refs)

            pattern_properties = resolved.get("patternProperties")
            if not pattern_properties.is_undef():
                for pattern in sorted(pattern_properties.val):
                    self.prepare_get_schema(
                            pattern_properties.get(pattern), active_refs)

            additional_properties = resolved.get("additionalProperties")
            if not additional_properties.is_undef() and \
                    self.is_type(additional_properties, "object"):
                self.prepare_get_schema(additional_properties, active_refs)

        elif isinstance(strategy, strategies.ArrayMergeById):
            subschema = resolved.get("items")
            if self.is_type(subschema, "array"):
                raise SchemaError(
                        "This strategy is not supported when 'items' is "
                        "an array", subschema)
            if not subschema.is_undef():
                self.prepare_get_schema(subschema, active_refs)

    def work(self, strategy, schema, **kwargs):
        assert isinstance(schema, JSONValue)

        self.prepare_get_schema(schema)

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
            validatorclass=Draft4Validator,
            strict_strategy_resolution=False):
        """Create a new Merger object.

        schema -- JSON schema to use when merging.
        strategies -- Any additional merge strategies to use during merge.
        objclass_def -- Name of the default class for JSON objects.
        objclass_menu -- Any additional classes for JSON objects.
        validatorclass -- JSON Schema validator class.
        strict_strategy_resolution -- Require all matching property rules to be
        compatible.

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

        strict_strategy_resolution enables collection of every properties and
        patternProperties rule matching a key. Compatible rules are combined;
        contradictory rules raise SchemaError before objects are modified.
        """

        self.schema = schema

        if hasattr(validatorclass, 'ID_OF'):
            resolver = LocalRefResolver.from_schema(schema, id_of=validatorclass.ID_OF)
        else:
            # jsonschema<3.0.0
            resolver = LocalRefResolver.from_schema(schema)
        self.validator = validatorclass(schema, resolver=resolver)

        self.strategies = dict(self.STRATEGIES)
        self.strategies.update(strategies)
        self.strict_strategy_resolution = strict_strategy_resolution

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

        walk = WalkInstance(self, base, head, merge_options,
                self.strict_strategy_resolution)
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

        walk = WalkSchema(self, merge_options,
                self.strict_strategy_resolution)
        return walk.descend(schema).val

def merge(base, head, schema={}, strict_strategy_resolution=False):
    """Merge two JSON documents using strategies defined in schema.

    base -- Old JSON document you are merging into.
    head -- New JSON document for merging into base.
    schema -- JSON schema to use when merging.

    Merge strategy for each value can be specified in the schema
    using the "mergeStrategy" keyword. If not specified, default
    strategy is to use "objectMerge" for objects and "overwrite"
    for all other types.
    """

    merger = Merger(schema,
            strict_strategy_resolution=strict_strategy_resolution)
    return merger.merge(base, head)
