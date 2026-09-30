# S5 — RAG spike

Status: not started | Depends on: — | Decisions: B33, B35, B38, B77 | Specs: [`../specs/data-model.md`](../specs/data-model.md) §7 (S3 Vectors bucket), §9 (AgentCore Memory, unrelated but sibling section), [`../specs/tools.md`](../specs/tools.md) §7 (KB retrieval tool), [`../specs/architecture.md`](../specs/architecture.md) (Bedrock KB row)

## Goal

Prove that a Bedrock Knowledge Base backed by S3 Vectors can ingest PDF, DOCX, TXT, MD, CSV, PNG, and JPG files via the foundation-model parser, chunk them with `user_id`/`session_id`/`file_id` metadata, and that every retrieval call can be constrained by a metadata filter so a chat only ever retrieves its own attachments. This spike de-risks B33/B35/B38 before F10 builds the real attachment pipeline, and confirms the retrieval-as-Gateway-target shape decided in B77.

## Scope — In / Out

**In:** a throwaway S3 bucket and Bedrock Knowledge Base (S3 Vectors index), manual upload of one sample file per B32 type (PDF, DOCX, TXT, MD, CSV, PNG, JPG), confirming the FM parser indexes each type, and a retrieval test that applies a `user_id`/`session_id`/`file_id` metadata filter and confirms cross-session/cross-user isolation. A minimal Gateway Lambda target wrapping KB retrieval, per B77.

**Out:** the real Attachments Bucket, Upload Worker Lambda, SQS queue, Attachment Processor Lambda, and Attachment Metadata Store (F10 builds all of these); the real per-chat scoping enforcement inside a live agent graph (F10 wires it in).

## Deliverables

- A throwaway KB + S3 Vectors index (spike-only, manual AWS console or a small script — not merged into `backend-stack.ts`)
- `backend/lambdas/spike-kb-retrieval/` (new — a minimal Python Lambda that calls KB retrieval with a metadata filter built from `user_id`/`session_id` inputs, matching the target shape decided in B77: "KB retrieval is a Gateway Lambda target; the Lambda adds the `user_id` + `session_id` filter from the token claims; the agent cannot change the filter")
- A findings note at `.agents/phases/findings/S5.md`

## Tasks

- [ ] 1. Create the throwaway S3 Vectors bucket and Bedrock Knowledge Base pointed at a source S3 prefix.
- [ ] 2. Upload one sample file of each B32 type (PDF, DOCX, TXT, MD, CSV, PNG, JPG) with distinct `user_id`/`session_id`/`file_id` values tagged as S3 object metadata or a manifest file, and trigger ingestion.
- [ ] 3. Confirm the FM parser successfully indexes each file type, including that PNG/JPG content is actually chunked (not skipped) and CSV content is chunked in a way that preserves row/column meaning reasonably well.
- [ ] 4. Confirm each resulting chunk carries `user_id`, `session_id`, `file_id` metadata (per B33) — inspect the KB's stored vector records directly if the console/API allows it.
- [ ] 5. Write the spike Gateway Lambda: given a query and a `user_id`/`session_id` (simulating token claims per B77), call KB retrieval with a metadata filter restricting to that `session_id`, and confirm results never include chunks from a different session's upload even when the query text would otherwise match them.
- [ ] 6. Confirm the Lambda's filter cannot be overridden by anything in the query payload (i.e., the agent-supplied query text has no way to widen or bypass the filter) — this is the specific guarantee B77 requires ("the agent cannot change the filter").
- [ ] 7. Test the image-to-vision-model path in isolation: confirm a PNG/JPG attachment can also be passed directly to a vision-capable Bedrock model call (per B38), separate from its KB-indexed chunks — this is a parallel path, not a KB retrieval concern, but B38 requires both to work.
- [ ] 8. Test the CSV-to-Code-Interpreter path similarly: confirm a CSV attachment can be handed to the Code Interpreter as a file input (per B38), separate from its KB chunks.
- [ ] 9. Write the findings note.

## Acceptance criteria

- All seven B32 file types are successfully indexed and retrievable from the KB.
- A retrieval query scoped to `session_id: A` never returns chunks from `session_id: B`, even for adjacent/similar content.
- The spike Gateway Lambda's filter is not overridable by the caller.
- PNG/JPG attachments are separately usable by a vision-capable model call, and CSV attachments are separately usable by the Code Interpreter, independent of KB retrieval.
- The findings note has a pass/fail verdict for every item in "Spike questions to answer" below.

## Tests

- No unit tests for the KB/S3 Vectors service itself (external AWS service behavior). The spike Gateway Lambda should have a small pytest asserting it always includes the metadata filter in its retrieval call, using a mocked KB client — this is the one piece of spike code worth a real automated test, since F10 will extend this Lambda directly.
- Record retrieval results (query, filter applied, chunks returned, chunks' `session_id`) in the findings note for auditability.

## Spike questions to answer

Findings note: `.agents/phases/findings/S5.md`.

1. Does the FM parser actually index PNG/JPG and CSV content usably, or does it silently skip/degrade them? Pass: confirms B38. Fail: B38 needs a fallback design (for example, a separate OCR/description step before KB ingestion) — flag to the user before F10 starts.
2. Does the metadata filter reliably prevent cross-session retrieval with no edge cases (for example, very similar content across two sessions)? Pass: confirms B33/B31's per-chat isolation guarantee end to end. Fail: this is a T2 (cross-user data access) security-relevant finding per `security.md` and must be raised before F10.
3. Is KB retrieval best exposed as a custom Gateway MCP target (as B77 now decides) or does the AgentCore Managed RAG built-in binding fight against applying a caller-supplied metadata filter? This spike should confirm B77's decided shape works in practice, since `tools.md` §7 previously listed this as TBD before B77 resolved it.
4. Can the retrieval Lambda enforce the filter even if a malicious or buggy caller tries to smuggle a different `session_id` into the query text itself (as opposed to the token-claim-derived filter)? Pass: confirms the filter is applied server-side from trusted claims, not from caller input.

## Risks and open items

- If PNG/JPG or CSV indexing does not work well with the FM parser (question 1), this is a real gap against B38 that needs to go back to the user, not be silently patched around in F10.
- Cross-session leakage (question 2) is a security-critical finding (T2 in `security.md`) — any failure here must be escalated, not just noted, before F10's design is finalized.
- `tools.md` §9's TBD about "custom Gateway MCP target vs. AgentCore Managed RAG built-in binding" is resolved by B77 in favor of the Gateway Lambda target shape; this spike validates that decision rather than re-opening the choice.

## Handoff notes

- F10 (Attachments + RAG) builds the real Attachments Bucket, Upload Worker, SQS pipeline, Attachment Processor, and Attachment Metadata Store, and wires the confirmed KB retrieval Gateway Lambda (extended from this spike's version) into the real agent graphs with real token-claim-derived filters (per B77, using the token flow from `auth-and-tool-scope.md` §5).
- Any cross-session leakage or FM-parser gap found here must be resolved (or explicitly accepted by the user as a documented gap) before F10 starts, since F10's entire security model for attachments rests on this spike's filter guarantee holding.
