#!/usr/bin/env python3
"""Offline baseline vs XGBoost comparator.

This script evaluates a deterministic baseline score and an XGBoost model
side-by-side on the same dataset, then writes comparison artifacts.

Usage example:
    python tools/offline_compare_baseline_xgboost.py \
        --input-csv probable_recurrence_candidates.csv \
        --label-column is_reopened \
        --id-column work_item_id \
        --output-dir artifacts/offline_model_compare
"""

from __future__ import annotations

import argparse
import inspect
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def _require_dependencies() -> tuple[object, object, object, object, object, object, object]:
    try:
        import numpy as np
        import pandas as pd
        from sklearn.metrics import (
            accuracy_score,
            average_precision_score,
            f1_score,
            precision_score,
            recall_score,
            roc_auc_score,
        )
        from sklearn.model_selection import train_test_split
        from xgboost import XGBClassifier
    except Exception as exc:
        raise RuntimeError(
            "Missing required packages. Install with: "
            "pip install pandas numpy scikit-learn xgboost"
        ) from exc

    return (
        np,
        pd,
        accuracy_score,
        average_precision_score,
        f1_score,
        precision_score,
        recall_score,
        roc_auc_score,
        train_test_split,
        XGBClassifier,
    )


def _normalize_col_name(name: str) -> str:
    return "".join(ch.lower() for ch in name.strip() if ch.isalnum() or ch == "_")


def _find_column(candidates: Iterable[str], columns: Iterable[str]) -> str | None:
    normalized_lookup = {_normalize_col_name(c): c for c in columns}
    for cand in candidates:
        found = normalized_lookup.get(_normalize_col_name(cand))
        if found:
            return found
    return None


def _is_leakage_feature_name(column_name: str) -> bool:
    name = _normalize_col_name(column_name)
    leakage_tokens = (
        "isreopened",
        "iseverreopened",
        "reopen",
        "labelsource",
        "labeldefinition",
        "labelwindow",
        "terminalstatesused",
        "labelstatus",
        "labelerror",
    )
    return any(token in name for token in leakage_tokens)


def _coerce_binary_label(series: object, np: object) -> object:
    s = series.astype(str).str.strip().str.lower()
    positive = {"1", "true", "t", "yes", "y", "reopened", "positive"}
    negative = {"0", "false", "f", "no", "n", "closed", "negative"}

    out = []
    for raw in s:
        if raw in positive:
            out.append(1)
            continue
        if raw in negative:
            out.append(0)
            continue
        try:
            val = float(raw)
            out.append(1 if val > 0 else 0)
        except ValueError:
            out.append(np.nan)
    return np.array(out)


def _coerce_binary_label_custom(series: object, np: object, positive: set[str], negative: set[str]) -> object:
    s = series.astype(str).str.strip().str.lower()
    out = []
    for raw in s:
        if raw in positive:
            out.append(1)
            continue
        if raw in negative:
            out.append(0)
            continue
        out.append(np.nan)
    return np.array(out)


def _minmax(series: object, np: object) -> object:
    vals = series.astype(float)
    finite = vals[np.isfinite(vals)]
    if len(finite) == 0:
        return np.zeros(len(vals))
    vmin = float(np.min(finite))
    vmax = float(np.max(finite))
    if math.isclose(vmin, vmax):
        return np.zeros(len(vals))
    return (vals - vmin) / (vmax - vmin)


def _derive_baseline_score(df: object, np: object) -> tuple[object, str]:
    direct_candidates = [
        "baseline_score",
        "impact_score",
        "recurrence_score",
        "score",
        "risk_score",
        "ticket_count",
    ]
    col = _find_column(direct_candidates, df.columns)
    if col:
        return _minmax(df[col].fillna(0), np), f"direct:{col}"

    # Weighted fallback using common predictive fields.
    dep_col = _find_column(["dependency_distance", "hop", "hop_count"], df.columns)
    cnt_col = _find_column(["ticket_count", "bug_count", "bug_cust_ticket_count"], df.columns)
    sev_col = _find_column(["severity", "priority", "risk"], df.columns)

    score = np.zeros(len(df))
    used_parts: list[str] = []

    if cnt_col:
        score += 0.65 * _minmax(df[cnt_col].fillna(0), np)
        used_parts.append(cnt_col)
    if dep_col:
        dep = df[dep_col].fillna(df[dep_col].median()).astype(float)
        inv_dep = 1.0 / (1.0 + np.maximum(dep, 0))
        score += 0.25 * _minmax(inv_dep, np)
        used_parts.append(dep_col)
    if sev_col:
        sev = df[sev_col].astype(str).str.lower().map(
            {
                "critical": 1.0,
                "high": 0.8,
                "medium": 0.5,
                "low": 0.2,
                "p1": 1.0,
                "p2": 0.75,
                "p3": 0.5,
                "p4": 0.25,
            }
        ).fillna(0.0)
        score += 0.10 * _minmax(sev, np)
        used_parts.append(sev_col)

    if not used_parts:
        return np.full(len(df), 0.5), "fallback:constant"
    return _minmax(score, np), f"fallback:{'+'.join(used_parts)}"


@dataclass
class EvalResult:
    accuracy: float
    precision: float
    recall: float
    f1: float
    roc_auc: float
    pr_auc: float
    threshold: float


@dataclass
class ThresholdCandidate:
    threshold: float
    precision: float
    recall: float
    predicted_positive: int


@dataclass
class ThresholdSelection:
    selected_threshold: float
    target_precision: float
    achieved_precision: float
    achieved_recall: float
    predicted_positive: int
    met_target: bool
    met_constraints: bool
    selection_mode: str
    search_start: float
    search_stop: float
    search_step: float
    candidates: list[ThresholdCandidate]


def _best_candidate_for_target(
    candidates: list[ThresholdCandidate],
    target_precision: float,
) -> dict[str, float | int | bool]:
    meets = [c for c in candidates if c.precision >= target_precision and c.predicted_positive > 0]
    if meets:
        best = max(meets, key=lambda c: (c.recall, c.precision, -c.threshold))
        met_target = True
    else:
        non_empty = [c for c in candidates if c.predicted_positive > 0]
        if non_empty:
            best = max(non_empty, key=lambda c: (c.precision, c.recall, -c.threshold))
        else:
            best = max(candidates, key=lambda c: (c.precision, c.recall, -c.threshold))
        met_target = False

    return {
        "target_precision": float(target_precision),
        "selected_threshold": float(best.threshold),
        "achieved_precision": float(best.precision),
        "achieved_recall": float(best.recall),
        "predicted_positive": int(best.predicted_positive),
        "met_target": bool(met_target),
    }


@dataclass
class TuningResult:
    trials_run: int
    best_trial: int
    best_metric_name: str
    best_metric_value: float
    best_params: dict[str, float | int]
    base_scale_pos_weight: float


def _evaluate(
    y_true: object,
    y_score: object,
    threshold: float,
    np: object,
    accuracy_score: object,
    precision_score: object,
    recall_score: object,
    f1_score: object,
    roc_auc_score: object,
    average_precision_score: object,
) -> EvalResult:
    y_pred = (y_score >= threshold).astype(int)

    roc = float("nan")
    pr = float("nan")
    if len(np.unique(y_true)) > 1:
        roc = float(roc_auc_score(y_true, y_score))
        pr = float(average_precision_score(y_true, y_score))

    return EvalResult(
        accuracy=float(accuracy_score(y_true, y_pred)),
        precision=float(precision_score(y_true, y_pred, zero_division=0)),
        recall=float(recall_score(y_true, y_pred, zero_division=0)),
        f1=float(f1_score(y_true, y_pred, zero_division=0)),
        roc_auc=roc,
        pr_auc=pr,
        threshold=float(threshold),
    )


def _topk_metrics(y_true: object, y_score: object, ks: list[int], np: object) -> list[dict[str, float]]:
    order = np.argsort(-y_score)
    y_sorted = y_true[order]
    total_pos = int(np.sum(y_true))
    rows: list[dict[str, float]] = []
    for k in ks:
        k_eff = min(k, len(y_sorted))
        if k_eff == 0:
            rows.append({"k": float(k), "hits": 0.0, "precision_at_k": 0.0, "recall_at_k": 0.0})
            continue
        hits = int(np.sum(y_sorted[:k_eff]))
        precision_at_k = hits / float(k_eff)
        recall_at_k = (hits / float(total_pos)) if total_pos > 0 else 0.0
        rows.append(
            {
                "k": float(k),
                "hits": float(hits),
                "precision_at_k": float(precision_at_k),
                "recall_at_k": float(recall_at_k),
            }
        )
    return rows


def _as_dict(res: EvalResult) -> dict[str, float]:
    return {
        "accuracy": res.accuracy,
        "precision": res.precision,
        "recall": res.recall,
        "f1": res.f1,
        "roc_auc": res.roc_auc,
        "pr_auc": res.pr_auc,
        "threshold": res.threshold,
    }


def _frange(start: float, stop: float, step: float) -> list[float]:
    vals: list[float] = []
    x = start
    guard = 0
    while x <= stop + (step / 10.0):
        vals.append(round(float(x), 6))
        x += step
        guard += 1
        if guard > 10000:
            break
    return vals


def _select_threshold_for_precision_target(
    *,
    y_true: object,
    y_score: object,
    target_precision: float,
    start: float,
    stop: float,
    step: float,
    min_recall: float,
    min_predicted_positives: int,
    np: object,
    precision_score: object,
    recall_score: object,
) -> ThresholdSelection:
    thresholds = _frange(start, stop, step)
    candidates: list[ThresholdCandidate] = []
    for t in thresholds:
        y_pred = (y_score >= t).astype(int)
        pred_pos = int(np.sum(y_pred))
        p = float(precision_score(y_true, y_pred, zero_division=0))
        r = float(recall_score(y_true, y_pred, zero_division=0))
        candidates.append(
            ThresholdCandidate(
                threshold=float(t),
                precision=p,
                recall=r,
                predicted_positive=pred_pos,
            )
        )

    meets_full = [
        c
        for c in candidates
        if c.precision >= target_precision
        and c.recall >= min_recall
        and c.predicted_positive >= min_predicted_positives
    ]
    meets_precision_only = [c for c in candidates if c.precision >= target_precision and c.predicted_positive > 0]
    meets_constraints_only = [
        c for c in candidates if c.recall >= min_recall and c.predicted_positive >= min_predicted_positives
    ]

    if meets_full:
        best = max(meets_full, key=lambda c: (c.recall, c.precision, -c.threshold))
        met_target = True
        met_constraints = True
        selection_mode = "target_plus_constraints"
    elif meets_precision_only:
        best = max(meets_precision_only, key=lambda c: (c.recall, c.precision, -c.threshold))
        met_target = True
        met_constraints = False
        selection_mode = "target_only_fallback"
    elif meets_constraints_only:
        best = max(meets_constraints_only, key=lambda c: (c.precision, c.recall, -c.threshold))
        met_target = False
        met_constraints = True
        selection_mode = "constraints_only_fallback"
    else:
        # Final fallback: pick the strongest precision option that still predicts at least one positive.
        non_empty = [c for c in candidates if c.predicted_positive > 0]
        if non_empty:
            best = max(non_empty, key=lambda c: (c.precision, c.recall, -c.threshold))
        else:
            best = max(candidates, key=lambda c: (c.precision, c.recall, -c.threshold))
        met_target = False
        met_constraints = False
        selection_mode = "best_effort_fallback"

    return ThresholdSelection(
        selected_threshold=float(best.threshold),
        target_precision=float(target_precision),
        achieved_precision=float(best.precision),
        achieved_recall=float(best.recall),
        predicted_positive=int(best.predicted_positive),
        met_target=met_target,
        met_constraints=met_constraints,
        selection_mode=selection_mode,
        search_start=float(start),
        search_stop=float(stop),
        search_step=float(step),
        candidates=candidates,
    )


def _sample_xgb_params(np: object, rng: object, base_scale_pos_weight: float) -> dict[str, float | int]:
    def _log_uniform(low: float, high: float) -> float:
        return float(np.exp(rng.uniform(np.log(low), np.log(high))))

    scale_low = max(1.0, base_scale_pos_weight * 0.5)
    scale_high = max(scale_low + 0.01, base_scale_pos_weight * 2.5)

    return {
        "n_estimators": int(rng.integers(150, 901)),
        "max_depth": int(rng.integers(3, 9)),
        "learning_rate": _log_uniform(0.01, 0.2),
        "subsample": float(rng.uniform(0.6, 1.0)),
        "colsample_bytree": float(rng.uniform(0.6, 1.0)),
        "min_child_weight": float(rng.uniform(1.0, 12.0)),
        "reg_lambda": _log_uniform(0.5, 20.0),
        "reg_alpha": _log_uniform(0.001, 5.0),
        "gamma": float(rng.uniform(0.0, 5.0)),
        "scale_pos_weight": float(rng.uniform(scale_low, scale_high)),
    }


def _build_data_quality_profile(df: object, y: object, np: object) -> dict[str, object]:
    missing_rates = (df.isna().mean() * 100.0).sort_values(ascending=False)
    top_missing = [
        {
            "column": str(col),
            "missing_pct": float(pct),
        }
        for col, pct in missing_rates.head(10).items()
        if float(pct) > 0.0
    ]

    nunique = df.nunique(dropna=False).sort_values(ascending=False)
    high_card_cols = [
        str(col)
        for col, val in nunique.items()
        if int(val) >= int(0.8 * max(len(df), 1))
    ][:10]

    return {
        "row_count": int(len(df)),
        "column_count": int(len(df.columns)),
        "duplicate_row_count": int(df.duplicated().sum()),
        "all_null_column_count": int((df.isna().all()).sum()),
        "top_missing_columns": top_missing,
        "high_cardinality_columns": high_card_cols,
        "label_positive_rate": float(np.mean(y)),
    }


def _build_label_diagnostics(y: object, np: object) -> dict[str, object]:
    positives = int(np.sum(y == 1))
    negatives = int(np.sum(y == 0))
    total = int(len(y))
    pos_rate = float(positives / total) if total > 0 else 0.0
    imbalance_ratio = float(negatives / max(positives, 1))

    return {
        "total": total,
        "positives": positives,
        "negatives": negatives,
        "positive_rate": pos_rate,
        "class_imbalance_ratio_neg_to_pos": imbalance_ratio,
        "is_single_class": bool(positives == 0 or negatives == 0),
    }


def _apply_feature_engineering(df: object, pd: object, np: object) -> tuple[object, list[str]]:
    engineered: list[str] = []
    out = df.copy()

    date_col = _find_column(["changed_date", "created_date", "created_at", "timestamp", "date"], out.columns)
    dt = None
    if date_col:
        dt = pd.to_datetime(out[date_col], errors="coerce", utc=True)
        if dt.notna().any():
            anchor = dt.max()
            age_days = (anchor - dt).dt.total_seconds() / 86400.0
            fill_age = float(age_days.dropna().median()) if age_days.notna().any() else 180.0
            age_days = age_days.fillna(fill_age).clip(lower=0)
            out["feat_age_days"] = age_days.astype(float)
            out["feat_age_bucket_0_30"] = (age_days <= 30).astype(int)
            out["feat_age_bucket_31_90"] = ((age_days > 30) & (age_days <= 90)).astype(int)
            out["feat_age_bucket_91_180"] = ((age_days > 90) & (age_days <= 180)).astype(int)
            out["feat_age_bucket_181_plus"] = (age_days > 180).astype(int)
            engineered.extend(
                [
                    "feat_age_days",
                    "feat_age_bucket_0_30",
                    "feat_age_bucket_31_90",
                    "feat_age_bucket_91_180",
                    "feat_age_bucket_181_plus",
                ]
            )

            # Leakage-safe historical domain counts: only past ticket volume by key (no labels used).
            hist = out[[date_col]].copy()
            hist["_dt"] = dt
            hist["_row"] = np.arange(len(out))
            hist = hist.sort_values("_dt", ascending=True, na_position="last", kind="mergesort")

            def _add_prior_count_feature(source_col: str, feature_name: str) -> None:
                key_series = out[source_col].fillna("<NA>").astype(str)
                key_sorted = key_series.iloc[hist["_row"].to_numpy()]
                prior = key_sorted.groupby(key_sorted).cumcount().astype(float)
                prior = np.log1p(prior)
                out.loc[hist["_row"].to_numpy(), feature_name] = prior.to_numpy()
                engineered.append(feature_name)

            mod_col = _find_column(["module_name"], out.columns)
            feat_col = _find_column(["modified_functionality", "feature_name"], out.columns)
            dep_col = _find_column(["dependent_functionality"], out.columns)

            if mod_col:
                _add_prior_count_feature(mod_col, "feat_hist_module_prior_count_log1p")
            if feat_col:
                _add_prior_count_feature(feat_col, "feat_hist_feature_prior_count_log1p")
            if dep_col:
                _add_prior_count_feature(dep_col, "feat_hist_dependent_prior_count_log1p")

            if mod_col and feat_col:
                combo = (
                    out[mod_col].fillna("<NA>").astype(str)
                    + "||"
                    + out[feat_col].fillna("<NA>").astype(str)
                )
                combo_sorted = combo.iloc[hist["_row"].to_numpy()]
                prior_combo = combo_sorted.groupby(combo_sorted).cumcount().astype(float)
                prior_combo = np.log1p(prior_combo)
                out.loc[hist["_row"].to_numpy(), "feat_hist_module_feature_prior_count_log1p"] = prior_combo.to_numpy()
                engineered.append("feat_hist_module_feature_prior_count_log1p")

    title_col = _find_column(["title", "work_item_title", "summary"], out.columns)
    if title_col:
        title = out[title_col].fillna("").astype(str)
        title_l = title.str.lower()
        out["feat_title_len_chars"] = title.str.len().astype(float)
        out["feat_title_len_words"] = title.str.split().str.len().fillna(0).astype(float)
        engineered.extend(["feat_title_len_chars", "feat_title_len_words"])

        keywords = [
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
        for kw in keywords:
            col = f"feat_kw_{kw.replace(' ', '_')}"
            out[col] = title_l.str.contains(kw, regex=False).astype(int)
            engineered.append(col)

    priority_col = _find_column(["customer_priority", "priority", "risk_level", "severity"], out.columns)
    if priority_col:
        priority_map = {
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
        out["feat_priority_score"] = (
            out[priority_col].astype(str).str.lower().map(priority_map).fillna(0.0).astype(float)
        )
        engineered.append("feat_priority_score")

    rel_col = _find_column(["relationship_type"], out.columns)
    if rel_col:
        rel = out[rel_col].fillna("").astype(str).str.lower()
        out["feat_is_direct"] = rel.str.contains("direct", regex=False).astype(int)
        engineered.append("feat_is_direct")

    wi_type_col = _find_column(["work_item_type", "type"], out.columns)
    if wi_type_col:
        wi = out[wi_type_col].fillna("").astype(str).str.lower()
        out["feat_is_bug"] = wi.str.contains("bug", regex=False).astype(int)
        out["feat_is_customer_ticket"] = wi.str.contains("customer", regex=False).astype(int)
        engineered.extend(["feat_is_bug", "feat_is_customer_ticket"])

    return out, engineered


def _classify_production_readiness(
    *,
    label_mode: str,
    split_mode: str,
    rows_test: int,
    positive_rate_test: float,
    baseline_eval: EvalResult,
    xgb_eval: EvalResult,
    topk_baseline: list[dict[str, float]],
    topk_xgb: list[dict[str, float]],
) -> dict[str, object]:
    checks: list[dict[str, object]] = []

    checks.append(
        {
            "name": "true_outcome_labels",
            "passed": label_mode == "explicit_binary",
            "details": "Requires true business outcome labels (not proxy/custom state mapping).",
        }
    )
    checks.append(
        {
            "name": "time_aware_split",
            "passed": split_mode == "time",
            "details": "Requires time-based train/test split to reduce leakage risk.",
        }
    )
    checks.append(
        {
            "name": "sufficient_test_size",
            "passed": rows_test >= 500,
            "details": f"Recommended minimum test rows is 500; current={rows_test}.",
        }
    )
    checks.append(
        {
            "name": "label_balance_reasonable",
            "passed": 0.02 <= positive_rate_test <= 0.98,
            "details": (
                "Test positive class rate should not be extreme; "
                f"current={positive_rate_test:.4f}."
            ),
        }
    )

    pr_auc_lift = xgb_eval.pr_auc - baseline_eval.pr_auc
    checks.append(
        {
            "name": "pr_auc_improvement",
            "passed": pr_auc_lift >= 0.10,
            "details": f"Expected PR-AUC lift >= 0.10; current lift={pr_auc_lift:+.4f}.",
        }
    )

    base_top50 = next((r for r in topk_baseline if int(r.get("k", 0)) == 50), None)
    xgb_top50 = next((r for r in topk_xgb if int(r.get("k", 0)) == 50), None)
    top50_recall_lift = 0.0
    if base_top50 and xgb_top50:
        top50_recall_lift = float(xgb_top50["recall_at_k"] - base_top50["recall_at_k"])
    checks.append(
        {
            "name": "top50_recall_non_degradation",
            "passed": top50_recall_lift >= 0.0,
            "details": f"Expected Recall@50 lift >= 0; current lift={top50_recall_lift:+.4f}.",
        }
    )

    passed_count = sum(1 for c in checks if c["passed"])
    total_count = len(checks)

    if label_mode != "explicit_binary":
        status = "exploratory"
        rationale = "Proxy/custom labels detected; run is useful for experimentation but not production claims."
    elif split_mode != "time":
        status = "pre-production"
        rationale = "True labels are present, but split is not time-based."
    elif all(c["passed"] for c in checks):
        status = "production-claimable"
        rationale = "All readiness checks passed including data quality, split strategy, and model lift gates."
    else:
        status = "pre-production"
        rationale = "True labels and time split are present, but one or more readiness gates are not met."

    return {
        "status": status,
        "rationale": rationale,
        "checks_passed": passed_count,
        "checks_total": total_count,
        "checks": checks,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline baseline vs XGBoost comparator")
    parser.add_argument("--input-csv", required=True, help="Input dataset CSV path")
    parser.add_argument("--label-column", default="", help="Binary label column name")
    parser.add_argument(
        "--positive-values",
        default="",
        help="Comma-separated positive label values (example: Active,Open,New)",
    )
    parser.add_argument(
        "--negative-values",
        default="",
        help="Comma-separated negative label values (example: Closed,Resolved,Removed)",
    )
    parser.add_argument("--id-column", default="", help="Optional record identifier column")
    parser.add_argument("--time-column", default="", help="Optional timestamp column for time split")
    parser.add_argument("--test-size", type=float, default=0.2, help="Test split ratio for random split")
    parser.add_argument("--threshold", type=float, default=0.5, help="Decision threshold")
    parser.add_argument(
        "--optimize-threshold",
        action="store_true",
        help="If set, select XGBoost threshold from validation data to meet target precision.",
    )
    parser.add_argument(
        "--target-precision",
        type=float,
        default=0.70,
        help="Precision target used when --optimize-threshold is enabled.",
    )
    parser.add_argument(
        "--threshold-search-start",
        type=float,
        default=0.70,
        help="Threshold search start (inclusive) for optimization mode.",
    )
    parser.add_argument(
        "--threshold-search-stop",
        type=float,
        default=0.99,
        help="Threshold search stop (inclusive) for optimization mode.",
    )
    parser.add_argument(
        "--threshold-search-step",
        type=float,
        default=0.01,
        help="Threshold search step for optimization mode.",
    )
    parser.add_argument(
        "--validation-size",
        type=float,
        default=0.2,
        help="Validation split ratio inside train when threshold optimization is enabled.",
    )
    parser.add_argument(
        "--min-threshold-recall",
        type=float,
        default=0.10,
        help="Minimum recall constraint for threshold selection in optimization mode.",
    )
    parser.add_argument(
        "--min-threshold-predicted-positives",
        type=int,
        default=25,
        help="Minimum predicted-positive volume constraint for threshold selection in optimization mode.",
    )
    parser.add_argument(
        "--enable-feature-engineering",
        action="store_true",
        help="Enable additional engineered features (date/title/priority/relationship signals).",
    )
    parser.add_argument(
        "--xgb-tune-trials",
        type=int,
        default=0,
        help="Number of random-search XGBoost tuning trials. Set >0 to enable tuning.",
    )
    parser.add_argument(
        "--xgb-early-stopping-rounds",
        type=int,
        default=30,
        help="Early stopping rounds used during tuning with validation data.",
    )
    parser.add_argument("--random-state", type=int, default=42, help="Random seed")
    parser.add_argument("--output-dir", default="artifacts/offline_model_compare", help="Output directory")
    return parser


def main() -> int:
    (
        np,
        pd,
        accuracy_score,
        average_precision_score,
        f1_score,
        precision_score,
        recall_score,
        roc_auc_score,
        train_test_split,
        XGBClassifier,
    ) = _require_dependencies()

    args = build_arg_parser().parse_args()
    input_path = Path(args.input_csv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        print(f"ERROR: input CSV not found: {input_path}", file=sys.stderr)
        return 2

    df = pd.read_csv(input_path)
    if df.empty:
        print("ERROR: input CSV has no rows.", file=sys.stderr)
        return 2

    candidate_labels = [
        args.label_column,
        "is_reopened",
        "reopened",
        "reopen_flag",
        "label",
        "target",
        "y",
    ]
    label_col = _find_column([c for c in candidate_labels if c], df.columns)
    label_mode = "explicit_binary"
    if not label_col:
        state_col = _find_column(["state", "work_item_state", "status"], df.columns)
        if state_col:
            label_col = state_col
            label_mode = "proxy_from_state"
        else:
            print(
                "ERROR: label column not found. Use --label-column, or provide one of: "
                "is_reopened, reopened, reopen_flag, label, target, y, state.",
                file=sys.stderr,
            )
            return 2

    pos_values = {v.strip().lower() for v in args.positive_values.split(",") if v.strip()}
    neg_values = {v.strip().lower() for v in args.negative_values.split(",") if v.strip()}
    if pos_values or neg_values:
        y = _coerce_binary_label_custom(df[label_col], np, pos_values, neg_values)
        label_mode = f"custom_map:{label_col}"
    elif label_mode == "proxy_from_state":
        y = _coerce_binary_label_custom(
            df[label_col],
            np,
            positive={"active", "new", "approved", "committed", "in progress"},
            negative={"closed", "resolved", "removed", "done", "completed"},
        )
    else:
        y = _coerce_binary_label(df[label_col], np)
    valid_mask = np.isfinite(y)
    dropped = int((~valid_mask).sum())
    df = df.loc[valid_mask].copy()
    y = y[valid_mask].astype(int)

    if len(np.unique(y)) < 2:
        print("ERROR: label column has only one class after cleaning; cannot compare models.", file=sys.stderr)
        return 2

    label_diagnostics = _build_label_diagnostics(y, np)
    data_quality_profile = _build_data_quality_profile(df, y, np)

    engineered_feature_names: list[str] = []
    if args.enable_feature_engineering:
        df, engineered_feature_names = _apply_feature_engineering(df, pd, np)

    baseline_score, baseline_source = _derive_baseline_score(df, np)

    id_col = _find_column([args.id_column] if args.id_column else [], df.columns)
    if not id_col:
        id_col = _find_column(["work_item_id", "ticket_id", "id"], df.columns)
    if not id_col:
        df["_row_id"] = np.arange(len(df))
        id_col = "_row_id"

    time_col = _find_column([args.time_column] if args.time_column else [], df.columns)
    if not time_col:
        time_col = _find_column(["created_date", "created_at", "timestamp", "date"], df.columns)

    # Build model features from all columns except ID/time/label and label-derivation fields.
    excluded = {label_col, id_col}
    if time_col:
        excluded.add(time_col)
    candidate_feature_cols = [
        c for c in df.columns if c not in excluded and not _is_leakage_feature_name(c)
    ]

    X_raw = df[candidate_feature_cols].copy()
    for col in X_raw.columns:
        if str(X_raw[col].dtype) in {"object", "string"}:
            X_raw[col] = X_raw[col].fillna("<NA>").astype(str)
        else:
            X_raw[col] = pd.to_numeric(X_raw[col], errors="coerce")
            if X_raw[col].isna().all():
                X_raw[col] = 0
            else:
                X_raw[col] = X_raw[col].fillna(X_raw[col].median())

    X_model = pd.get_dummies(X_raw, dummy_na=True)
    if X_model.shape[1] == 0:
        print("ERROR: no usable feature columns found for model training.", file=sys.stderr)
        return 2

    split_mode = "random"
    if time_col:
        time_series = pd.to_datetime(df[time_col], errors="coerce", utc=True)
        if time_series.notna().sum() >= max(10, int(0.5 * len(df))):
            # Sort by UTC nanoseconds; assign NaT rows to the end.
            sort_key = time_series.astype("int64").to_numpy(copy=True)
            sort_key[time_series.isna().to_numpy()] = np.iinfo(np.int64).max
            order = np.argsort(sort_key)
            split_at = max(1, int((1.0 - args.test_size) * len(df)))
            train_idx = order[:split_at]
            test_idx = order[split_at:]
            split_mode = "time"
        else:
            train_idx, test_idx = train_test_split(
                np.arange(len(df)),
                test_size=args.test_size,
                random_state=args.random_state,
                stratify=y,
            )
    else:
        train_idx, test_idx = train_test_split(
            np.arange(len(df)),
            test_size=args.test_size,
            random_state=args.random_state,
            stratify=y,
        )

    X_train = X_model.iloc[train_idx]
    X_test = X_model.iloc[test_idx]
    y_train = y[train_idx]
    y_test = y[test_idx]

    fit_idx = train_idx
    val_idx = train_idx
    needs_validation = bool(args.optimize_threshold or args.xgb_tune_trials > 0)
    if needs_validation:
        if split_mode == "time":
            val_cut = max(1, int((1.0 - args.validation_size) * len(train_idx)))
            fit_idx = train_idx[:val_cut]
            val_idx = train_idx[val_cut:]
        else:
            fit_idx, val_idx = train_test_split(
                train_idx,
                test_size=args.validation_size,
                random_state=args.random_state,
                stratify=y[train_idx],
            )

        # Guardrail for tiny datasets.
        if len(fit_idx) == 0 or len(val_idx) == 0:
            fit_idx = train_idx
            val_idx = train_idx

    baseline_test_score = baseline_score[test_idx]

    y_fit = y[fit_idx]
    positives = int(np.sum(y_fit == 1))
    negatives = int(np.sum(y_fit == 0))
    base_scale_pos_weight = float(negatives / positives) if positives > 0 else 1.0

    tuning_result: TuningResult | None = None
    model: object
    if args.xgb_tune_trials > 0:
        rng = np.random.default_rng(args.random_state)
        best_model = None
        best_metric = float("-inf")
        best_trial = -1
        best_params: dict[str, float | int] = {}
        metric_name = "validation_pr_auc"

        for trial in range(1, args.xgb_tune_trials + 1):
            trial_params = _sample_xgb_params(np, rng, base_scale_pos_weight)
            trial_model = XGBClassifier(
                objective="binary:logistic",
                eval_metric="logloss",
                random_state=args.random_state + trial,
                n_jobs=4,
                **trial_params,
            )

            fit_kwargs: dict[str, object] = {}
            if needs_validation and len(val_idx) > 0 and len(fit_idx) > 0:
                fit_kwargs["eval_set"] = [(X_model.iloc[val_idx], y[val_idx])]
                fit_kwargs["verbose"] = False
                fit_sig = inspect.signature(trial_model.fit)
                if "early_stopping_rounds" in fit_sig.parameters:
                    fit_kwargs["early_stopping_rounds"] = int(args.xgb_early_stopping_rounds)

            trial_model.fit(X_model.iloc[fit_idx], y[fit_idx], **fit_kwargs)
            val_score = trial_model.predict_proba(X_model.iloc[val_idx])[:, 1]

            val_metric = float("nan")
            if len(np.unique(y[val_idx])) > 1:
                val_metric = float(average_precision_score(y[val_idx], val_score))

            if np.isfinite(val_metric) and val_metric > best_metric:
                best_metric = val_metric
                best_model = trial_model
                best_trial = trial
                best_params = trial_params

        if best_model is None:
            print("ERROR: tuning failed to produce a valid model.", file=sys.stderr)
            return 2

        model = best_model
        tuning_result = TuningResult(
            trials_run=int(args.xgb_tune_trials),
            best_trial=int(best_trial),
            best_metric_name=metric_name,
            best_metric_value=float(best_metric),
            best_params=best_params,
            base_scale_pos_weight=float(base_scale_pos_weight),
        )
    else:
        model = XGBClassifier(
            n_estimators=300,
            max_depth=6,
            learning_rate=0.05,
            subsample=0.9,
            colsample_bytree=0.9,
            scale_pos_weight=float(base_scale_pos_weight),
            objective="binary:logistic",
            eval_metric="logloss",
            random_state=args.random_state,
            n_jobs=4,
        )
        model.fit(X_model.iloc[fit_idx], y[fit_idx])
    xgb_test_score = model.predict_proba(X_test)[:, 1]

    xgb_threshold = float(args.threshold)
    threshold_selection: ThresholdSelection | None = None
    multi_target_operating_points: list[dict[str, float | int | bool]] = []
    if args.optimize_threshold:
        xgb_val_score = model.predict_proba(X_model.iloc[val_idx])[:, 1]
        threshold_selection = _select_threshold_for_precision_target(
            y_true=y[val_idx],
            y_score=xgb_val_score,
            target_precision=float(args.target_precision),
            start=float(args.threshold_search_start),
            stop=float(args.threshold_search_stop),
            step=float(args.threshold_search_step),
            min_recall=float(args.min_threshold_recall),
            min_predicted_positives=int(args.min_threshold_predicted_positives),
            np=np,
            precision_score=precision_score,
            recall_score=recall_score,
        )
        xgb_threshold = float(threshold_selection.selected_threshold)

        for tgt in [0.40, 0.50, 0.60, 0.70]:
            multi_target_operating_points.append(
                _best_candidate_for_target(threshold_selection.candidates, float(tgt))
            )

    baseline_eval = _evaluate(
        y_test,
        baseline_test_score,
        args.threshold,
        np,
        accuracy_score,
        precision_score,
        recall_score,
        f1_score,
        roc_auc_score,
        average_precision_score,
    )
    xgb_eval = _evaluate(
        y_test,
        xgb_test_score,
        xgb_threshold,
        np,
        accuracy_score,
        precision_score,
        recall_score,
        f1_score,
        roc_auc_score,
        average_precision_score,
    )

    ks = [10, 25, 50, 100]
    topk_baseline = _topk_metrics(y_test, baseline_test_score, ks, np)
    topk_xgb = _topk_metrics(y_test, xgb_test_score, ks, np)

    production_readiness = _classify_production_readiness(
        label_mode=label_mode,
        split_mode=split_mode,
        rows_test=int(len(test_idx)),
        positive_rate_test=float(np.mean(y_test)),
        baseline_eval=baseline_eval,
        xgb_eval=xgb_eval,
        topk_baseline=topk_baseline,
        topk_xgb=topk_xgb,
    )

    out = pd.DataFrame(
        {
            "id": df.iloc[test_idx][id_col].values,
            "label": y_test,
            "baseline_score": baseline_test_score,
            "xgb_score": xgb_test_score,
        }
    )
    out["baseline_pred"] = (out["baseline_score"] >= args.threshold).astype(int)
    out["xgb_pred"] = (out["xgb_score"] >= xgb_threshold).astype(int)
    out["agree"] = (out["baseline_pred"] == out["xgb_pred"]).astype(int)

    predictions_csv = output_dir / "model_compare_predictions.csv"
    disagreements_csv = output_dir / "model_compare_disagreements.csv"
    metrics_json = output_dir / "model_compare_metrics.json"
    topk_csv = output_dir / "model_compare_topk.csv"
    threshold_search_csv = output_dir / "model_compare_threshold_search.csv"
    summary_md = output_dir / "model_compare_report.md"

    out.sort_values("xgb_score", ascending=False).to_csv(predictions_csv, index=False)
    out[out["agree"] == 0].sort_values("xgb_score", ascending=False).to_csv(disagreements_csv, index=False)

    topk_df = pd.concat(
        [
            pd.DataFrame(topk_baseline).assign(model="baseline"),
            pd.DataFrame(topk_xgb).assign(model="xgboost"),
        ],
        ignore_index=True,
    )
    topk_df.to_csv(topk_csv, index=False)

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_csv": str(input_path),
        "label_column": label_col,
        "id_column": id_col,
        "time_column": time_col or "",
        "rows_total": int(len(valid_mask)),
        "rows_used": int(len(df)),
        "rows_dropped_invalid_label": dropped,
        "rows_train": int(len(train_idx)),
        "rows_test": int(len(test_idx)),
        "positive_rate_train": float(np.mean(y_train)),
        "positive_rate_test": float(np.mean(y_test)),
        "feature_count_model_input": int(X_model.shape[1]),
        "feature_engineering": {
            "enabled": bool(args.enable_feature_engineering),
            "engineered_feature_count": int(len(engineered_feature_names)),
            "engineered_features": engineered_feature_names,
        },
        "label_diagnostics": label_diagnostics,
        "data_quality_profile": data_quality_profile,
        "excluded_leakage_columns": [c for c in df.columns if _is_leakage_feature_name(c)],
        "label_mode": label_mode,
        "split_mode": split_mode,
        "baseline_score_source": baseline_source,
        "threshold": float(args.threshold),
        "xgboost_threshold_applied": float(xgb_threshold),
        "threshold_optimization": {
            "enabled": bool(args.optimize_threshold),
            "target_precision": float(args.target_precision),
            "search_start": float(args.threshold_search_start),
            "search_stop": float(args.threshold_search_stop),
            "search_step": float(args.threshold_search_step),
            "validation_size": float(args.validation_size),
            "constraints": {
                "min_recall": float(args.min_threshold_recall),
                "min_predicted_positives": int(args.min_threshold_predicted_positives),
            },
            "operating_points": multi_target_operating_points,
            "selected": (
                {
                    "selected_threshold": threshold_selection.selected_threshold,
                    "target_precision": threshold_selection.target_precision,
                    "achieved_precision": threshold_selection.achieved_precision,
                    "achieved_recall": threshold_selection.achieved_recall,
                    "predicted_positive": threshold_selection.predicted_positive,
                    "met_target": threshold_selection.met_target,
                    "met_constraints": threshold_selection.met_constraints,
                    "selection_mode": threshold_selection.selection_mode,
                }
                if threshold_selection
                else None
            ),
        },
        "xgboost_tuning": (
            {
                "enabled": True,
                "trials_run": tuning_result.trials_run,
                "best_trial": tuning_result.best_trial,
                "best_metric_name": tuning_result.best_metric_name,
                "best_metric_value": tuning_result.best_metric_value,
                "base_scale_pos_weight": tuning_result.base_scale_pos_weight,
                "best_params": tuning_result.best_params,
            }
            if tuning_result
            else {
                "enabled": False,
                "trials_run": 0,
                "best_trial": 0,
                "best_metric_name": "",
                "best_metric_value": float("nan"),
                "base_scale_pos_weight": float(base_scale_pos_weight),
                "best_params": {},
            }
        ),
        "metrics": {
            "baseline": _as_dict(baseline_eval),
            "xgboost": _as_dict(xgb_eval),
        },
        "metric_lift_xgboost_minus_baseline": {
            "accuracy": xgb_eval.accuracy - baseline_eval.accuracy,
            "precision": xgb_eval.precision - baseline_eval.precision,
            "recall": xgb_eval.recall - baseline_eval.recall,
            "f1": xgb_eval.f1 - baseline_eval.f1,
            "roc_auc": xgb_eval.roc_auc - baseline_eval.roc_auc,
            "pr_auc": xgb_eval.pr_auc - baseline_eval.pr_auc,
        },
        "topk": {
            "baseline": topk_baseline,
            "xgboost": topk_xgb,
        },
        "production_readiness": production_readiness,
        "artifacts": {
            "predictions_csv": str(predictions_csv),
            "disagreements_csv": str(disagreements_csv),
            "metrics_json": str(metrics_json),
            "topk_csv": str(topk_csv),
            "threshold_search_csv": str(threshold_search_csv),
            "report_md": str(summary_md),
        },
    }

    with metrics_json.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    if threshold_selection:
        threshold_df = pd.DataFrame(
            [
                {
                    "threshold": c.threshold,
                    "precision": c.precision,
                    "recall": c.recall,
                    "predicted_positive": c.predicted_positive,
                }
                for c in threshold_selection.candidates
            ]
        )
        threshold_df.to_csv(threshold_search_csv, index=False)

    baseline = summary["metrics"]["baseline"]
    xgb = summary["metrics"]["xgboost"]
    lift = summary["metric_lift_xgboost_minus_baseline"]
    readiness = summary["production_readiness"]
    md = [
        "# Offline Model Comparison Report",
        "",
        f"Generated (UTC): {summary['generated_at_utc']}",
        f"Input CSV: {summary['input_csv']}",
        f"Label column: {summary['label_column']}",
        f"Label mode: {summary['label_mode']}",
        f"Split mode: {summary['split_mode']}",
        f"Rows used: {summary['rows_used']} / {summary['rows_total']} (dropped invalid labels: {summary['rows_dropped_invalid_label']})",
        f"Train/Test: {summary['rows_train']} / {summary['rows_test']}",
        f"Baseline score source: {summary['baseline_score_source']}",
        f"Feature engineering enabled: {'yes' if summary['feature_engineering']['enabled'] else 'no'}",
        f"Engineered feature count: {summary['feature_engineering']['engineered_feature_count']}",
        f"Label positive rate: {summary['label_diagnostics']['positive_rate']:.4f}",
        f"Label imbalance (neg:pos): {summary['label_diagnostics']['class_imbalance_ratio_neg_to_pos']:.2f}",
        f"Baseline threshold: {summary['threshold']:.4f}",
        f"XGBoost threshold: {summary['xgboost_threshold_applied']:.4f}",
        "",
        "## Metrics",
        "",
        "| Metric | Baseline | XGBoost | Lift (XGB - Base) |",
        "|---|---:|---:|---:|",
        f"| Accuracy | {baseline['accuracy']:.4f} | {xgb['accuracy']:.4f} | {lift['accuracy']:+.4f} |",
        f"| Precision | {baseline['precision']:.4f} | {xgb['precision']:.4f} | {lift['precision']:+.4f} |",
        f"| Recall | {baseline['recall']:.4f} | {xgb['recall']:.4f} | {lift['recall']:+.4f} |",
        f"| F1 | {baseline['f1']:.4f} | {xgb['f1']:.4f} | {lift['f1']:+.4f} |",
        f"| ROC-AUC | {baseline['roc_auc']:.4f} | {xgb['roc_auc']:.4f} | {lift['roc_auc']:+.4f} |",
        f"| PR-AUC | {baseline['pr_auc']:.4f} | {xgb['pr_auc']:.4f} | {lift['pr_auc']:+.4f} |",
        "",
        "## Threshold Selection",
        "",
        f"Optimization enabled: {'yes' if summary['threshold_optimization']['enabled'] else 'no'}",
    ]

    if threshold_selection:
        selected = summary["threshold_optimization"]["selected"]
        md.extend(
            [
                f"Search range: {summary['threshold_optimization']['search_start']:.2f} to {summary['threshold_optimization']['search_stop']:.2f} (step {summary['threshold_optimization']['search_step']:.2f})",
                f"Target precision: {summary['threshold_optimization']['target_precision']:.2f}",
                f"Constraint min recall: {summary['threshold_optimization']['constraints']['min_recall']:.2f}",
                f"Constraint min predicted positives: {summary['threshold_optimization']['constraints']['min_predicted_positives']}",
                f"Selected threshold: {selected['selected_threshold']:.4f}",
                f"Validation precision/recall at selected threshold: {selected['achieved_precision']:.4f} / {selected['achieved_recall']:.4f}",
                f"Target met on validation: {'yes' if selected['met_target'] else 'no'}",
                f"Constraints met on validation: {'yes' if selected['met_constraints'] else 'no'}",
                f"Selection mode: {selected['selection_mode']}",
                "",
            ]
        )

        op_points = summary["threshold_optimization"].get("operating_points", [])
        if op_points:
            md.extend(
                [
                    "Operating points by target precision:",
                    "",
                    "| Target Precision | Selected Threshold | Achieved Precision | Achieved Recall | Predicted Positives | Met Target |",
                    "|---:|---:|---:|---:|---:|---|",
                ]
            )
            for row in op_points:
                md.append(
                    "| "
                    f"{row['target_precision']:.2f} | "
                    f"{row['selected_threshold']:.4f} | "
                    f"{row['achieved_precision']:.4f} | "
                    f"{row['achieved_recall']:.4f} | "
                    f"{row['predicted_positive']} | "
                    f"{'yes' if row['met_target'] else 'no'} |"
                )
            md.append("")

    tuning = summary["xgboost_tuning"]
    md.extend(
        [
            "## XGBoost Tuning",
            "",
            f"Tuning enabled: {'yes' if tuning['enabled'] else 'no'}",
            f"Trials run: {tuning['trials_run']}",
        ]
    )
    if tuning["enabled"]:
        md.extend(
            [
                f"Best trial: {tuning['best_trial']}",
                f"Best validation metric ({tuning['best_metric_name']}): {tuning['best_metric_value']:.6f}",
                f"Base scale_pos_weight: {tuning['base_scale_pos_weight']:.4f}",
                "Best params:",
            ]
        )
        for k, v in tuning["best_params"].items():
            md.append(f"- {k}: {v}")
    md.append("")

    dq = summary["data_quality_profile"]
    md.extend(
        [
            "## Data Quality",
            "",
            f"Rows: {dq['row_count']}",
            f"Columns: {dq['column_count']}",
            f"Duplicate rows: {dq['duplicate_row_count']}",
            f"All-null columns: {dq['all_null_column_count']}",
            "",
        ]
    )
    if dq["top_missing_columns"]:
        md.append("Top missing columns (%):")
        for item in dq["top_missing_columns"]:
            md.append(f"- {item['column']}: {item['missing_pct']:.2f}")
        md.append("")
    if dq["high_cardinality_columns"]:
        md.append("High-cardinality columns:")
        for col in dq["high_cardinality_columns"]:
            md.append(f"- {col}")
        md.append("")

    md.extend([
        "## Production Readiness",
        "",
        f"Status: {readiness['status']}",
        f"Rationale: {readiness['rationale']}",
        f"Checks passed: {readiness['checks_passed']} / {readiness['checks_total']}",
        "",
        "| Check | Passed | Details |",
        "|---|---|---|",
    ])
    for check in readiness["checks"]:
        md.append(
            f"| {check['name']} | {'yes' if check['passed'] else 'no'} | {check['details']} |"
        )

    md.extend([
        "",
        "## Artifacts",
        "",
        f"- Predictions: {predictions_csv}",
        f"- Disagreements: {disagreements_csv}",
        f"- Metrics JSON: {metrics_json}",
        f"- Top-K CSV: {topk_csv}",
        f"- Threshold Search CSV: {threshold_search_csv}",
    ])
    summary_md.write_text("\n".join(md), encoding="utf-8")

    print("Offline comparison completed.")
    print(f"Metrics JSON: {metrics_json}")
    print(f"Report: {summary_md}")
    print(f"Predictions: {predictions_csv}")
    print(f"Disagreements: {disagreements_csv}")
    print(f"Top-K: {topk_csv}")
    if threshold_selection:
        print(f"Threshold Search: {threshold_search_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
