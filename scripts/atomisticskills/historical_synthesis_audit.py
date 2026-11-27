#!/usr/bin/env python3
"""Summarize the private route/source adjudication without publishing answer sets."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

TASK = 'synthesis-precursor-recommendation'
REQUIRED = 5
SUPPORT_TIERS = {
    'including_snippet_only': {'full_text', 'database', 'abstract', 'snippet'},
    'excluding_snippet_only': {'full_text', 'database', 'abstract'},
    'excluding_snippet_and_abstract': {'full_text', 'database'},
    'full_text_only': {'full_text'},
}


def summarize(ledger: dict) -> dict:
    if ledger['task'] != TASK or len(ledger['cards']) != 18:
        raise ValueError('Unexpected historical synthesis cohort')
    routes = {route['letter']: route for route in ledger['routes']}
    if len(routes) != len(ledger['routes']) or len({c['audit_id'] for c in ledger['cards']}) != 18:
        raise ValueError('Duplicate route or card ID in synthesis ledger')
    cards = []
    for i, card in enumerate(ledger['cards'], 1):
        distinct = {routes[row['letter']]['canonical_key']: routes[row['letter']]
                    for row in card['routes']}.values()
        if len(distinct) != card['distinct_count'] or len(card['routes']) != card['reported_count']:
            raise ValueError(f'Inconsistent route counts in historical card {i}')
        by_status = Counter(row['status'] for row in distinct)
        supported = [row for row in distinct if row['status'] == 'supported']
        evidence_levels = [{s['level'] for s in row['sources'] if s['status'] == 'supported'}
                           for row in supported]
        support = {tier: sum(bool(levels & accepted) for levels in evidence_levels)
                   for tier, accepted in SUPPORT_TIERS.items()}
        if support['including_snippet_only'] != len(supported):
            raise ValueError(f'Missing or unknown positive evidence level for historical card {i}')
        status = ('supported' if support['excluding_snippet_only'] >= REQUIRED else
                  'snippet_dependent' if len(supported) >= REQUIRED else 'insufficient_evidence')
        cards.append({
            'card': f'synthesis-{i:02d}',
            'published_reward': card['published_reward'],
            'reported_combinations': card['reported_count'],
            **{f'supported_routes_{tier}': count for tier, count in support.items()},
            'additive_dependent': by_status['supported_with_additive'],
            'unverified': by_status['unverified'],
            'contradicted': by_status['contradicted'],
            'initial_strict_whitelist_pass': card['fixed_verifier_pass'],
            'historical_disposition': status,
        })
    return {
        'schema_version': 2,
        'task': TASK,
        'published_site_commit': 'd9610cfcecfe8dc44d97c6a7546deee3caeb7d21',
        'rule': 'Confirm at least five distinct published precursor sets under the original scope; additional sets do not count against a qualifying answer.',
        'evidence': 'Source adjudication of Materials Project extraction records and accessible publications, with snippet-only support reported separately.',
        'counts': {
            'cards': len(cards),
            'published_reward_one': sum(c['published_reward'] == 1 for c in cards),
            'initial_strict_whitelist_pass': sum(c['initial_strict_whitelist_pass'] for c in cards),
            **{f'supported_{tier}': sum(c[f'supported_routes_{tier}'] >= REQUIRED for c in cards)
               for tier in SUPPORT_TIERS},
            **{status: sum(c['historical_disposition'] == status for c in cards)
               for status in ('insufficient_evidence', 'snippet_dependent')},
        },
        'limitations': [
            'Text mining can omit additives; evidence tiers are reported separately. Some archived abstract or database text was not independently re-retrievable.',
            'Most checked full-text evidence in the private ledger is summarized rather than quoted verbatim.',
            'Insufficient evidence does not establish fabrication.',
            'An invalid text-mined entry was unused by these cards and was removed from the current corpus. A second incorrect source association was not relied on, and the remaining corpus was not exhaustively recertified.',
            'The detailed precursor and source ledger is retained privately to avoid publishing an answer key; the public summary is not sufficient to independently verify route-to-source assignments.',
        ],
        'cards': cards,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('private_ledger', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    report = summarize(json.loads(args.private_ledger.read_text(encoding='utf-8')))
    args.out.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps(report['counts']))


if __name__ == '__main__':
    main()
