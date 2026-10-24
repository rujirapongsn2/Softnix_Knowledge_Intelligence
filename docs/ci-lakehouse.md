# SKI × Softnix Data Lakehouse CI

SKI follows the same corporate identity as EDC1 / Servio. Tokens live in
`apps/web/src/lakehouse-ci.css` (loaded last) and `apps/web/src/kumo.css` (@theme).

| Token | Value | Use |
|---|---|---|
| `--lh-primary-fill` | `#3a6f9e` (hover `#325f88`) | Primary button fill, white text |
| `--lh-primary` | `#4582b8` | Accent, links, focus ring, graph nodes |
| `--lh-ink` / `--lh-ink-2` / `--lh-ink-3` | `#101b2a` / `#566377` / `#5f6b7a` | Text (all ≥ 4.5:1 on white and `#f9fafb`) |
| `--lh-line` / `--lh-line-strong` | `#e2e5ea` / `#d3d8e0` | Borders |
| `--lh-bg` | `#f9fafb` | Page / table header background |
| ok / warn / bad | `#3e9f4d` `#d0761f` `#e74341` (text `#2f7a3b` `#9a5a14` `#c23432`) | Status dots, badges |

- Fonts: Inter + Noto Sans Thai (+ JetBrains Mono for code), Google Fonts in `index.html`.
- Radius: 6px controls, 12px cards. Shadow `0 1px 2px rgba(16,27,42,.04)`.
- Lockup: Softnix mark + "Knowledge Intelligence" + "by Softnix".
- Floors: no text under 11px; text contrast ≥ 4.5:1 (3:1 for ≥ 24px). No side-stripe borders over 1px, no decorative gradients, no eyebrows above page titles.
- Admin-only (role `admin`, enforced by the API with `require_admin`): KB create / rename / disable / activate / delete, Retrieval Policy, Reindex embeddings, MCP Tokens, Ingest API. Manager+: system status and logs. Admin: users and groups.
