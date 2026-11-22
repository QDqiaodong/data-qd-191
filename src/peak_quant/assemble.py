"""Reassemble time-aligned fragment streams into a quantitative v1 source.

The plan describes runs, spans, fragments and the exact affine time mapping of
each fragment (two anchors).  A JSON Lines stream on standard input delivers
each declared fragment exactly once, in arbitrary order.  Fragments are
spooled to local temporary space because the stream cannot be re-read and the
batch may be far larger than memory.

All alignment, overlap comparison and collinear removal use exact Fraction
arithmetic.  v1 decimal representability is only checked for the points that
survive into the final source; intermediate collinear points are never a
reason to reject the batch.  The target file is created exclusively and only
after the whole stream has been consumed and every run validated.
"""
import json
import os
import re
import shutil
import tempfile
from bisect import bisect_right
from fractions import Fraction
from pathlib import Path

from .checks import array, fields, ident, number, unique
from .io import constant, pairs, publish_bytes, read_limited

PLAN_BYTES_LIMIT = 2_000_000
STREAM_BYTES_LIMIT = 320_000_000
LINE_BYTES_LIMIT = 160_000
MAX_FRAGMENTS_PER_RUN = 16
SAMPLES_MIN, SAMPLES_MAX = 2, 2_000
V1_SOURCE_BYTES_LIMIT = 8_000_000
V1_SAMPLE_LIMIT = 2_000
_DECIMAL_RE = re.compile(r'-?(?:0|[1-9][0-9]{0,8})(?:[.][0-9]{1,6})?\Z')


class AssembleError(ValueError):
    """Base class: the message always names the error category explicitly."""

    category = 'assemble'

    def __init__(self, detail):
        super().__init__(f'{self.category} error: {detail}')


class PlanError(AssembleError):
    category = 'assemble plan input'


class StreamError(AssembleError):
    category = 'assemble stream input'


class CoverageError(AssembleError):
    category = 'assemble coverage'


class ConflictError(AssembleError):
    category = 'assemble conflict'


class RepresentationError(AssembleError):
    category = 'assemble representation'


# --------------------------------------------------------------------- plan

def _parse_analytes(data):
    fields(data, ('analytes', 'runs'))
    analytes = {}
    for analyte in array(data['analytes'], 1, 30):
        fields(analyte, ('id', 'window'))
        analyte_id = ident(analyte['id'])
        lo, hi = (number(x) for x in array(analyte['window'], 2, 2))
        if lo < 0 or hi <= lo:
            raise PlanError('analyte window must be non-negative with positive width')
        if analyte_id in analytes:
            raise PlanError(f'duplicate analyte id {analyte_id}')
        analytes[analyte_id] = (lo, hi)
    return analytes


def _parse_fragment(entry, known_ids):
    fields(entry, ('id', 'anchors'))
    fragment_id = ident(entry['id'])
    if fragment_id in known_ids:
        raise PlanError(f'duplicate fragment id {fragment_id}')
    anchors = array(entry['anchors'], 2, 2)
    parsed = []
    for anchor in anchors:
        local, absolute = (number(x) for x in array(anchor, 2, 2))
        if local < 0 or absolute < 0:
            raise PlanError(f'fragment {fragment_id} anchors must be non-negative')
        parsed.append((local, absolute))
    (local0, abs0), (local1, abs1) = parsed
    if local1 <= local0:
        raise PlanError(f'fragment {fragment_id} local anchors must strictly increase')
    if abs1 <= abs0:
        raise PlanError(f'fragment {fragment_id} absolute anchors must strictly increase')
    known_ids.add(fragment_id)
    return {'id': fragment_id,
            'local0': local0, 'local1': local1, 'abs0': abs0,
            'scale': (abs1 - abs0) / (local1 - local0)}


def _parse_runs(data, analytes):
    analyte_ids = set(analytes)
    runs = []
    known_fragment_ids = set()
    run_ids = []
    for run in array(data['runs'], 1, 200):
        fields(run, ('id', 'kind', 'span', 'fragments'), ('concentrations',))
        run_id = ident(run['id'])
        if run['kind'] not in ('standard', 'unknown'):
            raise PlanError(f'run {run_id} has unknown kind')
        lo, hi = (number(x) for x in array(run['span'], 2, 2))
        if lo < 0 or hi <= lo:
            raise PlanError(f'run {run_id} span must be non-negative with positive width')
        for analyte_id, (window_lo, window_hi) in analytes.items():
            if window_lo < lo or window_hi > hi:
                raise PlanError(
                    f'run {run_id} span does not contain analyte {analyte_id} window')
        fragments = []
        for entry in array(run['fragments'], 1, MAX_FRAGMENTS_PER_RUN):
            fragments.append(_parse_fragment(entry, known_fragment_ids))
        if run['kind'] == 'standard':
            concentrations = run.get('concentrations')
            if not isinstance(concentrations, dict) or set(concentrations) != analyte_ids:
                raise PlanError(f'standard run {run_id} needs every analyte concentration')
            if any(number(value) < 0 for value in concentrations.values()):
                raise PlanError(f'standard run {run_id} has a negative concentration')
        elif 'concentrations' in run:
            raise PlanError(f'unknown run {run_id} must not carry concentrations')
        run_ids.append(run_id)
        runs.append({'id': run_id, 'kind': run['kind'], 'span': (lo, hi),
                     'fragments': fragments,
                     'concentrations': run.get('concentrations')})
    unique(run_ids)
    return runs


def parse_plan(path):
    try:
        raw = read_limited(path, PLAN_BYTES_LIMIT)
    except (OSError, ValueError) as exc:
        raise PlanError(f'cannot read plan: {exc}') from exc
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=constant)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise PlanError(f'invalid plan JSON: {exc}') from exc
    if not isinstance(data, dict):
        raise PlanError('plan top level must be an object')
    analytes = _parse_analytes(data)
    runs = _parse_runs(data, analytes)
    fragment_index = {}
    for run in runs:
        for fragment in run['fragments']:
            fragment_index[fragment['id']] = (run['id'], fragment)
    return analytes, runs, fragment_index


# ------------------------------------------------------------------- spooling

def _parse_line(raw, line_number, fragment_index, received):
    try:
        obj = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                         parse_constant=constant)
    except UnicodeError as exc:
        raise StreamError(f'line {line_number} is not valid UTF-8: {exc}') from exc
    except json.JSONDecodeError as exc:
        raise StreamError(f'line {line_number} is not valid JSON: {exc}') from exc
    if not isinstance(obj, dict):
        raise StreamError(f'line {line_number} must be a JSON object')
    fields(obj, ('fragment_id', 'samples'))
    fragment_id = obj['fragment_id']
    if not isinstance(fragment_id, str):
        raise StreamError(f'line {line_number} fragment_id must be a string')
    try:
        ident(fragment_id)
    except ValueError as exc:
        raise StreamError(f'line {line_number} invalid fragment_id: {exc}') from exc
    if fragment_id not in fragment_index:
        raise StreamError(
            f'line {line_number} fragment {fragment_id} is not declared in the plan')
    if fragment_id in received:
        raise StreamError(
            f'line {line_number} fragment {fragment_id} was already delivered')
    run_id, fragment = fragment_index[fragment_id]
    rows = array(obj['samples'], SAMPLES_MIN, SAMPLES_MAX)
    previous = None
    for row in rows:
        if not isinstance(row, list) or len(row) != 2:
            raise StreamError(
                f'line {line_number} fragment {fragment_id} samples must be [t, y] pairs')
        local, signal = (number(value) for value in row)
        if local < fragment['local0'] or local > fragment['local1']:
            raise StreamError(
                f'line {line_number} fragment {fragment_id} sample time {row[0]} '
                'lies outside its local anchors')
        if previous is not None and local <= previous:
            raise StreamError(
                f'line {line_number} fragment {fragment_id} sample times must increase')
        previous = local
    received.add(fragment_id)
    return run_id, fragment_id


def spool_stream(stream, fragment_index, spool_dir):
    """Read the non-seekable stream once, validating and spooling every line."""
    handles = {}
    spool_index = {}
    received = set()
    line_number = 0
    total = 0
    try:
        while True:
            line = stream.readline()
            if not line:
                break
            line_number += 1
            total += len(line)
            if total > STREAM_BYTES_LIMIT:
                raise StreamError(
                    f'stream exceeds {STREAM_BYTES_LIMIT} bytes before its end')
            if len(line) > LINE_BYTES_LIMIT:
                raise StreamError(
                    f'line {line_number} exceeds {LINE_BYTES_LIMIT} bytes')
            if line == b'\n':
                raise StreamError(f'line {line_number} is empty')
            if line.endswith(b'\n'):
                raw = line[:-1]
            else:
                raw = line
            if not raw:
                raise StreamError(f'line {line_number} is empty')
            run_id, fragment_id = _parse_line(raw, line_number,
                                              fragment_index, received)
            index = spool_index.get(run_id)
            if index is None:
                index = len(spool_index)
                spool_index[run_id] = index
                handles[run_id] = open(
                    os.path.join(spool_dir, f'{index}.jsonl'), 'wb')
            handles[run_id].write(raw)
            handles[run_id].write(b'\n')
    finally:
        for handle in handles.values():
            handle.close()
    declared = set(fragment_index)
    missing = sorted(declared - received)
    if missing:
        raise StreamError('stream ended without declared fragment(s): '
                          + ', '.join(missing[:8])
                          + (f' and {len(missing) - 8} more' if len(missing) > 8 else ''))
    return {run_id: index for index, run_id in enumerate(handles)}


# ------------------------------------------------------------------ assembly

def _mapped_curve(fragment, rows):
    """Map validated [local, signal] rows onto the absolute time axis."""
    points = []
    for local_text, signal_text in rows:
        absolute = fragment['abs0'] + (number(local_text) - fragment['local0']) \
            * fragment['scale']
        points.append((absolute, number(signal_text)))
    return points


def _evaluate(points, t):
    """Exact linear interpolation of a mapped fragment curve at time t."""
    times = [point[0] for point in points]
    if t == times[-1]:
        return points[-1][1]
    index = bisect_right(times, t) - 1
    a, ya = points[index]
    b, yb = points[index + 1]
    return ya + (yb - ya) * (t - a) / (b - a)


def _merge_run(run, spool_path):
    span_lo, span_hi = run['span']
    curves = []
    with open(spool_path, 'rb') as handle:
        for raw in handle:
            obj = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
            fragment_id = obj['fragment_id']
            fragment = next(f for f in run['fragments'] if f['id'] == fragment_id)
            points = _mapped_curve(fragment, obj['samples'])
            curves.append((fragment_id, points))

    events = {span_lo, span_hi}
    for _fragment_id, points in curves:
        for t, _y in points:
            if span_lo <= t <= span_hi:
                events.add(t)
    events = sorted(events)

    def providers_at(t):
        return [(fragment_id, points) for fragment_id, points in curves
                if points[0][0] <= t <= points[-1][0]]

    merged = []
    for t in events:
        providers = providers_at(t)
        if not providers:
            raise CoverageError(
                f'run {run["id"]} has no fragment covering time {t}')
        values = {_evaluate(points, t) for _fragment_id, points in providers}
        if len(values) != 1:
            raise ConflictError(
                f'run {run["id"]} fragments disagree at time {t}: '
                + ', '.join(fragment_id for fragment_id, _points in providers))
        merged.append((t, values.pop()))

    for a, b in zip(events, events[1:]):
        if not any(points[0][0] <= a and b <= points[-1][0]
                   for _fragment_id, points in curves):
            raise CoverageError(
                f'run {run["id"]} has an uncovered gap between {a} and {b}')

    # Keep span endpoints and genuine slope changes only; never keep a point
    # merely because a fragment boundary or a collinear sample sits there.
    reduced = [merged[0]]
    for previous, current, following in zip(merged, merged[1:], merged[2:]):
        (t0, y0), (t1, y1), (t2, y2) = previous, current, following
        if (y1 - y0) * (t2 - t1) != (y2 - y1) * (t1 - t0):
            reduced.append(current)
    reduced.append(merged[-1])
    if len(reduced) > V1_SAMPLE_LIMIT:
        raise RepresentationError(
            f'run {run["id"]} needs {len(reduced)} samples; v1 allows '
            f'{V1_SAMPLE_LIMIT}')
    return reduced


def v1_decimal(value, context):
    """Render an exact Fraction as a bounded v1 decimal string or fail loudly."""
    fraction = Fraction(value)
    scaled = fraction * 10 ** 6
    if scaled.denominator != 1:
        raise RepresentationError(
            f'{context} value {fraction} is not representable as a v1 decimal '
            '(at most six fractional digits)')
    numerator = scaled.numerator
    sign = '-' if numerator < 0 else ''
    whole, frac = divmod(abs(numerator), 10 ** 6)
    if whole > 999_999_999:
        raise RepresentationError(
            f'{context} value {fraction} exceeds the v1 integer bound')
    if frac == 0:
        text = f'{sign}{whole}'
    else:
        text = f'{sign}{whole}.{str(frac).zfill(6).rstrip("0")}'
    if not _DECIMAL_RE.fullmatch(text):
        raise RepresentationError(f'{context} value {fraction} fails the v1 number grammar')
    return text


def _build_source(analytes, runs, spool_index, spool_dir):
    output_runs = []
    for run in runs:
        spool_path = os.path.join(spool_dir, f'{spool_index[run["id"]]}.jsonl')
        reduced = _merge_run(run, spool_path)
        samples = [[v1_decimal(t, f'run {run["id"]} time'),
                    v1_decimal(y, f'run {run["id"]} signal')]
                   for t, y in reduced]
        entry = {'id': run['id'], 'kind': run['kind'], 'samples': samples}
        if run['kind'] == 'standard':
            entry['concentrations'] = {
                analyte_id: v1_decimal(number(text),
                                       f'run {run["id"]} concentration {analyte_id}')
                for analyte_id, text in sorted(run['concentrations'].items())}
        output_runs.append(entry)
    source = {
        'analytes': [{'id': analyte_id,
                      'window': [v1_decimal(lo, f'analyte {analyte_id} window'),
                                 v1_decimal(hi, f'analyte {analyte_id} window')]}
                     for analyte_id, (lo, hi) in sorted(analytes.items())],
        'runs': output_runs,
    }
    payload = (json.dumps(source, ensure_ascii=False, sort_keys=True, indent=2,
                          allow_nan=False) + '\n').encode('utf-8')
    if len(payload) > V1_SOURCE_BYTES_LIMIT:
        raise RepresentationError(
            f'assembled source is {len(payload)} bytes; v1 allows '
            f'{V1_SOURCE_BYTES_LIMIT}')
    return source, payload


def assemble(plan_path, output_path, stream=None):
    """Drive plan validation, stream spooling, exact assembly and publishing."""
    analytes, runs, fragment_index = parse_plan(plan_path)
    target = Path(output_path)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f'output {output_path} already exists; refusing to overwrite')
    if stream is None:
        import sys
        stream = sys.stdin.buffer
    spool_root = tempfile.mkdtemp(prefix='peakquant-assemble-')
    try:
        spool_index = spool_stream(stream, fragment_index, spool_root)
        _source, payload = _build_source(analytes, runs, spool_index, spool_root)
        # Publishing happens only after the full stream and every run checked.
        publish_bytes(output_path, payload)
    finally:
        shutil.rmtree(spool_root, ignore_errors=True)
