"""Release request parsing, strict validation and canonical identity."""
import json
from .checks import fields, ident, number
from .io import constant, pairs, read_limited

REQUEST_LIMIT = 8000000
_SHA256 = set('0123456789abcdef')
_IDENTITY_FIELDS = ('batch_id', 'request_id', 'expected_revision', 'source_sha256')


def load_request(path):
    """Read and JSON-decode a release request with the same strict rules as v1 input."""
    raw = read_limited(path, REQUEST_LIMIT)
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)


def _bounded_nonnegative(value):
    if not isinstance(value, str):
        raise ValueError('bounded decimal string required')
    # reuse the v1 bounded-decimal grammar (which also accepts a leading minus)
    parsed = number(value)
    if parsed < 0:
        raise ValueError('negative control value')
    return parsed


def _pair_list(value, name):
    if not isinstance(value, list):
        raise ValueError(f'{name} must be a list of [run_id, analyte_id] pairs')
    result = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError(f'{name} entries must be [run_id, analyte_id] pairs')
        run_id, analyte_id = item
        ident(run_id)
        ident(analyte_id)
        pair = [run_id, analyte_id]
        if pair in result:
            raise ValueError(f'duplicate {name} entry')
        result.append(pair)
    return result


def parse_request(data):
    """Validate every structural and value constraint that does not need INPUT.

    Returns a normalized request dict. Review lists are sorted into a canonical
    order; control decimal strings are normalized to exact Fraction strings so
    numerically equal inputs share one identity.
    """
    fields(data, ('batch_id', 'request_id', 'expected_revision', 'source_sha256',
                  'controls', 'excluded_standards', 'invalid_controls'))
    batch_id = ident(data['batch_id'])
    request_id = ident(data['request_id'])
    expected = data['expected_revision']
    if type(expected) is not int or isinstance(expected, bool) or expected < 0:
        raise ValueError('expected_revision must be a non-negative JSON integer')
    digest = data['source_sha256']
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in _SHA256 for c in digest):
        raise ValueError('source_sha256 must be 64 lowercase hex characters')
    controls_raw = data['controls']
    if not isinstance(controls_raw, dict):
        raise ValueError('controls must be an object keyed by run id')
    controls = {}
    for run_id, entry in controls_raw.items():
        ident(run_id)
        if not isinstance(entry, dict) or not entry:
            raise ValueError('each control entry must be a non-empty analyte object')
        targets = {}
        for analyte_id, spec in entry.items():
            ident(analyte_id)
            fields(spec, ('target', 'tolerance'))
            target = _bounded_nonnegative(spec['target'])
            tolerance = _bounded_nonnegative(spec['tolerance'])
            if analyte_id in targets:
                raise ValueError('duplicate control analyte registration')
            targets[analyte_id] = {'target': str(target), 'tolerance': str(tolerance)}
        controls[run_id] = targets
    excluded = _pair_list(data['excluded_standards'], 'excluded_standards')
    invalid = _pair_list(data['invalid_controls'], 'invalid_controls')
    return {
        'batch_id': batch_id,
        'request_id': request_id,
        'expected_revision': expected,
        'source_sha256': digest,
        'controls': controls,
        'excluded_standards': sorted(excluded),
        'invalid_controls': sorted(invalid),
    }


def load_and_parse(path):
    return parse_request(load_request(path))


def canonical_identity(request):
    """Deterministic identity: whitespace, key order and review-list order ignored.

    Control numeric equality counts as equality (values are already normalized
    to exact Fraction strings). All other identity fields participate,
    including expected_revision; the source fingerprint is the hash of the
    raw INPUT bytes, never of a re-serialized document.
    """
    controls = {
        run_id: {
            analyte_id: {'target': spec['target'], 'tolerance': spec['tolerance']}
            for analyte_id, spec in sorted(entry.items())
        }
        for run_id, entry in sorted(request['controls'].items())
    }
    payload = {name: request[name] for name in _IDENTITY_FIELDS}
    payload['controls'] = controls
    payload['excluded_standards'] = sorted(request['excluded_standards'])
    payload['invalid_controls'] = sorted(request['invalid_controls'])
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False)
