# vim:ts=4 sw=4 expandtab softtabstop=4
import json
import heapq
from jsonmerge.exceptions import SchemaError
from jsonmerge.jsonvalue import JSONValue


def canonical_id(value):
    """Return a type-aware, hashable token for a JSON item ID.

    JSON values of different JSON types must not compare equal here.  For
    example, the integer 1 and the boolean true are distinct IDs even though
    Python's ``==`` considers them equal.
    """
    try:
        encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
                allow_nan=True)
    except (TypeError, ValueError):
        encoded = repr(value)

    return '%s:%s' % (type(value).__name__, encoded)


def format_id(value):
    """Format an ID for use in an error message."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return repr(value)


class OrderConstraints(object):
    """Ordering constraints collected from arrays merged by ID.

    Only the order explicitly declared by the source arrays is recorded.  The
    object also records the first occurrence of every ID.  The first-seen
    order is used as a stable tie-breaker, so elements that have no declared
    ordering relationship keep a deterministic order.

    The object is mutable and can be passed to the next merge.  This is
    important: a previously sorted result array no longer contains the
    original constraints, so reconstructing them from that array would add
    ordering relationships that were never declared by any source.
    """

    VERSION = 1

    def __init__(self):
        self._ids = {}
        self._first_seen = []
        self._edges = {}

    @classmethod
    def from_dict(cls, value):
        rv = cls()

        if isinstance(value, OrderConstraints):
            return value

        if not isinstance(value, dict):
            raise ValueError("Order constraints state must be a mapping")

        first_seen = (
                value.get('first_seen')
                or value.get('firstSeen')
                or value.get('ids')
                or value.get('seen')
                or value.get('order')
                or [])

        for item_id in first_seen:
            rv.add_id(item_id)

        edges = value.get('edges')
        if edges is None:
            edges = value.get('constraints', [])

        for edge in edges:
            if isinstance(edge, dict):
                before = edge.get('before')
                after = edge.get('after')
                sources = edge.get('sources')
                if sources is None:
                    sources = edge.get('source')
            else:
                before = edge[0]
                after = edge[1]
                sources = edge[2] if len(edge) > 2 else None

            if sources is None:
                sources = []
            elif not isinstance(sources, (list, tuple)):
                sources = [sources]

            rv.add_edge(before, after, sources=sources)

        return rv

    def copy(self):
        return OrderConstraints.from_dict(self.to_dict())

    def add_id(self, item_id):
        token = canonical_id(item_id)
        if token not in self._ids:
            self._ids[token] = item_id
            self._first_seen.append(token)
        return token

    def add_edge(self, before, after, source=None, sources=None):
        before_token = self.add_id(before)
        after_token = self.add_id(after)

        if before_token == after_token:
            raise ValueError("Order constraint relates ID %s to itself" % (
                    format_id(before),))

        key = (before_token, after_token)
        if key not in self._edges:
            self._edges[key] = []

        if sources is None:
            sources = [source]

        for source_name in sources:
            if source_name not in self._edges[key]:
                self._edges[key].append(source_name)

        return key

    def add_order(self, item_ids, source=None):
        """Record the order declared by one source array.

        Only adjacent elements are joined.  In particular, if an element has
        no usable ID, the elements on either side of it are not connected:
        no ordering was declared directly between them.
        """
        tokens = []

        for item_id in item_ids:
            if item_id is None:
                tokens.append(None)
            else:
                tokens.append(self.add_id(item_id))

        for i in range(len(tokens) - 1):
            if tokens[i] is not None and tokens[i + 1] is not None:
                self.add_edge(
                        self._ids[tokens[i]],
                        self._ids[tokens[i + 1]],
                        source=source)

    def remove(self, item_id):
        """Remove one member and only the edges that touch it.

        Removing an edge must not implicitly join the member's predecessor
        to its successor.  That would invent an ordering constraint that no
        source declared.
        """
        token = canonical_id(item_id)

        for key in list(self._edges):
            if token in key:
                del self._edges[key]

        if token in self._ids:
            del self._ids[token]
        if token in self._first_seen:
            self._first_seen.remove(token)

    def remove_many(self, item_ids):
        for item_id in item_ids:
            self.remove(item_id)

    def remove_id(self, item_id):
        return self.remove(item_id)

    def _nodes(self):
        nodes = list(self._first_seen)
        known = set(nodes)

        for before, after in self._edges:
            for token in (before, after):
                if token not in known:
                    nodes.append(token)
                    known.add(token)

        return nodes

    def _cycle(self, nodes):
        """Return one actual directed cycle, or None."""
        adjacency = dict((token, []) for token in nodes)
        for before, after in self._edges:
            if after not in adjacency[before]:
                adjacency[before].append(after)

        color = dict((token, 0) for token in nodes)
        parent = {}
        cycle = []

        def visit(token):
            color[token] = 1
            for next_token in adjacency[token]:
                if color[next_token] == 0:
                    parent[next_token] = token
                    found = visit(next_token)
                    if found:
                        return found
                elif color[next_token] == 1:
                    # Reconstruct the actual cycle ending at next_token.
                    current = token
                    found = [current]
                    while current != next_token:
                        current = parent[current]
                        found.append(current)
                    found.reverse()
                    found.append(next_token)
                    return found
            color[token] = 2
            return None

        for token in nodes:
            if color[token] == 0:
                found = visit(token)
                if found:
                    cycle = found
                    break

        return cycle

    def _describe_cycle(self, cycle):
        parts = [format_id(self._ids[token]) for token in cycle]
        route = ' -> '.join(parts)

        source_parts = []
        for i in range(len(cycle) - 1):
            key = (cycle[i], cycle[i + 1])
            sources = self._edges.get(key, [])
            source_parts.append('%s -> %s: %s' % (
                format_id(self._ids[key[0]]),
                format_id(self._ids[key[1]]),
                ', '.join(str(s) for s in sources) if sources else 'unknown'))

        return route, source_parts

    def order(self):
        """Return IDs ordered by the collected constraints.

        Ties are broken by the explicit first-occurrence order.  If the
        constraints contain a cycle, the exception names the actual cycle
        and the source arrays that contributed each edge.  No edge is
        silently discarded in order to produce a partial result.
        """
        nodes = self._nodes()

        indegree = dict((token, 0) for token in nodes)
        adjacency = dict((token, []) for token in nodes)

        for (before, after) in self._edges:
            adjacency[before].append(after)
            indegree[after] += 1

        first_seen = dict((token, i) for i, token in enumerate(self._first_seen))
        ready = []
        for token in nodes:
            if indegree[token] == 0:
                heapq.heappush(ready, (first_seen.get(token, len(first_seen)), token))

        ordered = []
        while ready:
            _, token = heapq.heappop(ready)
            ordered.append(token)

            for next_token in adjacency[token]:
                indegree[next_token] -= 1
                if indegree[next_token] == 0:
                    heapq.heappush(
                            ready,
                            (first_seen.get(next_token, len(first_seen)), next_token))

        if len(ordered) != len(nodes):
            cycle = self._cycle(nodes)
            if cycle is None:
                # This should be impossible if Kahn's algorithm stopped
                # early, but fail loudly rather than return a partial order.
                raise RuntimeError("Order constraints contain a cycle")

            route, source_parts = self._describe_cycle(cycle)
            cycle_ids = self.ids_for(cycle[:-1])
            error = OrderConstraintError(
                    "Order constraints form a cycle: %s (sources: %s)" % (
                        route, '; '.join(source_parts)),
                    value=JSONValue(cycle_ids))
            error.ids = cycle_ids
            raise error

        return [self._ids[token] for token in ordered]

    def ids_for(self, tokens):
        return [self._ids[token] for token in tokens]

    def to_dict(self):
        edges = sorted(
                self._edges.items(),
                key=lambda item: (item[0][0], item[0][1]))

        return {
            '_order_constraints': self.VERSION,
            'first_seen': [self._ids[token] for token in self._first_seen],
            'edges': [
                {
                    'before': self._ids[before],
                    'after': self._ids[after],
                    'sources': sorted(sources, key=lambda source: str(source)),
                }
                for (before, after), sources in edges
            ],
        }

    @property
    def constraints(self):
        return self.to_dict()['edges']

    @property
    def first_seen(self):
        return self.to_dict()['first_seen']

    def __getitem__(self, key):
        return self.to_dict()[key]

    def get(self, key, default=None):
        return self.to_dict().get(key, default)

    def keys(self):
        return self.to_dict().keys()

    def items(self):
        return self.to_dict().items()

    def __eq__(self, other):
        if isinstance(other, OrderConstraints):
            other = other.to_dict()
        return self.to_dict() == other

    def __ne__(self, other):
        return not self == other

    def __repr__(self):
        return 'OrderConstraints(%r)' % (self.to_dict(),)


class OrderConstraintError(SchemaError, ValueError):
    """Raised when declared order constraints cannot be satisfied."""

    def __init__(self, message, value=None):
        Exception.__init__(self, message)
        self.message = message
        self.value = value
        self.strategy_name = None

    def __str__(self):
        rv = self.message
        try:
            ref = self.value.ref
        except AttributeError:
            ref = None
        if ref is not None:
            rv = "%s: %s" % (rv, ref)
        if self.strategy_name is not None:
            rv = "'%s' merge strategy: %s" % (self.strategy_name, rv)
        return rv


class ConstraintState(dict):
    """Serializable constraint state with small mutation helpers.

    The object is a normal dictionary, so it can be serialized and passed
    back as a merge option.  The helper methods make state updates (notably
    member removal) explicit and keep the dictionary in sync.
    """

    def __init__(self, value=None):
        dict.__init__(self)
        if value is not None:
            dict.update(self, value)

    def __missing__(self, key):
        aliases = {
            'constraints': 'edges',
            'order': 'first_seen',
            'seen': 'first_seen',
            'ids': 'first_seen',
        }

        if key in aliases:
            return dict.get(self, aliases[key], [])

        if key == 'sources':
            sources = set()
            for edge in dict.get(self, 'edges', []):
                sources.update(edge.get('sources', []))
            return sorted(sources, key=lambda source: str(source))

        raise KeyError(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def _constraints(self):
        return OrderConstraints.from_dict(self)

    def _replace(self, constraints):
        dict.clear(self)
        dict.update(self, constraints.to_dict())
        return self

    def copy(self):
        return ConstraintState(self)

    def to_dict(self):
        return dict(self)

    def add_order(self, item_ids, source=None):
        constraints = self._constraints()
        constraints.add_order(item_ids, source=source)
        return self._replace(constraints)

    def add_edge(self, before, after, source=None):
        constraints = self._constraints()
        constraints.add_edge(before, after, source=source)
        return self._replace(constraints)

    def remove(self, item_id):
        constraints = self._constraints()
        constraints.remove(item_id)
        return self._replace(constraints)

    def remove_id(self, item_id):
        return self.remove(item_id)

    def remove_many(self, item_ids):
        constraints = self._constraints()
        constraints.remove_many(item_ids)
        return self._replace(constraints)

    def order(self):
        return self._constraints().order()


# Convenient names for callers that spell out "state".
OrderConstraintsState = OrderConstraints
OrderConstraintState = OrderConstraints
