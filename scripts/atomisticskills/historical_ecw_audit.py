#!/usr/bin/env python3
"""Regrade published ECW answer artifacts without rerunning any model.

Input is the private telemetry match and saved answers; output never includes
submitted values or archive identifiers. Do not infer the identity or method
from an answer that matches the reference.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path


TASK = 'electrochemical-window'
KEYS = ('reduction_voltage_V', 'oxidation_voltage_V', 'window_width_V')
ORIGINAL = dict(zip(KEYS, (0.020, 0.020, 0.030)))
SENSITIVITY = dict(zip(KEYS, (0.010, 0.010, 0.020)))
PUBLIC_SNAPSHOT = 'd9610cfcecfe8dc44d97c6a7546deee3caeb7d21'


def grade(data, reference, tolerances):
    if any(reference[key] is None for key in KEYS):
        # The old artifact required numeric voltages and could not express this.
        return 'not_comparable', []
    if not isinstance(data, dict) or any(key not in data for key in (*KEYS, 'stable')):
        return 'unverified', []
    try:
        # Exact original key names; the old verifier used float() for voltages.
        volts = {key: float(data[key]) for key in KEYS}
    except (ValueError, TypeError, OverflowError):
        return 'unverified', []
    if not all(map(math.isfinite, volts.values())) or type(data['stable']) is not bool:
        return 'unverified', []
    outside = [key for key in KEYS if abs(volts[key] - reference[key]) > tolerances[key]]
    if data['stable'] is not reference['stable']:
        outside.append('stable')
    return ('fail' if outside else 'pass'), outside


def read_answers(path):
    answers = {}
    for line in path.read_text().splitlines():
        row = json.loads(line)
        trial = row['trial']
        if trial in answers:
            raise ValueError('Duplicate trial in saved-answer export')
        try:
            value = row['answer']
            answers[trial] = json.loads(value) if isinstance(value, str) else value
        except json.JSONDecodeError:
            answers[trial] = None
    return answers


def audit(audit_dir: Path, reference_path: Path, expected_cards: int = 18) -> dict:
    reference_bytes = reference_path.read_bytes()
    targets = json.loads(reference_bytes)['targets']
    reference = {item['path']: item['expected'] for item in targets}
    red, ox, width = (reference[key] for key in KEYS)
    if (any(value is None for value in (red, ox, width)) and (red, ox, width) != (None,) * 3
            or red is not None and not (all(map(math.isfinite, (red, ox, width)))
                                        and 0 <= red < ox and abs(width - (ox - red)) <= 1e-7)
            or type(reference['stable']) is not bool):
        raise ValueError('Inconsistent revised reference')
    matched = json.loads((audit_dir / 'published-match.json').read_text())
    rows = sorted((r for r in matched if r['task'] == TASK),
                  key=lambda r: (r['label'], r['skills'], r['sec']))
    if len(rows) != expected_cards or len({r['trial'] for r in rows}) != expected_cards:
        raise ValueError('Incomplete or duplicate published ECW cohort')
    answers = read_answers(audit_dir / 'answers.jsonl')
    cards = []
    for i, row in enumerate(rows, 1):
        answer = answers.get(row['trial']) if not row['exception'] else None
        result, outside = grade(answer, reference, ORIGINAL)
        sensitivity, tight_outside = grade(answer, reference, SENSITIVITY)
        cards.append({'card': f'ecw-{i:02d}', 'model': row['label'],
                      'mode': 'with-skill' if row['skills'] else 'no-skill',
                      'recorded': 'error' if row['exception'] else {1: 'pass', 0: 'fail'}.get(row['reward'], 'unverified'),
                      'regraded': result, 'outside_original_tolerance': outside,
                      'voltage_tolerance_sensitivity': sensitivity, 'outside_sensitivity_tolerance': tight_outside})
    return {
        'schema_version': 1, 'task': TASK, 'published_site_commit': PUBLIC_SNAPSHOT,
        'reference_sha256': hashlib.sha256(reference_bytes).hexdigest(),
        'rule': 'Revised numerical reference; four original output keys and original tolerances. No requirement for newer output fields.',
        'original_tolerances_V': ORIGINAL, 'sensitivity_tolerances_V': SENSITIVITY,
        'scope': 'Saved-artifact regrade only. It does not prove target identity, relaxation, or performance with the revised prompt.',
        'counts': {key: dict(sorted(Counter(r[key] for r in cards).items()))
                   for key in ('recorded', 'regraded', 'voltage_tolerance_sensitivity')},
        'cards': cards,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit-dir', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    output = json.dumps(audit(args.audit_dir, args.reference), indent=2, allow_nan=False) + '\n'
    if args.out:
        args.out.write_text(output)
    else:
        print(output, end='')


if __name__ == '__main__':
    main()
