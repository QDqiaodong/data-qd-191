import hashlib
import io
import json
import os
import tempfile
import unittest
import copy
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from peak_quant import cli
from peak_quant.release import (encode_report, release, release_show, compute_release,
                                _Store)
from peak_quant.request import canonical_identity, load_and_parse, parse_request

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'release'


def read_fixture(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


def write_request(directory, payload, name='request.json', raw=None):
    path = Path(directory) / name
    if raw is not None:
        path.write_bytes(raw)
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return str(path)


def triangle_run(run_id, kind, area_by_analyte, window_a=('0', '2'), window_b=('2', '4'),
                 concentration=None):
    """Build a run whose integral over each window equals the requested area.

    A triangle over [lo, hi] with apex height H has signed area H. Windows are
    disjoint so two triangles sit in one sample list.
    """
    ha = area_by_analyte.get('A', 0)
    hb = area_by_analyte.get('B', 0)
    samples = [['0', '0'], ['1', str(ha)], ['2', '0'], ['3', str(hb)], ['4', '0']]
    run = {'id': run_id, 'kind': kind, 'samples': samples}
    if concentration is not None:
        run['concentrations'] = concentration
    return run


def two_analyte_input():
    return {
        'analytes': [{'id': 'A', 'window': ['0', '2']}, {'id': 'B', 'window': ['2', '4']}],
        'runs': [
            triangle_run('s0', 'standard', {'A': 0, 'B': 0},
                         concentration={'A': '0', 'B': '0'}),
            triangle_run('s1', 'standard', {'A': 3, 'B': 3},
                         concentration={'A': '1', 'B': '1'}),
            triangle_run('s2', 'standard', {'A': 6, 'B': 6},
                         concentration={'A': '2', 'B': '2'}),
            triangle_run('c1', 'unknown', {'A': '1.2', 'B': '1.2'}),
            triangle_run('u1', 'unknown', {'A': '1.2', 'B': '1.2'}),
            triangle_run('c2', 'unknown', {'A': '6', 'B': '6'}),
            triangle_run('u2', 'unknown', {'A': '6', 'B': '6'}),
            triangle_run('c3', 'unknown', {'A': '6', 'B': '6'}),
        ],
    }


def write_input(directory, data, name='input.json'):
    path = Path(directory) / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return str(path)


def make_request(data, **overrides):
    raw = json.dumps(data).encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    controls = {'c1': {'A': {'target': '0.1', 'tolerance': '0.3'},
                       'B': {'target': '0.1', 'tolerance': '0.3'}},
                'c2': {'A': {'target': '2', 'tolerance': '0'},
                       'B': {'target': '2', 'tolerance': '0'}},
                'c3': {'A': {'target': '2', 'tolerance': '0'},
                       'B': {'target': '2', 'tolerance': '0'}}}
    payload = {'batch_id': 'b1', 'request_id': 'r1', 'expected_revision': 0,
               'source_sha256': digest, 'controls': controls,
               'excluded_standards': [], 'invalid_controls': []}
    payload.update(overrides)
    return payload, raw


class AcceptanceFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / 'store'
        self.source = str(FIXTURES / 'input.json')
        self.reports = []

    def submit(self, fixture_name, source=None):
        req = load_and_parse(str(FIXTURES / fixture_name))
        payload, revision, retried = release(str(self.store), req,
                                             source if source is not None else self.source)
        return json.loads(payload), payload, revision, retried

    def test_three_revisions_signing_and_corrections(self):
        r1, _, rev1, retried1 = self.submit('request-1.json')
        self.assertEqual((rev1, retried1), (1, False))
        self.assertEqual(r1['revision'], 1)
        statuses = {(s['run_id'], s['analyte_id']): s for s in r1['samples']}
        self.assertEqual({key: statuses[key]['status'] for key in statuses},
                         {('U', 'A'): 'released', ('U', 'B'): 'released',
                          ('V', 'A'): 'released', ('V', 'B'): 'released'})
        self.assertEqual(statuses[('U', 'A')]['estimate'], '3/2')
        self.assertEqual([c['action'] for c in r1['corrections']],
                         ['added', 'added', 'added', 'added'])
        basis = statuses[('U', 'A')]['release']
        self.assertEqual(basis['concentration'], '3/2')
        self.assertEqual(basis['calibration']['standard_run_ids'], ['s0', 's1', 's2'])
        self.assertEqual(basis['left_control']['run_id'], 'c_left')
        self.assertEqual(basis['right_control']['run_id'], 'c_right')
        self.assertEqual(basis['left_control']['estimate'], '1/2')

        r2, _, rev2, _ = self.submit('request-2.json')
        self.assertEqual(rev2, 2)
        s2 = {(s['run_id'], s['analyte_id']): s for s in r2['samples']}
        ua = s2[('U', 'A')]
        self.assertEqual(ua['status'], 'withheld')
        self.assertEqual(ua['estimate'], '3/2')
        self.assertEqual(ua['reasons'], ['left_control_failed'])
        self.assertIsNone(ua['release'])
        # Failed/invalid control is retained with its estimate, not deleted.
        invalid_control = next(c for c in r2['controls']
                               if c['run_id'] == 'c_left' and c['analyte_id'] == 'A')
        self.assertTrue(invalid_control['manually_invalid'])
        self.assertEqual(invalid_control['estimate'], '1/2')
        self.assertEqual(invalid_control['reasons'], ['marked_invalid'])
        self.assertEqual([(c['action'], c['run_id'], c['analyte_id'])
                          for c in r2['corrections']], [('withdrawn', 'U', 'A')])
        withdrawn = r2['corrections'][0]
        self.assertIsNone(withdrawn['current'])
        self.assertEqual(withdrawn['previous']['concentration'], '3/2')
        # The other three releases keep identical bases.
        for key in (('U', 'B'), ('V', 'A'), ('V', 'B')):
            self.assertEqual(s2[key]['release'], statuses[key]['release'])

        r3, _, rev3, _ = self.submit('request-3.json')
        self.assertEqual(rev3, 3)
        s3 = {(s['run_id'], s['analyte_id']): s for s in r3['samples']}
        self.assertEqual(s3[('U', 'A')]['reasons'], ['estimate_out_of_range'])
        self.assertEqual(s3[('U', 'A')]['estimate'], '3/2')
        cal_a = next(c for c in r3['calibrations'] if c['analyte_id'] == 'A')
        self.assertEqual(cal_a['standard_run_ids'], ['s0', 's1'])
        self.assertEqual(cal_a['min_concentration'], '0')
        self.assertEqual(cal_a['max_concentration'], '1')
        self.assertEqual([(c['action'], c['run_id'], c['analyte_id'])
                          for c in r3['corrections']], [('replaced', 'V', 'A')])
        replaced = r3['corrections'][0]
        self.assertEqual(replaced['previous']['calibration']['standard_run_ids'],
                         ['s0', 's1', 's2'])
        self.assertEqual(replaced['current']['calibration']['standard_run_ids'],
                         ['s0', 's1'])
        self.assertEqual(replaced['current']['concentration'], '1/2')
        # B calibration and releases never change.
        cal_b = next(c for c in r3['calibrations'] if c['analyte_id'] == 'B')
        self.assertEqual(cal_b['standard_run_ids'], ['s0', 's1', 's2'])
        self.assertEqual(s3[('U', 'B')]['status'], 'released')
        self.assertEqual(s3[('V', 'B')]['status'], 'released')

    def test_history_queries_match_committed_bytes_across_all_revisions(self):
        committed = []
        for name in ('request-1.json', 'request-2.json', 'request-3.json'):
            _, payload, revision, _ = self.submit(name)
            committed.append((revision, payload))
        for revision, payload in committed:
            self.assertEqual(release_show(str(self.store), 'batch1', revision), payload)

    def test_retry_is_idempotent_without_source_and_with_input_removed(self):
        _, first_bytes, _, _ = self.submit('request-1.json')
        source_path = Path(self.source)
        removed = source_path.read_bytes()
        source_path.unlink()
        try:
            # Equivalent JSON representation, no --source at all.
            req = load_and_parse(str(FIXTURES / 'request-1-retry.json'))
            retry_bytes, revision, retried = release(str(self.store), req)
            self.assertTrue(retried)
            self.assertEqual(revision, 1)
            self.assertEqual(retry_bytes, first_bytes)
            self.assertEqual(release_show(str(self.store), 'batch1', 1), first_bytes)
            # Only one revision exists.
            state = json.loads((self.store / 'state.json').read_text())
            self.assertEqual(state['batches']['batch1']['current_revision'], 1)
        finally:
            source_path.write_bytes(removed)

    def test_conflict_stale_revision_and_bad_fingerprint(self):
        self.submit('request-1.json')
        with self.assertRaisesRegex(ValueError, 'different identity'):
            release(str(self.store), load_and_parse(str(FIXTURES / 'request-1-conflict.json')),
                    self.source)
        with self.assertRaisesRegex(ValueError, 'expected_revision'):
            release(str(self.store), load_and_parse(str(FIXTURES / 'request-stale.json')),
                    self.source)
        badfp = json.loads((FIXTURES / 'request-badfp.json').read_text())
        badfp['batch_id'] = 'batch-fresh'
        badfp['request_id'] = 'req-badfp-fresh'
        badfp['expected_revision'] = 0
        badfp_path = write_request(self.tmp.name, badfp, 'badfp.json')
        with self.assertRaisesRegex(ValueError, 'source_sha256 does not match'):
            release(str(self.store), load_and_parse(badfp_path), self.source)
        state = json.loads((self.store / 'state.json').read_text())
        self.assertEqual(state['batches']['batch1']['current_revision'], 1)
        self.assertEqual(set(state['requests']), {'req-r1'})

    def test_retry_precedes_expected_revision_and_source_checks(self):
        self.submit('request-1.json')
        # Retry representation still carries expected_revision 0 while the
        # batch is now at revision 1; retry must win over the stale check.
        req = load_and_parse(str(FIXTURES / 'request-1-retry.json'))
        _, revision, retried = release(str(self.store), req)
        self.assertEqual((revision, retried), (1, True))


class CrashRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = str(Path(self.tmp.name) / 'store')
        self.data = two_analyte_input()
        self.source = write_input(self.tmp.name, self.data)
        self.request, _ = make_request(self.data)

    def test_pre_commit_failure_leaves_no_revision_or_reserved_id(self):
        original_commit = _Store.commit

        def failing_commit(store, request, identity, report_bytes):
            original_commit(store, request, identity, report_bytes)
            # Simulate loss of the process right at the commit boundary: the
            # durable state is then reverted to its pre-commit content while
            # staged debris remains, which recover() must sweep.
            (store.root / 'state.json').unlink()
            raise OSError('simulated pre-commit interruption')

        with mock.patch.object(_Store, 'commit', failing_commit):
            with self.assertRaises(OSError):
                release(self.store, parse_request(dict(self.request)), self.source)

        recovered = _Store(self.store)
        self.assertEqual(recovered.state, {'batches': {}, 'requests': {}})
        self.assertFalse(list(Path(self.store).rglob('.tmp-*')))
        self.assertFalse((Path(self.store) / 'requests').exists())
        # Re-submission starts cleanly at revision 1 and the id is not occupied.
        payload, revision, retried = release(
            self.store, parse_request(dict(self.request)), self.source)
        self.assertEqual((revision, retried), (1, False))
        self.assertEqual(release_show(self.store, 'b1', 1), payload)
        self.assertFalse(list(Path(self.store).rglob('.tmp-*')))

    def test_post_commit_interruption_is_recoverable_by_request_id(self):
        # Persist exactly as release() would, then emulate dying before stdout.
        request = parse_request(dict(self.request))
        source_raw = Path(self.source).read_bytes()
        computed = compute_release(request, source_raw)
        report = {'schema_version': 2, 'batch_id': request['batch_id'],
                  'request_id': request['request_id'],
                  'source_sha256': request['source_sha256'], 'revision': 1,
                  'calibrations': computed['calibrations'],
                  'controls': computed['controls'], 'samples': computed['samples'],
                  'corrections': []}
        report_bytes = encode_report(report)
        store = _Store(self.store)
        store.commit(request, canonical_identity(request), report_bytes)

        # A second process opening the store can fetch the original report,
        # and the id never produces a second revision.
        retry, revision, retried = release(
            self.store, parse_request(dict(self.request)), self.source)
        self.assertTrue(retried)
        self.assertEqual(revision, 1)
        self.assertEqual(retry, report_bytes)
        state = json.loads(Path(self.store, 'state.json').read_text())
        self.assertEqual(state['batches']['b1']['current_revision'], 1)
        next_request = dict(self.request, request_id='r2', expected_revision=1)
        _, revision2, retried2 = release(
            self.store, parse_request(next_request), self.source)
        self.assertEqual((revision2, retried2), (2, False))
        root = Path(self.store)
        self.assertEqual(sorted(p.relative_to(root).as_posix() for p in root.rglob('*')
                                if p.is_file()),
                         ['b1/1.json', 'b1/2.json', 'requests/r1.json',
                          'requests/r2.json', 'state.json'])


class BoundaryRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = str(Path(self.tmp.name) / 'store')

    def submit(self, data, request_overrides=None):
        source = write_input(self.tmp.name, data)
        payload, raw = make_request(data, **(request_overrides or {}))
        request_path = write_request(self.tmp.name, payload)
        req = load_and_parse(request_path)
        result, revision, _ = release(self.store, req, source)
        return json.loads(result), raw, payload, req, source

    def test_exact_fraction_tolerance_and_range_endpoints(self):
        report, _, _, _, _ = self.submit(two_analyte_input())
        control = next(c for c in report['controls'] if c['run_id'] == 'c1'
                       and c['analyte_id'] == 'A')
        # 1.2 / 3 = 2/5 exactly; |2/5 - 1/10| = 3/10 equals the tolerance.
        self.assertEqual(control['estimate'], '2/5')
        self.assertTrue(control['passed'])
        self.assertEqual(control['reasons'], [])
        endpoint_control = next(c for c in report['controls']
                                if c['run_id'] == 'c2' and c['analyte_id'] == 'A')
        self.assertEqual(endpoint_control['estimate'], '2')
        self.assertTrue(endpoint_control['passed'])
        sample = next(s for s in report['samples']
                      if s['run_id'] == 'u2' and s['analyte_id'] == 'A')
        self.assertEqual(sample['status'], 'released')
        self.assertEqual(sample['estimate'], '2')

    def test_control_registered_for_one_analyte_cannot_backfill_the_other(self):
        data = two_analyte_input()
        # Drop the B registrations on both controls; B samples must stay
        # withheld even though A-side controls pass.
        report, _, payload, _, _ = self.submit(
            data, {'controls': {rid: {'A': spec['A']}
                                for rid, spec in make_request(data)[0]['controls'].items()}})
        rows = {(s['run_id'], s['analyte_id']): s for s in report['samples']}
        self.assertEqual(rows[('u1', 'A')]['status'], 'released')
        self.assertEqual(rows[('u1', 'B')]['reasons'],
                         ['missing_left_control', 'missing_right_control'])
        self.assertIsNone(rows[('u1', 'B')]['release'])

    def test_duplicate_concentration_standards_are_independently_weighted(self):
        # Two standards share concentration 0 but have different responses;
        # equal-weight LS must keep both observations.
        data = {
            'analytes': [{'id': 'A', 'window': ['0', '2']}],
            'runs': [
                triangle_run('s0a', 'standard', {'A': 0},
                             concentration={'A': '0'}),
                triangle_run('s0b', 'standard', {'A': 4},
                             concentration={'A': '0'}),
                triangle_run('s1', 'standard', {'A': 3},
                             concentration={'A': '1'}),
                triangle_run('s2', 'standard', {'A': 9},
                             concentration={'A': '2'}),
                triangle_run('c1', 'unknown', {'A': 3}),
                triangle_run('u1', 'unknown', {'A': 3}),
                triangle_run('c2', 'unknown', {'A': 9}),
                triangle_run('u2', 'unknown', {'A': 9}),
                triangle_run('c3', 'unknown', {'A': 9}),
            ],
        }
        report, _, _, _, _ = self.submit(
            data, {'controls': {
                'c1': {'A': {'target': '1', 'tolerance': '0'}},
                'c2': {'A': {'target': '2', 'tolerance': '0'}},
                'c3': {'A': {'target': '2', 'tolerance': '0'}}}})
        cal = report['calibrations'][0]
        self.assertTrue(cal['available'])
        self.assertEqual(cal['slope'], '36/11')
        self.assertEqual(cal['standard_run_ids'], ['s0a', 's0b', 's1', 's2'])

    def test_unavailable_calibration_withholds_without_fabricating_estimate(self):
        data = two_analyte_input()
        for run in data['runs']:
            if run['kind'] == 'standard':
                run['concentrations']['B'] = '1'  # all B concentrations identical
        report, _, _, _, _ = self.submit(data)
        cal_b = next(c for c in report['calibrations'] if c['analyte_id'] == 'B')
        self.assertFalse(cal_b['available'])
        self.assertEqual(cal_b['reason'], 'identical_concentrations')
        cal_a = next(c for c in report['calibrations'] if c['analyte_id'] == 'A')
        self.assertTrue(cal_a['available'])
        for sample in report['samples']:
            if sample['analyte_id'] == 'B':
                self.assertIsNone(sample['estimate'])
                self.assertIn('calibration_unavailable', sample['reasons'])
                self.assertEqual(sample['status'], 'withheld')
            else:
                self.assertEqual(sample['status'], 'released')
        control_b = next(c for c in report['controls']
                         if c['run_id'] == 'c1' and c['analyte_id'] == 'B')
        self.assertIsNone(control_b['estimate'])
        self.assertEqual(control_b['reasons'], ['calibration_unavailable'])
        # No fabricated estimate can appear on a correction record.
        self.assertFalse(any(c['run_id'] == 'u1' and c['analyte_id'] == 'B'
                             for c in report['corrections']))

    def test_negative_estimate_is_retained_exactly_when_withheld(self):
        data = two_analyte_input()
        data['runs'].append(triangle_run('u_neg', 'unknown', {'A': '-3', 'B': '-3'}))
        report, _, _, _, _ = self.submit(data)
        neg = next(s for s in report['samples'] if s['run_id'] == 'u_neg'
                   and s['analyte_id'] == 'A')
        self.assertEqual(neg['estimate'], '-1')
        self.assertIn('estimate_negative', neg['reasons'])
        self.assertEqual(neg['status'], 'withheld')

    def test_nearest_controls_are_by_batch_position_and_never_skipped(self):
        data = two_analyte_input()
        # Insert an extra passing control before c1; marking c1 invalid must
        # still fail u1 because c1 remains the nearest left control.
        data['runs'].insert(3, triangle_run('c0', 'unknown', {'A': '1.2', 'B': '1.2'}))
        overrides = {'request_id': 'r1x',
                     'controls': {
                         'c0': {'A': {'target': '0.1', 'tolerance': '0.3'},
                                'B': {'target': '0.1', 'tolerance': '0.3'}},
                         'c1': {'A': {'target': '0.1', 'tolerance': '0.3'},
                                'B': {'target': '0.1', 'tolerance': '0.3'}},
                         'c2': {'A': {'target': '2', 'tolerance': '0'},
                                'B': {'target': '2', 'tolerance': '0'}}},
                     'invalid_controls': [['c1', 'A']]}
        report, _, _, _, _ = self.submit(data, overrides)
        u1 = next(s for s in report['samples'] if s['run_id'] == 'u1'
                  and s['analyte_id'] == 'A')
        self.assertEqual(u1['left_control_id'], 'c1')
        self.assertEqual(u1['reasons'], ['left_control_failed'])

    def test_input_validation_failure_is_not_a_local_fit_failure(self):
        data = two_analyte_input()
        del data['runs'][0]['concentrations']
        source = write_input(self.tmp.name, data)
        payload, _ = make_request(data)
        request_path = write_request(self.tmp.name, payload)
        with self.assertRaises(ValueError):
            release(self.store, load_and_parse(request_path), source)
        self.assertFalse(Path(self.store).exists()
                         and (Path(self.store) / 'state.json').exists())

    def test_empty_controls_withholds_everything_and_is_a_valid_revision(self):
        data = two_analyte_input()
        report, _, _, _, _ = self.submit(data, {'controls': {}})
        self.assertTrue(all(sample['status'] == 'withheld' for sample in report['samples']))
        self.assertTrue(all('missing_left_control' in sample['reasons']
                            and 'missing_right_control' in sample['reasons']
                            for sample in report['samples']))
        self.assertEqual(report['controls'], [])
        self.assertEqual(report['corrections'], [])
        self.assertEqual(report['revision'], 1)

    def test_all_withheld_followed_by_all_released_emits_only_added(self):
        data = two_analyte_input()
        full_payload, _ = make_request(data)
        source = write_input(self.tmp.name, data, name='input2.json')
        # Revision 1 keeps the full control registration but disables both
        # calibrations: every sample is withheld, and nothing is issued.
        first = copy.deepcopy(full_payload)
        first['excluded_standards'] = [['s0', 'A'], ['s2', 'A'],
                                       ['s0', 'B'], ['s2', 'B']]
        report1 = json.loads(release(self.store, parse_request(first), source)[0])
        self.assertTrue(all(sample['status'] == 'withheld' for sample in report1['samples']))
        self.assertEqual(report1['corrections'], [])
        # Revision 2 lifts the exclusions (a review-list change, not a lock
        # change); every now-released sample must appear as an addition.
        follow_up = copy.deepcopy(full_payload)
        follow_up.update(request_id='r2', expected_revision=1)
        report2 = json.loads(release(self.store, parse_request(follow_up), source)[0])
        self.assertEqual([c['action'] for c in report2['corrections']],
                         ['added'] * len(report2['samples']))
        self.assertIsNone(report2['corrections'][0]['previous'])

    def test_request_id_is_globally_unique_across_batches(self):
        data = two_analyte_input()
        _, _, payload, _, source = self.submit(data)
        other = copy.deepcopy(payload)
        other['batch_id'] = 'other-batch'
        other['expected_revision'] = 0
        with self.assertRaisesRegex(ValueError, 'different identity'):
            release(self.store, parse_request(other), source)
        state = json.loads((Path(self.store) / 'state.json').read_text())
        self.assertNotIn('other-batch', state['batches'])

    def test_review_lists_are_full_sets_not_patches_between_revisions(self):
        data = two_analyte_input()
        _, _, payload, _, source = self.submit(
            data, {'invalid_controls': [['c1', 'A']]})
        # The next revision drops the marker; the control must pass again and
        # the withheld sample be re-added.
        follow_up = copy.deepcopy(payload)
        follow_up.update(request_id='r2', expected_revision=1, invalid_controls=[])
        report = json.loads(release(self.store, parse_request(follow_up), source)[0])
        control = next(c for c in report['controls']
                       if c['run_id'] == 'c1' and c['analyte_id'] == 'A')
        self.assertFalse(control['manually_invalid'])
        self.assertTrue(control['passed'])
        actions = {(c['run_id'], c['analyte_id']): c['action']
                   for c in report['corrections']}
        self.assertEqual(actions[('u1', 'A')], 'added')


    def test_history_requires_existing_batch_and_positive_revision(self):
        data = two_analyte_input()
        source = write_input(self.tmp.name, data)
        payload, _ = make_request(data)
        release(self.store, parse_request(copy.deepcopy(payload)), source)
        with self.assertRaisesRegex(ValueError, 'unknown batch'):
            release_show(self.store, 'ghost', 1)
        for bad_revision in (0, -1, 2):
            with self.assertRaisesRegex(ValueError, 'revision'):
                release_show(self.store, 'b1', bad_revision)
        # Reading history never needs the INPUT: remove it and query again.
        Path(source).unlink()
        shown = release_show(self.store, 'b1', 1)
        self.assertTrue(shown)


class RequestValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def base(self, **overrides):
        data = two_analyte_input()
        payload, _ = make_request(data)
        payload.update(overrides)
        return data, payload

    def test_identity_normalization_ignores_order_and_spacing_but_keeps_values(self):
        _, payload = self.base()
        reordered = {
            'invalid_controls': [],
            'excluded_standards': [],
            'controls': {
                'c3': {'B': {'tolerance': '0.0', 'target': '2.0'},
                       'A': {'target': '2.00', 'tolerance': '0'}},
                'c2': {'B': {'tolerance': '0.0', 'target': '2.0'},
                       'A': {'target': '2.00', 'tolerance': '0'}},
                'c1': {'B': {'tolerance': '0.30', 'target': '0.10'},
                       'A': {'target': '0.100', 'tolerance': '0.300'}}},
            'source_sha256': payload['source_sha256'],
            'expected_revision': 0,
            'request_id': 'r1',
            'batch_id': 'b1',
        }
        self.assertEqual(canonical_identity(parse_request(payload)),
                         canonical_identity(parse_request(reordered)))
        changed = json.loads(json.dumps(reordered))
        changed['controls']['c1']['A']['tolerance'] = '0.31'
        self.assertNotEqual(canonical_identity(parse_request(payload)),
                            canonical_identity(parse_request(changed)))

    def test_strict_field_and_value_rules(self):
        _, payload = self.base()
        for mutated in (
            dict(payload, expected_revision=True),
            dict(payload, expected_revision=-1),
            dict(payload, source_sha256='g' * 64),
            dict(payload, source_sha256='f' * 63),
            dict(payload, batch_id='bad id'),
            dict(payload, extra=1),
        ):
            with self.assertRaises(ValueError):
                parse_request(mutated)
        missing = dict(payload)
        del missing['controls']
        with self.assertRaises(ValueError):
            parse_request(missing)
        bad_controls = json.loads(json.dumps(payload))
        bad_controls['controls']['c1']['A']['target'] = '-0.1'
        with self.assertRaises(ValueError):
            parse_request(bad_controls)
        bad_controls = json.loads(json.dumps(payload))
        bad_controls['controls']['c1']['A'] = {'target': '0.1'}
        with self.assertRaises(ValueError):
            parse_request(bad_controls)
        bad_pairs = dict(payload, excluded_standards=[['s1', 'A'], ['s1', 'A']])
        with self.assertRaises(ValueError):
            parse_request(bad_pairs)
        empty_control = dict(payload, controls={'c1': {}})
        with self.assertRaises(ValueError):
            parse_request(empty_control)

    def test_duplicate_json_keys_and_oversize_request_rejected(self):
        raw = b'{"batch_id":"b1","batch_id":"b2"}'
        path = Path(self.tmp.name) / 'dup.json'
        path.write_bytes(raw)
        with self.assertRaises(ValueError):
            load_and_parse(str(path))
        big = Path(self.tmp.name) / 'big.json'
        big.write_bytes(b'{"x":' + b'1' * 8000000 + b'}')
        with self.assertRaises(ValueError):
            load_and_parse(str(big))

    def test_reference_validation_against_input(self):
        data, payload = self.base()
        store = str(Path(self.tmp.name) / 'store')
        source = write_input(self.tmp.name, data)
        # Control must reference an unknown run.
        bad = dict(payload, controls={'s1': {'A': {'target': '1', 'tolerance': '0'}}})
        with self.assertRaisesRegex(ValueError, 'existing unknown run'):
            release(store, parse_request(bad), source)
        # Excluded standard must reference a standard.
        bad = dict(payload, excluded_standards=[['u1', 'A']])
        with self.assertRaisesRegex(ValueError, 'reference a standard'):
            release(store, parse_request(bad), source)
        # Invalid control pair must be a registered control observation.
        bad = dict(payload, invalid_controls=[['u2', 'A']])
        with self.assertRaisesRegex(ValueError, 'registered control'):
            release(store, parse_request(bad), source)
        # Unknown analyte reference.
        bad = dict(payload, excluded_standards=[['s1', 'Z']])
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            release(store, parse_request(bad), source)

    def test_batch_lock_rejects_source_or_registration_changes(self):
        data, payload = self.base()
        store = str(Path(self.tmp.name) / 'store')
        source = write_input(self.tmp.name, data)
        release(store, parse_request(copy.deepcopy(payload)), source)
        changed_target = copy.deepcopy(payload)
        changed_target.update(request_id='r2', expected_revision=1)
        changed_target['controls']['c1']['A']['target'] = '0.2'
        with self.assertRaisesRegex(ValueError, 'locked for this batch'):
            release(store, parse_request(changed_target), source)
        dropped = copy.deepcopy(payload)
        dropped.update(request_id='r3', expected_revision=1,
                       controls={'c2': payload['controls']['c2']})
        with self.assertRaisesRegex(ValueError, 'locked for this batch'):
            release(store, parse_request(dropped), source)
        changed_fp = copy.deepcopy(payload)
        changed_fp.update(request_id='r4', expected_revision=1,
                          source_sha256='f' * 64)
        with self.assertRaises(ValueError):
            release(store, parse_request(changed_fp), source)
        # A legitimate revision that only flips review markers is accepted.
        follow_up = copy.deepcopy(payload)
        follow_up.update(request_id='r5', expected_revision=1,
                         invalid_controls=[['c1', 'A']])
        _, revision, retried = release(store, parse_request(follow_up), source)
        self.assertEqual((revision, retried), (2, False))


class HistoryAndCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = str(Path(self.tmp.name) / 'store')
        data = two_analyte_input()
        self.data = data
        self.source = write_input(self.tmp.name, data)
        payload, _ = make_request(data)
        self.raw_payload = payload
        self.request_path = write_request(self.tmp.name, payload)
        self.request = load_and_parse(self.request_path)

    def test_release_cli_success_and_show_errors(self):
        def invoke(argv):
            buffer = io.BytesIO()

            class Out:
                def flush(self):
                    pass
            Out.buffer = buffer
            with redirect_stdout(Out()), redirect_stderr(io.StringIO()) as err:
                code = cli.main(argv)
            return code, buffer.getvalue(), err.getvalue()

        code, first, _ = invoke(['release', '--store', self.store,
                                 '--request', self.request_path, '--source', self.source])
        self.assertEqual(code, 0)
        self.assertTrue(first.endswith(b'\n'))
        json.loads(first.decode('utf-8'))
        # Retry without --source prints identical bytes.
        code, retried_out, _ = invoke(['release', '--store', self.store,
                                       '--request', self.request_path])
        self.assertEqual(code, 0)
        self.assertEqual(retried_out, first)
        code, shown, _ = invoke(['release-show', '--store', self.store,
                                 '--batch', 'b1', '--revision', '1'])
        self.assertEqual(code, 0)
        self.assertEqual(shown, first)
        code, _, error_text = invoke(['release-show', '--store', self.store,
                                      '--batch', 'b1', '--revision', '9'])
        self.assertEqual(code, 2)
        self.assertIn('revision', error_text)
        code, _, error_text = invoke(['release-show', '--store', self.store,
                                      '--batch', 'ghost', '--revision', '1'])
        self.assertEqual(code, 2)
        self.assertIn('unknown batch', error_text)

    def test_legacy_cli_contract_overwrite_and_symlink_refusal(self):
        output = str(Path(self.tmp.name) / 'legacy.json')
        err = io.StringIO()
        with redirect_stderr(err):
            code = cli.main(['--source', self.source, '--output', output])
        self.assertEqual(code, 0)
        first = Path(output).read_bytes()
        # Existing file is never overwritten.
        with redirect_stderr(err):
            code = cli.main(['--source', self.source, '--output', output])
        self.assertEqual(code, 2)
        self.assertEqual(Path(output).read_bytes(), first)
        # A symlink target is refused as well.
        link = str(Path(self.tmp.name) / 'link.json')
        os.symlink(output, link)
        with redirect_stderr(err):
            code = cli.main(['--source', self.source, '--output', link])
        self.assertEqual(code, 2)
        self.assertEqual(os.readlink(link), output)

    def test_cli_conflict_exit_code_and_no_success_payload(self):
        captured = io.BytesIO()

        class Out:
            buffer = captured

            @staticmethod
            def flush():
                pass

        with redirect_stdout(Out()), redirect_stderr(io.StringIO()):
            code = cli.main(['release', '--store', self.store,
                             '--request', self.request_path, '--source', self.source])
        self.assertEqual(code, 0)
        first = captured.getvalue()
        conflict = copy.deepcopy(self.raw_payload)
        conflict['expected_revision'] = 1
        conflict_path = write_request(
            self.tmp.name, conflict, 'conflict.json')
        with redirect_stdout(Out()), redirect_stderr(io.StringIO()) as err:
            code = cli.main(['release', '--store', self.store,
                             '--request', conflict_path, '--source', self.source])
        self.assertEqual(code, 2)
        # The conflict emits no success report and cannot append a second one.
        self.assertEqual(captured.getvalue(), first)
        self.assertIn('different identity', err.getvalue())


if __name__ == '__main__':
    unittest.main()
