# SmartCare QA Assistant — Copilot Studio Configuration

## Prerequisites
- Copilot Studio environment ready
- Power Automate cloud connection available
- Bridge API running on http://127.0.0.1:8010

---

## Topic 1: Generate QA Pack

**Topic Name:** Generate QA Pack for Feature

**Trigger Phrases:**
- "Generate test cases for {subject}"
- "Create QA pack for {subject}"
- "Generate regression tests for {subject}"
- "I need test cases for {subject}"
- "Generate test suite for {subject}"

**Entities:**
- Subject (string, required): The feature or scenario name

**Dialog Flow:**

1. **Initial Trigger**
   - User: "Generate test cases for Patient Login"
   - System recognizes: subject = "Patient Login"

2. **Clarification (conditional)**
   - Ask: "Got it! You want test cases for '{Subject}'. Do you have an Azure DevOps work item ID to link?"
   - Allow user to provide work_item_id (optional)

3. **Call Power Automate Flow**
   - Trigger: "Call SmartCare Power Automate Flow"
   - Wait for response (Adaptive Card with metrics)

4. **Reply with Results**
   - Direct reply showing the Adaptive Card from Power Automate
   - Include summary, metrics, test cases sample

5. **Offer Follow-up**
   - "Would you like to:"
   - Option A: "Export this to a file"
   - Option B: "Generate user guide for this feature"
   - Option C: "Check platform status"

---

## Topic 2: Platform Status

**Topic Name:** SmartCare Platform Status

**Trigger Phrases:**
- "What is your status?"
- "Are you ready?"
- "Show platform health"
- "What can you do?"
- "Tell me about yourself"

**Dialog Flow:**

1. **Fetch Status**
   - Action: HTTP GET to `http://127.0.0.1:8010/bridge/info`
   - Parse response

2. **Compose Status Reply**
   ```
   SmartCare QA Assistant is operational!
   
   ✅ Service: Copilot/Power Automate Bridge
   ✅ Mode: Mock (offline demo mode)
   ✅ Default: QA pack generation
   ✅ Compliance: HIPAA-safe output
   
   Features available:
   • Generate test cases for any feature
   • Risk-based regression pack generation
   • Compliance-safe output (PHI sanitized)
   • Audit trail of all requests
   
   Status: Ready for demo
   Next: Awaiting Azure OpenAI private endpoint access (IT ticket in progress)
   ```

3. **Offer Next Steps**
   - "Try saying: 'Generate test cases for Patient Registration'"

---

## Topic 3: Export Test Cases (Optional)

**Topic Name:** Export Test Cases

**Trigger Phrases:**
- "Export the test cases"
- "Save as file"
- "Download the results"

**Dialog Flow:**

1. **Ask Confirmation**
   - "Export the last generated QA pack?"
   - Options: Yes / No / Different format

2. **Call Export Action**
   - Use Power Automate connector to:
     - Save Adaptive Card as JSON to OneDrive
     - Or email the pack
     - Or post to Teams channel

3. **Confirm Export**
   - "✅ Exported: REQ-20260415152358-qa-pack.json to [location]"

---

## Setup Steps in Copilot Studio

1. **Create new bot:** SmartCare QA Assistant
2. **Add Topic 1:** Generate QA Pack
   - Copy triggers as listed above
   - Add entity: subject (string)
   - Create dialog node tree per flow above
3. **Add Topic 2:** Platform Status
   - Copy triggers
   - Create HTTP action to /bridge/info
4. **Test in browser:**
   - Query: "Generate test cases for Patient Onboarding"
   - Expected: Receive Adaptive Card with 3 test cases, metrics, compliance badges
5. **Publish to Teams/Web** when demo-ready

---

## Configuration Details

**Bridge API Endpoints Used:**
- `POST /bridge/copilot-chat` — for direct Copilot calls (alternative to Power Automate)
- `GET /bridge/info` — for platform status
- Headers: `Content-Type: application/json`
- No API key required in demo mode (set `SMARTCARE_BRIDGE_API_KEY_REQUIRED=false`)

**Expected Adaptive Card Response:**
```json
{
  "channel": "copilot_studio",
  "status": "success",
  "summary": "Generated 3 risk-balanced test cases for 'Patient Onboarding'",
  "metrics": {
    "total_test_cases": 3,
    "critical_tests": 1,
    "high_tests": 1,
    "medium_tests": 1,
    "estimated_minutes_saved": 75,
    "traceability_lines": 3
  },
  "compliance": {
    "hipaa_safe": true,
    "phi_sanitized": true
  }
}
```

---

## Testing Checklist

- [ ] Bot responds to all trigger phrases for Topic 1
- [ ] Bot fetches platform status from /bridge/info for Topic 2
- [ ] Adaptive Card renders correctly in Teams/Web
- [ ] Metrics display correctly (test counts, minutes saved)
- [ ] Compliance badges are visible and accurate
- [ ] Bot offers follow-up options (export, user guide, status)
- [ ] Demo script talks through exactly what user sees
