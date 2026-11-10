# NCBI target primitives

These are isolated functions, not deployed Lambda handlers or Gateway targets.
They validate inputs, call fixed NCBI endpoints, cap response bytes, and require
a rate limiter callback on every outbound operation. No direct mode or identity
transport has been integrated.

Before deployment, provision a DynamoDB table for `limiter.NCBIRateLimiter` and
ensure every NCBI caller in the deployment uses it. The helper uses transactions
to enforce shared global and per-RID slots, but has not been tested against a
deployed table or integrated with existing PubMed/E-utilities calls. NCBI's BLAST guidance requires at least 10 seconds between
server requests and at least 60 seconds between polls for the same RID; E-utilities
limits also apply across callers. Configure the service contact email. Do not
retry a throttled request outside the limiter. Sources: [BLAST guidelines](https://blast.ncbi.nlm.nih.gov/doc/blast-help/developerinfo.html),
[BLAST API](https://blast.ncbi.nlm.nih.gov/doc/blast-help/urlapi.html).

Build separate target handlers and schemas, bind all three targets to the
`cei-tools/src.ncbi` scope, enforce ownership of submitted BLAST RIDs, and add
the asynchronous submit/poll loop. Include the scope in specialist tokens and
the NCBI profile while maintaining source isolation. Add per-user NCBI keys only
after B99's identity transport has been proven and approved. Finally verify real
NCBI response formats, error cases, caller isolation and rate limits in an
identified development environment; none of these have been verified live here.
