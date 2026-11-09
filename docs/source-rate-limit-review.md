# Data-source rate limits: review

Reviewed 2026-11-08 against provider documentation. The common retry helper retries 429, selected 5xx responses and network failures with jitter and a four-second maximum delay. It does **not** impose a shared rate limit across Lambda instances, so retries do not guarantee compliance with provider quotas. Add a shared per-provider limiter or other verified control before production traffic; per-process sleeps alone cannot enforce an account/IP-wide quota.

| Provider | Published guidance | Source |
|---|---|---|
| NCBI E-utilities (PubMed) | Three requests/second without a key; ten per key by default. | [NCBI E-utilities guide](https://www.ncbi.nlm.nih.gov/books/NBK25497/) |
| PubChem PUG-REST | At most five requests/second for a user, application or organization; dynamic throttling also applies. | [PubChem PUG-REST documentation](https://pubchem.ncbi.nlm.nih.gov/pcfe/docs/markdown/pug-rest.md) |
| openFDA | The published page lists 240 requests/minute and 1,000/day without a key, 240/minute and 120,000/day with one. The same page also says a key is required; confirm current access policy before rollout. Each tool call can make three upstream requests. | [openFDA authentication](https://open.fda.gov/apis/authentication/) |
| CMS Coverage API | The provider FAQ states a 10,000 requests/second service throttling limit; some endpoints require a license token. | [CMS Coverage API](https://api.coverage.cms.gov/) |
| PatentsView | A key is required by the existing tool. Confirm the current per-key quota and provision an approved server-side key before treating the deployed source as operational. | [PatentsView API](https://search.patentsview.org/docs/) |

The existing source inventory also includes ClinicalTrials.gov, bioRxiv, medRxiv, ChEMBL, Open Targets, ICD-10 and NPI Registry. Their numeric limits were not verified in this review. Confirm their current published guidance before setting production traffic limits; do not infer unlimited access from an undocumented limit.

## Prerequisites for aggregate enforcement

A limiter at the Lambda handler boundary is insufficient: a single tool invocation can make several upstream requests, CMS makes detail requests concurrently, and the retry helper can repeat each request. Limits must be checked immediately before **each** actual outbound request, including retries, across every Lambda instance and every caller that shares the provider quota. Local process state and Lambda reserved concurrency cannot enforce a request rate or a daily quota.

Before implementing this safely:

1. Verify the current rate, window, burst rules and quota identity (for example organization, egress IP or API key) for each provider and credential mode. Clarify any undocumented policy with the provider. Configure these values explicitly; do not infer them from retry settings.
2. Identify all applications and deployments sharing each quota. A table in this stack cannot enforce an organization-wide limit if other applications or accounts bypass it. Route all relevant outbound requests through the shared control, or allocate conservative, agreed portions of each shared quota to each independently controlled client.
3. Design and approve a shared, atomic admission store and its deployment/IAM and operational ownership. It must cover every relevant time window, use opaque non-secret quota identifiers, and avoid logging keys or user identity. Consider how credential rotation changes the quota identity.
4. Integrate admission into `retrying_http.request` before each `requests.request` attempt. Propagate a bounded, typed rate-limit/unavailable outcome; fail closed when required configuration or the shared store is unavailable. Define a maximum wait that fits the Lambda timeout and cancellation behavior.
5. Test simultaneous callers and retry attempts across instances, every configured window, clock and boundary behavior, store failures, credential isolation, and the absence of secret material from keys/logs. Validate the resulting outbound rate under a controlled deployed test before enabling live traffic.

No aggregate limiter is included yet: the unverified provider rules and shared-quota boundaries prevent selecting correct limits and keys. The existing retry helper remains backoff only, and must not be treated as quota enforcement.
