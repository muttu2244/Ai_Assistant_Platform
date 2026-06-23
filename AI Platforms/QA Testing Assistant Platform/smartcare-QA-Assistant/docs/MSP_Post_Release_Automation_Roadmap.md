# SmartCare QA Assistant — Post-MSP Automation Roadmap
## Auto-Generation & Execution of Test Automation Upon Release

**Document Version:** 1.0  
**Date:** June 2026  
**Status:** Draft — For Review  
**Classification:** Internal Use

---

## 1. Executive Summary

Currently, after a Major Software Release (MSP), the Automation team manually:
1. Reviews changed requirements
2. Creates test cases
3. Generates automation scripts
4. Runs test suites
5. Reports results

**Delays observed:** 2–7 days between MSP release and first automated test execution.

**Proposed Solution:** The SmartCare QA Assistant Agent should automatically:
- Detect MSP release trigger
- Extract changed requirements/user stories from Azure DevOps
- Generate test cases and automation scripts in parallel
- Execute test suites automatically
- Post results and coverage report to ADO

**Expected Outcome:** Test automation ready and running **within 30 minutes of MSP release**, reducing time-to-validation by 85%.

---

## 2. Problem Statement

### Current State (Manual Process)
```
MSP Release
    ↓ (Wait 1-3 days)
Automation team reviews ADO items
    ↓ (1-2 days)
Create test cases manually
    ↓ (1-2 days)
Write automation scripts
    ↓ (1-2 days)
Execute and debug
    ↓ (1-2 days)
Report results
```

**Pain Points:**
- **Delay:** 2–7 days before testing begins
- **Manual overhead:** Repeated extraction and analysis of requirements
- **Error-prone:** Manual transcription and inconsistencies
- **No early defect detection:** Bugs found late in cycle, higher remediation cost
- **Resource constraint:** Automation team capacity becomes bottleneck

### Desired State (Automated Process)
```
MSP Release (e.g., artifact in blob storage, ADO release created)
    ↓ (Immediate trigger)
SmartCare Agent detects release
    ↓ (5 minutes)
Extract changed requirements/stories from ADO
    ↓ (5 minutes)
Generate test cases + automation scripts (parallel)
    ↓ (10 minutes)
Provision test environment
    ↓ (5 minutes)
Execute automation suites
    ↓ (5-10 minutes)
Post results + coverage report to ADO release
    ↓ (Total: ~30 minutes)
READY FOR AUTOMATION TEAM REVIEW
```

---

## 3. Vision & Goals

### Vision
*Enable the SmartCare QA Assistant to autonomously orchestrate test automation creation, execution, and reporting immediately upon MSP release, reducing validation cycle time from days to minutes.*

### Goals
1. **Speed:** Test automation execution within 30 minutes of MSP release
2. **Reliability:** 95%+ pass rate on newly generated automation scripts
3. **Coverage:** Auto-generate test cases for 90%+ of changed requirements
4. **Traceability:** Link all generated tests to source requirements in ADO
5. **Observability:** Complete logs and reports posted back to ADO release
6. **Scalability:** Support parallel test execution across multiple environments

---

## 4. Roadmap

### Phase 1: Foundation & Infrastructure (Weeks 1–2)
**Goals:** Establish detection, integration, and baseline automation.

#### 1.1 MSP Release Detection
- Configure webhook/event trigger in ADO to detect MSP release
  - Listen to Release Pipeline completion event
  - Capture release artifact location (blob storage, container registry)
  - Trigger SmartCare Agent via Azure Service Bus message or HTTP webhook
- Alternative: Manual trigger via chat ("`@SmartCare generate post-MSP automation for release v2.4.0`")

#### 1.2 Agent Capability Expansion
- Extend `FoundryClient` to support batch test case generation
- Add feature to auto-generate Selenium/UI automation scripts
- Implement test execution orchestration (invoke existing test runners)
- Add reporting/consolidation module to summarize results

#### 1.3 Infrastructure
- Provision dedicated test environment pool (dev, staging)
- Set up test data seeding service
- Configure Service Bus queue: `post-msp-automation-requests`
- Create ADO Service Connection for test environment deployment

#### 1.4 Scope
- Support .NET Core UI automation (Selenium/Playwright)
- Support SQL integration tests
- Support API/integration tests (RestSharp, HttpClient)
- First iteration: LLM-generated test cases only (human review required before execution)

---

### Phase 2: Intelligent Test Case & Script Generation (Weeks 3–4)
**Goals:** Auto-generate quality test cases and automation scripts, reduce human review time.

#### 2.1 Enhanced Requirement Analysis
- Fetch changed work items (User Stories, Features) from ADO release
- Extract acceptance criteria, linked defects, test plans
- Sanitize PHI using existing Presidio integration
- Summarize changes per module/feature

#### 2.2 Test Case Generation Pipeline
- Accept requirement summary as input
- Generate test cases in parallel:
  - Happy path (positive)
  - Negative cases (error handling)
  - Edge cases (boundary, null, etc.)
  - Regression cases (verify existing behavior unchanged)
- Output: Structured test case definitions (CSV/JSON) with IDs, preconditions, steps, expected results
- Quality gate: Presidio validation on generated test data

#### 2.3 Automation Script Generation
- For each test case, auto-generate:
  - Selenium/Playwright UI automation code (C#)
  - SQL validation scripts (T-SQL)
  - API test code (RestSharp/HttpClient)
- Output format: Ready-to-compile C# test projects, structured for CI/CD
- Include POM (Page Object Model) patterns and reusable test helpers

#### 2.4 Generated Artifact Storage
- Store generated test cases in ADO test plans (linked to release)
- Store generated scripts in dedicated git branch: `generated/v-<release>-automation`
- Tag all generated items with metadata: `generated-by=smartcare-agent` `release=v2.4.0` `timestamp=ISO8601`

#### 2.5 Human Review Gate (Optional)
- If `REQUIRE_HUMAN_REVIEW=true`: Post generated artifacts as ADO work item
- Automation team reviews, approves, or requests regeneration
- If approved: trigger Phase 3 (execution)
- If not approved: notify and halt

---

### Phase 3: Automated Test Execution & Reporting (Weeks 5–6)
**Goals:** Execute generated tests, capture results, post reports back to ADO.

#### 3.1 Test Environment Setup
- Deploy MSP build to test environment (automated via existing deployment pipeline)
- Seed test data (customers, orders, claims, etc.)
- Verify connectivity to database, APIs, UI endpoints
- Health check: Run smoke tests before main suite

#### 3.2 Parallel Test Execution
- Execute test suites in parallel across compute pools:
  - UI tests (Selenium grid)
  - API tests (multi-threaded)
  - Database tests (sequential, transactional)
- Capture execution logs, screenshots (UI failures), API responses
- Implement retry logic for transient failures (max 2 retries)

#### 3.3 Results Aggregation
- Consolidate results from all test suites
- Calculate coverage metrics:
  - % of requirements covered by tests
  - % of acceptance criteria validated
  - Code coverage (if instrumented build available)
- Identify flaky tests (pass rate < 90%)

#### 3.4 Reporting & Notification
- Generate summary report:
  - Pass/fail counts by feature
  - Failed test details with logs
  - Coverage gaps (requirements without tests)
  - Timeline and resource utilization
- Post report as ADO release annotation/comment
- Notify automation team via Teams/email: "Post-MSP automation complete. X tests passed, Y failed. Review report here: [link]"

#### 3.5 Artifact Retention
- Archive generated scripts and test results in blob storage
- Retain for 90 days (configurable)
- Link results to ADO release for traceability

---

### Phase 4: Continuous Learning & Optimization (Weeks 7–8)
**Goals:** Improve test quality, reduce false positives, handle edge cases.

#### 4.1 Feedback Loop
- Track which auto-generated tests fail due to LLM error vs. real defects
- Classify failures:
  - Script error (syntax, wrong locator) → Refine prompts
  - Test logic error (wrong assertion) → Improve test case generation
  - Environment issue (timeout, connectivity) → Add retry/wait logic
  - Real defect found → Log as product issue

#### 4.2 Prompt Refinement
- Build library of working test patterns from successful generations
- Update system prompts with:
  - Common mistakes to avoid
  - SmartCare-specific patterns (POM structure, test helpers)
  - Edge cases from past failures
- Monthly review of false positive patterns

#### 4.3 Performance Tuning
- Measure end-to-end time: release trigger → results posted
- Identify bottlenecks (generation, environment setup, execution)
- Optimize:
  - Parallel batch sizes
  - LLM token budgets
  - Test environment provisioning
  - Test data setup time
- Target: < 20 minutes end-to-end for 80% of releases

#### 4.4 Coverage Expansion
- Add support for:
  - Mobile UI automation (Appium)
  - Performance tests (load/stress)
  - Security/compliance validation
  - Accessibility tests
- Integrate with Azure Load Testing service

---

## 5. Implementation Details

### 5.1 Architecture Overview
```
┌──────────────────────────────────────────────────────────────┐
│                    MSP RELEASE TRIGGER                        │
│         (ADO Release Pipeline Completion Event)               │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         ↓
┌──────────────────────────────────────────────────────────────┐
│          SERVICE BUS: post-msp-automation-queue               │
│              (Message: release metadata)                      │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         ↓
┌──────────────────────────────────────────────────────────────┐
│    SmartCare QA Agent — Post-MSP Orchestration Service       │
│                                                               │
│  1. Poll Service Bus for release events                      │
│  2. Extract changed requirements from ADO (parallel)         │
│  3. Generate test cases (FoundryClient + feature module)     │
│  4. Generate automation scripts (template + LLM)             │
│  5. Commit scripts to git branch                             │
│  6. Trigger test execution pipeline                          │
│  7. Monitor execution, aggregate results                     │
│  8. Post report to ADO release                               │
│                                                               │
└────────┬───────────┬──────────────┬──────────────┬───────────┘
         │           │              │              │
         ↓           ↓              ↓              ↓
      ┌──────┐  ┌──────┐  ┌──────────┐  ┌──────────────┐
      │ ADO  │  │ Git  │  │Test Env  │  │ Blob Storage │
      │Fetch │  │ Push │  │ Provision│  │   (Results)  │
      └──────┘  └──────┘  └──────────┘  └──────────────┘
```

### 5.2 Service Architecture
**New Component: `PostMspOrchestrationService`**

Location: `src/features/post_msp_orchestration/`

```
post_msp_orchestration/
├── __init__.py
├── orchestrator.py          # Main orchestration logic
├── requirement_fetcher.py   # ADO requirement extraction
├── test_case_generator.py   # Enhanced test case generation
├── script_generator.py      # Automation script template + LLM
├── test_executor.py         # Test environment setup + execution
├── results_aggregator.py    # Results consolidation
└── reporting.py             # Report generation + ADO posting
```

### 5.3 Data Flow

**Input:** MSP Release metadata
```json
{
  "release_id": "Release-2024-v2.4.0",
  "release_name": "v2.4.0",
  "artifact_uri": "https://smartcareacr.azurecr.io/smartcare:v2.4.0",
  "changed_features": ["Feature-1234", "Feature-5678"],
  "changed_stories": ["Story-9012", "Story-3456"],
  "timestamp": "2026-06-04T10:00:00Z"
}
```

**Processing Steps:**
1. Fetch full requirement details from ADO for each changed feature/story
2. Sanitize PHI using Presidio
3. Generate test cases:
   ```python
   test_cases = await test_case_generator.generate(
       requirement_summary=sanitized_requirement,
       module_context=module_name,
       count_target=5  # avg 5 cases per story
   )
   ```
4. Generate automation scripts:
   ```python
   scripts = await script_generator.generate(
       test_cases=test_cases,
       language="csharp",
       framework="selenium"
   )
   ```
5. Push scripts to git: `git push origin generated/v2.4.0-automation`
6. Trigger test execution pipeline (Azure Pipelines)
7. Poll for completion and aggregate results
8. Post report to ADO release

**Output:** Report in ADO Release comments
```
✅ Post-MSP Automation Summary
Generated: 45 test cases | Created: 38 automation scripts | Failed to generate: 7

Execution Results:
- UI Tests: 28 passed, 2 failed, 8 skipped (flaky)
- API Tests: 8 passed, 1 failed
- Database Tests: 5 passed, 0 failed

Coverage: 
- Features covered: 8/9 (89%)
- Acceptance criteria validated: 34/37 (92%)
- New defects found: 3 (logged to backlog)

Timeline: Generated in 12 min | Executed in 8 min | Total: 20 min

Generated scripts: [Link to git branch]
Detailed report: [Link to ADO test run]
```

### 5.4 Integration Points

**1. ADO Webhook/Event Grid**
- Configure ADO Release Pipeline to emit event on completion
- Or: Scheduled Service Bus trigger every 15 min to check for new releases

**2. Azure DevOps REST API**
- Fetch work items, test plans, linked defects
- Post comments to release
- Create test run records

**3. Git Integration**
- Commit generated scripts to feature branch
- Create PR automatically for human review (Phase 2)
- Merge post-approval

**4. Azure Pipelines**
- Trigger existing test execution pipeline
- Pass generated script location as parameter
- Poll for completion

**5. Blob Storage**
- Store generated scripts (backup)
- Store execution logs and screenshots
- Archive for audit trail

### 5.5 Error Handling & Resilience

| Failure Scenario | Detection | Recovery |
|---|---|---|
| ADO unreachable | HTTP 503/timeout | Retry with exponential backoff; notify via Teams |
| Test case generation fails | LLM error, no output | Log error, skip that requirement, continue with others |
| Script generation produces invalid code | Compilation error | Regenerate with refined prompt; flag as flaky |
| Test environment provisioning fails | Deploy task failure | Halt orchestration, notify automation team |
| Test execution timeout | Poll timeout after 60 min | Kill tests, mark as incomplete, post partial results |
| Presidio sanitization fails | Exception in sanitizer | Skip that requirement, log warning |

### 5.6 Configuration (Environment Variables / .env)

```env
# Post-MSP Automation Feature
POST_MSP_AUTOMATION_ENABLED=true
POST_MSP_AUTOMATION_TRIGGER=webhook  # or 'scheduled'
POST_MSP_SERVICE_BUS_QUEUE=post-msp-automation-requests
POST_MSP_TEST_ENV_CONNECTION_STRING=<service-connection-id>
POST_MSP_GIT_BRANCH_PREFIX=generated/
POST_MSP_REQUIRE_HUMAN_REVIEW=false
POST_MSP_PARALLEL_TEST_WORKERS=4
POST_MSP_TEST_TIMEOUT_MINUTES=60
POST_MSP_RETENTION_DAYS=90
```

---

## 6. Key Features by Phase

### Phase 1
- ✅ MSP release detection via webhook
- ✅ Manual trigger via chat
- ✅ Extract requirements from ADO
- ✅ Generate basic test cases (happy path + negative)
- ✅ Generate UI automation scripts (Selenium template + LLM)

### Phase 2
- ✅ Enhanced test case generation (edge cases, regression)
- ✅ Support multiple test types (UI, API, DB)
- ✅ Store generated artifacts in ADO test plans
- ✅ Human review gate (optional)
- ✅ Metadata tagging for traceability

### Phase 3
- ✅ Automated test environment provisioning
- ✅ Parallel test execution
- ✅ Flaky test detection
- ✅ Results aggregation
- ✅ Coverage metrics calculation
- ✅ Report posting to ADO

### Phase 4
- ✅ Feedback loop & prompt refinement
- ✅ Performance optimization
- ✅ Support for additional test types (mobile, performance, security)

---

## 7. Success Metrics

| Metric | Current | Target (Phase 3) | Target (Phase 4) |
|--------|---------|------------------|------------------|
| Time to automation readiness | 2–7 days | < 30 min | < 20 min |
| Test cases generated per MSP | Manual | 40–50 | 60–80 |
| Automation script success rate | N/A | 80%+ | 95%+ |
| Manual review overhead | ~16 hrs | ~2 hrs | ~30 min |
| Early defect detection | Days after release | Minutes | Minutes |
| Automation team capacity freed | 0% | 70% | 85% |

---

## 8. Risk & Mitigation

| Risk | Impact | Mitigation |
|---|---|---|
| LLM generates incorrect test logic | False positives/negatives; wasted test time | Phase 2: Human review gate; Phase 4: feedback loop |
| Test environment unavailable | Tests cannot run | Fallback: manual trigger; health checks before execution |
| Auto-generated scripts have bugs | Test suite fails; blocks validation | Phase 4: Prompt refinement; version control + git review |
| Overload ADO/Git with too many requests | API rate limiting; service degradation | Implement backoff; batch operations; dedicated service account |
| Data residency/compliance violation | Regulatory breach | Presidio sanitization; no PHI in prompts; audit logs |
| Cost escalation (LLM tokens, compute) | Budget overrun | Token budgeting; cache prompts; cost monitoring |

---

## 9. Timeline & Resource

### Development Effort
- **Phase 1:** 2 weeks (2 engineers)
- **Phase 2:** 2 weeks (2 engineers, 1 QA)
- **Phase 3:** 2 weeks (1 engineer, 1 DevOps, 1 QA)
- **Phase 4:** 2 weeks (1 engineer, 1 QA)

**Total:** ~8 weeks

### Infrastructure
- Azure Service Bus queue (minimal cost)
- Test environment VMs (existing, repurposed)
- Blob storage for artifacts (minimal cost)
- LLM token consumption: ~$500/month (at current usage rates)

### Go-Live
- **Phase 1 & 2 (internal pilot):** Week 4
- **Phase 3 (production):** Week 6
- **Phase 4 (optimization):** Week 8

---

## 10. Next Steps

1. **Approval:** Stakeholder sign-off on roadmap
2. **Resource allocation:** Assign engineering team
3. **Phase 1 kickoff:** Setup webhook trigger and ADO integration
4. **Prototype:** Create proof-of-concept for test case generation
5. **Validation:** Internal pilot with automation team
6. **Feedback:** Iterate based on pilot results
7. **Production rollout:** Phase 3 deployment

---

## 11. Appendix

### A. Chat Command Examples (Phase 1)
```
User: @SmartCare generate post-MSP automation for release v2.4.0

SmartCare: ✓ Post-MSP automation triggered.
  Release: v2.4.0
  Changed features: 9
  Changed stories: 23
  Status: Generating test cases...
  ETA: 15 minutes

[After 15 min]
✅ Complete!
  Test cases: 47 generated
  Automation scripts: 42 created
  Artifacts: [git branch link]
  Next: Team review or auto-execute? (Reply: 'execute' or 'review')
```

### B. MSP Release Event Schema
```json
{
  "eventType": "release.completed",
  "release": {
    "id": "Release-2024-v2.4.0",
    "name": "SmartCare v2.4.0",
    "status": "succeeded",
    "artifacts": [
      {
        "type": "container",
        "uri": "smartcareacr.azurecr.io/smartcare:v2.4.0"
      }
    ]
  },
  "changedItems": {
    "features": ["Feature-1234", "Feature-5678"],
    "stories": ["Story-9012", "Story-3456", "Story-7890"],
    "bugs": ["Bug-1111", "Bug-2222"]
  },
  "timestamp": "2026-06-04T10:00:00Z"
}
```

---

**Document End**

*For questions or clarifications, contact the Engineering team.*
