# MVP acceptance walkthrough

Use [`fixtures/app-01-architecture.txt`](../fixtures/app-01-architecture.txt), a sanitized IT-architecture fixture containing `Customer Portal runs on APP-01`.

1. Start Compose, verify health, sign in, create and activate a KB.
2. Upload the fixture and wait for the worker to mark it completed.
3. Query for APP-01 and verify the returned source citation.
4. Create a KB/tool-scoped token; initialize MCP, list tools and call `search_knowledge`.
5. Revoke/disable the token via the future lifecycle UI/API and verify MCP denial.

## Legal registry and temporal retrieval walkthrough

Use [`fixtures/legal/`](../fixtures/legal/): a sanitized fictional labour-protection act (`act-2541.md`), its amendment (`act-amendment-2562.md`, which amends มาตรา 15), a ministerial notification (`notification-2563.md`) and its amendment (`notification-2566.md`, which repeals its own ข้อ 5), and a FAQ (`faq.md`) that quotes the original มาตรา 15 wording as a similarity trap. `apps/api/tests/test_legal_acceptance.py` automates this walkthrough end to end (upload → process → set legal metadata → rebuild → approve → query); the same steps apply manually against a running Compose stack once `POST /documents/{id}/legal-extract` has populated real metadata via OpenRouter.

1. Upload all five fixtures as `legal` (act, amendment), `regulation` (both notifications), and `general` (FAQ); wait for each to complete.
2. Extract legal metadata for the four legal/regulation documents, review the suggested `AMENDS`/`ISSUED_UNDER`/`REPEALS` relationships in Explore Graph, and approve each one.
3. Ask "มาตรา 15 กำหนดอัตราค่าชดเชยไว้อย่างไร" (no date filter). The amendment's มาตรา 15 (เก้าสิบวัน) must appear; the original act's มาตรา 15 (หกสิบวัน) must not, and the response carries a `SUPERSEDED_VERSION_REMOVED` or `PROVISION_AMENDED` warning. The FAQ, if it appears at all, ranks below the amendment's citation.
4. Repeat the same question with `filters.as_of_date` set to a date before 2019-05-01. The original 1998 act's มาตรา 15 must appear instead, and the amendment must not.
5. Ask "ข้อ 5 ของประกาศแจ้งอัตราค่าชดเชยก่อนกี่วัน". The 2566 notification's ข้อ 5 (สิบห้าวัน) must appear; the 2563 notification's ข้อ 5 (เจ็ดวัน) must not, and the response carries a `SUPERSEDED_VERSION_REMOVED` warning.
6. Before approving the `AMENDS`/`REPEALS` suggestions in step 2, repeat step 3's query: both versions must appear together with no warnings — an unreviewed suggestion has no retrieval effect.
7. Confirm a Knowledge Base with no legal documents (e.g. the APP-01 fixture above) behaves identically to before this feature: no `legal_context` in the retrieval trace, no `warnings`, no `legal_label` on any source.
