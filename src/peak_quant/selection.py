"""Best-explanation selection over ordered candidate windows.

The engine is intentionally independent of JSON, files and chromatography:
it operates on per-position option lists carrying exact Fraction windows and
integer penalties.  Both the explanation package builder and the offline
contrast query use the same primitives, so the "original best set" is always
defined by one rule:

1. maximize the number of selected windows;
2. among those explanations minimize the summed penalty;
3. presentation ties (never eliminations) are broken by the lexicographically
   smallest candidate-id sequence with the vacancy sorting after every id.

Every comparison here is exact (Fraction / int); no floating point is used.
"""
from fractions import Fraction


def better(trial, best):
    """True if (count, penalty) trial dominates best: more picks, then less cost."""
    if best is None:
        return True
    trial_count, trial_penalty = trial
    best_count, best_penalty = best
    return trial_count > best_count or (
        trial_count == best_count and trial_penalty < best_penalty)


def _suffix(tables, position, last_hi):
    """Best (count, penalty) over positions >= position given last_hi."""
    if position == len(tables) - 1:
        return (0, 0)
    return tables[position][last_hi]


def build_backward(options):
    """Backward DP tables.

    options[i] is the list of candidate option dicts at analyte position i,
    each with Fraction 'lo'/'hi' and integer 'penalty'.  tables[i] maps the
    rightmost selected window end (None when nothing is selected yet) to the
    best achievable (count, penalty) over positions i..n-1.
    """
    n = len(options)
    tables = [None] * (n + 1)
    tables[n] = {}
    for i in range(n - 1, -1, -1):
        incoming = {None}
        for j in range(i):
            for candidate in options[j]:
                incoming.add(candidate['hi'])
        table = {}
        here = options[i]
        for last_hi in incoming:
            # Vacancy at position i: the trailing state does not change.
            best = _suffix(tables, i + 1, last_hi)
            for candidate in here:
                if last_hi is not None and last_hi > candidate['lo']:
                    continue
                count, penalty = _suffix(tables, i + 1, candidate['hi'])
                trial = (count + 1, penalty + candidate['penalty'])
                if better(trial, best):
                    best = trial
            table[last_hi] = best
        tables[i] = table
    return tables


def best_metrics(options):
    """The two optimum indicators (selected count, total penalty)."""
    return build_backward(options)[0][None]


def lex_key(witness):
    """Sort key for a full explanation: ids lexically, vacancy after all ids."""
    return tuple((0, entry['id']) if entry is not None else (1, '')
                 for entry in witness)


def representative(options):
    """Greedy lexicographically smallest best explanation.

    Returns one option dict or None (vacancy) per position.  The backward
    table makes every tentative choice checkable against the global optimum,
    so no best explanation is ever enumerated.
    """
    tables = build_backward(options)
    target_count, target_penalty = tables[0][None]
    last_hi = None
    used_count = 0
    used_penalty = 0
    witness = []
    for i, here in enumerate(options):
        chosen = None
        for candidate in sorted(here, key=lambda item: item['id']):
            if last_hi is not None and last_hi > candidate['lo']:
                continue
            count, penalty = _suffix(tables, i + 1, candidate['hi'])
            if (used_count + 1 + count == target_count
                    and used_penalty + candidate['penalty'] + penalty == target_penalty):
                chosen = candidate
                break
        if chosen is None:
            # The vacancy must remain viable; the optimum always stays reachable.
            count, penalty = _suffix(tables, i + 1, last_hi)
            if (used_count + count != target_count
                    or used_penalty + penalty != target_penalty):  # pragma: no cover
                raise RuntimeError('best explanation became unreachable')
            witness.append(None)
        else:
            witness.append(chosen)
            last_hi = chosen['hi']
            used_count += 1
            used_penalty += chosen['penalty']
    return witness


def attainable(options):
    """For each position: candidate ids appearing in >=1 best explanation and
    whether the vacancy appears in >=1 best explanation.

    At every boundary each trailing state keeps at most one viable
    (count, penalty) pair, because the backward table fixes the only pair that
    can still reach the global optimum; paths therefore never proliferate.
    """
    tables = build_backward(options)
    target_count, target_penalty = tables[0][None]
    forward = {None: (0, 0)}
    result = []
    for i, here in enumerate(options):
        next_forward = {}
        kept_ids = set()
        vacancy_ok = False

        def viable(position, last_hi, count, penalty):
            suffix_count, suffix_penalty = _suffix(tables, position, last_hi)
            return (count + suffix_count == target_count
                    and penalty + suffix_penalty == target_penalty)

        for last_hi, (count, penalty) in forward.items():
            if viable(i + 1, last_hi, count, penalty):
                vacancy_ok = True
                next_forward[last_hi] = (count, penalty)
            for candidate in here:
                if last_hi is not None and last_hi > candidate['lo']:
                    continue
                new_count = count + 1
                new_penalty = penalty + candidate['penalty']
                if viable(i + 1, candidate['hi'], new_count, new_penalty):
                    kept_ids.add(candidate['id'])
                    next_forward[candidate['hi']] = (new_count, new_penalty)
        result.append((kept_ids, vacancy_ok))
        forward = next_forward
    return result


_PIN_UNSET = object()


def constrained_extrema(options, pins, term_positions, contribution):
    """Min/max weighted concentration sum inside the original best set.

    pins: position -> None (vacancy required) or candidate id (fixed pick);
    term_positions: positions whose analyte must carry a selected peak;
    contribution(position, candidate) -> Fraction coefficient * concentration.

    Returns None when no best explanation satisfies the constraints, otherwise
    (minimum, maximum) where each extreme is (Fraction value, witness tuple of
    option dicts / None).  Ties use the representative lexical rule.

    A boundary state is the rightmost selected window end (None = none yet).
    Every viable path reaching one state carries the same (count, penalty),
    because only one such pair can still reach the global optimum; we therefore
    merge paths per state while retaining both score extremes and their
    lexicographically smallest witnesses, never enumerating the best set.
    """
    tables = build_backward(options)
    target_count, target_penalty = tables[0][None]
    # state -> [count, penalty, min_value, min_witness, max_value, max_witness]
    forward = {None: [0, 0, Fraction(0), (), Fraction(0), ()]}
    for i, here in enumerate(options):
        pin = pins.get(i, _PIN_UNSET)
        next_forward = {}
        for last_hi, bucket in forward.items():
            count, penalty, min_value, min_wit, max_value, max_wit = bucket

            def viable(state_end, candidate_count, candidate_penalty):
                suffix_count, suffix_penalty = _suffix(tables, i + 1, state_end)
                return (candidate_count + suffix_count == target_count
                        and candidate_penalty + suffix_penalty == target_penalty)

            # Vacancy transition: allowed when no pin fixes a pick AND this
            # position is not required to carry a term peak.  An explicit
            # vacancy pin still loses to term coverage (the query is then
            # unsatisfiable), and an explicit pick pin forbids vacancy.
            fixed_pick = pin is not _PIN_UNSET and pin is not None
            vacancy_allowed = (not fixed_pick) and i not in term_positions
            if vacancy_allowed and viable(last_hi, count, penalty):
                _merge(next_forward, last_hi, count, penalty,
                       min_value, min_wit + (None,),
                       max_value, max_wit + (None,))
            # Candidate transitions, id order so ties resolve deterministically.
            if pin is None:
                continue
            for candidate in sorted(here, key=lambda item: item['id']):
                if pin is not _PIN_UNSET and pin != candidate['id']:
                    continue
                if last_hi is not None and last_hi > candidate['lo']:
                    continue
                new_count = count + 1
                new_penalty = penalty + candidate['penalty']
                if not viable(candidate['hi'], new_count, new_penalty):
                    continue
                added = contribution(i, candidate) if i in term_positions else Fraction(0)
                _merge(next_forward, candidate['hi'], new_count, new_penalty,
                       min_value + added, min_wit + (candidate,),
                       max_value + added, max_wit + (candidate,))
        forward = next_forward
        if not forward:
            return None
    minimum = None
    maximum = None
    for bucket in forward.values():
        _, _, min_value, min_wit, max_value, max_wit = bucket
        if minimum is None or min_value < minimum[0] or (
                min_value == minimum[0] and lex_key(min_wit) < lex_key(minimum[1])):
            minimum = (min_value, min_wit)
        if maximum is None or max_value > maximum[0] or (
                max_value == maximum[0] and lex_key(max_wit) < lex_key(maximum[1])):
            maximum = (max_value, max_wit)
    return minimum, maximum


def _merge(forward, state, count, penalty, min_value, min_wit, max_value, max_wit):
    existing = forward.get(state)
    if existing is None:
        forward[state] = [count, penalty, min_value, min_wit, max_value, max_wit]
        return
    # Paths meeting at one boundary state are only merged when they carry the
    # same optimum-reaching count and penalty; the backward viability filter
    # guarantees this, so a mismatch signals an engine bug rather than data.
    if (count, penalty) != (existing[0], existing[1]):
        raise RuntimeError('conflicting boundary states during constrained extrema')
    if min_value < existing[2] or (min_value == existing[2]
                                   and lex_key(min_wit) < lex_key(existing[3])):
        existing[2], existing[3] = min_value, min_wit
    if max_value > existing[4] or (max_value == existing[4]
                                   and lex_key(max_wit) < lex_key(existing[5])):
        existing[4], existing[5] = max_value, max_wit
