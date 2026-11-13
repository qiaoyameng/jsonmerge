# vim:ts=4 sw=4 expandtab softtabstop=4
"""Provenance tracking for jsonmerge.

This module implements :class:`ProvenanceWalk`, a variant of the regular
:class:`~jsonmerge.WalkInstance`.  It performs exactly one merge walk per
:meth:`Merger.merge_with_provenance` call and, while walking, records which
head document contributed every value in the merged document.

The public, JSON-serializable shape of a provenance tree is documented in
the README.  Internally nodes are represented by :class:`PNode`.  Nodes
only ever hold source identifiers and child nodes; they never hold
references to the caller's input objects, so mutating the inputs after a
merge cannot change the recorded provenance.
"""
import jsonmerge
from jsonmerge.jsonvalue import JSONValue


def _unescape_token(token):
    return token.replace('~1', '/').replace('~0', '~')


def parse_ref(ref):
    """Split a JSON pointer into unescaped key tokens."""
    if ref == '#' or ref == '':
        return []
    assert ref.startswith('#/')
    return [_unescape_token(tok) for tok in ref[2:].split('/')]


def align_items(old_nodes, appended_nodes, final_value):
    """Re-order provenance nodes to match a merged array's final ordering.

    ``old_nodes`` and ``appended_nodes`` are concatenated in exactly the
    same order in which the merge concatenated their values.  The merged
    array may then be re-sorted (e.g. via ``sortByRef``), so we re-align
    nodes by object identity.  Equal scalar values that share a position
    fall back to value equality, which is stable for the only reordering
    operation strategies perform (a deterministic sort).
    """
    combined = list(old_nodes) + list(appended_nodes)

    if [node.val for node in combined] == list(final_value):
        return combined

    # The array was reordered (a sortByRef sort).  Replay the same
    # permutation on the nodes: prefer object identity, then value
    # equality.  Positions already consumed are skipped so that equal
    # scalar values remain matched one-to-one and in order.
    aligned = []
    for value in final_value:
        candidates = [pos for pos, node in enumerate(combined) if node not in aligned]
        found = None
        for pos in candidates:
            if combined[pos].val is value:
                found = pos
                break
        if found is None:
            for pos in candidates:
                if combined[pos].val == value:
                    found = pos
                    break
        if found is None:
            raise RuntimeError("internal error: provenance item lost during re-alignment")
        aligned.append(combined[found])

    return aligned


class PNode(object):
    """One node of the provenance tree.

    A node is either a *leaf* (an adopted scalar or an indivisible custom
    strategy result), an *object* or an *array*.  Overwritten leaves may
    additionally keep a bounded list of ``history`` snapshots.
    """

    def __init__(self, ref, sources, node_type, value=None):
        self.ref = ref
        self.sources = list(sources)
        self.node_type = node_type
        self.val = value
        self.children = None
        self.history = None
        if node_type in ('object', 'array'):
            self.children = []

    def add_history(self, node, limit):
        if node is None or limit <= 0:
            return
        if self.history is None:
            self.history = []
        snapshot = node.to_dict()
        snapshot.pop('history', None)
        # Preserve chronological order: the previous node's older history
        # comes first, followed by the immediately preceding source.
        self.history = list(node.history or ()) + [snapshot]
        if limit is not None and len(self.history) > limit:
            del self.history[:-limit]

    def to_dict(self):
        rv = {
            'ref': self.ref,
            'type': self.node_type,
            'sources': list(self.sources),
        }
        if self.history:
            rv['history'] = list(self.history)
        if self.children is not None:
            rv['children'] = [child.to_dict() for child in self.children]
        return rv

    @classmethod
    def from_dict(cls, data, value=None):
        node = cls(
            ref=data['ref'],
            sources=data.get('sources', []),
            node_type=data['type'],
            value=value,
        )
        node.history = list(data.get('history', ()))
        if 'children' in data:
            node.children = [cls.from_dict(child) for child in data['children']]
        return node


def build_skeleton(value, ref, source_id):
    """Build a provenance subtree for a whole value adopted from one head.

    Used by strategies that adopt head values without descending into them
    (``append`` and the ``version`` history entries).  Containers get
    element-level structure, scalars become leaves.  ``None`` is a real
    leaf value; an actually missing key produces no node at all.
    """
    if isinstance(value, dict):
        node = PNode(ref, [source_id], 'object', value)
        for key, child_val in value.items():
            child_ref = ref + '/' + JSONValue('')._ref_escape(str(key))
            node.children.append(build_skeleton(child_val, child_ref, source_id))
        return node
    elif isinstance(value, list):
        node = PNode(ref, [source_id], 'array', value)
        for index, child_val in enumerate(value):
            child_ref = '%s/%d' % (ref, index)
            node.children.append(build_skeleton(child_val, child_ref, source_id))
        return node
    else:
        return PNode(ref, [source_id], 'leaf', value)


def rebase_refs(node, ref):
    """Rewrite a subtree's refs so that it is rooted at ``ref``.

    Only the path depth is preserved: array elements are re-numbered by
    their output position, since head-side indices are not output indices
    after an append or arrayMergeById insertion.
    """
    node.ref = ref
    if node.children:
        for index, child in enumerate(node.children):
            token = parse_ref(child.ref)[-1] if parse_ref(child.ref) else str(index)
            if node.node_type == 'array':
                token = str(index)
            rebase_refs(child, ref + '/' + JSONValue('')._ref_escape(token))


def collect_sources(node):
    seen = []
    if node is not None:
        for source in node.sources:
            if source not in seen:
                seen.append(source)
        if node.children:
            for child in node.children:
                for source in collect_sources(child):
                    if source not in seen:
                        seen.append(source)
    return seen


class ProvenanceTracker(object):
    """Holds state spanning one or more merge_with_provenance calls."""

    def __init__(self, history_limit=10, base_document=None):
        self.history_limit = history_limit
        self.root = None
        self._index = None
        self.base_document = base_document

    def index(self):
        if self._index is None and self.root is not None:
            self._index = {}
            self._index_node(self.root)
        return self._index

    def _index_node(self, node):
        self._index[node.ref] = node
        if node.children:
            for child in node.children:
                self._index_node(child)

    def invalidate(self):
        self._index = None

    @staticmethod
    def _value_at(document, ref):
        tokens = parse_ref(ref)
        current = document
        for token in tokens:
            if isinstance(current, dict):
                if token not in current:
                    return None, False
                current = current[token]
            elif isinstance(current, list):
                try:
                    index = int(token)
                except ValueError:
                    return None, False
                if index >= len(current):
                    return None, False
                current = current[index]
            else:
                return None, False
        return current, True

    def lookup(self, ref):
        if self._index is None:
            self.index()
        if self._index is None:
            return None
        node = self.index().get(ref)
        if node is None:
            return None
        value, found = self._value_at(self.base_document, ref)
        rehydrated = PNode.from_dict(node.to_dict(), value=value if found else node.val)
        self._rebind_children(rehydrated, value if found else None)
        return rehydrated

    def _rebind_children(self, node, value):
        if not node.children:
            return
        for child in node.children:
            tokens = parse_ref(child.ref)
            token = tokens[-1] if tokens else None
            child_value = child.val
            if isinstance(value, dict) and token in value:
                child_value = value[token]
            elif isinstance(value, list):
                try:
                    index = int(token)
                except (TypeError, ValueError):
                    index = None
                if index is not None and index < len(value):
                    child_value = value[index]
            child.val = child_value
            self._rebind_children(child, child_value)

    def set_root(self, node):
        self.root = node
        self.invalidate()


class Context(object):
    """Bookkeeping for one strategy invocation on the merge walk."""

    def __init__(self, walk, base, head, schema, ref, source_id,
                 incoming, history_limit, **options):
        self.walk = walk
        self.base = base
        self.head = head
        self.schema = schema
        self.ref = ref
        self.source_id = source_id
        self.incoming = incoming
        self.history_limit = history_limit
        self.options = options
        self.attached = []

    def base_node(self):
        # Look up by ref first: an explicit JSON null is a real value even
        # though JSONValue.get() represents it as an undefined sub-value.
        return self.incoming.lookup(self.base.ref)

    def previous_children(self):
        previous = self.base_node()
        if previous is not None and previous.children:
            return list(previous.children)
        return []

    def attach(self, key, node):
        self.attached.append((key, node))

    def on_child(self, node):
        """Receive a finished child node from the next deeper work() call."""
        tokens = parse_ref(node.ref)
        self.attach(tokens[-1] if tokens else None, node)

    def finalize(self, result_value):
        raise NotImplementedError


class ObjectMergeCtx(Context):

    def finalize(self, result_value):
        node = PNode(self.ref, [], 'object', result_value)

        by_key = {}
        for key, child in self.attached:
            by_key[key] = child

        if isinstance(result_value, dict):
            for key in result_value:
                child = by_key.get(key)
                if child is None:
                    # A key untouched by this head: re-adopt its previous
                    # provenance, keyed by the output location.
                    base_ref = self.ref + '/' + JSONValue('')._ref_escape(str(key))
                    child = self.incoming.lookup(base_ref)
                    if child is None:
                        continue
                    child.ref = base_ref
                node.children.append(child)

        for child in node.children:
            for source in collect_sources(child):
                if source not in node.sources:
                    node.sources.append(source)
        return node


class OverwriteCtx(Context):

    def finalize(self, result_value):
        previous = self.base_node()
        if self.head.is_undef():
            if previous is not None:
                previous.ref = self.ref
                return previous
            return PNode(self.ref, [], 'leaf', None)

        # An overwrite that does not actually change the value keeps the
        # provenance of the document that first supplied that value.  This
        # mirrors how version treats duplicate values and avoids attributing
        # an unchanged field to every later batch.
        if previous is not None and previous.val == self.head.val:
            previous.ref = self.ref
            return previous

        node = build_skeleton(self.head.val, self.ref, self.source_id)
        if previous is not None:
            node.add_history(previous, self.history_limit)
        return node


class DiscardCtx(Context):

    def finalize(self, result_value):
        previous = self.base_node()
        keep_if_undef = self.options.get('keepIfUndef', False)
        if previous is not None:
            previous.ref = self.ref
            return previous
        if keep_if_undef and not self.head.is_undef():
            return build_skeleton(self.head.val, self.ref, self.source_id)
        return PNode(self.ref, [], 'leaf', result_value)


class AppendCtx(Context):

    def finalize(self, result_value):
        node = PNode(self.ref, [], 'array', result_value)

        old_children = self.previous_children()

        appended = []
        if not self.head.is_undef():
            head_ref = self.head.ref if self.head.ref != '#' else self.ref
            for index, value in enumerate(self.head.val):
                child_ref = '%s/%d' % (head_ref, index)
                appended.append(build_skeleton(value, child_ref, self.source_id))

        combined = align_items(old_children, appended, result_value)
        for index, child in enumerate(combined):
            rebase_refs(child, '%s/%d' % (self.ref, index))
        node.children = combined
        for child in node.children:
            for source in child.sources:
                if source not in node.sources:
                    node.sources.append(source)
        return node


class ArrayMergeByIdCtx(Context):

    def __init__(self, *args, **kwargs):
        self.strategy_name = kwargs.pop('strategy_name', 'arrayMergeById')
        super(ArrayMergeByIdCtx, self).__init__(*args, **kwargs)
        self.id_ref = self.options.get('idRef', 'id')
        self.ignore_id = self.options.get('ignoreId', None)
        self.base_keys = self._collect_base_keys()

    def _key_strategy(self):
        return self.walk.merger.strategies[self.strategy_name]

    def _collect_base_keys(self):
        keys = {}
        if self.base.is_undef():
            return keys
        strategy = self._key_strategy()
        for index, key, _ in strategy.iter_index_key_item(self.walk, self.base, self.id_ref):
            keys[key] = index
        return keys

    def on_child(self, node):
        # Head indices are not output indices.  arrayMergeById replaces an
        # element when its id already exists in the base array, otherwise it
        # appends a new element.
        index = int(parse_ref(node.ref)[-1])
        if self.strategy_name == 'arrayMergeByIndex':
            base_length = 0 if self.base.is_undef() else len(self.base.val)
            if index < base_length:
                self.attach(('replace', index), node)
            else:
                self.attach(('append', index), node)
            return

        head_item = self.head[index]
        key = self._key_strategy().get_key(self.walk, head_item, self.id_ref)
        if key == self.ignore_id:
            return
        if key in self.base_keys:
            self.attach(('replace', self.base_keys[key]), node)
        else:
            self.attach(('append', index), node)

    def attach(self, key, node):
        kind, index = key
        if kind == 'append':
            index = self._next_append_index()
        self.attached.append((kind, index, node))

    def _next_append_index(self):
        base_count = 0
        if not self.base.is_undef():
            base_count = len(self.base.val)
        appended = sum(1 for kind, _, _ in self.attached if kind == 'append')
        return base_count + appended

    def finalize(self, result_value):
        node = PNode(self.ref, [], 'array', result_value)

        old_children = self.previous_children()
        working = list(old_children)

        # Replay the replaces and appends recorded during the walk.
        pre_values = [child.val for child in old_children]
        replayed = list(pre_values)
        for kind, index, child in self.attached:
            if kind == 'replace':
                working[index] = child
                replayed[index] = child.val
            else:
                working.append(child)
                replayed.append(child.val)

        if replayed == list(result_value):
            node.children = working
        else:
            node.children = align_items(
                old_children, working[len(old_children):], result_value)

        for index, child in enumerate(node.children):
            rebase_refs(child, '%s/%d' % (self.ref, index))

        for child in node.children:
            for source in child.sources:
                if source not in node.sources:
                    node.sources.append(source)
        return node


class VersionCtx(Context):

    def finalize(self, result_value):
        node = PNode(self.ref, [], 'array', result_value)

        old_children = self.previous_children()

        # A new entry is appended only when the head value differs from the
        # last recorded value (see Version.merge).  Everything else in the
        # result array is carried over from the previous version array, so
        # the number of surviving old entries is the result length minus one
        # when an entry was appended, and the full length otherwise.
        head_appended = self._head_appended(result_value)
        if head_appended:
            kept_count = len(result_value) - 1
        else:
            kept_count = len(result_value)
        kept = old_children[-kept_count:] if kept_count else []

        new_nodes = []
        if head_appended and not self.head.is_undef():
            new_entries = result_value[kept_count:]
            start_index = kept_count
            for offset, entry in enumerate(new_entries):
                entry_ref = '%s/%d' % (self.ref, start_index + offset)
                entry_node = build_skeleton(entry, entry_ref, self.source_id)
                new_nodes.append(entry_node)

        node.children = kept + new_nodes

        for index, child in enumerate(node.children):
            rebase_refs(child, '%s/%d' % (self.ref, index))

        for child in node.children:
            for source in child.sources:
                if source not in node.sources:
                    node.sources.append(source)
        return node

    def _head_appended(self, result_value):
        if self.head.is_undef():
            return False
        if self.base.is_undef():
            return len(result_value) > 0
        # Version.merge appends iff the head differs from the value wrapped
        # in the last existing version entry.  Mirror that exact condition so
        # it works regardless of 'limit' trimming.
        base_entries = self.base.val
        if not base_entries:
            return len(result_value) > 0
        last_value = base_entries[-1].get('value')
        return last_value != self.head.val


class CoarseCtx(Context):
    """Fallback for strategies that cannot report fine-grained adoption."""

    def finalize(self, result_value):
        sources = []
        previous = self.base_node()
        if previous is not None:
            for source in collect_sources(previous):
                if source not in sources:
                    sources.append(source)
        if not self.head.is_undef() and self.source_id not in sources:
            sources.append(self.source_id)

        return PNode(self.ref, sources, 'leaf', result_value)


class ProvenanceWalk(jsonmerge.WalkInstance):
    """A WalkInstance that builds a provenance tree alongside the merge."""

    def __init__(self, merger, base, head, merge_options, source_id, tracker):
        super(ProvenanceWalk, self).__init__(merger, base, head, merge_options)
        self.source_id = source_id
        self.tracker = tracker
        self.stack = []

    def _make_context(self, name, base, head, schema, kwargs):
        ref = head.ref if not head.is_undef() else base.ref
        common = dict(
            walk=self,
            base=base,
            head=head,
            schema=schema,
            ref=ref,
            source_id=self.source_id,
            incoming=self.tracker,
            history_limit=self.tracker.history_limit,
        )

        if name == 'objectMerge':
            return ObjectMergeCtx(**common)
        elif name == 'overwrite':
            return OverwriteCtx(**common)
        elif name == 'discard':
            return DiscardCtx(**common, keepIfUndef=kwargs.get('keepIfUndef', False))
        elif name == 'append':
            return AppendCtx(**common)
        elif name in ('arrayMergeById', 'arrayMergeByIndex'):
            return ArrayMergeByIdCtx(strategy_name=name, **common)
        elif name == 'version':
            return VersionCtx(**common)
        else:
            return CoarseCtx(**common)

    def work(self, strategy, schema, base, head, **kwargs):
        name = None
        for registered_name, registered in self.merger.strategies.items():
            if registered is strategy:
                name = registered_name
                break

        ctx = self._make_context(name, base, head, schema, kwargs)
        self.stack.append(ctx)
        try:
            result = strategy.merge(self, base, head, schema,
                                    objclass_menu=self.merger.objclass_menu, **kwargs)
        finally:
            finished = self.stack.pop()
        assert finished is ctx

        node = ctx.finalize(result.val)

        if self.stack:
            parent = self.stack[-1]
            parent.on_child(node)
        else:
            self.tracker.set_root(node)

        return result
