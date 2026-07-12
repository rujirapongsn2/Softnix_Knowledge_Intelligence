# REST API

Base path: `/api/v1`. Admin APIs use secure HTTP-only login cookies.

- `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`
- `GET|POST /knowledge-bases`, `POST /knowledge-bases/{id}/activate`
- `POST /knowledge-bases/{id}/documents`, `GET /documents/{id}/text`
- `GET /jobs/{id}`
- `POST /query`, `GET /query/results/{id}/sources`
- `GET|POST /knowledge-bases/{id}/documents`, `GET /documents/{id}/text|jobs`, `POST /documents/{id}/reprocess`
- `GET|POST /knowledge-bases/{id}/entities`, `GET /entities/{id}/sources|graph`
- `GET|POST /knowledge-bases/{id}/relationships`, `POST /query/impact`
- `GET /knowledge-bases/{id}/legal-graph?view=verified|suggested|manual|all`
- `POST|GET /knowledge-bases/{id}/legal-graph/rebuild`, `PATCH /relationships/{id}/legal-review`
- `GET|POST /tokens`, `POST /tokens/{id}/enable|disable|revoke`

Errors have `{status:"error", error:{code,message,retryable}}` and never expose stack traces.
