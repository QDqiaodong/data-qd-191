"""Acceptance and contract tests for ``peak-quant reconcile``.

The cases mirror docs/reconcile-contract.md: the four-batch ring with a single
misfiled closing panel, the all-consistent and disconnected variants, the
two-analyte parallel panels with no common single-panel repair, batches with
different real curve parameters (including a non-zero intercept) and negative
or extrapolated readings, byte-deterministic reports under input reordering,
the full invalid-input/error matrix and refusal to overwrite an existing
target or symlink.  An independent brute-force oracle enumerates every panel
removal (and the no-removal case) with plain graph searches and checks both
the candidate set and every query extremum on randomized small instances.  A
separate subprocess driver runs the 12,000-batch month-end case under an
RLIMIT_CPU=90 budget.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from peak_quant import cli
from peak_quant.reconcile import (RECONCILE_REPORT_VERSION, ReconcileError,
                                  reconcile)

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------- tiny v1 builder

def _signal_for(values, windows):
    """Synthesize a piecewise-linear signal whose per-window areas are exact.

    Each analyte gets a private triangle on integer time points with zero
    endpoints, so its baseline is flat and the area on its window equals the
    designed value.  Times are integers, response heights may be negative.
    """
    points = {0: Fraction(0)}
    for index, (lo, hi) in enumerate(windows):
        mid = lo + (hi - lo) / 2
        points.setdefault(lo, Fraction(0))
        points[mid] = Fraction(values[index]) * 2 / (hi - lo)
        points.setdefault(hi, Fraction(0))
    times = sorted(points)
    return [[str(t), str(points[t])] for t in times]


def build_source(analyte_windows, standards, unknowns):
    """Build a valid v1 source with independent per-analyte responses.

    standards:  list of (run_id, {analyte: concentration Fractions})
    unknowns:   dict run_id -> {analyte: designed local concentration Fractions}
    Every run reuses the same time grid so all windows are fully covered.
    """
    analyte_ids = [a for a, _w in analyte_windows]
    runs = []
    for run_id, concentrations in standards:
        values = [concentrations[a] for a in analyte_ids]
        runs.append({'id': run_id, 'kind': 'standard',
                     'concentrations': {a: str(concentrations[a])
                                        for a in analyte_ids},
                     'samples': _signal_for(values,
                                            [w for _a, w in analyte_windows])})
    for run_id, values in unknowns.items():
        runs.append({'id': run_id, 'kind': 'unknown',
                     'samples': _signal_for([values[a] for a in analyte_ids],
                                            [w for _a, w in analyte_windows])})
    return {
        'analytes': [{'id': a, 'window': [str(lo), str(hi)]}
                     for a, (lo, hi) in analyte_windows],
        'runs': runs,
    }


def write_case(tmp, sources, plan):
    sources_dir = Path(tmp) / 'sources'
    sources_dir.mkdir()
    for file_id, source in sources.items():
        (sources_dir / f'{file_id}.json').write_text(
            json.dumps(source), encoding='utf-8')
    plan_path = Path(tmp) / 'plan.json'
    plan_path.write_text(json.dumps(plan), encoding='utf-8')
    return sources_dir, plan_path


def ring_case(tmp):
    """The spec's four-batch ring with a misfiled closing panel."""
    windows = [('X', (Fraction(0), Fraction(2)))]
    def src(unknowns):
        return build_source(
            windows,
            [('s0', {'X': Fraction(0)}), ('s1', {'X': Fraction(1)})],
            unknowns)
    sources = {
        'A': src({'u0': {'X': Fraction(0)}, 'u1': {'X': Fraction(1)}}),
        'B': src({'u0': {'X': Fraction(0)}}),
        'C': src({'u0': {'X': Fraction(0)}, 'u1': {'X': Fraction(2)},
                  'uca': {'X': Fraction(1)}}),
        'D': src({'u0': {'X': Fraction(0)}, 'u1': {'X': Fraction(1)}}),
    }
    plan = {
        'batches': {b: b for b in 'ABCD'},
        'anchor': 'A',
        'analytes': ['X'],
        'panels': [
            {'id': 'AB', 'left': ['A', 'u0'], 'right': ['B', 'u0']},
            {'id': 'BC', 'left': ['B', 'u0'], 'right': ['C', 'u0']},
            {'id': 'CD', 'left': ['C', 'u0'], 'right': ['D', 'u0']},
            {'id': 'CA', 'left': ['C', 'uca'], 'right': ['A', 'u0']},
        ],
        'queries': [
            {'id': 'qBC', 'analyte_id': 'X',
             'left': ['B', 'u0'], 'right': ['C', 'u0']},
            {'id': 'qCD', 'analyte_id': 'X',
             'left': ['C', 'u1'], 'right': ['D', 'u1']},
        ],
    }
    return write_case(tmp, sources, plan)


# ------------------------------------------------------------- brute-force oracle

def oracle(plan, local):
    """Independently enumerate every feasible removal scenario.

    Returns (status, sorted removable panel ids, {scenario: corrections})
    where scenario is None for "no removal" or a panel id.  Graph searches and
    equation propagation use plain Fraction arithmetic with no spanning trees.
    """
    batches = sorted(plan['batches'])
    anchor = plan['anchor']
    analyte_ids = sorted(plan['analytes'])
    panels = sorted(plan['panels'], key=lambda p: p['id'])
    panel_ids = [p['id'] for p in panels]

    def connected_without(removed):
        kept = [p for p in panels if p['id'] != removed]
        adjacency = {b: set() for b in batches}
        for panel in kept:
            left_batch = panel['left'][0]
            right_batch = panel['right'][0]
            adjacency[left_batch].add(right_batch)
            adjacency[right_batch].add(left_batch)
        seen = {anchor}
        stack = [anchor]
        while stack:
            node = stack.pop()
            for other in adjacency[node] - seen:
                seen.add(other)
                stack.append(other)
        return len(seen) == len(batches)

    def solve(removed):
        """BFS-propagate per-analyte corrections; verify every kept edge."""
        kept = [p for p in panels if p['id'] != removed]
        adjacency = {b: [] for b in batches}
        for panel in kept:
            lb, lr = panel['left']
            rb, rr = panel['right']
            adjacency[lb].append(panel)
            adjacency[rb].append(panel)
        corrections = {}
        for analyte in analyte_ids:
            values = {anchor: Fraction(0)}
            queue = [anchor]
            head = 0
            while head < len(queue):
                node = queue[head]
                head += 1
                for panel in adjacency[node]:
                    lb, lr = panel['left']
                    rb, rr = panel['right']
                    c_left = local[lb][(lr, analyte)]
                    c_right = local[rb][(rr, analyte)]
                    shift = c_left - c_right  # t[R] - t[L]
                    if node == lb:
                        other, candidate = rb, values[node] + shift
                    else:
                        other, candidate = lb, values[node] - shift
                    if other in values:
                        if values[other] != candidate:
                            return None
                    else:
                        values[other] = candidate
                        queue.append(other)
            if len(values) != len(batches):
                return None
            corrections[analyte] = values
        return corrections

    if not connected_without(None):
        return 'full_graph_disconnected', [], {}
    full = solve(None)
    if full is not None:
        return 'all_panels_consistent', [], {None: full}
    removable = []
    scenarios = {}
    for panel_id in panel_ids:
        if not connected_without(panel_id):
            continue
        solved = solve(panel_id)
        if solved is not None:
            removable.append(panel_id)
            scenarios[panel_id] = solved
    if not removable:
        return 'no_common_single_panel_repair', [], {}
    return 'single_panel_repairable', removable, scenarios


class ReconcileTests(unittest.TestCase):
    def _run_cli(self, sources_dir, plan_path, output):
        return cli.main(['reconcile', '--sources', str(sources_dir),
                         '--plan', str(plan_path), '--output', str(output)])

    def test_ring_repairable_matches_acceptance_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources_dir, plan_path = ring_case(tmp)
            report = reconcile(str(sources_dir), str(plan_path))
        self.assertEqual(report['format_version'], RECONCILE_REPORT_VERSION)
        self.assertEqual(report['anchor'], 'A')
        self.assertEqual(report['status'], 'single_panel_repairable')
        self.assertIsNone(report['unreconcileable_reason'])
        self.assertEqual(report['removable_panel_ids'], ['AB', 'BC', 'CA'])
        q_bc, q_cd = report['queries']
        self.assertEqual(
            (q_bc['min_net_difference'], q_bc['min_witness_panel'],
             q_bc['max_net_difference'], q_bc['max_witness_panel'],
             q_bc['bookable_net_difference']),
            ('0', 'AB', '1', 'BC', None))
        self.assertEqual(
            (q_cd['min_net_difference'], q_cd['min_witness_panel'],
             q_cd['max_net_difference'], q_cd['max_witness_panel'],
             q_cd['bookable_net_difference']),
            ('1', 'AB', '1', 'AB', '1'))
        self.assertEqual(q_cd['left_local_concentration'], '2')
        self.assertEqual(q_cd['right_local_concentration'], '1')

    def test_ring_all_consistent_when_ab_right_reading_is_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources_dir, plan_path = ring_case(tmp)
            plan = json.loads(plan_path.read_text())
            source_b = json.loads((sources_dir / 'B.json').read_text())
            extra = build_source(
                [('X', (Fraction(0), Fraction(2)))],
                [('s0', {'X': Fraction(0)}), ('s1', {'X': Fraction(1)})],
                {'uab': {'X': Fraction(1)}})
            source_b['runs'].append(
                next(run for run in extra['runs'] if run['id'] == 'uab'))
            (sources_dir / 'B.json').write_text(json.dumps(source_b))
            plan['panels'][0]['right'] = ['B', 'uab']
            plan_path.write_text(json.dumps(plan))
            report = reconcile(str(sources_dir), str(plan_path))
        self.assertEqual(report['status'], 'all_panels_consistent')
        self.assertEqual(report['removable_panel_ids'], [])
        for query in report['queries']:
            self.assertIsNone(query['min_witness_panel'])
            self.assertIsNone(query['max_witness_panel'])
        self.assertEqual(report['queries'][0]['bookable_net_difference'], '0')
        self.assertEqual(report['queries'][1]['bookable_net_difference'], '1')

    def test_ring_disconnected_after_dropping_cd(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources_dir, plan_path = ring_case(tmp)
            plan = json.loads(plan_path.read_text())
            plan['panels'] = [p for p in plan['panels'] if p['id'] != 'CD']
            plan_path.write_text(json.dumps(plan))
            report = reconcile(str(sources_dir), str(plan_path))
        self.assertEqual(report['status'], 'unreconcileable')
        self.assertEqual(report['unreconcileable_reason'],
                         'full_graph_disconnected')
        self.assertEqual(report['removable_panel_ids'], [])
        for query in report['queries']:
            self.assertIsNone(query['min_net_difference'])
            self.assertIsNone(query['max_net_difference'])
            self.assertIsNone(query['min_witness_panel'])
            self.assertIsNone(query['max_witness_panel'])
            self.assertIsNone(query['bookable_net_difference'])
            self.assertIn(query['left_local_concentration'], ('0', '2'))

    def test_parallel_panels_two_analytes_have_no_common_repair(self):
        windows = [('X', (Fraction(0), Fraction(2))),
                   ('Y', (Fraction(3), Fraction(5)))]
        def src(values):
            return build_source(
                windows,
                [('s0', {'X': Fraction(0), 'Y': Fraction(0)}),
                 ('s1', {'X': Fraction(1), 'Y': Fraction(1)})],
                values)
        left = {f'p{i}a': v for i, v in enumerate(
            [{'X': Fraction(1), 'Y': Fraction(0)},
             {'X': Fraction(0), 'Y': Fraction(0)},
             {'X': Fraction(0), 'Y': Fraction(0)}], 1)}
        right = {f'p{i}b': v for i, v in enumerate(
            [{'X': Fraction(0), 'Y': Fraction(0)},
             {'X': Fraction(0), 'Y': Fraction(1)},
             {'X': Fraction(0), 'Y': Fraction(0)}], 1)}
        with tempfile.TemporaryDirectory() as tmp:
            sources_dir, plan_path = write_case(tmp, {
                'A': src(left), 'B': src(right)}, {
                'batches': {'A': 'A', 'B': 'B'},
                'anchor': 'A',
                'analytes': ['X', 'Y'],
                'panels': [
                    {'id': f'P{i}',
                     'left': ['A', f'p{i}a'],
                     'right': ['B', f'p{i}b']} for i in (1, 2, 3)],
                'queries': [
                    {'id': 'qX', 'analyte_id': 'X',
                     'left': ['A', 'p1a'], 'right': ['B', 'p1b']},
                    {'id': 'qY', 'analyte_id': 'Y',
                     'left': ['A', 'p2a'], 'right': ['B', 'p2b']}],
            })
            # Single-analyte plans must expose the per-analyte repair.
            for analyte, expected in (('X', 'P1'), ('Y', 'P2')):
                one = json.loads(plan_path.read_text())
                one['analytes'] = [analyte]
                one['queries'] = [q for q in one['queries']
                                  if q['analyte_id'] == analyte]
                one_path = plan_path.with_name(f'plan-{analyte}.json')
                one_path.write_text(json.dumps(one))
                report = reconcile(str(sources_dir), str(one_path))
                self.assertEqual(report['status'], 'single_panel_repairable')
                self.assertEqual(report['removable_panel_ids'], [expected])
            both = reconcile(str(sources_dir), str(plan_path))
        self.assertEqual(both['status'], 'unreconcileable')
        self.assertEqual(both['unreconcileable_reason'],
                         'no_common_single_panel_repair')
        self.assertEqual(both['removable_panel_ids'], [])
        for query in both['queries']:
            self.assertIsNone(query['bookable_net_difference'])
            self.assertIsNone(query['min_net_difference'])

    def test_different_curve_intercepts_negative_and_extrapolated_readings(self):
        # Batch A: triangle area = concentration (slope 1, intercept 0).
        # Batch B: areas 3 and 5 at standard concentrations 0 and 1, so the
        # fit is area = 2c + 3 with a non-zero intercept.
        windows = [('X', (Fraction(0), Fraction(2)))]
        source_a = build_source(
            windows,
            [('s0', {'X': Fraction(0)}), ('s1', {'X': Fraction(1)})],
            {'u': {'X': Fraction(-2)}})  # negative and below standard range
        source_b = {
            'analytes': [{'id': 'X', 'window': ['0', '2']}],
            'runs': [
                {'id': 's0', 'kind': 'standard',
                 'concentrations': {'X': '0'},
                 'samples': [['0', '0'], ['1', '3'], ['2', '0']]},
                {'id': 's1', 'kind': 'standard',
                 'concentrations': {'X': '1'},
                 'samples': [['0', '0'], ['1', '5'], ['2', '0']]},
                # Area 13 -> local concentration (13 - 3)/2 = 5, above the
                # standard range, so the reading is extrapolated but kept.
                {'id': 'u', 'kind': 'unknown',
                 'samples': [['0', '0'], ['1', '13'], ['2', '0']]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            sources_dir, plan_path = write_case(tmp, {
                'A': source_a, 'B': source_b}, {
                'batches': {'A': 'A', 'B': 'B'},
                'anchor': 'A',
                'analytes': ['X'],
                'panels': [{'id': 'P', 'left': ['A', 'u'],
                            'right': ['B', 'u']}],
                'queries': [{'id': 'q', 'analyte_id': 'X',
                             'left': ['A', 'u'], 'right': ['B', 'u']}]})
            report = reconcile(str(sources_dir), str(plan_path))
        query, = report['queries']
        self.assertEqual(query['left_local_concentration'], '-2')
        self.assertEqual(query['right_local_concentration'], '5')
        self.assertEqual(report['status'], 'all_panels_consistent')
        # The additive correction aligns the same virtual control exactly, so
        # the unified left-minus-right net difference is zero and bookable.
        self.assertEqual(query['bookable_net_difference'], '0')
        self.assertEqual(query['min_witness_panel'], None)

    def test_input_reordering_is_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources_dir, plan_path = ring_case(tmp)
            first = Path(tmp) / 'r1.json'
            second = Path(tmp) / 'r2.json'
            third = Path(tmp) / 'r3.json'
            self._run_cli(sources_dir, plan_path, first)
            plan = json.loads(plan_path.read_text())
            plan['batches'] = {k: plan['batches'][k]
                               for k in reversed(list(plan['batches']))}
            plan['panels'] = list(reversed(plan['panels']))
            plan['analytes'] = list(reversed(plan['analytes']))
            plan['queries'] = list(reversed(plan['queries']))
            plan_path.write_text(json.dumps(plan))
            self._run_cli(sources_dir, plan_path, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            # Permuting the v1 source's runs (no business order there) must
            # also leave the report byte-identical.
            for path in sources_dir.glob('*.json'):
                source = json.loads(path.read_text())
                source['runs'] = list(reversed(source['runs']))
                path.write_text(json.dumps(source))
            self._run_cli(sources_dir, plan_path, third)
            self.assertEqual(first.read_bytes(), third.read_bytes())

    def test_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources_dir, plan_path = ring_case(tmp)
            raw = plan_path.read_text()
            raw = raw.replace('"anchor": "A"', '"anchor": "B", "anchor": "A"', 1)
            plan_path.write_text(raw)
            output = Path(tmp) / 'out.json'
            with self.assertRaises(ValueError):
                reconcile(str(sources_dir), str(plan_path))
            self.assertFalse(output.exists())

    def test_invalid_plan_and_reference_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources_dir, plan_path = ring_case(tmp)

            def reject(mutator, message):
                plan = json.loads(plan_path.read_text())
                mutator(plan)
                broken = plan_path.with_name('broken.json')
                broken.write_text(json.dumps(plan))
                with self.assertRaises((ValueError, KeyError), msg=message):
                    reconcile(str(sources_dir), str(broken))

            reject(lambda p: p.pop('anchor'), 'missing field')
            reject(lambda p: p.update(extra=1), 'extra field')
            reject(lambda p: p.update(anchor='Z'), 'anchor not a batch')
            reject(lambda p: p.update(analytes=['X', 'X']), 'duplicate analyte')
            reject(lambda p: p.update(analytes=['NOPE']), 'missing analyte in source')
            reject(lambda p: p.update(analytes=['X', 'Y', 'Z', 'Q', 'R']),
                   'too many analytes')
            reject(lambda p: p['panels'][1].update(id='AB'),
                   'duplicate panel id')
            reject(lambda p: p['panels'][0].update(
                right=['A', 'u0']), 'same-batch panel endpoints')
            reject(lambda p: p['panels'][0].update(
                left=['A', 's0']), 'standard run reference')
            reject(lambda p: p['panels'][0].update(
                left=['Z', 'u0']), 'unknown batch reference')
            reject(lambda p: p['queries'][0].update(
                analyte_id='Y'), 'query analyte not selected')
            reject(lambda p: p.update(queries=[]), 'empty queries')
            reject(lambda p: p.update(queries=[
                {'id': f'q{i}', 'analyte_id': 'X',
                 'left': ['A', 'u0'], 'right': ['B', 'u0']}
                for i in range(17)]), 'query cap')
            reject(lambda p: p.update(panels=p['panels'] + [
                {'id': f'X{i:05d}', 'left': ['A', 'u0'],
                 'right': ['B', 'u0']}
                for i in range(24001 - len(p['panels']))]), 'panel cap')
            # Too many batches / panels / files are guarded by explicit caps.
            reject(lambda p: p.update(batches={**p['batches'],
                                               **{f'E{i}': 'A' for i in range(11997)}}),
                   'batch cap')

    def test_distinct_source_file_cap_is_enforced(self):
        windows = [('X', (Fraction(0), Fraction(2)))]
        source = build_source(
            windows,
            [('s0', {'X': Fraction(0)}), ('s1', {'X': Fraction(1)})],
            {'u': {'X': Fraction(0)}})
        with tempfile.TemporaryDirectory() as tmp:
            sources_dir, plan_path = ring_case(tmp)
            for index in range(257):
                (sources_dir / f'f{index:03d}.json').write_text(
                    json.dumps(source))
            plan = json.loads(plan_path.read_text())
            plan['batches'] = {f'b{index:03d}': f'f{index:03d}'
                               for index in range(257)}
            plan['panels'] = []
            plan_path.write_text(json.dumps(plan))
            with self.assertRaises(ReconcileError):
                reconcile(str(sources_dir), str(plan_path))

    def test_fit_failure_is_a_command_error_without_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources_dir, plan_path = ring_case(tmp)
            source = json.loads((sources_dir / 'A.json').read_text())
            # Two standards with identical concentrations -> unidentifiable fit.
            source['runs'][1]['concentrations']['X'] = '0'
            (sources_dir / 'A.json').write_text(json.dumps(source))
            output = Path(tmp) / 'out.json'
            with self.assertRaises(ReconcileError):
                reconcile(str(sources_dir), str(plan_path))
            self.assertFalse(output.exists())

    def test_existing_file_and_symlink_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources_dir, plan_path = ring_case(tmp)
            output = Path(tmp) / 'out.json'
            output.write_text('keeper')
            stderr = []
            code = cli.main(['reconcile', '--sources', str(sources_dir),
                             '--plan', str(plan_path), '--output', str(output)])
            self.assertEqual(code, 2)
            self.assertEqual(output.read_text(), 'keeper')
            link = Path(tmp) / 'link.json'
            os.symlink(output, link)
            code = cli.main(['reconcile', '--sources', str(sources_dir),
                             '--plan', str(plan_path), '--output', str(link)])
            self.assertEqual(code, 2)
            self.assertTrue(link.is_symlink())
            self.assertEqual(output.read_text(), 'keeper')

    def test_empty_panels_connect_only_the_anchor(self):
        windows = [('X', (Fraction(0), Fraction(2)))]
        source = build_source(
            windows,
            [('s0', {'X': Fraction(0)}), ('s1', {'X': Fraction(1)})],
            {'u': {'X': Fraction(0)}})
        with tempfile.TemporaryDirectory() as tmp:
            sources_dir, plan_path = write_case(tmp, {
                'A': source, 'B': source}, {
                'batches': {'A': 'A', 'B': 'B'},
                'anchor': 'A', 'analytes': ['X'], 'panels': [],
                'queries': [{'id': 'q', 'analyte_id': 'X',
                             'left': ['A', 'u'], 'right': ['B', 'u']}]})
            report = reconcile(str(sources_dir), str(plan_path))
        self.assertEqual(report['status'], 'unreconcileable')
        self.assertEqual(report['unreconcileable_reason'],
                         'full_graph_disconnected')
        query, = report['queries']
        self.assertEqual(query['left_local_concentration'], '0')
        self.assertIsNone(query['bookable_net_difference'])

    def test_brute_force_oracle_on_randomized_small_instances(self):
        import random
        rng = random.Random(20261123)
        windows = [('X', (Fraction(0), Fraction(2))),
                   ('Y', (Fraction(3), Fraction(5)))]
        standards = [('s0', {'X': Fraction(0), 'Y': Fraction(0)}),
                     ('s1', {'X': Fraction(1), 'Y': Fraction(1)})]
        for trial in range(40):
            batch_count = rng.randint(2, 5)
            analyte_count = rng.randint(1, 2)
            analyte_ids = ['X', 'Y'][:analyte_count]
            batch_ids = [f'b{i}' for i in range(batch_count)]
            sources = {}
            local = {}
            run_counter = 0
            for batch_id in batch_ids:
                unknowns = {}
                for _ in range(rng.randint(2, 4)):
                    run_counter += 1
                    run_id = f'u{run_counter:03d}'
                    unknowns[run_id] = {
                        a: Fraction(rng.randint(-3, 3)) for a in analyte_ids}
                    for analyte in analyte_ids:
                        local.setdefault(batch_id, {})[(run_id, analyte)] = \
                            unknowns[run_id][analyte]
                sources[batch_id] = build_source(
                    windows[:analyte_count], standards, unknowns)
            panels = []
            runs_by_batch = {b: sorted({r for (r, _a) in local[b]})
                             for b in batch_ids}
            for panel_index in range(rng.randint(batch_count - 1,
                                                 batch_count + 3)):
                left_batch, right_batch = rng.sample(batch_ids, 2)
                left_run = rng.choice(runs_by_batch[left_batch])
                right_run = rng.choice(runs_by_batch[right_batch])
                panels.append({'id': f'p{panel_index:02d}',
                               'left': [left_batch, left_run],
                               'right': [right_batch, right_run]})
            queries = []
            for query_index in range(rng.randint(1, 3)):
                batch_left, batch_right = rng.sample(batch_ids, 2)
                run_left = rng.choice(runs_by_batch[batch_left])
                run_right = rng.choice(runs_by_batch[batch_right])
                queries.append({'id': f'q{query_index}',
                                'analyte_id': rng.choice(analyte_ids),
                                'left': [batch_left, run_left],
                                'right': [batch_right, run_right]})
            plan = {'batches': {b: b for b in batch_ids},
                    'anchor': batch_ids[0],
                    'analytes': analyte_ids,
                    'panels': panels, 'queries': queries}
            with tempfile.TemporaryDirectory() as tmp:
                sources_dir, plan_path = write_case(tmp, sources, plan)
                report = reconcile(str(sources_dir), str(plan_path))
            reason, removable, scenarios = oracle(plan, local)
            if reason == 'full_graph_disconnected':
                self.assertEqual(report['status'], 'unreconcileable')
                self.assertEqual(report['unreconcileable_reason'], reason)
                continue
            if reason == 'all_panels_consistent':
                self.assertEqual(report['status'], 'all_panels_consistent')
                scenario_keys = [None]
            elif reason == 'no_common_single_panel_repair':
                self.assertEqual(report['status'], 'unreconcileable')
                self.assertEqual(report['unreconcileable_reason'], reason)
                continue
            else:
                self.assertEqual(report['status'], 'single_panel_repairable')
                self.assertEqual(report['removable_panel_ids'], removable)
                scenario_keys = removable
            for query in report['queries']:
                analyte = query['analyte_id']
                lb, lr = query['left']
                rb, rr = query['right']
                nets = []
                for removed in scenario_keys:
                    corrections = scenarios[removed][analyte]
                    net = (local[lb][(lr, analyte)] + corrections[lb]
                           - local[rb][(rr, analyte)] - corrections[rb])
                    nets.append((removed, net))
                # Both extrema take the lexicographically smallest panel id on
                # ties, so select on value first and id second in both cases.
                minimum = min(nets, key=lambda item: (item[1],
                                                      '' if item[0] is None
                                                      else item[0]))
                maximum_value = max(value for _panel, value in nets)
                maximum = min(
                    (item for item in nets if item[1] == maximum_value),
                    key=lambda item: '' if item[0] is None else item[0])
                self.assertEqual(query['min_net_difference'], str(minimum[1]))
                self.assertEqual(query['max_net_difference'], str(maximum[1]))
                self.assertEqual(query['min_witness_panel'], minimum[0])
                self.assertEqual(query['max_witness_panel'], maximum[0])
                if minimum[1] == maximum[1]:
                    self.assertEqual(query['bookable_net_difference'],
                                     str(minimum[1]))
                else:
                    self.assertIsNone(query['bookable_net_difference'])

    def test_month_end_demo_runs_under_cpu_budget_and_full_contents(self):
        driver = ROOT / 'scripts' / 'reconcile_month_end_demo.py'
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [sys.executable, '-I', str(driver), '--workdir',
                 str(Path(tmp) / 'month-end'), '--cpu', '90'],
                capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report_path = Path(tmp) / 'month-end' / 'reconcile-report.json'
            report = json.loads(report_path.read_text())
            self.assertEqual(len(report['removable_panel_ids']), 12_000)
            self.assertEqual(report['removable_panel_ids'][0], 'p00000')
            self.assertEqual(report['removable_panel_ids'][-1], 'p11999')
            query, = report['queries']
            self.assertEqual(query['min_net_difference'], '1')
            self.assertEqual(query['max_net_difference'], '2')
            self.assertEqual(query['min_witness_panel'], 'p00000')
            self.assertEqual(query['max_witness_panel'], 'p05999')
            self.assertIsNone(query['bookable_net_difference'])

    def test_same_batch_query_and_panels_sharing_runs_are_allowed(self):
        windows = [('X', (Fraction(0), Fraction(2)))]
        def src(unknowns):
            return build_source(
                windows,
                [('s0', {'X': Fraction(0)}), ('s1', {'X': Fraction(1)})],
                unknowns)
        sources = {
            'A': src({'u': {'X': Fraction(1)}, 'v': {'X': Fraction(2)}}),
            'B': src({'u': {'X': Fraction(1)}}),
            'C': src({'u': {'X': Fraction(1)}}),
        }
        # Two parallel panels between the same batch pair, reusing one run,
        # and a query whose two endpoints live in the same batch.
        plan = {
            'batches': {'A': 'A', 'B': 'B', 'C': 'C'},
            'anchor': 'A', 'analytes': ['X'],
            'panels': [
                {'id': 'P1', 'left': ['A', 'u'], 'right': ['B', 'u']},
                {'id': 'P2', 'left': ['A', 'u'], 'right': ['C', 'u']}],
            'queries': [
                {'id': 'same-batch', 'analyte_id': 'X',
                 'left': ['A', 'v'], 'right': ['A', 'u']}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            sources_dir, plan_path = write_case(tmp, sources, plan)
            report = reconcile(str(sources_dir), str(plan_path))
        query, = report['queries']
        self.assertEqual(report['status'], 'all_panels_consistent')
        # Same batch => identical correction cancels; 2 - 1 = 1 exactly.
        self.assertEqual(query['bookable_net_difference'], '1')

    def test_nonpositive_slope_fails_even_when_another_batch_fits(self):
        windows = [('X', (Fraction(0), Fraction(2)))]
        good = build_source(
            windows,
            [('s0', {'X': Fraction(0)}), ('s1', {'X': Fraction(1)})],
            {'u': {'X': Fraction(0)}})
        flat = {
            'analytes': [{'id': 'X', 'window': ['0', '2']}],
            'runs': [
                {'id': 's0', 'kind': 'standard',
                 'concentrations': {'X': '0'},
                 'samples': [['0', '0'], ['1', '0'], ['2', '0']]},
                {'id': 's1', 'kind': 'standard',
                 'concentrations': {'X': '1'},
                 'samples': [['0', '0'], ['1', '0'], ['2', '0']]},
                {'id': 'u', 'kind': 'unknown',
                 'samples': [['0', '0'], ['1', '0'], ['2', '0']]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            sources_dir, plan_path = write_case(tmp, {
                'A': good, 'B': flat}, {
                'batches': {'A': 'A', 'B': 'B'},
                'anchor': 'A', 'analytes': ['X'],
                'panels': [{'id': 'P', 'left': ['A', 'u'],
                            'right': ['B', 'u']}],
                'queries': [{'id': 'q', 'analyte_id': 'X',
                             'left': ['A', 'u'], 'right': ['B', 'u']}]})
            output = Path(tmp) / 'out.json'
            with self.assertRaises(ReconcileError):
                reconcile(str(sources_dir), str(plan_path))
            self.assertFalse(output.exists())

    def test_checked_in_fixture_matches_the_acceptance_contract(self):
        fixture = ROOT / 'fixtures' / 'reconcile'
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'report.json'
            code = cli.main([
                'reconcile', '--sources', str(fixture / 'sources'),
                '--plan', str(fixture / 'plan.json'),
                '--output', str(output)])
            self.assertEqual(code, 0)
            report = json.loads(output.read_text())
        self.assertEqual(report['removable_panel_ids'], ['AB', 'BC', 'CA'])
        first, second = report['queries']
        self.assertEqual((first['min_net_difference'],
                          first['min_witness_panel'],
                          first['max_net_difference'],
                          first['max_witness_panel'],
                          first['bookable_net_difference']),
                         ('0', 'AB', '1', 'BC', None))
        self.assertEqual((second['min_net_difference'],
                          second['max_net_difference'],
                          second['min_witness_panel'],
                          second['bookable_net_difference']),
                         ('1', '1', 'AB', '1'))

    def test_legacy_v1_command_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'legacy.json'
            code = cli.main(['--source', str(ROOT / 'fixtures' / 'input.json'),
                             '--output', str(output)])
            self.assertEqual(code, 0)
            report = json.loads(output.read_text())
            self.assertEqual(report['schema_version'], 1)
            self.assertEqual(report['quantified'][0]['concentration'], '1')


if __name__ == '__main__':
    unittest.main()
