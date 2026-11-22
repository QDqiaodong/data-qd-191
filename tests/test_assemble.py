"""Acceptance and contract tests for ``peak-quant assemble``.

The synthetic cases mirror the worked example in docs/assemble-contract.md:
exact affine anchor mapping, overlap agreement including single-point
contacts, exact Fraction merge/collinear removal and v1-decimal checks only
for final output points.  A separate subprocess test runs the required
round-robin pipeline with the consumer capped by RLIMIT_AS.
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
from peak_quant.assemble import (LINE_BYTES_LIMIT, STREAM_BYTES_LIMIT,
                                 v1_decimal)
from peak_quant.service import process

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'assemble'


def base_plan():
    return {
        'analytes': [{'id': 'A', 'window': ['0', '2']}],
        'runs': [
            {'id': 's0', 'kind': 'standard', 'concentrations': {'A': '0'},
             'span': ['0', '2'],
             'fragments': [{'id': 'f-s0', 'anchors': [['0', '0'], ['2', '2']]}]},
            {'id': 's2', 'kind': 'standard', 'concentrations': {'A': '2'},
             'span': ['0', '2'],
             'fragments': [{'id': 'f-s2', 'anchors': [['0', '0'], ['2', '2']]}]},
            {'id': 'u1', 'kind': 'unknown', 'span': ['0', '2'], 'fragments': [
                {'id': 'f-left', 'anchors': [['0', '0'], ['3', '1']]},
                {'id': 'f-right', 'anchors': [['0', '1'], ['3', '2']]}]},
        ],
    }


def base_lines():
    return {
        'f-s0': [['0', '0'], ['1', '0'], ['2', '0']],
        'f-s2': [['0', '0'], ['1', '6'], ['2', '0']],
        'f-left': [['0', '0'], ['1', '1'], ['3', '3']],
        'f-right': [['0', '3'], ['1', '2'], ['2', '1'], ['3', '0']],
    }


def encode_stream(lines):
    return ''.join(json.dumps({'fragment_id': fid, 'samples': rows}) + '\n'
                   for fid, rows in lines.items())


def invoke(plan, stream_text, output_name='out.json'):
    class Out:
        buffer = io.BytesIO()

        @staticmethod
        def flush():
            return None
    with tempfile.TemporaryDirectory() as directory:
        plan_path = Path(directory) / 'plan.json'
        output_path = Path(directory) / output_name
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
        old_stdin = sys.stdin
        sys.stdin = io.TextIOWrapper(io.BytesIO(stream_text.encode('utf-8')),
                                     encoding='utf-8')
        try:
            with redirect_stdout(Out()), redirect_stderr(io.StringIO()) as err:
                code = cli.main(['assemble', '--plan', str(plan_path),
                                 '--output', str(output_path)])
            stderr = err.getvalue()
        finally:
            sys.stdin = old_stdin
        payload = json.loads(output_path.read_text(encoding='utf-8')) \
            if output_path.exists() else None
        return code, stderr, payload, output_path


class WorkedExampleTests(unittest.TestCase):
    def test_assembled_points_and_legacy_quantification(self):
        code, _err, source, _path = invoke(base_plan(), encode_stream(base_lines()))
        self.assertEqual(code, 0)
        unknown = next(run for run in source['runs'] if run['id'] == 'u1')
        self.assertEqual(unknown['samples'],
                         [['0', '0'], ['1', '3'], ['2', '0']])
        # The delivered source is directly consumable by the v1 command.
        result = process(source)
        self.assertEqual(result['integrations']['u1']['A']['area'], '3')
        self.assertEqual(result['quantified'][0]['concentration'], '1')
        self.assertEqual(result['calibrations']['A']['slope'], '3')

    def test_bend_at_one_third_is_representation_error(self):
        plan = base_plan()
        lines = base_lines()
        lines['f-left'] = [['0', '0'], ['1', '2'], ['3', '3']]
        code, stderr, payload, path = invoke(plan, encode_stream(lines))
        self.assertEqual(code, 2)
        self.assertIn('representation', stderr)
        self.assertIsNone(payload)
        self.assertFalse(path.exists())

    def test_anchor_shift_to_1_1_keeps_samples_but_opens_gap(self):
        plan = base_plan()
        plan['runs'][2]['fragments'][1]['anchors'] = [['0', '1.1'], ['3', '2']]
        code, stderr, payload, path = invoke(plan, encode_stream(base_lines()))
        self.assertEqual(code, 2)
        self.assertIn('coverage', stderr)
        self.assertIsNone(payload)
        self.assertFalse(path.exists())


class OverlapTests(unittest.TestCase):
    def _plan_with_redundant(self):
        plan = base_plan()
        plan['runs'][2]['fragments'].append(
            {'id': 'f-redundant', 'anchors': [['0', '0'], ['1', '1']]})
        return plan

    def test_aligned_redundant_fragment_is_invisible(self):
        lines = base_lines()
        lines['f-redundant'] = [['0', '0'], ['0.5', '1.5'], ['1', '3']]
        code, _err, source, _path = invoke(self._plan_with_redundant(),
                                           encode_stream(lines))
        self.assertEqual(code, 0)
        unknown = next(run for run in source['runs'] if run['id'] == 'u1')
        self.assertEqual(unknown['samples'],
                         [['0', '0'], ['1', '3'], ['2', '0']])

    def test_endpoint_agreement_does_not_hide_midpoint_conflict(self):
        lines = base_lines()
        lines['f-redundant'] = [['0', '0'], ['0.5', '2'], ['1', '3']]
        code, stderr, payload, _path = invoke(self._plan_with_redundant(),
                                              encode_stream(lines))
        self.assertEqual(code, 2)
        self.assertIn('conflict', stderr)
        self.assertIsNone(payload)

    def test_single_point_contact_mismatch_is_conflict(self):
        plan = base_plan()
        lines = base_lines()
        lines['f-right'] = [['0', '3.1'], ['1', '2'], ['2', '1'], ['3', '0']]
        code, stderr, payload, _path = invoke(plan, encode_stream(lines))
        self.assertEqual(code, 2)
        self.assertIn('conflict', stderr)
        self.assertIsNone(payload)


class EndpointAndDeterminismTests(unittest.TestCase):
    def test_span_endpoints_are_interpolated_when_unsampled(self):
        # No sample sits at span ends 1 and 3; both must be exact interp points.
        plan = {'analytes': [{'id': 'A', 'window': ['1', '2']}],
                'runs': [{'id': 'u', 'kind': 'unknown', 'span': ['1', '3'],
                          'fragments': [
                              {'id': 'g-left', 'anchors': [['0', '0.5'], ['2', '2.5']]},
                              {'id': 'g-right', 'anchors': [['0', '2'], ['3', '3.5']]}]}]}
        lines = {
            'g-left': [['0', '0'], ['1', '4'], ['1.5', '4']],
            'g-right': [['0', '4'], ['1', '2'], ['3', '2']],
        }
        code, _err, source, _path = invoke(plan, encode_stream(lines))
        self.assertEqual(code, 0)
        self.assertEqual(source['runs'][0]['samples'],
                         [['1', '2'], ['1.5', '4'], ['2', '4'],
                          ['2.5', '2'], ['3', '2']])

    def test_intermediate_collinear_undecimal_point_is_accepted(self):
        # Two fragments describe one straight line y = 3t. The right
        # fragment starts at the mapped time t = 1/3 (its anchors use valid
        # v1 decimals; 1/3 itself is not representable). That meeting point is
        # collinear and is dropped, so the unrepresentable intermediate never
        # reaches the v1 output checks.
        plan = {'analytes': [{'id': 'A', 'window': ['0', '2']}],
                'runs': [{'id': 'u', 'kind': 'unknown', 'span': ['0', '2'],
                          'fragments': [
                              {'id': 'h-left',
                               'anchors': [['0', '0'], ['3', '1']]},
                              {'id': 'h-right',
                               'anchors': [['0', '0'], ['6', '4']]}]}]}
        lines = {
            # h-right scale = 4/6 = 2/3; local 0.5 -> absolute 1/3, y = 1.
            'h-left': [['0', '0'], ['1', '1'], ['3', '3']],
            'h-right': [['0.5', '1'], ['1.5', '3'], ['3', '6']],
        }
        code, stderr, source, _path = invoke(plan, encode_stream(lines))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(source['runs'][0]['samples'],
                         [['0', '0'], ['2', '6']])

    def test_output_bytes_independent_of_arrival_and_fragment_order(self):
        plan = base_plan()
        lines = base_lines()
        first = invoke(plan, encode_stream(lines))[2]
        reversed_text = encode_stream(dict(reversed(list(lines.items()))))
        second = invoke(plan, reversed_text)[2]
        plan2 = json.loads(json.dumps(plan))
        run = plan2['runs'][2]
        run['fragments'] = list(reversed(run['fragments']))
        third = invoke(plan2, reversed_text)[2]
        canonical = json.dumps(first, sort_keys=True)
        self.assertEqual(canonical, json.dumps(second, sort_keys=True))
        self.assertEqual(canonical, json.dumps(third, sort_keys=True))


class StreamValidationTests(unittest.TestCase):
    def test_duplicate_fragment_at_end_rejected_without_output(self):
        text = encode_stream(base_lines())
        text += json.dumps({'fragment_id': 'f-left',
                            'samples': base_lines()['f-left']}) + '\n'
        code, stderr, payload, path = invoke(base_plan(), text)
        self.assertEqual(code, 2)
        self.assertIn('already delivered', stderr)
        self.assertFalse(path.exists())
        self.assertIsNone(payload)

    def test_legal_prefix_but_undeliverable_batch_leaves_no_target(self):
        partial = dict(list(base_lines().items())[:-1])
        code, stderr, payload, path = invoke(base_plan(), encode_stream(partial))
        self.assertEqual(code, 2)
        self.assertIn('without declared fragment', stderr)
        self.assertFalse(path.exists())
        self.assertIsNone(payload)

    def test_unknown_fragment_blank_line_and_bad_json(self):
        plan = base_plan()
        good = encode_stream(base_lines())
        code, stderr, _payload, path = invoke(
            plan, good + '{"fragment_id":"nope","samples":[["0","0"],["1","1"]]}\n')
        self.assertEqual(code, 2)
        self.assertIn('not declared', stderr)
        self.assertFalse(path.exists())

        code, stderr, _payload, path = invoke(plan, good + '\n')
        self.assertEqual(code, 2)
        self.assertIn('empty', stderr)
        self.assertFalse(path.exists())

        code, stderr, _payload, path = invoke(plan, good + '{not json\n')
        self.assertEqual(code, 2)
        self.assertIn('JSON', stderr)
        self.assertFalse(path.exists())

    def test_duplicate_json_key_unknown_field_and_bad_anchor_rejected(self):
        plan = base_plan()
        # Duplicate key inside a stream record.
        text = encode_stream(base_lines()).rstrip('\n')
        text = text.replace(
            '{"fragment_id": "f-left", "samples":',
            '{"fragment_id": "f-left", "fragment_id": "f-left", "samples":', 1)
        code, stderr, _payload, path = invoke(plan, text + '\n')
        self.assertEqual(code, 2)
        self.assertIn('duplicate JSON key', stderr)
        self.assertFalse(path.exists())

        # Unknown plan field.
        bad_plan = json.loads(json.dumps(plan))
        bad_plan['runs'][0]['extra'] = 1
        code, stderr, _payload, path = invoke(bad_plan, encode_stream(base_lines()))
        self.assertEqual(code, 2)
        self.assertIn('expected fields', stderr)
        self.assertFalse(path.exists())

        # Anchor axes must strictly increase.
        bad_plan = base_plan()
        bad_plan['runs'][2]['fragments'][0]['anchors'] = [['3', '1'], ['0', '0']]
        code, stderr, _payload, path = invoke(bad_plan, encode_stream(base_lines()))
        self.assertEqual(code, 2)
        self.assertIn('anchors', stderr)
        self.assertFalse(path.exists())

    def test_no_trailing_newline_is_accepted(self):
        text = encode_stream(base_lines()).rstrip('\n')
        self.assertFalse(text.endswith('\n'))
        code, _err, source, _path = invoke(base_plan(), text)
        self.assertEqual(code, 0)
        self.assertEqual(len(source['runs']), 3)

    def test_existing_target_and_symlink_are_never_overwritten(self):
        plan = base_plan()
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / 'plan.json'
            plan_path.write_text(json.dumps(plan))
            output_path = Path(directory) / 'out.json'
            output_path.write_text('keeper')
            old_stdin = sys.stdin
            sys.stdin = io.TextIOWrapper(
                io.BytesIO(encode_stream(base_lines()).encode()))
            try:
                with redirect_stderr(io.StringIO()) as err:
                    code = cli.main(['assemble', '--plan', str(plan_path),
                                     '--output', str(output_path)])
            finally:
                sys.stdin = old_stdin
            self.assertEqual(code, 2)
            self.assertEqual(output_path.read_text(), 'keeper')
            self.assertIn('already exists', err.getvalue())

            link_path = Path(directory) / 'link.json'
            os.symlink(output_path, link_path)
            sys.stdin = io.TextIOWrapper(
                io.BytesIO(encode_stream(base_lines()).encode()))
            try:
                with redirect_stderr(io.StringIO()):
                    code = cli.main(['assemble', '--plan', str(plan_path),
                                     '--output', str(link_path)])
            finally:
                sys.stdin = old_stdin
            self.assertEqual(code, 2)
            self.assertEqual(output_path.read_text(), 'keeper')


    def test_sample_cap_is_enforced_on_final_points(self):
        # Two fragments partition the span and agree exactly at the contact
        # t=1999. Each record stays within the 2000-sample stream cap, but the
        # merged curve keeps 2000 interior bends plus both span ends = 2002
        # final points, exceeding the v1 cap without any silent truncation.
        a_points = [[str(i), str(i % 2)] for i in range(2000)]       # 0..1999
        b_points = [['0', '1'], ['2000', '0'], ['2002', '1']]        # 1999..2001
        plan = {'analytes': [{'id': 'A', 'window': ['1', '2']}],
                'runs': [{'id': 'u', 'kind': 'unknown',
                          'span': ['0', '2001'],
                          'fragments': [
                              {'id': 'f-a',
                               'anchors': [['0', '0'], ['2002', '2002']]},
                              {'id': 'f-b',
                               'anchors': [['0', '1999'], ['2002', '2001']]}]}]}
        code, stderr, payload, path = invoke(
            plan, encode_stream({'f-a': a_points, 'f-b': b_points}))
        self.assertEqual(code, 2)
        self.assertIn('representation', stderr)
        self.assertIsNone(payload)
        self.assertFalse(path.exists())


class PlanValidationTests(unittest.TestCase):
    def test_span_must_contain_every_window(self):
        plan = base_plan()
        plan['runs'][2]['span'] = ['0.5', '2']
        code, stderr, _payload, path = invoke(plan, encode_stream(base_lines()))
        self.assertEqual(code, 2)
        self.assertIn('plan', stderr)
        self.assertFalse(path.exists())

    def test_anchor_axes_non_negative_and_strictly_increasing(self):
        plan = base_plan()
        plan['runs'][2]['fragments'][0]['anchors'] = [['0', '0'], ['-3', '1']]
        code, stderr, _payload, path = invoke(plan, encode_stream(base_lines()))
        self.assertEqual(code, 2)
        self.assertIn('non-negative', stderr)
        self.assertFalse(path.exists())

        plan = base_plan()
        plan['runs'][2]['fragments'][0]['anchors'] = [['3', '1'], ['0', '0']]
        code, stderr, _payload, path = invoke(plan, encode_stream(base_lines()))
        self.assertEqual(code, 2)
        self.assertIn('local anchors', stderr)
        self.assertFalse(path.exists())

        plan = base_plan()
        plan['runs'][2]['fragments'][0]['anchors'] = [['0', '1'], ['3', '1']]
        code, stderr, _payload, path = invoke(plan, encode_stream(base_lines()))
        self.assertEqual(code, 2)
        self.assertIn('absolute anchors', stderr)
        self.assertFalse(path.exists())

    def test_duplicate_fragment_id_unknown_kind_and_bad_concentration(self):
        plan = base_plan()
        plan['runs'][0]['fragments'].append(
            {'id': 'f-left', 'anchors': [['0', '0'], ['1', '1']]})
        code, stderr, _payload, path = invoke(plan, encode_stream(base_lines()))
        self.assertEqual(code, 2)
        self.assertIn('duplicate fragment id', stderr)
        self.assertFalse(path.exists())

        plan = base_plan()
        plan['runs'][0]['kind'] = 'control'
        code, stderr, _payload, path = invoke(plan, encode_stream(base_lines()))
        self.assertEqual(code, 2)
        self.assertIn('unknown kind', stderr)
        self.assertFalse(path.exists())

        plan = base_plan()
        plan['runs'][0]['concentrations']['A'] = '-1'
        code, stderr, _payload, path = invoke(plan, encode_stream(base_lines()))
        self.assertEqual(code, 2)
        self.assertIn('negative concentration', stderr)
        self.assertFalse(path.exists())

    def test_sample_count_and_local_range_enforced(self):
        plan = base_plan()
        lines = base_lines()
        lines['f-left'] = [['0', '0']]  # below the two-sample minimum
        code, stderr, _payload, path = invoke(plan, encode_stream(lines))
        self.assertEqual(code, 2)
        self.assertIn('array size', stderr)
        self.assertFalse(path.exists())

        plan = base_plan()
        lines = base_lines()
        lines['f-left'] = [['0', '0'], ['3.1', '3']]  # past local anchor 3
        code, stderr, _payload, path = invoke(plan, encode_stream(lines))
        self.assertEqual(code, 2)
        self.assertIn('outside its local anchors', stderr)
        self.assertFalse(path.exists())


class LimitsAndRepresentationTests(unittest.TestCase):
    def test_v1_decimal_rules(self):
        with self.assertRaises(ValueError):
            v1_decimal(Fraction(1, 3), 'x')
        with self.assertRaises(ValueError):
            v1_decimal(Fraction(1, 10 ** 7), 'x')
        with self.assertRaises(ValueError):
            v1_decimal(Fraction(10 ** 9), 'x')
        self.assertEqual(v1_decimal(Fraction(0), 'x'), '0')
        self.assertEqual(v1_decimal(Fraction('1.5'), 'x'), '1.5')
        self.assertEqual(v1_decimal(Fraction(-1, 4), 'x'), '-0.25')
        self.assertEqual(v1_decimal(Fraction(999_999_999), 'x'), '999999999')

    def test_line_over_160k_rejected_without_target(self):
        # The per-line cap (160,000 bytes) is independent of the 2,000-sample
        # cap: an oversized record is refused on size before parsing its body,
        # and the target is never created.
        plan = {'analytes': [{'id': 'A', 'window': ['0', '2']}],
                'runs': [{'id': 'r', 'kind': 'unknown', 'span': ['0', '2000'],
                          'fragments': [
                              {'id': 'big',
                               'anchors': [['0', '0'], ['2000', '2000']]}]}]}
        padding = '0' * LINE_BYTES_LIMIT
        oversized = ('{"fragment_id": "big", "samples": [["0", "0"], '
                     '["1", "' + padding + '"]]}\n')
        self.assertGreater(len(oversized), LINE_BYTES_LIMIT)
        code, stderr, _payload, path = invoke(plan, oversized)
        self.assertEqual(code, 2)
        self.assertIn('line', stderr)
        self.assertFalse(path.exists())

    def test_stream_cap_constant_exists(self):
        self.assertEqual(STREAM_BYTES_LIMIT, 320_000_000)
        self.assertEqual(LINE_BYTES_LIMIT, 160_000)


class PipelineSubprocessTests(unittest.TestCase):
    """Round-robin delivery with a memory-capped consumer process."""

    def test_200_run_pipeline_under_rlimit(self):
        demo = Path(__file__).resolve().parents[1] / 'scripts' \
            / 'assemble_pipeline_demo.py'
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, '-I', str(demo), '--workdir', directory,
                 '--runs', '12', '--mem-mib', '192'],
                capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn('verified point-by-point', completed.stdout)
            source = json.loads((Path(directory) / 'assembled.json').read_text())
            self.assertEqual(len(source['runs']), 12)
            for run in source['runs']:
                self.assertTrue(2 <= len(run['samples']) <= 4)

    def test_tight_rlimit_fails_consumer_without_delivering(self):
        # A deliberately tight cap exercises the RLIMIT_AS plumbing: the
        # generator keeps running, but the capped consumer must not deliver.
        demo = Path(__file__).resolve().parents[1] / 'scripts' \
            / 'assemble_pipeline_demo.py'
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, '-I', str(demo), '--workdir', directory,
                 '--runs', '8', '--mem-mib', '24'],
                capture_output=True, text=True, check=False)
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse((Path(directory) / 'assembled.json').exists())


if __name__ == '__main__':
    unittest.main()
