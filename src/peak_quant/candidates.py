"""Strict validation of the candidate-peak batch document (offline synthetic use)."""
from .checks import array, fields, ident, integer, number, unique

CANDIDATE_LIMIT = 30000
PAIR_LIMIT = 12
CANDIDATE_BYTES_LIMIT = 8000000
_PENALTY_MAX = 10000


def parse_candidates(data, analytes, runs):
    """Validate a CANDIDATES document against a validated v1 INPUT.

    analytes: dict id -> (lo, hi) Fractions; runs: validated v1 run list.
    Returns (analyte_order, records) where records keep the exact input
    decimals plus parsed Fraction windows.  The INPUT is never modified.
    """
    fields(data, ('analyte_order', 'candidates'))
    order = data['analyte_order']
    if not isinstance(order, list) or len(order) != len(analytes):
        raise ValueError('analyte_order must list every analyte id once')
    for analyte_id in order:
        ident(analyte_id)
    unique(order)
    if set(order) != set(analytes):
        raise ValueError('analyte_order must be a permutation of the source analytes')

    unknown_runs = {run['id']: run for run in runs if run['kind'] == 'unknown'}
    raw_records = data['candidates']
    array(raw_records, 0, CANDIDATE_LIMIT)
    records = []
    per_pair = {}
    for index, item in enumerate(raw_records):
        fields(item, ('run_id', 'analyte_id', 'candidate_id', 'window', 'penalty'))
        run_id = ident(item['run_id'])
        analyte_id = ident(item['analyte_id'])
        candidate_id = ident(item['candidate_id'])
        if run_id not in unknown_runs:
            raise ValueError(f'candidate {index}: run {run_id} must be an existing unknown run')
        if analyte_id not in analytes:
            raise ValueError(f'candidate {index}: unknown analyte reference {analyte_id}')
        lo_text, hi_text = array(item['window'], 2, 2)
        lo = number(lo_text)
        hi = number(hi_text)
        if lo < 0 or hi <= lo:
            raise ValueError(f'candidate {index}: require 0 <= start < end')
        run = unknown_runs[run_id]
        first = number(run['samples'][0][0])
        last = number(run['samples'][-1][0])
        if lo < first or hi > last:
            raise ValueError(
                f'candidate {index}: window must lie inside the sample range of run {run_id}')
        # Booleans are explicitly rejected even though bool subclasses int.
        penalty = item['penalty']
        integer(penalty, 0, _PENALTY_MAX)
        pair = (run_id, analyte_id)
        bucket = per_pair.setdefault(pair, set())
        if candidate_id in bucket:
            raise ValueError(
                f'duplicate candidate_id {candidate_id} for run {run_id}, analyte {analyte_id}')
        if len(bucket) >= PAIR_LIMIT:
            raise ValueError(
                f'at most {PAIR_LIMIT} candidates per run/analyte pair ({run_id}/{analyte_id})')
        bucket.add(candidate_id)
        records.append({
            'run_id': run_id,
            'analyte_id': analyte_id,
            'candidate_id': candidate_id,
            'window': (lo_text, hi_text),
            'lo': lo,
            'hi': hi,
            'penalty': penalty,
        })
    return order, records
