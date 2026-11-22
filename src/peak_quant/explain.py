"""Explain package generation: candidate windows quantified with v1 evidence.

The package is a self-contained, byte-deterministic JSON document.  It never
embeds sample arrays, source paths, caches or process state: a contrast query
only needs the package plus its query file in an arbitrary directory.
"""
import hashlib
import json
from fractions import Fraction

from .calibrate import fit
from .candidates import CANDIDATE_BYTES_LIMIT
from .candidates import parse_candidates
from .checks import number
from .integrate import integrate
from .io import constant, pairs, read_limited
from .model import validate
from .selection import attainable, best_metrics, representative

PACKAGE_VERSION = 3
PACKAGE_BYTES_LIMIT = 16000000


def build_package(source_raw, candidates_raw):
    """Compute the explain package from exact INPUT and CANDIDATES byte strings.

    Standard calibration follows the v1 fixed windows, signed baseline
    subtraction and equal-weight fit.  Any analyte whose fit fails aborts the
    whole package; no partial package is returned.
    """
    data = json.loads(source_raw.decode('utf-8'), object_pairs_hook=pairs,
                      parse_constant=constant)
    candidate_data = json.loads(candidates_raw.decode('utf-8'), object_pairs_hook=pairs,
                                parse_constant=constant)
    analytes, runs = validate(data)
    analyte_order, records = parse_candidates(candidate_data, analytes, runs)
    analyte_index = {analyte_id: i for i, analyte_id in enumerate(analyte_order)}
    unknown_runs = [run for run in runs if run['kind'] == 'unknown']

    # v1 standard evidence: fixed windows only; candidates never reselect them.
    calibrations = {}
    standard_run_order = [run['id'] for run in runs if run['kind'] == 'standard']
    for analyte_id in analyte_order:
        points = []
        for run in runs:
            if run['kind'] != 'standard':
                continue
            lo, hi = analytes[analyte_id]
            area = Fraction(integrate(run['samples'], lo, hi)['area'])
            points.append((run['id'], number(run['concentrations'][analyte_id]), area))
        # A failed fit raises, failing the whole generation as required.
        calibrations[analyte_id] = fit(points)

    # Group candidates by run/analyte and quantify each with its own window.
    run_by_id = {run['id']: run for run in unknown_runs}
    grouped = {run['id']: {analyte_id: [] for analyte_id in analyte_order}
               for run in unknown_runs}
    candidate_entries = []
    for record in records:
        run = run_by_id[record['run_id']]
        result = integrate(run['samples'], record['lo'], record['hi'])
        area = Fraction(result['area'])
        cal = calibrations[record['analyte_id']]
        concentration = (area - Fraction(cal['intercept'])) / Fraction(cal['slope'])
        entry = {
            'analyte_id': record['analyte_id'],
            'area': str(area),
            'candidate_id': record['candidate_id'],
            'concentration': str(concentration),
            'extrapolated': not (Fraction(cal['min_concentration']) <= concentration
                                 <= Fraction(cal['max_concentration'])),
            'negative': concentration < 0,
            'penalty': record['penalty'],
            'run_id': record['run_id'],
            'window': list(record['window']),
        }
        candidate_entries.append(entry)
        grouped[record['run_id']][record['analyte_id']].append({
            'id': record['candidate_id'],
            'lo': record['lo'],
            'hi': record['hi'],
            'penalty': record['penalty'],
            'area': area,
            'concentration': concentration,
        })
    candidate_entries.sort(
        key=lambda entry: (entry['run_id'], analyte_index[entry['analyte_id']],
                           entry['candidate_id']))

    run_blocks = []
    for run in unknown_runs:
        options = [grouped[run['id']][analyte_id] for analyte_id in analyte_order]
        best_count, best_penalty = best_metrics(options)
        witness = representative(options)
        witness_sequence = [None if option is None else option['id'] for option in witness]
        kept = attainable(options)
        attainable_block = []
        for analyte_id, (kept_ids, vacancy_ok) in zip(analyte_order, kept):
            attainable_block.append({
                'analyte_id': analyte_id,
                'candidate_ids': sorted(kept_ids),
                'vacancy_available': vacancy_ok,
            })
        run_blocks.append({
            'attainable': attainable_block,
            'best_penalty': best_penalty,
            'best_selected': best_count,
            'representative': witness_sequence,
            'run_id': run['id'],
            # Keep the original v1 decimal text instead of re-formatting it.
            'sample_range': [run['samples'][0][0], run['samples'][-1][0]],
        })

    standard_run_ids = list(standard_run_order)
    calibration_basis = [{
        'analyte_id': analyte_id,
        'intercept': calibrations[analyte_id]['intercept'],
        'max_concentration': calibrations[analyte_id]['max_concentration'],
        'min_concentration': calibrations[analyte_id]['min_concentration'],
        'slope': calibrations[analyte_id]['slope'],
        'standard_run_ids': list(standard_run_ids),
    } for analyte_id in analyte_order]

    return {
        'analyte_order': list(analyte_order),
        'calibrations': calibration_basis,
        'candidates': candidate_entries,
        'package_version': PACKAGE_VERSION,
        'runs': run_blocks,
        'source_sha256': hashlib.sha256(source_raw).hexdigest(),
    }


def load_inputs(source_path, candidates_path):
    source_raw = read_limited(source_path, CANDIDATE_BYTES_LIMIT)
    candidates_raw = read_limited(candidates_path, CANDIDATE_BYTES_LIMIT)
    return source_raw, candidates_raw


def encode_package(package):
    return (json.dumps(package, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + '\n').encode('utf-8')
