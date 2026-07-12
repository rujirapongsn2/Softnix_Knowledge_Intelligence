# MVP acceptance walkthrough

Use [`fixtures/app-01-architecture.txt`](../fixtures/app-01-architecture.txt), a sanitized IT-architecture fixture containing `Customer Portal runs on APP-01`.

1. Start Compose, verify health, sign in, create and activate a KB.
2. Upload the fixture and wait for the worker to mark it completed.
3. Query for APP-01 and verify the returned source citation.
4. Create a KB/tool-scoped token; initialize MCP, list tools and call `search_knowledge`.
5. Revoke/disable the token via the future lifecycle UI/API and verify MCP denial.
