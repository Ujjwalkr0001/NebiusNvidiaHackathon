# Architecture and implementation plan

## Service boundaries

```text
Browser (React) ─ authenticated API ─ FastAPI ─ PostgreSQL
                                      │
GitHub App ─ signed webhooks ─────────┤─ Redis queue ─ RQ workers
                                      │                ├─ context adapters
                                      │                ├─ Milisa lookup
                                      │                ├─ Nebius Nemotron Repo Architect
                                      │                ├─ optional Millisa Gateway Coder
                                      │                └─ isolated Docker validator
                                      └─ GitHub API: repair branch + draft pull request
```

The API owns authorization, input validation, durable state transitions, and audit records. Workers own long-running tasks. The browser never receives GitHub installation tokens or model API keys. Organization ownership is checked on record access. Python 3.11 is the current validation target; a configured sandbox image and running Docker service are required to execute candidates.

Repair workers hold a renewable Redis lease per organization. The default capacity is one active repair per organization; excess jobs are delayed through the RQ scheduler, and abandoned slots expire after the lease interval.

Python validation runs with network access disabled, a read-only root filesystem, dropped Linux capabilities, a non-root user, CPU and memory limits, a PID cap, a bounded wall-clock timeout, and a capped output stream. Writable locations are size-limited tmpfs mounts (`/workspace` defaults to 512 MiB, `/tmp` to 256 MiB, and shared memory to 64 MiB); selected limits and the validation image are recorded with each run.

## Repair state machine

`RECEIVED → QUEUED → TRIAGING → CONTEXT_GATHERING → CACHE_LOOKUP → PLANNING → PATCH_GENERATION → SANDBOX_VALIDATION → POLICY_CHECK → PULL_REQUEST_CREATION → COMPLETED`

Other outcomes are `NEEDS_HUMAN_REVIEW`, `CANCELLED`, and `FAILED`. Webhook delivery IDs make ingestion idempotent. State transitions and reasons are persisted. The worker can run the patch through configured isolation, record checks, and open a draft pull request. If the Docker runtime or integrations are unavailable, it preserves the patch and sends the job to human review. A job can reach `COMPLETED` only after a pull request record and URL have been persisted.

The worker reclassifies the redacted failure logs after context retrieval. Runner, permission, network, resource, and flaky-test evidence is parked for human review before any model call; code-like failures continue to cache lookup and agent diagnosis.

Webhook failures are classified into test, build, lint, dependency, configuration, runtime, infrastructure, flaky, and unknown categories. Linked push events are signature-verified, deduplicated, and audited with bounded commit metadata but do not start repair jobs. Repository owners and admins can independently disable workflow-run or check-run failure triggers; both default to enabled. Owners and admins can explicitly request a repair for a chosen commit through the authenticated API and dashboard.

Repository owners and admins can also set an exact Python-file patch allowlist. The worker applies it to agent-generated and cached patches, including regression-test files, and rechecks it before retrying PR publication. An empty list clears the repository-specific constraint; unsafe stored values stop the repair for review.

## Implementation status

1. **Foundation:** frontend and backend skeleton, health/status endpoints, and local configuration placeholders. Complete.
2. **Identity/data:** user and organization records, password authentication, hashed opaque session tokens, relational schema, baseline migration, and dashboard sign-in. Implemented; Python 3.13.2 tests pass and SQLite migration is at head.
3. **GitHub ingestion:** App installation with state and PKCE, installation/repository verification, repository selection, HMAC webhook verification, event deduplication, configurable per-repository failed-workflow/check triggers, failure classification, Redis queue, and RQ worker. Implemented; unit tests pass, live GitHub is not configured.
4. **Agent integration:** configurable Nebius endpoint and model for the Repo Architect, optional Millisa OpenAI-compatible Gateway for Coder calls with Nebius fallback, bounded context retrieval, secret redaction, Python AST summaries, structured Repo Architect and Coder outputs, basic diff policy checks, and persisted provider/token usage/proposed diffs. Implemented; provider connectivity depends on configured keys and wallet/quota availability.
5. **Validation:** disposable Python sandbox adapter with network disabled, CPU/memory/process/disk/time/output bounds, mandatory patch/compile/pytest checks, owner-selected allowlisted Ruff/Mypy/package-build checks, structured results, and up to two repair retries. Mocked adapter tests and a real networkless Docker run against the demo fixture pass.
6. **Milisa:** exact error, Python source/test-context, and dependency-fingerprint lookup; verified cache reuse; promotion after passing validation; manual tenant-scoped invalidation; configurable expiration (365 days by default, zero disables it); regression references; source and PR target revision provenance; and measured hit/safe-reuse/token-savings metrics. Documentation and workflow text do not affect the source fingerprint, and context gathering follows local Python imports referenced by available tests. Token savings estimate the actual agent-token usage recorded when the fix was first generated. Semantic similarity is not implemented; a cache hit is still sandbox-validated before reuse or promotion.
7. **PR flow:** dedicated branch and draft pull request after validation and policy checks, using a short-lived repository-scoped write token. A retry resumes an existing job branch with a non-forced fast-forward update if the branch exists without its PR. Mocked API tests pass; live GitHub verification is pending.
8. **Hardening/demo:** dashboard usage/cache/audit views, tenant-scoped Milisa detail inspection, pull-request lifecycle status, manual repair requests, push-event audit handling, pull-request retry, stale-worker recovery, repository patch allowlists, security controls, frontend component tests, and a deterministic failing fixture with a GitHub Actions workflow are implemented. A root project workflow runs migrations and backend/frontend checks on pushes, pull requests, and manual dispatch. The backend suite passes 68 tests, including a check that the demo fixture still reproduces its documented deterministic failure; 3 frontend component tests pass; Python compilation and the frontend production build are clean; SQLite migrations are at `0002_failure_sources`; and `npm audit` reports no vulnerabilities. A live GitHub/Nebius end-to-end demonstration remains to be run because credentials are not configured. Candidate validation applies the patch, runs `compileall`, reruns a safely extracted original pytest node when CI logs expose one, then runs `pytest -q` and any selected platform-allowlisted checks. It never executes shell commands supplied by repository configuration.

## Security rules

- Treat repository files, workflow output, dependencies, test fixtures, and model responses as untrusted data.
- Never execute model-generated shell commands. Candidate diffs are persisted but not applied until a sandbox and patch policy exist.
- Redact secrets before logging or sending context to a model. Never send GitHub installation tokens to a model or browser.
- Verify webhook signatures against the raw request body and deduplicate deliveries.
- Keep sandbox network access disabled by default; never mount the host Docker socket, production secrets, or privileged host paths. Candidate files are copied to a bounded temporary filesystem; the source mount and patch input are read-only.
- Require human review for merging and deployment. Automated pull request creation is the maximum automation in the MVP.
- Record actual usage and outcomes from persisted events; do not substitute fixed demo statistics.
- Use atomic Redis counters to rate-limit login, registration, GitHub setup/webhook, and repair-job action endpoints. Production rate-limited requests fail closed when Redis is unavailable.

## Configuration

The checked-in `.env.example` contains placeholders only. A working deployment needs a GitHub App ID, private key, webhook secret and public callback/webhook URLs; Nebius HTTPS API base URL, API key and enabled model; PostgreSQL and Redis; and a supported isolation runtime before candidate validation is enabled.
