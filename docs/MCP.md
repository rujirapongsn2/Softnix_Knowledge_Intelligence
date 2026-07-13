# MCP connection

Use Streamable HTTP compatible JSON-RPC at `POST /mcp` with `Authorization: Bearer skik_live_…`. Tool discovery returns only token-approved tools. Supported tools are `search_knowledge`, `find_entities`, `analyze_relationships`, `analyze_impact`, and `get_sources`.

The token secret is returned exactly once by `POST /api/v1/tokens`; only an HMAC-SHA-256 digest is stored. Calls return a text content block plus `structuredContent` with evidence and citations.

Each token enforces its own knowledge-base/tool scopes, expiry, requests-per-minute, concurrent-request and query-timeout limits. The Knowledge Base scope stored on the token is authoritative: MCP client-supplied `knowledge_base_ids` are ignored for retrieval tools, so a client cannot broaden or redirect a token's access. A scoped token searches its active scoped Knowledge Bases; legacy unscoped tokens search active Knowledge Bases only. Rejected MCP calls return a JSON-RPC `error` object with a stable code such as `MCP_RATE_LIMITED`, `MCP_CONCURRENCY_LIMITED`, `MCP_TIMEOUT`, `AUTH_TOOL_NOT_ALLOWED`, or `KNOWLEDGE_BASE_INACTIVE`.

## Claude Code

Create a scoped token in **Access & MCP**, then on the machine running Claude Code:

```bash
claude mcp add --transport http softnix-knowledge "https://your-softnix-host/mcp" \
  --header "Authorization: Bearer skik_live_..."
```

Run `/mcp` in Claude Code to verify the connection. For a shared `.mcp.json`, use a `${SOFTNIX_MCP_TOKEN}` environment variable in the `Authorization` header and keep the token out of version control.
