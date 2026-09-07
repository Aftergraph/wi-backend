# Security Policy

## Supported Versions

Only the current `main` head is supported. Security fixes land on `main`
and are deployed from exact-SHA releases.

## Reporting a Vulnerability

Do NOT open a public issue for a suspected vulnerability.

Report privately to the repository owner via a private GitHub security
advisory for this repository. Include:

- affected ref (commit SHA) and component
- steps to reproduce or proof of concept
- assessed impact (confidentiality / integrity / availability)

## Response

- Acknowledgement as capacity allows; this is a small-team project.
- Valid reports are fixed on `main` with a reviewable PR; credit on request.
- Secret leaks (tokens, webhook secrets): rotate first, then report —
  see `scripts/rotate-webhook-secret.sh` for the supported rotation path.

## Scope Notes

- The autonomy evaluator (`/v1/autonomy/decisions/evaluate`) is read-only
  by design (ADR-008); write side effects require a new capability + ADR.
- Production runs the fail-closed boundary (`secure_api.py`); findings
  against the permissive dev entrypoint (`api.py` without middleware)
  are valid only if reachable in the deployed configuration.
