"""Source-isolated blind benchmark: leave-one-group-out cross validation.

A PLAN maps every *standard* v1 run to an origin id and names the scored
standard runs.  Two standards must never be split across folds when either:

* their origin ids are equal, or
* their entire sampled domain and exact piecewise-linear signal are equal.

Both relations are transitive and may chain, so the final groups are the
connected components of a union-find over the standard runs.  Adding or
removing collinear samples, or changing the decimal spelling of an equal
value, never changes the normalized curve key: points are reduced to genuine
slope changes and compared as exact Fractions on the common time domain.

Every fold removes one whole component (including its unscored standards),
fits the remaining standards independently per analyte with the v1
equal-weight straight-line rules, and only then compares each scored record's
blind prediction with its own reference concentration.  Unknown runs still
pass v1 validation but never take part in grouping, fitting or scoring.

All arithmetic is exact (fractions.Fraction); every number in the report is a
reduced integer/fraction string.  The report is byte-deterministic: list
orders, fold ids and key order never depend on input object ordering.
"""
import json
from fractions import Fraction

from .checks import fields, ident
from .integrate import _interpolate, area_of
from .io import constant, pairs, publish, read_limited
from .model import validate

BENCHMARK_REPORT_VERSION = 1
PLAN_BYTES_LIMIT = 1_000_000

# Fit unavailability reasons, judged strictly in this order.
REASON_TOO_FEW = 'fewer_than_two_training_standards'
REASON_EQUAL_CONCENTRATIONS = 'all_training_concentrations_equal'
REASON_NONPOSITIVE_SLOPE = 'slope_not_positive'


# --------------------------------------------------------------------- plans

def load_plan(path):
    """Read and validate the benchmark plan document."""
    raw = read_limited(path, PLAN_BYTES_LIMIT)
    data = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                      parse_constant=constant)
    if not isinstance(data, dict):
        raise ValueError('benchmark plan top level must be an object')
    fields(data, ('origins', 'score_ids'))
    origins = data['origins']
    if not isinstance(origins, dict) or not origins:
        raise ValueError('benchmark plan origins must be a non-empty object')
    parsed_origins = {}
    for run_id, origin_id in origins.items():
        ident(run_id)
        ident(origin_id)
        parsed_origins[run_id] = origin_id
    score_entries = data['score_ids']
    if not isinstance(score_entries, list) or not score_entries:
        raise ValueError('benchmark plan score_ids must be a non-empty array')
    score_ids = []
    for value in score_entries:
        score_ids.append(ident(value))
    if len(set(score_ids)) != len(score_ids):
        raise ValueError('benchmark plan score_ids must not repeat a run id')
    return parsed_origins, score_ids


def _check_plan_against_source(origins, score_ids, standard_ids):
    standard_set = set(standard_ids)
    if set(origins) != standard_set:
        missing = sorted(standard_set - set(origins))
        extra = sorted(set(origins) - standard_set)
        if missing:
            raise ValueError('benchmark plan missing origins for standard run(s): '
                             + ', '.join(missing))
        raise ValueError('benchmark plan origins reference unknown or non-standard run(s): '
                         + ', '.join(extra))
    unknown = sorted(set(score_ids) - standard_set)
    if unknown:
        raise ValueError('benchmark plan score_ids reference unknown or non-standard run(s): '
                         + ', '.join(unknown))


# -------------------------------------------------------------- curve identity

def _reduced_curve(samples):
    """Normalize a run to its domain plus exact piecewise-linear signal.

    Decimal strings are parsed to Fractions so equivalent spellings compare
    equal, and collinear interior samples are removed so adding/removing a
    sample on the same segment never changes the identity.  Domain endpoints
    are always retained (their times define the domain).
    """
    points = [(Fraction(t), Fraction(y)) for t, y in samples]
    if len(points) <= 2:
        return tuple(points)
    reduced = [points[0]]
    for (t0, y0), (t1, y1), (t2, y2) in zip(points, points[1:], points[2:]):
        # (y1-y0)/(t1-t0) == (y2-y1)/(t2-t1), cross-multiplied.
        if (y1 - y0) * (t2 - t1) != (y2 - y1) * (t1 - t0):
            reduced.append((t1, y1))
    reduced.append(points[-1])
    return tuple(reduced)


class _UnionFind:
    def __init__(self, items):
        self.parent = {item: item for item in items}

    def find(self, item):
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            nxt = self.parent[item]
            self.parent[item] = root
            item = nxt
        return root

    def union(self, left, right):
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            # Deterministic representative regardless of union order.
            if left_root < right_root:
                self.parent[right_root] = left_root
            else:
                self.parent[left_root] = right_root


def build_groups(standards, origins):
    """Connected components over standard runs via origin and exact-curve edges."""
    run_ids = [run['id'] for run in standards]
    union_find = _UnionFind(run_ids)
    # Origin edges.
    by_origin = {}
    for run_id in run_ids:
        by_origin.setdefault(origins[run_id], []).append(run_id)
    for members in by_origin.values():
        first = members[0]
        for run_id in members[1:]:
            union_find.union(first, run_id)
    # Exact domain + piecewise-linear signal edges.
    by_curve = {}
    for run in standards:
        key = _reduced_curve(run['samples'])
        by_curve.setdefault(key, []).append(run['id'])
    for members in by_curve.values():
        first = members[0]
        for run_id in members[1:]:
            union_find.union(first, run_id)
    components = {}
    for run_id in run_ids:
        components.setdefault(union_find.find(run_id), []).append(run_id)
    # Canonical component identity: sorted member tuple.  Fold order is the
    # lexicographic order of these tuples.
    groups = sorted(tuple(sorted(members)) for members in components.values())
    return groups


# --------------------------------------------------------------------- fitting

def fit_line(points):
    """v1 equal-weight straight-line fit on (concentration, area) Fractions.

    Returns (slope, intercept, minimum, maximum) or (reason, None, None, None)
    where reason follows the mandated judgement order.
    """
    if len(points) < 2:
        return REASON_TOO_FEW, None, None, None
    n = len(points)
    sx = sum(x for x, _y in points)
    sy = sum(y for _x, y in points)
    denominator = n * sum(x * x for x, _y in points) - sx * sx
    if denominator == 0:
        return REASON_EQUAL_CONCENTRATIONS, None, None, None
    slope = (n * sum(x * y for x, y in points) - sx * sy) / denominator
    if slope <= 0:
        return REASON_NONPOSITIVE_SLOPE, None, None, None
    intercept = (sy - slope * sx) / n
    return slope, intercept, min(x for x, _y in points), max(x for x, _y in points)


# ------------------------------------------------------------------ benchmark

def benchmark(source_data, plan_path):
    """Run the leave-one-group-out benchmark and return the report object."""
    analytes, runs = validate(source_data)
    origins, score_ids = load_plan(plan_path)
    standards = [run for run in runs if run['kind'] == 'standard']
    standard_ids = [run['id'] for run in standards]
    _check_plan_against_source(origins, score_ids, standard_ids)
    score_set = set(score_ids)
    analyte_ids = sorted(analytes)
    run_index = {run['id']: run for run in standards}

    groups = build_groups(standards, origins)
    folds = [members for members in groups if score_set.intersection(members)]

    # Fold-independent evidence: parse each curve once and compute every
    # analyte area once.  Nothing here depends on fold membership, so reusing
    # it cannot leak held-out information; every prediction below still uses
    # only that fold's training standards for its calibration.  Window-endpoint
    # interpolation is likewise fold-independent and is therefore shared.
    parsed = {run['id']: [(Fraction(t), Fraction(y))
                          for t, y in run['samples']]
              for run in standards}
    time_axes = {run_id: [t for t, _y in points]
                 for run_id, points in parsed.items()}
    areas = {}
    for run_id, points in parsed.items():
        times = time_axes[run_id]
        run_areas = {}
        for analyte_id in analyte_ids:
            lo, hi = analytes[analyte_id]
            start = _interpolate(times, points, lo)
            end = _interpolate(times, points, hi)
            run_areas[analyte_id] = area_of(points, lo, hi, start, end)
        areas[run_id] = run_areas
    concentrations = {run_id: {analyte_id: Fraction(run_index[run_id]['concentrations'][analyte_id])
                               for analyte_id in analyte_ids}
                      for run_id in run_index}

    fold_entries = []
    # Per-analyte, per-fold MAE over scored records (None when unavailable).
    group_mae = {analyte_id: [] for analyte_id in analyte_ids}
    requested_groups = {analyte_id: 0 for analyte_id in analyte_ids}
    requested_records = {analyte_id: 0 for analyte_id in analyte_ids}
    successful_groups = {analyte_id: 0 for analyte_id in analyte_ids}
    successful_records = {analyte_id: 0 for analyte_id in analyte_ids}

    for fold_index, held in enumerate(folds, start=1):
        held_set = set(held)
        # Training identity is reported as a sorted set so the report cannot
        # depend on the source's run ordering.
        training_ids = sorted(run_id for run_id in standard_ids
                              if run_id not in held_set)
        scored_ids = sorted(score_set.intersection(held_set))
        for analyte_id in analyte_ids:
            requested_groups[analyte_id] += 1
            requested_records[analyte_id] += len(scored_ids)
        fold_analytes = []
        for analyte_id in analyte_ids:
            points = [(concentrations[run_id][analyte_id], areas[run_id][analyte_id])
                      for run_id in training_ids]
            slope, intercept, c_min, c_max = fit_line(points)
            analyte_entry = {'analyte_id': analyte_id}
            if c_min is None:
                # On failure fit_line returns (reason, None, None, None).
                analyte_entry['available'] = False
                analyte_entry['unavailable_reason'] = slope
                analyte_entry['slope'] = None
                analyte_entry['intercept'] = None
                analyte_entry['min_concentration'] = None
                analyte_entry['max_concentration'] = None
                group_mae[analyte_id].append((fold_index, None))
            else:
                analyte_entry['available'] = True
                analyte_entry['unavailable_reason'] = None
                analyte_entry['slope'] = str(slope)
                analyte_entry['intercept'] = str(intercept)
                analyte_entry['min_concentration'] = str(c_min)
                analyte_entry['max_concentration'] = str(c_max)
                successful_groups[analyte_id] += 1
            scores = []
            fold_errors = []
            for run_id in scored_ids:
                area = areas[run_id][analyte_id]
                reference = concentrations[run_id][analyte_id]
                row = {'run_id': run_id,
                       'area': str(area),
                       'reference_concentration': str(reference)}
                if analyte_entry['available']:
                    prediction = (area - intercept) / slope
                    error = prediction - reference
                    row['prediction'] = str(prediction)
                    row['error'] = str(error)
                    row['negative'] = prediction < 0
                    row['extrapolated'] = not (c_min <= prediction <= c_max)
                    fold_errors.append(abs(error))
                    successful_records[analyte_id] += 1
                else:
                    row['prediction'] = None
                    row['error'] = None
                    row['negative'] = None
                    row['extrapolated'] = None
                scores.append(row)
            analyte_entry['scores'] = scores
            if analyte_entry['available']:
                mae = sum(fold_errors, Fraction(0)) / len(fold_errors)
                group_mae[analyte_id].append((fold_index, mae))
            fold_analytes.append(analyte_entry)
        fold_entries.append({
            'fold_id': f'F{fold_index:03d}',
            'held_out_run_ids': list(held),
            'training_run_ids': training_ids,
            'scored_run_ids': scored_ids,
            'analytes': fold_analytes,
        })

    summaries = []
    for analyte_id in analyte_ids:
        per_group = [{'fold_id': f'F{fold_index:03d}',
                      'mean_absolute_error': (None if mae is None else str(mae))}
                     for fold_index, mae in group_mae[analyte_id]]
        successful_maes = [mae for _index, mae in group_mae[analyte_id]
                           if mae is not None]
        total = (sum(successful_maes, Fraction(0)) / len(successful_maes)
                 if successful_maes else None)
        summaries.append({
            'analyte_id': analyte_id,
            'requested_groups': requested_groups[analyte_id],
            'requested_records': requested_records[analyte_id],
            'successful_groups': successful_groups[analyte_id],
            'successful_records': successful_records[analyte_id],
            'group_mean_absolute_errors': per_group,
            'total_mean_absolute_error': None if total is None else str(total),
        })

    return {
        'schema_version': BENCHMARK_REPORT_VERSION,
        'analyte_ids': analyte_ids,
        'score_ids': sorted(score_ids),
        'folds': fold_entries,
        'summaries': summaries,
    }


def run_benchmark(source_path, plan_path, output_path):
    """Load inputs, benchmark, and publish the report without overwriting."""
    from .io import load
    source_data = load(source_path)
    report = benchmark(source_data, plan_path)
    publish(output_path, report)
    return report
