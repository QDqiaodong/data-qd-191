"""Offline contrast queries over a self-contained explain package.

A query never leaves the original best-explanation set: pins and term coverage
only filter that set, they never relax its two optimum indicators.  All
arithmetic is exact Fraction arithmetic and scores are printed as reduced
integer/fraction strings.
"""
import json
import re
from fractions import Fraction

from .checks import ident, number
from .explain import PACKAGE_BYTES_LIMIT, PACKAGE_VERSION
from .io import constant, pairs, read_limited
from .selection import attainable, best_metrics, constrained_extrema, representative

QUERY_BYTES_LIMIT = 8000000
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_COEFF_MIN = -1000
_COEFF_MAX = 1000


_FRACTION = re.compile(r'-?[0-9]+\Z')
_DECIMAL = re.compile(r'-?(?:0|[1-9][0-9]{0,8})(?:[.][0-9]{1,6})?\Z')


def _decimal(value, label):
    if not isinstance(value, str):
        raise ValueError(f'{label} must be a decimal string')
    return number(value)


def _exact(value, label):
    """Parse the exact numeric strings a package stores.

    Areas are v1 decimal strings; concentrations are reduced integer or
    fraction strings produced from exact Fraction arithmetic (v1 itself emits
    the same forms, e.g. '3/2').  Nothing here parses as float.
    """
    if not isinstance(value, str):
        raise ValueError(f'{label} must be an exact numeric string')
    if _DECIMAL.fullmatch(value):
        return number(value)
    if '/' in value:
        numerator, denominator = value.split('/')
        if _FRACTION.fullmatch(numerator) and denominator.isdigit() \
                and int(denominator) > 0:
            result = Fraction(int(numerator), int(denominator))
            if str(result) != value:
                raise ValueError(f'{label} must be a reduced fraction string')
            return result
        raise ValueError(f'{label} must be a reduced positive-denominator fraction string')
    elif _FRACTION.fullmatch(value):
        return Fraction(int(value))
    raise ValueError(f'{label} is not a bounded decimal or reduced fraction string')


def _is_bool(value):
    return isinstance(value, bool)


def validate_package(data):
    """Structural and arithmetic integrity check for an explain package.

    The best-set indicators stored in the package are recomputed from the
    candidate table and calibration basis; any mismatch is corruption rather
    than a query result.
    """
    if not isinstance(data, dict):
        raise ValueError('package must be a JSON object')
    expected = {'analyte_order', 'calibrations', 'candidates', 'package_version',
                'runs', 'source_sha256'}
    if set(data) != expected:
        raise ValueError(f'package fields must be exactly {sorted(expected)}')
    if data['package_version'] != PACKAGE_VERSION:
        raise ValueError(
            f'unsupported package version {data["package_version"]}; supported: {PACKAGE_VERSION}')
    digest = data['source_sha256']
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        raise ValueError('source_sha256 must be 64 lowercase hex characters')

    order = data['analyte_order']
    if not isinstance(order, list) or not order or len(set(order)) != len(order):
        raise ValueError('analyte_order must be a non-empty list of unique ids')
    analyte_index = {}
    for analyte_id in order:
        ident(analyte_id)
        analyte_index[analyte_id] = len(analyte_index)

    calibrations = data['calibrations']
    if not isinstance(calibrations, list) or len(calibrations) != len(order):
        raise ValueError('one calibration entry per analyte required')
    cal_by_analyte = {}
    for block in calibrations:
        if not isinstance(block, dict) or set(block) != {
                'analyte_id', 'intercept', 'max_concentration', 'min_concentration',
                'slope', 'standard_run_ids'}:
            raise ValueError('corrupt calibration entry')
        analyte_id = ident(block['analyte_id'])
        if analyte_id not in analyte_index or analyte_id in cal_by_analyte:
            raise ValueError('calibration analyte does not match analyte_order')
        slope = _exact(block['slope'], 'slope')
        intercept = _exact(block['intercept'], 'intercept')
        minimum = _exact(block['min_concentration'], 'min_concentration')
        maximum = _exact(block['max_concentration'], 'max_concentration')
        if slope <= 0 or minimum > maximum:
            raise ValueError('corrupt calibration bounds')
        ids = block['standard_run_ids']
        if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
            raise ValueError('standard_run_ids must be a non-empty unique list')
        for run_id in ids:
            ident(run_id)
        cal_by_analyte[analyte_id] = (slope, intercept, minimum, maximum)

    candidates = data['candidates']
    if not isinstance(candidates, list) or len(candidates) > 30000:
        raise ValueError('candidates must be a list of at most 30000 entries')
    registered = {}
    grouped = {}
    for entry in candidates:
        if not isinstance(entry, dict) or set(entry) != {
                'analyte_id', 'area', 'candidate_id', 'concentration', 'extrapolated',
                'negative', 'penalty', 'run_id', 'window'}:
            raise ValueError('corrupt candidate entry')
        run_id = ident(entry['run_id'])
        analyte_id = ident(entry['analyte_id'])
        candidate_id = ident(entry['candidate_id'])
        if analyte_id not in analyte_index:
            raise ValueError(f'candidate references unknown analyte {analyte_id}')
        if not isinstance(entry['window'], list) or len(entry['window']) != 2:
            raise ValueError('candidate window must have two decimals')
        lo = _decimal(entry['window'][0], 'window start')
        hi = _decimal(entry['window'][1], 'window end')
        if lo < 0 or hi <= lo:
            raise ValueError('candidate window requires 0 <= start < end')
        if _is_bool(entry['penalty']) or not isinstance(entry['penalty'], int) \
                or not 0 <= entry['penalty'] <= 10000:
            raise ValueError('candidate penalty must be an integer in 0..10000')
        if not isinstance(entry['negative'], bool) or not isinstance(entry['extrapolated'], bool):
            raise ValueError('candidate flags must be booleans')
        area = _exact(entry['area'], 'area')
        concentration = _exact(entry['concentration'], 'concentration')
        slope, intercept, minimum, maximum = cal_by_analyte[analyte_id]
        expected_concentration = (area - intercept) / slope
        if expected_concentration != concentration:
            raise ValueError('candidate concentration does not match its area and calibration')
        if entry['negative'] != (concentration < 0):
            raise ValueError('candidate negative flag is corrupt')
        if entry['extrapolated'] != (not minimum <= concentration <= maximum):
            raise ValueError('candidate extrapolated flag is corrupt')
        key = (run_id, analyte_id)
        bucket = registered.setdefault(key, set())
        if candidate_id in bucket:
            raise ValueError('duplicate candidate id within run/analyte pair')
        if len(bucket) >= 12:
            raise ValueError('at most 12 candidates per run/analyte pair')
        bucket.add(candidate_id)
        grouped.setdefault(run_id, {}).setdefault(analyte_id, []).append({
            'id': candidate_id,
            'lo': lo,
            'hi': hi,
            'penalty': entry['penalty'],
            'area': area,
            'concentration': concentration,
            'window_text': list(entry['window']),
        })

    runs = data['runs']
    if not isinstance(runs, list):
        raise ValueError('runs must be a list')
    run_ids = set()
    for block in runs:
        if not isinstance(block, dict) or set(block) != {
                'attainable', 'best_penalty', 'best_selected', 'representative',
                'run_id', 'sample_range'}:
            raise ValueError('corrupt run block')
        run_id = ident(block['run_id'])
        if run_id in run_ids:
            raise ValueError(f'duplicate run block {run_id}')
        run_ids.add(run_id)
        if not isinstance(block['sample_range'], list) or len(block['sample_range']) != 2:
            raise ValueError('corrupt sample_range')
        first = _decimal(block['sample_range'][0], 'sample range start')
        last = _decimal(block['sample_range'][1], 'sample range end')
        if first < 0 or last <= first:
            raise ValueError('corrupt sample_range')
        if _is_bool(block['best_selected']) or _is_bool(block['best_penalty']) \
                or not isinstance(block['best_selected'], int) \
                or not isinstance(block['best_penalty'], int) \
                or block['best_selected'] < 0 or block['best_penalty'] < 0:
            raise ValueError('best indicators must be non-negative integers')
        representative_seq = block['representative']
        if not isinstance(representative_seq, list) or len(representative_seq) != len(order):
            raise ValueError('representative must have one slot per analyte')
        attainable_block = block['attainable']
        if not isinstance(attainable_block, list) or len(attainable_block) != len(order):
            raise ValueError('attainable must have one slot per analyte')

        options = [grouped.get(run_id, {}).get(analyte_id, []) for analyte_id in order]
        count, penalty = best_metrics(options)
        if count != block['best_selected'] or penalty != block['best_penalty']:
            raise ValueError(f'best indicators for run {run_id} do not match its candidates')
        expected_witness = representative(options)
        expected_seq = [None if option is None else option['id'] for option in expected_witness]
        if expected_seq != representative_seq:
            raise ValueError(f'representative explanation for run {run_id} is corrupt')
        expected_attainable = attainable(options)
        for position, analyte_id in enumerate(order):
            slot = attainable_block[position]
            if not isinstance(slot, dict) or set(slot) != {
                    'analyte_id', 'candidate_ids', 'vacancy_available'}:
                raise ValueError('corrupt attainable slot')
            if slot['analyte_id'] != analyte_id:
                raise ValueError('attainable analyte order is corrupt')
            kept_ids, vacancy_ok = expected_attainable[position]
            if not isinstance(slot['candidate_ids'], list) \
                    or set(slot['candidate_ids']) != kept_ids \
                    or len(slot['candidate_ids']) != len(set(slot['candidate_ids'])):
                raise ValueError(f'attainable candidate ids for {run_id}/{analyte_id} are corrupt')
            if not isinstance(slot['vacancy_available'], bool) \
                    or slot['vacancy_available'] != vacancy_ok:
                raise ValueError(f'attainable vacancy flag for {run_id}/{analyte_id} is corrupt')
            known = registered.get((run_id, analyte_id), set())
            for candidate_id in slot['candidate_ids']:
                ident(candidate_id)
                if candidate_id not in known:
                    raise ValueError('attainable id is not registered')
            chosen = representative_seq[position]
            if chosen is not None and chosen not in known:
                raise ValueError('representative id is not registered')
        for analyte_id, option_list in grouped.get(run_id, {}).items():
            for option in option_list:
                if option['lo'] < first or option['hi'] > last:
                    raise ValueError('candidate window outside the recorded sample range')
    # Every candidate must belong to a run block.
    orphan = set(grouped) - run_ids
    if orphan:
        raise ValueError(f'candidates reference runs without a run block: {sorted(orphan)}')
    return data


def parse_query(data, package):
    """Validate a contrast query against an already validated package."""
    if not isinstance(data, dict) or set(data) != {'pins', 'run_id', 'terms'}:
        raise ValueError('query fields must be exactly run_id, terms, pins')
    run_id = ident(data['run_id'])
    run_ids = {block['run_id'] for block in package['runs']}
    if run_id not in run_ids:
        raise ValueError(f'run_id {run_id} is not an unknown run in the package')
    order = package['analyte_order']
    analyte_index = {analyte_id: i for i, analyte_id in enumerate(order)}

    terms_raw = data['terms']
    if not isinstance(terms_raw, dict) or not terms_raw:
        raise ValueError('terms must be a non-empty object keyed by analyte id')
    terms = {}
    for analyte_id, coefficient in terms_raw.items():
        ident(analyte_id)
        if analyte_id not in analyte_index:
            raise ValueError(f'term analyte {analyte_id} does not exist in the package')
        if _is_bool(coefficient) or not isinstance(coefficient, int) \
                or not _COEFF_MIN <= coefficient <= _COEFF_MAX or coefficient == 0:
            raise ValueError('term coefficients must be non-zero integers in -1000..1000')
        terms[analyte_id] = coefficient

    pins_raw = data['pins']
    if not isinstance(pins_raw, dict):
        raise ValueError('pins must be an object keyed by analyte id')
    pins = {}
    registered = {}
    for entry in package['candidates']:
        if entry['run_id'] == run_id:
            registered.setdefault(entry['analyte_id'], set()).add(entry['candidate_id'])
    for analyte_id, candidate_id in pins_raw.items():
        ident(analyte_id)
        if analyte_id not in analyte_index:
            raise ValueError(f'pin analyte {analyte_id} does not exist in the package')
        if candidate_id is None:
            pins[analyte_id] = None
            continue
        ident(candidate_id)
        known = registered.get(analyte_id, set())
        if candidate_id not in known:
            raise ValueError(
                f'pin {candidate_id} is not registered for run {run_id}, analyte {analyte_id}')
        pins[analyte_id] = candidate_id
    return run_id, terms, pins


def _load_package(path):
    raw = read_limited(path, PACKAGE_BYTES_LIMIT)
    data = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    return validate_package(data)


def answer_query(package, query_data):
    """Compute the contrast report dict from validated package and raw query."""
    run_id, terms, pins = parse_query(query_data, package)
    order = package['analyte_order']
    analyte_index = {analyte_id: i for i, analyte_id in enumerate(order)}
    run_block = next(block for block in package['runs'] if block['run_id'] == run_id)
    grouped = {}
    window_text = {}
    for entry in package['candidates']:
        if entry['run_id'] != run_id:
            continue
        option = {
            'id': entry['candidate_id'],
            'lo': number(entry['window'][0]),
            'hi': number(entry['window'][1]),
            'penalty': entry['penalty'],
            'concentration': _exact(entry['concentration'], 'concentration'),
        }
        grouped.setdefault(entry['analyte_id'], []).append(option)
        # Ids are unique per run/analyte, not across analytes: key both.
        window_text[(entry['analyte_id'], entry['candidate_id'])] = \
            list(entry['window'])
    options = [grouped.get(analyte_id, []) for analyte_id in order]
    position_pins = {analyte_index[analyte_id]: candidate_id
                     for analyte_id, candidate_id in pins.items()}
    term_positions = {analyte_index[analyte_id] for analyte_id in terms}
    coefficients = {analyte_index[analyte_id]: coefficient
                    for analyte_id, coefficient in terms.items()}

    def contribution(position, candidate):
        return Fraction(coefficients[position]) * candidate['concentration']

    extrema = constrained_extrema(options, position_pins, term_positions, contribution)
    if extrema is None:
        result = {'status': 'no_joint_explanation', 'maximum': None, 'minimum': None}
    else:
        (minimum, minimum_witness), (maximum, maximum_witness) = extrema
        result = {
            'status': 'joint_explanation',
            'minimum': {
                'score': str(minimum),
                'witness': _witness_entries(order, minimum_witness, terms, window_text),
            },
            'maximum': {
                'score': str(maximum),
                'witness': _witness_entries(order, maximum_witness, terms, window_text),
            },
        }
    return {
        'best_penalty': run_block['best_penalty'],
        'best_selected': run_block['best_selected'],
        'package_version': PACKAGE_VERSION,
        'pins': {analyte_id: pins[analyte_id] for analyte_id in sorted(pins)},
        'result': result,
        'run_id': run_id,
        'terms': {analyte_id: terms[analyte_id] for analyte_id in sorted(terms)},
    }


def _witness_entries(order, witness, terms, window_text):
    entries = []
    for analyte_id, option in zip(order, witness):
        if option is None:
            entries.append({
                'analyte_id': analyte_id,
                'candidate_id': None,
                'concentration': None,
                'included_in_score': False,
                'penalty': None,
                'selected': False,
                'window': None,
            })
        else:
            entries.append({
                'analyte_id': analyte_id,
                'candidate_id': option['id'],
                'concentration': str(option['concentration']),
                'included_in_score': analyte_id in terms,
                'penalty': option['penalty'],
                'selected': True,
                'window': list(window_text[(analyte_id, option['id'])]),
            })
    return entries


def load_query(path):
    raw = read_limited(path, QUERY_BYTES_LIMIT)
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)


def encode_report(report):
    return (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + '\n').encode('utf-8')


def contrast(package_path, query_path):
    package = _load_package(package_path)
    query_data = load_query(query_path)
    return answer_query(package, query_data)
