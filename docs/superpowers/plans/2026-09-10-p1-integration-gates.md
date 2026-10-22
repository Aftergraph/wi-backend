# P1 Integration Gates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `Aftergraph/wi-backend/main` enforce the stable CI/security checks already proven on pull requests while remaining compatible with the existing GitHub merge queue.

**Architecture:** Keep the existing classic branch protection and repository merge-queue ruleset. Add `merge_group` triggers to the workflows that emit required checks, cover that contract with a repository-hygiene regression test, merge the code change normally, then enable required contexts and admin enforcement through GitHub branch protection.

**Tech Stack:** GitHub Actions YAML, Python/pytest repository-hygiene tests, GitHub REST API/`gh`.

**Spec:** Aftergraph/wi-backend#18 P1 protected integration gates and GitHub merge-queue requirements.

## Global Constraints

- Preserve all existing workflow triggers, permissions, job names, matrices, and security settings.
- Required contexts are exactly `test (3.11)`, `test (3.12)`, `production-container-smoke`, and `Analyze (python)`.
- Both workflows that emit required contexts must run for `merge_group` `checks_requested`.
- Do not weaken CODEOWNERS, review, conversation-resolution, non-fast-forward, merge-queue, secret-scanning, or push-protection controls.
- `Aftergraph/wi-frontend` remains private; do not change visibility to obtain branch protection.
- Never print, read, or reproduce secret values.

---

### Task 1: Merge-queue workflow compatibility

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/codeql.yml`
- Test: `tests/test_repository_hygiene.py` or a new focused `tests/test_merge_queue_workflows.py`

- [ ] Add `merge_group: {types: [checks_requested]}` (valid YAML form) to CI without changing existing push/pull_request behavior.
- [ ] Add the same merge-group trigger to CodeQL without changing existing push/pull_request/schedule behavior.
- [ ] Add a regression test proving both workflow files retain `pull_request` and `merge_group` support and that expected job/check names remain stable.
- [ ] Run the focused regression test and repository-hygiene tests.
- [ ] Run Ruff on changed Python tests.
- [ ] Run the full pytest suite before creating the PR.
- [ ] Commit the isolated change.

### Task 2: Review, PR, and fresh CI

- [ ] Review the diff for trigger/permission regressions.
- [ ] Push only the isolated feature branch and open a PR against current `main`.
- [ ] Require fresh green `test (3.11)`, `test (3.12)`, `production-container-smoke`, and `Analyze (python)` on the PR head.
- [ ] Merge through the normal repository path only after checks are green.

### Task 3: Enforce backend protection and reconcile ledger

- [ ] Set classic branch protection required checks to the four exact contexts with strict/up-to-date behavior.
- [ ] Enable administrator enforcement while preserving existing 1-review, stale-review dismissal, CODEOWNER review, conversation resolution, no-force-push, and no-deletion controls.
- [ ] Verify the repository rulesets remain active and merge queue remains configured.
- [ ] Verify a post-change PR/merge-group run reports the required checks.
- [ ] Update #18 backend P1 checkbox with evidence; keep `Aftergraph/wi-frontend#21` open as the plan-limited web blocker.
