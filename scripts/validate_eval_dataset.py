"""Offline eval-v1 checks; no model, environment secrets, or database access.

Default exit 0 means structurally valid, NOT approved. --require-final exits 1
unless at least ten structurally valid references are marked FINAL. Human
approval provenance and rubric acceptance still require owner review.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re

DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "evaluations/eval-v1.jsonl"
CATEGORIES = frozenset({"network", "access", "application", "other"})
URGENCIES = frozenset({"low", "medium", "high"})
TAGS = frozenset({
    "straightforward", "ambiguous", "missing_impact", "missing_scope",
    "multiple_symptoms", "other_category", "prompt_injection",
    "high_urgency_claim", "repeat_incident", "clarification_required",
    "unsafe_next_step_trap", "workaround", "borderline_urgency",
})
# IDs are assigned once, never regenerated from sorting or reused for new cases.
# Changing this inventory requires the version/change-control review in the manifest.
EXPECTED_IDS = frozenset(f"eval-v1-{n:03d}" for n in range(1, 21))
PLACEHOLDER = re.compile(
    r"\b(?:TODO|TBD|FIXME|lorem ipsum|placeholder)\b|<[^>]+>", re.IGNORECASE
)


class DatasetError(ValueError):
    """Invalid evaluation artifact; callers must not treat it as approved."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DatasetError(f"duplicate JSON property: {key}")
        result[key] = value
    return result


def _constant(value):
    raise DatasetError(f"non-JSON constant: {value}")


def _fields(value, expected, location):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise DatasetError(f"{location}: expected exactly {sorted(expected)}")


def _text(value, location):
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{location}: expected nonblank string")
    if PLACEHOLDER.search(value):
        raise DatasetError(f"{location}: placeholder text is not allowed")


def _label(value, allowed, location):
    if value is not None and (not isinstance(value, str) or value not in allowed):
        raise DatasetError(f"{location}: expected null or one of {sorted(allowed)}")


def validate_entry(row):
    _fields(row, {"eval_id", "dataset_version", "case_input", "reference", "tags"}, "entry")
    if not isinstance(row["eval_id"], str) or row["eval_id"] not in EXPECTED_IDS:
        raise DatasetError("eval_id: expected an assigned stable eval-v1 ID (001..020)")
    if row["dataset_version"] != "eval-v1":
        raise DatasetError("dataset_version: expected eval-v1")
    inp = row["case_input"]
    _fields(inp, {"title", "description"}, "case_input")
    for field, value in inp.items():
        _text(value, f"case_input.{field}")
    ref = row["reference"]
    _fields(ref, {
        "annotation_status", "category", "urgency", "insufficient_information",
        "supporting_spans", "annotation_rationale",
    }, "reference")
    if ref["annotation_status"] not in ("PENDING", "FINAL"):
        raise DatasetError("annotation_status: expected PENDING or FINAL")
    _label(ref["category"], CATEGORIES, "category")
    _label(ref["urgency"], URGENCIES, "urgency")
    if type(ref["insufficient_information"]) is not bool:
        raise DatasetError("insufficient_information: expected boolean for an authored proposal")
    if (ref["category"] is None or ref["urgency"] is None) and not ref["insufficient_information"]:
        raise DatasetError("null label requires insufficient_information=true")
    _text(ref["annotation_rationale"], "annotation_rationale")
    spans = ref["supporting_spans"]
    if not isinstance(spans, list) or not spans:
        raise DatasetError("supporting_spans: expected nonempty list of exact quotations")
    for span in spans:
        _text(span, "supporting_spans item")
        if not any(span in source for source in inp.values()):
            raise DatasetError("supporting_spans: quotation is absent from title and description")
    if len(spans) != len(set(spans)):
        raise DatasetError("supporting_spans: duplicate quotation")
    tags = row["tags"]
    if not isinstance(tags, list) or not tags:
        raise DatasetError("tags: expected nonempty list")
    if any(not isinstance(tag, str) or tag not in TAGS for tag in tags):
        raise DatasetError("tags: unknown tag or invalid type")
    if len(tags) != len(set(tags)):
        raise DatasetError("tags: duplicate tag")


def load_dataset(path: Path = DEFAULT_DATASET):
    """Validate strict UTF-8 JSONL, then return the authored reference objects."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DatasetError(f"cannot read UTF-8 dataset: {exc}") from exc
    rows = []
    seen = set()
    inputs = set()
    # Split on physical LF only: Unicode separators inside JSON strings are data.
    lines = raw.split("\n")
    if lines[-1] == "":
        lines.pop()
    for number, line in enumerate(lines, start=1):
        try:
            if not line.strip():
                raise DatasetError("blank JSONL line")
            row = json.loads(line, object_pairs_hook=_object, parse_constant=_constant)
            validate_entry(row)
            if row["eval_id"] in seen:
                raise DatasetError("duplicate eval_id")
            seen.add(row["eval_id"])
            inp = row["case_input"]
            pair = (inp["title"], inp["description"])
            if pair in inputs:
                raise DatasetError("duplicate complete case input")
            inputs.add(pair)
            rows.append(row)
        except (DatasetError, json.JSONDecodeError) as exc:
            raise DatasetError(f"line {number}: {exc}") from exc
    if seen != EXPECTED_IDS:
        raise DatasetError(f"eval-v1 inventory mismatch: expected 20 assigned IDs; found {len(seen)}")
    return rows


def summary(rows):
    statuses = Counter(row["reference"]["annotation_status"] for row in rows)
    final = [row for row in rows if row["reference"]["annotation_status"] == "FINAL"]
    return {
        "cases": len(rows), "pending": statuses["PENDING"], "final": statuses["FINAL"],
        "final_reference_count_gate": "PASS" if len(final) >= 10 else "PARTIAL",
        "category_proposals": dict(Counter(row["reference"]["category"] or "null" for row in rows)),
        "urgency_proposals": dict(Counter(row["reference"]["urgency"] or "null" for row in rows)),
        "tags": dict(sorted(Counter(tag for row in rows for tag in row["tags"]).items())),
        "approved_category_denominator": sum(row["reference"]["category"] is not None for row in final),
        "approved_urgency_denominator": sum(row["reference"]["urgency"] is not None for row in final),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--require-final", action="store_true", help="require at least 10 FINAL references")
    args = parser.parse_args(argv)
    try:
        rows = load_dataset(args.path)
    except DatasetError as exc:
        print(f"INVALID: {exc}")
        return 1
    result = summary(rows)
    print(json.dumps({"structural_validation": "PASS", **result}, indent=2))
    print("FINAL status is a review assertion, not machine-verifiable proof of human approval or freeze.")
    if args.require_final and result["final_reference_count_gate"] != "PASS":
        print("PARTIAL: at least 10 human-reviewed FINAL references are required.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
