# Screenshot evidence contract — Work Intelligence

Real UI captures from a running build replace the earlier concept render.

## Capture contract (real, 2026-09-06)

- `01-overview.webp` — Dashboard (1280×900, real capture)
- `02-primary-workflow.webp` — Swagger UI / live API docs (1280×900, real capture)
- `03-detail-view.webp` — OpenAPI spec view (1280×900, real capture)
- `04-live-state.webp` — Dashboard mobile viewport (real capture)

Source: `python -m aftergraph_work_intelligence.api` running locally
(exact HEAD 44def8d) with `--port 8811`, captured via headless Chrome.
See `capture-metadata.json` for provenance.

`product-main.webp` was a technical product visual rendered from the
repository architecture — **not a UI screenshot** — and has been replaced by
the real captures above.

Generated or edited mock UI must never be presented as evidence of
implemented behavior.