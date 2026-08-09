"""Integration tests for POST /api/predictive/ml-score endpoint."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.chat_ui.app import app

client = TestClient(app)

URL = "/api/predictive/ml-score"

# Patch target: where get_ml_inference_service is defined (local import in endpoint)
_PATCH_TARGET = "src.services.ml_inference_service.get_ml_inference_service"


def _mock_service(risk_score: float = 0.37, threshold: float = 0.42) -> MagicMock:
    """Return a mock MLInferenceService that returns a fixed score."""
    svc = MagicMock()
    svc.is_loaded = True
    svc.score_tickets.return_value = [
        {
            "ticket_id": "12345",
            "risk_score": risk_score,
            "predicted_label": int(risk_score >= threshold),
            "threshold_used": threshold,
            "model_version": "20260809T000000Z",
            "ranked_priority": 1,
        }
    ]
    return svc


# ── Happy path ────────────────────────────────────────────────────────────────

def test_ml_score_returns_200_with_valid_ticket():
    with patch(_PATCH_TARGET, return_value=_mock_service()):
        resp = client.post(URL, json={"tickets": [{"ticket_id": "12345", "title": "Invoice error"}]})
    assert resp.status_code == 200


def test_ml_score_response_schema():
    with patch(_PATCH_TARGET, return_value=_mock_service()):
        resp = client.post(URL, json={"tickets": [{"ticket_id": "12345", "title": "Invoice error"}]})
    body = resp.json()
    assert "scores" in body
    assert "model_version" in body
    assert "threshold_used" in body
    assert "total_tickets" in body
    assert "predicted_positive_count" in body


def test_ml_score_ticket_fields():
    with patch(_PATCH_TARGET, return_value=_mock_service(risk_score=0.37)):
        resp = client.post(URL, json={"tickets": [{"ticket_id": "12345", "title": "Claim denial timeout"}]})
    score = resp.json()["scores"][0]
    assert score["ticket_id"] == "12345"
    assert "risk_score" in score
    assert "predicted_label" in score
    assert "threshold_used" in score
    assert "ranked_priority" in score
    assert score["ranked_priority"] == 1


def test_ml_score_above_threshold_flags_ticket():
    with patch(_PATCH_TARGET, return_value=_mock_service(risk_score=0.45, threshold=0.42)):
        resp = client.post(URL, json={"tickets": [{"ticket_id": "99", "title": "Crash on invoice"}]})
    body = resp.json()
    assert body["scores"][0]["predicted_label"] == 1
    assert body["predicted_positive_count"] == 1


def test_ml_score_below_threshold_does_not_flag():
    with patch(_PATCH_TARGET, return_value=_mock_service(risk_score=0.30, threshold=0.42)):
        resp = client.post(URL, json={"tickets": [{"ticket_id": "99", "title": "Minor UI issue"}]})
    body = resp.json()
    assert body["scores"][0]["predicted_label"] == 0
    assert body["predicted_positive_count"] == 0


def test_ml_score_threshold_override_respected():
    svc = MagicMock()
    svc.is_loaded = True
    svc.score_tickets.return_value = [
        {
            "ticket_id": "T1",
            "risk_score": 0.37,
            "predicted_label": 1,
            "threshold_used": 0.35,
            "model_version": "20260809T000000Z",
            "ranked_priority": 1,
        }
    ]
    with patch(_PATCH_TARGET, return_value=svc):
        resp = client.post(URL, json={"tickets": [{"ticket_id": "T1"}], "threshold_override": 0.35})
    assert resp.status_code == 200
    svc.score_tickets.assert_called_once()
    call_kwargs = svc.score_tickets.call_args
    assert call_kwargs.kwargs.get("threshold_override") == 0.35 or call_kwargs.args[1] == 0.35


def test_ml_score_multiple_tickets_ranked():
    svc = MagicMock()
    svc.is_loaded = True
    svc.score_tickets.return_value = [
        {"ticket_id": "A", "risk_score": 0.45, "predicted_label": 1, "threshold_used": 0.42, "model_version": "v1", "ranked_priority": 1},
        {"ticket_id": "B", "risk_score": 0.30, "predicted_label": 0, "threshold_used": 0.42, "model_version": "v1", "ranked_priority": 2},
    ]
    with patch(_PATCH_TARGET, return_value=svc):
        resp = client.post(URL, json={"tickets": [{"ticket_id": "A"}, {"ticket_id": "B"}]})
    body = resp.json()
    assert body["total_tickets"] == 2
    assert body["predicted_positive_count"] == 1
    ranks = [s["ranked_priority"] for s in body["scores"]]
    assert ranks == [1, 2]


# ── Validation errors ─────────────────────────────────────────────────────────

def test_ml_score_empty_tickets_list_rejected():
    with patch(_PATCH_TARGET, return_value=_mock_service()):
        resp = client.post(URL, json={"tickets": []})
    assert resp.status_code == 422


def test_ml_score_missing_tickets_field_rejected():
    with patch(_PATCH_TARGET, return_value=_mock_service()):
        resp = client.post(URL, json={})
    assert resp.status_code == 422


def test_ml_score_invalid_threshold_override_rejected():
    with patch(_PATCH_TARGET, return_value=_mock_service()):
        resp = client.post(URL, json={"tickets": [{"ticket_id": "1"}], "threshold_override": 1.5})
    assert resp.status_code == 422


# ── Error handling ────────────────────────────────────────────────────────────

def test_ml_score_returns_503_when_model_not_found():
    with patch(
        _PATCH_TARGET,
        side_effect=FileNotFoundError("model.joblib not found"),
    ):
        resp = client.post(URL, json={"tickets": [{"ticket_id": "1"}]})
    assert resp.status_code == 503
    assert "Run the offline training script" in resp.json()["detail"]


def test_ml_score_returns_500_on_scoring_failure():
    svc = MagicMock()
    svc.is_loaded = True
    svc.score_tickets.side_effect = RuntimeError("feature mismatch")
    with patch(_PATCH_TARGET, return_value=svc):
        resp = client.post(URL, json={"tickets": [{"ticket_id": "1"}]})
    assert resp.status_code == 500
