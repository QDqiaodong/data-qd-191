"""Acceptance and contract tests for the offline aggregate disclosure review.

The brute-force oracle below is deliberately independent of
peak_quant.disclosure's elimination engine: it decides whether a protected
unit is determined by a prime-modulus feasibility test (two different forced
values must both be solvable), recomputing the whole priority review from
scratch for every scenario.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
from itertools import product as cartesian
from pathlib import Path

from peak_quant import cli
from peak_quant import disclosure as disc
from peak_quant.io import load

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'disclosure'
SOURCE = FIXTURES / 'input.json'
NEG_SOURCE = FIXTURES / 'input-negative.json'


# --------------------------------------------------------------------- helpers

def invoke(argv):
    buffer = io.BytesIO()

    class Out:
        flush = staticmethod(lambda: None)
    Out.buffer = buffer
    with redirect_stdout(Out()), redirect_stderr(io.StringIO()) as err:
        code = cli.main(argv)
    return code, buffer.getvalue(), err.getvalue()


def plan_path(name):
    return str(FIXTURES / f'{name}.json')


def generate(name, bundle, source=SOURCE):
    code, out, err = invoke(['disclosure', '--source', str(source),
                             '--plan', plan_path(name), '--output', str(bundle)])
    assert code == 0, err
    return json.loads((bundle / 'public.json').read_text()), \
        json.loads((bundle / 'audit.json').read_text())


def check(name, bundle):
    code, _out, err = invoke(['disclosure-check', '--plan', plan_path(name),
                              '--public', str(bundle / 'public.json'),
                              '--audit', str(bundle / 'audit.json')])
    assert code == 0, err


def write_json(directory, name, payload):
    path = Path(directory) / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return str(path)


# ------------------------------------------------------- independent oracle

def _solvable(rows, fixed, modulus, protected=()):
    """Gaussian elimination over GF(modulus); rows are member coefficient lists.

    Returns whether the system rows @ x = (arbitrary RHS) admits an assignment
    with the fixed units pinned to their forced values.  Only feasibility of
    the homogeneous coefficient system under pinning is needed.
    """
    units = sorted({unit for row in rows for unit in row} | set(fixed) | set(protected))
    index = {unit: i for i, unit in enumerate(units)}
    matrix = [[0] * len(units) for _ in rows]
    for r, row in enumerate(rows):
        for unit in row:
            matrix[r][index[unit]] = 1
    # Pin constraints: one extra row x_u = v for each forced unit.
    for unit, value in fixed.items():
        row = [0] * len(units)
        row[index[unit]] = 1
        matrix.append(row)
    rhs = [0] * len(rows) + [value % modulus for value in fixed.values()]
    rank = 0
    ncols = len(units)
    for column in range(ncols):
        pivot = next((r for r in range(rank, len(matrix))
                      if matrix[r][column] % modulus), None)
        if pivot is None:
            continue
        matrix[rank], matrix[pivot] = matrix[pivot], matrix[rank]
        rhs[rank], rhs[pivot] = rhs[pivot], rhs[rank]
        inverse = pow(matrix[rank][column] % modulus, modulus - 2, modulus)
        for c in range(ncols):
            matrix[rank][c] = matrix[rank][c] * inverse % modulus
        rhs[rank] = rhs[rank] * inverse % modulus
        for r in range(len(matrix)):
            if r == rank or matrix[r][column] == 0:
                continue
            factor = matrix[r][column]
            for c in range(ncols):
                matrix[r][c] = (matrix[r][c] - factor * matrix[rank][c]) % modulus
            rhs[r] = (rhs[r] - factor * rhs[rank]) % modulus
        rank += 1
    for r in range(rank, len(matrix)):
        if matrix[r] == [0] * ncols and rhs[r] % modulus != 0:
            return False
    return True


def oracle_is_determined(rows, protected, modulus=101):
    """A protected unit is determined iff not both forced values are feasible."""
    result = set()
    for unit in protected:
        if not _solvable(rows, {unit: 0}, modulus, protected) \
                or not _solvable(rows, {unit: 1}, modulus, protected):
            result.add(unit)
    return result


def oracle_review(plan):
    """Replay the priority review with the independent prime-modulus oracle."""
    rows = []
    decisions = []
    protected = {tuple(p) for p in plan['protected']}
    for entry in plan['published']:
        rows.append([(m, entry['analyte_id']) for m in entry['members']])
    if oracle_is_determined(rows, protected):
        return None
    for entry in plan['requests']:
        trial = rows + [[(m, entry['analyte_id']) for m in entry['members']]]
        if oracle_is_determined(trial, protected):
            decisions.append((entry['id'], 'withheld'))
        else:
            decisions.append((entry['id'], 'issued'))
            rows = trial
    return decisions


# --------------------------------------------------------------------- tests

class AcceptanceScenarioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_main_scenario_decisions_totals_and_proofs(self):
        bundle = self.dir / 'b1'
        public, audit = generate('plan', bundle)
        self.assertEqual([r['id'] for r in public['issued']], ['B', 'D'])
        totals = {r['id']: r['total'] for r in public['issued']}
        self.assertEqual(totals, {'B': '5/3', 'D': '11/3'})
        self.assertEqual(
            [(r['id'], r['decision']) for r in audit['requests']],
            [('B', 'issued'), ('C', 'withheld'), ('D', 'issued')])
        proof = audit['requests'][1]['withholding_proof']
        self.assertEqual(proof['protected_unit'], ['X', 'A'])
        coeffs = {t['row_id']: t['coefficient']
                  for t in proof['combination']}
        # (H + B - C) / 2 = e_X
        self.assertEqual(coeffs, {'H': '1/2', 'B': '1/2', 'C': '-1/2'})
        check('plan', bundle)
        # The audit never stores a withheld total or any detail value.
        text = (bundle / 'audit.json').read_text()
        for forbidden in ('2', '5/3', '11/3', '1/3', '2/3', '4/3'):
            self.assertNotIn(f'"total": "{forbidden}"', text)
        public_text = (bundle / 'public.json').read_text()
        for forbidden in ('protected', 'withheld', 'H', 'proof', 'source'):
            self.assertNotIn(forbidden, public_text)

    def test_request_e_is_withheld_even_after_d(self):
        bundle = self.dir / 'be'
        public, audit = generate('plan-e', bundle)
        self.assertEqual([r['id'] for r in public['issued']], ['B', 'D'])
        self.assertEqual(
            [(r['id'], r['decision']) for r in audit['requests']],
            [('B', 'issued'), ('C', 'withheld'),
             ('D', 'issued'), ('E', 'withheld')])
        e_proof = audit['requests'][3]['withholding_proof']
        ids = {t['row_id'] for t in e_proof['combination']}
        self.assertIn('E', ids)
        self.assertNotIn('C', ids)  # a withheld row can never be a fact
        check('plan-e', bundle)

    def test_swapped_priority_reverses_b_and_c(self):
        bundle = self.dir / 'bs'
        public, audit = generate('plan-swapped', bundle)
        self.assertEqual([r['id'] for r in public['issued']], ['C', 'D'])
        self.assertEqual(
            [(r['id'], r['decision']) for r in audit['requests']],
            [('C', 'issued'), ('B', 'withheld'), ('D', 'issued')])
        check('plan-swapped', bundle)

    def test_history_yw_changes_every_outcome(self):
        bundle = self.dir / 'byw'
        public, audit = generate('plan-yw', bundle)
        self.assertEqual([r['id'] for r in public['issued']], ['B', 'C'])
        self.assertEqual(
            [(r['id'], r['decision']) for r in audit['requests']],
            [('B', 'issued'), ('C', 'issued'), ('D', 'withheld')])
        check('plan-yw', bundle)

    def test_bundles_differ_in_content_not_just_counts(self):
        first = self.dir / 'a'
        second = self.dir / 'b'
        pub1, aud1 = generate('plan', first)
        pub2, aud2 = generate('plan-yw', second)
        self.assertNotEqual(
            json.dumps(pub1, sort_keys=True), json.dumps(pub2, sort_keys=True))
        self.assertNotEqual(
            json.dumps(aud1, sort_keys=True), json.dumps(aud2, sort_keys=True))
        # Both still verify against their own plans.
        check('plan', first)
        check('plan-yw', second)

    def test_history_that_already_loses_protection_fails_generation(self):
        target = self.dir / 'lost'
        code, _out, err = invoke(['disclosure', '--source', str(SOURCE),
                                  '--plan', plan_path('plan-hbc-history'),
                                  '--output', str(target)])
        self.assertEqual(code, 2)
        self.assertIn('already determine', err)
        self.assertFalse(target.exists())

    def test_forged_history_total_rejected(self):
        target = self.dir / 'forged'
        code, _out, err = invoke(['disclosure', '--source', str(SOURCE),
                                  '--plan', plan_path('plan-bad-total'),
                                  '--output', str(target)])
        self.assertEqual(code, 2)
        self.assertIn('does not match the fixed snapshot total', err)
        self.assertFalse(target.exists())

    def test_all_withheld_is_success_with_empty_public_list(self):
        bundle = self.dir / 'aw'
        public, audit = generate('plan-all-withheld', bundle)
        self.assertEqual(public['issued'], [])
        self.assertEqual(audit['requests'][0]['decision'], 'withheld')
        self.assertIsNotNone(audit['requests'][0]['withholding_proof'])
        check('plan-all-withheld', bundle)

    def test_other_analyte_of_same_run_publishes_independently(self):
        bundle = self.dir / 'ab'
        public, audit = generate('plan-analyte-b', bundle)
        self.assertEqual([r['id'] for r in public['issued']], ['BB'])
        self.assertEqual(public['issued'][0]['analyte_id'], 'B')
        check('plan-analyte-b', bundle)

    def test_negative_and_extrapolated_values_pass_through_unfiltered(self):
        report = disc.process(load(str(NEG_SOURCE)))
        flags = {q['run_id']: q for q in report['quantified']}
        self.assertEqual(flags['N']['concentration'], '-1')
        self.assertTrue(flags['N']['negative'])
        self.assertTrue(flags['N']['extrapolated'])
        self.assertTrue(flags['P']['extrapolated'])
        bundle = self.dir / 'neg'
        public, _audit = generate('plan-negative', bundle, source=NEG_SOURCE)
        r0 = next(row for row in public['issued'] if row['id'] == 'R0')
        # -1 + 3 + 1 = 3: the negative extrapolated member is included.
        self.assertEqual(r0['total'], '3')
        self.assertEqual(r0['members'], ['N', 'P', 'Q'])
        check('plan-negative', bundle)


    def test_safe_redundant_row_with_different_id_is_issued(self):
        data = {
            'protected': [['X', 'A']],
            'published': [
                {'id': 'H', 'analyte_id': 'A', 'members': ['X', 'Y'],
                 'total': '1'}],
            'requests': [
                {'id': 'B', 'analyte_id': 'A', 'members': ['X', 'Z']},
                {'id': 'B2', 'analyte_id': 'A', 'members': ['Z', 'X']},
            ],
        }
        plan_file = write_json(self.dir, 'plan.json', data)
        bundle = self.dir / 'bundle'
        code, _out, err = invoke(['disclosure', '--source', str(SOURCE),
                                  '--plan', plan_file, '--output', str(bundle)])
        self.assertEqual(code, 0, err)
        public = json.loads((bundle / 'public.json').read_text())
        self.assertEqual([r['id'] for r in public['issued']], ['B', 'B2'])
        self.assertEqual([r['total'] for r in public['issued']],
                         ['5/3', '5/3'])
        code, _out, err = invoke(['disclosure-check', '--plan', plan_file,
                                  '--public', str(bundle / 'public.json'),
                                  '--audit', str(bundle / 'audit.json')])
        self.assertEqual(code, 0, err)


class OracleCrossCheckTests(unittest.TestCase):
    """Compare the engine against the independent oracle on many random plans."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _plans(self):
        plan = disc.load_plan(plan_path('plan'))
        source = load(str(SOURCE))
        concentrations = disc.validate_plan_against_source(plan, source)
        runs = ['X', 'Y', 'Z', 'W']
        return plan, concentrations, runs

    def test_random_plans_match_prime_oracle(self):
        import random
        rng = random.Random(20261122)
        base_plan, concentrations, runs = self._plans()
        totals = {
            tuple(sorted(members)): str(sum(
                concentrations[(m, 'A')] for m in members))
            for size in range(1, 4)
            for members in cartesian(runs, repeat=size)
            if len(set(members)) == size
        }
        for trial in range(60):
            history = []
            for index in range(rng.randint(0, 2)):
                members = rng.sample(runs, rng.randint(2, 3))
                history.append({
                    'id': f'H{index}', 'analyte_id': 'A',
                    'members': members,
                    'total': totals[tuple(sorted(members))],
                })
            requests = []
            used = {h['id'] for h in history}
            for index in range(rng.randint(1, 5)):
                members = rng.sample(runs, rng.randint(2, 3))
                row_id = f'R{index}'
                if row_id in used:
                    continue
                used.add(row_id)
                requests.append({'id': row_id, 'analyte_id': 'A',
                                 'members': members})
            data = {'protected': [['X', 'A']], 'published': history,
                    'requests': requests}
            plan = disc.parse_plan(data)
            expected = oracle_review(plan)
            if expected is None:
                with self.assertRaises(disc.DisclosureError):
                    disc.run_review(plan, concentrations)
                continue
            public, audit = disc.build_bundle_documents(load(str(SOURCE)), plan)
            actual = [(r['id'], r['decision']) for r in audit['requests']]
            self.assertEqual(actual, expected, f'trial {trial}: {data}')
            # The independent checker must accept every generated bundle, fed
            # through the real JSON encoding (lists, not internal tuples).
            public_json = json.loads(disc.encode_document(public))
            audit_json = json.loads(disc.encode_document(audit))
            disc.verify_disclosure(plan, public_json, audit_json)
            # Every withholding proof must use the withheld row itself and
            # evaluate coefficient-by-coefficient to a protected unit vector.
            row_by_id = {r['id']: r for r in plan['published'] + plan['requests']}
            for entry in audit['requests']:
                if entry['decision'] != 'withheld':
                    continue
                proof = entry['withholding_proof']
                terms = proof['combination']
                self.assertTrue(
                    any(t['row_id'] == entry['id'] for t in terms))
                accumulated = {}
                analyte = proof['protected_unit'][1]
                for term in terms:
                    coefficient = Fraction(term['coefficient'])
                    for member in row_by_id[term['row_id']]['members']:
                        key = (member, analyte)
                        accumulated[key] = accumulated.get(key, Fraction(0)) + coefficient
                        if accumulated[key] == 0:
                            del accumulated[key]
                self.assertEqual(
                    accumulated,
                    {tuple(proof['protected_unit']): Fraction(1)})


class DeterminismAndLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_repeated_generation_is_byte_identical(self):
        first = self.dir / 'one'
        second = self.dir / 'two'
        p1, a1 = generate('plan', first)
        p2, a2 = generate('plan', second)
        self.assertEqual((first / 'public.json').read_bytes(),
                         (second / 'public.json').read_bytes())
        self.assertEqual((first / 'audit.json').read_bytes(),
                         (second / 'audit.json').read_bytes())

    def test_legal_reorderings_do_not_change_bytes(self):
        raw = json.loads((FIXTURES / 'plan.json').read_text())
        shuffled = {
            'protected': list(reversed(raw['protected'])),
            'published': [
                {**row, 'members': list(reversed(row['members']))}
                for row in reversed(raw['published'])
            ],
            'requests': raw['requests'],
        }
        plan_a = disc.load_plan(plan_path('plan'))
        plan_b = disc.parse_plan(shuffled)
        source = load(str(SOURCE))
        pa, aa = disc.build_bundle_documents(source, plan_a)
        pb, ab = disc.build_bundle_documents(source, plan_b)
        self.assertEqual(disc.encode_document(pa), disc.encode_document(pb))
        self.assertEqual(disc.encode_document(aa), disc.encode_document(ab))

    def test_priority_order_cannot_be_reordered(self):
        raw = json.loads((FIXTURES / 'plan.json').read_text())
        reordered = dict(raw)
        reordered['requests'] = list(reversed(raw['requests']))
        plan_a = disc.parse_plan(raw)
        plan_b = disc.parse_plan(reordered)
        source = load(str(SOURCE))
        pa, _aa = disc.build_bundle_documents(source, plan_a)
        pb, _ab = disc.build_bundle_documents(source, plan_b)
        self.assertNotEqual(disc.encode_document(pa), disc.encode_document(pb))

    def test_existing_bundle_and_symlink_are_refused(self):
        target = self.dir / 'bundle'
        generate('plan', target)
        code, _out, err = invoke(['disclosure', '--source', str(SOURCE),
                                  '--plan', plan_path('plan'),
                                  '--output', str(target)])
        self.assertEqual(code, 2)
        self.assertIn('already exists', err)
        link = self.dir / 'link'
        os.symlink(target, link)
        code, _out, err = invoke(['disclosure', '--source', str(SOURCE),
                                  '--plan', plan_path('plan'),
                                  '--output', str(link)])
        self.assertEqual(code, 2)
        self.assertEqual(os.readlink(link), str(target))

    def test_second_write_failure_leaves_no_bundle_and_keeps_existing_dirs(self):
        target = self.dir / 'bundle'
        original_write = disc.write_bundle
        calls = {'n': 0}

        def failing_write(bundle_dir, public_bytes, audit_bytes):
            calls['n'] += 1
            if calls['n'] == 1:
                raise OSError('simulated second-file write failure')
            return original_write(bundle_dir, public_bytes, audit_bytes)

        disc.write_bundle = failing_write
        try:
            code, _out, err = invoke(['disclosure', '--source', str(SOURCE),
                                      '--plan', plan_path('plan'),
                                      '--output', str(target)])
        finally:
            disc.write_bundle = original_write
        self.assertEqual(code, 2)
        self.assertIn('simulated', err)
        self.assertFalse(target.exists())
        # No hidden staging debris remains in the parent.
        self.assertEqual([p for p in self.dir.iterdir()
                          if p.name.startswith('.disclosure-')], [])
        # A pre-existing sibling directory is untouched.
        sibling = self.dir / 'keeper'
        sibling.mkdir()
        code, _out, _err = invoke(['disclosure', '--source', str(SOURCE),
                                   '--plan', plan_path('plan'),
                                   '--output', str(target)])
        self.assertEqual(code, 0)
        self.assertTrue(sibling.is_dir())

    def test_tailing_garbage_in_plan_rejected_without_bundle(self):
        plan_file = self.dir / 'bad.json'
        plan_file.write_text((FIXTURES / 'plan.json').read_text() + '\n[]',
                             encoding='utf-8')
        target = self.dir / 'out'
        code, _out, err = invoke(['disclosure', '--source', str(SOURCE),
                                  '--plan', str(plan_file),
                                  '--output', str(target)])
        self.assertEqual(code, 2)
        self.assertFalse(target.exists())


class OfflineCheckerTamperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.bundle = self.dir / 'bundle'
        generate('plan', self.bundle)

    def _reject(self, mutate, fragment):
        public = json.loads((self.bundle / 'public.json').read_text())
        audit = json.loads((self.bundle / 'audit.json').read_text())
        mutate(public, audit)
        pub_path = write_json(self.dir, 'public.json', public)
        aud_path = write_json(self.dir, 'audit.json', audit)
        code, _out, err = invoke(['disclosure-check', '--plan', plan_path('plan'),
                                  '--public', pub_path, '--audit', aud_path])
        self.assertEqual(code, 2)
        self.assertIn(fragment, err)

    def test_withholding_proof_citing_a_later_row_is_rejected(self):
        def mutate(_public, audit):
            proof = audit['requests'][1]['withholding_proof']
            proof['combination'] = [
                {'row_id': 'H', 'coefficient': '1/2'},
                {'row_id': 'B', 'coefficient': '1/2'},
                {'row_id': 'D', 'coefficient': '-1/2'},
            ]
        self._reject(mutate, 'real issued prefix')

    def test_withheld_row_must_participate_in_its_own_proof(self):
        def mutate(_public, audit):
            proof = audit['requests'][1]['withholding_proof']
            proof['combination'] = [
                {'row_id': 'H', 'coefficient': '1'},
                {'row_id': 'B', 'coefficient': '1'},
            ]
        self._reject(mutate, 'include the withheld row')

    def test_freedom_proof_that_changes_an_issued_row_is_rejected(self):
        def mutate(_public, audit):
            audit['freedom_proofs'][0]['changes'] = [
                {'unit': ['X', 'A'], 'change': '1'},
                {'unit': ['Z', 'A'], 'change': '1'},
            ]
        self._reject(mutate, 'changes published/issued row')

    def test_freedom_proof_with_zero_target_move_is_rejected(self):
        def mutate(_public, audit):
            audit['freedom_proofs'][0]['changes'] = [
                {'unit': ['X', 'A'], 'change': '0'},
                {'unit': ['Y', 'A'], 'change': '1'},
            ]
        self._reject(mutate, 'non-zero')

    def test_public_member_tampering_is_rejected(self):
        def mutate(public, _audit):
            public['issued'][0]['members'] = ['X', 'Y']
        self._reject(mutate, 'members do not match PLAN')

    def test_public_order_tampering_is_rejected(self):
        def mutate(public, _audit):
            public['issued'].reverse()
        self._reject(mutate, 'in priority order')

    def test_proof_unit_unknown_to_plan_is_rejected(self):
        def mutate(_public, audit):
            audit['freedom_proofs'][0]['changes'].append(
                {'unit': ['GHOST', 'A'], 'change': '1'})
        self._reject(mutate, 'appears nowhere in the PLAN')

    def test_missing_freedom_proof_is_rejected(self):
        def mutate(_public, audit):
            audit['freedom_proofs'] = []
        self._reject(mutate, 'every protected unit exactly once')

    def test_check_runs_in_clean_directory_without_source(self):
        isolated = self.dir / 'clean'
        isolated.mkdir()
        shutil.copy(self.bundle / 'public.json', isolated / 'public.json')
        shutil.copy(self.bundle / 'audit.json', isolated / 'audit.json')
        shutil.copy(FIXTURES / 'plan.json', isolated / 'plan.json')
        env = dict(os.environ)
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
        completed = subprocess.run(
            [sys.executable, '-m', 'peak_quant.cli', 'disclosure-check',
             '--plan', 'plan.json', '--public', 'public.json',
             '--audit', 'audit.json'],
            cwd=isolated, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)


class PlanValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def base(self):
        return json.loads((FIXTURES / 'plan.json').read_text())

    def expect_error(self, mutate, fragment):
        data = self.base()
        mutate(data)
        path = write_json(self.dir, 'plan.json', data)
        target = self.dir / 'bundle'
        code, _out, err = invoke(['disclosure', '--source', str(SOURCE),
                                  '--plan', path, '--output', str(target)])
        self.assertEqual(code, 2)
        self.assertIn(fragment, err)
        self.assertFalse(target.exists())

    def test_duplicate_json_keys_rejected(self):
        path = self.dir / 'dup.json'
        text = (FIXTURES / 'plan.json').read_text()
        text = text.replace('"protected":', '"protected": [], "protected":', 1)
        path.write_text(text, encoding='utf-8')
        code, _out, err = invoke(['disclosure', '--source', str(SOURCE),
                                  '--plan', str(path),
                                  '--output', str(self.dir / 'b')])
        self.assertEqual(code, 2)
        self.assertIn('duplicate JSON key', err)

    def test_structural_and_reference_rules(self):
        self.expect_error(lambda d: d.__setitem__('extra', 1), 'expected fields')
        self.expect_error(lambda d: d['requests'][0].__setitem__('extra', 1),
                          'expected fields')
        self.expect_error(lambda d: d.__delitem__('requests'), 'expected fields')
        self.expect_error(lambda d: d['protected'].pop(), 'non-empty')
        self.expect_error(lambda d: d['protected'].append(d['protected'][0]),
                          'unique')
        self.expect_error(lambda d: d['requests'][0]['members'].append('X'),
                          'unique')
        self.expect_error(lambda d: d['requests'][0].__setitem__('members', []),
                          'non-empty')
        self.expect_error(lambda d: d['requests'][0].__setitem__('id', 'H'),
                          'unique across')
        self.expect_error(lambda d: d['requests'][0].__setitem__(
            'analyte_id', 'NOPE'), 'unknown analyte')
        self.expect_error(lambda d: d['requests'][0]['members'].__setitem__(0, 's0'),
                          'unknown run')
        self.expect_error(lambda d: d['published'][0].__setitem__('total', '1/0'),
                          'positive')

    def test_rational_string_grammar_and_size(self):
        for bad in ('2/2', '-0', '01', '1.5', '3/04', 'abc', 2, True):
            with self.assertRaises(disc.DisclosureError):
                disc.parse_rational(bad, 'test value')
        huge = '1' + '0' * 40
        with self.assertRaises(disc.DisclosureError):
            disc.parse_rational(huge, 'test value')
        for good in ('0', '-3', '5/3', '-7/9', '12345678901234567890'):
            self.assertEqual(str(disc.parse_rational(good, 'test value')), good)

    def test_over_limit_plan_rejected(self):
        data = self.base()
        data['requests'] = [
            {'id': f'R{i}', 'analyte_id': 'A', 'members': ['Y', 'Z']}
            for i in range(513)
        ]
        path = write_json(self.dir, 'big.json', data)
        code, _out, err = invoke(['disclosure', '--source', str(SOURCE),
                                  '--plan', path,
                                  '--output', str(self.dir / 'b')])
        self.assertEqual(code, 2)
        self.assertIn('512', err)

    def test_source_fit_failure_delivers_nothing(self):
        bad_source = json.loads(SOURCE.read_text())
        for run in bad_source['runs']:
            if run['kind'] == 'standard':
                run['concentrations'] = {'A': '1', 'B': '1'}
        source_path = write_json(self.dir, 'input.json', bad_source)
        target = self.dir / 'bundle'
        code, _out, err = invoke(['disclosure', '--source', source_path,
                                  '--plan', plan_path('plan'),
                                  '--output', str(target)])
        self.assertEqual(code, 2)
        self.assertFalse(target.exists())


class CheckerVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.bundle = self.dir / 'bundle'
        generate('plan', self.bundle)

    def test_wrong_versions_rejected(self):
        for name, fragment in (('public.json', 'public format version'),
                               ('audit.json', 'audit format version')):
            doc = json.loads((self.bundle / name).read_text())
            doc['format_version'] = 99
            path = write_json(self.dir, name, doc)
            code, _out, err = invoke([
                'disclosure-check', '--plan', plan_path('plan'),
                '--public', str(self.bundle / 'public.json')
                if name == 'audit.json' else path,
                '--audit', str(self.bundle / 'audit.json')
                if name == 'public.json' else path])
            self.assertEqual(code, 2)
            self.assertIn(fragment, err)


if __name__ == '__main__':
    unittest.main()
