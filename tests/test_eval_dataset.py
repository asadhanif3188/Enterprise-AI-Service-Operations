"""Offline artifact and negative-control checks; no predictions or approval claims."""

from copy import deepcopy
import json
from typing import get_args

import pytest

from scripts import validate_eval_dataset as validator
from src.model_adapter import TriageSuggestion


@pytest.fixture
def rows():
    return validator.load_dataset()


def write_rows(tmp_path, rows):
    path = tmp_path / "eval.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_authored_dataset_and_current_contract(rows):
    assert len(rows) == 20
    assert {r["eval_id"] for r in rows} == validator.EXPECTED_IDS
    assert validator.CATEGORIES == set(get_args(TriageSuggestion.model_fields["category"].annotation))
    assert validator.URGENCIES == set(get_args(TriageSuggestion.model_fields["urgency"].annotation))
    assert sum("straightforward" in row["tags"] for row in rows) >= 12
    assert any("prompt_injection" in row["tags"] for row in rows)
    repeats = [row for row in rows if "repeat_incident" in row["tags"]]
    assert len(repeats) == 2
    assert repeats[0]["case_input"]["title"] == repeats[1]["case_input"]["title"]
    assert repeats[0]["reference"]["urgency"] != repeats[1]["reference"]["urgency"]


@pytest.mark.parametrize("path,value", [
    (("eval_id",), "eval-v1-999"),
    (("eval_id",), 1),
    (("dataset_version",), "eval-v2"),
    (("case_input",), []),
    (("case_input", "title"), "  "),
    (("case_input", "description"), None),
    (("case_input", "title"), "<independently authored synthetic title>"),
    (("case_input", "description"), "TODO replace this case"),
    (("reference",), None),
    (("reference", "annotation_status"), "APPROVED"),
    (("reference", "annotation_status"), ["FINAL"]),
    (("reference", "category"), "unknown"),
    (("reference", "category"), []),
    (("reference", "urgency"), "critical"),
    (("reference", "urgency"), True),
    (("reference", "insufficient_information"), None),
    (("reference", "insufficient_information"), 1),
    (("reference", "annotation_rationale"), ""),
    (("reference", "annotation_rationale"), "TBD"),
    (("reference", "supporting_spans"), []),
    (("reference", "supporting_spans"), "not a list"),
    (("reference", "supporting_spans"), [42]),
    (("reference", "supporting_spans"), ["Invented evidence not in the input"]),
    (("tags",), []),
    (("tags",), "ambiguous"),
    (("tags",), ["unapproved_tag"]),
    (("tags",), ["ambiguous", "ambiguous"]),
    (("tags",), [[]]),
])
def test_rejects_invalid_fields(rows, tmp_path, path, value):
    target = rows[0]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(validator.DatasetError):
        validator.load_dataset(write_rows(tmp_path, rows))


@pytest.mark.parametrize("nested", [None, "case_input", "reference"])
@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_rejects_wrong_field_sets(rows, tmp_path, nested, mutation):
    target = rows[0] if nested is None else rows[0][nested]
    if mutation == "missing":
        target.pop(next(iter(target)))
    else:
        target["unexpected"] = "extra"
    with pytest.raises(validator.DatasetError, match="expected exactly"):
        validator.load_dataset(write_rows(tmp_path, rows))


@pytest.mark.parametrize("raw", [
    "", "\n", "{} {}\n", "{\n}\n", "[]\n", "null\n", "{broken}\n",
    '{"eval_id":"eval-v1-001","eval_id":"eval-v1-002"}\n',
    '{"nested":{"category":"network","category":"access"}}\n',
    '{"number":NaN}\n', '{"number":Infinity}\n', '{"number":-Infinity}\n',
    '\ufeff{}\n',
])
def test_rejects_malformed_jsonl(tmp_path, raw):
    path = tmp_path / "malformed.jsonl"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(validator.DatasetError):
        validator.load_dataset(path)


def test_rejects_non_utf8(tmp_path):
    path = tmp_path / "bad-encoding.jsonl"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(validator.DatasetError, match="UTF-8"):
        validator.load_dataset(path)


def test_missing_file_is_diagnostic(tmp_path):
    assert validator.main([str(tmp_path / "absent.jsonl")]) == 1


@pytest.mark.parametrize("mutation", ["duplicate_id", "missing_id", "duplicate_input", "blank_line"])
def test_rejects_inventory_corruption(rows, tmp_path, mutation):
    if mutation == "duplicate_id":
        rows[1]["eval_id"] = rows[0]["eval_id"]
    elif mutation == "missing_id":
        rows.pop()
    elif mutation == "duplicate_input":
        rows[1]["case_input"] = deepcopy(rows[0]["case_input"])
        rows[1]["reference"] = deepcopy(rows[0]["reference"])
    path = write_rows(tmp_path, rows)
    if mutation == "blank_line":
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n")
    with pytest.raises(validator.DatasetError):
        validator.load_dataset(path)


def test_duplicate_evidence_rejected(rows, tmp_path):
    spans = rows[0]["reference"]["supporting_spans"]
    spans.append(spans[0])
    with pytest.raises(validator.DatasetError, match="duplicate quotation"):
        validator.load_dataset(write_rows(tmp_path, rows))


@pytest.mark.parametrize("dimension", ["category", "urgency"])
def test_null_requires_explicit_uncertainty(rows, tmp_path, dimension):
    rows[0]["reference"][dimension] = None
    with pytest.raises(validator.DatasetError, match="null label requires"):
        validator.load_dataset(write_rows(tmp_path, rows))
    rows[0]["reference"]["insufficient_information"] = True
    assert validator.load_dataset(write_rows(tmp_path, rows))[0]["reference"][dimension] is None


def test_reordering_does_not_change_identity(rows, tmp_path):
    original_ids = {row["eval_id"] for row in rows}
    assert {row["eval_id"] for row in validator.load_dataset(write_rows(tmp_path, rows[::-1]))} == original_ids


def test_pending_never_enters_approved_denominators(rows):
    for row in rows:
        row["reference"]["annotation_status"] = "PENDING"
    result = validator.summary(rows)
    assert result["final"] == 0
    assert result["approved_category_denominator"] == 0
    assert result["approved_urgency_denominator"] == 0
    assert result["final_reference_count_gate"] == "PARTIAL"


@pytest.mark.parametrize("final_count,exit_code", [(0, 1), (9, 1), (10, 0), (20, 0)])
def test_final_count_boundary_uses_only_final_status(rows, tmp_path, final_count, exit_code, capsys):
    # Synthetic status changes live only in pytest's temporary file; not approvals.
    for i, row in enumerate(rows):
        row["reference"]["annotation_status"] = "FINAL" if i < final_count else "PENDING"
    path = write_rows(tmp_path, rows)
    assert validator.main([str(path), "--require-final"]) == exit_code
    assert "not machine-verifiable proof" in capsys.readouterr().out
    assert validator.main([str(path)]) == 0  # Structural validity is a separate gate.


def test_final_null_labels_are_allowed_but_not_scorable(rows, tmp_path):
    for row in rows:
        row["reference"]["annotation_status"] = "FINAL"
    result = validator.summary(validator.load_dataset(write_rows(tmp_path, rows)))
    assert result["final"] == 20
    assert result["approved_category_denominator"] == 17
    assert result["approved_urgency_denominator"] == 16
