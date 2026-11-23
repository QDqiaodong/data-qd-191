"""Cross-batch reconciliation of locally quantified synthetic chromatograms.

Each offline workstation quantifies its own batches with that batch's v1
calibration (fixed window, exact signed integration, equal-weight intercept
fit with a positive-slope gate).  A *panel* registers the same virtual control
on two workstations, i.e. two unknown runs in different batches: after adding a
per-batch, per-analyte zero-point correction the two unified concentrations
must be exactly equal.  A misfiled panel is an all-or-nothing record: either
the whole panel (every analyte, every reading) is retained or the whole panel
id is discarded.

The anchor batch's correction is zero for every analyte.  For a retained set
of panels the correction equations are linear difference constraints over the
batch graph; cycles are real constraints, never short-circuited by an existing
path.  The full panel set is tested first and, when it is simultaneously
consistent for every analyte and connects every batch to the anchor, no
removal is ever tried.  Otherwise every single-panel removal that keeps the
graph connected to the anchor and consistent for *all* selected analytes at
once is reported as a possible repair; dropping different panels for different
analytes is forbidden.  If none exists the report declares the account
unreconcileable instead of inventing a path or allowing two removals.

The graph analysis builds one spanning tree per analyte (the graph itself is
analyte independent).  Every non-tree edge gives an exact oriented residual on
that tree; tree-difference aggregates then decide, for each tree edge, whether
deleting it leaves every crossing edge simultaneously satisfiable and give
the single correction shift its cut needs.  All arithmetic is exact
``fractions.Fraction``; the report only carries reduced integer/fraction
strings and is byte-deterministic under input reordering.  Nothing here ever
touches real samples, instruments, networks or external services.
"""
import json
from fractions import Fraction
from pathlib import Path

from .benchmark import fit_line
from .checks import array, fields, ident, unique
from .integrate import _interpolate, area_of
from .io import constant, pairs, read_limited
from .model import validate

RECONCILE_REPORT_VERSION = 1
PLAN_BYTES_LIMIT = 8_000_000
SOURCE_BYTES_LIMIT = 8_000_000
SOURCE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_BATCHES = 12_000
MAX_DISTINCT_FILES = 256
MAX_PANELS = 24_000
MAX_QUERIES = 16
MAX_ANALYTES = 4

# Report status vocabulary.
STATUS_ALL_CONSISTENT = 'all_panels_consistent'
STATUS_REPAIRABLE = 'single_panel_repairable'
STATUS_UNRECONCILEABLE = 'unreconcileable'
REASON_DISCONNECTED = 'full_graph_disconnected'
REASON_NO_COMMON_REPAIR = 'no_common_single_panel_repair'


class ReconcileError(ValueError):
    """Every rejection names the reconcile object and the offending part."""


# ----------------------------------------------------------------- plan input

def load_plan(path):
    raw = read_limited(path, PLAN_BYTES_LIMIT)
    data = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                      parse_constant=constant)
    return parse_plan(data)


def _reference(value, label):
    """Validate a ``[batch_id, run_id]`` reference pair."""
    entries = array(value, 2, 2)
    return ident(entries[0]), ident(entries[1])


def parse_plan(data):
    """Validate every source-independent PLAN rule and normalize ordering."""
    if not isinstance(data, dict):
        raise ReconcileError('reconcile plan top level must be an object')
    fields(data, ('batches', 'anchor', 'analytes', 'panels', 'queries'))

    batches_raw = data['batches']
    if not isinstance(batches_raw, dict) \
            or not 2 <= len(batches_raw) <= MAX_BATCHES:
        raise ReconcileError(
            f'batches must be an object mapping 2..{MAX_BATCHES} batch ids '
            'to source file ids')
    batches = {}
    for batch_id, file_id in batches_raw.items():
        batches[ident(batch_id)] = ident(file_id)
    if len(set(batches.values())) > MAX_DISTINCT_FILES:
        raise ReconcileError(
            f'at most {MAX_DISTINCT_FILES} distinct source files may be referenced')

    anchor = ident(data['anchor'])
    if anchor not in batches:
        raise ReconcileError('anchor must name a batch id present in batches')

    analyte_ids = [ident(value) for value in array(data['analytes'], 1, MAX_ANALYTES)]
    unique(analyte_ids)
    # Canonical order: reordering analytes in the plan must not change a byte.
    analyte_ids.sort()

    panels = []
    panel_ids = []
    for entry in array(data['panels'], 0, MAX_PANELS):
        fields(entry, ('id', 'left', 'right'))
        panel_id = ident(entry['id'])
        left = _reference(entry['left'], f'panel {panel_id} left')
        right = _reference(entry['right'], f'panel {panel_id} right')
        if left[0] == right[0]:
            raise ReconcileError(
                f'panel {panel_id} endpoints must belong to different batches')
        panels.append({'id': panel_id, 'left': left, 'right': right})
        panel_ids.append(panel_id)
    unique(panel_ids)
    panels.sort(key=lambda entry: entry['id'])
    queries = []
    query_ids = []
    analyte_set = set(analyte_ids)
    for entry in array(data['queries'], 1, MAX_QUERIES):
        fields(entry, ('id', 'analyte_id', 'left', 'right'))
        query_id = ident(entry['id'])
        analyte_id = ident(entry['analyte_id'])
        if analyte_id not in analyte_set:
            raise ReconcileError(
                f'query {query_id} analyte {analyte_id} is not selected by the plan')
        left = _reference(entry['left'], f'query {query_id} left')
        right = _reference(entry['right'], f'query {query_id} right')
        queries.append({'id': query_id, 'analyte_id': analyte_id,
                        'left': left, 'right': right})
        query_ids.append(query_id)
    unique(query_ids)
    queries.sort(key=lambda entry: entry['id'])

    return {'batches': batches, 'anchor': anchor, 'analyte_ids': analyte_ids,
            'panels': panels, 'queries': queries}


# ------------------------------------------------------------- source loading

def load_sources(sources_dir, batches):
    """Load and fully v1-validate every distinct referenced source file.

    The byte budget counts each distinct file once, as mandated.  The returned
    mapping keeps the validated analyte windows and runs so validation never
    runs twice.
    """
    directory = Path(sources_dir)
    loaded = {}
    total_bytes = 0
    for file_id in sorted(set(batches.values())):
        path = directory / f'{file_id}.json'
        raw = read_limited(path, SOURCE_BYTES_LIMIT)
        total_bytes += len(raw)
        if total_bytes > SOURCE_TOTAL_BYTES:
            raise ReconcileError(
                f'referenced sources exceed {SOURCE_TOTAL_BYTES} bytes in total')
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=constant)
        # Full v1 validation, including records never used by any panel/query.
        analytes, runs = validate(data)
        loaded[file_id] = (data, analytes, runs)
    return loaded


def _check_references(plan, loaded):
    """Resolve every panel/query reference against validated unknown runs."""
    batches = plan['batches']
    analyte_ids = plan['analyte_ids']
    indexes = {}
    for batch_id, file_id in batches.items():
        _data, analytes, runs = loaded[file_id]
        missing = [a for a in analyte_ids if a not in analytes]
        if missing:
            raise ReconcileError(
                f"batch {batch_id} source lacks selected analyte(s): "
                + ', '.join(missing))
        indexes[batch_id] = {run['id']: run for run in runs}

    def require_unknown(reference, label):
        batch_id, run_id = reference
        if batch_id not in batches:
            raise ReconcileError(f'{label} references unknown batch {batch_id}')
        run = indexes[batch_id].get(run_id)
        if run is None:
            raise ReconcileError(
                f'{label} references run {run_id} absent from batch {batch_id}')
        if run['kind'] != 'unknown':
            raise ReconcileError(f'{label} must reference an unknown run; '
                                 f'{batch_id}/{run_id} is a standard run')

    for panel in plan['panels']:
        require_unknown(panel['left'], f"panel {panel['id']} left")
        require_unknown(panel['right'], f"panel {panel['id']} right")
    for query in plan['queries']:
        require_unknown(query['left'], f"query {query['id']} left")
        require_unknown(query['right'], f"query {query['id']} right")
    return indexes


def quantify_selected(analytes, runs, analyte_ids):
    """Local v1 back-inferred concentrations of every unknown run.

    Only the selected analytes are integrated and fitted, but the standards of
    the whole source feed each fit and every selected analyte must fit on its
    own batch's curve -- another batch can never fill a missing fit.  Negative
    and extrapolated results are kept, and displayed rounding or raw areas are
    never used.  Returns ``{(run_id, analyte_id): Fraction}``.
    """
    parsed = {run['id']: [(Fraction(t), Fraction(y)) for t, y in run['samples']]
              for run in runs}
    areas = {}
    for run_id, points in parsed.items():
        times = [t for t, _y in points]
        per_analyte = {}
        for analyte_id in analyte_ids:
            lo, hi = analytes[analyte_id]
            start = _interpolate(times, points, lo)
            end = _interpolate(times, points, hi)
            per_analyte[analyte_id] = area_of(points, lo, hi, start, end)
        areas[run_id] = per_analyte

    quantified = {}
    for analyte_id in analyte_ids:
        calibration_points = [
            (Fraction(run['concentrations'][analyte_id]),
             areas[run['id']][analyte_id])
            for run in runs if run['kind'] == 'standard']
        slope, intercept, _c_min, _c_max = fit_line(calibration_points)
        if _c_min is None:
            # fit_line returns the unavailability reason in place of slope.
            raise ReconcileError(
                f'calibration for analyte {analyte_id} unavailable: {slope}')
        for run in runs:
            if run['kind'] == 'unknown':
                quantified[(run['id'], analyte_id)] = \
                    (areas[run['id']][analyte_id] - intercept) / slope
    return quantified


# ------------------------------------------------------------- graph analysis

class _GraphAnalysis:
    """Spanning-tree residual analysis for one analyte over one fixed graph."""

    def __init__(self, node_count, adjacency, edges, root_index, d_values):
        self.n = node_count
        self.edges = edges  # list of (panel_id, L, R) sorted by panel id
        self.d = d_values   # panel_id -> oriented residual d = cR - cL
        self.root = root_index
        self._build_tree(adjacency)
        self._build_lca()
        self._build_residuals(adjacency)

    def _build_tree(self, adjacency):
        """Iterative DFS spanning tree rooted at the anchor; subtree tin/tout."""
        self.parent = [-1] * self.n
        self.parent_edge = [-1] * self.n   # edge index per non-root node
        self.depth = [0] * self.n
        self.tin = [0] * self.n
        self.tout = [0] * self.n
        self.order = []
        seen = [False] * self.n
        seen[self.root] = True
        clock = 0
        self.tin[self.root] = clock
        clock += 1
        self.order.append(self.root)
        stack = [(self.root, 0)]
        while stack:
            node, next_slot = stack[-1]
            if next_slot < len(adjacency[node]):
                neighbour, edge_index, sign = adjacency[node][next_slot]
                stack[-1] = (node, next_slot + 1)
                if seen[neighbour]:
                    continue
                seen[neighbour] = True
                self.parent[neighbour] = node
                self.parent_edge[neighbour] = edge_index
                self.depth[neighbour] = self.depth[node] + 1
                self.tin[neighbour] = clock
                clock += 1
                self.order.append(neighbour)
                stack.append((neighbour, 0))
            else:
                self.tout[node] = clock - 1
                stack.pop()
        self.connected = len(self.order) == self.n

        # Tree potentials t with t[anchor] = 0 solve every tree edge
        # cL + t[L] = cR + t[R], i.e. t[R] - t[L] = -(cR - cL) = -d.
        self.potential = [Fraction(0)] * self.n
        for node in self.order[1:]:
            edge_index = self.parent_edge[node]
            panel_id, left, right = self.edges[edge_index]
            upper = self.parent[node]
            sign = 1 if left == upper else -1
            # sign=+1: node is R and t[node] = t[upper] - d
            # sign=-1: node is L and t[node] = t[upper] + d
            self.potential[node] = self.potential[upper] - sign * self.d[panel_id]

        self.panel_edge = {panel_id: edge_index
                           for edge_index, (panel_id, _l, _r) in enumerate(self.edges)}
        self.tree_edges = set(self.parent_edge[1:])
        self.edge_child = {edge_index: node
                           for node, edge_index in enumerate(self.parent_edge)
                           if edge_index >= 0}
        self.nontree_edges = [i for i in range(len(self.edges))
                              if i not in self.tree_edges]

    def _build_lca(self):
        self.log = max(1, self.n.bit_length())
        table = [self.parent[:]]
        table[0][self.root] = self.root
        for _level in range(1, self.log):
            previous = table[-1]
            table.append([previous[previous[node]] for node in range(self.n)])
        self.ancestors = table

    def lca(self, left, right):
        if self.depth[left] < self.depth[right]:
            left, right = right, left
        difference = self.depth[left] - self.depth[right]
        for bit in range(self.log):
            if difference >> bit & 1:
                left = self.ancestors[bit][left]
        if left == right:
            return left
        for bit in range(self.log - 1, -1, -1):
            if self.ancestors[bit][left] != self.ancestors[bit][right]:
                left = self.ancestors[bit][left]
                right = self.ancestors[bit][right]
        return self.ancestors[0][left]

    def _build_residuals(self, adjacency):
        """Non-tree residuals plus tree-difference aggregates over the tree."""
        bad_count = [0] * self.n
        good_count = [0] * self.n
        signed_sum = [Fraction(0)] * self.n
        square_sum = [Fraction(0)] * self.n
        self.residual = {}
        self.bad_edges = set()
        for edge_index in self.nontree_edges:
            panel_id, left, right = self.edges[edge_index]
            # Oriented residual of the edge equation against tree potentials.
            residual = self.d[panel_id] - self.potential[left] + self.potential[right]
            self.residual[edge_index] = residual
            if residual != 0:
                self.bad_edges.add(edge_index)
            ancestor = self.lca(left, right)
            target = bad_count if residual != 0 else good_count
            # Path counters (bad/good) on the tree path L..R.
            target[left] += 1
            target[ancestor] -= 1
            target[right] += 1
            target[ancestor] -= 1
            # Signed aggregate: +r on the L side of the path, -r on the R side,
            # so a cut reads the shift the subtree needs with its correct sign.
            signed_sum[left] += residual
            signed_sum[ancestor] -= residual
            signed_sum[right] -= residual
            signed_sum[ancestor] += residual
            # Squared aggregate on the whole path, used as an exact
            # "all crossing residuals agree" test: b*s2 == s1^2.
            square = residual * residual
            square_sum[left] += square
            square_sum[ancestor] -= square
            square_sum[right] += square
            square_sum[ancestor] -= square

        for node in reversed(self.order[1:]):
            upper = self.parent[node]
            bad_count[upper] += bad_count[node]
            good_count[upper] += good_count[node]
            signed_sum[upper] += signed_sum[node]
            square_sum[upper] += square_sum[node]
        self.bad_count = bad_count
        self.good_count = good_count
        self.signed_sum = signed_sum
        self.square_sum = square_sum

        # Per tree-edge cut: crossing counts and the shift delta if defined.
        self.tree_delta = {}
        self.tree_agrees = {}
        for node in self.order[1:]:
            crossing = bad_count[node] + good_count[node]
            if crossing == 0:
                continue  # bridge: deleting it disconnects the graph
            if bad_count[node] == 0:
                # Every crossing edge is already satisfied; no shift needed.
                self.tree_agrees[node] = True
                self.tree_delta[node] = Fraction(0)
                continue
            if good_count[node] != 0:
                self.tree_agrees[node] = False
                continue
            # Only bad edges cross; they must demand one identical shift.
            if bad_count[node] * square_sum[node] != signed_sum[node] ** 2:
                self.tree_agrees[node] = False
                continue
            self.tree_agrees[node] = True
            # The signed aggregate already carries the required sign: +r for
            # edges whose left endpoint sits in the subtree, -r otherwise, so
            # the one common shift is exactly s1/b.
            self.tree_delta[node] = signed_sum[node] / bad_count[node]

    def candidate_panels(self):
        """Panel ids whose removal leaves this analyte's system consistent."""
        result = set()
        if not self.bad_edges:
            # Any non-bridge panel may be dropped; the tree itself stays valid.
            for edge_index, (panel_id, _l, _r) in enumerate(self.edges):
                if edge_index in self.tree_edges:
                    node = self.edge_child[edge_index]
                    if self.bad_count[node] + self.good_count[node] > 0:
                        result.add(panel_id)
                else:
                    result.add(panel_id)
            return result
        for edge_index, (panel_id, _l, _r) in enumerate(self.edges):
            if edge_index in self.tree_edges:
                node = self.edge_child[edge_index]
                # Deleting a tree edge only lets the cut shift repair bad
                # non-tree edges that cross the cut: every bad non-tree edge
                # must cross it (a bad edge internal to either side keeps its
                # nonzero residual), the crossing edges must all be bad and
                # they must demand one identical shift.
                if self.tree_agrees.get(node, False) \
                        and self.bad_count[node] == len(self.bad_edges):
                    result.add(panel_id)
            elif self.bad_edges == {edge_index}:
                # The single bad non-tree edge disappears; the tree is valid.
                result.add(panel_id)
        return result

    def correction_for(self, node, removed_panel):
        """Correction of one batch when exactly ``removed_panel`` is dropped.

        Query endpoints are the only nodes that ever need a correction, so the
        full per-batch array is never materialized: a non-tree edge removal
        leaves the tree solution untouched, while removing a tree edge shifts
        the subtree below it by the single delta derived from its cut.
        """
        edge_index = self.panel_edge[removed_panel]
        if edge_index not in self.tree_edges:
            return self.potential[node]
        child = self.edge_child[edge_index]
        if self.tin[child] <= self.tin[node] <= self.tout[child]:
            return self.potential[node] + self.tree_delta[child]
        return self.potential[node]

    def correction_full(self, node):
        """Correction of one batch under the full (no-removal) solution."""
        return self.potential[node]


def _build_adjacency(node_count, edges):
    adjacency = [[] for _ in range(node_count)]
    for edge_index, (_panel_id, left, right) in enumerate(edges):
        adjacency[left].append((right, edge_index, 1))
        adjacency[right].append((left, edge_index, -1))
    return adjacency


# ------------------------------------------------------------- reconciliation

def reconcile(sources_dir, plan_path):
    """Full reconcile pipeline returning the report object (never written)."""
    plan = load_plan(plan_path)
    batches = plan['batches']
    analyte_ids = plan['analyte_ids']
    anchor = plan['anchor']

    loaded = load_sources(sources_dir, batches)
    indexes = _check_references(plan, loaded)

    # Independent local quantification per batch; a failed fit anywhere fails
    # the whole run and is never borrowed from another batch.
    local = {}
    for batch_id in sorted(batches):
        _data, analytes, runs = loaded[batches[batch_id]]
        local[batch_id] = quantify_selected(analytes, runs, analyte_ids)

    batch_ids = sorted(batches)
    node_of = {batch_id: index for index, batch_id in enumerate(batch_ids)}
    root_index = node_of[anchor]

    # Graph edges in canonical (panel id) order.  d = cR - cL per analyte.
    edges = []
    d_values = {analyte_id: {} for analyte_id in analyte_ids}
    for panel in plan['panels']:
        left_batch, left_run = panel['left']
        right_batch, right_run = panel['right']
        edge = (panel['id'], node_of[left_batch], node_of[right_batch])
        edges.append(edge)
        for analyte_id in analyte_ids:
            c_left = local[left_batch][(left_run, analyte_id)]
            c_right = local[right_batch][(right_run, analyte_id)]
            d_values[analyte_id][panel['id']] = c_right - c_left

    adjacency = _build_adjacency(len(batch_ids), edges)

    # One analysis per analyte; the first one also establishes connectivity of
    # the shared graph (the graph is the same for every analyte).
    analyses = {}
    connected = True
    for analyte_id in analyte_ids:
        analysis = _GraphAnalysis(len(batch_ids), adjacency, edges,
                                  root_index, d_values[analyte_id])
        connected = connected and analysis.connected
        analyses[analyte_id] = analysis

    if not connected:
        status, reason, removable = STATUS_UNRECONCILEABLE, REASON_DISCONNECTED, []
        scenarios = []
    else:
        all_consistent = all(not analysis.bad_edges
                             for analysis in analyses.values())
        if all_consistent:
            status, reason, removable = STATUS_ALL_CONSISTENT, None, []
            scenarios = [None]
        else:
            common = None
            for analyte_id in analyte_ids:
                candidates = analyses[analyte_id].candidate_panels()
                common = candidates if common is None else common & candidates
            common = common or set()
            if not common:
                status, reason, removable = \
                    STATUS_UNRECONCILEABLE, REASON_NO_COMMON_REPAIR, []
                scenarios = []
            else:
                status, reason = STATUS_REPAIRABLE, None
                removable = sorted(common)
                scenarios = removable

    query_entries = []
    for query in plan['queries']:
        analyte_id = query['analyte_id']
        left_batch, left_run = query['left']
        right_batch, right_run = query['right']
        local_left = local[left_batch][(left_run, analyte_id)]
        local_right = local[right_batch][(right_run, analyte_id)]
        entry = {
            'id': query['id'],
            'analyte_id': analyte_id,
            'left': [left_batch, left_run],
            'right': [right_batch, right_run],
            'left_local_concentration': str(local_left),
            'right_local_concentration': str(local_right),
        }
        if status == STATUS_UNRECONCILEABLE:
            entry.update(min_net_difference=None, min_witness_panel=None,
                         max_net_difference=None, max_witness_panel=None,
                         bookable_net_difference=None)
        else:
            analysis = analyses[analyte_id]
            left_node = node_of[left_batch]
            right_node = node_of[right_batch]

            def net_under(removed):
                if removed is None:
                    correction_left = analysis.correction_full(left_node)
                    correction_right = analysis.correction_full(right_node)
                else:
                    correction_left = analysis.correction_for(left_node, removed)
                    correction_right = analysis.correction_for(right_node, removed)
                return (local_left + correction_left
                        - local_right - correction_right)

            # scenarios is either [None] or the lexicographically sorted panel
            # ids, so strict comparisons keep the smallest id on every tie.
            values = [(removed, net_under(removed)) for removed in scenarios]
            min_witness, minimum = values[0]
            max_witness, maximum = values[0]
            for removed, net in values[1:]:
                if net < minimum:
                    minimum, min_witness = net, removed
                if net > maximum:
                    maximum, max_witness = net, removed
            if scenarios == [None]:
                min_witness = max_witness = None
            entry.update(
                min_net_difference=str(minimum),
                min_witness_panel=min_witness,
                max_net_difference=str(maximum),
                max_witness_panel=max_witness,
                # Only book the value when every feasible scenario agrees; the
                # witnesses are never treated as confirmed bad panels.
                bookable_net_difference=(str(minimum) if minimum == maximum else None))
        query_entries.append(entry)

    return {
        'format_version': RECONCILE_REPORT_VERSION,
        'anchor': anchor,
        'analyte_ids': analyte_ids,
        'status': status,
        'unreconcileable_reason': reason,
        'removable_panel_ids': removable,
        'queries': query_entries,
    }


def run_reconcile(sources_dir, plan_path, output_path):
    """Load inputs, reconcile, and publish the report without overwriting."""
    from .io import publish
    report = reconcile(sources_dir, plan_path)
    publish(output_path, report)
    return report
