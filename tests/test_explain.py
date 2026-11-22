"""Acceptance and contract tests for explain packages and contrast queries.

The brute-force oracle below is intentionally independent of
peak_quant.selection: it enumerates every feasible explanation on small
instances and recomputes the optimum indicators, attainable sets and pinned
extrema directly, so the tested DP is never used as its own reference.
"""
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
from itertools import product
from pathlib import Path

from peak_quant import cli
from peak_quant.candidates import parse_candidates
from peak_quant.contrast import answer_query, parse_query, validate_package
from peak_quant.explain import PACKAGE_VERSION, build_package, encode_package
from peak_quant.selection import (attainable, best_metrics, constrained_extrema,
                                  representative)


# --------------------------------------------------------------------- helpers

def dec(value):
    """Format an exact Fraction as a v1 bounded decimal string (denominator
    must divide 10**6); test geometry only uses halves and integers."""
    fraction = Fraction(value) * 10 ** 6
    assert fraction.denominator == 1, f'{value} needs more than six decimals'
    numerator = fraction.numerator
    sign = '-' if numerator < 0 else ''
    whole, frac = divmod(abs(numerator), 10 ** 6)
    if frac == 0:
        return f'{sign}{whole}'
    return f'{sign}{whole}.{str(frac).rstrip("0")}'


def triangle_samples(points, span):
    """points: list of (lo, hi, height) triangles sharing a zero baseline."""
    levels = {Fraction(0): Fraction(0), Fraction(span): Fraction(0)}
    for lo, hi, height in points:
        mid = (lo + hi) / 2
        levels[lo] = levels.get(lo, Fraction(0))
        levels[hi] = levels.get(hi, Fraction(0))
        levels[mid] = levels.get(mid, Fraction(0)) + height
    return [[dec(t), dec(levels[t])] for t in sorted(levels)]


def standard_run(run_id, concentrations, window_areas, span, analyte_ids=None):
    """concentrations: dict analyte id -> concentration string."""
    points = []
    for lo, hi, area in window_areas:
        if area != 0:
            height = 2 * Fraction(area) / (Fraction(hi) - Fraction(lo))
            points.append((Fraction(lo), Fraction(hi), height))
    rows = triangle_samples(points, span)
    ids = analyte_ids if analyte_ids is not None else [
        f'A{i}' for i in range(len(window_areas))]
    return {'id': run_id, 'kind': 'standard',
            'concentrations': {analyte_id: str(concentrations[analyte_id])
                               for analyte_id in ids},
            'samples': rows}


def write_json(directory, name, payload):
    path = Path(directory) / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return str(path)


def invoke(argv):
    buffer = io.BytesIO()

    class Out:
        flush = staticmethod(lambda: None)
    Out.buffer = buffer
    with redirect_stdout(Out()), redirect_stderr(io.StringIO()) as err:
        code = cli.main(argv)
    return code, buffer.getvalue(), err.getvalue()


# ------------------------------------------------------- independent brute force

def brute_force(options):
    """Enumerate every feasible explanation; return (best_witnesses, metrics).

    options[i]: list of dicts with id/lo/hi/penalty/concentration.
    Feasibility: adjacent selected windows satisfy end <= start.
    """
    n = len(options)
    feasible = []
    for choice in product(*([None] + list(enumerate(opts)) for opts in options)):
        witness = []
        last_hi = None
        ok = True
        for picked, opts in zip(choice, options):
            if picked is None:
                witness.append(None)
                continue
            option = opts[picked[0]]
            if last_hi is not None and last_hi > option['lo']:
                ok = False
                break
            witness.append(option)
            last_hi = option['hi']
        if ok:
            feasible.append(witness)
    count = max(sum(1 for entry in w if entry is not None) for w in feasible)
    top = [w for w in feasible
           if sum(1 for entry in w if entry is not None) == count]
    penalty = min(sum(entry['penalty'] for entry in w if entry is not None) for w in top)
    best = [w for w in top
            if sum(entry['penalty'] for entry in w if entry is not None) == penalty]
    return best, (count, penalty)


def brute_attainable(options):
    best, _ = brute_force(options)
    result = []
    for i in range(len(options)):
        ids = {w[i]['id'] for w in best if w[i] is not None}
        vacancy = any(w[i] is None for w in best)
        result.append((ids, vacancy))
    return result


def brute_extrema(options, pins, term_positions, coefficients):
    best, _ = brute_force(options)
    scored = []
    for witness in best:
        if any((witness[i] is None) != (candidate_id is None)
               or (candidate_id is not None and witness[i]['id'] != candidate_id)
               for i, candidate_id in pins.items()):
            continue
        if any(witness[i] is None for i in term_positions):
            continue
        score = sum(Fraction(coefficients[i]) * witness[i]['concentration']
                    for i in term_positions)
        scored.append((score, witness))
    if not scored:
        return None
    witness_key = lambda witness: tuple((0, e['id']) if e else (1, '')
                                        for e in witness)
    min_score = min(score for score, _ in scored)
    minimum = min((item for item in scored if item[0] == min_score),
                  key=lambda item: witness_key(item[1]))
    max_score = max(score for score, _ in scored)
    maximum = min((item for item in scored if item[0] == max_score),
                  key=lambda item: witness_key(item[1]))
    return minimum, maximum


# --------------------------------------------------------------------- fixtures

def acceptance_source():
    span = 6
    ids = ['A', 'B']
    standards = [
        standard_run('s0', {'A': 0, 'B': 0}, [(0, 2, 0), (2, 4, 0)], span, ids),
        standard_run('s1', {'A': 1, 'B': 3}, [(0, 2, 1), (2, 4, 3)], span, ids),
        standard_run('s2', {'A': 2, 'B': 6}, [(0, 2, 2), (2, 4, 6)], span, ids),
    ]
    unknown = {'id': 'u1', 'kind': 'unknown',
               'samples': triangle_samples(
                   [(Fraction(0), Fraction(2), 1),
                    (Fraction(2), Fraction(4), 3),
                    (Fraction(4), Fraction(6), 5)], span)}
    return {
        'analytes': [{'id': 'A', 'window': ['0', '2']},
                     {'id': 'B', 'window': ['2', '4']}],
        'runs': standards + [unknown],
    }


def acceptance_candidates(b2_penalty=0):
    return {
        'analyte_order': ['A', 'B'],
        'candidates': [
            {'run_id': 'u1', 'analyte_id': 'A', 'candidate_id': 'a1',
             'window': ['0', '2'], 'penalty': 0},
            {'run_id': 'u1', 'analyte_id': 'A', 'candidate_id': 'a2',
             'window': ['2', '4'], 'penalty': 0},
            {'run_id': 'u1', 'analyte_id': 'B', 'candidate_id': 'b1',
             'window': ['2', '4'], 'penalty': 0},
            {'run_id': 'u1', 'analyte_id': 'B', 'candidate_id': 'b2',
             'window': ['4', '6'], 'penalty': b2_penalty},
        ],
    }


def options_from_package(package, run_id):
    grouped = {}
    for entry in package['candidates']:
        if entry['run_id'] != run_id:
            continue
        grouped.setdefault(entry['analyte_id'], []).append({
            'id': entry['candidate_id'],
            'lo': Fraction(entry['window'][0]),
            'hi': Fraction(entry['window'][1]),
            'penalty': entry['penalty'],
            'concentration': Fraction(entry['concentration']),
        })
    return [grouped.get(analyte_id, []) for analyte_id in package['analyte_order']]


class ExplainAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def package(self, b2_penalty=0):
        source = write_json(self.directory, 'input.json', acceptance_source())
        candidates = write_json(self.directory, 'candidates.json',
                                acceptance_candidates(b2_penalty))
        raw_source = Path(source).read_bytes()
        raw_candidates = Path(candidates).read_bytes()
        return build_package(raw_source, raw_candidates)

    def test_package_metrics_representative_and_attainable(self):
        package = self.package()
        block = package['runs'][0]
        self.assertEqual(package['package_version'], PACKAGE_VERSION)
        self.assertEqual((block['best_selected'], block['best_penalty']), (2, 0))
        self.assertEqual(block['representative'], ['a1', 'b1'])
        slots = {slot['analyte_id']: slot for slot in block['attainable']}
        self.assertEqual(slots['A']['candidate_ids'], ['a1', 'a2'])
        self.assertEqual(slots['B']['candidate_ids'], ['b1', 'b2'])
        self.assertFalse(slots['A']['vacancy_available'])
        self.assertFalse(slots['B']['vacancy_available'])
        self.assertEqual(len(package['candidates']), 4)
        concentrations = {entry['candidate_id']: entry['concentration']
                          for entry in package['candidates']}
        self.assertEqual(concentrations, {'a1': '1', 'a2': '3', 'b1': '3', 'b2': '5'})
        a2 = next(entry for entry in package['candidates']
                  if entry['candidate_id'] == 'a2')
        self.assertTrue(a2['extrapolated'])
        self.assertFalse(a2['negative'])

    def test_contrast_min_max_and_tie_witness_rule(self):
        package = self.package()
        query = {'run_id': 'u1', 'terms': {'A': 1, 'B': -1}, 'pins': {}}
        report = answer_query(package, query)
        result = report['result']
        self.assertEqual(result['status'], 'joint_explanation')
        self.assertEqual(result['minimum']['score'], '-4')
        self.assertEqual(result['maximum']['score'], '-2')
        min_ids = [e['candidate_id'] for e in result['minimum']['witness']
                   if e['selected']]
        max_ids = [e['candidate_id'] for e in result['maximum']['witness']
                   if e['selected']]
        self.assertEqual(min_ids, ['a1', 'b2'])
        self.assertEqual(max_ids, ['a1', 'b1'])
        # The witness carries every slot, including vacancies and exact values.
        witness = result['maximum']['witness']
        self.assertEqual(len(witness), 2)
        self.assertTrue(all(e['selected'] for e in witness))
        values = {e['analyte_id']: e['concentration'] for e in witness}
        self.assertEqual(values, {'A': '1', 'B': '3'})
        self.assertEqual((report['best_selected'], report['best_penalty']), (2, 0))

    def test_pins_filter_without_relaxing_the_best_set(self):
        package = self.package()
        pinned = answer_query(package, {
            'run_id': 'u1', 'terms': {'A': 1, 'B': -1}, 'pins': {'A': 'a2'}})
        result = pinned['result']
        self.assertEqual(result['minimum']['score'], '-2')
        self.assertEqual(result['maximum']['score'], '-2')
        ids = [e['candidate_id'] for e in result['minimum']['witness']
               if e['selected']]
        self.assertEqual(ids, ['a2', 'b2'])
        conflicting = answer_query(package, {
            'run_id': 'u1', 'terms': {'A': 1, 'B': -1},
            'pins': {'A': 'a2', 'B': 'b1'}})
        self.assertEqual(conflicting['result']['status'], 'no_joint_explanation')
        self.assertIsNone(conflicting['result']['minimum'])
        self.assertIsNone(conflicting['result']['maximum'])

    def test_non_term_pin_is_kept_out_of_the_score(self):
        package = self.package()
        report = answer_query(package, {
            'run_id': 'u1', 'terms': {'A': 1}, 'pins': {'B': 'b2'}})
        result = report['result']
        self.assertEqual(result['status'], 'joint_explanation')
        self.assertEqual(result['minimum']['score'], '1')
        by_analyte = {entry['analyte_id']: entry
                      for entry in result['minimum']['witness']}
        self.assertEqual(by_analyte['B']['candidate_id'], 'b2')
        self.assertFalse(by_analyte['B']['included_in_score'])
        self.assertTrue(by_analyte['A']['included_in_score'])

    def test_null_pin_requires_vacancy_and_is_reported(self):
        source = {
            'analytes': [{'id': 'A', 'window': ['0', '2']},
                         {'id': 'B', 'window': ['2', '4']}],
            'runs': [
                {'id': 's0', 'kind': 'standard',
                 'concentrations': {'A': '0', 'B': '0'},
                 'samples': [['0', '0'], ['1', '0'], ['2', '0'],
                             ['3', '0'], ['4', '0']]},
                {'id': 's1', 'kind': 'standard',
                 'concentrations': {'A': '1', 'B': '1'},
                 'samples': [['0', '0'], ['1', '1'], ['2', '0'],
                             ['3', '1'], ['4', '0']]},
                {'id': 'u1', 'kind': 'unknown',
                 'samples': [['0', '0'], ['1', '1'], ['2', '0'],
                             ['3', '2'], ['4', '0']]},
            ],
        }
        candidates = {
            'analyte_order': ['A', 'B'],
            'candidates': [
                {'run_id': 'u1', 'analyte_id': 'A', 'candidate_id': 'a1',
                 'window': ['0', '3'], 'penalty': 0},
                {'run_id': 'u1', 'analyte_id': 'B', 'candidate_id': 'b1',
                 'window': ['1', '4'], 'penalty': 0},
            ],
        }
        package = build_package(json.dumps(source).encode(),
                                json.dumps(candidates).encode())
        self.assertEqual(package['runs'][0]['best_selected'], 1)
        report = answer_query(package, {
            'run_id': 'u1', 'terms': {'B': 1}, 'pins': {'A': None}})
        self.assertEqual(report['pins'], {'A': None})
        result = report['result']
        self.assertEqual(result['status'], 'joint_explanation')
        witness = {entry['analyte_id']: entry
                   for entry in result['maximum']['witness']}
        self.assertFalse(witness['A']['selected'])
        self.assertIsNone(witness['A']['candidate_id'])
        self.assertEqual(witness['B']['candidate_id'], 'b1')
        self.assertTrue(witness['B']['included_in_score'])

    def test_coefficient_bounds(self):
        package = self.package()
        parse_query({'run_id': 'u1',
                     'terms': {'A': 1000, 'B': -1000}, 'pins': {}}, package)
        for bad in (0, 1001, -1001, True):
            with self.assertRaises(ValueError):
                parse_query({'run_id': 'u1',
                             'terms': {'A': bad}, 'pins': {}}, package)

    def test_penalty_change_narrows_the_best_set(self):
        package = self.package(b2_penalty=1)
        block = package['runs'][0]
        self.assertEqual(block['representative'], ['a1', 'b1'])
        self.assertEqual((block['best_selected'], block['best_penalty']), (2, 0))
        slots = {slot['analyte_id']: slot for slot in block['attainable']}
        self.assertEqual(slots['A']['candidate_ids'], ['a1'])
        self.assertEqual(slots['B']['candidate_ids'], ['b1'])
        report = answer_query(package, {
            'run_id': 'u1', 'terms': {'A': 1, 'B': -1}, 'pins': {}})
        self.assertEqual(report['result']['minimum']['score'], '-2')
        self.assertEqual(report['result']['maximum']['score'], '-2')
        pinned = answer_query(package, {
            'run_id': 'u1', 'terms': {'A': 1, 'B': -1}, 'pins': {'A': 'a2'}})
        # a2 is registered but cannot appear in a minimum-penalty explanation.
        self.assertEqual(pinned['result']['status'], 'no_joint_explanation')

    def test_deterministic_bytes_and_output_refusal(self):
        source = write_json(self.directory, 'input.json', acceptance_source())
        candidates = write_json(self.directory, 'candidates.json',
                                acceptance_candidates())
        out1 = str(self.directory / 'p1.json')
        out2 = str(self.directory / 'p2.json')
        self.assertEqual(invoke(['explain', '--source', source,
                                 '--candidates', candidates, '--output', out1])[0], 0)
        self.assertEqual(invoke(['explain', '--source', source,
                                 '--candidates', candidates, '--output', out2])[0], 0)
        self.assertEqual(Path(out1).read_bytes(), Path(out2).read_bytes())
        code, _, error = invoke(['explain', '--source', source,
                                 '--candidates', candidates, '--output', out1])
        self.assertEqual(code, 2)
        self.assertIn('File exists', error)
        link = str(self.directory / 'link.json')
        os.symlink(out1, link)
        code, _, error = invoke(['explain', '--source', source,
                                 '--candidates', candidates, '--output', link])
        self.assertEqual(code, 2)
        self.assertEqual(os.readlink(link), out1)

    def test_candidate_array_order_does_not_change_package_bytes(self):
        source_raw = json.dumps(acceptance_source()).encode()
        ordered = acceptance_candidates()
        shuffled = json.loads(json.dumps(ordered))
        shuffled['candidates'] = list(reversed(shuffled['candidates']))
        first = encode_package(build_package(source_raw, json.dumps(ordered).encode()))
        second = encode_package(
            build_package(source_raw, json.dumps(shuffled).encode()))
        self.assertEqual(first, second)

    def test_offline_query_after_inputs_are_moved_away(self):
        source = write_json(self.directory, 'input.json', acceptance_source())
        candidates = write_json(self.directory, 'candidates.json',
                                acceptance_candidates())
        package_path = str(self.directory / 'pkg.json')
        self.assertEqual(invoke(['explain', '--source', source,
                                 '--candidates', candidates,
                                 '--output', package_path])[0], 0)
        isolated = Path(self.tmp.name) / 'offline'
        isolated.mkdir()
        query_path = write_json(isolated, 'query.json',
                                {'run_id': 'u1', 'terms': {'A': 1, 'B': -1},
                                 'pins': {}})
        moved = Path(self.tmp.name) / 'moved'
        moved.mkdir()
        Path(source).rename(moved / 'input.json')
        Path(candidates).rename(moved / 'candidates.json')
        copied_package = isolated / 'package.json'
        copied_package.write_bytes(Path(package_path).read_bytes())
        report_path = isolated / 'report.json'
        # A fresh CLI process with only the package and query in its directory.
        env = dict(os.environ)
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
        completed = subprocess.run(
            [sys.executable, '-m', 'peak_quant.cli', 'contrast',
             '--package', str(copied_package), '--query', query_path,
             '--output', str(report_path)],
            cwd=isolated, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(report_path.read_text())
        self.assertEqual(report['result']['minimum']['score'], '-4')
        self.assertEqual(report['result']['maximum']['score'], '-2')


class GapAndOverlapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_middle_analyte_without_candidates(self):
        span = 6
        source = {
            'analytes': [{'id': 'A', 'window': ['0', '2']},
                         {'id': 'B', 'window': ['2', '4']},
                         {'id': 'C', 'window': ['4', '6']}],
            'runs': [
                standard_run('s0', {'A': 0, 'B': 0, 'C': 0},
                             [(0, 2, 0), (2, 4, 0), (4, 6, 0)],
                             span, ['A', 'B', 'C']),
                standard_run('s1', {'A': 1, 'B': 1, 'C': 1},
                             [(0, 2, 1), (2, 4, 1), (4, 6, 1)],
                             span, ['A', 'B', 'C']),
                {'id': 'u1', 'kind': 'unknown',
                 'samples': triangle_samples(
                     [(Fraction(0), Fraction(4), 1),
                      (Fraction(3), Fraction(5), 1)], span)},
            ],
        }
        candidates = {
            'analyte_order': ['A', 'B', 'C'],
            'candidates': [
                {'run_id': 'u1', 'analyte_id': 'A', 'candidate_id': 'a1',
                 'window': ['0', '4'], 'penalty': 0},
                {'run_id': 'u1', 'analyte_id': 'C', 'candidate_id': 'c1',
                 'window': ['3', '5'], 'penalty': 0},
            ],
        }
        package = build_package(
            json.dumps(source).encode(), json.dumps(candidates).encode())
        block = package['runs'][0]
        self.assertEqual(block['best_selected'], 1)
        self.assertEqual(block['best_penalty'], 0)
        slots = {slot['analyte_id']: slot for slot in block['attainable']}
        self.assertEqual(slots['A']['candidate_ids'], ['a1'])
        self.assertTrue(slots['A']['vacancy_available'])
        self.assertEqual(slots['B']['candidate_ids'], [])
        self.assertTrue(slots['B']['vacancy_available'])
        self.assertEqual(slots['C']['candidate_ids'], ['c1'])
        self.assertTrue(slots['C']['vacancy_available'])
        no_joint = answer_query(package, {
            'run_id': 'u1', 'terms': {'A': 1, 'C': 1}, 'pins': {}})
        self.assertEqual(no_joint['result']['status'], 'no_joint_explanation')
        only_a = answer_query(package, {
            'run_id': 'u1', 'terms': {'A': 1}, 'pins': {}})
        self.assertEqual(only_a['result']['status'], 'joint_explanation')


class OffsetCalibrationTests(unittest.TestCase):
    def test_negative_and_extrapolated_concentrations_remain_selectable(self):
        span = 8
        source = {
            'analytes': [{'id': 'X', 'window': ['0', '2']}],
            'runs': [
                standard_run('s0', {'X': 0}, [(0, 2, 1)], span, ['X']),
                standard_run('s2', {'X': 2}, [(0, 2, 5)], span, ['X']),
                {'id': 'u1', 'kind': 'unknown', 'samples': triangle_samples(
                    [(Fraction(2), Fraction(4), 0),
                     (Fraction(4), Fraction(6), 2),
                     (Fraction(6), Fraction(8), 6)], span)},
            ],
        }
        candidates = {
            'analyte_order': ['X'],
            'candidates': [
                {'run_id': 'u1', 'analyte_id': 'X', 'candidate_id': 'x0',
                 'window': ['2', '4'], 'penalty': 0},
                {'run_id': 'u1', 'analyte_id': 'X', 'candidate_id': 'x2',
                 'window': ['4', '6'], 'penalty': 0},
                {'run_id': 'u1', 'analyte_id': 'X', 'candidate_id': 'x6',
                 'window': ['6', '8'], 'penalty': 0},
            ],
        }
        package = build_package(
            json.dumps(source).encode(), json.dumps(candidates).encode())
        cal = package['calibrations'][0]
        self.assertEqual((cal['slope'], cal['intercept']), ('2', '1'))
        by_id = {entry['candidate_id']: entry for entry in package['candidates']}
        self.assertEqual(by_id['x0']['concentration'], '-1/2')
        self.assertTrue(by_id['x0']['negative'])
        self.assertTrue(by_id['x0']['extrapolated'])
        self.assertEqual(by_id['x2']['concentration'], '1/2')
        self.assertFalse(by_id['x2']['negative'])
        self.assertFalse(by_id['x2']['extrapolated'])
        self.assertEqual(by_id['x6']['concentration'], '5/2')
        self.assertTrue(by_id['x6']['extrapolated'])
        report = answer_query(package, {
            'run_id': 'u1', 'terms': {'X': 1}, 'pins': {}})
        result = report['result']
        self.assertEqual(result['minimum']['score'], '-1/2')
        self.assertEqual(result['maximum']['score'], '5/2')
        ids = package['runs'][0]['attainable'][0]['candidate_ids']
        self.assertEqual(ids, ['x0', 'x2', 'x6'])


class AllVacancyAndValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def test_all_vacancy_run_has_unique_empty_explanation(self):
        source = dict(acceptance_source())
        source['runs'] = [run for run in source['runs']
                          if run['kind'] == 'standard']
        source['runs'].append({
            'id': 'empty', 'kind': 'unknown',
            'samples': [['0', '0'], ['6', '0']]})
        package = build_package(json.dumps(source).encode(),
                                json.dumps({'analyte_order': ['A', 'B'],
                                            'candidates': []}).encode())
        block = package['runs'][0]
        self.assertEqual(block['run_id'], 'empty')
        self.assertEqual((block['best_selected'], block['best_penalty']), (0, 0))
        self.assertEqual(block['representative'], [None, None])
        self.assertTrue(all(slot['vacancy_available']
                            and slot['candidate_ids'] == []
                            for slot in block['attainable']))
        report = answer_query(package, {
            'run_id': 'empty', 'terms': {'A': 1}, 'pins': {}})
        self.assertEqual(report['result']['status'], 'no_joint_explanation')

    def test_query_reference_errors(self):
        package = build_package(
            json.dumps(acceptance_source()).encode(),
            json.dumps(acceptance_candidates()).encode())
        with self.assertRaisesRegex(ValueError, 'not an unknown run'):
            parse_query({'run_id': 'ghost', 'terms': {'A': 1}, 'pins': {}}, package)
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            parse_query({'run_id': 'u1', 'terms': {'Z': 1}, 'pins': {}}, package)
        with self.assertRaisesRegex(ValueError, 'not registered'):
            parse_query({'run_id': 'u1', 'terms': {'A': 1},
                         'pins': {'A': 'nope'}}, package)
        # Registered but never optimal is structurally valid.
        query = parse_query(
            {'run_id': 'u1', 'terms': {'B': 1}, 'pins': {'B': 'b2'}},
            build_package(json.dumps(acceptance_source()).encode(),
                          json.dumps(acceptance_candidates(1)).encode()))
        self.assertEqual(query[2]['B'], 'b2')

    def test_candidate_document_rules(self):
        source = acceptance_source()
        good = acceptance_candidates()
        def expect_error(mutate):
            data = json.loads(json.dumps(good))
            mutate(data)
            with self.assertRaises(ValueError):
                build_package(json.dumps(source).encode(),
                              json.dumps(data).encode())
        expect_error(lambda d: d['candidates'][0].__setitem__('penalty', True))
        expect_error(lambda d: d['candidates'][0].__setitem__(
            'run_id', 's0'))
        expect_error(lambda d: d['candidates'][0].__setitem__(
            'window', ['5', '7']))
        expect_error(lambda d: d.__setitem__('analyte_order', ['A', 'A']))
        expect_error(lambda d: d.__setitem__('extra', 1))
        expect_error(lambda d: d['candidates'][1].__setitem__(
            'candidate_id', 'a1'))

    def test_candidate_count_and_pair_limits(self):
        source = acceptance_source()
        base = {'run_id': 'u1', 'analyte_id': 'A', 'window': ['0', '2'],
                'penalty': 0}
        twelve = {'analyte_order': ['A', 'B'], 'candidates': [
            dict(base, candidate_id=f'c{i}') for i in range(12)]}
        package = build_package(json.dumps(source).encode(),
                                json.dumps(twelve).encode())
        self.assertEqual(len(package['candidates']), 12)
        thirteen = json.loads(json.dumps(twelve))
        thirteen['candidates'].append(dict(base, candidate_id='c12'))
        with self.assertRaisesRegex(ValueError, 'at most 12'):
            build_package(json.dumps(source).encode(),
                          json.dumps(thirteen).encode())
        # The 30001st record trips the document cap; stay at <=12 per pair by
        # spreading records over both analytes (ids stay unique per pair).
        many = {'analyte_order': ['A', 'B'], 'candidates': [
            dict(base, candidate_id=f'c{i:05d}',
                 analyte_id='A' if i % 2 == 0 else 'B')
            for i in range(30001)]}
        with self.assertRaisesRegex(ValueError, '30000'):
            build_package(json.dumps(source).encode(),
                          json.dumps(many).encode())

    def test_duplicate_json_keys_rejected_and_no_target_created(self):
        source = write_json(self.directory, 'input.json', acceptance_source())
        dup = self.directory / 'dup.json'
        dup.write_text('{"analyte_order":["A","B"],"analyte_order":["A","B"],'
                       '"candidates":[]}', encoding='utf-8')
        target = str(self.directory / 'out.json')
        code, _, error = invoke(['explain', '--source', source,
                                 '--candidates', str(dup), '--output', target])
        self.assertEqual(code, 2)
        self.assertIn('duplicate JSON key', error)
        self.assertFalse(Path(target).exists())

    def test_failed_standard_fit_aborts_the_whole_package(self):
        source = acceptance_source()
        for run in source['runs']:
            if run['kind'] == 'standard':
                run['concentrations'] = {'A': '1', 'B': '1'}
        with self.assertRaisesRegex(ValueError, 'must not all be equal'):
            build_package(json.dumps(source).encode(),
                          json.dumps(acceptance_candidates()).encode())

    def test_corrupt_package_is_refused(self):
        package = build_package(
            json.dumps(acceptance_source()).encode(),
            json.dumps(acceptance_candidates()).encode())
        validate_package(json.loads(json.dumps(package)))
        tampered = json.loads(json.dumps(package))
        tampered['package_version'] = 99
        with self.assertRaisesRegex(ValueError, 'unsupported package version'):
            validate_package(tampered)
        tampered = json.loads(json.dumps(package))
        tampered['candidates'][0]['concentration'] = '999'
        with self.assertRaisesRegex(ValueError, 'concentration does not match'):
            validate_package(tampered)
        tampered = json.loads(json.dumps(package))
        tampered['runs'][0]['best_selected'] = 1
        with self.assertRaisesRegex(ValueError, 'best indicators'):
            validate_package(tampered)
        tampered = json.loads(json.dumps(package))
        tampered['calibrations'][0]['slope'] = '1/0'
        with self.assertRaises(ValueError):
            validate_package(tampered)
        tampered = json.loads(json.dumps(package))
        tampered['calibrations'][0]['slope'] = '0'
        with self.assertRaisesRegex(ValueError, 'corrupt calibration bounds'):
            validate_package(tampered)
        tampered = json.loads(json.dumps(package))
        base = dict(tampered['candidates'][0])
        # Reusing a registered id is rejected.
        tampered['candidates'].append(dict(base, candidate_id='a1'))
        with self.assertRaisesRegex(ValueError, 'candidate id within run'):
            validate_package(json.loads(json.dumps(tampered)))
        # A fresh package grown to 13 entries per pair trips the pair cap.
        oversized = json.loads(json.dumps(package))
        for index in range(11):  # the pair already holds a1/a2
            oversized['candidates'].append(
                dict(base, candidate_id=f'zz{index:02d}'))
        with self.assertRaisesRegex(ValueError, 'at most 12'):
            validate_package(oversized)


class ScaleCaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_thirty_analytes_two_to_the_thirty_explanations(self):
        n = 30
        span = 4 * n
        ids = [f'A{i}' for i in range(n)]
        standards = [
            standard_run('s0', {analyte: 0 for analyte in ids},
                         [(4 * i, 4 * i + 2, 0) for i in range(n)], span, ids),
            standard_run('s1', {analyte: 1 for analyte in ids},
                         [(4 * i, 4 * i + 2, 1) for i in range(n)], span, ids),
        ]
        triangles = []
        for i in range(n):
            base = Fraction(4 * i)
            triangles.append((base, base + 2, 1))
            triangles.append((base + 2, base + 4, 2))
        unknown = {'id': 'u1', 'kind': 'unknown',
                   'samples': triangle_samples(triangles, span)}
        source = {
            'analytes': [{'id': f'A{i}',
                          'window': [str(4 * i), str(4 * i + 2)]}
                         for i in range(n)],
            'runs': standards + [unknown],
        }
        candidates = []
        for i in range(n):
            base = 4 * i
            candidates.extend([
                {'run_id': 'u1', 'analyte_id': f'A{i}', 'candidate_id': f'{i}a',
                 'window': [str(base), str(base + 2)], 'penalty': 0},
                {'run_id': 'u1', 'analyte_id': f'A{i}', 'candidate_id': f'{i}b',
                 'window': [str(base + 2), str(base + 4)], 'penalty': 0},
            ])
        package = build_package(
            json.dumps(source).encode(),
            json.dumps({'analyte_order': [f'A{i}' for i in range(n)],
                        'candidates': candidates}).encode())
        block = package['runs'][0]
        self.assertEqual(block['best_selected'], 30)
        self.assertEqual(block['best_penalty'], 0)
        self.assertTrue(all(len(slot['candidate_ids']) == 2
                            and not slot['vacancy_available']
                            for slot in block['attainable']))
        report = answer_query(package, {
            'run_id': 'u1',
            'terms': {f'A{i}': 1 for i in range(n)},
            'pins': {}})
        result = report['result']
        self.assertEqual(result['minimum']['score'], '30')
        self.assertEqual(result['maximum']['score'], '60')
        self.assertEqual(sum(1 for e in result['minimum']['witness']
                             if e['selected']), 30)
        self.assertEqual(sum(1 for e in result['maximum']['witness']
                             if e['selected']), 30)
        self.assertTrue(all(e['candidate_id'].endswith('a')
                            for e in result['minimum']['witness']))
        self.assertTrue(all(e['candidate_id'].endswith('b')
                            for e in result['maximum']['witness']))


class BruteForceOracleTests(unittest.TestCase):
    """Cross-check the DP against an independent enumerator on small cases."""

    def make_options(self, spec):
        options = []
        for group in spec:
            options.append([{
                'id': cid, 'lo': Fraction(lo), 'hi': Fraction(hi),
                'penalty': penalty, 'concentration': Fraction(conc)}
                for cid, lo, hi, penalty, conc in group])
        return options

    def cases(self):
        return [
            # Empty: unique all-vacancy explanation.
            [[], [], []],
            # Single analyte, two candidates with different penalties.
            [[('p', '0', '1', 2, '3'), ('q', '1', '2', 0, '5')]],
            # Overlapping windows: the two picks cannot coexist.
            [[('a', '0', '3', 0, '1')],
             [('b', '2', '5', 0, '2')],
             [('c', '5', '7', 0, '4')]],
            # Endpoint touching is allowed (end == start).
            [[('a', '0', '2', 1, '1'), ('a2', '2', '4', 0, '9')],
             [('b', '2', '4', 0, '2'), ('b2', '4', '6', 3, '8')]],
            # Penalty trade-off: a cheaper smaller explanation must not win.
            [[('a', '0', '1', 0, '1')],
             [('b', '2', '3', 5, '2')],
             [('c', '4', '5', 0, '3')]],
            # Nested windows and a vacancy gap.
            [[('a1', '0', '8', 0, '1'), ('a2', '0', '2', 0, '7')],
             [],
             [('c1', '3', '4', 1, '2'), ('c2', '6', '7', 0, '3')]],
        ]

    def test_metrics_attainable_representative_and_extrema(self):
        rng_fractions = [Fraction(1), Fraction(-2), Fraction(3, 2), Fraction(-1, 3)]
        for spec in self.cases():
            options = self.make_options(spec)
            best_witnesses, metrics = brute_force(options)
            self.assertEqual(best_metrics(options), metrics)
            expected_ids = {tuple((0, e['id']) if e else (1, '')
                                  for e in witness) for witness in best_witnesses}
            witness = representative(options)
            witness_key = tuple((0, e['id']) if e else (1, '') for e in witness)
            self.assertIn(witness_key, expected_ids)
            self.assertEqual(witness_key, min(expected_ids))
            self.assertEqual(attainable(options), brute_attainable(options))

            n = len(options)
            # Several pin/term configurations, all checked by enumeration.
            pin_choices = [{}]
            for i, group in enumerate(options):
                for option in group[:1]:
                    pin_choices.append({i: option['id']})
                pin_choices.append({i: None})
            for pins in pin_choices:
                term_positions = set(range(n))
                if not term_positions:
                    continue
                coefficients = {i: rng_fractions[i % len(rng_fractions)]
                                for i in term_positions}
                expected = brute_extrema(options, pins, term_positions,
                                         coefficients)
                actual = constrained_extrema(
                    options, dict(pins), term_positions,
                    lambda position, candidate:
                    coefficients[position] * candidate['concentration'])
                if expected is None:
                    self.assertIsNone(actual)
                    continue
                (min_score, min_witness), (max_score, max_witness) = expected
                (actual_min, actual_max) = actual
                self.assertEqual(actual_min[0], min_score)
                self.assertEqual(actual_max[0], max_score)
                self.assertEqual([e['id'] if e else None for e in actual_min[1]],
                                 [e['id'] if e else None for e in min_witness])
                self.assertEqual([e['id'] if e else None for e in actual_max[1]],
                                 [e['id'] if e else None for e in max_witness])

    def test_term_subset_and_unknown_but_registered_pins(self):
        spec = [
            [('a1', '0', '2', 0, '1'), ('a2', '3', '5', 0, '4')],
            [('b1', '2', '4', 0, '2')],
        ]
        options = self.make_options(spec)
        pins = {1: 'b1'}
        term_positions = {0}
        coefficients = {0: Fraction(1)}
        expected = brute_extrema(options, pins, term_positions, coefficients)
        actual = constrained_extrema(
            options, pins, term_positions,
            lambda position, candidate:
            coefficients[position] * candidate['concentration'])
        self.assertIsNotNone(actual)
        self.assertEqual(actual[0][0], expected[0][0])
        self.assertEqual(actual[1][0], expected[1][0])

    def test_randomized_instances_against_the_enumerator(self):
        rng = random.Random(20261122)
        for _ in range(300):
            n = rng.randint(1, 5)
            options = []
            for i in range(n):
                group = []
                for j in range(rng.randint(0, 4)):
                    lo = Fraction(rng.randint(0, 6))
                    hi = lo + Fraction(rng.randint(1, 3))
                    group.append({
                        'id': f'c{i}_{j}', 'lo': lo, 'hi': hi,
                        'penalty': rng.randint(0, 3),
                        'concentration': Fraction(rng.randint(-3, 4),
                                                  rng.randint(1, 3))})
                options.append(group)
            best_witnesses, metrics = brute_force(options)
            self.assertEqual(best_metrics(options), metrics)
            self.assertEqual(attainable(options), brute_attainable(options))
            witness = representative(options)
            witness_key = tuple((0, e['id']) if e else (1, '') for e in witness)
            all_keys = {tuple((0, e['id']) if e else (1, '') for e in w)
                        for w in best_witnesses}
            self.assertEqual(witness_key, min(all_keys))
            pins = {}
            for i, group in enumerate(options):
                roll = rng.random()
                if roll < 0.25:
                    pins[i] = None
                elif roll < 0.5 and group:
                    pins[i] = rng.choice(group)['id']
            term_positions = {i for i in range(n) if rng.random() < 0.7}
            coefficients = {i: Fraction(rng.randint(-4, 4) or 1)
                            for i in term_positions}
            expected = brute_extrema(options, pins, term_positions, coefficients)
            actual = constrained_extrema(
                options, dict(pins), term_positions,
                lambda position, candidate:
                coefficients[position] * candidate['concentration'])
            if expected is None:
                self.assertIsNone(actual)
                continue
            (min_score, min_witness), (max_score, max_witness) = expected
            self.assertEqual(actual[0][0], min_score)
            self.assertEqual(actual[1][0], max_score)
            self.assertEqual(
                [e['id'] if e else None for e in actual[0][1]],
                [e['id'] if e else None for e in min_witness])
            self.assertEqual(
                [e['id'] if e else None for e in actual[1][1]],
                [e['id'] if e else None for e in max_witness])


class PackageLayoutTests(unittest.TestCase):
    def test_package_contains_every_contract_field(self):
        package = build_package(
            json.dumps(acceptance_source()).encode(),
            json.dumps(acceptance_candidates()).encode())
        self.assertEqual(set(package), {
            'analyte_order', 'calibrations', 'candidates', 'package_version',
            'runs', 'source_sha256'})
        calibration = package['calibrations'][0]
        self.assertEqual(set(calibration), {
            'analyte_id', 'intercept', 'max_concentration', 'min_concentration',
            'slope', 'standard_run_ids'})
        entry = package['candidates'][0]
        self.assertEqual(set(entry), {
            'analyte_id', 'area', 'candidate_id', 'concentration',
            'extrapolated', 'negative', 'penalty', 'run_id', 'window'})
        block = package['runs'][0]
        self.assertEqual(set(block), {
            'attainable', 'best_penalty', 'best_selected', 'representative',
            'run_id', 'sample_range'})
        # Re-encoding stays byte stable and reloads cleanly.
        encoded = encode_package(package)
        self.assertEqual(encoded, encode_package(json.loads(encoded)))

    def test_fractional_decimal_times_round_trip(self):
        # Half-integer sample times and fractional areas must survive build and
        # the offline integrity check without re-formatting into fractions.
        source = {
            'analytes': [{'id': 'A', 'window': ['0.5', '1.5']}],
            'runs': [
                {'id': 's0', 'kind': 'standard',
                 'concentrations': {'A': '0'},
                 'samples': [['0.5', '0'], ['1', '0'], ['1.5', '0']]},
                {'id': 's1', 'kind': 'standard',
                 'concentrations': {'A': '1'},
                 'samples': [['0.5', '0'], ['1', '1'], ['1.5', '0']]},
                {'id': 'u1', 'kind': 'unknown',
                 'samples': [['0.5', '0'], ['1', dec(Fraction(3, 2))],
                             ['1.5', '0']]},
            ],
        }
        candidates = {
            'analyte_order': ['A'],
            'candidates': [
                {'run_id': 'u1', 'analyte_id': 'A', 'candidate_id': 'a',
                 'window': ['0.5', '1.5'], 'penalty': 0},
            ],
        }
        package = build_package(json.dumps(source).encode(),
                                json.dumps(candidates).encode())
        self.assertEqual(package['runs'][0]['sample_range'], ['0.5', '1.5'])
        # Triangle base 1, height 3/2 -> area 3/4.  Standards give slope 1/2
        # (area 1/2 at concentration 1), so concentration = (3/4)/(1/2) = 3/2.
        self.assertEqual(package['candidates'][0]['area'], '3/4')
        self.assertEqual(package['candidates'][0]['concentration'], '3/2')
        validate_package(json.loads(json.dumps(package)))
        report = answer_query(json.loads(json.dumps(package)), {
            'run_id': 'u1', 'terms': {'A': 1}, 'pins': {}})
        self.assertEqual(report['result']['minimum']['score'], '3/2')


if __name__ == '__main__':
    unittest.main()
