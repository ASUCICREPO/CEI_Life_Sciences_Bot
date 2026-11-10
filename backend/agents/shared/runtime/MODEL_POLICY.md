# Model and mode integration

`model_policy.py` is a tested foundation and is **not wired to agent calls**.
The existing `shared.utils.get_model()` currently creates an unguarded model,
and title generation has a separate D3-only blocklist. Do not represent F8 as
complete until all call paths use one policy and a deployed guardrail.

1. Provision an input-masking guardrail and pass its ID/version securely to all
   specialist and classifier runtimes; configure the exact PII/PHI entity list
   and verify B51/B51a. Keep guardrail identifiers, assessments and model IDs
   out of user-facing errors and traces.
2. Resolve each model against the account's server-discovered enabled models and
   the global blocklist before saving settings and before every invocation.
   Resolve custom inference profiles to their underlying model IDs; a profile
   with an opaque name cannot be safely classified using a string pattern alone.
   Explicitly rejected model selections must not silently fall back.
3. Reload the session's saved mode/model each turn and carry it through every
   model call, including subgraphs and title generation. Preserve the science
   and business prompt variants and the independent fixed classifier model.
4. Apply the input guardrail to user-authored text before it reaches any model,
   including prior unsanitized history and classifier inputs. Do not filter tool
   results or generated output with the PII filter. Treat missing, malformed,
   unavailable or blocked assessments as failures. `mask_user_input` expects
   a single input block and does not itself invoke a model.
5. Implement prompt caching at the model-message boundary for static system and
   tool definitions only; do not cache conversation history. Test tool-enabled,
   streaming, retries and model-specific cache compatibility.
6. Add route and frontend controls and tests for model discovery/filtering,
   session/settings ownership, mid-chat mode changes, cross-profile blocklist
   bypass, traces, and all agent paths. Verify real synthetic PII masking and
   unfiltered public tool-result data in an isolated deployment. A mock test
   cannot prove the Guardrail's configured behavior or complete coverage.

The independent ApplyGuardrail API distinguishes masking and blocking within
assessment actions even though both can report `GUARDRAIL_INTERVENED`; see
[AWS's response examples](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-use-independent-api.html).
