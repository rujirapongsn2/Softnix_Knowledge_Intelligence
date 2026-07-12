# Security controls

- Argon2id passwords; secure HTTP-only session cookies.
- Token HMAC digests only; constant-time digest comparison; scoped tools/KBs; expiry and revocation fields; Redis-backed request-rate and concurrency enforcement plus per-token outbound query deadlines.
- Upload extension/MIME policy, streaming file-size limit, randomized storage filename, SHA-256 duplicate guard, outside-webroot storage.
- User-supplied document text is untrusted data. The LightRAG adapter marks instruction-like document text as untrusted evidence and prefixes retrieval prompts with an instruction boundary; it never treats retrieved text as executable instructions.
- Production requires HTTPS, non-public data services, allowlisted CORS origins, masked authorization headers, and no secret commits.
# Audit and metrics

The API persists administrator actions in `audit_logs` without session secrets or
token plaintext. Prometheus-compatible service metrics are exposed at `/metrics`;
place that endpoint behind the same private network or monitoring proxy as the API.
