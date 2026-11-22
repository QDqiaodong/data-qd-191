#!/usr/bin/env python3
"""Runnable synthetic pipeline for ``peak-quant assemble``.

It generates a plan and a fragment stream entirely from made-up piecewise
linear curves (no real substances, instruments, experiments or network),
feeds the fragments to a separate consumer process in round-robin order so no
run is complete before the final round, caps only the consumer with
RLIMIT_AS, and then verifies every run of the assembled v1 source against an
independently computed expectation point by point.

Usage:
    python scripts/assemble_pipeline_demo.py --workdir run/pipeline

The default scale is the required 200 runs x 8 fragments x 1024 samples.
Only the Python 3.11+ standard library is used.
"""
import argparse
import json
import os
import resource
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

RUN_COUNT = 200
FRAGMENT_COUNT = 8
SAMPLE_COUNT = 1024
MEM_MIB = 192
SPAN_LO, SPAN_HI = Fraction(0), Fraction(8)


def decimal_text(fraction):
    """Exact decimal rendering; every generated value divides 10**6."""
    value = Fraction(fraction) * 10 ** 6
    if value.denominator != 1:
        raise AssertionError(f'generated value {fraction} is not a 6-digit decimal')
    whole, frac = divmod(abs(value.numerator), 10 ** 6)
    sign = '-' if value.numerator < 0 else ''
    if frac == 0:
        return f'{sign}{whole}'
    return f'{sign}{whole}.{str(frac).zfill(6).rstrip("0")}'


def master_curve(run_index):
    """2, 3 or 4 exact breakpoints; the assembled run must keep only these.

    Every segment width divides 10**6 (1, 2 or 4) and every sample time is an
    integer thousandth, so all made-up values are bounded v1 decimals even
    after exact linear interpolation.
    """
    kind = run_index % 3
    if kind == 0:
        return [(Fraction(0), Fraction(run_index % 7)),
                (Fraction(4), Fraction(1 + run_index % 6)),
                (Fraction(8), Fraction(2 + run_index % 5))]
    if kind == 1:
        return [(Fraction(0), Fraction(0)),
                (Fraction(4), Fraction(1 + run_index % 6)),
                (Fraction(8), Fraction(0))]
    # Widths 4, 2, 2: all divide 10**6.
    return [(Fraction(0), Fraction(0)),
            (Fraction(4), Fraction(1 + run_index % 4)),
            (Fraction(6), Fraction(2 + run_index % 5)),
            (Fraction(8), Fraction(0))]


def curve_at(curve, t):
    for (a, ya), (b, yb) in zip(curve, curve[1:]):
        if a <= t <= b:
            return ya + (yb - ya) * (t - a) / (b - a)
    raise AssertionError('time outside master curve')


def expected_samples(run_index):
    """Independent oracle: span ends plus true interior slope changes only."""
    curve = master_curve(run_index)
    breakpoints = [(SPAN_LO, curve_at(curve, SPAN_LO))]
    for position, (t, y) in enumerate(curve):
        if not (SPAN_LO < t < SPAN_HI):
            continue
        slope_before = (curve[position][1] - curve[position - 1][1]) / \
                       (curve[position][0] - curve[position - 1][0])
        slope_after = (curve[position + 1][1] - curve[position][1]) / \
                      (curve[position + 1][0] - curve[position][0])
        if slope_before != slope_after:
            breakpoints.append((t, y))
    breakpoints.append((SPAN_HI, curve_at(curve, SPAN_HI)))
    return [[decimal_text(t), decimal_text(y)] for t, y in breakpoints]


def build_plan(run_count):
    return {
        'analytes': [{'id': 'A', 'window': ['1', '7']}],
        'runs': [
            {
                'id': f'r{index:03d}',
                'kind': 'unknown',
                'span': ['0', '8'],
                'fragments': [
                    {'id': f'r{index:03d}-f{k}',
                     'anchors': (
                         [['0', '0'], ['2', '1']] if k == 0
                         else [['0', '7'], ['2', '8']] if k == FRAGMENT_COUNT - 1
                         else [['0', str(k)], ['2', str(k + 2)]])}
                    for k in range(FRAGMENT_COUNT)
                ],
            }
            for index in range(run_count)
        ],
    }


def fragment_line(run_index, fragment_index):
    """One JSON Lines record; local grids stay strictly inside the anchors."""
    curve = master_curve(run_index)
    samples = []
    # Boundary anchors use scale 1/2 and include the anchor local time 2 as a
    # sample, so the mapped domains start exactly at 0 and end exactly at 8.
    # Boundary grids use even thousandths (mapping to integer thousandths);
    # interior anchors use scale 1 with plain thousandth grids. Every mapped
    # time is therefore an integer-thousandth decimal.
    if fragment_index in (0, FRAGMENT_COUNT - 1):
        def local_at(i):
            if i == SAMPLE_COUNT - 1:
                return Fraction(2)
            # Even numerators: local/2 is an integer thousandth, and the
            # final sample is the anchor local 2 (strictly increasing).
            return Fraction(2 * i, 2000)
        def absolute_at(local):
            return local / 2
    else:
        def local_at(i):
            return Fraction(i, 1000)            # 0 .. 1.023
        def absolute_at(local):
            return local
    for i in range(SAMPLE_COUNT):
        local = local_at(i)
        if fragment_index == 0:
            absolute = absolute_at(local)                 # 0 .. 1 (exact end)
        elif fragment_index == FRAGMENT_COUNT - 1:
            absolute = Fraction(7) + absolute_at(local)   # 7 .. 8 (exact end)
        else:
            absolute = Fraction(fragment_index) + local
        samples.append([decimal_text(local), decimal_text(curve_at(curve, absolute))])
    return json.dumps({'fragment_id': f'r{run_index:03d}-f{fragment_index}',
                       'samples': samples}, ensure_ascii=False)


def limit_consumer(mem_mib):
    limit = mem_mib * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--runs', type=int, default=RUN_COUNT)
    parser.add_argument('--mem-mib', type=int, default=MEM_MIB)
    args = parser.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    plan_path = workdir / 'plan.json'
    output_path = workdir / 'assembled.json'
    if output_path.exists():
        output_path.unlink()
    plan_path.write_text(json.dumps(build_plan(args.runs), ensure_ascii=False),
                         encoding='utf-8')

    project_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    # When run from a plain checkout (no installed package), make src importable.
    env['PYTHONPATH'] = str(project_root / 'src') + (
        os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    consumer = subprocess.Popen(
        [sys.executable, '-m', 'peak_quant.cli', 'assemble',
         '--plan', str(plan_path), '--output', str(output_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=project_root, env=env,
        preexec_fn=lambda: limit_consumer(args.mem_mib))
    total_bytes = 0
    # Round-robin by fragment index: a run only completes in the last round.
    try:
        for fragment_index in range(FRAGMENT_COUNT):
            for run_index in range(args.runs):
                line = fragment_line(run_index, fragment_index) + '\n'
                encoded = line.encode('utf-8')
                assert len(encoded) <= 160_000
                consumer.stdin.write(encoded)
                total_bytes += len(encoded)
            consumer.stdin.flush()
        consumer.stdin.close()
    except BrokenPipeError:
        pass
    stderr = consumer.stderr.read().decode('utf-8')
    stdout = consumer.stdout.read().decode('utf-8')
    code = consumer.wait()
    if code != 0:
        print(f'consumer exited {code}\n{stderr}', file=sys.stderr)
        return 1

    source = json.loads(output_path.read_text(encoding='utf-8'))
    if len(source['runs']) != args.runs:
        print(f'expected {args.runs} runs, got {len(source["runs"])}', file=sys.stderr)
        return 1
    for run_index, run in enumerate(source['runs']):
        expect = expected_samples(run_index)
        if not (2 <= len(run['samples']) <= 4):
            print(f'{run["id"]}: sample count {len(run["samples"])} outside 2..4',
                  file=sys.stderr)
            return 1
        if run['samples'] != expect:
            print(f'{run["id"]}: mismatch\n  got {run["samples"]}\n  want {expect}',
                  file=sys.stderr)
            return 1
    print(f'PASS: {args.runs} runs x {FRAGMENT_COUNT} fragments x {SAMPLE_COUNT} '
          f'samples; stream {total_bytes} bytes; consumer RLIMIT_AS={args.mem_mib} MiB')
    print(f'      every run verified point-by-point against the oracle; stdout: {stdout.strip()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
