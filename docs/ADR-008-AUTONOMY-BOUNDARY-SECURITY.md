# ADR-008: Autonomy Boundary Security Design

**Status:** Adopted  
**Date:** 2026-09-07  
**Scope:** `POST /v1/autonomy/decisions/evaluate`, `GET /v1/autonomy/decisions/history`

---

## Context

The Aftergraph platform exposes an Autonomy Boundary evaluator that decides whether
a proposed change may be auto-approved, must be manually reviewed, or is blocked.
Earlier security work (weak-1 through weak-7) addressed seven concrete weaknesses
in the evaluator's trust assumptions. This ADR records the resulting security
posture so future changes do not regress it.

The evaluator is **fail-closed**: any ambiguity, error, or missing signal must
trend toward `blocked` or `manual_review`, never toward `auto_approve`.

## Trust Boundaries

### Caller-declared flags are untrusted

The evaluator does not trust caller-supplied boolean flags (e.g. a hypothetical
`is_security_change: false`). Instead it independently scans `changed_files` for
signals:

- `security/` path prefix
- `auth*` filename glob
- `proxy/` and `ssl` substrings
- other critical path patterns defined in the signal module

Caller flags may supplement but never override this scan.

### Authentication: two paths

1. **Bearer token** — standard `Authorization: Bearer <token>` header, validated
   by the shared auth dependency.
2. **Webhook HMAC-SHA256** — `X-Hub-Signature-256` header in GitHub style
   (`sha256=<hex>`) or raw hex. Verified with `hmac.compare_digest` against
   `AFTERGRAPH_WEBHOOK_SECRET`.

### ProductionSecurityMiddleware is the real boundary

`ProductionSecurityMiddleware` in `secure_api.py` is the production auth
boundary. Patches to core `api.py` alone are **insufficient** — `secure_api.py`
wraps the app with additional middleware that is what actually runs in
production. Any auth change must be verified against both files.

### Webhook verification deferred to handler

Webhook HMAC verification happens inside the endpoint handler, not in the auth
dependency. Reason: calling `await request.body()` inside a FastAPI dependency
races the framework's own body parsing and can produce empty reads or double
consumption. Deferring to the handler is the pragmatic, correct choice.

## Fail-Closed Decision Table

The following signals **always** force `decision=blocked` or
`decision=manual_review`, regardless of other positive signals:

- `auth_or_secret_touched`
- `proxy_or_ssl_touched`
- `critical_file_touched`
- `canary_error_rate > threshold`
- `superseded_head`
- `stale_review`

No combination of low-risk signals can override these.

**Confidence cap:** confidence scores use sublinear weighting and are hard-capped
at **80**. Even a theoretically perfect low-risk patch never reaches 100. This
prevents any downstream consumer from treating an evaluator score as certainty.

## Rate Limiting

| Scope | Default | Env override | Key |
|-------|---------|--------------|-----|
| `/v1/autonomy/decisions/evaluate` | 20 req/min | `AFTERGRAPH_AUTONOMY_RATE_LIMIT` | `autonomy:<method>` |
| Global | 60 req/min | `AFTERGRAPH_RATE_LIMIT` | per auth identity |

The per-endpoint key is namespaced by auth method (`autonomy:bearer`,
`autonomy:webhook`) so a busy webhook source cannot starve bearer-token callers.

## Operations

- **Secret location:** `/etc/aftergraph/work-intelligence.env` on the VDS.
- **Secret reload:** `systemctl daemon-reload && systemctl restart work-intelligence`.
- **Leak response:** rotate immediately with `secrets.token_hex(32)`, then rewrite
  public git history with BFG Repo-Cleaner to remove the leaked value.
- **Production config:** port `8090` bound on `172.17.0.1` (Docker bridge),
  systemd unit `work-intelligence`.
- **Deploy:** `scripts/deploy-production-vds.sh`.

## Threat Model Gaps (Future Work)

These are honestly documented as open items, not hidden:

- No formal threat model review has been conducted on the evaluator.
- The evaluate endpoint is **read-only** (no execution side effects) — this is
  a strong natural mitigation and should be preserved.
- No per-tenant isolation is documented beyond rate-limit key namespacing.
- The HMAC webhook secret is shared across all tenants (single secret, not
  per-tenant).

Future ADRs should address these gaps before the evaluator gains any
write-side-effect capability.

## Decision

**Adopted.** The fail-closed posture, confidence cap, dual-auth with deferred
HMAC verification, and per-endpoint rate limiting described above constitute the
current security contract for the Autonomy Boundary evaluator. Any change that
weakens a fail-closed rule or raises the confidence cap requires a new ADR.

---

See also: [docs/ARCHITECTURE.md](ARCHITECTURE.md)
