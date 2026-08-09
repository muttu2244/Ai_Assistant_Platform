"""Unit tests for MLInferenceService."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.services.ml_inference_service import MLInferenceService


def _make_model_dir(tmp_path: Path, threshold: float = 0.42, n_features: int = 3) -> Path:
    """Write minimal model.joblib + model_metadata.json to a temp dir."""
    import joblib
    import numpy as np
    from xgboost import XGBClassifier

    feature_cols = [f"feat_{i}" for i in range(n_features)]

    model = XGBClassifier(n_estimators=5, max_depth=2, random_state=0)
    X = np.random.default_rng(0).random((20, n_features))
    y = (X[:, 0] > 0.5).astype(int)
    model.fit(X, y)

    joblib.dump(model, tmp_path / "model.joblib")
    metadata = {
        "model_version": "test-v1",
        "threshold": threshold,
        "feature_columns": feature_cols,
        "feature_engineering_enabled": False,
        "engineered_feature_names": [],
        "roc_auc": 0.70,
        "pr_auc": 0.40,
        "training_rows": 20,
        "label_column": "is_reopened",
    }
    (tmp_path / "model_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return tmp_path


# ── Load ──────────────────────────────────────────────────────────────────────

def test_load_sets_is_loaded(tmp_path):
    pytest.importorskip("joblib")
    pytest.importorskip("xgboost")
    model_dir = _make_model_dir(tmp_path)
    svc = MLInferenceService(model_dir)
    assert not svc.is_loaded
    svc.load()
    assert svc.is_loaded


def test_load_reads_threshold(tmp_path):
    pytest.importorskip("joblib")
    pytest.importorskip("xgboost")
    model_dir = _make_model_dir(tmp_path, threshold=0.38)
    svc = MLInferenceService(model_dir)
    svc.load()
    assert svc._threshold == pytest.approx(0.38)


def test_load_raises_if_model_missing(tmp_path):
    pytest.importorskip("joblib")
    svc = MLInferenceService(tmp_path)
    with pytest.raises(FileNotFoundError, match="model.joblib"):
        svc.load()


def test_load_raises_if_metadata_missing(tmp_path):
    pytest.importorskip("joblib")
    pytest.importorskip("xgboost")
    import joblib
    from xgboost import XGBClassifier
    import numpy as np

    model = XGBClassifier(n_estimators=5)
    model.fit(np.zeros((10, 2)), [0] * 5 + [1] * 5)
    joblib.dump(model, tmp_path / "model.joblib")

    svc = MLInferenceService(tmp_path)
    with pytest.raises(FileNotFoundError, match="model_metadata"):
        svc.load()


# ── score_tickets ─────────────────────────────────────────────────────────────

def test_score_tickets_raises_if_not_loaded(tmp_path):
    svc = MLInferenceService(tmp_path)
    with pytest.raises(RuntimeError, match="not loaded"):
        svc.score_tickets([{"ticket_id": "1"}])


def test_score_tickets_returns_one_result_per_ticket(tmp_path):
    pytest.importorskip("joblib")
    pytest.importorskip("xgboost")
    model_dir = _make_model_dir(tmp_path)
    svc = MLInferenceService(model_dir)
    svc.load()
    tickets = [{"ticket_id": "A"}, {"ticket_id": "B"}, {"ticket_id": "C"}]
    results = svc.score_tickets(tickets)
    assert len(results) == 3


def test_score_tickets_result_has_required_fields(tmp_path):
    pytest.importorskip("joblib")
    pytest.importorskip("xgboost")
    model_dir = _make_model_dir(tmp_path)
    svc = MLInferenceService(model_dir)
    svc.load()
    results = svc.score_tickets([{"ticket_id": "T1", "title": "Invoice error", "customer_priority": "high"}])
    r = results[0]
    assert r["ticket_id"] == "T1"
    assert 0.0 <= r["risk_score"] <= 1.0
    assert r["predicted_label"] in (0, 1)
    assert r["threshold_used"] == pytest.approx(0.42)
    assert r["model_version"] == "test-v1"
    assert r["ranked_priority"] == 1


def test_score_tickets_threshold_override(tmp_path):
    pytest.importorskip("joblib")
    pytest.importorskip("xgboost")
    model_dir = _make_model_dir(tmp_path, threshold=0.42)
    svc = MLInferenceService(model_dir)
    svc.load()
    results = svc.score_tickets([{"ticket_id": "T1"}], threshold_override=0.0)
    # threshold=0.0 means everything should be predicted positive
    assert results[0]["predicted_label"] == 1
    assert results[0]["threshold_used"] == pytest.approx(0.0)


def test_score_tickets_ranked_priority_order(tmp_path):
    pytest.importorskip("joblib")
    pytest.importorskip("xgboost")
    model_dir = _make_model_dir(tmp_path)
    svc = MLInferenceService(model_dir)
    svc.load()
    tickets = [{"ticket_id": str(i)} for i in range(5)]
    results = svc.score_tickets(tickets)
    ranks = [r["ranked_priority"] for r in results]
    # Ranks must be a permutation of 1..5
    assert sorted(ranks) == list(range(1, 6))


def test_score_tickets_risk_score_in_range(tmp_path):
    pytest.importorskip("joblib")
    pytest.importorskip("xgboost")
    model_dir = _make_model_dir(tmp_path)
    svc = MLInferenceService(model_dir)
    svc.load()
    tickets = [{"ticket_id": str(i), "title": f"ticket {i}"} for i in range(10)]
    results = svc.score_tickets(tickets)
    for r in results:
        assert 0.0 <= r["risk_score"] <= 1.0
