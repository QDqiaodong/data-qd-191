#!/usr/bin/env python3
"""Generate and verify the month-end 12,000-batch reconciliation case.

The contract case uses one shared synthetic v1 source for every batch (each
batch still keeps its own independent zero point):

* the source has "area = concentration" standards and two distinct unknown
  runs with local concentrations 0 and 1;
* 12,000 batches b00000..b11999 are anchored at b00000;
* panels p00000..p11998 chain the batches in order with zero readings on both
  sides, and the closing ring panel p11999 runs b11999 -> b00000 with readings
  1 and 0, so exactly one panel is misfiled and every panel is a repair
  candidate;
* the single query subtracts b06000's zero reading from b05999's unit reading.

Only the reconciliation process receives an RLIMIT_CPU budget (90 seconds by
default).  Fixture generation and the independent verification below run
outside that cap, and none of the computation is moved into the generator or
an uncapped child.  Standard library only; no real samples or instruments.
"""
import argparse
import json
import resource
import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC = PACKAGE_ROOT / 'src'

BATCHES = 12_000
CPU_BUDGET = 90


def build_fixtures(workdir):
    workdir.mkdir(parents=True, exist_ok=True)
    sources = workdir / 'sources'
    sources.mkdir(exist_ok=True)
    # Triangle on window [0,2] with height 2 has area 2 per unit; standards at
    # concentrations 0 and 1 therefore give slope 2, intercept 0, i.e. the
    # back-inferred concentration equals the unknown's designed value.
    source = {
        'analytes': [{'id': 'X', 'window': ['0', '2']}],
        'runs': [
            {'id': 's0', 'kind': 'standard',
             'concentrations': {'X': '0'},
             'samples': [['0', '0'], ['1', '0'], ['2', '0']]},
            {'id': 's1', 'kind': 'standard',
             'concentrations': {'X': '1'},
             'samples': [['0', '0'], ['1', '2'], ['2', '0']]},
            {'id': 'u0', 'kind': 'unknown',
             'samples': [['0', '0'], ['1', '0'], ['2', '0']]},
            {'id': 'u1', 'kind': 'unknown',
             'samples': [['0', '0'], ['1', '2'], ['2', '0']]},
        ],
    }
    source_path = sources / 'base.json'
    source_path.write_text(json.dumps(source, separators=(',', ':')),
                           encoding='utf-8')

    batch_ids = [f'b{i:05d}' for i in range(BATCHES)]
    panels = [
        {'id': f'p{i:05d}',
         'left': [f'b{i:05d}', 'u0'],
         'right': [f'b{i + 1:05d}', 'u0']}
        for i in range(BATCHES - 1)
    ]
    panels.append({'id': f'p{BATCHES - 1:05d}',
                   'left': [f'b{BATCHES - 1:05d}', 'u1'],
                   'right': ['b00000', 'u0']})
    plan = {
        'batches': {batch_id: 'base' for batch_id in batch_ids},
        'anchor': 'b00000',
        'analytes': ['X'],
        'panels': panels,
        'queries': [
            {'id': 'q1', 'analyte_id': 'X',
             'left': ['b05999', 'u1'], 'right': ['b06000', 'u0']},
        ],
    }
    plan_path = workdir / 'plan.json'
    plan_path.write_text(json.dumps(plan, separators=(',', ':')),
                         encoding='utf-8')
    if plan_path.stat().st_size > 8_000_000:
        raise SystemExit('plan exceeds the 8,000,000 byte plan limit')
    return sources, plan_path


def verify(report_path):
    report = json.loads(report_path.read_text(encoding='utf-8'))
    assert report['format_version'] == 1
    assert report['anchor'] == 'b00000'
    assert report['status'] == 'single_panel_repairable'
    assert report['unreconcileable_reason'] is None
    expected_panels = [f'p{i:05d}' for i in range(BATCHES)]
    assert report['removable_panel_ids'] == expected_panels, \
        'full candidate panel list mismatch'
    query, = report['queries']
    assert query['id'] == 'q1'
    assert query['left'] == ['b05999', 'u1']
    assert query['right'] == ['b06000', 'u0']
    assert query['left_local_concentration'] == '1'
    assert query['right_local_concentration'] == '0'
    assert query['min_net_difference'] == '1'
    assert query['max_net_difference'] == '2'
    assert query['min_witness_panel'] == 'p00000'
    assert query['max_witness_panel'] == 'p05999'
    assert query['bookable_net_difference'] is None
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--cpu', type=int, default=CPU_BUDGET)
    args = parser.parse_args(argv)
    workdir = Path(args.workdir)
    sources, plan_path = build_fixtures(workdir)
    report_path = workdir / 'reconcile-report.json'
    if report_path.exists() or report_path.is_symlink():
        raise SystemExit(f'output {report_path} already exists; refusing to overwrite')

    # The CPU cap is imposed inside the reconciliation process itself; neither
    # this generator nor the verification below is counted against it.
    runner = (
        'import sys, resource;'
        f'resource.setrlimit(resource.RLIMIT_CPU, ({args.cpu}, {args.cpu}));'
        f'sys.path.insert(0, {str(SRC)!r});'
        'from peak_quant.cli import main;'
        f'sys.exit(main(["reconcile", "--sources", {str(sources)!r},'
        f' "--plan", {str(plan_path)!r}, "--output", {str(report_path)!r}]))')
    child = subprocess.run([sys.executable, '-I', '-c', runner],
                           capture_output=True, text=True, check=False)
    if child.returncode != 0:
        sys.stderr.write(child.stderr)
        raise SystemExit(child.returncode)
    if not report_path.exists():
        raise SystemExit('reconcile reported success but wrote no report')

    report = verify(report_path)
    print(f'plan {plan_path.stat().st_size} bytes, '
          f'shared source {list(sources.iterdir())[0].stat().st_size} bytes')
    print(f'verified all {len(report["removable_panel_ids"])} candidate panels; '
          'query interval [1, 2], witnesses p00000/p05999, nothing bookable')


if __name__ == '__main__':
    main()
