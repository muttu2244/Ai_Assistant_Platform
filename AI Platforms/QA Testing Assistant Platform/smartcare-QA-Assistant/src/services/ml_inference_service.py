"""ML Inference Service — loads the trained XGBoost model and scores tickets at runtime."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PRIORITY_MAP: dict[str, float] = {
    "urgent": 1.0,
    "critical": 0.95,
    "high": 0.8,
    "medium": 0.5,
    "low": 0.2,
    "p1": 1.0,
    "p2": 0.75,
    "p3": 0.5,
    "p4": 0.25,
}

_KEYWORDS = [
    "error",
    "fail",
    "timeout",
    "crash",
    "invoice",
    "claim",
    "report",
    "duplicate",
    "not working",
    "denial",
]


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


class MLInferenceService:
    """Loads a serialized XGBoost model and scores a batch of tickets."""

    def __init__(self, model_dir: Path) -> None:
        self._model_dir = Path(model_dir)
        self._model: Any = None
        self._metadata: dict[str, Any] = {}
        self._feature_columns: list[str] = []
        self._threshold: float = 0.5
        self._model_version: str = "unknown"

    def load(self) -> None:
        """Load model.joblib and model_metadata.json from model_dir."""
        try:
            import joblib
        except ImportError as exc:
            raise RuntimeError("joblib is required for ML inference: pip install joblib") from exc

        model_path = self._model_dir / "model.joblib"
        metadata_path = self._model_dir / "model_metadata.json"

        if not model_path.exists():
            raise FileNotFoundError(f"Model file not found: {model_path}")
        if not metadata_path.exists():
            raise FileNotFoundError(f"Model metadata not found: {metadata_path}")

        self._model = joblib.load(model_path)
        with metadata_path.open("r", encoding="utf-8") as f:
            self._metadata = json.load(f)

        self._feature_columns = self._metadata.get("feature_columns", [])
        self._threshold = float(self._metadata.get("threshold", 0.5))
        self._model_version = self._metadata.get("model_version", "unknown")

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def _build_features(self, tickets: list[dict[str, Any]]) -> Any:
        """Apply feature engineering and align to training columns."""
        try:
            import numpy as np
            import pandas as pd
        except ImportError as exc:
            raise RuntimeError("numpy and pandas are required for ML inference") from exc

        rows = []
        for t in tickets:
            extra = t.get("extra_fields") or {}
            row: dict[str, Any] = {**extra}
            # Copy known fields
            for field in (
                "module_name",
                "modified_functionality",
                "dependent_functionality",
                "customer_priority",
                "relationship_type",
                "work_item_type",
            ):
                if t.get(field) is not None:
                    row[field] = t[field]
            if t.get("title") is not None:
                row["title"] = t["title"]
            if t.get("changed_date") is not None:
                row["changed_date"] = t["changed_date"]
            rows.append(row)

        df = pd.DataFrame(rows)

        # ── Age features ──────────────────────────────────────────────────────
        date_col = next((c for c in ["changed_date", "created_date"] if c in df.columns), None)
        if date_col:
            dt = pd.to_datetime(df[date_col], errors="coerce", utc=True)
            if dt.notna().any():
                now = datetime.now(timezone.utc)
                age_days = (now - dt).dt.total_seconds() / 86400.0
                fill_age = float(age_days.dropna().median()) if age_days.notna().any() else 90.0
                age_days = age_days.fillna(fill_age).clip(lower=0)
                df["feat_age_days"] = age_days.astype(float)
                df["feat_age_bucket_0_30"] = (age_days <= 30).astype(int)
                df["feat_age_bucket_31_90"] = ((age_days > 30) & (age_days <= 90)).astype(int)
                df["feat_age_bucket_91_180"] = ((age_days > 90) & (age_days <= 180)).astype(int)
                df["feat_age_bucket_181_plus"] = (age_days > 180).astype(int)

        # Historical prior counts are 0 for point-in-time inference (no historical context available)
        for feat in [
            "feat_hist_module_prior_count_log1p",
            "feat_hist_feature_prior_count_log1p",
            "feat_hist_dependent_prior_count_log1p",
            "feat_hist_module_feature_prior_count_log1p",
        ]:
            if feat not in df.columns:
                df[feat] = 0.0

        # ── Title features ────────────────────────────────────────────────────
        title_col = next((c for c in ["title", "work_item_title", "summary"] if c in df.columns), None)
        if title_col:
            title = df[title_col].fillna("").astype(str)
            title_l = title.str.lower()
            df["feat_title_len_chars"] = title.str.len().astype(float)
            df["feat_title_len_words"] = title.str.split().str.len().fillna(0).astype(float)
            for kw in _KEYWORDS:
                col = f"feat_kw_{kw.replace(' ', '_')}"
                df[col] = title_l.str.contains(kw, regex=False).astype(int)

        # ── Priority feature ──────────────────────────────────────────────────
        priority_col = next(
            (c for c in ["customer_priority", "priority", "risk_level", "severity"] if c in df.columns), None
        )
        if priority_col:
            df["feat_priority_score"] = (
                df[priority_col].astype(str).str.lower().map(_PRIORITY_MAP).fillna(0.0).astype(float)
            )

        # ── Relationship / work-item type features ────────────────────────────
        if "relationship_type" in df.columns:
            rel = df["relationship_type"].fillna("").astype(str).str.lower()
            df["feat_is_direct"] = rel.str.contains("direct", regex=False).astype(int)

        if "work_item_type" in df.columns:
            wi = df["work_item_type"].fillna("").astype(str).str.lower()
            df["feat_is_bug"] = wi.str.contains("bug", regex=False).astype(int)
            df["feat_is_customer_ticket"] = wi.str.contains("customer", regex=False).astype(int)

        # ── One-hot encode categoricals, align to training columns ────────────
        non_feat_cols = {date_col} if date_col else set()
        object_cols = [c for c in df.columns if str(df[c].dtype) in {"object", "string"} and c not in non_feat_cols]
        for col in object_cols:
            df[col] = df[col].fillna("<NA>").astype(str)
        numeric_cols = [c for c in df.columns if c not in object_cols and c not in non_feat_cols]
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

        X = pd.get_dummies(df.drop(columns=list(non_feat_cols), errors="ignore"), dummy_na=True)

        # Align to training feature columns: add missing with 0, drop extra
        for col in self._feature_columns:
            if col not in X.columns:
                X[col] = 0
        X = X[self._feature_columns]

        return X.astype(float)

    def score_tickets(
        self,
        tickets: list[dict[str, Any]],
        threshold_override: float | None = None,
    ) -> list[dict[str, Any]]:
        """Score a list of ticket dicts. Returns list of score dicts."""
        if not self.is_loaded:
            raise RuntimeError("Model not loaded. Call load() first.")

        threshold = threshold_override if threshold_override is not None else self._threshold
        X = self._build_features(tickets)
        scores = self._model.predict_proba(X)[:, 1]

        sorted_indices = sorted(range(len(scores)), key=lambda i: -scores[i])
        rank_map = {idx: rank + 1 for rank, idx in enumerate(sorted_indices)}

        results = []
        for i, t in enumerate(tickets):
            risk_score = float(scores[i])
            results.append(
                {
                    "ticket_id": t.get("ticket_id", i),
                    "risk_score": round(risk_score, 6),
                    "predicted_label": int(risk_score >= threshold),
                    "threshold_used": float(threshold),
                    "model_version": self._model_version,
                    "ranked_priority": rank_map[i],
                }
            )
        return results


# ── Module-level singleton loaded lazily ─────────────────────────────────────

_service_instance: MLInferenceService | None = None


def get_ml_inference_service(model_dir: Path | None = None) -> MLInferenceService:
    """Return (and lazily initialise) the module-level inference service singleton."""
    global _service_instance
    if _service_instance is None or not _service_instance.is_loaded:
        from src.chat_ui.config import DEFAULT_ML_MODEL_DIR

        resolved_dir = model_dir or DEFAULT_ML_MODEL_DIR
        _service_instance = MLInferenceService(resolved_dir)
        _service_instance.load()
    return _service_instance
