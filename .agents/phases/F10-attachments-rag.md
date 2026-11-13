# F10 — Attachments + RAG

Status: partial (B110 managed KB path built: upload presign, SQS + DLQ processor, managed KB ingestion, runtime retrieval in all 4 agents, chat upload UI; per-file GET/DELETE, image types, CSV→Code Interpreter, and image→vision not built; needs live AWS verification; see "Implementation status") | Depends on: F6, S5 | Decisions: B31, B32, B33, B35, B38, B70, B77, B78, B86, B110 (supersedes B33, B77) | Specs: [`../specs/data-model.md`](../specs/data-model.md) §5 (Attachment Metadata Store), §7 (S3 buckets), §9 (AgentCore Memory, sibling), [`../specs/tools.md`](../specs/tools.md) §7 (KB retrieval), [`../specs/api.md`](../specs/api.md) (Attachments routes)

## Implementation status (2026-11-12)

B110 replaces the S3 Vectors index and the KB retrieval Gateway target in the plan below. Tasks 4–6 follow B110, not B33/B77.

Built:
- Attachments S3 bucket (`backend-stack.ts`, `RETAIN`, objects under `uploads/`) and the `AttachmentsMetadataStore` table with a session GSI.
- `POST /attachments/presign` (`backend/lambdas/attachment-presign/handler.py`): checks `content_type`, checks an optional declared `size`, writes the metadata row, and returns a presigned **POST** (5-minute expiry). The POST policy holds `content-length-range` 1–55,000,000 bytes and the exact `Content-Type`, so S3 rejects a larger body before it stores the object (B32). The bucket CORS rule allows `POST` only, from any origin (the signed policy is the access control; a CloudFront origin would make a stack cycle). Allowed types: PDF, TXT, MD, CSV, DOCX, XLSX. The route is `/attachments/presign`, not the `/sessions/{id}/attachments/presign` path in the Scope section.
- SQS queue with a DLQ (`maxReceiveCount: 3`) on S3 object-created events, and the Attachment Processor (`backend/lambdas/attachment-processor/handler.py`). The processor checks the metadata row against the S3 key, rejects files over 50 MB, and calls `IngestKnowledgeBaseDocuments` on the managed KB CUSTOM data source (document id = `attachment_id`; inline metadata `user_id`, `session_id`, `file_id`, `filename`).
- Managed Knowledge Base construct: `backend/lib/constructs/attachments-knowledge-base.ts` (`type: MANAGED`).
- Runtime retrieval: `backend/agents/shared/tools/attachments_kb.py` calls `bedrock:Retrieve` with an `andAll` `user_id` + `session_id` filter from the verified `AuthorizedSession`. Research, Drafting, Analysis and the Pipeline dispatcher use it. The keyword-scan `rag_tool.py` is removed.
- Frontend: file upload in the chat composer (`frontend/src/components/LiveAgentChat.tsx`); the CSP `connect-src` includes the attachments bucket origin.

Not built:
- `GET`/`DELETE` for one attachment (task 10).
- PNG/JPG types, the image→vision path (task 9) and the CSV→Code Interpreter path (task 8).
- The processor still rejects files over 50 MiB (the managed KB per-file quota). A file between 50 MiB and 55 MB uploads, then fails at the processor.
- Attachments, metadata rows and KB documents are not in the account-delete cascade (F19).

## Goal

Let a user attach a file (PDF, DOCX, TXT, MD, CSV, PNG, JPG — max 55 MB) to a chat, have it indexed into the Knowledge Base per chat, and have every agent retrieval for that chat honor a metadata filter so a chat only ever retrieves its own attachments. CSV also reaches the Code Interpreter directly; PNG/JPG also reach vision-capable models directly.

## Scope — In / Out

**In:** `POST /sessions/{id}/attachments/presign`, `GET/DELETE /sessions/{id}/attachments/{fileId}`, the Attachments S3 bucket (presigned POST, `content-length-range` capped at 55 MB per B32), the SQS + Attachment Processor pipeline, the Attachment Metadata Store table, the Bedrock Knowledge Base + S3 Vectors index (using S5's confirmed setup), the KB retrieval Gateway Lambda target (B77: adds the `user_id`+`session_id` filter from token claims, the agent cannot change it), and the direct CSV→Code-Interpreter and PNG/JPG→vision-model paths (B38).

**Out:** attachment deletion as part of the full chat/account delete cascade (F19 owns the cascade itself; this phase's `DELETE` route removes one file on request, which is a different, simpler operation than F19's cascade, though this phase's delete logic is reused by F19), user-configured paid sources (deferred), any UI beyond a basic attach/list/remove control in the chat composer.

## Deliverables

- `backend/lambdas/attachment-upload-worker/` (new, Python) — `POST /sessions/{sessionId}/attachments/presign`: validates `content_type` against the B32 allowlist, generates a presigned POST with `content-length-range` capping at 55,000,000 bytes, writes an initial `AttachmentMetadataStore` row (`status: "uploading"`).
- `backend/lib/backend-stack.ts` (modify) — add: the Attachments S3 bucket (private, per-chat key scheme `{user_id}/{session_id}/{file_id}/{filename}`), an SQS queue on S3 object-created events, the Attachment Processor Lambda, the Attachment Metadata Store table (`PK=session_id`, `SK=file_id`, GSI-1 `user_id`/`created_at` per `data-model.md` §5), the Bedrock Knowledge Base + S3 Vectors index (production version of S5's spike), and the KB retrieval Gateway target added to F4's Gateway.
- `backend/lambdas/attachment-processor/` (new, Python) — SQS-triggered: extracts metadata, checks the file belongs to the claimed session (per-chat scope, B31), updates `AttachmentMetadataStore.status`, triggers KB ingestion with `user_id`/`session_id`/`file_id` chunk metadata (B33).
- `backend/lambdas/kb-retrieval/` (new, Python) — the Gateway target: takes a query + the caller's token claims, builds the `user_id`+`session_id` metadata filter server-side (the agent/caller cannot supply or override it), calls KB retrieval, returns ranked chunks with `file_id`/page-offset citation fields.
- `backend/lambdas/attachments-handler/` (new, Python) — `GET /sessions/{sessionId}/attachments/{fileId}` (status), `DELETE /sessions/{sessionId}/attachments/{fileId}` (removes S3 object, metadata row, KB chunks for that file).
- `backend/agents/research/nodes.py`, `drafting/nodes.py` (modify) — add the KB retrieval tool call (per-chat attachments) alongside the existing 10 data-source tools; add the CSV→Code-Interpreter and PNG/JPG→vision-model direct-use paths (B38) where an attachment of that type is present for the current turn.

## Tasks

- [ ] 1. Add the Attachments S3 bucket, SQS queue, and Attachment Metadata Store table to `backend-stack.ts`, following S5's confirmed KB/S3-Vectors setup for the KB itself.
- [ ] 2. Write `attachment-upload-worker`: validate `content_type` against {PDF, DOCX, TXT, MD, CSV, PNG, JPG}, generate the presigned POST with the 55 MB `content-length-range` condition, write the initial metadata row.
- [ ] 3. Write `attachment-processor`: on the SQS-triggered S3 event, confirm the uploaded object matches the claimed `session_id`/`user_id` (reject anything that doesn't, per B31's per-chat scope), update `status` to `processing` then `ready`/`failed`, and trigger KB ingestion.
- [ ] 4. Add the KB ingestion trigger with the S5-confirmed FM parser path, tagging every resulting chunk with `user_id`/`session_id`/`file_id` metadata (B33).
- [ ] 5. Write `kb-retrieval` as a new Gateway target added to F4's Gateway: the Lambda reads `sub` and the session ID from the forwarded token's claims (per `auth-and-tool-scope.md` §5), builds the metadata filter itself, and ignores/rejects any caller-supplied filter override attempt (per B77 and S5's confirmed guarantee).
- [ ] 6. Add the KB retrieval Gateway target under the existing shared `cei-tools/common` scope (B70/B86 — the same scope F4 already created for LS Ops/Web Search; KB retrieval is not a data source in the B27 sense, so it does not get its own `cei-tools/src.*` scope). No new Cognito resource-server scope is needed; only the new Gateway target and its Cedar permit.
- [ ] 7. Wire the KB retrieval tool call into Research's and Drafting's graphs, alongside the existing 10 data-source tools — call it whenever the current session has `ready` attachments.
- [ ] 8. Add the direct CSV→Code-Interpreter path: when a CSV attachment is present, pass it as a file input to the Code Interpreter tool (`shared/tools/code_interpreter.py`) in addition to its KB-indexed chunks.
- [ ] 9. Add the direct PNG/JPG→vision-model path: when an image attachment is present, include it directly in the model call's multimodal input, in addition to its KB-indexed chunks.
- [ ] 10. Write `attachments-handler`: `GET .../{fileId}` (status), `DELETE .../{fileId}` (S3 object + metadata row + KB chunks for that file only).

## Acceptance criteria

- A presigned upload over 55 MB is rejected by S3 before the object is stored (the `content-length-range` condition, not application-level validation, enforces this).
- Each of the seven B32 file types uploads, processes, and reaches `status: "ready"`.
- A KB retrieval call for session A never returns chunks from session B, even when both sessions' attachments would otherwise match the query (reusing S5's confirmed guarantee, now enforced in production against a real token-claim-derived filter).
- No caller-supplied input can widen or bypass the KB retrieval filter.
- A CSV attachment is usable by the Code Interpreter directly, independent of its KB chunks; a PNG/JPG attachment is usable by a vision-capable model call directly, independent of its KB chunks.
- `DELETE .../{fileId}` removes the S3 object, the metadata row, and that file's KB chunks, and a subsequent retrieval no longer returns anything from it.
- `npx cdk synth` passes cdk-nag with ADR-format suppressions only.

## Tests

- `pytest`: `attachment-upload-worker`'s content-type/size validation, presigned POST condition correctness; `attachment-processor`'s cross-session-claim rejection (B31 enforcement) and status transitions; `kb-retrieval`'s filter-cannot-be-overridden guarantee (mocked KB client, assert the filter used matches only the token claims regardless of what the query payload contains).
- `pytest`: `attachments-handler`'s get/delete happy paths and cross-user isolation (user A cannot `GET`/`DELETE` user B's attachment).
- Integration test (scripted, extending S5's spike harness): upload one file of each type, wait for `ready`, run a KB retrieval query, confirm scoping; upload a CSV and confirm the Code Interpreter can read it; upload a PNG and confirm a vision-model call can see it.
- CDK assertion tests: Attachments bucket has no public access, SQS queue and its DLQ (if added) exist, Attachment Metadata Store GSI-1 exists as specified.

## Risks and open items

- If S5's spike found any cross-session leakage or FM-parser gap (its own named risks), this phase must not proceed past that finding without it being resolved or explicitly accepted by the user first — do not silently reimplement around a known S5 gap.
- The Export outputs bucket (F7) and the Attachments bucket (this phase) are separate buckets with separate lifecycles — do not conflate their key schemes or delete logic.
- B70/B86 resolve KB retrieval's scope: it shares the existing `cei-tools/common` scope, not a dedicated scope. Nothing further to decide here.

## Needs live AWS verification

- Upload each allowed type and confirm it reaches `ready` in the managed KB.
- A retrieval in session A never returns a document from session B or from another user.
- A browser POST over 55,000,000 bytes gets an S3 `EntityTooLarge` error and no object is stored; a file between 50 MiB and 55 MB uploads and its metadata row shows `failed`.
- A message that fails 3 times goes to the DLQ.

## Handoff notes

- F19 (Data deletion) reuses this phase's per-file delete logic (S3 object + metadata + KB chunks) as one step inside the larger chat-delete and account-delete cascades — F19 should call into this phase's deletion code path per attachment, not reimplement it.
- F11 (Long-term memory) is unrelated to this phase's KB (long-term memory uses AgentCore Memory strategies, not the Bedrock Knowledge Base) — do not conflate the two "memory" concepts when handing off.
- F14/F17 (Analysis agent, Pipeline Manager), once built, should wire the same KB retrieval tool call this phase adds to Research/Drafting.
