# vim:ts=4 sw=4 expandtab softtabstop=4
from jsonmerge.exceptions import JSONMergeError


class OrderConstraintError(JSONMergeError):
    """Raised when declared order constraints cannot be satisfied.

    The 'cycle' attribute holds the list of IDs that form the cycle (the
    first ID is repeated at the end to close the loop). The 'sources'
    attribute maps each edge of the cycle, given as a (before, after)
    tuple, to the list of sources that declared it.
    """

    def __init__(self, message, cycle=None, sources=None, value=None):
        JSONMergeError.__init__(self, message, value)
        self.cycle = cycle
        self.sources = sources or {}


class OrderConstraints(object):
    """Directed graph of declared before/after relations between item IDs.

    The graph only ever contains relations that were explicitly declared
    by an input array. Deleting an ID removes only the edges that
    directly mention it and never connects its former neighbors.
    """

    def __init__(self):
        # maps ID -> OrderedDict of successor ID -> list of sources
        self._edges = {}

    def _add_id(self, id):
        if id not in self._edges:
            self._edges[id] = {}

    def ids(self):
        return list(self._edges)

    def edges(self):
        for before, afters in self._edges.items():
            for after in afters:
                yield (before, after)

    def add_order(self, ids, source):
        """Declare that the given IDs must appear in this relative order.

        Records one edge for each pair of adjacent IDs. 'source' is an
        arbitrary tag identifying the input array that declared the
        relation.
        """
        for id in ids:
            self._add_id(id)

        for before, after in zip(ids, ids[1:]):
            sources = self._edges[before].setdefault(after, [])
            if source not in sources:
                sources.append(source)

    def remove(self, id):
        """Remove an ID and only the constraints that directly mention it."""
        self._edges.pop(id, None)

        for afters in self._edges.values():
            afters.pop(id, None)

    def _find_cycle(self):
        # iterative depth-first search; returns the cycle as a list of
        # IDs with the first ID repeated at the end, or None
        WHITE, GRAY, BLACK = 0, 1, 2
        color = dict((id, WHITE) for id in self._edges)

        for root in self._edges:
            if color[root] != WHITE:
                continue

            stack = [(root, iter(sorted(self._edges[root], key=repr)))]
            path = [root]
            color[root] = GRAY

            while stack:
                node, it = stack[-1]
                advanced = False

                for next_id in it:
                    if color[next_id] == GRAY:
                        i = path.index(next_id)
                        return path[i:] + [next_id]
                    elif color[next_id] == WHITE:
                        color[next_id] = GRAY
                        stack.append((next_id, iter(sorted(self._edges[next_id], key=repr))))
                        path.append(next_id)
                        advanced = True
                        break

                if not advanced:
                    color[node] = BLACK
                    stack.pop()
                    path.pop()

        return None

    def check_acyclic(self):
        """Raise OrderConstraintError if the declared relations form a cycle."""
        cycle = self._find_cycle()

        if cycle is not None:
            sources = {}
            for before, after in zip(cycle, cycle[1:]):
                sources[(before, after)] = list(self._edges[before][after])

            raise OrderConstraintError(
                "Order constraints form a cycle: %s" %
                " -> ".join(repr(id) for id in cycle),
                cycle=cycle, sources=sources)

    def order(self, ids):
        """Return 'ids' sorted so that every declared relation is satisfied.

        Elements that are not constrained relative to each other keep the
        order in which they first appear in 'ids'.
        """
        self.check_acyclic()

        first_seen = dict((id, i) for i, id in enumerate(ids))
        ids = sorted(ids, key=lambda id: first_seen[id])

        # insertion sort; only constrained pairs are ever swapped, so the
        # result is stable with respect to first occurrence in 'ids'
        result = []
        for id in ids:
            for pos, other in enumerate(result):
                if self._is_before(id, other):
                    break
            else:
                pos = len(result)

            result.insert(pos, id)

        return result

    def _is_before(self, a, b):
        # True if constraints declare that 'a' must come before 'b'
        seen = set()
        stack = [a]

        while stack:
            node = stack.pop()
            if node == b:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(self._edges.get(node, ()))

        return False

    def to_state(self):
        """Return a JSON-serializable snapshot of the declared relations."""
        return [
            {"before": before, "after": after, "sources": list(sources)}
            for before, afters in self._edges.items()
            for after, sources in afters.items()
        ]

    @classmethod
    def from_state(cls, state):
        """Rebuild constraints from a snapshot returned by to_state()."""
        constraints = cls()

        for edge in state:
            before = edge["before"]
            after = edge["after"]
            constraints._add_id(before)
            constraints._add_id(after)
            constraints._edges[before].setdefault(after, []).extend(
                edge.get("sources", ()))

        return constraints
