"""Acceptance and contract tests for ``peak-quant benchmark``.

The cases mirror docs/benchmark-contract.md: origin/signal transitive group
isolation (including collinear-point and decimal-spelling invariance),
per-analyte unavailability reasons in the mandated order, per-fold blind
scoring with the v1 fit rules, byte-deterministic reports and refusal to
overwrite an existing target or symlink.  An independent reference rebuilds
each available fold with the existing v1 ``process`` pipeline and compares
predictions and training ranges.  A separate subprocess driver runs the
200-run / 30-analyte synthetic case under an RLIMIT_CPU cap.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
from pathlib import Path

from peak_quant import cli
from peak_quant.benchmark import (REASON_EQUAL_CONCENTRATIONS,
                                  REASON_NONPOSITIVE_SLOPE, REASON_TOO_FEW,
                                  _reduced_curve, benchmark)
from peak_quant.service import process

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------- fixture builders

def triangle(height):
    """Triangular v1 curve on [0, 2] with apex height at t=1 (area == height)."""
    return [['0', '0'], ['1', str(height)], ['2', '0']]


def write_json(directory, name, payload, raw=None):
    path = Path(directory) / name
    if raw is None:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    else:
        path.write_bytes(raw)
    return path


def worked_source(v_samples):
    """S0/S1/S2/U/V/W from the contract worked example, one analyte A."""
    return {
        'analytes': [{'id': 'A', 'window': ['0', '2']}],
        'runs': [
            {'id': 'S0', 'kind': 'standard', 'concentrations': {'A': '0'},
             'samples': triangle(0)},
            {'id': 'S1', 'kind': 'standard', 'concentrations': {'A': '2'},
             'samples': triangle(4)},
            {'id': 'S2', 'kind': 'standard', 'concentrations': {'A': '4'},
             'samples': triangle(8)},
            {'id': 'U', 'kind': 'standard', 'concentrations': {'A': '1'},
             'samples': triangle(2)},
            {'id': 'V', 'kind': 'standard', 'concentrations': {'A': '1'},
             'samples': v_samples},
            {'id': 'W', 'kind': 'standard', 'concentrations': {'A': '3'},
             'samples': triangle(12)},
            {'id': 'UNK', 'kind': 'unknown', 'samples': triangle(6)},
        ],
    }


WORKED_PLAN = {
    'origins': {'S0': 'o0', 'S1': 'o1', 'S2': 'o2',
                'U': 'ou', 'V': 'ov', 'W': 'ov'},
    'score_ids': ['U', 'W'],
}


def invoke_cli(directory, source, plan, output_name='report.json',
               source_raw=None, plan_raw=None):
    source_path = (write_json(directory, 'source.json', None, raw=source_raw)
                   if source_raw is not None
                   else write_json(directory, 'source.json', source))
    plan_path = (write_json(directory, 'plan.json', None, raw=plan_raw)
                 if plan_raw is not None
                 else write_json(directory, 'plan.json', plan))
    output_path = Path(directory) / output_name

    class Out:
        buffer = io.BytesIO()

        @staticmethod
        def flush():
            return None

    with redirect_stdout(Out()), redirect_stderr(io.StringIO()) as err:
        code = cli.main(['benchmark', '--source', str(source_path),
                         '--plan', str(plan_path), '--output', str(output_path)])
    payload = json.loads(output_path.read_text(encoding='utf-8')) \
        if output_path.exists() else None
    return code, err.getvalue(), payload, output_path


class CliHarnessTests(unittest.TestCase):
    """CLI-level tests use the worked example as their default fixture."""

    def setUp(self):
        self.source = worked_source(
            [['0', '0'], ['0.5', '1'], ['1', '2'], ['2', '0']])
        self.plan = WORKED_PLAN


# ------------------------------------------------------------- curve identity

class CurveIdentityTests(unittest.TestCase):
    def test_collinear_points_and_decimal_spellings_normalize_away(self):
        base = [('0', '0'), ('1', '2'), ('2', '0')]
        with_extra = [('0.0', '0'), ('0.50', '1.0'), ('1', '2'), ('2.0', '0')]
        self.assertEqual(_reduced_curve(base), _reduced_curve(with_extra))

    def test_different_piecewise_shape_is_not_identical(self):
        u = [('0', '0'), ('1', '2'), ('2', '0')]
        v = [('0', '0'), ('0.5', '2'), ('2', '0')]
        self.assertNotEqual(_reduced_curve(u), _reduced_curve(v))

    def test_domain_is_part_of_identity(self):
        left = [('0', '0'), ('1', '2'), ('2', '0')]
        shifted = [('0.5', '0'), ('1.5', '2'), ('2.5', '0')]
        self.assertNotEqual(_reduced_curve(left), _reduced_curve(shifted))


# ------------------------------------------------------------- worked examples

class WorkedExampleTests(CliHarnessTests):
    def test_identical_curves_chain_unscored_standard_into_held_group(self):
        with tempfile.TemporaryDirectory() as directory:
            plan_path = write_json(
                directory, 'plan.json', WORKED_PLAN)
            report = benchmark(
                worked_source([['0', '0'], ['0.5', '1'], ['1', '2'],
                               ['2', '0']]), plan_path)
        self.assertEqual(len(report['folds']), 1)
        fold = report['folds'][0]
        self.assertEqual(fold['held_out_run_ids'], ['U', 'V', 'W'])
        self.assertEqual(fold['training_run_ids'], ['S0', 'S1', 'S2'])
        self.assertEqual(fold['scored_run_ids'], ['U', 'W'])
        analyte = fold['analytes'][0]
        self.assertTrue(analyte['available'])
        self.assertEqual((analyte['slope'], analyte['intercept']), ('2', '0'))
        self.assertEqual((analyte['min_concentration'],
                          analyte['max_concentration']), ('0', '4'))
        rows = {row['run_id']: row for row in analyte['scores']}
        self.assertEqual(rows['U']['prediction'], '1')
        self.assertEqual(rows['U']['error'], '0')
        self.assertFalse(rows['U']['extrapolated'])
        self.assertEqual(rows['W']['prediction'], '6')
        self.assertEqual(rows['W']['error'], '3')
        self.assertTrue(rows['W']['extrapolated'])
        summary = report['summaries'][0]
        self.assertEqual(summary['requested_groups'], 1)
        self.assertEqual(summary['requested_records'], 2)
        self.assertEqual(summary['successful_groups'], 1)
        self.assertEqual(summary['successful_records'], 2)
        self.assertEqual(summary['group_mean_absolute_errors'],
                         [{'fold_id': 'F001', 'mean_absolute_error': '3/2'}])
        self.assertEqual(summary['total_mean_absolute_error'], '3/2')

    def test_reshaped_curve_splits_the_groups_and_scores_21_over_13(self):
        with tempfile.TemporaryDirectory() as directory:
            plan_path = write_json(directory, 'plan.json', WORKED_PLAN)
            report = benchmark(
                worked_source([['0', '0'], ['0.5', '2'], ['2', '0']]), plan_path)
        self.assertEqual([f['held_out_run_ids'] for f in report['folds']],
                         [['U'], ['V', 'W']])
        first, second = report['folds']
        self.assertEqual(first['training_run_ids'],
                         ['S0', 'S1', 'S2', 'V', 'W'])
        a = first['analytes'][0]
        self.assertEqual(a['slope'], '13/5')
        self.assertEqual(a['intercept'], '0')
        self.assertEqual(a['scores'][0]['prediction'], '10/13')
        self.assertEqual(a['scores'][0]['error'], '-3/13')
        b = second['analytes'][0]
        self.assertEqual(b['slope'], '2')
        self.assertEqual(b['scores'][0]['run_id'], 'W')
        self.assertEqual(b['scores'][0]['prediction'], '6')
        summary = report['summaries'][0]
        self.assertEqual(
            [g['mean_absolute_error'] for g in summary['group_mean_absolute_errors']],
            ['3/13', '3'])
        self.assertEqual(summary['total_mean_absolute_error'], '21/13')

    def test_window_only_match_and_equal_area_do_not_join_groups(self):
        # V keeps area 2 and the same window [0, 2] but a different shape, so
        # it must stay on the training side of U's fold.
        with tempfile.TemporaryDirectory() as directory:
            plan_path = write_json(directory, 'plan.json', WORKED_PLAN)
            report = benchmark(
                worked_source([['0', '0'], ['0.5', '2'], ['2', '0']]), plan_path)
        self.assertEqual(report['folds'][0]['held_out_run_ids'], ['U'])

    def test_reference_concentrations_never_define_identity(self):
        # U and V share the exact curve but the plan gives them different
        # origins; the signal edge still merges them.  Different concentrations
        # on identical curves do not split the component.
        source = worked_source(triangle(2))
        plan = {'origins': {'S0': 'o0', 'S1': 'o1', 'S2': 'o2',
                            'U': 'ou', 'V': 'ov', 'W': 'ow'},
                'score_ids': ['U', 'W']}
        with tempfile.TemporaryDirectory() as directory:
            report = benchmark(source, write_json(directory, 'p.json', plan))
        held = {tuple(f['held_out_run_ids']) for f in report['folds']}
        self.assertIn(('U', 'V'), held)

    def test_replicate_standards_on_training_side_keep_separate_weights(self):
        # Two distinct-curve standards at concentration 2 both stay in the
        # training set and count independently in the least-squares fit.
        source = {
            'analytes': [{'id': 'A', 'window': ['0', '2']}],
            'runs': [
                {'id': 'S0', 'kind': 'standard', 'concentrations': {'A': '0'},
                 'samples': triangle(0)},
                {'id': 'S1a', 'kind': 'standard', 'concentrations': {'A': '2'},
                 'samples': triangle(4)},
                {'id': 'S1b', 'kind': 'standard', 'concentrations': {'A': '2'},
                 'samples': [['0', '0'], ['0.4', '5'], ['2', '0']]},
                {'id': 'S2', 'kind': 'standard', 'concentrations': {'A': '4'},
                 'samples': triangle(8)},
                {'id': 'U', 'kind': 'standard', 'concentrations': {'A': '1'},
                 'samples': triangle(2)},
            ],
        }
        plan = {'origins': {run['id']: f'o-{run["id"]}' for run in source['runs']},
                'score_ids': ['U']}
        with tempfile.TemporaryDirectory() as directory:
            report = benchmark(source, write_json(directory, 'p.json', plan))
        fold = report['folds'][0]
        self.assertEqual(fold['held_out_run_ids'], ['U'])
        self.assertEqual(fold['training_run_ids'], ['S0', 'S1a', 'S1b', 'S2'])
        # Equal-weight LS with points (0,0),(2,4),(2,5),(4,8):
        # slope = (4*34-8*17)/(4*24-64) = 32/32 ... compute via Fraction check.
        pts = [(Fraction(0), Fraction(0)), (Fraction(2), Fraction(4)),
               (Fraction(2), Fraction(5)), (Fraction(4), Fraction(8))]
        n = 4
        sx = sum(x for x, _ in pts)
        sy = sum(y for _, y in pts)
        slope = (n * sum(x * y for x, y in pts) - sx * sy) / \
                (n * sum(x * x for x, _ in pts) - sx * sx)
        self.assertEqual(fold['analytes'][0]['slope'], str(slope))

    def test_unknown_runs_validate_but_never_join_groups_or_fits(self):
        source = worked_source(triangle(2))
        # The unknown run reuses U's exact curve; it must not pull U anywhere.
        source['runs'][-1]['samples'] = triangle(2)
        with tempfile.TemporaryDirectory() as directory:
            report = benchmark(source, write_json(directory, 'p.json', WORKED_PLAN))
        for fold in report['folds']:
            self.assertNotIn('UNK', fold['held_out_run_ids'])
            self.assertNotIn('UNK', fold['training_run_ids'])


# ------------------------------------------------- multi-analyte unavailability

def multi_source():
    """Two analytes A/B, five standards, two groups G1 (R1,R2) and G2 (R3..R5).

    Each run carries two disjoint triangular peaks: analyte A integrates the
    first peak over [0, 1] (apex at 0.5), analyte B the second over [1, 2]
    (apex at 1.5).  A peak's area equals half its apex height, so heights are
    chosen to give the intended per-analyte areas while every curve stays
    distinct across runs.
    """
    specs = [
        # run id, (area A, area B)
        ('R1', 2, 10),
        ('R2', 4, 20),
        ('R3', 1, 7),
        ('R4', 3, 14),
        ('R5', 5, 21),
    ]
    # Analyte A references deliberately differ from response areas so blind
    # predictions carry genuine, non-zero and sometimes extrapolated errors.
    concentrations_a = {'R1': '2', 'R2': '4', 'R3': '0',
                        'R4': '1', 'R5': '3'}
    runs = []
    for run_id, area_a, area_b in specs:
        height_a, height_b = area_a * 2, area_b * 2
        runs.append({
            'id': run_id, 'kind': 'standard',
            'concentrations': {'A': concentrations_a[run_id],
                               'B': str(area_b)},
            'samples': [['0', '0'], ['0.5', str(height_a)], ['1', '0'],
                        ['1.5', str(height_b)], ['2', '0']],
        })
    runs.append({'id': 'K', 'kind': 'unknown',
                 'samples': [['0', '0'], ['0.5', '18'], ['1', '0'],
                             ['1.5', '18'], ['2', '0']]})
    return {
        'analytes': [{'id': 'A', 'window': ['0', '1']},
                     {'id': 'B', 'window': ['1', '2']}],
        'runs': runs,
    }


MULTI_PLAN = {
    'origins': {'R1': 'g1', 'R2': 'g1', 'R3': 'g2', 'R4': 'g2', 'R5': 'g2'},
    'score_ids': ['R1', 'R3', 'R4'],
}


class MultiAnalyteTests(unittest.TestCase):
    def test_equal_concentrations_and_positive_slope_coexist_in_one_fold(self):
        source = multi_source()
        # Analyte A: force identical concentrations inside the held G1 fold's
        # *remaining* training set by making R3/R4/R5 concentrations equal.
        for run_id in ('R3', 'R4', 'R5'):
            run = next(r for r in source['runs'] if r['id'] == run_id)
            run['concentrations']['A'] = '7'
        with tempfile.TemporaryDirectory() as directory:
            report = benchmark(source, write_json(directory, 'p.json', MULTI_PLAN))
        self.assertEqual(len(report['folds']), 2)
        g1 = next(f for f in report['folds'] if f['held_out_run_ids'] == ['R1', 'R2'])
        by_id = {a['analyte_id']: a for a in g1['analytes']}
        self.assertFalse(by_id['A']['available'])
        self.assertEqual(by_id['A']['unavailable_reason'],
                         REASON_EQUAL_CONCENTRATIONS)
        self.assertTrue(by_id['B']['available'])
        # B still scores every scored record of the fold (only R1 here).
        row = by_id['B']['scores'][0]
        self.assertEqual(row['run_id'], 'R1')
        self.assertIsNotNone(row['prediction'])
        # Unavailable rows carry area/reference but null prediction fields.
        a_row = by_id['A']['scores'][0]
        self.assertIsNone(a_row['prediction'])
        self.assertIsNone(a_row['error'])
        self.assertIsNone(a_row['negative'])
        self.assertIsNone(a_row['extrapolated'])
        self.assertIsNotNone(a_row['area'])
        self.assertIsNotNone(a_row['reference_concentration'])
        summary_a = next(s for s in report['summaries'] if s['analyte_id'] == 'A')
        summary_b = next(s for s in report['summaries'] if s['analyte_id'] == 'B')
        # The unavailable fold is null, not a zero-error group.
        g1_mae = next(g for g in summary_a['group_mean_absolute_errors']
                      if g['fold_id'] == g1['fold_id'])
        self.assertIsNone(g1_mae['mean_absolute_error'])
        self.assertEqual(summary_a['successful_groups'], 1)
        self.assertIsNotNone(summary_a['total_mean_absolute_error'])
        self.assertEqual(summary_b['successful_groups'], 2)

    def test_too_few_and_nonpositive_slope_reasons_in_order(self):
        # H is alone in its origin group and has a unique curve, so its fold
        # leaves exactly three training standards.  Analyte A fits fine;
        # analyte B has a descending response and fails the slope gate.
        source = {
            'analytes': [{'id': 'A', 'window': ['0', '2']},
                         {'id': 'B', 'window': ['0', '2']}],
            'runs': [
                {'id': 'T0', 'kind': 'standard',
                 'concentrations': {'A': '0', 'B': '4'}, 'samples': triangle(0)},
                {'id': 'T1', 'kind': 'standard',
                 'concentrations': {'A': '1', 'B': '3'}, 'samples': triangle(2)},
                {'id': 'T2', 'kind': 'standard',
                 'concentrations': {'A': '2', 'B': '2'}, 'samples': triangle(4)},
                {'id': 'H', 'kind': 'standard',
                 'concentrations': {'A': '3', 'B': '1'}, 'samples': triangle(6)},
            ],
        }
        plan = {'origins': {'T0': 'g0', 'T1': 'g0', 'T2': 'g0', 'H': 'g1'},
                'score_ids': ['H']}
        with tempfile.TemporaryDirectory() as directory:
            report = benchmark(source, write_json(directory, 'p.json', plan))
        fold = report['folds'][0]
        reasons = {a['analyte_id']: a['unavailable_reason']
                   for a in fold['analytes']}
        self.assertTrue(next(a for a in fold['analytes']
                             if a['analyte_id'] == 'A')['available'])
        self.assertEqual(reasons['B'], REASON_NONPOSITIVE_SLOPE)
        summary = {s['analyte_id']: s for s in report['summaries']}
        self.assertEqual(summary['A']['successful_groups'], 1)
        self.assertIsNotNone(summary['A']['total_mean_absolute_error'])
        self.assertEqual(summary['B']['successful_groups'], 0)
        self.assertIsNone(summary['B']['total_mean_absolute_error'])

    def test_too_few_standards_reason_is_reported(self):
        # Two standards total; each scored fold leaves one training standard.
        source = {
            'analytes': [{'id': 'A', 'window': ['0', '2']}],
            'runs': [
                {'id': 'T0', 'kind': 'standard',
                 'concentrations': {'A': '0'}, 'samples': triangle(0)},
                {'id': 'H', 'kind': 'standard',
                 'concentrations': {'A': '3'}, 'samples': triangle(6)},
            ],
        }
        plan = {'origins': {'T0': 'g0', 'H': 'g1'},
                'score_ids': ['H']}
        with tempfile.TemporaryDirectory() as directory:
            report = benchmark(source, write_json(directory, 'p.json', plan))
        analyte = report['folds'][0]['analytes'][0]
        self.assertFalse(analyte['available'])
        self.assertEqual(analyte['unavailable_reason'], REASON_TOO_FEW)
        self.assertEqual(report['summaries'][0]['total_mean_absolute_error'],
                         None)

    def test_group_sizes_errors_and_extrapolation_are_not_flattened(self):
        with tempfile.TemporaryDirectory() as directory:
            report = benchmark(multi_source(),
                               write_json(directory, 'p.json', MULTI_PLAN))
        g1, g2 = report['folds']
        self.assertEqual(g1['scored_run_ids'], ['R1'])
        self.assertEqual(g2['scored_run_ids'], ['R3', 'R4'])
        analyte = g2['analytes'][0]
        rows = {row['run_id']: row for row in analyte['scores']}
        # G2 training set is R1(c=2),R2(c=4); R3 predicts 1 against
        # reference 0 (below the training minimum, extrapolated), R4 predicts 3
        # against reference 1 (in range, non-zero error).  Both are counted.
        self.assertEqual((analyte['min_concentration'],
                          analyte['max_concentration']), ('2', '4'))
        self.assertTrue(rows['R3']['extrapolated'])
        self.assertEqual(rows['R3']['error'], '1')
        self.assertFalse(rows['R4']['extrapolated'])
        self.assertEqual(rows['R4']['error'], '2')
        summary = report['summaries'][0]
        # Two groups of different sizes; total MAE weights each group once.
        g1_mae = Fraction(next(g for g in summary['group_mean_absolute_errors']
                               if g['fold_id'] == g1['fold_id'])
                          ['mean_absolute_error'])
        g2_mae = Fraction(next(g for g in summary['group_mean_absolute_errors']
                               if g['fold_id'] == g2['fold_id'])
                          ['mean_absolute_error'])
        self.assertEqual(Fraction(summary['total_mean_absolute_error']),
                         (g1_mae + g2_mae) / 2)

    def test_negative_prediction_is_reported_not_dropped(self):
        source = {
            'analytes': [{'id': 'A', 'window': ['0', '2']}],
            'runs': [
                {'id': 'P0', 'kind': 'standard', 'concentrations': {'A': '0'},
                 'samples': triangle(10)},
                {'id': 'P1', 'kind': 'standard', 'concentrations': {'A': '10'},
                 'samples': triangle(20)},
                {'id': 'H', 'kind': 'standard', 'concentrations': {'A': '5'},
                 'samples': triangle(0)},
            ],
        }
        plan = {'origins': {'P0': 'g0', 'P1': 'g0', 'H': 'g1'},
                'score_ids': ['H']}
        with tempfile.TemporaryDirectory() as directory:
            report = benchmark(source, write_json(directory, 'p.json', plan))
        row = report['folds'][0]['analytes'][0]['scores'][0]
        self.assertEqual(row['prediction'], '-10')
        self.assertTrue(row['negative'])
        self.assertTrue(row['extrapolated'])
        self.assertEqual(row['error'], '-15')
        self.assertEqual(
            report['summaries'][0]['group_mean_absolute_errors'][0]
            ['mean_absolute_error'], '15')


# --------------------------------------------- independent v1 reference check

class IndependentReferenceTests(unittest.TestCase):
    def test_fold_matches_v1_pipeline_with_held_standards_as_unknowns(self):
        source = multi_source()
        with tempfile.TemporaryDirectory() as directory:
            report = benchmark(source, write_json(directory, 'p.json', MULTI_PLAN))
        standard_ids = [run['id'] for run in source['runs']
                        if run['kind'] == 'standard']
        for fold in report['folds']:
            held = set(fold['held_out_run_ids'])
            # Hand-built membership list: held standards become unknowns, the
            # rest remain standards; nothing else changes.
            rebuilt = {'analytes': source['analytes'], 'runs': []}
            for run in source['runs']:
                if run['id'] not in standard_ids:
                    rebuilt['runs'].append(dict(run))
                elif run['id'] in held:
                    rebuilt['runs'].append(
                        {'id': run['id'], 'kind': 'unknown',
                         'samples': run['samples']})
                else:
                    rebuilt['runs'].append(dict(run))
            v1 = process(rebuilt)
            for analyte in fold['analytes']:
                if not analyte['available']:
                    continue
                cal = v1['calibrations'][analyte['analyte_id']]
                self.assertEqual(cal['slope'], analyte['slope'])
                self.assertEqual(cal['intercept'], analyte['intercept'])
                self.assertEqual(cal['min_concentration'],
                                 analyte['min_concentration'])
                self.assertEqual(cal['max_concentration'],
                                 analyte['max_concentration'])
                quantified = {(q['run_id'], q['analyte_id']): q
                              for q in v1['quantified']}
                for row in analyte['scores']:
                    ref = quantified[(row['run_id'], analyte['analyte_id'])]
                    self.assertEqual(ref['concentration'], row['prediction'])
                    self.assertEqual(ref['negative'], row['negative'])
                    self.assertEqual(ref['extrapolated'], row['extrapolated'])


# ----------------------------------------------------- determinism and CLI I/O

class DeterminismTests(CliHarnessTests):
    def test_reports_are_byte_identical_under_permutations(self):
        source = worked_source([['0', '0'], ['0.5', '2'], ['2', '0']])
        shuffled = dict(source)
        shuffled['runs'] = list(reversed(source['runs']))
        plan_a = WORKED_PLAN
        plan_b = {'score_ids': list(reversed(WORKED_PLAN['score_ids'])),
                  'origins': dict(reversed(list(WORKED_PLAN['origins'].items())))}
        payloads = []
        with tempfile.TemporaryDirectory() as directory:
            p1 = write_json(directory, 'p1.json', plan_a)
            p2 = write_json(directory, 'p2.json', plan_b)
            payloads.append(json.dumps(benchmark(source, p1), sort_keys=True))
            payloads.append(json.dumps(benchmark(shuffled, p1), sort_keys=True))
            payloads.append(json.dumps(benchmark(source, p2), sort_keys=True))
        self.assertEqual(len(set(payloads)), 1)

    def test_cli_writes_byte_deterministic_report(self):
        with tempfile.TemporaryDirectory() as directory:
            code, _err, payload, path = invoke_cli(
                directory, self.source, self.plan)
            self.assertEqual(code, 0)
            first = path.read_bytes()
            second_path = Path(directory) / 'again.json'
            code2, _err2, _payload2, _ = invoke_cli(
                directory, self.source, self.plan, output_name='again.json')
            self.assertEqual(code2, 0)
            self.assertEqual(first, second_path.read_bytes())
            self.assertEqual(payload['schema_version'], 1)


class PlanValidationTests(CliHarnessTests):
    def _bad(self, mutate_raw=None, plan=None):
        with tempfile.TemporaryDirectory() as directory:
            if mutate_raw is not None:
                raw = mutate_raw(json.dumps(WORKED_PLAN).encode('utf-8'))
                code, stderr, payload, path = invoke_cli(
                    directory, self.source, self.plan, plan_raw=raw)
            else:
                code, stderr, payload, path = invoke_cli(
                    directory, worked_source(triangle(2)), plan)
            self.assertEqual(code, 2, stderr)
            self.assertTrue(stderr.strip())
            self.assertIsNone(payload)
            self.assertFalse(path.exists())

    def test_missing_extra_unknown_and_duplicate_references(self):
        plan = json.loads(json.dumps(WORKED_PLAN))
        del plan['origins']['U']
        self._bad(plan=plan)
        plan = json.loads(json.dumps(WORKED_PLAN))
        plan['origins']['GHOST'] = 'ox'
        self._bad(plan=plan)
        plan = json.loads(json.dumps(WORKED_PLAN))
        plan['score_ids'] = ['U', 'UNK']
        self._bad(plan=plan)
        plan = json.loads(json.dumps(WORKED_PLAN))
        plan['score_ids'] = ['U', 'U']
        self._bad(plan=plan)

    def test_bad_structure_duplicate_keys_and_oversize_plan(self):
        self._bad(plan={'origins': WORKED_PLAN['origins']})
        self._bad(plan={'score_ids': ['U'], 'origins': {}})
        self._bad(plan={'origins': WORKED_PLAN['origins'],
                        'score_ids': [], 'extra': 1})

        def dup_key(raw):
            return raw.replace(b'"score_ids"', b'"score_ids": [] , "score_ids"', 1)
        self._bad(mutate_raw=dup_key)
        with tempfile.TemporaryDirectory() as directory:
            big = write_json(directory, 'big.json', None,
                             raw=b'{"origins":{},"score_ids":[]}' + b' ' * 1_000_001)
            out = Path(directory) / 'o.json'

            class Out:
                buffer = io.BytesIO()

                @staticmethod
                def flush():
                    return None

            with redirect_stdout(Out()), redirect_stderr(io.StringIO()) as err:
                code = cli.main(['benchmark', '--source',
                                 str(write_json(directory, 'source.json',
                                                worked_source(triangle(2)))),
                                 '--plan', str(big), '--output', str(out)])
            self.assertEqual(code, 2)
            self.assertFalse(out.exists())

    def test_invalid_origin_id_and_bad_source_are_refused(self):
        plan = json.loads(json.dumps(WORKED_PLAN))
        plan['origins']['U'] = 'bad origin!'
        self._bad(plan=plan)
        with tempfile.TemporaryDirectory() as directory:
            broken = write_json(directory, 'broken.json', None,
                                raw=b'{"analytes": [], "runs": []}')
            out = Path(directory) / 'o.json'

            class Out:
                buffer = io.BytesIO()

                @staticmethod
                def flush():
                    return None

            with redirect_stdout(Out()), redirect_stderr(io.StringIO()) as err:
                code = cli.main(['benchmark', '--source', str(broken),
                                 '--plan',
                                 str(write_json(directory, 'p.json', WORKED_PLAN)),
                                 '--output', str(out)])
            self.assertEqual(code, 2)
            self.assertFalse(out.exists())


class OutputConflictTests(CliHarnessTests):
    def test_existing_file_and_symlink_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            code, _err, _payload, path = invoke_cli(
                directory, self.source, self.plan, output_name='r.json')
            self.assertEqual(code, 0)
            keeper = path.read_bytes()
            code, stderr, _payload, _path2 = invoke_cli(
                directory, self.source, self.plan, output_name='r.json')
            self.assertEqual(code, 2)
            self.assertIn('exists', stderr)
            self.assertEqual(path.read_bytes(), keeper)

            link = Path(directory) / 'link.json'
            os.symlink(path, link)
            code, stderr, _payload, _ = invoke_cli(
                directory, self.source, self.plan, output_name='link.json')
            self.assertEqual(code, 2)
            self.assertEqual(path.read_bytes(), keeper)
            self.assertTrue(link.is_symlink())


# --------------------------------------------------------- large synthetic run

class LargeSyntheticSubprocessTests(unittest.TestCase):
    """The 200-run / 30-analyte case runs under an RLIMIT_CPU budget."""

    def test_small_scale_drive_verifies_zero_errors(self):
        driver = ROOT / 'scripts' / 'benchmark_synthetic_demo.py'
        runs, points, analytes = 8, 200, 3
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, '-I', str(driver), '--workdir', directory,
                 '--runs', str(runs), '--points', str(points),
                 '--analytes', str(analytes), '--cpu', '60'],
                capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads((Path(directory) / 'benchmark-report.json')
                                .read_text())
        expected_slope = sum(1 for t in range(points)
                             if t % 2 == 0 and 2 <= t <= points - 4)
        self._assert_clean(report, runs, analytes, expected_slope)

    def _assert_clean(self, report, expected_runs, expected_analytes,
                      expected_slope):
        self.assertEqual(len(report['analyte_ids']), expected_analytes)
        self.assertEqual(len(report['folds']), expected_runs // 2)
        for fold in report['folds']:
            self.assertEqual(len(fold['training_run_ids']), expected_runs - 2)
            for analyte in fold['analytes']:
                self.assertTrue(analyte['available'])
                self.assertEqual(analyte['slope'], str(expected_slope))
                self.assertEqual(analyte['intercept'], '0')
                for row in analyte['scores']:
                    self.assertEqual(row['error'], '0')
                    self.assertFalse(row['negative'])
        for summary in report['summaries']:
            self.assertEqual(summary['successful_groups'], expected_runs // 2)
            self.assertEqual(summary['total_mean_absolute_error'], '0')
            for group in summary['group_mean_absolute_errors']:
                self.assertEqual(group['mean_absolute_error'], '0')


if __name__ == '__main__':
    unittest.main()
