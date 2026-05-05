"""
SmartCare QA Assistant — STEP 2: Agent 0 — Microsoft Presidio PHI Sanitizer
HIPAA GATE: All ADO data passes through here before touching the LLM or any
downstream service. Raw PHI is replaced with deterministic dummy values.
"""

from __future__ import annotations

import hashlib
import logging

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.common.config.settings import get_settings
from src.common.exceptions.errors import PhiSanitizationError
from src.common.models.schemas import (
    AdoTestCase,
    AdoWorkItem,
    SanitizationStatus,
    SanitizedTestCase,
    SanitizedWorkItem,
)
from src.common.utils.helpers import strip_html

logger = logging.getLogger(__name__)

# PHI entity types we ask Presidio to detect and mask
_PHI_ENTITIES = [
    "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "US_SSN",
    "US_ITIN", "US_PASSPORT", "MEDICAL_LICENSE", "NRP",
    "LOCATION", "DATE_TIME", "IP_ADDRESS", "URL",
]


def _dummy_for(original: str, label: str) -> str:
    """
    Deterministic realistic fake: same input → same fake output every time.
    Uses the SHA-256 hash as a stable index into entity-specific pools so
    the replacement looks like real data while containing no actual PHI.
    """
    idx = int(hashlib.sha256(original.encode()).hexdigest(), 16)

    # ── Entity-specific realistic pools ──────────────────────────────────────
    _PERSONS = [
        "Anil Ghosh", "Priya Sharma", "David Nguyen", "Maria Lopez",
        "James Carter", "Sara Mitchell", "Kevin Patel", "Linda Brooks",
        "Robert Chen", "Amanda Torres", "Michael Evans", "Jessica Ramos",
        "Daniel Kim", "Ashley Moore", "Christopher Lee", "Natalie Scott",
    ]
    _EMAILS = [
        "jsmith@examplecare.com", "aghosh@healthbridge.org",
        "pnguyen@wellnesscorp.net", "mlopez@caresolutions.com",
        "dcarter@medibridge.io", "smitchell@healthaxis.com",
        "kpatel@clarityhealth.org", "lbrooks@carestream.net",
        "rchen@nexushealth.com", "atorres@vitalcare.org",
    ]
    _PHONES = [
        "415-555-0182", "212-555-0147", "312-555-0193", "713-555-0128",
        "202-555-0164", "404-555-0176", "617-555-0139", "503-555-0155",
        "702-555-0118", "858-555-0141",
    ]
    _SSNS = [
        "243-61-8732", "518-74-2190", "367-29-5841", "491-83-6072",
        "625-17-3948", "784-50-2163", "132-96-4857", "869-42-7015",
        "957-31-6284", "316-78-5049",
    ]
    _DATES = [
        "01/15/1985", "03/22/1990", "07/04/1978", "11/30/1965",
        "09/12/1982", "05/28/1995", "02/17/1970", "08/09/1988",
        "04/03/2000", "12/25/1975",
    ]
    _LOCATIONS = [
        "Springfield, IL", "Riverside, CA", "Greenfield, OH",
        "Lakewood, CO", "Fairview, TX", "Maplewood, NJ",
        "Brookside, GA", "Clearwater, FL", "Hillcrest, AZ", "Oakdale, WA",
    ]
    _IPS = [
        "192.0.2.10", "198.51.100.42", "203.0.113.17", "192.0.2.88",
        "198.51.100.5", "203.0.113.99", "192.0.2.201", "198.51.100.77",
    ]
    _URLS = [
        "https://example-ehr.com/patient", "https://portal.carebridge.org",
        "https://records.healthaxis.net", "https://app.medvault.io",
    ]

    _POOLS: dict[str, list[str]] = {
        "PERSON":          _PERSONS,
        "EMAIL_ADDRESS":   _EMAILS,
        "PHONE_NUMBER":    _PHONES,
        "US_SSN":          _SSNS,
        "US_ITIN":         _SSNS,          # same shape as SSN
        "DATE_TIME":       _DATES,
        "LOCATION":        _LOCATIONS,
        "IP_ADDRESS":      _IPS,
        "URL":             _URLS,
    }

    pool = _POOLS.get(label)
    if pool:
        return pool[idx % len(pool)]

    # Generic fallback for rarer entity types (passport, medical licence, etc.)
    short = hashlib.sha256(original.encode()).hexdigest()[:8].upper()
    return f"[{label}_{short}]"


def _filter_overlapping_results(results: list[object]) -> list[object]:
    """Keep the strongest non-overlapping detections to avoid double-masking corruption."""
    ranked = sorted(
        results,
        key=lambda result: (
            int(getattr(result, "start", 0)),
            -1 * (int(getattr(result, "end", 0)) - int(getattr(result, "start", 0))),
            -1 * float(getattr(result, "score", 0.0) or 0.0),
        ),
    )

    selected: list[object] = []
    for candidate in ranked:
        start = int(getattr(candidate, "start", 0))
        end = int(getattr(candidate, "end", 0))
        overlaps = any(start < int(getattr(existing, "end", 0)) and end > int(getattr(existing, "start", 0)) for existing in selected)
        if overlaps:
            continue
        selected.append(candidate)

    return selected


class PhiSanitizer:
    """
    Wraps the Presidio Analyzer + Anonymizer REST containers.
    Runs in strict Presidio-only mode: no regex fallback.
    """

    def __init__(self) -> None:
        s = get_settings()
        self._backend = s.presidio_backend.strip().lower()
        self._analyzer_url = s.presidio_analyzer_url.rstrip("/")
        self._anonymizer_url = s.presidio_anonymizer_url.rstrip("/")
        self._timeout = 15
        self._modes_used: set[str] = set()
        self._local_analyzer = None

    @property
    def sanitization_mode(self) -> str:
        if "presidio_local" in self._modes_used:
            return "presidio_local"
        if "presidio_remote" in self._modes_used:
            return "presidio_remote"
        if "presidio" in self._modes_used:
            return "presidio"
        return "unknown"

    def reset_tracking(self) -> None:
        self._modes_used.clear()

    # ── Public API ────────────────────────────────────────────────────────────

    async def sanitize_text(self, text: str) -> str:
        """Sanitize a raw text string. Returns the masked safe version."""
        if not text or not text.strip():
            return text

        if self._backend == "local":
            try:
                result = self._local_presidio_sanitize(text)
                self._modes_used.add("presidio_local")
                return result
            except (Exception, SystemExit) as exc:
                raise PhiSanitizationError(
                    "Local Presidio sanitization failed; regex fallback is disabled.",
                    str(exc),
                ) from exc

        try:
            result = await self._presidio_sanitize(text)
            self._modes_used.add("presidio_remote")
            return result
        except Exception as exc:
            raise PhiSanitizationError(
                "Remote Presidio sanitization failed; regex fallback is disabled.",
                str(exc),
            ) from exc

    def _init_local_analyzer(self) -> None:
        try:
            from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
        except ImportError as exc:
            raise PhiSanitizationError(
                "presidio-analyzer is not installed for local backend",
                str(exc),
            ) from exc

        try:
            self._local_analyzer = AnalyzerEngine()
            # Enforce SSN coverage in local mode with an explicit recognizer.
            ssn_recognizer = PatternRecognizer(
                supported_entity="US_SSN",
                patterns=[
                    Pattern(
                        name="strict_us_ssn",
                        regex=r"\b\d{3}-\d{2}-\d{4}\b",
                        score=0.9,
                    )
                ],
            )
            patient_name_recognizer = PatternRecognizer(
                supported_entity="PERSON",
                patterns=[
                    Pattern(
                        name="patient_name_dash",
                        regex=r"(?<=Patient Name - )[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+",
                        score=0.9,
                    ),
                    Pattern(
                        name="patient_name_colon",
                        regex=r"(?<=Patient Name: )[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+",
                        score=0.9,
                    ),
                ],
            )
            self._local_analyzer.registry.add_recognizer(ssn_recognizer)
            self._local_analyzer.registry.add_recognizer(patient_name_recognizer)
        except (Exception, SystemExit) as exc:
            raise PhiSanitizationError(
                "Local Presidio analyzer initialization failed. Ensure spaCy model assets and package installer (pip/uv) are available.",
                str(exc),
            ) from exc

    def _local_presidio_sanitize(self, text: str) -> str:
        if self._local_analyzer is None:
            self._init_local_analyzer()

        analyzer_results = self._local_analyzer.analyze(
            text=text,
            language="en",
            entities=_PHI_ENTITIES,
        )
        analyzer_results = _filter_overlapping_results(analyzer_results)
        if not analyzer_results:
            return text

        masked = text
        # Replace from right to left so offsets stay valid.
        for result in sorted(analyzer_results, key=lambda r: r.start, reverse=True):
            original = masked[result.start:result.end]
            replacement = _dummy_for(original, result.entity_type)
            masked = masked[:result.start] + replacement + masked[result.end:]
        return masked

    async def sanitize_work_item(self, item: AdoWorkItem) -> SanitizedWorkItem:
        """Return a sanitized copy of an AdoWorkItem safe to pass to GPT-4o."""
        if item.sanitization_status == SanitizationStatus.SANITIZED:
            return SanitizedWorkItem(**item.model_dump(exclude={"raw_fields", "sanitization_status"}))

        title = await self.sanitize_text(item.title or "")
        description = await self.sanitize_text(strip_html(item.description or "")) if item.description else None
        acceptance = await self.sanitize_text(strip_html(item.acceptance_criteria or "")) if item.acceptance_criteria else None
        assigned_to = await self.sanitize_text(item.assigned_to or "") if item.assigned_to else None
        created_by = await self.sanitize_text(item.created_by or "") if item.created_by else None
        tags = [await self.sanitize_text(tag) for tag in item.tags]

        return SanitizedWorkItem(
            id=item.id,
            title=title,
            description=description,
            work_item_type=item.work_item_type,
            state=item.state,
            assigned_to=assigned_to,
            area_path=item.area_path,
            iteration_path=item.iteration_path,
            tags=tags,
            acceptance_criteria=acceptance,
            created_by=created_by,
            sanitization_status=SanitizationStatus.SANITIZED,
        )

    async def sanitize_test_case(self, tc: AdoTestCase) -> SanitizedTestCase:
        """Return a sanitized copy of an AdoTestCase."""
        if tc.sanitization_status == SanitizationStatus.SANITIZED:
            return SanitizedTestCase(**tc.model_dump(exclude={"sanitization_status"}))

        title = await self.sanitize_text(tc.title or "")
        clean_steps = []
        for step in tc.steps:
            clean_steps.append({
                "action": await self.sanitize_text(step.get("action", "")),
                "expected": await self.sanitize_text(step.get("expected", "")),
            })

        return SanitizedTestCase(
            id=tc.id,
            title=title,
            steps=clean_steps,
            priority=tc.priority,
            state=tc.state,
            associated_story_ids=tc.associated_story_ids,
            automated=tc.automated,
            sanitization_status=SanitizationStatus.SANITIZED,
        )

    async def sanitize_output(self, text: str) -> str:
        """
        STEP 4 — Double-protection: scrub LLM output before sending to user.
        Catches any PHI that may have leaked through the HIPAA boundary.
        """
        return await self.sanitize_text(text)

    # ── Presidio REST calls ───────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=4))
    async def _presidio_sanitize(self, text: str) -> str:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            # 1. Analyze — identify PHI spans
            analyze_resp = await client.post(
                f"{self._analyzer_url}/analyze",
                json={
                    "text": text,
                    "language": "en",
                    "entities": _PHI_ENTITIES,
                    "return_decision_process": False,
                },
            )
            if not analyze_resp.is_success:
                raise PhiSanitizationError(
                    "Presidio Analyzer failed",
                    analyze_resp.text[:300],
                )

            results = analyze_resp.json()
            if not results:
                return text  # No PHI detected

            # Preserve the strongest span where providers return overlapping entities
            # such as EMAIL_ADDRESS and URL over the email domain.
            ranked_results = sorted(
                results,
                key=lambda result: (
                    int(result.get("start", 0)),
                    -1 * (int(result.get("end", 0)) - int(result.get("start", 0))),
                    -1 * float(result.get("score", 0.0) or 0.0),
                ),
            )
            filtered_results: list[dict[str, object]] = []
            for candidate in ranked_results:
                start = int(candidate.get("start", 0))
                end = int(candidate.get("end", 0))
                overlaps = any(start < int(existing.get("end", 0)) and end > int(existing.get("start", 0)) for existing in filtered_results)
                if overlaps:
                    continue
                filtered_results.append(candidate)

            # 2. Replace each detected span with a deterministic dummy based on the
            # original value, preserving sentence structure and utility.
            masked = text
            for result in sorted(filtered_results, key=lambda r: int(r.get("start", 0)), reverse=True):
                start = int(result.get("start", -1))
                end = int(result.get("end", -1))
                entity = str(result.get("entity_type", "PHI"))
                if start < 0 or end <= start or end > len(masked):
                    continue
                original = masked[start:end]
                replacement = _dummy_for(original, entity)
                masked = masked[:start] + replacement + masked[end:]

            return masked

