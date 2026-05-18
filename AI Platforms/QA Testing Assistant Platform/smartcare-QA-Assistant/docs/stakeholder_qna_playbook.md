# SmartCare QA Assistant - Stakeholder Q&A Playbook

## Purpose
This document provides a comprehensive list of likely questions and crisp answers for demo discussions with:
- Human managers (non-technical or mixed technical depth)
- Technical managers (engineering, architecture, security, operations)

Use this as a prep sheet for Tuesday demos, steering committees, architecture reviews, and budget conversations.

## How To Use During Demo
1. Start with business outcomes first.
2. Use architecture details only when asked.
3. Tie every technical choice to risk reduction, speed, quality, or cost.
4. If a question is out of scope, acknowledge and offer follow-up with data.

---

## Section A: Questions from Human Managers (Layman-Friendly)

### A1. What is this application in one line?
Q: What does this platform do?
A: It helps QA teams generate better test cases, find coverage gaps, and summarize risks from Azure DevOps data while masking sensitive health information.

### A2. Why do we need this if we already use Azure DevOps?
Q: Why not just use ADO boards manually?
A: ADO stores work; this system interprets it. It reduces manual analysis, speeds test planning, and gives structured QA outputs from natural-language requests.

### A3. What business problem does it solve?
Q: What pain points are we removing?
A: Slow test design, inconsistent coverage, missed defect risk patterns, and high manual effort in interpreting requirements and linking evidence.

### A4. How is this different from a normal chatbot?
Q: Is this just ChatGPT with a UI?
A: No. It is grounded in your project data, enforces PHI masking, and uses domain-specific QA workflows rather than generic chat-only answers.

### A5. What is the value to leadership?
Q: Why should leadership care?
A: Faster release confidence, fewer escaped defects, auditable QA reasoning, and measurable productivity gains.

### A6. What is the ROI story?
Q: Where do savings come from?
A: Less manual triage, faster test case authoring, better defect prevention, and fewer late-stage production incidents.

### A7. Will this replace testers?
Q: Is AI replacing QA staff?
A: No. It augments testers by handling repetitive analysis so humans focus on critical thinking and edge-case strategy.

### A8. Is patient data safe?
Q: Are we exposing PHI to AI?
A: The design sanitizes PHI before model generation and sanitizes output again, so no raw PHI should be shown in responses.

### A9. Can it make mistakes?
Q: Can it be wrong?
A: Yes, like any AI system. That is why grounding, confidence controls, and human review are included in the operating model.

### A10. What if users ask vague questions?
Q: Will it guess?
A: It should ask for clarification when context is weak or ambiguous, instead of pretending certainty.

### A11. Why not just copy-paste prompts into a public LLM?
Q: Why build this platform at all?
A: Public prompting lacks secure enterprise grounding, compliance controls, traceability, and workflow integration with your ADO ecosystem.

### A12. Is this only for QA?
Q: Can other teams use it?
A: Primary design is QA and testing support, but patterns can extend to BA, release management, and documentation workflows.

### A13. How quickly can teams adopt it?
Q: Is onboarding hard?
A: Early adoption is quick for prompt-based workflows; deeper adoption improves with standard templates and process integration.

### A14. What success looks like in 90 days?
Q: How do we measure outcomes?
A: Reduction in test design cycle time, increased requirement-to-test coverage, reduced escaped defects, and improved audit readiness.

### A15. What are the top risks?
Q: Where can this fail?
A: Wrong retrieval context, over-trust in AI outputs, weak prompt hygiene, and operational drift if monitoring is ignored.

---

## Section B: Questions from Technical Managers

### B1. LLM Strategy
Q: Which model strategy are we using?
A: Managed enterprise-hosted LLM endpoints with controlled access, observability, and policy enforcement.

Q: Why not one single model forever?
A: Different tasks need different cost/latency/quality tradeoffs; model abstraction prevents lock-in and enables evolution.

Q: Why use Claude or GPT variants?
A: Selection depends on output quality, reliability, context handling, safety behavior, cost per task, and enterprise controls.

Q: Why not only GitHub Copilot?
A: Copilot is excellent for developer productivity, but this app needs runtime data-grounded QA workflows, PHI controls, and application-specific orchestration.

### B2. RAG and Grounding
Q: Is this true RAG?
A: It uses retrieval-augmented generation by pulling relevant ADO context at query time and injecting it into prompts.

Q: Is it vector RAG right now?
A: Current behavior is primarily live text retrieval from ADO. Vector index support can be added for stronger semantic retrieval at scale.

Q: Why not just use prompts without retrieval?
A: Prompt-only answers can be fluent but ungrounded. Retrieval provides project-specific evidence and reduces hallucinations.

Q: What happens when retrieval is ambiguous?
A: Best practice is confidence-gated disambiguation with top candidate options rather than auto-picking one item.

### B3. Prompting vs Fine-Tuning
Q: Why not fine-tune immediately?
A: Fine-tuning has data prep, governance, drift, and lifecycle overhead. Prompting plus retrieval usually delivers faster and cheaper initial value.

Q: When should we fine-tune?
A: Only when repeated failure patterns persist after prompt engineering, retrieval improvements, and structured output controls.

Q: Can we do both?
A: Yes. Start with retrieval + prompt templates, then selectively fine-tune for stable high-volume tasks.

### B4. Agentic AI
Q: What does agentic mean here?
A: Multi-step orchestration where the system can retrieve context, sanitize data, choose generation path, and format outputs with policy checks.

Q: Are autonomous agents risky?
A: They can be. Guardrails include bounded tools, deterministic retrieval stages, approval points, and audit trails.

### B5. Security and Compliance
Q: How is PHI protected?
A: Input sanitization before model calls, output sanitization before display, plus strict secrets management and controlled endpoints.

Q: Is data sent outside enterprise boundaries?
A: Deployment should use enterprise-approved managed endpoints and network policies aligned with compliance requirements.

Q: How are secrets managed?
A: Use centralized key management, managed identities where possible, and no hard-coded credentials.

Q: How do we support audits?
A: Keep request metadata, source references, and policy outcomes in immutable or controlled audit logs.

### B6. Architecture and Reliability
Q: What are key components?
A: API/chat layer, ADO retrieval client, PHI sanitizer, generation client, optional search layer, observability, and deployment automation.

Q: How do we avoid single-point failure?
A: Retries, fallbacks, timeouts, graceful degradation, and clear user-facing error paths.

Q: How do we handle rate limits?
A: Retry with exponential backoff, model fallback strategy, and queue-based smoothing for bursts.

Q: What is the failure mode when AI is unavailable?
A: Return deterministic error responses and preserve partial grounded insights where safe.

### B7. Networking and Cloud
Q: Can this run in private networking?
A: Yes, with private endpoints, restricted outbound rules, and enterprise DNS/network controls.

Q: Which cloud services are involved?
A: Typical stack includes managed LLM endpoint, identity, key vault, logging/monitoring, storage, and optional API management.

Q: Can we deploy hybrid?
A: Yes, if network, identity federation, and data residency controls are designed upfront.

### B8. Cost and FinOps
Q: What drives cost most?
A: Token usage, model choice, request volume, context size, retrieval API calls, and logging retention.

Q: How do we control spend?
A: Prompt/context optimization, model tiering by workload, caching, request budgeting, and usage dashboards.

Q: Is the solution cheaper than manual effort?
A: Usually yes at scale, when measured against analyst time, cycle delays, and defect leakage costs.

Q: What is the right KPI set?
A: Cost per useful answer, time saved per artifact, reduction in escaped defects, and adoption rate.

### B9. Delivery and DevOps
Q: How do we deploy safely?
A: CI/CD with environment promotion, config as code, secrets from vault, and rollback-ready release strategy.

Q: How do we test quality?
A: Unit tests, integration tests, prompt regression suites, and scenario-based evaluation with known expected outcomes.

Q: Can we do blue/green or canary?
A: Yes. Recommended for model or prompt changes to reduce blast radius.

### B10. Governance and Ownership
Q: Who owns prompts and behavior?
A: Joint ownership: product + QA + platform engineering + compliance.

Q: Who approves changes?
A: Change governance should include risk review for retrieval logic, sanitization rules, and production prompt updates.

Q: How do we prevent drift?
A: Versioned prompts, benchmark sets, periodic evaluations, and controlled release gates.

---

## Section C: Tough Questions Managers May Ask (And How To Answer)

Q: Why cannot we just ask simple prompts and get everything done?
A: Because enterprise outcomes need grounded accuracy, compliance controls, repeatability, and traceability. Simple prompts alone do not guarantee those.

Q: If users still need clarification sometimes, where is the value?
A: Value comes from reducing dozens of manual ADO navigation steps to one or two guided interactions and automating synthesis.

Q: Is this overengineering?
A: It may look that way if judged as a chatbot. It is actually risk engineering for healthcare-grade quality workflows.

Q: Could we buy an off-the-shelf copilot and stop here?
A: Possibly for generic tasks, but custom healthcare QA workflows and PHI-safe grounding usually require tailored implementation.

Q: What if model vendors change pricing or policy?
A: Abstract model providers behind a unified interface and maintain fallback options to reduce vendor lock-in risk.

Q: Are we creating another tool people will not use?
A: Adoption depends on workflow fit. Integrate with existing ADO practices, measure time saved, and keep UX low-friction.

---

## Section D: Demo-Day Question Bank (Rapid Fire)

### Product and Business
1. What is the one KPI this improves first?
2. How many hours per sprint can this save?
3. What happens if users do not trust AI outputs?
4. Does this help compliance reporting?
5. How does this reduce release risk?

### AI and Model
1. How do you reduce hallucinations?
2. Why this model and not another?
3. Can we switch models later?
4. Do we fine-tune or not?
5. What is your model fallback plan?

### RAG and Data
1. Where does grounding data come from?
2. What if no exact match is found?
3. How do you rank candidates?
4. Do you expose source evidence in output?
5. Can we add semantic/vector search later?

### Security and Compliance
1. Is PHI ever sent to the model?
2. How are secrets stored?
3. Do we have audit logs?
4. What is the data retention policy?
5. How do we handle incident response?

### Engineering and Operations
1. What is p95 latency?
2. What happens on ADO API outages?
3. How do we test prompt changes?
4. What is release rollback strategy?
5. How do we monitor quality over time?

### Cost and Governance
1. What is cost per request?
2. How do we avoid runaway token usage?
3. Who approves production prompt edits?
4. How do we justify this against manual process?
5. What is phase-2 roadmap after demo?

---

## Section E: Suggested Answer Templates (Use in Meetings)












### E1. 20-Second Executive Answer
"This platform accelerates QA planning and risk analysis by grounding AI in our ADO work data while enforcing PHI-safe outputs. It reduces manual effort, improves coverage confidence, and creates audit-friendly artifacts."

### E2. 45-Second Technical Answer
"We use retrieval-grounded generation: fetch relevant ADO context, sanitize PHI, generate structured QA outputs, and track provenance. We manage reliability with retries/fallbacks and enforce governance through prompt/version controls and monitoring."

### E3. Cost Justification Answer
"The business case is time and defect economics: less analyst effort per request, faster cycle decisions, and fewer escaped defects. We monitor cost per useful output and tune model/context policies to keep spend controlled."

### E4. Clarification-Value Answer
"Clarification is not manual rework. It is a one-step disambiguation that replaces multiple ADO filtering and navigation actions, then AI completes the synthesis automatically."

---

## Section F: Decision Matrix Talking Points

### Build vs Buy
Q: Should we build this or buy a generic copilot?
A: Buy is faster for generic tasks; build wins where domain workflow, PHI compliance, and custom grounded outputs are mandatory.

### Prompting vs Fine-Tuning
Q: Which is better?
A: Prompting + retrieval first for speed/cost; fine-tune only when evidence proves repeated gaps.

### Single Model vs Multi-Model
Q: Why not standardize one model?
A: Multi-model strategy enables better cost-performance by task and resilience against provider outages.

---

## Section G: Business Benefits Summary

1. Faster QA artifact generation and review cycles.
2. Better requirement-to-test traceability.
3. Earlier identification of defect risk areas.
4. Reduced manual triage/navigation effort in ADO.
5. Improved consistency and auditability of QA decisions.
6. Controlled PHI exposure through sanitization pipeline.
7. Scalable AI operations with governance and observability.

---

## Section H: Risks and Mitigations Summary

1. Retrieval mismatch risk.
Mitigation: confidence gates, candidate disambiguation, source transparency.

2. Hallucination risk.
Mitigation: strict grounding, structured output templates, human review.

3. Cost overrun risk.
Mitigation: token budgets, model routing, caching, FinOps dashboards.

4. Compliance risk.
Mitigation: PHI sanitization, controlled endpoints, audit logging, policy checks.

5. Adoption risk.
Mitigation: embed in existing workflow, reduce friction, show measurable wins.

---

## Section I: Questions to Ask Back (So You Control the Room)

1. Which KPI matters most for this quarter: speed, quality, or cost?
2. Which workflows should be phase-1 mandatory coverage?
3. What confidence threshold is acceptable before auto-answering?
4. What audit depth is required by compliance teams?
5. What is the acceptable per-request cost target?

---

## Section J: Tuesday Demo Recommended Narrative

1. Problem: QA analysis is slow and inconsistent when done manually.
2. Solution: Grounded AI with PHI-safe pipeline and structured QA outputs.
3. Proof: Show 3 real prompts (ID exact, no-ID search, ambiguous requiring clarification).
4. Governance: Show source/provenance, sanitization, and monitoring path.
5. Outcome: Faster, safer, more consistent QA decisions with measurable ROI.

---

## Appendix: One-Line Responses for Common Objections

Q: "AI is just hype."
A: "Only if ungrounded. Grounded QA automation shows measurable cycle-time and quality impact."

Q: "Users can do this in ADO manually."
A: "Yes, but AI removes repetitive search/synthesis steps and standardizes outputs."

Q: "This looks expensive."
A: "We track cost per useful output and optimize routing; savings come from reduced manual analysis and defect leakage."

Q: "What if model output is wrong?"
A: "We enforce confidence checks, source grounding, and human approval for high-impact decisions."

Q: "Why not wait for tooling to mature?"
A: "The architecture is modular, so we can adopt newer models without rebuilding the workflow foundation."
