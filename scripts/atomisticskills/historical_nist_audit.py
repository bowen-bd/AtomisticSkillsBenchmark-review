#!/usr/bin/env python3
"""Audit the historical public NIST JANAF trial cards.

The audit answers one narrow question: for each published card, did the saved
trajectory contain a WebBook response showing the intended compound and every
graded value, or only the answer that the old instruction disclosed?

The task's own ``tests/expected.json`` is the only source of reference
metadata.  It is loaded at run time, used locally for answer checking, and sent
to the scanner in the request JSON.  The reference values, names, identifiers,
and tolerances are never embedded in this script or copied into the report.

Only aggregate evidence is written.  Raw prompts, observations, trial/job
identifiers, host names, and archive paths are never included in the report.

The archive is read-only: trajectories are opened, never modified, and the
agent is never rerun.  The same scanner runs locally or over SSH.

Typical use (the archive host and paths are supplied by the operator):

    python3 scripts/atomisticskills/historical_nist_audit.py \
        --audit-dir <prior-audit-dir> \
        --remote <archive-host> \
        --jobs-root <remote-jobs-root> \
        --out docs/issue-1/nist.json

For an already-mounted archive, omit --remote and pass --jobs-root as a local
path.  The report is deterministic for a fixed input set.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

TASK = "nist-janaf-query"
EXPECTED_CARDS = 18
SCHEMA_VERSION = 2

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXPECTED = REPO_ROOT / "tasks" / "materials-science" / TASK / "tests" / "expected.json"

# The scanner is deliberately a self-contained program.  It can be sent to a
# remote Python over stdin, or run locally with the same code path, so the two
# modes cannot drift apart.  It emits only booleans and step numbers.  All
# reference metadata arrives in the request JSON.
_SCANNER = r'''
import ast
import html
import json
import os
import re
import sys
import unicodedata


def _decode_content(value):
    """Turn a trajectory observation payload into plain text."""
    if value is None:
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(value, str):
        try:
            parsed = ast.literal_eval(value)
        except Exception:
            return value
        if isinstance(parsed, list):
            parts = []
            for item in parsed:
                if isinstance(item, dict):
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(str(item))
            return "\n".join(parts)
        if isinstance(parsed, dict):
            if "text" in parsed:
                return str(parsed.get("text", ""))
            return value
        return value
    return str(value)


def _normalize(text):
    text = unicodedata.normalize("NFKC", str(text))
    text = text.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    return html.unescape(text)


def _number_pattern(value):
    return r"(?<![\d.])" + re.escape(str(value)) + r"(?![\d])"


def _has_number(text, value):
    return re.search(_number_pattern(value), text) is not None


def _has_raw_value(text, value):
    pattern = (
        r'raw["\']?\s*[:=]\s*["\']?\s*'
        + re.escape(str(value))
        + r'\s*(?:\u00b1|\+/-)\s*[\d.]+'
    )
    return re.search(pattern, text, re.I) is not None


def _step_text(step):
    parts = []
    observation = step.get("observation") or {}
    for result in observation.get("results") or []:
        if isinstance(result, dict):
            parts.append(_decode_content(result.get("content")))
    return "\n".join(parts)


def _tool_text(step):
    parts = []
    for call in step.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        parts.append(str(call.get("function_name") or call.get("name") or ""))
        parts.append(json.dumps(call.get("arguments"), ensure_ascii=False, sort_keys=True))
    return "\n".join(parts)


def _is_result_write(tool_text):
    lowered = tool_text.lower()
    if "result.json" not in lowered:
        return False
    markers = (
        "apply_patch",
        "cat >",
        "tee ",
        ">>",
        " > ",
        "write_file",
        "write_text",
        "writefile",
    )
    return any(marker in lowered for marker in markers)


def _features(text, tool_text, reference):
    t = _normalize(text)
    quantities = reference["quantities"]
    values_found = [item for item in quantities if _has_number(t, item["value"])]
    values = len(values_found) == len(quantities)
    folded = t.casefold()
    compound = any(str(name).casefold() in folded for name in reference["names"])
    nist_id = any(str(identifier) in t.upper() for identifier in reference["ids"])
    cox = re.search(r"Cox\s*,\s*Wagman", t, re.I) is not None
    raw_values = all(_has_raw_value(t, item["value"]) for item in quantities)
    table = all(word in t for word in ("Quantity", "Value", "Units"))
    html_table = (
        "<tr" in t
        or "<td" in t
        or "One dimensional data" in t
        or table
    )
    row_output = (
        all(item["key"] in t for item in quantities)
        and cox
        and ("\u00b1" in t or "|" in t)
    )
    quantity_artifact = re.search(r'["\']Quantity["\']\s*:\s*\{', t) is not None
    validated = "Validated:" in t
    crawled = "Crawled:" in t
    http200 = (
        re.search(
            r"(?:HTTP(?:/\d(?:\.\d)?)?\s*(?:status\s*:?\s*)?|Status\s*:\s*)200\b",
            t,
            re.I,
        )
        is not None
    )
    http_error = (
        re.search(
            r"HTTPError|HTTP Error 403|403 Forbidden|not safe to open|"
            r"Failed to fetch|URLError|Connection refused|timed out|"
            r"Name or service not known",
            t,
            re.I,
        )
        is not None
    )
    download = (
        ("curl" in tool_text.lower() or "wget" in tool_text.lower())
        and "Process exited with code 0" in t
    )
    return {
        "values": values,
        "value_hits": len(values_found),
        "compound": compound or nist_id,
        "cox": cox,
        "raw_values": raw_values,
        "html_table": html_table,
        "row_output": row_output,
        "quantity": quantity_artifact,
        "validated": validated,
        "crawled": crawled,
        "http200": http200,
        "http_error": http_error,
        "download": download,
    }


def scan_one(root, entry, reference):
    path = os.path.join(
        os.path.expanduser(root), entry["job"], entry["trial"], "agent", "trajectory.json"
    )
    record = {
        "job": entry["job"],
        "trial": entry["trial"],
        "exists": os.path.isfile(path),
        "steps": 0,
        "values_expected": len(reference["quantities"]),
        "prompt_disclosed": False,
        "target_observed": False,
        "step_flags": [],
        "result_write_steps": [],
    }
    if not record["exists"]:
        return record
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        record["error"] = "unreadable"
        return record
    steps = data.get("steps") or []
    record["steps"] = len(steps)
    prompt_parts = []
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            continue
        source = step.get("source")
        message = _decode_content(step.get("message"))
        if source in ("system", "user"):
            prompt_parts.append(_normalize(message))
        tool_text = _normalize(_tool_text(step))
        if _is_result_write(tool_text):
            record["result_write_steps"].append(index)
        target = "webbook.nist.gov" in tool_text or "query_janaf.py" in tool_text
        if target:
            record["target_observed"] = True
        text = _step_text(step)
        if not text:
            continue
        flags = _features(text, tool_text, reference)
        flags["step"] = index
        flags["target"] = target
        record["step_flags"].append(flags)
    prompt = "\n".join(prompt_parts)
    record["prompt_disclosed"] = all(
        _has_number(prompt, item["value"]) for item in reference["quantities"]
    )
    return record


def main(request):
    reference = request["reference"]
    for entry in request["trials"]:
        try:
            record = scan_one(request["root"], entry, reference)
        except Exception:
            record = {
                "job": entry["job"],
                "trial": entry["trial"],
                "exists": False,
                "error": "scan-failed",
                "steps": 0,
                "values_expected": len(reference["quantities"]),
                "prompt_disclosed": False,
                "target_observed": False,
                "step_flags": [],
                "result_write_steps": [],
            }
        print(json.dumps(record, ensure_ascii=False))

'''


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit historical NIST JANAF cards without exporting raw traces."
    )
    parser.add_argument("--audit-dir", required=True, help="directory with published-match.json and answers.jsonl")
    parser.add_argument("--jobs-root", required=True, help="archive root containing <job>/<trial>/agent/trajectory.json")
    parser.add_argument(
        "--expected",
        default=str(DEFAULT_EXPECTED),
        help="task reference metadata (default: repository tests/expected.json)",
    )
    parser.add_argument("--remote", help="SSH destination; omit for an already-mounted archive")
    parser.add_argument("--ssh-option", action="append", default=[], help="extra -o option for SSH (repeatable)")
    parser.add_argument("--timeout", type=int, default=300, help="scanner timeout in seconds (default: 300)")
    parser.add_argument("--expect", type=int, default=EXPECTED_CARDS, help="expected number of cards (default: 18)")
    parser.add_argument("--out", default="-", help="report path, or - for stdout (default: -)")
    parser.add_argument("--compact", action="store_true", help="write compact JSON")
    return parser.parse_args(argv)


def _id_variants(values: list[str]) -> list[str]:
    variants: set[str] = set()
    for value in values:
        text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).upper()
        if text.startswith("C"):
            text = text[1:]
        if not text:
            continue
        variants.add(text)
        variants.add("C" + text)
    return sorted(variants)


def _load_reference(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"missing task reference metadata: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"cannot read task reference metadata: {path}") from exc
    if not isinstance(data, dict):
        raise SystemExit("task reference metadata must be a JSON object")
    formula = str(data.get("formula") or "").strip()
    name_info = data.get("name")
    id_info = data.get("nist_id")
    thermo = data.get("thermochemistry")
    if (
        not formula
        or not isinstance(name_info, dict)
        or not isinstance(id_info, dict)
        or not isinstance(thermo, dict)
        or not thermo
    ):
        raise SystemExit("task reference metadata is missing formula, name, nist_id, or thermochemistry")

    names = []
    canonical_name = str(name_info.get("canonical") or "").strip()
    if canonical_name:
        names.append(canonical_name)
    for alias in name_info.get("aliases") or []:
        text = str(alias).strip()
        if text:
            names.append(text)
    names = sorted(set(names))

    raw_ids = []
    canonical_id = str(id_info.get("canonical") or "").strip()
    if canonical_id:
        raw_ids.append(canonical_id)
    for alias in id_info.get("aliases") or []:
        text = str(alias).strip()
        if text:
            raw_ids.append(text)
    ids = _id_variants(raw_ids)
    if not names or not ids:
        raise SystemExit("task reference metadata has no usable name or identifier")

    quantities = []
    for key, item in thermo.items():
        if not isinstance(item, dict):
            raise SystemExit("task reference thermochemistry entries must be objects")
        try:
            value = float(item["value"])
            tolerance = float(item["tolerance"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(
                "task reference thermochemistry entry lacks a numeric value or tolerance"
            ) from exc
        units = str(item.get("units") or "").strip()
        if not units:
            raise SystemExit("task reference thermochemistry entry lacks units")
        quantities.append(
            {
                "key": str(key),
                "value": value,
                "tolerance": tolerance,
                "units": units,
            }
        )
    return {
        "formula": formula,
        "names": names,
        "ids": ids,
        "quantities": quantities,
    }


def _norm_formula(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value)))


def _norm_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[\s.;]+$", "", text)
    return text.casefold()


def _norm_id(value: Any) -> str:
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).upper()
    if text.startswith("C"):
        text = text[1:]
    return text


def _is_public_pass(row: dict[str, Any]) -> bool:
    if row.get("exception") is True:
        return False
    try:
        return float(row.get("reward")) == 1.0
    except (TypeError, ValueError):
        return False


def _load_selection(audit_dir: Path, expected: int) -> list[dict[str, Any]]:
    match_path = audit_dir / "published-match.json"
    answers_path = audit_dir / "answers.jsonl"
    if not match_path.is_file():
        raise SystemExit("missing published-match.json in --audit-dir")
    if not answers_path.is_file():
        raise SystemExit("missing answers.jsonl in --audit-dir")
    try:
        rows = json.loads(match_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit("cannot read published-match.json") from exc
    if not isinstance(rows, list):
        raise SystemExit("published-match.json must contain a list")
    selected = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("task") == TASK
        and row.get("trajectory") is True
        and _is_public_pass(row)
    ]
    if len(selected) != expected:
        raise SystemExit(
            f"expected {expected} cards with saved trajectories, found {len(selected)}"
        )
    keys = [(str(row.get("job")), str(row.get("trial"))) for row in selected]
    if len(set(keys)) != len(keys):
        raise SystemExit("duplicate trial in published-match selection")
    return selected


def _load_answers(audit_dir: Path) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    with (audit_dir / "answers.jsonl").open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict) and "trial" in item:
                answers[str(item["trial"])] = item.get("answer")
    return answers


def _answer_matches(answer: Any, reference: dict[str, Any]) -> tuple[bool, bool]:
    if isinstance(answer, str):
        try:
            data = json.loads(answer)
        except Exception:
            return False, False
    elif isinstance(answer, dict):
        data = answer
    else:
        return False, False
    if not isinstance(data, dict):
        return False, False
    if _norm_formula(data.get("formula", "")) != _norm_formula(reference["formula"]):
        return False, False

    accepted_names = {_norm_text(name) for name in reference["names"]}
    name_ok = _norm_text(data.get("name", "")) in accepted_names
    accepted_ids = set(reference["ids"])
    id_ok = _norm_id(data.get("nist_id", "")) in accepted_ids
    if not (name_ok or id_ok):
        return False, False

    thermo = data.get("thermochemistry")
    if not isinstance(thermo, dict):
        return False, False
    values_ok = True
    units_ok = True
    for quantity in reference["quantities"]:
        item = thermo.get(quantity["key"])
        if not isinstance(item, dict):
            return False, False
        raw_value = item.get("value")
        if isinstance(raw_value, bool):
            return False, False
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return False, False
        values_ok = values_ok and abs(value - quantity["value"]) <= quantity["tolerance"]
        units_ok = units_ok and str(item.get("units", "")).strip() == quantity["units"]
    return values_ok, units_ok


def _run_scanner(
    request: dict[str, Any],
    remote: str | None,
    ssh_options: list[str],
    timeout: int,
) -> dict[tuple[str, str], dict[str, Any]]:
    # Reference metadata is passed as JSON, never as scanner source literals.
    request_json = json.dumps(request, ensure_ascii=False)
    program = _SCANNER + "\n\nmain(json.loads(" + repr(request_json) + "))\n"
    if remote:
        if not remote or remote.startswith("-") or any(ch.isspace() for ch in remote):
            raise SystemExit("invalid --remote destination")
        command = ["ssh"]
        command.extend(["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=20"])
        for option in ssh_options:
            command.extend(["-o", option])
        command.extend([remote, "python3", "-"])
    else:
        command = [sys.executable, "-"]
    try:
        result = subprocess.run(
            command,
            input=program,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit("trajectory scan timed out") from exc
    if result.returncode != 0:
        raise SystemExit(f"trajectory scan failed (exit {result.returncode}); check archive access")
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict) and "job" in item and "trial" in item:
            records[(str(item["job"]), str(item["trial"]))] = item
    return records


def _classify(scan: dict[str, Any], answer_ok: bool) -> dict[str, Any]:
    steps = [step for step in scan.get("step_flags") or [] if isinstance(step, dict)]
    direct_candidates = []
    parsed_candidates = []
    web_candidates = []
    for step in steps:
        if step.get("crawled"):
            continue
        if step.get("values") and step.get("compound") and step.get("cox"):
            direct_candidates.append(step)
        if (
            step.get("values")
            and step.get("compound")
            and (step.get("raw_values") or step.get("quantity"))
        ):
            parsed_candidates.append(step)
    web_candidates = [
        step
        for step in steps
        if step.get("crawled")
        and step.get("values")
        and step.get("compound")
        and step.get("cox")
    ]

    active_steps = [step for step in steps if not step.get("crawled")]
    has_values = any(step.get("values") for step in active_steps)
    has_compound = any(step.get("compound") for step in active_steps)
    has_cox = any(step.get("cox") for step in active_steps)
    has_validated = any(step.get("validated") for step in active_steps)
    has_http200 = any(step.get("http200") for step in active_steps)
    has_download = any(step.get("download") for step in active_steps)
    has_row_output = any(step.get("row_output") for step in active_steps)
    has_answer = any(
        step.get("values") and step.get("compound") for step in active_steps
    )
    value_hits = max((int(step.get("value_hits") or 0) for step in steps), default=0)
    errors = [step["step"] for step in steps if step.get("http_error")]
    success_step = None

    kind = "none"
    step = None
    if direct_candidates:
        kind, step = "direct_http", direct_candidates[0]["step"]
    elif has_http200 and has_values and has_compound and (has_cox or has_validated):
        kind = "direct_http"
        step = next(
            (
                item["step"]
                for item in steps
                if item.get("validated") and item.get("values")
            ),
            None,
        )
        if step is None:
            step = next(
                (
                    item["step"]
                    for item in steps
                    if item.get("values") and item.get("compound")
                ),
                None,
            )
        if step is None:
            step = next((item["step"] for item in steps if item.get("http200")), None)
    elif has_row_output and has_values and has_compound and has_cox:
        kind = "direct_http"
        step = next((item["step"] for item in steps if item.get("row_output")), None)
    elif has_download and has_values and has_compound and has_cox:
        kind = "direct_http"
        step = next((item["step"] for item in steps if item.get("download")), None)
    elif parsed_candidates:
        kind, step = "parsed_http", parsed_candidates[0]["step"]
    elif web_candidates:
        kind, step = "web_fetch_cache", web_candidates[0]["step"]
    elif has_answer:
        kind = "answer_only"
        step = next(
            item["step"]
            for item in steps
            if item.get("values") and item.get("compound")
        )

    if step is not None:
        success_step = step
    if errors and success_step is not None and success_step > min(errors):
        recovered = True
    else:
        recovered = False

    if kind in {"direct_http", "parsed_http", "web_fetch_cache"}:
        status = "verified" if answer_ok else "unsupported"
        level = "live_http" if kind in {"direct_http", "parsed_http"} else "cached_web"
    elif kind == "answer_only":
        status = "unsupported" if not answer_ok else "unverified"
        level = "none"
    else:
        status = "unsupported" if not answer_ok else "unverified"
        level = "none"

    cached_after_write = None
    if kind == "web_fetch_cache" and step is not None:
        writes = [
            item
            for item in scan.get("result_write_steps") or []
            if isinstance(item, int)
        ]
        if writes:
            cached_after_write = step > min(writes)

    evidence = {
        "status": status,
        "level": level,
        "kind": kind,
        "step": step,
        "compound": has_compound,
        "values": {
            "found": value_hits,
            "expected": int(scan.get("values_expected") or 0),
        },
        "source_table": has_cox,
        "http_200_observed": has_http200,
        "http_error_observed": bool(errors),
        "cached_web_content": kind == "web_fetch_cache",
        "recovered_from_http_error": recovered,
    }
    if kind == "web_fetch_cache":
        evidence["cached_after_first_result_write"] = cached_after_write
    return evidence


def _build_report(cards: list[dict[str, Any]], prompt_count: int) -> dict[str, Any]:
    by_status = Counter(card["evidence"]["status"] for card in cards)
    by_kind = Counter(card["evidence"]["kind"] for card in cards)
    by_mode = Counter(card["mode"] for card in cards)
    by_model = Counter(card["model"] for card in cards)
    live = sum(1 for card in cards if card["evidence"]["level"] == "live_http")
    cached_cards = [card for card in cards if card["evidence"]["kind"] == "web_fetch_cache"]
    cached = len(cached_cards)
    cached_after = sum(
        1
        for card in cached_cards
        if card["evidence"].get("cached_after_first_result_write") is True
    )
    cached_unknown = sum(
        1
        for card in cached_cards
        if card["evidence"].get("cached_after_first_result_write") is None
    )
    verified = by_status.get("verified", 0)
    answers = sum(1 for card in cards if card["answer"]["matches_expected"])
    rewards = Counter(f"{card['published_reward']:g}" for card in cards)
    all_verified = verified == len(cards) and answers == len(cards)

    if all_verified:
        verified_clause = (
            f"All {len(cards)} cards show a response-bearing record with the intended compound "
            "and every graded value, so the published pass is not answer-only."
        )
    else:
        verified_clause = (
            f"{verified} of {len(cards)} cards show a response-bearing record with the intended "
            "compound and every graded value; the rest do not."
        )
    if cached:
        cached_clause = (
            f"{cached} cards show WebBook page content through cached web-fetch results rather "
            "than a container HTTP response; cached responses may arrive after an initial result "
            "write, so this is supporting evidence, not proof of derivation."
        )
        if cached_after:
            cached_clause += (
                f" {cached_after} cached observation(s) were first seen after a detected result write."
            )
        elif cached_unknown == cached:
            cached_clause += " No result write was detected, so the ordering is unknown."
        else:
            cached_clause += (
                " No cached observation was first seen after a detected result write, but "
                "delivery ordering remains unproven."
            )
    else:
        cached_clause = "No card relied on cached web-fetch content."
    if prompt_count == len(cards):
        prompt_clause = (
            f"All {prompt_count} cards saw every graded value in the old prompt, so none "
            "establishes independent retrieval."
        )
    else:
        prompt_clause = (
            f"{prompt_count} of {len(cards)} cards saw every graded value in the old prompt."
        )

    limitations = [
        "The prior match is telemetry-based, not an explicit trial-selection manifest.",
        (
            "The old instruction disclosed the exact answer; all audited cards saw it."
            if prompt_count == len(cards)
            else f"The old instruction disclosed the exact answer; {prompt_count} of {len(cards)} audited cards saw it."
        ),
        (
            f"{cached} cards rely on cached web-fetch content. Cached responses may arrive after "
            "an initial result write; cached_after_first_result_write records ordering only when "
            "a result write was detected, and null means no write was detected."
            if cached
            else "No card relied on a cached web-fetch page."
        ),
        "No new live WebBook query was made during this audit; page freshness at run time is not established.",
        "A card with no observed response is unverified when the saved answer matches, and unsupported when it does not; unsupported is therefore an answer mismatch, not proof that no retrieval occurred.",
        "No raw traces, prompts, trial/job identifiers, host names, or private paths are included; no agent was rerun.",
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "task": TASK,
        "scope": "published NIST cards in the prior telemetry match",
        "selection": {
            "cards": len(cards),
            "source": "prior model/mode/reward/telemetry match",
            "explicit_trial_manifest": False,
        },
        "summary": {
            "cards": len(cards),
            "by_status": {
                key: by_status.get(key, 0)
                for key in ("verified", "unverified", "unsupported")
            },
            "by_evidence": {
                key: by_kind.get(key, 0)
                for key in ("direct_http", "parsed_http", "web_fetch_cache", "answer_only", "none")
            },
            "by_mode": dict(sorted(by_mode.items())),
            "by_model": dict(sorted(by_model.items())),
            "published_rewards": dict(sorted(rewards.items())),
            "verified_live_http": live,
            "verified_cached_web": cached,
            "cached_web_after_first_result_write": cached_after,
            "cached_web_order_unknown": cached_unknown,
            "prompt_disclosed_values": prompt_count,
            "answers_match_expected": answers,
            "all_public_cards_verified": all_verified,
        },
        "cards": cards,
        "disposition": {
            "keep_as_historical_cards": all_verified,
            "keep_as_current_hardened_scores": False,
            "independent_retrieval_established": False,
            "reason": f"{verified_clause} {cached_clause} {prompt_clause}",
        },
        "method": {
            "inputs": [
                "task tests/expected.json (expected metadata, read at run time and not copied into this report)",
                "prior published-card telemetry match",
                "saved agent trajectories, read-only",
                "saved final answers",
            ],
            "criteria": {
                "prompt_disclosure": "every graded numeric value occurs in a system or user message",
                "compound": "a compound name or registry identifier from the task reference occurs in the response-bearing output",
                "values": "every graded numeric value from the task reference occurs in the response-bearing output",
                "source_table": "a NIST reference-table marker occurs in the response-bearing output",
                "direct_http": "HTTP 200, a NIST reference-table marker, a row-form output, or a successful download alongside the expected values",
                "parsed_http": "agent output contains a raw value paired with a plus/minus term or a parsed NIST quantity table",
                "web_fetch_cache": "platform web tool returned a page marked Crawled:",
            },
            "status_definitions": {
                "verified": (
                    "the saved answer matches and response-bearing output shows the intended "
                    "compound and every graded value; evidence.level distinguishes live HTTP "
                    "from cached web content"
                ),
                "unverified": "the saved answer matches but no response evidence was observed",
                "unsupported": "the saved answer mismatches the expected answer",
            },
            "privacy": {
                "raw_trajectories_included": False,
                "trial_or_job_identifiers_included": False,
                "hostnames_or_private_paths_included": False,
                "prompts_or_observations_included": False,
            },
        },
        "limitations": limitations,
        "reproduce": {
            "command": (
                "python3 scripts/atomisticskills/historical_nist_audit.py "
                "--audit-dir <prior-audit-dir> --remote <archive-host> "
                "--jobs-root <jobs-root> --out docs/issue-1/nist.json"
            ),
            "local_command": (
                "python3 scripts/atomisticskills/historical_nist_audit.py "
                "--audit-dir <prior-audit-dir> --jobs-root <jobs-dir> "
                "--out docs/issue-1/nist.json"
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    audit_dir = Path(args.audit_dir).expanduser()
    reference = _load_reference(Path(args.expected).expanduser())
    selected = _load_selection(audit_dir, args.expect)
    answers = _load_answers(audit_dir)
    request = {
        "root": args.jobs_root,
        "reference": reference,
        "trials": [
            {"job": str(row.get("job")), "trial": str(row.get("trial"))}
            for row in selected
        ],
    }
    scans = _run_scanner(request, args.remote, args.ssh_option, args.timeout)
    missing = [
        key
        for key in ((str(row.get("job")), str(row.get("trial"))) for row in selected)
        if key not in scans
    ]
    if missing:
        raise SystemExit(f"scanner returned {len(missing)} missing records")

    ordered = sorted(
        selected,
        key=lambda row: (
            str(row.get("label") or ""),
            bool(row.get("skills")),
            str(row.get("model") or ""),
            str(row.get("trial")),
        ),
    )
    cards = []
    prompt_count = 0
    for index, row in enumerate(ordered, 1):
        key = (str(row.get("job")), str(row.get("trial")))
        scan = scans[key]
        answer_ok, units_ok = _answer_matches(answers.get(key[1]), reference)
        evidence = _classify(scan, answer_ok)
        if scan.get("prompt_disclosed"):
            prompt_count += 1
        cards.append(
            {
                "card": f"nist-{index:02d}",
                "model": str(row.get("label") or row.get("model") or "unknown"),
                "mode": "with-skill" if row.get("skills") else "no-skill",
                "published_reward": (
                    float(row.get("reward")) if row.get("reward") is not None else None
                ),
                "answer": {
                    "matches_expected": answer_ok,
                    "units_match_expected": units_ok,
                },
                "prompt": {
                    "disclosed_values": bool(scan.get("prompt_disclosed")),
                },
                "evidence": evidence,
                "independent_retrieval_established": False,
            }
        )

    report = _build_report(cards, prompt_count)
    text = json.dumps(
        report,
        ensure_ascii=False,
        indent=None if args.compact else 2,
        sort_keys=False,
    )
    if args.out == "-":
        print(text)
    else:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    summary = report["summary"]
    print(
        "NIST audit: "
        f"{summary['cards']} cards; "
        f"verified={summary['by_status']['verified']} "
        f"unverified={summary['by_status']['unverified']} "
        f"unsupported={summary['by_status']['unsupported']}; "
        f"live_http={summary['verified_live_http']} "
        f"cached_web={summary['verified_cached_web']} "
        f"cached_after_write={summary['cached_web_after_first_result_write']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
