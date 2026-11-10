# B99: Gateway identity transport spike

Status: **local prototype only; the deployed proof has not run**. These files are not
connected to the application or an existing Gateway. The user identified the
`cei-project` profile in `us-east-1` as the development environment and requested
that deployment not be performed from the CLI. Do not deploy the test target to a
shared Gateway.

## What this tests

The request interceptor receives a Gateway token with `passRequestHeaders=true`,
verifies it using the existing Cognito access-token verifier and validates the
`cei_user_sub`, `cei_session_id`, and configured source scope. For a `tools/call`,
it overwrites `_cei_spike_identity` in the parsed request's tool arguments. The
isolated target reports whether that field was actually delivered. It deliberately
does not access Secrets Manager, and its event is **not** a trusted production
identity channel. Never attach either function to a production Gateway or target.

## Deployment and evidence required

Use the isolated `IdentitySpikeStack` and the repository's CodeBuild asset build;
the stack has not yet been deployed. It defines a temporary Cognito pool/client
and pre-token trigger, CUSTOM_JWT Gateway with a REQUEST interceptor, and one
Lambda target with an input schema for `query` and the reserved context field.
The Gateway role can invoke only the spike interceptor and target. Test the
separate probe caller role's explicit direct-invocation denial. Verify that other
application/runtime principals cannot directly invoke the target. Inspect the deployed
policies and test direct invocation with representative application credentials.
Do not log JWTs, keys, or request bodies. Remove the temporary resources after the
recorded tests.

Use real, correctly signed scoped tokens representing two distinct users and
sessions. Record sanitized results and deployment/version identifiers for:

1. Both users' tokens: observe whether the target receives the correct injected
   identity, including whether the Gateway's tool schema accepts the field.
2. A forged `_cei_spike_identity` tool argument, including mismatched user/session:
   the target must receive the verified token's values, never the forged values.
3. Invalid, expired, wrong-client, wrong-issuer, missing-claim, and wrong-scope
   tokens: no target execution. Test token refresh preserves the binding.
4. Direct target invocation by agent/runtime and ordinary user principals: denied.
   Confirm the Gateway cannot invoke a different unprotected path to the target.
5. Concurrency: interleave calls for the two users and verify no cross-user identity
   reuse. Check interceptor failures and retries do not accidentally pass through.

If any item fails or cannot be observed, B99 remains unresolved. An authenticated
context in tool arguments is only acceptable if the interceptor cannot be bypassed
and always overwrites caller-supplied values. Record limitations and present a
concrete design for user approval before changing the real target resolver or
granting it access to user secrets.

## Local test

From the repository root, run with the agent Python environment and
`PYTHONPATH=backend/agents`: `python -m pytest backend/lambdas/spikes/b99_identity/test_interceptor.py -q`.
The test stubs the verifier; signature, issuer, client ID, and expiry validation
are provided by the existing verifier and must also be checked in the live spike.
