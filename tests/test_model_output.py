"""Offline output-contract checks. These are NOT evidence of a real-model run."""

import pytest
from pydantic import ValidationError

from src.model_adapter import TriageSuggestion


def valid_response():
    return {
        "category": "network", "urgency": "medium",
        "reason": "The fictional users report failed VPN connections.",
        "suggested_next_step": "Check the VPN gateway's status.",
    }


def test_valid_suggestion():
    assert TriageSuggestion.model_validate(valid_response()).category == "network"


def test_invalid_category():
    data = valid_response()
    data["category"] = "critical"
    with pytest.raises(ValidationError):
        TriageSuggestion.model_validate(data)


def test_blank_reason():
    data = valid_response()
    data["reason"] = "   "
    with pytest.raises(ValidationError):
        TriageSuggestion.model_validate(data)


def test_extra_status_field_rejected():
    data = valid_response()
    data["status"] = "CLOSED"
    with pytest.raises(ValidationError):
        TriageSuggestion.model_validate(data)
