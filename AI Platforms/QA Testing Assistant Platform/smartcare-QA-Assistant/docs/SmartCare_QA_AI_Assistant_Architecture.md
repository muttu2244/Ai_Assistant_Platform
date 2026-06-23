# SmartCare QA AI Assistant — Architecture Document

**Version:** 1.0  
**Date:** June 2026  
**Status:** Active  
**Owner:** QA Modernization Team — Streamline Healthcare

---

## 1. Executive Summary

The SmartCare QA AI Assistant is an enterprise-grade, AI-powered testing platform built on Microsoft Azure. It automates test case generation, coverage gap analysis, defect risk summarization, and sprint QA reporting by grounding AI outputs in real Azure DevOps (ADO) work item data. The platform enforces HIPAA-grade PHI sanitization before any data reaches the AI model, ensuring compliance without sacrificing usability.

This document describes the system architecture, component design, data flows, security controls, integration patterns, and deployment topology for the platform.

---

## 2. Problem Statement

Manual QA test design is slow, inconsistent, and error-prone. Teams spend days extracting requirements from ADO stories, drafting test cases, and identifying coverage gaps — work that produces variable quality and consumes high-value engineer time.

Key gaps this platform addresses:

| Problem | Impact |
|---------|--------|
| Manual test case authoring from requirements | 2–5 days per sprint cycle |
| Inconsistent coverage across modules | Escaped defects in production |
| No automated PHI masking before LLM prompts | HIPAA exposure risk |
| No structured sprint QA reporting | Delayed release decisions |
| ADO data not actionable in natural language | Under-utilised project intelligence |

---

## 3. Architecture Overview

The platform follows a **layered AI pipeline architecture**:

```
User (Browser / Copilot Studio / Power Automate)
        │
        ▼
  FastAPI Gateway (src/chat_ui/app.py)
        │
        ├──► PHI Sanitizer (Presidio)       ← HIPAA gate
        │
        ├──► ADO Fetcher (AdoClient)         ← Grounding layer
        │
        ├──► AI Engine (FoundryClient)       ← Azure AI Foundry / Claude
        │
        └──► Feature Modules
               ├── Test Case Generator
               ├── Coverage Gap Analyzer
               ├── Automation Script Generator
               ├── Sprint Summary Generator
               └── User Guide Generator
```

### 3.1 Architectural Principles

- **Grounding first:** AI responses are anchored to real ADO work items, not hallucinated.
- **PHI before LLM:** All ADO data passes through Microsoft Presidio before reaching the AI model.
- **Fail-safe defaults:** Missing config degrades gracefully (mock mode, clarification prompts).
- **Stateless API:** All endpoints are stateless; session history lives in browser localStorage.
- **Managed identity:** No static secrets in code; Azure DefaultAzureCredential used throughout.

---

## 4. Component Architecture

### 4.1 Frontend (Chat UI)

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Chat Interface | Embedded HTML/CSS/JS in FastAPI | Free-text QA assistant interaction |
| Chat History | Browser localStorage | Session persistence (client-side) |
| Presidio Status Panel | HTML widget | Displays PHI check result per message |
| Search Chat | JS filter on localStorage | Find past chat sessions by title |

**Key Design Decision:** The UI is embedded as an inline HTML template in `app.py` to simplify single-binary deployment with no CDN or static file server dependency.

### 4.2 API Gateway Layer

**File:** `src/chat_ui/app.py`  
**Framework:** FastAPI  

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/chat` | POST | Main free-text QA assistant chat |
| `/api/generate-test-cases` | POST | Structured test case generation |
| `/api/coverage-gaps` | GET | Coverage gap analysis for a work item |
| `/api/sprint-summary` | GET | Sprint-level QA risk and coverage summary |
| `/api/test-case-lookup` | GET | Keyword-based test case search |
| `/health` | GET | Liveness probe |
| `/readiness` | GET | Feature-level readiness check |

### 4.3 PHI Sanitization Layer

**File:** `src/agents/phi_sanitizer/presidio_analyzer.py`  
**Library:** Microsoft Presidio Analyzer + Anonymizer

- Runs on every ADO work item payload **before** it enters the LLM prompt.
- Detects and masks: Names, Dates, Phone numbers, SSNs, Medical Record Numbers, Email addresses, IP addresses, Location data.
- Returns sanitization mode: `REDACTED`, `MASKED`, or `PASSTHROUGH` (when PHI risk is low).
- Presidio status is surfaced to users in the chat metadata pills.

### 4.4 ADO Fetcher

**File:** `src/agents/ado_fetcher/ado_client.py`  
**API Version:** Azure DevOps REST API v7.1

| Method | API Pattern | Reliability |
|--------|-------------|-------------|
| `get_work_item(id)` | Direct fetch by ID | High (simple endpoint) |
| `search_work_items_by_text()` | WIQL full-text search | Moderate (heavier query) |
| `get_work_item_full_context(id)` | ID + linked items + comments | High |

**Fallback Strategy:** When WIQL search is slow or unavailable, the system presents clarification prompts requesting explicit work item IDs, avoiding silent failures.

### 4.5 AI Engine

**File:** `src/agents/ai_engine/foundry_client.py`  
**Service:** Azure AI Foundry (Anthropic Claude)

| Concern | Implementation |
|---------|---------------|
| Authentication | DefaultAzureCredential (Managed Identity); falls back to API key if configured |
| Retry logic | Exponential backoff decorator on generate() calls |
| Context injection | ADO work item context injected as system prompt prefix |
| Feature routing | Prompt template varies per feature: `test_case`, `coverage_gap`, `automation_script`, `user_guide` |
| Conversation history | Passed as structured message array per chat session |

### 4.6 Feature Modules

| Module | Path | Capability |
|--------|------|-----------|
| Test Case Generator | `src/features/test_case_generator/` | Generates functional, negative, and edge test cases from ADO stories |
| Coverage Gap Analyzer | `src/features/` (via app.py routing) | Identifies missing test coverage for a work item or sprint |
| Automation Script Generator | `src/features/automation_script_generator/` | Generates C# Selenium automation stubs from test cases |
| User Guide Generator | `src/features/user_guide_generator/` | Produces user-facing documentation from ADO requirements |
| Sprint Summary | Inline in `app.py` | Aggregates sprint work items into a QA risk summary |

---

## 5. Data Flow — Test Case Generation (Primary Scenario)

```
1. User submits prompt via Free Text View
        │
2. app.py — Extract intent (_is_testcase_generation_intent)
        │
3. app.py — Extract work item ID or feature query from prompt
        │
4a. [With ID]  AdoClient.get_work_item_full_context(id)
4b. [No ID]    AdoClient.search_work_items_by_text(query)
               → If multiple matches: present clarification list
               → If no match: ask user for ID or module name
        │
5. PHI Sanitizer — Run Presidio on ADO payload
        │
6. FoundryClient.generate(prompt + sanitized_context)
        │
7. Return assistant_message + source + presidio_status to UI
        │
8. UI renders response with metadata pills (Source, Presidio, Mode)
```

---

## 6. Security Architecture

### 6.1 Authentication & Authorization

| Layer | Mechanism |
|-------|-----------|
| Azure AI Foundry | DefaultAzureCredential (Managed Identity) |
| Azure DevOps | PAT Token (configurable via `.env`) |
| APIM Gateway | Subscription key + JWT validation (optional) |
| Entra ID | Service principal for Azure resource access |

### 6.2 PHI / HIPAA Controls

- Presidio runs **in-process** — no PHI leaves the compute boundary before sanitization.
- Sanitized results only are forwarded to the LLM.
- Audit trail of Presidio mode is logged per request.
- HIPAA policy module: `src/compliance/hipaa_policy.py`

### 6.3 Content Safety

- Azure AI Content Safety integrated at `src/security/content_safety.py`
- Prompts screened for harmful content before LLM submission.
- OWASP Top 10 mitigations applied (input validation, exception wrapping, no secret logging).

---

## 7. Integration Architecture

### 7.1 Azure DevOps Integration

- REST API v7.1 via `httpx` async client.
- Supports: Work item fetch, WIQL queries, linked items, PR comments (planned).
- Connection configured via `ADO_ORG_URL`, `ADO_PROJECT`, `ADO_PAT`.

### 7.2 Copilot Studio / Power Automate Bridge

**Path:** `integrations/copilot_power_automate_bridge/`

- Exposes the QA AI assistant as an API callable from Copilot Studio topics.
- Power Automate flows can trigger test case generation post-release.
- Supports MSP post-release automation scenario (webhook → generate → execute).

### 7.3 Azure Service Bus (Messaging)

**Path:** `src/messaging/service_bus.py`

- Planned: Async event-driven test generation triggers.
- Enables decoupled post-release automation pipeline.

---

## 8. Infrastructure & Deployment Topology

```
┌─────────────────────────────────────────────────────────┐
│                     Azure Subscription                   │
│                                                          │
│  ┌──────────────────┐     ┌──────────────────────────┐  │
│  │  Azure Container │     │  Azure AI Foundry         │  │
│  │  Apps (FastAPI)  │────►│  (Claude / Anthropic)     │  │
│  └──────────────────┘     └──────────────────────────┘  │
│           │                                              │
│           │         ┌────────────────────────────────┐  │
│           ├────────►│  Azure DevOps (ADO REST API)   │  │
│           │         └────────────────────────────────┘  │
│           │                                              │
│           │         ┌────────────────────────────────┐  │
│           ├────────►│  Azure Key Vault (Secrets)     │  │
│           │         └────────────────────────────────┘  │
│           │                                              │
│           │         ┌────────────────────────────────┐  │
│           ├────────►│  Azure APIM (API Gateway)      │  │
│           │         └────────────────────────────────┘  │
│           │                                              │
│           │         ┌────────────────────────────────┐  │
│           └────────►│  Azure Monitor / App Insights  │  │
│                     └────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

### 8.1 Deployment Options

| Mode | Description | Use Case |
|------|-------------|---------|
| Docker (local) | `docker-compose up` | Developer local testing |
| Azure Container Apps | Container registry push + ACA deploy | Production |
| IIS (Windows) | HttpPlatform handler + `web.config` | On-premise / legacy |
| ADO Pipeline | `azure-pipelines.yml` | CI/CD automated deployment |

---

## 9. Observability & Monitoring

**Path:** `src/observability/`

| Component | Purpose |
|-----------|---------|
| Application Insights | Request tracing, exception logging, performance metrics |
| Log Analytics | Workspace-level query and alerting |
| Audit Trail | Per-request PHI sanitization audit log |
| Cost Management | Token usage and AI Foundry cost tracking |
| Notifications | Alert on LLM failures, ADO connectivity errors |

---

## 10. Configuration & Settings

**File:** `src/common/config/settings.py`  
**Pattern:** Pydantic BaseSettings from `.env`

| Variable | Purpose | Required |
|----------|---------|---------|
| `AI_FOUNDRY_ENDPOINT` | Azure AI Foundry endpoint | Yes |
| `AI_FOUNDRY_DEPLOYMENT` | Model deployment name | Yes |
| `AI_FOUNDRY_API_KEY` | API key (optional; Managed Identity fallback) | No |
| `ADO_ORG_URL` | ADO organization URL | For grounded features |
| `ADO_PROJECT` | ADO project name | For grounded features |
| `ADO_PAT` | ADO Personal Access Token | For grounded features |
| `APP_ENV` | Environment name (dev/prod) | Yes |

---

## 11. Non-Functional Requirements

| Requirement | Target |
|-------------|--------|
| Response latency (P95) | < 8 seconds for grounded test case generation |
| PHI sanitization overhead | < 300ms per request |
| Availability | 99.5% (Azure Container Apps SLA) |
| HIPAA compliance | Presidio gate on all ADO data before LLM |
| Scalability | Stateless API — horizontal scale via ACA |
| Audit | Every request logged with PHI check status |

---

## 12. Future Roadmap (Phase 2+)

| Capability | Target Phase |
|-----------|-------------|
| Azure AI Search integration for stable work item search | Phase 2 |
| Cosmos DB chat history persistence | Phase 2 |
| Selenium/Java automation script generation | Phase 2 |
| Post-release auto-trigger via Service Bus webhook | Phase 2 |
| Redis cache for ADO work item context | Phase 3 |
| Multi-project ADO support | Phase 3 |
| Power BI QA coverage dashboard | Phase 4 |

---

## 13. Glossary

| Term | Definition |
|------|-----------|
| ADO | Azure DevOps |
| PHI | Protected Health Information |
| HIPAA | Health Insurance Portability and Accountability Act |
| WIQL | Work Item Query Language (ADO-specific) |
| Presidio | Microsoft open-source PHI detection and anonymization library |
| Foundry | Azure AI Foundry — managed AI model deployment service |
| Grounding | Anchoring AI responses in real project data (ADO work items) |
| ACA | Azure Container Apps |
| APIM | Azure API Management |
| PAT | Personal Access Token |

---

*SmartCare QA AI Assistant — Architecture Document v1.0 | Streamline Healthcare | Confidential*
