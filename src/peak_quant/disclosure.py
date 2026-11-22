"""Offline review of aggregate disclosures over one fixed synthetic snapshot.

The review model is deliberately simple and explicit.  Every detail value is an
independent rational unknown to the receiver.  A published aggregate row is an
equation: the equal-weight sum of its named members equals its published total.
Membership is public; no real values, non-negativity, calibration ranges, other
data sources or cross-analyte assumptions ever enter the model.

A protected (run, analyte) unit is *determined* by the published equations when
its detail variable is forced to one value in every rational assignment
consistent with those equations.  Exact rational Gaussian elimination decides
this over the complete per-analyte column universe (every protected unit and
every row member registered up front): in reduced row echelon form the free
columns parametrize the solution space, and a unit is determined exactly when
no null-space direction can move it.

Two commands live here:

* ``disclosure``       -- run the priority-ordered review against a legal v1
  source, write a bundle containing ``public.json`` (the rows actually issued)
  and ``audit.json`` (decisions plus independently re-checkable proofs);
* ``disclosure-check`` -- re-verify every decision and proof from PLAN plus the
  two products alone, with no source, cache or running fit available.

All arithmetic is exact ``fractions.Fraction``.  Nothing here ever touches real
substances, instruments, experiments, networks or attack scenarios.
"""
import json
import os
import re
import shutil
import tempfile
from fractions import Fraction

from .checks import fields, ident
from .io import constant, pairs, read_limited
from .service import process

PLAN_BYTES_LIMIT = 2_000_000
PRODUCT_BYTES_LIMIT = 16_000_000
MAX_REVIEW_LINES = 512
PUBLIC_FORMAT_VERSION = 1
AUDIT_FORMAT_VERSION = 1

# Exact rational strings: an optional sign, then an integer, optionally divided
# by a positive integer.  Reducedness and the digit budget are checked after
# parsing so spellings such as "2/2", "0/3" or "-0" cannot sneak through.
_RATIONAL_RE = re.compile(r'-?[0-9]+(?:/[0-9]+)?\Z')
_RATIONAL_DIGIT_LIMIT = 40
_RATIONAL_TEXT_LIMIT = 128


class DisclosureError(ValueError):
    """Every rejection names the disclosure object and the offending part."""


# ----------------------------------------------------------- exact rationals

def parse_rational(value, label):
    """Parse a reduced integer/fraction string with explicit size checks.

    Accepted grammar: an optional minus sign followed by digits, optionally
    followed by ``/`` and a positive-digit denominator.  The fraction must be
    reduced (``Fraction`` spelling), the denominator must be positive, each
    component may carry at most 40 digits and the whole string at most 128
    characters.  JSON numbers are never accepted: exact text is the contract.
    """
    if not isinstance(value, str):
        raise DisclosureError(f'{label} must be an exact integer/fraction string')
    if len(value) > _RATIONAL_TEXT_LIMIT or not _RATIONAL_RE.fullmatch(value):
        raise DisclosureError(
            f'{label} must match the optional-sign integer/fraction grammar '
            f'(at most {_RATIONAL_DIGIT_LIMIT} digits per component)')
    if '/' in value:
        numerator_text, denominator_text = value.split('/')
        if denominator_text.startswith('0') and denominator_text != '0':
            raise DisclosureError(f'{label} denominator must not have leading zeros')
        if int(denominator_text) <= 0:
            raise DisclosureError(f'{label} denominator must be positive')
        digits = [numerator_text.lstrip('-'), denominator_text]
    else:
        digits = [value.lstrip('-')]
    if any(len(part) > _RATIONAL_DIGIT_LIMIT for part in digits):
        raise DisclosureError(
            f'{label} component exceeds {_RATIONAL_DIGIT_LIMIT} digits')
    result = Fraction(value)
    if str(result) != value:
        raise DisclosureError(f'{label} must be a reduced integer/fraction string')
    return result


def _rational_text(value):
    """Canonical reduced spelling; Fraction strings already satisfy the caps
    for values derived from bounded v1 decimals and bounded plan inputs."""
    text = str(value)
    if len(text) > _RATIONAL_TEXT_LIMIT:
        raise DisclosureError('exact value exceeds the rational text limit')
    return text


# ------------------------------------------------------------------- plan IO

def load_plan(path):
    raw = read_limited(path, PLAN_BYTES_LIMIT)
    data = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                      parse_constant=constant)
    return parse_plan(data)


def _review_line(entry, expect_total, label):
    if not isinstance(entry, dict):
        raise DisclosureError(f'{label} entry must be an object')
    required = ('id', 'analyte_id', 'members') + (('total',) if expect_total else ())
    fields(entry, required)
    row_id = ident(entry['id'])
    analyte_id = ident(entry['analyte_id'])
    members_raw = entry['members']
    if not isinstance(members_raw, list) or not members_raw:
        raise DisclosureError(f'{label} line {row_id}: members must be a non-empty array')
    if len(members_raw) > 200:
        raise DisclosureError(f'{label} line {row_id}: at most 200 members')
    members = []
    for run_id in members_raw:
        members.append(ident(run_id))
    if len(set(members)) != len(members):
        raise DisclosureError(f'{label} line {row_id}: members must be unique')
    total = parse_rational(entry['total'], f'{label} line {row_id} total') \
        if expect_total else None
    return {'id': row_id, 'analyte_id': analyte_id,
            'members': tuple(sorted(members)), 'total': total}


def parse_plan(data):
    """Validate every source-independent PLAN rule and normalize ordering.

    The request array keeps its order because it is the business priority;
    every other array (history, protected list, members) is sorted into a
    canonical order since the contract allows those reorderings.
    """
    if not isinstance(data, dict):
        raise DisclosureError('plan top level must be an object')
    fields(data, ('protected', 'published', 'requests'))

    protected_raw = data['protected']
    if not isinstance(protected_raw, list) or not protected_raw:
        raise DisclosureError('protected must be a non-empty array')
    protected = []
    for pair in protected_raw:
        if not isinstance(pair, list) or len(pair) != 2:
            raise DisclosureError('protected entries must be [run_id, analyte_id] pairs')
        run_id, analyte_id = pair
        protected.append([ident(run_id), ident(analyte_id)])
    protected.sort()
    if any(left == right for left, right in zip(protected, protected[1:])):
        raise DisclosureError('protected entries must be unique')
    protected_set = {tuple(pair) for pair in protected}

    published_raw = data['published']
    requests_raw = data['requests']
    if not isinstance(published_raw, list) or len(published_raw) > MAX_REVIEW_LINES:
        raise DisclosureError(f'published must be an array of at most {MAX_REVIEW_LINES} lines')
    if not isinstance(requests_raw, list) or not requests_raw \
            or len(requests_raw) > MAX_REVIEW_LINES:
        raise DisclosureError(
            f'requests must be a non-empty array of at most {MAX_REVIEW_LINES} lines')
    published = [_review_line(entry, True, 'published') for entry in published_raw]
    # Request order is deliberately preserved: it is the review priority.
    requests = [_review_line(entry, False, 'requests') for entry in requests_raw]

    all_ids = [row['id'] for row in published] + [row['id'] for row in requests]
    if len(set(all_ids)) != len(all_ids):
        raise DisclosureError('row ids must be unique across published and requests')
    request_ids = [row['id'] for row in requests]
    if len(set(request_ids)) != len(request_ids):
        raise DisclosureError('request ids must be unique')

    return {'protected': protected, 'protected_set': protected_set,
            'published': published, 'requests': requests}


def _quantified_source(source_data):
    """Run the unchanged v1 pipeline; any fit or validation failure propagates."""
    report = process(source_data)
    concentrations = {}
    for entry in report['quantified']:
        concentrations[(entry['run_id'], entry['analyte_id'])] = \
            Fraction(entry['concentration'])
    unknown_runs = {run['id'] for run in source_data['runs']
                    if run['kind'] == 'unknown'}
    analyte_ids = {analyte['id'] for analyte in source_data['analytes']}
    return concentrations, unknown_runs, analyte_ids


def validate_plan_against_source(plan, source_data):
    """Check every PLAN reference against a validated v1 source and verify the
    historical totals against the fixed snapshot's exact concentrations."""
    concentrations, unknown_runs, analyte_ids = _quantified_source(source_data)

    def check_row(row, label):
        if row['analyte_id'] not in analyte_ids:
            raise DisclosureError(
                f"{label} line {row['id']} references unknown analyte {row['analyte_id']}")
        for run_id in row['members']:
            if run_id not in unknown_runs:
                raise DisclosureError(
                    f"{label} line {row['id']} member {run_id} must be an unknown run; "
                    'standards cannot be aggregate members')
            if (run_id, row['analyte_id']) not in concentrations:
                raise DisclosureError(
                    f"{label} line {row['id']} member {run_id} has no quantified result")

    for run_id, analyte_id in plan['protected']:
        if run_id not in unknown_runs:
            raise DisclosureError(f'protected unit [{run_id}, {analyte_id}] must be an unknown run')
        if analyte_id not in analyte_ids:
            raise DisclosureError(f'protected unit [{run_id}, {analyte_id}] analyte does not exist')
        if (run_id, analyte_id) not in concentrations:
            raise DisclosureError(f'protected unit [{run_id}, {analyte_id}] is not quantified')
    for row in plan['published']:
        check_row(row, 'published')
        actual = sum(concentrations[(run_id, row['analyte_id'])]
                     for run_id in row['members'])
        if actual != row['total']:
            raise DisclosureError(
                f"published line {row['id']} total {row['total']} does not match the "
                f'fixed snapshot total {_rational_text(actual)}')
    for row in plan['requests']:
        check_row(row, 'requests')
    return concentrations


# ------------------------------------------------------- rational row echelon

class _Rref:
    """Incremental reduced row echelon form over exact rationals.

    The full column universe -- every unit that can ever appear, including
    protected units -- is registered up front and only grows by explicit
    extension, never implicitly while reducing, because pivot status is only
    meaningful against the whole universe.  Rows stay sparse unit->coefficient
    dicts and every pivot row remembers the original-row combination producing
    it, so a determination can always be exported as a re-checkable proof.
    A unit is determined only when no null-space direction moves it, tested
    directly via ``determined`` rather than inferred from pivot membership.
    """

    def __init__(self, universe=()):
        self.universe = sorted(universe)
        self.unit_set = set(self.universe)
        self.rows = []          # list of dict(unit -> Fraction), in RREF
        self.basis = []         # list of dict(row_id -> Fraction), same order
        self.pivots = set()     # pivot units

    def extend_universe(self, units):
        for unit in units:
            if unit not in self.unit_set:
                self.unit_set.add(unit)
                # Insertion keeps the lexicographic column order; sparse rows do
                # not store zeros, so existing rows need no adjustment.
                self.universe.append(unit)
        self.universe.sort()
    def reduce(self, vector):
        """Return (residual unit->Fraction dict, original-row combination).

        Invariant: vector == residual + sum(combination[row] * original row),
        so subtracting factor times a stored row subtracts the same factor from
        the residual's original-row combination.
        """
        residual = dict(vector)
        combination = {}
        for index, row in enumerate(self.rows):
            pivot = min(row)
            factor = residual.get(pivot)
            if not factor:
                continue
            for unit, value in row.items():
                residual[unit] = residual.get(unit, Fraction(0)) - factor * value
                if residual[unit] == 0:
                    del residual[unit]
            for row_id, coefficient in self.basis[index].items():
                combination[row_id] = combination.get(row_id, Fraction(0)) \
                    - factor * coefficient
                if combination[row_id] == 0:
                    del combination[row_id]
        return residual, combination

    def add_history(self, vector, origin_id):
        """Insert a historical row, tolerating linear dependence.

        A dependent historical equation is still a published fact but contributes
        no new basis row, so the reduced system simply ignores it.
        """
        self.extend_universe(vector)
        residual, combination = self.reduce(vector)
        if not residual:
            return
        combination[origin_id] = combination.get(origin_id, Fraction(0)) + 1
        self.add_residual(residual, combination)

    def free_units(self):
        """Columns without a pivot in the current reduced system."""
        return [unit for unit in self.universe if unit not in self.pivots]

    def determined(self, unit):
        """A unit is determined iff no null-space direction moves it.

        In RREF every solution is parametrized by the free columns.  A pivot
        unit is fixed exactly when its pivot row has zero coefficient on every
        free column; a non-pivot (free) unit is never determined.
        """
        if unit not in self.pivots:
            return False
        row = next(row for row in self.rows if min(row) == unit)
        return all(not row.get(free_unit) for free_unit in self.free_units())

    def trial_clone(self, residual, origin_id, combination):
        """An engine clone with the reduced residual inserted as a new row."""
        clone = _Rref(self.universe)
        clone.unit_set = set(self.unit_set)
        clone.rows = [dict(row) for row in self.rows]
        clone.basis = [dict(basis) for basis in self.basis]
        clone.pivots = set(self.pivots)
        if not residual:
            return clone
        trial_basis = dict(combination)
        trial_basis[origin_id] = trial_basis.get(origin_id, Fraction(0)) + 1
        clone.add_residual(residual, trial_basis)
        return clone

    def add_residual(self, residual, basis):
        """Normalize an already-reduced residual into a new RREF pivot row.

        ``basis`` maps original row ids to the coefficients producing the
        residual (the new original row already included with coefficient 1);
        it is normalized and carried through elimination so every pivot row
        stays expressible as a combination of real original aggregate rows.
        """
        pivot = min(residual)
        scale = residual[pivot]
        new_row = {unit: value / scale for unit, value in residual.items()}
        new_basis = {row_id: coefficient / scale
                     for row_id, coefficient in basis.items()}
        for index in range(len(self.rows)):
            row = self.rows[index]
            factor = row.get(pivot)
            if not factor:
                continue
            for unit, value in new_row.items():
                row[unit] = row.get(unit, Fraction(0)) - factor * value
                if row[unit] == 0:
                    del row[unit]
            for row_id, coefficient in new_basis.items():
                self.basis[index][row_id] = \
                    self.basis[index].get(row_id, Fraction(0)) - factor * coefficient
                if self.basis[index][row_id] == 0:
                    del self.basis[index][row_id]
        self.rows.append(new_row)
        self.basis.append(new_basis)
        self.pivots.add(pivot)


def _row_vector(row):
    return {(run_id, row['analyte_id']): Fraction(1) for run_id in row['members']}


# ----------------------------------------------------------------- review run

def run_review(plan, concentrations):
    """Execute the priority-ordered review and build both product documents.

    Historical rows enter the equation set first.  If history alone determines
    any protected unit, the whole generation fails: withholding current rows
    cannot repair an already lost protection.
    """
    engines = {}
    history_ids = set()
    # Register the full column universe first: every protected unit and every
    # member of every historical and requested row, per analyte.  A protected
    # unit that no equation mentions still occupies a free column, so a later
    # row pivoting it must be withheld even though the unit is not a member.
    universes = {}
    for run_id, analyte_id in plan['protected']:
        universes.setdefault(analyte_id, set()).add((run_id, analyte_id))
    for row in plan['published'] + plan['requests']:
        bucket = universes.setdefault(row['analyte_id'], set())
        for run_id in row['members']:
            bucket.add((run_id, row['analyte_id']))
    for analyte_id, units in universes.items():
        engines[analyte_id] = _Rref(units)
    for row in plan['published']:
        # Historical rows may be linearly dependent (e.g. the same membership
        # published twice under distinct ids); only independent equations enter
        # the elimination basis, both facts still count as published totals.
        engines[row['analyte_id']].add_history(_row_vector(row), row['id'])
        history_ids.add(row['id'])

    protected_by_analyte = {}
    for run_id, analyte_id in plan['protected']:
        protected_by_analyte.setdefault(analyte_id, set()).add((run_id, analyte_id))
    for run_id, analyte_id in plan['protected']:
        engine = engines.get(analyte_id)
        if engine is not None and engine.determined((run_id, analyte_id)):
            raise DisclosureError(
                f'historical rows already determine protected unit [{run_id}, {analyte_id}]; '
                'the disclosure cannot proceed')

    decisions = []
    issued_rows = []
    for row in plan['requests']:
        engine = engines[row['analyte_id']]
        vector = _row_vector(row)
        residual, combination = engine.reduce(vector)
        # The trial row is withheld exactly when adding it would determine any
        # protected unit of this analyte.  A unit outside the residual can still
        # become determined indirectly, so every protected unit is tested on
        # the same simulated clone.
        clone = engine.trial_clone(residual, row['id'], combination)
        endangered = sorted(
            unit for unit in protected_by_analyte.get(row['analyte_id'], set())
            if clone.determined(unit))
        if endangered:
            if not residual:
                # A dependent row adds no equation; it cannot newly determine a
                # unit, so this state means an earlier step already failed its
                # protection invariant.
                raise DisclosureError(
                    f'internal review error: dependent row {row["id"]} cannot endanger protection')
            target = endangered[0]
            # The clone's pivot row for the target is a combination of original
            # rows equal to the target's unit vector, and the trial row always
            # participates in it because its residual was independent.
            pivot_index = next(index for index, pivot_row in enumerate(clone.rows)
                               if min(pivot_row) == target)
            proof_coefficients = dict(clone.basis[pivot_index])
            if row['id'] not in proof_coefficients:
                raise DisclosureError(
                    f'internal review error: withholding proof for {row["id"]} '
                    'does not use the withheld row')
            decisions.append({
                'id': row['id'],
                'decision': 'withheld',
                'proof': {'protected_unit': target,
                          'coefficients': proof_coefficients},
            })
            continue
        if residual:
            engine.add_residual(
                residual, {**combination,
                           row['id']: combination.get(row['id'], Fraction(0)) + 1})
        # A zero residual means a safe redundant row: it is still issued, but
        # its equation adds nothing to the set used by later decisions.
        total = sum(concentrations[(run_id, row['analyte_id'])]
                    for run_id in row['members'])
        issued_rows.append({**row, 'total': total})
        decisions.append({'id': row['id'], 'decision': 'issued', 'proof': None})

    freedom_proofs = _freedom_proofs(plan, engines, history_ids, issued_rows)
    return _build_documents(plan, decisions, issued_rows, freedom_proofs)


def _freedom_proofs(plan, engines, history_ids, issued_rows):
    """For every protected unit, exhibit a rational change direction that moves
    that unit while leaving every published and issued equation unchanged.

    The RREF null space supplies one direction per free column; the free column
    of the protected unit itself always gives a direction whose coefficient on
    that unit is one.  This also covers every shorter issued prefix, because a
    direction orthogonal to the final (largest) equation set is orthogonal to
    every subset of it.
    """
    rows_by_analyte = {}
    for row in plan['published'] + issued_rows:
        rows_by_analyte.setdefault(row['analyte_id'], []).append(row)

    proofs = []
    for run_id, analyte_id in plan['protected']:
        unit = (run_id, analyte_id)
        engine = engines.get(analyte_id)
        universe = {unit}
        for row in rows_by_analyte.get(analyte_id, ()):
            universe.update(_row_vector(row))
        if engine is not None:
            engine.extend_universe(universe)
        direction = _null_direction(engine, unit) if engine is not None \
            else {unit: Fraction(1)}
        if direction.get(unit, Fraction(0)) == 0:
            raise DisclosureError(f'internal review error: zero direction for {unit}')
        # Independently verify orthogonality against every equation.
        for row in rows_by_analyte.get(analyte_id, ()):
            check = sum(direction.get((member, analyte_id), Fraction(0))
                        for member in row['members'])
            if check != 0:
                raise DisclosureError(
                    f'internal review error: freedom direction changes row {row["id"]}')
        proofs.append({'protected_unit': [run_id, analyte_id],
                       'changes': {pair: value for pair, value in direction.items()
                                   if value != 0}})
    return proofs


def _null_direction(engine, unit):
    """Build a null-space vector whose coefficient on ``unit`` is non-zero.

    For a free column this is its canonical null-space basis vector (one on the
    free unit, minus the RREF entries on pivot units).  For a pivot column that
    remains undetermined because a free column precedes it, the canonical
    vector of the earliest free column reaching its pivot row is used.
    """
    if unit not in engine.pivots:
        return _free_column_direction(engine, unit)
    for free_unit in engine.free_units():
        direction = _free_column_direction(engine, free_unit)
        if direction.get(unit, Fraction(0)) != 0:
            return direction
    raise DisclosureError(
        f'internal review error: no null direction reaches protected unit {unit}')


def _free_column_direction(engine, free_unit):
    """Canonical null-space basis vector for one free column."""
    direction = {free_unit: Fraction(1)}
    for row in engine.rows:
        value = row.get(free_unit)
        if value:
            direction[min(row)] = direction.get(min(row), Fraction(0)) - value
    return direction


def _build_documents(plan, decisions, issued_rows, freedom_proofs):
    public = {
        'format_version': PUBLIC_FORMAT_VERSION,
        'issued': [
            {'id': row['id'], 'analyte_id': row['analyte_id'],
             'members': list(row['members']),
             'total': _rational_text(row['total'])}
            for row in issued_rows
        ],
    }
    request_proofs = []
    for decision in decisions:
        entry = {'id': decision['id'], 'decision': decision['decision']}
        if decision['proof'] is None:
            entry['withholding_proof'] = None
        else:
            proof = decision['proof']
            entry['withholding_proof'] = {
                'protected_unit': proof['protected_unit'],
                'combination': [
                    {'row_id': row_id, 'coefficient': _rational_text(coefficient)}
                    for row_id, coefficient in sorted(proof['coefficients'].items())
                ],
            }
        request_proofs.append(entry)
    audit = {
        'format_version': AUDIT_FORMAT_VERSION,
        'requests': request_proofs,
        'freedom_proofs': [
            {'protected_unit': proof['protected_unit'],
             'changes': [
                 {'unit': list(unit), 'change': _rational_text(value)}
                 for unit, value in sorted(proof['changes'].items())
             ]}
            for proof in sorted(freedom_proofs,
                                key=lambda item: item['protected_unit'])
        ],
    }
    return public, audit


def encode_document(document):
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + '\n').encode('utf-8')


def build_bundle_documents(source_data, plan):
    """Full generation path used by the CLI and by tests."""
    concentrations = validate_plan_against_source(plan, source_data)
    return run_review(plan, concentrations)


def write_bundle(bundle_dir, public_bytes, audit_bytes):
    """Deliver both products visibly at once.

    The directory is staged under a hidden sibling name and renamed into place
    only after both files are durably written, so an observer can never see a
    bundle with just one product and a failed second write leaves neither the
    target directory nor debris that could be mistaken for success.  An
    existing target (including a symlink) is never replaced or touched.
    """
    target = os.fspath(bundle_dir)
    if os.path.exists(target) or os.path.islink(target):
        raise FileExistsError(f'disclosure bundle already exists: {target}')
    parent = os.path.dirname(os.path.abspath(target)) or '.'
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix='.disclosure-', dir=parent)
    try:
        for name, payload in (('public.json', public_bytes),
                              ('audit.json', audit_bytes)):
            destination = os.path.join(staging, name)
            # Exclusive create inside the fresh staging directory.
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, 'wb') as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                os.unlink(destination)
                raise
        os.replace(staging, target)
        staging = None
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def disclosure(source_path, plan_path, bundle_dir):
    from .io import load
    plan = load_plan(plan_path)
    source_data = load(source_path)
    public, audit = build_bundle_documents(source_data, plan)
    write_bundle(bundle_dir, encode_document(public), encode_document(audit))
    return public, audit


# --------------------------------------------------------------- verification

def _load_product(path):
    raw = read_limited(path, PRODUCT_BYTES_LIMIT)
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                      parse_constant=constant)


def verify_disclosure(plan, public_data, audit_data):
    """Independently verify the two untrusted products against a trusted PLAN.

    No source is available: public totals are checked for exact-value format
    only, never recomputed.  Every proof equation is re-derived coefficient by
    coefficient; labels and counts alone never pass.
    """
    if not isinstance(public_data, dict) or set(public_data) != \
            {'format_version', 'issued'}:
        raise DisclosureError('public.json must contain exactly format_version and issued')
    if public_data['format_version'] != PUBLIC_FORMAT_VERSION:
        raise DisclosureError(
            f'unsupported public format version {public_data["format_version"]}')
    if not isinstance(audit_data, dict) or set(audit_data) != \
            {'format_version', 'freedom_proofs', 'requests'}:
        raise DisclosureError(
            'audit.json must contain exactly format_version, requests and freedom_proofs')
    if audit_data['format_version'] != AUDIT_FORMAT_VERSION:
        raise DisclosureError(
            f'unsupported audit format version {audit_data["format_version"]}')

    plan_requests = plan['requests']
    plan_published = plan['published']
    row_by_id = {row['id']: row for row in plan_published + plan_requests}
    history_ids = {row['id'] for row in plan_published}
    protected_set = plan['protected_set']

    issued_public = public_data['issued']
    if not isinstance(issued_public, list):
        raise DisclosureError('public issued must be an array')
    public_ids = []
    for entry in issued_public:
        if not isinstance(entry, dict) or set(entry) != \
                {'id', 'analyte_id', 'members', 'total'}:
            raise DisclosureError('corrupt public issued entry')
        row_id = ident(entry['id'])
        if row_id not in row_by_id or row_id in history_ids:
            raise DisclosureError(f'public row {row_id} is not a request line')
        plan_row = row_by_id[row_id]
        analyte_id = ident(entry['analyte_id'])
        if analyte_id != plan_row['analyte_id']:
            raise DisclosureError(f'public row {row_id} analyte does not match PLAN')
        if not isinstance(entry['members'], list) or not entry['members'] \
                or len(entry['members']) != len(set(entry['members'])):
            raise DisclosureError(f'public row {row_id} members must be unique and non-empty')
        members = tuple(sorted(ident(run_id) for run_id in entry['members']))
        if members != plan_row['members']:
            raise DisclosureError(f'public row {row_id} members do not match PLAN')
        parse_rational(entry['total'], f'public row {row_id} total')
        public_ids.append(row_id)

    audit_requests = audit_data['requests']
    if not isinstance(audit_requests, list) \
            or len(audit_requests) != len(plan_requests):
        raise DisclosureError('audit requests must align one-to-one with PLAN requests')
    audit_decisions = []
    for index, (entry, plan_row) in enumerate(zip(audit_requests, plan_requests)):
        if not isinstance(entry, dict) or set(entry) != \
                {'id', 'decision', 'withholding_proof'}:
            raise DisclosureError('corrupt audit request entry')
        if ident(entry['id']) != plan_row['id']:
            raise DisclosureError(
                f'audit request position {index} must be {plan_row["id"]} in priority order')
        decision = entry['decision']
        if decision not in ('issued', 'withheld'):
            raise DisclosureError(f'audit decision for {plan_row["id"]} is invalid')
        proof = entry['withholding_proof']
        if decision == 'issued':
            if proof is not None:
                raise DisclosureError(f'issued row {plan_row["id"]} must carry no withholding proof')
        else:
            _check_withholding_proof(plan_row, proof, audit_decisions, row_by_id,
                                     history_ids, protected_set)
        audit_decisions.append((plan_row, decision))

    audit_issued_ids = [row['id'] for row, decision in audit_decisions
                        if decision == 'issued']
    if audit_issued_ids != public_ids:
        raise DisclosureError(
            'public rows must be exactly the audit-issued requests in priority order')

    _check_freedom_proofs(plan, audit_data['freedom_proofs'], row_by_id,
                          history_ids, audit_issued_ids, protected_set)
    return {'requests': len(plan_requests), 'issued': len(audit_issued_ids),
            'withheld': len(plan_requests) - len(audit_issued_ids)}


def _check_withholding_proof(plan_row, proof, earlier_decisions, row_by_id,
                             history_ids, protected_set):
    if not isinstance(proof, dict) or set(proof) != {'protected_unit', 'combination'}:
        raise DisclosureError(
            f'withheld row {plan_row["id"]} must carry a withholding proof')
    unit = proof['protected_unit']
    if not isinstance(unit, list) or len(unit) != 2:
        raise DisclosureError(f'withheld row {plan_row["id"]} proof unit must be a pair')
    protected_unit = (ident(unit[0]), ident(unit[1]))
    if protected_unit not in protected_set:
        raise DisclosureError(
            f'withheld row {plan_row["id"]} proof does not name a protected unit')
    combination = proof['combination']
    if not isinstance(combination, list) or not combination:
        raise DisclosureError(f'withheld row {plan_row["id"]} proof needs a non-empty combination')
    prefix_ids = history_ids | {row['id'] for row, decision in earlier_decisions
                                if decision == 'issued'}
    coefficients = {}
    candidate_seen = False
    for term in combination:
        if not isinstance(term, dict) or set(term) != {'row_id', 'coefficient'}:
            raise DisclosureError(f'withheld row {plan_row["id"]} has a corrupt combination term')
        row_id = ident(term['row_id'])
        if row_id not in row_by_id:
            raise DisclosureError(
                f'withheld row {plan_row["id"]} proof references unknown row {row_id}')
        if row_id == plan_row['id']:
            candidate_seen = True
        elif row_id not in prefix_ids:
            raise DisclosureError(
                f'withheld row {plan_row["id"]} proof may only use its real issued prefix; '
                f'{row_id} was not published or issued earlier')
        coefficient = parse_rational(term['coefficient'],
                                     f'withheld row {plan_row["id"]} coefficient')
        if coefficient == 0:
            raise DisclosureError(
                f'withheld row {plan_row["id"]} combination coefficients must be non-zero')
        if row_id in coefficients:
            raise DisclosureError(
                f'withheld row {plan_row["id"]} combination repeats row {row_id}')
        coefficients[row_id] = coefficient
    if not candidate_seen:
        raise DisclosureError(
            f'withheld row {plan_row["id"]} proof must include the withheld row itself')

    # Re-derive the member coefficients of the rational linear combination and
    # require them to be exactly the protected unit vector.
    accumulated = {}
    for row_id, coefficient in coefficients.items():
        referenced = row_by_id[row_id]
        if referenced['analyte_id'] != protected_unit[1]:
            raise DisclosureError(
                f'withheld row {plan_row["id"]} proof mixes analytes via row {row_id}')
        for member in referenced['members']:
            key = (member, referenced['analyte_id'])
            accumulated[key] = accumulated.get(key, Fraction(0)) + coefficient
            if accumulated[key] == 0:
                del accumulated[key]
    if accumulated != {protected_unit: Fraction(1)}:
        raise DisclosureError(
            f'withheld row {plan_row["id"]} combination is not the unit vector of {list(protected_unit)}')


def _check_freedom_proofs(plan, raw_proofs, row_by_id, history_ids,
                          issued_ids, protected_set):
    if not isinstance(raw_proofs, list):
        raise DisclosureError('freedom_proofs must be an array')
    expected_units = sorted(protected_set)
    seen_units = []
    # Units a proof may mention: protected units plus members of any PLAN row.
    allowed_units = set(protected_set)
    for row in row_by_id.values():
        for member in row['members']:
            allowed_units.add((member, row['analyte_id']))
    fact_rows = [row_by_id[row_id] for row_id in sorted(history_ids | set(issued_ids))]

    for proof in raw_proofs:
        if not isinstance(proof, dict) or set(proof) != {'protected_unit', 'changes'}:
            raise DisclosureError('corrupt freedom proof')
        unit = proof['protected_unit']
        if not isinstance(unit, list) or len(unit) != 2:
            raise DisclosureError('freedom proof unit must be a pair')
        protected_unit = (ident(unit[0]), ident(unit[1]))
        if protected_unit not in protected_set:
            raise DisclosureError('freedom proof does not name a protected unit')
        seen_units.append(protected_unit)
        changes_raw = proof['changes']
        if not isinstance(changes_raw, list) or not changes_raw:
            raise DisclosureError(
                f'freedom proof for {list(protected_unit)} needs non-empty changes')
        changes = {}
        for term in changes_raw:
            if not isinstance(term, dict) or set(term) != {'unit', 'change'}:
                raise DisclosureError('corrupt freedom change term')
            pair = term['unit']
            if not isinstance(pair, list) or len(pair) != 2:
                raise DisclosureError('freedom change unit must be a pair')
            change_unit = (ident(pair[0]), ident(pair[1]))
            if change_unit not in allowed_units:
                raise DisclosureError(
                    f'freedom proof unit {list(change_unit)} appears nowhere in the PLAN')
            value = parse_rational(term['change'], 'freedom change')
            if value == 0:
                raise DisclosureError('freedom change entries must be non-zero')
            if change_unit in changes:
                raise DisclosureError(f'freedom proof repeats unit {list(change_unit)}')
            changes[change_unit] = value
        if changes.get(protected_unit, Fraction(0)) == 0:
            raise DisclosureError(
                f'freedom proof for {list(protected_unit)} must move that unit non-trivially')
        # Every published and issued equation must stay unchanged.
        for row in fact_rows:
            total_change = sum(
                changes.get((member, row['analyte_id']), Fraction(0))
                for member in row['members'])
            if total_change != 0:
                raise DisclosureError(
                    f'freedom proof for {list(protected_unit)} changes published/issued row {row["id"]}')
    if sorted(seen_units) != expected_units or len(seen_units) != len(set(seen_units)):
        raise DisclosureError(
            'freedom proofs must cover every protected unit exactly once')


def disclosure_check(plan_path, public_path, audit_path):
    plan = load_plan(plan_path)
    public_data = _load_product(public_path)
    audit_data = _load_product(audit_path)
    return verify_disclosure(plan, public_data, audit_data)
