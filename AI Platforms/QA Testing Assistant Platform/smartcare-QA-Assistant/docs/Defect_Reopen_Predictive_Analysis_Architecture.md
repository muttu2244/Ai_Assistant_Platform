# SmartCare QA Assistant - Predictive Defect Reopen Analysis Architecture

## 1. Executive Summary

This document defines the target architecture for a predictive AI engine that identifies old defects likely to reopen after a new feature release, maps affected areas, and recommends manual plus automation test coverage.

The solution uses a hybrid AI pattern:
- Machine Learning (ML) for reopen risk scoring (structured historical signal)
- GenAI for impact reasoning and executive narrative generation
- Deterministic rules for traceability and compliance

Primary business outcome:
- Before release sign-off, provide a ranked list of likely reopened defects, impacted modules, and the exact manual and automation test cases to execute.

---

## 2. Problem Statement

Current state:
- ADO contains around 20 years of test and defect history.
- New features are tested with new test cases.
- Automation team executes regression suites.
- Teams still miss regressions where old defects reappear.

Gap:
- No systematic prediction of historical defect reopen risk caused by current feature changes.
- No unified report that links change impact, predicted reopened defects, and exact test coverage needed.

Target state:
- Predict reopened defects before full regression completes.
- Prioritize testing for highest-risk areas.
- Provide traceable evidence for release decisions.

---

## 3. Scope And Outcomes

In scope:
- Defect reopen prediction for every feature release.
- Impact analysis by module, service, API, UI area, and data entity.
- Test recommendation mapping:
  - Manual test cases
  - Automation test cases
  - Coverage gaps where no tests exist
- Actionable reports for QA, automation, dev leads, and release managers.

Out of scope (phase 1):
- Auto-fixing code.
- Fully autonomous release approval.
- Real-time production incident prediction.

---

## 4. High-Level Architecture

```text
Release Change Intake (ADO + Git + Build Artifacts)
        |
        v
Feature Extraction And Impact Graph Builder
        |
        +--> Historical Defect/Test Data Mart (20 years)
        |
        v
Reopen Risk Engine (ML Classifier + Rules)
        |
        v
LLM Reasoning Layer (explanations, test strategy narrative)
        |
        v
Coverage Mapper (manual + automation + gaps)
        |
        v
Report Generator + ADO/Teams Publishing
```

Core design principle:
- Prediction outputs must be explainable and traceable to source evidence in ADO and repository metadata.

---

## 5. End-To-End Workflow

1. Detect new feature release event from ADO pipeline or manual trigger.
2. Pull changed work items, linked PRs, changed files, and impacted components.
3. Build release impact fingerprint (modules, APIs, UI pages, DB objects, dependencies).
4. Query 20-year history for:
   - Similar change patterns
   - Related closed/reopened defects
   - Previous test failures/pass trends
5. Generate structured feature vectors for ML scoring.
6. Score historical defects for reopen likelihood.
7. Apply business rules:
   - Critical severity defects get minimum floor score boost.
   - Recently reopened defects get recency weight.
8. Map predicted defects to manual and automation test cases.
9. Identify uncovered impacted areas and generate recommended new tests.
10. Use GenAI to create readable rationale and release risk summary.
11. Publish reports to ADO dashboard, release notes, and Teams.
12. After execution, feed actual outcomes back for model retraining.

---

## 6. Data Model And Storage Requirements

| Data Domain | Purpose | Recommended Store |
|---|---|---|
| Raw ADO work items, test cases, defects, runs | Source of truth ingestion | Azure Data Lake Storage Gen2 |
| Curated analytics tables | Fast joins for feature engineering | Azure SQL Database or Fabric Warehouse |
| Embeddings and semantic retrieval | Similar defect/test retrieval for GenAI context | Azure AI Search |
| Online prediction cache | Low-latency report generation | Azure Cache for Redis |
| Audit logs and prediction events | Compliance and traceability | Azure Log Analytics + Blob archive |

Additional logical tables:
- defect_history
- defect_reopen_events
- testcase_catalog
- automation_catalog
- release_change_footprint
- module_dependency_graph
- prediction_run
- prediction_explanations
- coverage_gap_register

---

## 7. AI Strategy: ML vs GenAI

Decision:
- Use ML for numeric reopen probability.
- Use GenAI for explanation and report narrative.

Why ML is required:
- Reopen prediction is a supervised classification problem with strong structured signals.
- Deterministic, measurable precision and recall are required.
- Easier governance for risk thresholds.

Why GenAI is still needed:
- Convert technical evidence into readable release-level insights.
- Summarize impacted areas and recommended test strategy.
- Assist in gap analysis wording for stakeholders.

Recommended ML models:
- Baseline: XGBoost or LightGBM classifier.
- Optional comparison: logistic regression for interpretability baseline.
- Later phase: graph neural model if dependency graph quality is high.

Feature examples for ML:
- Module overlap score between current release and historical defect origin.
- File path similarity score.
- API contract change indicator.
- Defect severity and priority.
- Reopen count history.
- Time since last reopen.
- Historical flaky test ratio in impacted module.
- Code churn and dependency fan-out.

Model outputs:
- reopen_probability (0-1)
- risk_band (Critical, High, Medium, Low)
- top_contributing_features

---

## 8. API And Function Changes In Current Platform

### 8.1 New APIs

| Endpoint | Method | Purpose |
|---|---|---|
| /api/predict-defect-reopen | POST | Run prediction for a release or feature set |
| /api/predict-defect-reopen/{run_id} | GET | Retrieve prediction result and report |
| /api/defect-impact-map/{run_id} | GET | Fetch impacted modules and dependency map |
| /api/test-coverage-recommendations/{run_id} | GET | Get mapped manual and automation tests |
| /api/prediction-feedback | POST | Submit actual outcomes for model learning |

### 8.2 Service-Level Changes

Proposed modules:
- src/features/defect_reopen_predictor/
  - predictor_service.py
  - feature_engineering.py
  - risk_scoring.py
  - coverage_mapper.py
  - report_builder.py
- src/storage/sql_db/prediction_repository.py
- src/storage/ai_search/defect_vector_index.py
- src/agents/ai_engine/reasoning_client.py

Updates to existing components:
- src/agents/ado_fetcher/ado_client.py
  - Add methods for historical defect reopen extraction.
  - Add methods for linked manual and automation testcase retrieval.
- src/chat_ui/app.py
  - Add UI flow for prediction run, results, and report download.
- src/common/models/schemas.py
  - Add PredictionRunRequest, DefectRiskResult, CoverageRecommendation.
- src/observability/audit_trail/
  - Add immutable prediction decision logs.

---

## 9. Reporting Outputs

Primary reports:

1. Defect Reopen Risk Report
- Ranked list of likely reopened defects.
- Probability, risk band, confidence indicators.
- Impacted modules and changed assets.

2. Affected Area Heatmap
- Module/component risk intensity.
- UI/API/DB area breakdown.

3. Test Coverage Recommendation Report
- Manual testcases to execute first.
- Automation testcases to execute first.
- Missing coverage areas requiring new tests.

4. Release Readiness Summary
- Predicted reopen risk index for the release.
- Blocker recommendations and go/no-go signals.

5. Explainability Annex
- Top drivers behind each prediction.
- Historical analog defects and evidence links.

Delivery channels:
- ADO work item comments and attachments.
- ADO dashboard widget.
- Teams summary notification.
- Downloadable DOCX and JSON artifacts.

---

## 10. Security, Compliance, And Governance

Requirements:
- PHI scrubbing before sending context to LLM.
- RBAC around prediction results and defect details.
- Audit every prediction input/output and model version.
- Data retention aligned to enterprise policy.
- Prompt and output content safety checks.

Model governance:
- Versioned models with approval workflow.
- Threshold tuning documented by release type.
- Drift monitoring on prediction quality.

---

## 11. Infrastructure Requirements

Azure services:
- Azure App Service or Container Apps for API runtime.
- Azure SQL Database for curated relational history.
- Azure Data Lake Storage Gen2 for raw ingestion and archives.
- Azure AI Search for semantic retrieval and vector index.
- Azure Machine Learning for model training and registry.
- Azure Service Bus for asynchronous prediction requests.
- Azure Monitor + Application Insights + Log Analytics for telemetry.

Compute profile guidance:
- Inference API: CPU optimized, autoscale based on queue length.
- Training jobs: scheduled compute cluster in Azure ML.
- Batch backfill for 20-year data: one-time high-memory ETL job.

---

## 12. Implementation Plan

Phase 0: Discovery and Data Readiness (2-3 weeks)
- Inventory ADO fields and historical data quality.
- Define module mapping taxonomy.
- Build gold dataset for reopen labels.

Phase 1: MVP Prediction Service (4-6 weeks)
- Build ingestion, feature engineering, baseline ML model.
- Expose prediction API and basic report.
- Provide manual and automation test mapping.

Phase 2: Explainability and Advanced Reports (3-4 weeks)
- Add GenAI narrative layer and impact heatmaps.
- Add confidence details and evidence links.
- Add report export to DOCX/PDF.

Phase 3: Feedback Loop and MLOps (4 weeks)
- Add prediction feedback capture.
- Build scheduled retraining and model promotion flow.
- Add drift detection and quality alerting.

Phase 4: Enterprise Hardening (3-5 weeks)
- Security penetration checks.
- Performance tuning for large releases.
- Operational runbook and support handoff.

---

## 13. Success Metrics

Model metrics:
- Precision at Top-N high-risk predictions.
- Recall of actual reopened defects.
- Calibration error for predicted probabilities.

Business metrics:
- Reduction in escaped regressions.
- Faster risk-based test planning time.
- Increased automation effectiveness on high-risk areas.
- Release decision confidence score improvement.

Operational metrics:
- Prediction run latency.
- Data freshness SLA.
- Report generation SLA.

---

## 14. Risks And Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Historical ADO data quality inconsistency | Incorrect predictions | Data profiling, rule-based cleanup, confidence penalties |
| Weak mapping between code changes and modules | Poor impacted-area detection | Build module taxonomy and ownership registry |
| Model drift over time | Declining accuracy | Monthly retraining and drift alarms |
| Over-reliance on GenAI narrative | Hallucinated rationale | Restrict GenAI to evidence-grounded context only |
| Missing automation linkage metadata | Incomplete recommendations | Mandatory testcase-to-module tagging policy |

---

## 15. Final Recommendation

Proceed with a hybrid architecture now:
- ML as primary reopen predictor.
- GenAI as explanation and reporting assistant.
- SQL plus AI Search plus Data Lake as the data backbone.

This approach gives measurable prediction quality, enterprise traceability, and practical release-time reporting needed by QA, automation, and leadership.

Next concrete deliverable after this document:
- Build Phase 0 dataset and baseline model using the last 5 years first, then backfill to 20 years once quality checks pass.
