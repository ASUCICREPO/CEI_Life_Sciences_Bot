# F9 classifier integration

`classifier.py` is an isolated foundation, not an active route. The current
Runner accepts only `live-research`, assumes every successful Router response
contains a Research Gateway token, and has no response path for a Router-created
ChoicePicker. Calling the classifier from the Router today would either break
those assumptions or expose routes whose runtimes are unavailable.

To activate it:

1. Configure a deployment-fixed Bedrock model ID which supports the specified
   Converse tool choice, a Guardrail ID and version, and least-privilege model
   and Guardrail IAM permissions for the Router. The model is independent of the
   user's chat-model setting. Provision a Guardrail whose PII/PHI filtering masks
   user input only (B51a); the exact entity list remains an implementation item.
2. Extend the authenticated Runner request contract to accept free text while
   preserving deterministic task routing and existing replay/ownership checks.
   Forward explicit absence of `task_type` to the Router only after the remaining
   steps are available. Avoid logging prompts, output, tokens or guardrail details.
3. Call `classify` with the fixed model and mandatory Guardrail configuration.
   Model errors, invalid output, missing configuration and Guardrail interventions
   must terminate safely without routing or minting a token. The helper's current
   low-confidence default is `0.7`; test and tune it with live fixture evaluation.
4. For confidence below the threshold, persist a CEI UI ChoicePicker with all four
   specialist options and end the turn. Accept the selected value only in the
   authenticated, owned session and bind it to the pending choice; route the
   original request without reclassifying. Do not treat arbitrary client values
   as proof of a pending choice.
5. Map each classification to its configured specialist runtime. Pipeline must
   use the async job path and the other specialists must follow B79. Do not report
   a destination available until its runtime and required wiring actually exist.
   Scope and token handling must retain the established user/session binding.
6. Add end-to-end Runner/Router/ChoicePicker tests for normal, ambiguous,
   invalid, denied, replayed, cross-user, unavailable destination, and
   Guardrail-intervention cases. Run a real model spot check only in an identified
   development environment and record model/version, sanitized results and
   classifier accuracy.

Bedrock applies the configured Guardrail to the marked user input; its docs say
tool-use argument fields are not assessed by Guardrails. The classifier strictly
validates the returned agent and numeric confidence and never executes output.
The helper cannot verify that the deployed Guardrail policy itself is configured
correctly, or that a particular model supports the requested tool configuration;
those require infrastructure and deployed integration tests.
