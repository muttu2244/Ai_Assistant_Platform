"""
SmartCare QA Assistant — Azure DevOps Fetcher (STEP 1)

Fetches test cases, test plans, work items, user stories, and sprint boards
from Azure DevOps REST API.

⚠️  ALL DATA RETURNED HERE IS RAW AND MAY CONTAIN PHI.
    It MUST be passed through PhiSanitizer (STEP 2) before any downstream use.
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Any, Optional

import httpx

from src.common.config.settings import get_settings
from src.common.exceptions.errors import (
    AdoAuthenticationError,
    AdoClientError,
    AdoItemNotFoundError,
)
from src.common.models.schemas import (
    AdoDefectSummary,
    AdoTestCase,
    AdoTestExecutionRecord,
    AdoTestPlan,
    AdoWorkItem,
    AdoWorkItemHierarchy,
    WorkItemType,
)
from src.common.utils.helpers import build_ado_test_steps, flatten_ado_fields, safe_get

logger = logging.getLogger(__name__)

_WIQL_ALL_STORIES = """
SELECT [System.Id]
FROM WorkItems
WHERE [System.WorkItemType] = 'User Story'
  AND [System.TeamProject] = @project
ORDER BY [System.ChangedDate] DESC
"""

_WIQL_SPRINT = """
SELECT [System.Id]
FROM WorkItems
WHERE [System.TeamProject] = @project
  AND [System.IterationPath] = @currentIteration('[{project}]\\<root>')
ORDER BY [System.WorkItemType] ASC
"""

_SEARCHABLE_WORK_ITEM_TYPES = (
        WorkItemType.USER_STORY.value,
        WorkItemType.FEATURE.value,
        WorkItemType.BUG.value,
        WorkItemType.TASK.value,
)


def _build_auth_header(pat: str) -> dict[str, str]:
    token = base64.b64encode(f":{pat}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _extract_relation_item_id(rel: dict[str, Any]) -> int | None:
    rel_url = rel.get("url", "")
    if not rel_url:
        return None
    try:
        return int(rel_url.rstrip("/").split("/")[-1])
    except ValueError:
        return None


class AdoClient:
    """
    Thin async wrapper around the Azure DevOps REST API.
    All responses are typed as raw AdoWorkItem / AdoTestCase / AdoTestPlan models
    with sanitization_status = RAW.
    """

    def __init__(self) -> None:
        s = get_settings()
        self._org_url = s.ado_org_url.rstrip("/")
        self._project = s.ado_project
        self._api_version = s.ado_api_version
        self._headers = {
            **_build_auth_header(s.ado_pat),
            "Content-Type": "application/json",
        }

    # ── Internal HTTP helpers ─────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return f"{self._org_url}/{self._project}/_apis/{path}"

    def _org_url_path(self, path: str) -> str:
        return f"{self._org_url}/_apis/{path}"

    async def _get(self, url: str, params: Optional[dict] = None) -> dict:
        params = params or {}
        params.setdefault("api-version", self._api_version)
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                resp = await client.get(url, headers=self._headers, params=params)
            except httpx.TransportError as exc:
                raise AdoClientError("ADO network error", str(exc)) from exc

        if resp.status_code == 401:
            raise AdoAuthenticationError("ADO PAT is invalid or expired.")
        if resp.status_code == 404:
            raise AdoItemNotFoundError(f"Resource not found: {url}")
        if not resp.is_success:
            raise AdoClientError(
                f"ADO API returned {resp.status_code}",
                resp.text[:500],
            )
        return resp.json()

    async def _post(self, url: str, body: dict) -> dict:
        params = {"api-version": self._api_version}
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                resp = await client.post(
                    url, headers=self._headers, params=params, json=body
                )
            except httpx.TransportError as exc:
                raise AdoClientError("ADO network error", str(exc)) from exc

        if resp.status_code == 401:
            raise AdoAuthenticationError("ADO PAT is invalid or expired.")
        if not resp.is_success:
            raise AdoClientError(
                f"ADO API returned {resp.status_code}",
                resp.text[:500],
            )
        return resp.json()

    # ── Work Item Fetching ────────────────────────────────────────────────────

    async def get_work_item(self, item_id: int) -> AdoWorkItem:
        """Fetch a single work item by ID."""
        url = self._url(f"wit/workitems/{item_id}")
        data = await self._get(url, params={"$expand": "all"})
        return self._parse_work_item(data)

    async def get_work_items(self, item_ids: list[int]) -> list[AdoWorkItem]:
        """Batch-fetch up to 200 work items by IDs."""
        if not item_ids:
            return []
        # ADO batch endpoint
        url = self._url("wit/workitems")
        ids_str = ",".join(str(i) for i in item_ids[:200])
        data = await self._get(url, params={"ids": ids_str, "$expand": "all"})
        return [self._parse_work_item(item) for item in data.get("value", [])]

    async def search_work_items_by_text(self, query: str, top_n: int = 10) -> list[AdoWorkItem]:
        """Search work items by keywords across title/description/acceptance criteria."""
        terms = [
            t.strip().lower()
            for t in re.findall(r"[A-Za-z0-9_\-]+", query or "")
            if len(t.strip()) >= 3
        ]
        if not terms:
            return []
        terms = terms[:5]

        type_filter = " OR ".join(
            f"[System.WorkItemType] = '{work_item_type}'" for work_item_type in _SEARCHABLE_WORK_ITEM_TYPES
        )

        text_clauses: list[str] = []
        for term in terms:
            safe = term.replace("'", "''")
            text_clauses.append(f"[System.Title] CONTAINS '{safe}'")
            text_clauses.append(f"[System.Description] CONTAINS '{safe}'")
            text_clauses.append(f"[Microsoft.VSTS.Common.AcceptanceCriteria] CONTAINS '{safe}'")

        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            "WHERE [System.TeamProject] = @project "
            f"AND ({type_filter}) "
            f"AND ({' OR '.join(text_clauses)}) "
            "ORDER BY [System.ChangedDate] DESC"
        )

        ids = await self._run_wiql(wiql)
        if not ids:
            return []
        return await self.get_work_items(ids[:max(1, min(top_n, 50))])

    async def get_work_item_full_context(self, item_id: int) -> dict[str, Any]:
        """Fetch work item + relations + linked test cases for grounded generation."""
        url = self._url(f"wit/workitems/{item_id}")
        data = await self._get(url, params={"$expand": "relations"})
        item = self._parse_work_item(data)
        relations = data.get("relations", [])

        related_ids: list[int] = []
        for rel in relations:
            rel_url = rel.get("url", "")
            if not rel_url:
                continue
            try:
                related_ids.append(int(rel_url.rstrip("/").split("/")[-1]))
            except ValueError:
                continue

        dedup_related_ids = list(dict.fromkeys(related_ids))
        related_items = await self.get_work_items(dedup_related_ids[:20]) if dedup_related_ids else []
        linked_test_cases: list[AdoTestCase] = []
        try:
            linked_test_cases = await self.get_test_cases_for_story(item.id)
        except Exception as exc:  # best effort: keep generation flowing
            logger.warning("Unable to fetch linked test cases for %s: %s", item.id, exc)

        return {
            "work_item": item,
            "related_items": related_items,
            "linked_test_cases": linked_test_cases,
            "relation_count": len(relations),
        }

    async def get_work_item_hierarchy(self, item_id: int, max_ancestors: int = 8) -> AdoWorkItemHierarchy:
        """Fetch focus item hierarchy (parents/children/test links/defect links)."""
        url = self._url(f"wit/workitems/{item_id}")
        data = await self._get(url, params={"$expand": "relations"})
        focus_item = self._parse_work_item(data)
        relations = data.get("relations", [])

        parent_ids: list[int] = []
        child_ids: list[int] = []
        for rel in relations:
            rel_name = rel.get("rel", "")
            rel_id = _extract_relation_item_id(rel)
            if rel_id is None:
                continue
            if "Hierarchy-Reverse" in rel_name:
                parent_ids.append(rel_id)
            elif "Hierarchy-Forward" in rel_name:
                child_ids.append(rel_id)

        ancestor_items: list[AdoWorkItem] = []
        visited: set[int] = {focus_item.id}
        frontier = list(dict.fromkeys(parent_ids))[:max_ancestors]
        while frontier and len(ancestor_items) < max_ancestors:
            current_id = frontier.pop(0)
            if current_id in visited:
                continue
            visited.add(current_id)
            try:
                parent_data = await self._get(self._url(f"wit/workitems/{current_id}"), params={"$expand": "relations"})
            except Exception as exc:
                logger.warning("Unable to fetch ancestor work item %s: %s", current_id, exc)
                continue
            parent_item = self._parse_work_item(parent_data)
            ancestor_items.append(parent_item)
            parent_relations = parent_data.get("relations", [])
            for rel in parent_relations:
                if "Hierarchy-Reverse" not in rel.get("rel", ""):
                    continue
                rel_id = _extract_relation_item_id(rel)
                if rel_id is None or rel_id in visited:
                    continue
                frontier.append(rel_id)

        child_items = await self.get_work_items(list(dict.fromkeys(child_ids))[:30]) if child_ids else []
        linked_test_cases = await self.get_test_cases_for_story(focus_item.id)
        linked_defects = await self.get_linked_defects_for_work_item(focus_item.id)

        return AdoWorkItemHierarchy(
            focus_item=focus_item,
            ancestor_items=ancestor_items,
            parent_item_ids=list(dict.fromkeys(parent_ids)),
            child_items=child_items,
            child_item_ids=list(dict.fromkeys(child_ids)),
            linked_test_cases=linked_test_cases,
            linked_defects=linked_defects,
            relation_count=len(relations),
        )

    async def get_linked_defects_for_work_item(self, item_id: int) -> list[AdoDefectSummary]:
        """Return linked bug/defect items for a work item using ADO relations."""
        url = self._url(f"wit/workitems/{item_id}")
        data = await self._get(url, params={"$expand": "relations"})
        relations = data.get("relations", [])
        related_ids: list[int] = []
        for rel in relations:
            rel_id = _extract_relation_item_id(rel)
            if rel_id is None:
                continue
            related_ids.append(rel_id)

        if not related_ids:
            return []

        related_items = await self.get_work_items(list(dict.fromkeys(related_ids))[:100])
        defects: list[AdoDefectSummary] = []
        for wi in related_items:
            if wi.work_item_type.lower() != WorkItemType.BUG.value.lower():
                continue
            raw_fields = wi.raw_fields or {}
            severity = str(raw_fields.get("Microsoft.VSTS.Common.Severity", "") or "")
            defects.append(
                AdoDefectSummary(
                    id=wi.id,
                    title=wi.title,
                    state=wi.state,
                    severity=severity,
                    area_path=wi.area_path,
                    iteration_path=wi.iteration_path,
                    linked_work_item_ids=[item_id],
                    tags=wi.tags,
                )
            )
        return defects

    async def get_test_case_execution_history(
        self,
        test_case_id: int,
        top_n: int = 20,
        max_runs_scan: int = 60,
    ) -> list[AdoTestExecutionRecord]:
        """
        Return execution history rows for a test case by scanning recent test runs.
        This is best-effort and depends on ADO test run/result availability.
        """
        runs_url = self._url("test/runs")
        runs_data = await self._get(runs_url, params={"$top": max(5, min(max_runs_scan, 200))})
        runs = runs_data.get("value", [])

        history: list[AdoTestExecutionRecord] = []
        for run in runs:
            run_id_raw = run.get("id")
            if not run_id_raw:
                continue
            run_id = int(run_id_raw)
            try:
                results_data = await self._get(self._url(f"test/Runs/{run_id}/results"), params={"$top": 1000})
            except Exception as exc:
                logger.warning("Unable to fetch results for test run %s: %s", run_id, exc)
                continue

            for result in results_data.get("value", []):
                tc_ref = result.get("testCase", {}) or {}
                tc_id_raw = tc_ref.get("id") or result.get("testCaseId")
                try:
                    tc_id = int(tc_id_raw)
                except (TypeError, ValueError):
                    continue
                if tc_id != test_case_id:
                    continue

                tester = safe_get(result, "owner", "displayName", default="") or safe_get(result, "runBy", "displayName", default="")
                duration_ms = result.get("durationInMs")
                try:
                    duration_ms = int(duration_ms) if duration_ms is not None else None
                except (TypeError, ValueError):
                    duration_ms = None

                history.append(
                    AdoTestExecutionRecord(
                        run_id=run_id,
                        test_case_id=tc_id,
                        test_case_title=str(tc_ref.get("name", "") or result.get("testCaseTitle", "") or ""),
                        outcome=str(result.get("outcome", "") or ""),
                        started_date=result.get("startedDate"),
                        completed_date=result.get("completedDate"),
                        duration_ms=duration_ms,
                        tester=str(tester or ""),
                        automated_test_name=str(result.get("automatedTestName", "") or ""),
                        run_state=str(run.get("state", "") or ""),
                        run_name=str(run.get("name", "") or ""),
                    )
                )
                if len(history) >= top_n:
                    return history

        return history

    async def get_regression_foundation_snapshot(self, item_id: int) -> dict[str, Any]:
        """Return retrieval-only snapshot used by optimization/reporting phases."""
        hierarchy = await self.get_work_item_hierarchy(item_id)
        execution_history: dict[int, list[AdoTestExecutionRecord]] = {}
        for tc in hierarchy.linked_test_cases[:25]:
            try:
                execution_history[tc.id] = await self.get_test_case_execution_history(tc.id, top_n=25)
            except Exception as exc:
                logger.warning("Unable to fetch execution history for test case %s: %s", tc.id, exc)
                execution_history[tc.id] = []

        return {
            "focus_item": hierarchy.focus_item,
            "ancestors": hierarchy.ancestor_items,
            "children": hierarchy.child_items,
            "linked_test_cases": hierarchy.linked_test_cases,
            "linked_defects": hierarchy.linked_defects,
            "execution_history": execution_history,
        }

    async def get_user_stories(self) -> list[AdoWorkItem]:
        """Fetch all User Stories in the project via WIQL."""
        ids = await self._run_wiql(_WIQL_ALL_STORIES)
        return await self.get_work_items(ids)

    async def get_sprint_items(self) -> list[AdoWorkItem]:
        """Fetch all work items in the current sprint via WIQL."""
        ids = await self._run_wiql(_WIQL_SPRINT)
        return await self.get_work_items(ids)

    async def _run_wiql(self, query: str) -> list[int]:
        """Execute a WIQL query and return a list of work item IDs."""
        url = self._url("wit/wiql")
        body = {"query": query}
        data = await self._post(url, body)
        return [item["id"] for item in data.get("workItems", [])]

    def _parse_work_item(self, data: dict) -> AdoWorkItem:
        raw_fields = data.get("fields", {})
        fields = flatten_ado_fields(raw_fields)
        custom_issue_description = raw_fields.get("Custom.DescriptionforIssues")
        if custom_issue_description:
            base_description = fields.get("description")
            if base_description and custom_issue_description not in base_description:
                fields["description"] = f"{base_description}\n\n{custom_issue_description}"
            elif not base_description:
                fields["description"] = custom_issue_description
        fields.setdefault("id", data.get("id"))
        tags_raw = fields.pop("tags", [])
        return AdoWorkItem(
            **fields,
            tags=tags_raw if isinstance(tags_raw, list) else [],
            raw_fields=raw_fields,
        )

    # ── Test Case Fetching ────────────────────────────────────────────────────

    async def get_test_case(self, test_case_id: int) -> AdoTestCase:
        """Fetch a single test case with its steps."""
        url = self._url(f"testplan/suites/testcases/{test_case_id}")
        wi_data = await self._get(self._url(f"wit/workitems/{test_case_id}"), params={"$expand": "all"})
        return self._parse_test_case(wi_data)

    async def get_test_cases_for_story(self, story_id: int) -> list[AdoTestCase]:
        """
        Return all test cases linked to a user story.
        Uses the work item links API to find TestCase relations.
        """
        url = self._url(f"wit/workitems/{story_id}")
        data = await self._get(url, params={"$expand": "relations"})
        relations = data.get("relations", [])
        tc_ids = [
            int(r["url"].split("/")[-1])
            for r in relations
            if "Microsoft.VSTS.Common.TestedBy" in r.get("rel", "")
        ]
        if not tc_ids:
            return []
        items = await self.get_work_items(tc_ids)
        return [self._work_item_to_test_case(wi) for wi in items]

    async def get_stories_without_test_cases(self) -> list[AdoWorkItem]:
        """Return user stories that have no linked test cases (coverage gaps)."""
        stories = await self.get_user_stories()
        gaps: list[AdoWorkItem] = []
        for story in stories:
            test_cases = await self.get_test_cases_for_story(story.id)
            if not test_cases:
                gaps.append(story)
        return gaps

    def _parse_test_case(self, data: dict) -> AdoTestCase:
        fields = data.get("fields", {})
        steps_xml = fields.get("Microsoft.VSTS.TCM.Steps", "")
        return AdoTestCase(
            id=data["id"],
            title=fields.get("System.Title", ""),
            steps=build_ado_test_steps(steps_xml),
            priority=int(fields.get("Microsoft.VSTS.Common.Priority", 2)),
            state=fields.get("System.State", "Design"),
            automated=fields.get("Microsoft.VSTS.TCM.AutomationStatus", "") == "Automated",
        )

    def _work_item_to_test_case(self, wi: AdoWorkItem) -> AdoTestCase:
        return AdoTestCase(
            id=wi.id,
            title=wi.title,
            state=wi.state,
        )

    # ── Test Plan Fetching ────────────────────────────────────────────────────

    async def get_test_plans(self) -> list[AdoTestPlan]:
        """Fetch all test plans in the project."""
        url = self._url("testplan/plans")
        data = await self._get(url)
        return [
            AdoTestPlan(
                id=plan["id"],
                name=plan.get("name", ""),
                sprint=safe_get(plan, "iteration", default=""),
            )
            for plan in data.get("value", [])
        ]
