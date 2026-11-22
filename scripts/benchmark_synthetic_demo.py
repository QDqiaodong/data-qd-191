#!/usr/bin/env python3
"""Generate the offline synthetic leave-group-out benchmark case and verify it.

Default full case (the contract case):

* 200 standard runs paired into 100 distinct-origin groups of adjacent runs;
  only the first run of each pair is scored;
* each run has 2,000 samples at integer times 0..1999; run i (1-based) holds
  signal i at every even time 2..1996 and zero elsewhere;
* 30 analytes with windows [j/100, 1999-j/100] for j = 0..29 and reference
  concentration i for every analyte;
* a compact JSON source still inside the v1 byte limits.

The benchmark *process* is launched with RLIMIT_CPU=180 seconds (the data
generation and the independent verification below are outside that cap).  The
report is then checked fold by fold, not just by exit code: training
concentration ranges, the shared slope (998 for the full case), and zero
error for every group and analyte.  Only standard-library code is used and
nothing here models real substances, instruments or experiments.
"""
import argparse
import json
import resource
import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC = PACKAGE_ROOT / 'src'

FULL_RUNS = 200
FULL_POINTS = 2000
FULL_ANALYTES = 30
FULL_CPU = 180


def spike_bounds(points):
    """Interior even-spike range; the full grid gives 2..1996 (998 spikes)."""
    last = points - 4
    return 2, last


def build_source(runs, points, analytes, workdir):
    """Build the compact v1 source and its benchmark plan."""
    times = list(range(points))
    spike_first, spike_last = spike_bounds(points)
    spike_times = {t for t in times if t % 2 == 0
                   and spike_first <= t <= spike_last}
    source_runs = []
    origins = {}
    score_ids = []
    for i in range(1, runs + 1):
        run_id = f'R{i:03d}'
        samples = [[str(t), str(i) if t in spike_times else '0']
                   for t in times]
        source_runs.append({
            'id': run_id,
            'kind': 'standard',
            'concentrations': {f'A{j:02d}': str(i)
                               for j in range(analytes)},
            'samples': samples,
        })
        # Adjacent pairs share an origin: (R1,R2), (R3,R4), ...
        origins[run_id] = f'g{(i - 1) // 2:03d}'
        if i % 2 == 1:
            score_ids.append(run_id)
    source = {
        'analytes': [
            {'id': f'A{j:02d}',
             'window': ['0' if j == 0 else f'{j / 100:.2f}',
                        str(points - 1) if j == 0
                        else f'{points - 1 - j / 100:.2f}']}
            for j in range(analytes)],
        'runs': source_runs,
    }
    plan = {'origins': origins, 'score_ids': score_ids}
    source_path = workdir / 'source.json'
    plan_path = workdir / 'plan.json'
    source_path.write_text(json.dumps(source, separators=(',', ':')),
                           encoding='utf-8')
    plan_path.write_text(json.dumps(plan, separators=(',', ':')),
                         encoding='utf-8')
    if source_path.stat().st_size > 8_000_000:
        raise SystemExit(f'source {source_path.stat().st_size} bytes exceeds '
                         'the v1 8,000,000 byte limit')
    if plan_path.stat().st_size > 1_000_000:
        raise SystemExit('plan exceeds 1,000,000 bytes')
    return source_path, plan_path, len(spike_times)


def verify_report(report_path, runs, points, analytes, expected_slope):
    report = json.loads(report_path.read_text(encoding='utf-8'))
    assert report['schema_version'] == 1
    folds = report['folds']
    assert len(folds) == runs // 2
    assert len(report['analyte_ids']) == analytes
    seen_held = set()
    for fold_index, fold in enumerate(folds, start=1):
        held = fold['held_out_run_ids']
        assert len(held) == 2
        assert len(fold['scored_run_ids']) == 1
        assert len(fold['training_run_ids']) == runs - 2
        seen_held.update(held)
        # Training concentration closed interval: the pair held out is the
        # only missing pair, so check boundaries explicitly.
        held_numbers = sorted(int(rid[1:]) for rid in held)
        train_numbers = [i for i in range(1, runs + 1)
                         if i not in held_numbers]
        for analyte in fold['analytes']:
            assert analyte['available'], analyte.get('unavailable_reason')
            assert analyte['slope'] == str(expected_slope)
            assert analyte['intercept'] == '0'
            assert analyte['min_concentration'] == str(min(train_numbers))
            assert analyte['max_concentration'] == str(max(train_numbers))
            for row in analyte['scores']:
                assert row['prediction'] == row['reference_concentration']
                assert row['error'] == '0'
                assert row['negative'] is False
                # Only folds holding run 1 (below min) or run runs (above max)
                # extrapolate; exact endpoint concentration is inside range.
                expect_extra = held_numbers[0] == 1 or held_numbers[1] == runs
                assert row['extrapolated'] == expect_extra
    assert seen_held == {f'R{i:03d}' for i in range(1, runs + 1)}
    for summary in report['summaries']:
        assert summary['requested_groups'] == runs // 2
        assert summary['requested_records'] == runs // 2
        assert summary['successful_groups'] == runs // 2
        assert summary['successful_records'] == runs // 2
        assert summary['total_mean_absolute_error'] == '0'
        for group in summary['group_mean_absolute_errors']:
            assert group['mean_absolute_error'] == '0'
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--runs', type=int, default=FULL_RUNS)
    parser.add_argument('--points', type=int, default=FULL_POINTS)
    parser.add_argument('--analytes', type=int, default=FULL_ANALYTES)
    parser.add_argument('--cpu', type=int, default=FULL_CPU)
    args = parser.parse_args(argv)
    if args.runs % 2 or not 2 <= args.runs <= 200:
        raise SystemExit('--runs must be an even number in 2..200')
    if not 1 <= args.analytes <= 30 or not 2 <= args.points <= 2000:
        raise SystemExit('--analytes must be 1..30 and --points 2..2000')

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    source_path, plan_path, spike_count = build_source(
        args.runs, args.points, args.analytes, workdir)
    report_path = workdir / 'benchmark-report.json'
    if report_path.exists() or report_path.is_symlink():
        raise SystemExit(f'output {report_path} already exists; refusing to overwrite')

    # The CPU cap applies to the benchmark process only.  On the full grid the
    # expected slope equals the number of interior even spikes (998).
    runner = (
        'import sys, resource;'
        f'resource.setrlimit(resource.RLIMIT_CPU, ({args.cpu}, {args.cpu}));'
        f'sys.path.insert(0, {str(SRC)!r});'
        'from peak_quant.cli import main;'
        f'sys.exit(main(["benchmark", "--source", {str(source_path)!r},'
        f' "--plan", {str(plan_path)!r}, "--output", {str(report_path)!r}]))')
    child = subprocess.run([sys.executable, '-I', '-c', runner],
                           capture_output=True, text=True, check=False)
    if child.returncode != 0:
        sys.stderr.write(child.stderr)
        raise SystemExit(child.returncode)
    if not report_path.exists():
        raise SystemExit('benchmark reported success but wrote no report')

    # The full-contract slope is the even-spike count on the integer grid;
    # scaled grids keep the same count formula.
    spike_first, spike_last = spike_bounds(args.points)
    expected_slope = sum(1 for t in range(args.points)
                         if t % 2 == 0 and spike_first <= t <= spike_last)
    report = verify_report(report_path, args.runs, args.points,
                           args.analytes, expected_slope)
    print(f'generated {source_path.stat().st_size}-byte source, '
          f'{plan_path.stat().st_size}-byte plan')
    print(f'verified {len(report["folds"])} folds, {args.analytes} analytes, '
          f'slope {expected_slope}, every group/analyte error 0')
    print('verified point-by-point fold ranges and zero-error predictions')


if __name__ == '__main__':
    main()
