import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';

function jsonResponse(body: unknown, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

describe('dashboard authentication', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v1/auth/me')) return jsonResponse({ detail: 'Not signed in' }, 401);
      if (url.endsWith('/api/v1/status')) return jsonResponse({
        service: 'Self-Healing CI/CD API', milestone: '8', integrations_configured: false,
        repair_jobs_enabled: false, github_app_configured: false,
        github_webhook_configured: false, nebius_configured: false, validation_configured: false,
      });
      return jsonResponse([]);
    }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('shows server login errors accessibly and keeps the form available', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v1/auth/me')) return jsonResponse({ detail: 'Not signed in' }, 401);
      if (url.endsWith('/api/v1/status')) return jsonResponse({
        service: 'Self-Healing CI/CD API', milestone: '8', integrations_configured: false,
        repair_jobs_enabled: false, github_app_configured: false,
        github_webhook_configured: false, nebius_configured: false, validation_configured: false,
      });
      if (url.endsWith('/api/v1/auth/login') && init?.method === 'POST') {
        return jsonResponse({ detail: 'Invalid email or password' }, 401);
      }
      return jsonResponse([]);
    });

    render(<App />);
    await screen.findByRole('heading', { name: 'Welcome back' });
    await user.type(screen.getByLabelText('Email'), 'dev@example.com');
    await user.type(screen.getByLabelText('Password'), 'incorrect-password');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid email or password');
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeEnabled();
  });

  it('registers a workspace and loads the authenticated dashboard', async () => {
    const user = userEvent.setup();
    let authenticated = false;
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v1/auth/me')) {
        return authenticated
          ? jsonResponse({ display_name: 'Ada Developer', organizations: [{ id: 'org-1', name: 'Repair Lab', role: 'owner' }] })
          : jsonResponse({ detail: 'Not signed in' }, 401);
      }
      if (url.endsWith('/api/v1/status')) return jsonResponse({
        service: 'Self-Healing CI/CD API', milestone: '8', integrations_configured: false,
        repair_jobs_enabled: false, github_app_configured: false,
        github_webhook_configured: false, nebius_configured: false, validation_configured: false,
      });
      if (url.endsWith('/api/v1/auth/register') && init?.method === 'POST') {
        authenticated = true;
        return jsonResponse({ id: 'user-1' }, 201);
      }
      if (url.endsWith('/api/v1/dashboard/overview')) return jsonResponse({
        connected_repositories: 0, monitored_repositories: 0, failures_detected: 0,
        active_repairs: 0, validated_repairs: 0, pull_requests_opened: 0,
        needs_human_review: 0, cache_hit_rate: null, safe_cache_reuse_rate: null,
        validation_success_rate: null, model_tokens: 0, estimated_tokens_saved: 0,
        estimated_cost: null, average_pr_minutes: null, recent_jobs: [],
      });
      if (url.endsWith('/api/v1/model/config')) return jsonResponse({
        provider: 'Nebius Token Factory', configured: false, model_id: null,
        endpoint_host: null, input_price_per_million: null, output_price_per_million: null,
      });
      return jsonResponse([]);
    });

    render(<App />);
    await screen.findByRole('heading', { name: 'Welcome back' });
    await user.click(screen.getByRole('button', { name: 'New here? Create a workspace' }));
    await screen.findByRole('heading', { name: 'Create your workspace' });
    await user.type(screen.getByLabelText('Display name'), 'Ada Developer');
    await user.type(screen.getByLabelText('Organization name'), 'Repair Lab');
    await user.type(screen.getByLabelText('Email'), 'ada@example.com');
    await user.type(screen.getByLabelText(/Password/), 'correct-horse-battery');
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    expect(await screen.findByRole('heading', { name: /Good to see you, Ada/ })).toBeInTheDocument();
    expect(await screen.findByText('No repair jobs have been recorded yet.')).toBeInTheDocument();
  });

  it('shows persisted pull-request status and lets an owner inspect a verified cache patch', async () => {
    const user = userEvent.setup();
    const pattern = {
      id: 'pattern-1', failure_category: 'test', language: 'python', framework: 'pytest',
      root_cause: 'An incorrect value caused the assertion failure.',
      verification_status: 'VERIFIED', success_count: 1, reuse_count: 3, last_used_at: null,
    };
    const job = {
      id: 'job-1', repository: 'org/demo', status: 'COMPLETED', failure_category: 'test',
      failure_summary: 'AssertionError: expected fixed behavior', commit_sha: 'a'.repeat(40),
      created_at: '2026-10-29T10:00:00Z',
    };
    const jobDetail = {
      ...job, base_branch: 'main', workflow_run_id: null, check_run_id: null, retry_count: 0,
      pr_url: 'https://github.com/org/demo/pull/12', error_message: null,
      pull_request: { number: 12, url: 'https://github.com/org/demo/pull/12', branch_name: 'patchwork/repair-1', status: 'MERGED', created_at: '2026-10-29T10:10:00Z' },
      events: [{ from_state: 'POLICY_CHECK', to_state: 'PULL_REQUEST_CREATION', actor: 'worker', detail: {}, created_at: '2026-10-29T10:09:00Z' }],
      agent_runs: [], patches: [], validation_runs: [],
    };
    const patternDetail = {
      ...pattern, patch_template: 'diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new',
      regression_reference: 'tests/test_app.py::test_value', version_constraints: { python_version: '3.13' },
      context_fingerprint: 'c'.repeat(64), error_signature: 'e'.repeat(64),
      validation_metadata: { status: 'PASSED', checks: [{ name: 'Pytest suite', outcome: 'PASSED' }], environment: { network: 'disabled' } },
      source_revision: 'a'.repeat(40), target_revision: 'b'.repeat(40),
      created_at: '2026-10-29T10:00:00Z', expires_at: null,
    };
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v1/auth/me')) return jsonResponse({ display_name: 'Ada Developer', organizations: [{ id: 'org-1', name: 'Repair Lab', role: 'owner' }] });
      if (url.endsWith('/api/v1/status')) return jsonResponse({
        service: 'Self-Healing CI/CD API', milestone: '8', integrations_configured: false,
        repair_jobs_enabled: false, github_app_configured: false,
        github_webhook_configured: false, nebius_configured: false, validation_configured: false,
      });
      if (url.endsWith('/api/v1/dashboard/overview')) return jsonResponse({
        connected_repositories: 0, monitored_repositories: 0, failures_detected: 1,
        active_repairs: 0, validated_repairs: 1, pull_requests_opened: 1,
        needs_human_review: 0, cache_hit_rate: 100, safe_cache_reuse_rate: 100,
        validation_success_rate: 100, model_tokens: 0, estimated_tokens_saved: 30,
        estimated_cost: null, average_pr_minutes: 10, recent_jobs: [],
      });
      if (url.endsWith('/api/v1/repair-jobs/job-1')) return jsonResponse(jobDetail);
      if (url.endsWith('/api/v1/repair-jobs')) return jsonResponse([job]);
      if (url.endsWith('/api/v1/milisa/patterns/pattern-1')) return jsonResponse(patternDetail);
      if (url.endsWith('/api/v1/milisa/patterns')) return jsonResponse([pattern]);
      if (url.endsWith('/api/v1/model/config')) return jsonResponse({
        provider: 'Nebius Token Factory', configured: false, model_id: null,
        endpoint_host: null, input_price_per_million: null, output_price_per_million: null,
      });
      return jsonResponse([]);
    });

    render(<App />);
    await screen.findByRole('heading', { name: /Good to see you, Ada/ });
    await user.click(await screen.findByRole('button', { name: 'Details' }));
    expect(await screen.findByRole('heading', { name: /test · python/ })).toBeInTheDocument();
    expect(screen.getByText('Verified patch')).toBeInTheDocument();
    expect(screen.getByText('tests/test_app.py::test_value')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /org\/demo/ }));
    expect(await screen.findByText('Pull request #12 · merged')).toBeInTheDocument();
  });
});
