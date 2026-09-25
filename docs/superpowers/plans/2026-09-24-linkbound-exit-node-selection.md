# LinkBound Exit Node Selection Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans on the isolated `codex/linkbound-exit-node-selection` branch.

**Goal:** Every hosted LinkedIn browser task uses a selected, verified Tailscale exit node. A missing or failed route blocks Chrome before navigation.

**Architecture:** The existing one-browser coordinator serializes work. A narrow egress module reads approved nodes, selects one through Tailscale's Linux operator permission for the existing non-root service user, verifies the route and public IP, and clears it after the browser closes. SQLite stores the default; existing campaign run options store overrides. The agent VPS is outside scope.

## Task 1: Route lease and runner gate

Files: `app/egress.py`, `app/settings.py`, `app/runner.py`, `tests/test_egress.py`.

1. Test the missing-node, offline-node, route-mismatch, public-IP-failure, and cleanup paths. Run the focused test and confirm it fails for the missing feature.
2. Implement the route lease and gate before Playwright starts. Local laptop runs retain the existing behavior when the hosted requirement is disabled.
3. Rerun focused tests and commit.

## Task 2: Task selection and durable default

Files: `app/db.py`, `app/models.py`, `app/coordinator.py`, `app/orchestrator.py`, `app/inbox_sync.py`, `app/queue_worker.py`, `app/inbound_schedule.py`, `tests/test_egress.py` and focused existing tests.

1. Write failing tests for default selection, manual override, queued choice and change, and inbound pause without a node.
2. Persist the default and pass the selected ID through every browser entrypoint. A running task keeps its pinned route.
3. Rerun focused tests and commit.

## Task 3: UI and hosted helper

Files: `app/main.py`, `static/index.html`, `static/app.js`, `static/styles.css`, `deploy/*`, API tests.

1. Write failing API tests for node inventory, invalid/default selection, and campaign edits.
2. Add visible selectors and a blocked state. Configure `tailscale set --operator=linkbound` on LinkBound only, preserving the service's `NoNewPrivileges=true` setting.
3. Run API/UI checks and a non-root route-selection pilot, then commit.

## Task 4: Verification and pilot

1. Run all tests, syntax checks, and diff review.
2. Deploy with hosted sends and inbound schedule still disabled. Verify no-node dry runs stop before Chrome opens while app, viewer, and SSH remain reachable.
3. After a laptop exit node is approved, run a no-send pilot and verify selected node, observed public IP, route continuity, and offline pause.

Review focus: direct `LinkedInRunner.start()` cannot bypass the gate; unread recovery shares the chosen node; offline routes never fall back to Linode egress; no changes to the agent VPS.
