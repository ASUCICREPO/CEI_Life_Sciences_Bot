# Later — Deferred items

Source of truth: `.agents/decision-log.md`. These four items are explicitly deferred, not planned into any of F1–F21 or S1–S5. Each entry gives why it was deferred, what would need to be true before it could be picked up, and what is still unanswered.

## Programmatic tool calling (PTC)

**Why deferred:** B39 states PTC through the Code Interpreter is "a later-phase spike with a safety review," separate from the Code Interpreter's in-scope use for analysis and chart rendering (which F1/F5+ do cover). PTC means letting the model write and run code that itself calls tools programmatically, which is a materially larger security surface than the sandboxed, non-tool-calling code execution already in scope.

**Prerequisites:**
- F4 (Gateway + Cedar Policy) and S2 (tool scope spike) must be stable in production, since PTC would let model-authored code reach the same Gateway a human-triggered tool call reaches, and the scope model must be proven before that is safe.
- A dedicated safety review (explicitly called for in B39), not just a `/cic-security-check` pass, since this is a new capability class, not a hardening pass over existing capability.
- A clear decision on which agents (if any) get this capability — no decision-log item currently names one.

**Open questions:**
- Whether PTC is exposed to specialist agents, tool agents, or neither.
- Whether PTC calls go through the same Cedar-scoped token as the calling agent, or need a narrower one.
- Cost/abuse controls specific to model-authored code execution loops (distinct from B22's request-level throttling).

## Slides (PPTX) export

**Why deferred:** B41 replaces the D11 export list with PDF, DOCX, Markdown, CSV, LaTeX, and (per B61) keeps TXT — and explicitly marks Slides as "deferred, optional." `tools.md` §8 confirms: "not implemented, not scheduled in F1–F21."

**Prerequisites:**
- F7 (Drafting + export) must ship the six in-scope formats first, since Slides would follow the same Code-Interpreter-backed export pattern (`shared/tools/export.py`, `export_pdf.py`, `export_docx.py`, `export_latex.py`) already used for PDF/DOCX.
- A decision on which slide layout/template system to use (no current design exists for this, unlike the other formats which already have partial code).

**Open questions:**
- Whether Slides export reuses the Drafting Agent's `plan_visuals` chart specs directly, or needs its own layout logic.
- Whether Slides export is agent-invoked only (like the other formats, per B74) or also has a client-initiated route — this mirrors the same open question `api.md` already flags for ad hoc exports of any format.

## Paid, user-configured data sources

**Why deferred:** B20 states "the design of paid sources is deferred to a later discussion," distinct from the ten always-free sources and the optional-key case (for example NCBI) that F18 does cover. The decision log's own "Still open" list names this explicitly: "Design of user-configured paid sources (deferred by the user)."

**Prerequisites:**
- F18 (user API keys) must ship first, since paid sources would extend the same Secrets Manager + settings-page pattern, just for sources that do not work at all without a key (unlike the ten free sources).
- A decision on billing/cost-passthrough model (does CIC ever pay for a paid source on the user's behalf, or is it strictly bring-your-own-key) — nothing in the decision log answers this.
- A decision on how a paid source's Gateway target and Cedar scope get added dynamically (today's 11 scopes are all fixed at deploy time; a user-added paid source implies either a fixed larger catalog of possible sources or a more dynamic scope-provisioning story).

**Open questions:**
- Whether paid sources use the same `<target>___<tool>` MCP naming convention and one-scope-per-source Cedar model as the free sources, or need a different model entirely.
- Whether the Tool Agent runtime's 11-profile design (F15) extends to paid sources, or those are specialist-agent-only.
- Rate limiting and cost-abuse controls specific to a source the user pays for directly.

## Long-term memory UI

**Why deferred:** B36 states "Users cannot see or edit long-term memories in the MVP. This can come in a later phase." F11 (long-term memory) delivers the two built-in strategies (`user_preferences`, `semantic_facts`) with no UI at all, by design — this is an accepted MVP gap, not an oversight.

**Prerequisites:**
- F11 must be live in production long enough to have real memory content worth showing a user.
- A decision on read vs. read/edit vs. read/edit/delete scope for the UI — the decision log only rules out "nothing" for the MVP; it does not describe what the later UI should look like.
- A decision on whether correcting a wrong memory through a UI bypasses the "no explicit remember tool" rule (B37) or needs its own mechanism (direct AgentCore Memory API calls from a settings-page action, most likely, rather than a new agent tool).

**Open questions:**
- Whether a user can delete an individual memory item, or only wipe all long-term memory (the latter already exists implicitly via account delete, F19).
- Whether showing raw extracted `semantic_facts` to a user could itself leak something unexpected the user did not realize was being remembered — a review question, not yet asked anywhere in the decision log.
