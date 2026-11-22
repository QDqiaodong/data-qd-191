"""Offline quality-release workflow: revisioned signing, corrections and local store."""
import hashlib
import json
import os
import re
import tempfile
from fractions import Fraction
from pathlib import Path

from .calibrate import fit
from .checks import number
from .integrate import integrate
from .io import constant, pairs
from .model import validate
from .request import canonical_identity

SCHEMA_VERSION = 2
INPUT_LIMIT = 8000000

# Reason codes (also documented in docs/contract.md).
FIT_INSUFFICIENT = 'insufficient_standards'
FIT_IDENTICAL = 'identical_concentrations'
FIT_NON_POSITIVE = 'non_positive_slope'
MARKED_INVALID = 'marked_invalid'
CALIBRATION_UNAVAILABLE = 'calibration_unavailable'
CONTROL_OUT_OF_RANGE = 'control_estimate_out_of_range'
CONTROL_OUT_OF_TOLERANCE = 'control_out_of_tolerance'
MISSING_LEFT_CONTROL = 'missing_left_control'
MISSING_RIGHT_CONTROL = 'missing_right_control'
LEFT_CONTROL_FAILED = 'left_control_failed'
RIGHT_CONTROL_FAILED = 'right_control_failed'
ESTIMATE_NEGATIVE = 'estimate_negative'
ESTIMATE_OUT_OF_RANGE = 'estimate_out_of_range'

_FIT_REASONS = {
    'at least two standards required per analyte': FIT_INSUFFICIENT,
    'standard concentrations must not all be equal': FIT_IDENTICAL,
    'positive calibration slope required': FIT_NON_POSITIVE,
}
_ID_RE = re.compile(r'[A-Za-z0-9_:-]{1,64}\Z')


def encode_report(report):
    """Canonical UTF-8 bytes for a release report; stable across retries/queries."""
    return (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + '\n').encode('utf-8')


def _validate_references(request, analytes, runs):
    """Cross-check every request reference against a validated v1 INPUT."""
    by_id = {run['id']: run for run in runs}
    analyte_ids = set(analytes)
    for run_id, entry in request['controls'].items():
        run = by_id.get(run_id)
        if run is None or run['kind'] != 'unknown':
            raise ValueError(f'control run {run_id} must reference an existing unknown run')
        for analyte_id in entry:
            if analyte_id not in analyte_ids:
                raise ValueError(f'control analyte {analyte_id} does not exist')
    for run_id, analyte_id in request['excluded_standards']:
        run = by_id.get(run_id)
        if run is None or run['kind'] != 'standard':
            raise ValueError(f'excluded_standards entry {run_id}/{analyte_id} must reference a standard')
        if analyte_id not in analyte_ids:
            raise ValueError(f'excluded_standards analyte {analyte_id} does not exist')
    for run_id, analyte_id in request['invalid_controls']:
        run = by_id.get(run_id)
        if run is None or run['kind'] != 'unknown':
            raise ValueError(f'invalid_controls entry {run_id}/{analyte_id} must reference an unknown run')
        if analyte_id not in analyte_ids:
            raise ValueError(f'invalid_controls analyte {analyte_id} does not exist')
        if run_id not in request['controls'] or analyte_id not in request['controls'][run_id]:
            raise ValueError(
                f'invalid_controls entry {run_id}/{analyte_id} must be a registered control observation')


def _fit_one(analyte, runs, areas, excluded_pairs):
    included = []
    for run in runs:
        if run['kind'] != 'standard':
            continue
        if [run['id'], analyte] in excluded_pairs:
            continue
        included.append((run['id'],
                         number(run['concentrations'][analyte]),
                         Fraction(areas[run['id']][analyte]['area'])))
    entry = {'analyte_id': analyte, 'available': False,
             'standard_run_ids': [point[0] for point in included]}
    try:
        cal = fit(included)
    except ValueError as exc:
        entry['reason'] = _FIT_REASONS.get(str(exc), CALIBRATION_UNAVAILABLE)
        return entry, None
    entry['available'] = True
    entry.update({key: cal[key] for key in
                  ('slope', 'intercept', 'min_concentration', 'max_concentration')})
    return entry, cal


def _estimate(cal, area):
    return (area - Fraction(cal['intercept'])) / Fraction(cal['slope'])


def compute_release(request, source_raw):
    """Run one revision end-to-end. Input structure is validated first, so an
    unusable calibration never masks an INPUT error."""
    data = json.loads(source_raw.decode('utf-8'), object_pairs_hook=pairs,
                      parse_constant=constant)
    analytes, runs = validate(data)
    _validate_references(request, analytes, runs)
    analyte_order = sorted(analytes)
    excluded = request['excluded_standards']
    invalid_pairs = {tuple(pair) for pair in request['invalid_controls']}
    controls = request['controls']
    control_run_positions = {run['id']: index for index, run in enumerate(runs)
                             if run['kind'] == 'unknown' and run['id'] in controls}

    areas = {run['id']: {a: integrate(run['samples'], *analytes[a]) for a in analyte_order}
             for run in runs}

    calibration_entries = {}
    fits = {}
    for analyte in analyte_order:
        entry, cal = _fit_one(analyte, runs, areas, excluded)
        calibration_entries[analyte] = entry
        fits[analyte] = cal

    # Registered control observations, in batch position order then analyte id.
    control_report = []
    control_pass = {}
    control_estimates = {}
    registered_positions = {a: [] for a in analyte_order}
    for run_id in sorted(control_run_positions, key=lambda rid: control_run_positions[rid]):
        position = control_run_positions[run_id]
        for analyte in analyte_order:
            spec = controls[run_id].get(analyte)
            if spec is None:
                continue
            registered_positions[analyte].append((position, run_id))
            target = Fraction(spec['target'])
            tolerance = Fraction(spec['tolerance'])
            manually_invalid = (run_id, analyte) in invalid_pairs
            reasons = []
            estimate_text = None
            passed = False
            if manually_invalid:
                reasons.append(MARKED_INVALID)
            cal = fits[analyte]
            if cal is None:
                reasons.append(CALIBRATION_UNAVAILABLE)
            else:
                estimate = _estimate(cal, Fraction(areas[run_id][analyte]['area']))
                estimate_text = str(estimate)
                control_estimates[(run_id, analyte)] = estimate
                if not (Fraction(cal['min_concentration']) <= estimate <=
                        Fraction(cal['max_concentration'])):
                    reasons.append(CONTROL_OUT_OF_RANGE)
                if abs(estimate - target) > tolerance:
                    reasons.append(CONTROL_OUT_OF_TOLERANCE)
            passed = not reasons
            control_pass[(run_id, analyte)] = passed
            control_report.append({
                'run_id': run_id,
                'analyte_id': analyte,
                'manually_invalid': manually_invalid,
                'target': spec['target'],
                'tolerance': spec['tolerance'],
                'estimate': estimate_text,
                'passed': passed,
                'reasons': reasons,
            })

    sample_runs = [run for run in runs if run['kind'] == 'unknown' and run['id'] not in controls]

    def nearest_controls(analyte, position):
        left = right = None
        for control_position, run_id in registered_positions[analyte]:
            if control_position < position:
                left = run_id
            elif control_position > position and right is None:
                right = run_id
        return left, right

    sample_report = []
    issued = {}
    for sample in sample_runs:
        position = runs.index(sample)
        for analyte in analyte_order:
            left_id, right_id = nearest_controls(analyte, position)
            reasons = []
            if left_id is None:
                reasons.append(MISSING_LEFT_CONTROL)
            elif not control_pass[(left_id, analyte)]:
                reasons.append(LEFT_CONTROL_FAILED)
            if right_id is None:
                reasons.append(MISSING_RIGHT_CONTROL)
            elif not control_pass[(right_id, analyte)]:
                reasons.append(RIGHT_CONTROL_FAILED)
            estimate_text = None
            cal = fits[analyte]
            if cal is None:
                reasons.append(CALIBRATION_UNAVAILABLE)
            else:
                estimate = _estimate(cal, Fraction(areas[sample['id']][analyte]['area']))
                estimate_text = str(estimate)
                if estimate < 0:
                    reasons.append(ESTIMATE_NEGATIVE)
                if not (Fraction(cal['min_concentration']) <= estimate <=
                        Fraction(cal['max_concentration'])):
                    reasons.append(ESTIMATE_OUT_OF_RANGE)
            record = None
            if not reasons:
                record = {
                    'run_id': sample['id'],
                    'analyte_id': analyte,
                    'concentration': estimate_text,
                    'calibration': {
                        'standard_run_ids': calibration_entries[analyte]['standard_run_ids'],
                        'slope': cal['slope'],
                        'intercept': cal['intercept'],
                        'min_concentration': cal['min_concentration'],
                        'max_concentration': cal['max_concentration'],
                    },
                    'left_control': _control_basis(left_id, analyte, controls,
                                                   control_estimates),
                    'right_control': _control_basis(right_id, analyte, controls,
                                                    control_estimates),
                }
                issued[(sample['id'], analyte)] = record
            sample_report.append({
                'run_id': sample['id'],
                'analyte_id': analyte,
                'estimate': estimate_text,
                'status': 'released' if record is not None else 'withheld',
                'reasons': reasons,
                'left_control_id': left_id,
                'right_control_id': right_id,
                'release': record,
            })

    return {
        'analyte_order': analyte_order,
        'calibrations': [calibration_entries[a] for a in analyte_order],
        'controls': control_report,
        'samples': sample_report,
        'issued': issued,
        'sample_order': [run['id'] for run in sample_runs],
    }


def _control_basis(run_id, analyte, controls, estimates):
    spec = controls[run_id][analyte]
    return {
        'run_id': run_id,
        'analyte_id': analyte,
        'estimate': str(estimates[(run_id, analyte)]),
        'target': spec['target'],
        'tolerance': spec['tolerance'],
    }


def build_corrections(previous_issued, current_issued, sample_order, analyte_order):
    """Diff two adjacent revisions. Keys are (sample id, analyte id)."""
    sample_rank = {run_id: index for index, run_id in enumerate(sample_order)}
    analyte_rank = {analyte: index for index, analyte in enumerate(analyte_order)}
    keys = set(previous_issued) | set(current_issued)
    corrections = []
    for key in sorted(keys, key=lambda key: (sample_rank[key[0]], analyte_rank[key[1]], key[0], key[1])):
        old = previous_issued.get(key)
        new = current_issued.get(key)
        run_id, analyte = key
        if old is None and new is not None:
            corrections.append({'action': 'added', 'run_id': run_id, 'analyte_id': analyte,
                                'previous': None, 'current': new})
        elif old is not None and new is None:
            corrections.append({'action': 'withdrawn', 'run_id': run_id, 'analyte_id': analyte,
                                'previous': old, 'current': None})
        elif old is not None and new is not None and old != new:
            corrections.append({'action': 'replaced', 'run_id': run_id, 'analyte_id': analyte,
                                'previous': old, 'current': new})
    return corrections


# --------------------------------------------------------------------------- store

class _Store:
    def __init__(self, root):
        self.root = Path(root)
        self.state_path = self.root / 'state.json'
        self.root.mkdir(parents=True, exist_ok=True)
        self.recover()
        self.state = json.loads(self.state_path.read_text(encoding='utf-8')) \
            if self.state_path.exists() else {'batches': {}, 'requests': {}}

    def recover(self):
        """Sweep staged-but-uncommitted artifacts. The state file is the commit
        point: anything it does not name is pre-commit debris."""
        for tmp in self.root.rglob('.tmp-*'):
            tmp.unlink(missing_ok=True)
        if not self.state_path.exists():
            debris = self.root / 'requests'
            if debris.is_dir():
                for child in debris.iterdir():
                    child.unlink(missing_ok=True)
                debris.rmdir()
            for child in list(self.root.iterdir()):
                if child.is_dir() and _ID_RE.fullmatch(child.name):
                    for revision_file in child.iterdir():
                        revision_file.unlink(missing_ok=True)
                    child.rmdir()
            return
        state = json.loads(self.state_path.read_text(encoding='utf-8'))
        known_requests = set(state['requests'])
        request_dir = self.root / 'requests'
        if request_dir.is_dir():
            for child in request_dir.iterdir():
                if child.stem not in known_requests:
                    child.unlink(missing_ok=True)
        for batch_id, batch in state['batches'].items():
            batch_dir = self.root / batch_id
            if not batch_dir.is_dir():
                continue
            keep = {f'{i}.json' for i in range(1, batch['current_revision'] + 1)}
            for child in batch_dir.iterdir():
                if child.name not in keep:
                    child.unlink(missing_ok=True)

    def _batch_dir(self, batch_id):
        directory = self.root / batch_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def commit(self, request, identity, report_bytes):
        batch = self.state['batches'].get(request['batch_id'])
        revision = 1 if batch is None else batch['current_revision'] + 1
        report_target = self._batch_dir(request['batch_id']) / f'{revision}.json'
        request_target = self.root / 'requests' / f'{request["request_id"]}.json'
        request_target.parent.mkdir(parents=True, exist_ok=True)
        new_state = {
            'batches': {
                **self.state['batches'],
                request['batch_id']: {
                    'current_revision': revision,
                    'source_sha256': request['source_sha256'],
                    'locked_identity': identity,
                },
            },
            'requests': {
                **self.state['requests'],
                request['request_id']: {
                    'batch_id': request['batch_id'],
                    'revision': revision,
                    'identity': identity,
                },
            },
        }
        state_bytes = (json.dumps(new_state, ensure_ascii=False, sort_keys=True,
                                  indent=2, allow_nan=False) + '\n').encode('utf-8')
        request_bytes = (json.dumps(new_state['requests'][request['request_id']],
                                    ensure_ascii=False, sort_keys=True, indent=2,
                                    allow_nan=False) + '\n').encode('utf-8')
        staged = []
        for target, payload in ((report_target, report_bytes),
                                (request_target, request_bytes),
                                (self.state_path, state_bytes)):
            descriptor, tmp_name = tempfile.mkstemp(dir=self.root, prefix='.tmp-')
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            staged.append((Path(tmp_name), target))
        # Renames are atomic; state.json is renamed last and is the commit point.
        for tmp_name, target in staged:
            os.replace(tmp_name, target)
        for directory in {self.root, report_target.parent, request_target.parent}:
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self.state = new_state
        return revision

    def committed_report(self, batch_id, revision):
        return (self.root / batch_id / f'{revision}.json').read_bytes()


def _same_lock(request, identity, locked_identity):
    locked = json.loads(locked_identity)
    current = json.loads(identity)
    return (locked['source_sha256'] == current['source_sha256']
            and locked['controls'] == current['controls'])


def release(store_dir, request, source_path=None):
    """Submit (or idempotently retry) one release request.

    Returns (report_bytes, revision, retried). Retry handling precedes the
    expected-revision check, INPUT reading and all recomputation.
    """
    store = _Store(store_dir)
    identity = canonical_identity(request)
    known = store.state['requests'].get(request['request_id'])
    if known is not None:
        if known['identity'] != identity:
            raise ValueError('request_id was already submitted with a different identity')
        return store.committed_report(known['batch_id'], known['revision']), \
            known['revision'], True

    batch = store.state['batches'].get(request['batch_id'])
    current_revision = 0 if batch is None else batch['current_revision']
    if request['expected_revision'] != current_revision:
        raise ValueError(
            f'expected_revision {request["expected_revision"]} does not match current revision '
            f'{current_revision}')
    if batch is not None and not _same_lock(request, identity, batch['locked_identity']):
        raise ValueError('source fingerprint and control registration are locked for this batch')
    if source_path is None:
        raise ValueError('a readable --source INPUT is required for a new request')
    with open(source_path, 'rb') as handle:
        source_raw = handle.read(INPUT_LIMIT + 1)
    if len(source_raw) > INPUT_LIMIT:
        raise ValueError('input over 8MB')
    digest = hashlib.sha256(source_raw).hexdigest()
    if digest != request['source_sha256']:
        raise ValueError('source_sha256 does not match the INPUT bytes')

    computed = compute_release(request, source_raw)
    previous_issued = _previous_issued(store, request['batch_id']) if batch is not None else {}
    corrections = build_corrections(previous_issued, computed['issued'],
                                    computed['sample_order'], computed['analyte_order'])
    revision = current_revision + 1
    report = {
        'schema_version': SCHEMA_VERSION,
        'batch_id': request['batch_id'],
        'request_id': request['request_id'],
        'source_sha256': request['source_sha256'],
        'revision': revision,
        'calibrations': computed['calibrations'],
        'controls': computed['controls'],
        'samples': computed['samples'],
        'corrections': corrections,
    }
    report_bytes = encode_report(report)
    # All validation and computation finished before any committed state changes.
    revision = store.commit(request, identity, report_bytes)
    return report_bytes, revision, False


def _previous_issued(store, batch_id):
    previous_revision = store.state['batches'][batch_id]['current_revision']
    previous = json.loads(store.committed_report(batch_id, previous_revision).decode('utf-8'))
    return {(item['run_id'], item['analyte_id']): item['release']
            for item in previous['samples'] if item['release'] is not None}


def release_show(store_dir, batch_id, revision):
    """Return the exact committed report bytes for a batch/revision."""
    store = _Store(store_dir)
    batch = store.state['batches'].get(batch_id)
    if batch is None:
        raise ValueError(f'unknown batch {batch_id}')
    if type(revision) is not int or isinstance(revision, bool) \
            or not 1 <= revision <= batch['current_revision']:
        raise ValueError('revision must reference a committed revision of the batch')
    return store.committed_report(batch_id, revision)
