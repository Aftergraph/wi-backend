# Autonomous Work Detection: From Ambient Signals to Evidence-Backed Work Items

**Principal Researcher:** Jonas Abde  
**Program:** Jonas Abde Intelligence Systems Research Program — Q3 2026  
**Publication Track:** Working Paper / Product Research Protocol  
**Date:** 5 September 2026  
**Status:** ENGINEERING QUALIFICATION COMPLETE; 30-DAY FIELD VALIDATION PENDING  
**Reference Implementation:** Wie by Aftergraph (V2)  
**Planned First Field Environment:** RenOS / Rendetalje operational workflows  

---

## Abstract

Operational work is often created before it is represented in a task system. A promise in an email, an instruction in a conversation, a changed calendar appointment, an unresolved customer request, or a software-system event can all create a real obligation while leaving no durable work record. Existing work-management products increasingly reduce this capture gap through integrations, AI assistants, channel rules, meeting-note extraction, and event-driven automation. Linear, Asana, ClickUp, Motion, Notion, and Zapier all provide meaningful pieces of this behavior.

This paper therefore does **not** claim novelty for “AI that creates tasks.” Instead, it investigates a narrower systems hypothesis: **actionable work can be treated as a source-independent inference object that is resolved across heterogeneous observations before it is published into one or more destination systems**. We call this capability *Autonomous Work Detection* (AWD).

The proposed architecture separates six concerns: `Signal → Observation → WorkCandidate → Resolution → WorkItem → Publication`. The separation makes provenance, deduplication, tenant policy, correction, and destination portability first-class. The V1 reference implementation deliberately begins with a deterministic extraction baseline, durable SQLite persistence, source-id idempotency, conservative same-tenant resolution, and an authenticated HTTP surface. Richer LLM extraction and external connectors are replaceable adapters rather than trusted-core requirements.

A 30-day RenOS/Rendetalje field study is proposed to falsify or support the hypothesis using actionable-work recall, false-creation rate, duplicate rate, false-merge rate, correction burden, manual ticket-creation rate, and the number of important obligations detected before a human independently records or recalls them.

---

## 1. Problem

### 1.1 Work exists before tickets

A task-system record is a representation of work, not the work itself. Obligations originate across many surfaces:

- conversations and voice transcripts;
- email threads;
- calendar events and schedule changes;
- team chat;
- customer and CRM records;
- code repositories and CI events;
- monitoring and incident systems;
- internal business applications;
- notes and documents.

The conventional path is frequently:

```text
real-world obligation
        ↓
human notices it
        ↓
human remembers to record it
        ↓
human chooses the correct destination
        ↓
human writes a task
        ↓
work becomes trackable
```

Every transition is a loss point. If the obligation is never recorded, scheduling, assignment, verification, and reporting cannot recover it.

### 1.2 Action-item extraction is established research

The linguistic problem is not new. Morgan et al. studied automatic action-item detection in meeting recordings in 2006; Purver et al. studied detection and summarization in multi-party dialogue in 2007. Diwanji et al. (2020) explicitly frame commitments and requests in email/chat as task-extraction targets. Mukherjee et al. (2020) introduced automatic To-Do generation from emails, while Zhang et al. (2022) focused on faithful email To-Do summarization. MailEx (Srivastava et al., 2023) demonstrates that conversational email event extraction remains difficult because of conversational history and complex event arguments.

The proposed contribution is therefore at the systems boundary: **resolve task-producing evidence from multiple sources into one durable canonical work representation before destination-specific publication**.

---

## 2. Market Landscape, September 2026

### Linear

Linear Asks accepts requests from Slack, email, and web surfaces. Advanced Slack Asks can automatically create an Ask for each new message in a configured intake channel, and synced threads keep later conversation attached. The general Linear Slack agent can infer issue details from conversation context when invoked.

**Implication:** automatic communication-to-issue capture is already real. The open question is whether a source-neutral canonical work layer adds value beyond Linear-centered intake.

### Asana

Asana can create tasks from Slack threads via `@Asana`, infer assignee/project/due date/custom fields from context, and use Slack rules to create tasks automatically from new channel messages. Two-way synchronization preserves subsequent discussion.

**Implication:** contextual field inference and automatic chat intake are established competitive capabilities.

### ClickUp

ClickUp Brain can create tasks from prompts, text, comments, chat messages, and voice clips. Brain Agents expose task creation and update tools.

**Implication:** work creation is becoming an ambient capability inside workspaces rather than a form-only interaction.

### Motion

Motion supports task creation through AI chat and email, and its AI Notetaker identifies action items after meetings and suggests them as tasks. Motion then differentiates downstream through automatic scheduling.

**Implication:** extraction and scheduling are converging, but meeting-derived tasks can still be approval-centric rather than continuously resolved against other sources.

### Notion

Notion AI Meeting Notes transcribes meetings, identifies key points and action items, and connects notes to Notion Calendar and task/project databases.

**Implication:** meeting context is increasingly treated as structured work input.

### Zapier Agents

Zapier Agents can run on triggers from apps such as Gmail and Slack and take actions across a large application ecosystem.

**Implication:** source/destination portability is already strong in generic automation platforms. AWD must prove that a dedicated durable *work-resolution* layer delivers value beyond trigger-action configuration.

### Competitive thesis

The defensible hypothesis is not “the market lacks automatic tasks.” It does not.

The hypothesis to test is:

> A destination-neutral intermediate representation that resolves observations across channels, retains provenance, and publishes to multiple destinations can reduce missed work, duplicate work, and destination lock-in compared with independent channel-to-task automations.

This remains unvalidated until field evidence exists.

---

## 3. Research Questions

**RQ1 — Actionability:** Can heterogeneous operational observations be classified as actionable/non-actionable with sufficient precision and recall for unattended capture?

**RQ2 — Resolution:** Can overlapping observations from different sources be merged into one WorkItem without destructive false merges?

**RQ3 — Provenance:** Does retaining observation-to-work lineage reduce correction time and improve operator trust compared with opaque generated tasks?

**RQ4 — Portability:** Does keeping canonical WorkItems independent of destinations reduce integration cost or lock-in across RenOS, issue trackers, calendars, and execution systems?

**RQ5 — Operational value:** Does autonomous capture reduce missed obligations and manual ticket-entry effort in real operations?

---

## 4. System Model

```text
Source event
    │
    ▼
  Signal
    │
    ▼
Observation ───────────────┐
    │                      │ provenance
    ▼                      │
WorkCandidate              │
    │                      │
    ▼                      │
Resolution / dedupe ◄──────┘
    │
    ▼
 WorkItem
    │
    ├────────► RenOS ticket
    ├────────► Linear/Jira/Asana issue
    ├────────► GitHub issue
    ├────────► calendar action
    └────────► governed executable mission (only through a separate authority boundary)
```

### Signal

A source-native event such as a Gmail message, Calendar update, transcript segment, RenOS customer event, or repository event.

### Observation

The durable source-neutral evidence record. V1 stores tenant, source, optional external source ID, actor, raw text, source metadata, occurrence time, and ingestion time.

The raw observation is retained because extraction can be wrong. A generated WorkItem without inspectable evidence is difficult to audit or correct.

### WorkCandidate

An inference containing proposed title, next action, priority, owner/deadline hints, confidence, reason, and canonicalization features. Candidates may later be created by rules, classifiers, LLMs, or tenant-specific models without changing the downstream contract.

### Resolution

Resolution determines whether a candidate creates new work, updates existing work, becomes supporting evidence only, is ignored, or requires review. This is the architectural center of AWD.

### WorkItem

The canonical operational representation. It is deliberately not identical to an executable WORKS `Work`: reminders, customer follow-ups, approvals, and human work must not silently acquire execution authority.

### Publication

A destination adapter maps canonical WorkItems to RenOS or another system on demand. The request names a configured destination, not an arbitrary URL.

---

## 5. V1 Baseline

The first implementation uses deterministic Danish/English actionability rules instead of an LLM.

This provides:

- reproducible behavior;
- a dependency-free inference baseline;
- deterministic regression tests;
- no model credential requirement;
- a baseline against which later model extraction can be measured;
- easier diagnosis of false positives and false merges.

Typical V1 action markers include explicit commitments and requests such as `skal`, `husk`, `mangler`, `send`, `ring`, `book`, `køb`, `fix`, `opdater`, `need to`, `must`, and `follow up`.

V1 intentionally keeps uncertain deadlines as text hints rather than fabricating exact timestamps.

---

## 6. Provenance, Security, and Failure Modes

### Data minimization

“Ambient” must not mean unrestricted surveillance. Connectors should ingest only sources explicitly enabled by the tenant, with separate retention/access controls for sensitive classes.

### Prompt injection / hostile source text

Incoming email, chat, and documents are untrusted data. They may describe work but must not acquire authority simply because they contain imperative language. Detection and execution remain separate boundaries.

Recent provenance research supports this separation. Wang et al. (2026) survey execution provenance as a foundation for debugging and accountability in LLM agents. She, Liang, and Kang (2026) model tool-action alignment through traceable evidence, while Liao (2026) reports that model action selection can remain sensitive to untrusted evidence even when source-authority cues are present.

### False positive

Non-work becomes work, causing noise and trust degradation.

### False negative

A real obligation is missed. Recall must therefore be measured against a human-reviewed ground truth.

### False merge

Two distinct obligations become one. This is potentially more dangerous than duplicate creation because one obligation can disappear. V1 therefore uses conservative, tenant-bounded merging.

### Duplicate creation

One obligation becomes multiple WorkItems. Less dangerous than false merge, but operationally costly.

### Incorrect owner/deadline

Structured fields can create false certainty. V1 avoids inventing fields unsupported by source evidence.

---

## 7. RenOS / Rendetalje Field Protocol

### Study unit

The unit is an **actionable obligation**, not a generated ticket.

### Duration

30-day prospective dogfood study.

### Proposed phases

1. **Shadow calibration, 3–7 days:** observations are processed and scored; operator review establishes baseline noise.
2. **Automatic internal creation:** sufficiently confident candidates create internal WorkItems automatically.
3. **Destination publication:** selected WorkItems publish into RenOS after false-creation/false-merge gates are acceptable.

### Metrics

Let `G` be the human-reviewed true obligations and `D` the automatically detected obligations.

**Actionable Work Recall (AWR)**  
`AWR = |G ∩ D| / |G|`

**Relevant Creation Rate (precision, RCR)**  
`RCR = |G ∩ D| / |D|`

**False Creation Rate (FCR-AWD)**  
`FCR-AWD = 1 - RCR`

This is intentionally distinct from the Intelligence Systems Research Program's False Completion Rate.

**Duplicate Work Rate (DWR):** generated WorkItems duplicating an already-open obligation.

**False Merge Rate (FMR):** merge decisions combining distinct ground-truth obligations.

**Manual Capture Rate (MCR):** true obligations still requiring manual task creation.

**Early Detection Count (EDC):** important obligations recorded before the operator independently records or explicitly recalls them.

**Median Correction Time (MCT):** human time required to correct a wrong WorkItem using its source evidence.

### Product targets, not achieved results

- stretch target `AWR ≥ 0.90`;
- stretch target `FCR-AWD < 0.10`;
- `FMR < 0.02` before unattended cross-source merging;
- zero cross-tenant merges;
- every created/merged WorkItem traceable to at least one Observation;
- material reduction in manual task creation.

### Falsification gates

Redesign or reject the product thesis if, after calibration:

- `AWR < 0.80` on representative sources;
- `FCR-AWD > 0.20` despite threshold tuning;
- false merges remain operationally significant;
- correction effort approaches or exceeds manual capture effort;
- source-specific automations deliver equal value at substantially lower complexity;
- provenance is rarely useful and adds burden without measurable trust/debugging value.

---

## 8. Relationship to the Aftergraph Architecture

The current Aftergraph governance material defines WORKS as the durable execution plane, Trust Gateway as runtime control/enforcement, AIE as a normative authority track, and ABDE Research as the research/evaluation surface. WORKS already exposes a durable `Work` primitive, SQLite-backed control-plane state, evidence/provenance, auth, and an HTTP API.

AWD should therefore remain a separate work-intake boundary:

```text
Work Intelligence             WORKS execution plane
-------------------------     -------------------------
Observation                   executable Work
WorkCandidate                 DAG / nodes
WorkItem                      leases / workers
resolution                    verification / evidence
publication adapter   ───►    optional governed Work creation
```

This avoids a dangerous shortcut: text that merely *looks like an instruction* does not automatically become executable authority.

RenOS is the first consumer and dogfood environment, not a hard-coded domain. Rendetalje-specific vocabulary and ownership rules belong in tenant policy/adapters.

---

## 9. V1 Acceptance Criteria

V1 is complete when:

1. A source-neutral API accepts an Observation.
2. Explicit Danish/English work statements can create WorkItems automatically.
3. Baseline completed/non-actionable statements do not create WorkItems.
4. Every Observation is durable.
5. `(tenant, source, external_id)` replay is idempotent.
6. Same-tenant repeated observations can merge conservatively.
7. Cross-tenant observations never merge.
8. WorkItems expose supporting Observations.
9. WorkItems can be listed/retrieved over API.
10. Optional bearer authentication works.
11. On-demand publication uses operator-configured destination adapters.
12. Publication receipts are durable.
13. The reference implementation has automated tests and a live local smoke test.

Gmail/Calendar/RenOS OAuth plumbing is explicitly outside the core V1 acceptance gate. Those are adapters to the stable Observation/Publication contracts, not reasons to postpone testing the work-resolution hypothesis.

---

## 10. Current Reference-Implementation Evidence

As of 5 September 2026, the local V1 reference implementation has:

- deterministic Danish/English actionability extraction;
- SQLite observation/work/publication persistence;
- idempotent external-source replay;
- conservative same-tenant deduplication;
- cross-tenant isolation;
- provenance links from WorkItems back to Observations;
- FastAPI/OpenAPI ingress and query surfaces;
- optional bearer auth;
- configured HMAC webhook publication;
- **15/15 local automated tests passing**;
- a live Uvicorn smoke test that created and listed a RenOS-tenant WorkItem from the sentence “Vi skal sende kunden en bekræftelse før mandag.”

These are software verification results, not field-effectiveness results. No recall, precision, productivity, or commercial claims are justified yet.

---

## References

1. Morgan, W., Chang, P.-C., Gupta, S., & Brenier, J. M. (2006). *Automatically Detecting Action Items in Audio Meeting Recordings*. SIGDIAL. https://aclanthology.org/W06-1314/
2. Purver, M., Dowding, J., Niekrasz, J., Ehlen, P., Noorbaloochi, S., & Peters, S. (2007). *Detecting and Summarizing Action Items in Multi-Party Dialogue*. SIGDIAL. https://aclanthology.org/2007.sigdial-1.4/
3. Diwanji, P., Guo, H., Singh, M., & Kalia, A. (2020). *Lin: Unsupervised Extraction of Tasks from Textual Communication*. COLING. https://aclanthology.org/2020.coling-main.164/
4. Mukherjee, S., Mukherjee, S., Hasegawa, M., Hassan Awadallah, A., & White, R. (2020). *Smart To-Do: Automatic Generation of To-Do Items from Emails*. ACL. https://aclanthology.org/2020.acl-main.767/
5. Zhang, K., Chen, J., & Yang, D. (2022). *Focus on the Action: Learning to Highlight and Summarize Jointly for Email To-Do Items Summarization*. Findings of ACL. https://aclanthology.org/2022.findings-acl.323/
6. Srivastava, S. et al. (2023). *MailEx: Email Event and Argument Extraction*. EMNLP. https://aclanthology.org/2023.emnlp-main.801/
7. Linear. *Linear Asks; Asks with Slack; Slack integration*. Accessed 5 Sep 2026. https://linear.app/docs/linear-asks ; https://linear.app/docs/linear-asks-slack ; https://linear.app/docs/slack
8. Asana. *Chat with Asana in Slack channels; Slack and Asana*. Accessed 5 Sep 2026. https://help.asana.com/s/article/chat-with-asana-in-slack-channels ; https://help.asana.com/s/article/slack-and-asana
9. ClickUp. *Create items with Brain AI*. Accessed 5 Sep 2026. https://help.clickup.com/hc/en-us/articles/19953994898711-Create-items-with-Brain-AI
10. Motion. *Task creation methods*. Accessed 5 Sep 2026. https://www.usemotion.com/help/project-management/task
11. Notion. *AI Meeting Notes*. Accessed 5 Sep 2026. https://www.notion.com/help/ai-meeting-notes
12. Zapier. *Set up your agent's trigger; Use actions on Zapier Agents*. Updated May 2026. https://help.zapier.com/hc/en-us/articles/45394909914381-Set-up-your-agent-s-trigger ; https://help.zapier.com/hc/en-us/articles/26028298697485-Use-actions-on-Zapier-Agents
13. Wang, Y. et al. (2026). *From Agent Traces to Trust: Evidence Tracing and Execution Provenance in LLM Agents*. arXiv:2606.04990. https://arxiv.org/abs/2606.04990
14. She, Y., Liang, Y., & Kang, E. (2026). *Safeguarding LLM Agents from Misalignment through Provenance Analysis*. arXiv:2607.01236. https://arxiv.org/abs/2607.01236
15. Liao, J. (2026). *Auditing Provenance Sensitivity in LLM Agent Action Selection*. arXiv:2607.20827. https://arxiv.org/abs/2607.20827

---

## Research Integrity Note

Competitive feature descriptions are based on vendor documentation available on 5 September 2026. Target metrics are targets, not achieved results. Any later claim of recall, precision, reduced manual effort, commercial differentiation, or operational superiority requires versioned field-study evidence before publication.
