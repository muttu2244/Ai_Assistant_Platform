from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MockGenerationResult:
    summary: str
    test_cases: list[dict[str, object]]
    traceability: list[dict[str, str]]
    estimated_minutes_saved: int


def build_mock_result(feature_text: str, work_item_id: int | None = None) -> MockGenerationResult:
    feature_label = feature_text.strip() or "Feature"
    prefix = f"WI-{work_item_id}" if work_item_id else "Adhoc"

    test_cases = [
        {
            "id": f"{prefix}-TC-001",
            "title": f"{feature_label}: happy path validation",
            "priority": 1,
            "tags": ["smoke", "regression"],
            "steps": [
                {"action": "Open module", "expected": "Module loads successfully"},
                {"action": "Submit valid input", "expected": "Submission accepted"},
            ],
        },
        {
            "id": f"{prefix}-TC-002",
            "title": f"{feature_label}: invalid input handling",
            "priority": 2,
            "tags": ["negative", "regression"],
            "steps": [
                {"action": "Enter invalid data", "expected": "Validation errors displayed"},
                {"action": "Submit", "expected": "No data persisted"},
            ],
        },
        {
            "id": f"{prefix}-TC-003",
            "title": f"{feature_label}: edge-case resilience",
            "priority": 2,
            "tags": ["edge", "regression"],
            "steps": [
                {"action": "Use boundary values", "expected": "System remains stable"},
                {"action": "Review logs", "expected": "No critical errors reported"},
            ],
        },
    ]

    traceability = [
        {
            "requirement": feature_label,
            "mapped_test_case": tc["id"],
            "coverage_type": ",".join(tc["tags"]),
        }
        for tc in test_cases
    ]

    return MockGenerationResult(
        summary=(
            f"Generated {len(test_cases)} risk-balanced test cases for '{feature_label}' "
            "using offline mock mode."
        ),
        test_cases=test_cases,
        traceability=traceability,
        estimated_minutes_saved=75,
    )
