#!/usr/bin/env python3
"""Regrade the 18 originally published synthesis slots, without enrolling new runs."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from itertools import combinations
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
TASK = 'synthesis-precursor-recommendation'
TASK_TESTS = ROOT / 'tasks/materials-science' / TASK / 'tests'
sys.path.insert(0, str(TASK_TESTS))
from precursor_identity import hydrate_parent_key, precursor_formula_key, route_key
from test_outputs import build_index


PUBLICATION = 'd9610cfcecfe8dc44d97c6a7546deee3caeb7d21'
MINIMUM = 5  # The original written contract, independent of the forward grader.


def historical_index(reference):
    """Normalize documented hydrates; count published sets, not forward cores."""
    forms = [reagent for route in reference['routes'] for source in route['evidence']
             if source['tier'] != 'patent-example'
             for reagent in source['reported_core'] + source.get('ancillary', [])]
    hydrate_aliases = {precursor_formula_key(f): parent for f in forms if (parent := hydrate_parent_key(f))}
    # Aqueous ammonia denotes a distinct solution reagent, not solid ammonium hydrate.
    aqueous_ammonia = precursor_formula_key('NH4OH')
    hydrate_aliases.pop(aqueous_ammonia, None)

    def key(values):
        if route_key(values) is None:
            return None
        keys = set()
        for formula in values:
            raw = precursor_formula_key(formula)
            hydrated_base = hydrate_parent_key(formula)
            keys.add(raw if raw == aqueous_ammonia else hydrate_aliases.get(raw, hydrated_base or raw))
        return frozenset(keys) if len(keys) == len(values) else None

    evidence = {}
    for route in reference['routes']:
        for source in route['evidence']:
            if source['tier'] == 'patent-example':
                continue  # The original prompt called for experimental papers.
            rest = source.get('ancillary', [])
            for n in range(len(rest) + 1):
                for subset in combinations(rest, n):
                    item = key([*source['reported_core'], *subset])
                    if item is None:
                        raise ValueError('Ambiguous reagent in historical reference')
                    evidence.setdefault(item, set()).add(source['doi'])
    return key, evidence


def summarize(cohort, answers, reference_bytes):
    reference = json.loads(reference_bytes)
    core_index = build_index(reference)
    key, evidence = historical_index(reference)
    selected = [row for row in cohort if row['task'] == TASK]
    slots = {(c['label'], c['skills'], c['number']) for c in selected}
    trials = {c['matched']['trial'] for c in selected}
    if len(cohort) != 558 or len(trials) != 18 or len(slots) != 18 or len(selected) != 18:
        raise ValueError('Unexpected published synthesis cohort')
    modes = Counter((label, skills) for label, skills, _ in slots)
    if (len(modes) != 14 or any(k != (3 if label == 'GPT-5.6 Luna' else 1)
                                for (label, _), k in modes.items())):
        raise ValueError('Unexpected per-model synthesis cohort')
    cards = []
    for row in selected:
        trial = row['matched']
        got = json.loads(answers[trial['trial']])
        if (trial['reward'] != 1 or got['target_formula'] != reference['target_formula']
                or not isinstance(got['precursor_combinations'], list)):
            raise ValueError('Unexpected published reward or saved answer shape')
        combos = got['precursor_combinations']
        historical = {k for combo in combos if (k := key(combo)) in evidence}
        forward = {core_index[k] for combo in combos if (k := route_key(combo)) in core_index}
        cards.append({
            'label': row['label'], 'skills': row['skills'], 'number': row['number'],
            'published_reward': 1, 'reported_combinations': len(combos),
            'supported_distinct_combinations': len(historical), 'forward_distinct_cores': len(forward),
            'status': 'pass' if len(historical) >= MINIMUM else 'not_verified',
        })
    return {
        'schema_version': 1, 'task': TASK, 'published_site_commit': PUBLICATION,
        'reference_sha256': hashlib.sha256(reference_bytes).hexdigest(),
        'method': ('Retrospective evidence audit of the original experimental-paper requirement: '
                   'at least five distinct source-supported precursor sets; documented differences in '
                   'reported ancillary reagents can distinguish combinations, hydration does not, and '
                   'extra unmatched sets do not undo a pass. A score of zero does not imply fabrication.'),
        'summary': {'cards': len(cards), 'pass': sum(c['status'] == 'pass' for c in cards),
                    'not_verified': sum(c['status'] == 'not_verified' for c in cards),
                    'forward_rule_pass_diagnostic': sum(c['forward_distinct_cores'] >= reference['required_routes_count']
                                                        for c in cards)},
        'cards': cards,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('matched_cohort', type=Path)
    parser.add_argument('answers_jsonl', type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    answers = {}
    for line in args.answers_jsonl.read_text().splitlines():
        row = json.loads(line)
        if row['trial'] in answers:
            raise ValueError('Duplicate archived answer')
        answers[row['trial']] = row['answer']
    report = summarize(json.loads(args.matched_cohort.read_text()), answers,
                       (TASK_TESTS / 'expected.json').read_bytes())
    args.out.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report['summary']))


if __name__ == '__main__':
    main()
