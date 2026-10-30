import { Activity, ArrowUpRight, Box, Code2, GitBranch, LayoutDashboard, ShieldCheck, Waypoints } from 'lucide-react';
import { useEffect, useState } from 'react';

type ApiStatus = {
  service: string;
  milestone: string;
  integrations_configured: boolean;
  repair_jobs_enabled: boolean;
  github_app_configured: boolean;
  github_webhook_configured: boolean;
  nebius_configured: boolean;
  millisa_configured: boolean;
  validation_configured: boolean;
};
type DashboardData = {
  connected_repositories: number;
  monitored_repositories: number;
  failures_detected: number;
  active_repairs: number;
  validated_repairs: number;
  pull_requests_opened: number;
  needs_human_review: number;
  cache_hit_rate: number | null;
  safe_cache_reuse_rate: number | null;
  validation_success_rate: number | null;
  model_tokens: number;
  estimated_tokens_saved: number;
  estimated_cost: number | null;
  average_pr_minutes: number | null;
  recent_jobs: { id: string; repository: string; status: string; summary: string; created_at: string }[];
};
type RepositoryData = { id: string; full_name: string; private: boolean; monitored: boolean; default_branch: string; triggers: { workflow_run_failure: boolean; check_run_failure: boolean }; required_checks: string[]; allowed_paths: string[] };
type MilisaPattern = { id: string; failure_category: string; language: string; framework: string | null; root_cause: string; verification_status: string; success_count: number; reuse_count: number; last_used_at: string | null };
type MilisaPatternDetail = MilisaPattern & { patch_template: string; regression_reference: string | null; version_constraints: Record<string, unknown>; context_fingerprint: string; error_signature: string; validation_metadata: Record<string, unknown>; source_revision: string | null; target_revision: string | null; created_at: string; expires_at: string | null };
type ModelConfiguration = { provider: string; configured: boolean; model_id: string | null; endpoint_host: string | null; coder_provider?: string; coder_model_id?: string | null; millisa_configured?: boolean; millisa_endpoint_host?: string | null; input_price_per_million: number | null; output_price_per_million: number | null };
type AuditLogRow = { id: string; action: string; entity_type: string; entity_id: string | null; created_at: string };
type ModelUsageRow = { provider: string; model_id: string; prompt_tokens: number; completion_tokens: number; estimated_cost: number | null; created_at: string };
type RepairJobData = { id: string; repository: string; status: string; failure_category: string; failure_summary: string; commit_sha: string; created_at: string; workflow_run_id?: number | null; check_run_id?: number | null };
type RepairJobDetail = RepairJobData & {
  base_branch: string;
  workflow_run_id: number | null;
  check_run_id: number | null;
  retry_count: number;
  pr_url: string | null;
  error_message: string | null;
  pull_request: { number: number; url: string; branch_name: string; status: string; created_at: string } | null;
  events: { from_state: string | null; to_state: string; actor: string; detail: Record<string, unknown>; created_at: string }[];
  agent_runs: { agent: string; status: string; output: Record<string, unknown>; model_id: string | null; created_at: string }[];
  patches: { diff: string; explanation: string; patch_hash: string; created_at: string }[];
  validation_runs: { attempt: number; status: string; environment: Record<string, unknown>; log_excerpt: string; created_at: string; results: { check_name: string; outcome: string; duration_ms: number | null; details: Record<string, unknown> }[] }[];
};

const API = import.meta.env.VITE_API_URL ?? '';
const ACTIVE_REPAIR_STATUSES = new Set([
  'RECEIVED', 'QUEUED', 'TRIAGING', 'CONTEXT_GATHERING', 'CACHE_LOOKUP', 'PLANNING',
  'PATCH_GENERATION', 'SANDBOX_VALIDATION', 'POLICY_CHECK', 'PULL_REQUEST_CREATION', 'RETRYING',
]);
const today = new Intl.DateTimeFormat('en', { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })
  .format(new Date()).toUpperCase();

export default function App() {
  const [apiStatus, setApiStatus] = useState<ApiStatus | null>(null);
  const [apiUnavailable, setApiUnavailable] = useState(false);
  const [overview, setOverview] = useState<DashboardData | null>(null);
  const [authUser, setAuthUser] = useState<{ display_name: string; organizations: { id: string; name: string; role: string }[] } | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [repositories, setRepositories] = useState<RepositoryData[]>([]);
  const [manualRepairRepository, setManualRepairRepository] = useState<RepositoryData | null>(null);
  const [manualRepairPending, setManualRepairPending] = useState(false);
  const [manualRepairMessage, setManualRepairMessage] = useState('');
  const [patchScopeRepository, setPatchScopeRepository] = useState<RepositoryData | null>(null);
  const [patchScopePending, setPatchScopePending] = useState(false);
  const [githubError, setGithubError] = useState('');
  const [githubNotice, setGithubNotice] = useState('');
  const [connectingGithub, setConnectingGithub] = useState(false);
  const [repairJobs, setRepairJobs] = useState<RepairJobData[]>([]);
  const [selectedJob, setSelectedJob] = useState<RepairJobDetail | null>(null);
  const [milisaPatterns, setMilisaPatterns] = useState<MilisaPattern[]>([]);
  const [selectedMilisaPattern, setSelectedMilisaPattern] = useState<MilisaPatternDetail | null>(null);
  const [modelConfiguration, setModelConfiguration] = useState<ModelConfiguration | null>(null);
  const [auditLogs, setAuditLogs] = useState<AuditLogRow[]>([]);
  const [modelUsage, setModelUsage] = useState<ModelUsageRow[]>([]);

  useEffect(() => {
    const url = new URL(window.location.href);
    if (url.searchParams.get('github') === 'connected') {
      setGithubNotice('GitHub connected. Your repository list has been refreshed.');
      url.searchParams.delete('github');
      window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`);
    }
  }, []);

  useEffect(() => {
    let active = true;
    fetch(`${API}/api/v1/auth/me`, { credentials: 'include' })
      .then((response) => response.ok ? response.json() : null)
      .then((data) => { if (active) { setAuthUser(data); setAuthChecked(true); } })
      .catch(() => { if (active) { setApiUnavailable(true); setAuthChecked(true); } });
    fetch(`${API}/api/v1/status`, { credentials: 'include' })
      .then((response) => {
        if (!response.ok) throw new Error('API unavailable');
        return response.json() as Promise<ApiStatus>;
      })
      .then((data) => { if (active) setApiStatus(data); })
      .catch(() => { if (active) setApiUnavailable(true); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!authUser) return;
    fetch(`${API}/api/v1/dashboard/overview`, { credentials: 'include' })
      .then((response) => { if (!response.ok) throw new Error('Could not load dashboard'); return response.json(); })
      .then((data: DashboardData) => setOverview(data))
      .catch(() => setApiUnavailable(true));
    fetch(`${API}/api/v1/repair-jobs`, { credentials: 'include' })
      .then((response) => response.ok ? response.json() : [])
      .then((data: RepairJobData[]) => setRepairJobs(data));
    fetch(`${API}/api/v1/milisa/patterns`, { credentials: 'include' })
      .then((response) => response.ok ? response.json() : [])
      .then((data: MilisaPattern[]) => setMilisaPatterns(data));
    fetch(`${API}/api/v1/model/config`, { credentials: 'include' })
      .then((response) => response.ok ? response.json() : null)
      .then((data: ModelConfiguration | null) => setModelConfiguration(data));
    fetch(`${API}/api/v1/audit-logs`, { credentials: 'include' })
      .then((response) => response.ok ? response.json() : [])
      .then((data: AuditLogRow[]) => setAuditLogs(data));
    fetch(`${API}/api/v1/model/usage`, { credentials: 'include' })
      .then((response) => response.ok ? response.json() : [])
      .then((data: ModelUsageRow[]) => setModelUsage(data));
    const organizationId = authUser.organizations[0]?.id;
    if (organizationId) {
      fetch(`${API}/api/v1/github/repositories?organization_id=${organizationId}`, { credentials: 'include' })
        .then((response) => response.ok ? response.json() : [])
        .then((data: RepositoryData[]) => setRepositories(data));
    }
  }, [authUser]);

  const hasActiveJobs = repairJobs.some((job) => ACTIVE_REPAIR_STATUSES.has(job.status));
  useEffect(() => {
    if (!authUser) return;
    const controller = new AbortController();
    const organizationId = authUser.organizations[0]?.id;
    let inFlight = false;

    async function fetchJson<T>(path: string): Promise<T | null> {
      const response = await fetch(`${API}${path}`, { credentials: 'include', signal: controller.signal });
      return response.ok ? await response.json() as T : null;
    }

    async function refreshWorkspace() {
      if (inFlight) return;
      inFlight = true;
      try {
        const requests = await Promise.all([
          fetchJson<DashboardData>('/api/v1/dashboard/overview'),
          fetchJson<RepairJobData[]>('/api/v1/repair-jobs'),
          fetchJson<MilisaPattern[]>('/api/v1/milisa/patterns'),
          fetchJson<AuditLogRow[]>('/api/v1/audit-logs'),
          fetchJson<ModelUsageRow[]>('/api/v1/model/usage'),
          organizationId
            ? fetchJson<RepositoryData[]>(`/api/v1/github/repositories?organization_id=${organizationId}`)
            : Promise.resolve(null),
          selectedJob ? fetchJson<RepairJobDetail>(`/api/v1/repair-jobs/${selectedJob.id}`) : Promise.resolve(null),
        ]);
        if (controller.signal.aborted) return;
        const [nextOverview, nextJobs, nextPatterns, nextAuditLogs, nextUsage, nextRepositories, nextSelectedJob] = requests;
        if (nextOverview) setOverview(nextOverview);
        if (nextJobs) setRepairJobs(nextJobs);
        if (nextPatterns) setMilisaPatterns(nextPatterns);
        if (nextAuditLogs) setAuditLogs(nextAuditLogs);
        if (nextUsage) setModelUsage(nextUsage);
        if (nextRepositories) setRepositories(nextRepositories);
        if (nextSelectedJob) setSelectedJob(nextSelectedJob);
      } catch {
        // Keep the last persisted dashboard state visible during transient polling failures.
      } finally {
        inFlight = false;
      }
    }

    const refreshInterval = window.setInterval(refreshWorkspace, hasActiveJobs ? 10_000 : 30_000);
    return () => {
      window.clearInterval(refreshInterval);
      controller.abort();
    };
  }, [authUser, hasActiveJobs, selectedJob?.id]);

  async function beginGitHubInstall() {
    const organizationId = authUser?.organizations[0]?.id;
    if (!organizationId) return;
    setConnectingGithub(true);
    setGithubError('');
    try {
      const response = await fetch(`${API}/api/v1/github/installations/start`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ organization_id: organizationId }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'GitHub App setup is unavailable.');
      window.location.assign(result.installation_url);
    } catch (cause) {
      setGithubError(cause instanceof Error ? cause.message : 'Could not start GitHub setup.');
      setConnectingGithub(false);
    }
  }

  async function toggleMonitoring(repository: RepositoryData) {
    setGithubError('');
    const response = await fetch(`${API}/api/v1/github/repositories/${repository.id}/monitor`, {
      method: 'PATCH', credentials: 'include', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ monitored: !repository.monitored }),
    });
    if (!response.ok) {
      setGithubError('Could not update repository monitoring.');
      return;
    }
    const updated = await response.json();
    setRepositories((items) => items.map((item) => item.id === updated.id ? { ...item, monitored: updated.monitored } : item));
    fetch(`${API}/api/v1/dashboard/overview`, { credentials: 'include' }).then((result) => result.ok ? result.json() : null).then((data) => { if (data) setOverview(data); });
  }

  async function toggleRepositoryTrigger(repository: RepositoryData, trigger: 'workflow_run_failure' | 'check_run_failure') {
    setGithubError('');
    const triggers = { ...repository.triggers, [trigger]: !repository.triggers[trigger] };
    const response = await fetch(`${API}/api/v1/github/repositories/${repository.id}/triggers`, {
      method: 'PATCH', credentials: 'include', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(triggers),
    });
    if (!response.ok) {
      setGithubError('Could not update repository failure triggers.');
      return;
    }
    setRepositories((items) => items.map((item) => item.id === repository.id ? { ...item, triggers } : item));
  }

  async function toggleRequiredCheck(repository: RepositoryData, check: 'ruff' | 'mypy' | 'build') {
    setGithubError('');
    const required_checks = repository.required_checks.includes(check)
      ? repository.required_checks.filter((item) => item !== check)
      : [...repository.required_checks, check];
    const response = await fetch(`${API}/api/v1/github/repositories/${repository.id}/checks`, {
      method: 'PATCH', credentials: 'include', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ required_checks }),
    });
    if (!response.ok) {
      setGithubError('Could not update repository validation checks.');
      return;
    }
    setRepositories((items) => items.map((item) => item.id === repository.id ? { ...item, required_checks } : item));
  }

  async function saveRepositoryPatchScope(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!patchScopeRepository) return;
    setPatchScopePending(true);
    setGithubError('');
    const fields = new FormData(event.currentTarget);
    const allowed_paths = String(fields.get('allowed_paths') ?? '').split(/\r?\n/).map((path) => path.trim()).filter(Boolean);
    try {
      const response = await fetch(`${API}/api/v1/github/repositories/${patchScopeRepository.id}/patch-scope`, {
        method: 'PATCH', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ allowed_paths }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Could not update repository patch scope.');
      setRepositories((items) => items.map((item) => item.id === patchScopeRepository.id ? { ...item, allowed_paths: result.allowed_paths } : item));
      setPatchScopeRepository(null);
    } catch (cause) {
      setGithubError(cause instanceof Error ? cause.message : 'Could not update repository patch scope.');
    } finally {
      setPatchScopePending(false);
    }
  }

  async function openRepairJob(jobId: string) {
    const response = await fetch(`${API}/api/v1/repair-jobs/${jobId}`, { credentials: 'include' });
    if (response.ok) setSelectedJob(await response.json());
  }

  async function requestManualRepair(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!manualRepairRepository) return;
    setManualRepairPending(true);
    setGithubError('');
    setManualRepairMessage('');
    const fields = new FormData(event.currentTarget);
    try {
      const response = await fetch(`${API}/api/v1/github/repositories/${manualRepairRepository.id}/repair`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          commit_sha: String(fields.get('commit_sha') ?? '').trim(),
          failure_category: String(fields.get('failure_category') ?? 'unknown'),
          failure_summary: String(fields.get('failure_summary') ?? '').trim(),
        }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Could not request a repair.');
      setManualRepairRepository(null);
      setManualRepairMessage(result.queued
        ? 'Repair request queued. Its execution history is open below.'
        : 'Repair request was saved, but the queue is unavailable. Open the job below to retry queueing.');
      const jobsResponse = await fetch(`${API}/api/v1/repair-jobs`, { credentials: 'include' });
      if (jobsResponse.ok) setRepairJobs(await jobsResponse.json());
      await openRepairJob(String(result.job_id));
    } catch (cause) {
      setGithubError(cause instanceof Error ? cause.message : 'Could not request a repair.');
    } finally {
      setManualRepairPending(false);
    }
  }

  async function retryQueuedJob(jobId: string) {
    setGithubError('');
    try {
      const response = await fetch(`${API}/api/v1/repair-jobs/${jobId}/retry-queue`, {
        method: 'POST', credentials: 'include',
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Could not queue this repair.');
      await openRepairJob(jobId);
      setManualRepairMessage('Repair job queued.');
    } catch (cause) {
      setGithubError(cause instanceof Error ? cause.message : 'Could not queue this repair.');
    }
  }

  async function retryPullRequest(jobId: string) {
    setGithubError('');
    try {
      const response = await fetch(`${API}/api/v1/repair-jobs/${jobId}/retry-pull-request`, {
        method: 'POST', credentials: 'include',
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        setGithubError(typeof result.detail === 'string' ? result.detail : 'Could not queue the pull request retry.');
        return;
      }
      await openRepairJob(jobId);
      setRepairJobs((current) => current.map((job) => job.id === jobId ? { ...job, status: 'PULL_REQUEST_CREATION' } : job));
    } catch {
      setGithubError('The API could not be reached to queue the pull request retry.');
    }
  }

  async function invalidateMilisaPattern(patternId: string) {
    const response = await fetch(`${API}/api/v1/milisa/patterns/${patternId}`, {
      method: 'DELETE', credentials: 'include',
    });
    if (!response.ok) {
      setGithubError('Could not invalidate this verified cache entry.');
      return;
    }
    setMilisaPatterns((current) => current.filter((pattern) => pattern.id !== patternId));
    if (selectedMilisaPattern?.id === patternId) setSelectedMilisaPattern(null);
  }

  async function openMilisaPattern(patternId: string) {
    setGithubError('');
    try {
      const response = await fetch(`${API}/api/v1/milisa/patterns/${patternId}`, { credentials: 'include' });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Could not load the verified fix.');
      setSelectedMilisaPattern(result as MilisaPatternDetail);
    } catch (cause) {
      setGithubError(cause instanceof Error ? cause.message : 'Could not load the verified fix.');
    }
  }

  async function cancelRepairJob(jobId: string) {
    const response = await fetch(`${API}/api/v1/repair-jobs/${jobId}/cancel`, {
      method: 'POST', credentials: 'include',
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      setGithubError(typeof result.detail === 'string' ? result.detail : 'Could not cancel this repair job.');
      return;
    }
    await openRepairJob(jobId);
    setRepairJobs((current) => current.map((job) => job.id === jobId ? { ...job, status: 'CANCELLED' } : job));
  }

  if (!authChecked) return <div className="loading-screen">Loading your workspace...</div>;
  if (!authUser) return <AuthPanel apiUnavailable={apiUnavailable} onAuthenticated={(user) => setAuthUser(user)} />;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#overview" aria-label="Patchwork home">
          <span className="brand-mark"><Waypoints size={20} /></span>
          <span>patchwork<span className="brand-dot">.</span></span>
        </a>
        <div className="workspace-label">WORKSPACE</div>
        <nav aria-label="Main navigation">
          <a className="nav-link active" href="#overview"><LayoutDashboard size={17} /> Overview</a>
          <a className="nav-link" href="#repositories"><Code2 size={17} /> Repositories</a>
          <a className="nav-link" href="#jobs"><Activity size={17} /> Repair jobs</a>
        </nav>
        <div className="sidebar-bottom">
          <div className="secure-note"><ShieldCheck size={16} /><span>Human review required<br /><small>for every repair PR</small></span></div>
          <div className="profile"><div className="avatar">{authUser.display_name.slice(0, 1).toUpperCase()}</div><div><strong>{authUser.display_name}</strong><small>{authUser.organizations[0]?.name ?? 'Workspace'}</small></div><button className="signout" onClick={async () => { await fetch(`${API}/api/v1/auth/logout`, { method: 'POST', credentials: 'include' }); setAuthUser(null); }}>Sign out</button></div>
        </div>
      </aside>

      <main className="main-content" id="overview">
        <header className="topbar"><div className="breadcrumb">Workspace <span>/</span> <b>Overview</b></div></header>
        <div className="page-wrap">
          <section className="welcome-row"><div><div className="eyebrow">{today}</div><h1>Overview</h1><p className="subtitle">Welcome back, {authUser.display_name.split(' ')[0]}. Here’s what’s happening in your workspace.</p></div><button className="primary-button connect-button" onClick={beginGitHubInstall} disabled={connectingGithub}><Code2 size={16} /> {connectingGithub ? 'Opening GitHub...' : 'Connect GitHub'} <ArrowUpRight size={15} /></button></section>

          {apiUnavailable && <div className="notice warning"><span className="notice-dot" /> The workspace service is unavailable. Check the local API, then refresh this page.</div>}
          {githubError && <div className="notice warning" role="alert"><span className="notice-dot" /> {githubError}</div>}
          {githubNotice && <div className="notice success" role="status">{githubNotice}</div>}
          {manualRepairMessage && <div className="notice success" role="status">{manualRepairMessage}</div>}

          <section className="metric-grid" aria-label="Workspace summary">
            <Metric label="Repositories" value={overview ? String(overview.connected_repositories) : '—'} caption="Connected to this workspace" icon={<Code2 size={17} />} />
            <Metric label="Monitored" value={overview ? String(overview.monitored_repositories) : '—'} caption="Watching for CI failures" icon={<ShieldCheck size={17} />} />
            <Metric label="Failed runs" value={overview ? String(overview.failures_detected) : '—'} caption="Repair jobs created" icon={<Activity size={17} />} />
            <Metric label="Needs review" value={overview ? String(overview.needs_human_review) : '—'} caption="Waiting for your decision" icon={<ShieldCheck size={17} />} />
          </section>
          <section id="repositories" className="panel repositories-panel">
            <div className="panel-heading"><div><div className="section-kicker">GITHUB</div><h2>Repositories</h2></div><button className="subtle-button" onClick={beginGitHubInstall}>Connect another account <ArrowUpRight size={13} /></button></div>
            {apiStatus && !apiStatus.repair_jobs_enabled && <p className="detail-empty">Repair automation is not configured yet. Repository connection and workspace settings remain available.</p>}
            {repositories.length ? <div className="repository-list">{repositories.map((repository) => <div className="repository-row" key={repository.id}><div className="repo-symbol"><Code2 size={16} /></div><div className="repo-name"><strong>{repository.full_name}</strong><small>{repository.private ? 'Private' : 'Public'} · default branch {repository.default_branch}</small></div><div className="repository-controls"><label className="monitor-switch"><span>Monitor</span><input type="checkbox" disabled={!apiStatus?.repair_jobs_enabled} checked={repository.monitored} onChange={() => toggleMonitoring(repository)} /><i /></label><label className="monitor-switch"><span>Workflow failures</span><input type="checkbox" disabled={!apiStatus?.repair_jobs_enabled} checked={repository.triggers.workflow_run_failure} onChange={() => toggleRepositoryTrigger(repository, 'workflow_run_failure')} /><i /></label><label className="monitor-switch"><span>Check failures</span><input type="checkbox" disabled={!apiStatus?.repair_jobs_enabled} checked={repository.triggers.check_run_failure} onChange={() => toggleRepositoryTrigger(repository, 'check_run_failure')} /><i /></label><label className="monitor-switch"><span>Ruff</span><input type="checkbox" checked={repository.required_checks.includes('ruff')} onChange={() => toggleRequiredCheck(repository, 'ruff')} /><i /></label><label className="monitor-switch"><span>Mypy</span><input type="checkbox" checked={repository.required_checks.includes('mypy')} onChange={() => toggleRequiredCheck(repository, 'mypy')} /><i /></label><label className="monitor-switch"><span>Build</span><input type="checkbox" checked={repository.required_checks.includes('build')} onChange={() => toggleRequiredCheck(repository, 'build')} /><i /></label>{['owner', 'admin'].includes(authUser.organizations[0]?.role ?? '') && <button className="subtle-button" disabled={!apiStatus?.repair_jobs_enabled} onClick={() => { setManualRepairRepository(repository); setManualRepairMessage(''); }}>Request repair</button>}</div></div>)}</div> : <div className="repositories-empty"><Code2 size={20} /><span>No repositories connected yet. Connect a GitHub App to choose repositories.</span></div>}
            {['owner', 'admin'].includes(authUser.organizations[0]?.role ?? '') && repositories.length > 0 && <div className="form-actions patch-scope-actions">{repositories.map((repository) => <button key={repository.id} className="subtle-button" onClick={() => { setPatchScopeRepository(repository); setManualRepairRepository(null); }}>Patch scope · {repository.full_name}</button>)}</div>}
            {patchScopeRepository && <form className="manual-repair-form" onSubmit={saveRepositoryPatchScope}>
              <div><div className="section-kicker">PATCH SCOPE</div><h3>Limit repair files for {patchScopeRepository.full_name}</h3><p className="detail-empty">Enter exact repository-relative Python paths, one per line. When set, every changed file, including regression tests, must be listed. Leave blank to clear this limit.</p></div>
              <label>Allowed paths<textarea name="allowed_paths" defaultValue={patchScopeRepository.allowed_paths.join('\n')} rows={5} placeholder={'src/module.py\ntests/test_module.py'} /></label>
              <div className="form-actions"><button className="primary-button" type="submit" disabled={patchScopePending}>{patchScopePending ? 'Saving scope...' : 'Save patch scope'}</button><button className="subtle-button" type="button" onClick={() => setPatchScopeRepository(null)}>Cancel</button></div>
            </form>}
            {manualRepairRepository && <form className="manual-repair-form" onSubmit={requestManualRepair}>
              <div><div className="section-kicker">MANUAL REPAIR</div><h3>Request a repair for {manualRepairRepository.full_name}</h3><p className="detail-empty">Provide a commit and a short failure description. The request uses the same validation and human-review gates as webhook repairs.</p></div>
              <label>Commit SHA<input name="commit_sha" required minLength={40} maxLength={64} pattern="[0-9a-fA-F]{40,64}" autoComplete="off" spellCheck={false} /></label>
              <label>Failure category<select name="failure_category" defaultValue="unknown"><option value="test">Test failure</option><option value="build">Build or compilation</option><option value="lint">Lint or static analysis</option><option value="dependency">Dependency</option><option value="configuration">Configuration</option><option value="runtime">Runtime</option><option value="unknown">Other / unsure</option></select></label>
              <label>Failure details<textarea name="failure_summary" required minLength={3} maxLength={5000} rows={3} /></label>
              <div className="form-actions"><button className="primary-button" type="submit" disabled={manualRepairPending}>{manualRepairPending ? 'Queueing repair...' : 'Create repair job'}</button><button className="subtle-button" type="button" onClick={() => setManualRepairRepository(null)}>Cancel</button></div>
            </form>}
            {repositories.length > 0 && <p className="validation-config-note">Ruff, Mypy, and package build checks run only when selected; each tool must be present in the configured trusted validation image.</p>}
          </section>

          <section id="jobs" className="panel jobs-panel">
            <div className="panel-heading"><div><div className="section-kicker">REPAIR HISTORY</div><h2>Repair jobs</h2></div><span className="progress-label">Recent</span></div>
            {repairJobs.length ? <div className="job-list">{repairJobs.map((job) => <button className="job-list-row" key={job.id} onClick={() => openRepairJob(job.id)}><span className="job-status-dot" /><span className="job-list-copy"><strong>{job.repository}</strong><small>{job.failure_summary || job.failure_category} · {job.commit_sha.slice(0, 8)}</small></span><span className="job-status-text">{job.status.replaceAll('_', ' ')}</span><ArrowUpRight size={14} /></button>)}</div> : <div className="repositories-empty"><Activity size={20} /><span>{overview ? 'No repair jobs have been recorded yet.' : 'Loading persisted repair history.'}</span></div>}
          </section>

          <details className="advanced-workspace panel">
            <summary>Advanced workspace data <span>Cache, model usage, and audit history</span></summary>
            <section id="cache" className="advanced-section">
            <div className="panel-heading"><div><div className="section-kicker">VERIFIED FIXES</div><h2>Milisa cache</h2></div><span className="progress-label">{milisaPatterns.length} verified patterns</span></div>
            {milisaPatterns.length ? <div className="job-list">{milisaPatterns.map((pattern) => <article className="job-list-row" key={pattern.id}><span className="job-status-dot" /><span className="job-list-copy"><strong>{pattern.failure_category} · {pattern.language}{pattern.framework ? ` / ${pattern.framework}` : ''}</strong><small>{pattern.root_cause}</small></span><button className="subtle-button" onClick={() => openMilisaPattern(pattern.id)}>Details</button><span className="job-status-text">{pattern.reuse_count} uses · {pattern.success_count} successes</span>{['owner', 'admin'].includes(authUser.organizations[0]?.role ?? '') && <button className="subtle-button" onClick={() => invalidateMilisaPattern(pattern.id)}>Invalidate</button>}</article>)}</div> : <div className="repositories-empty"><Box size={20} /><span>No verified fixes are available. Repair attempts are recorded as cache misses until validation can promote a fix.</span></div>}
            </section>
          </details>

          {selectedJob && <section className="panel job-detail-panel" aria-labelledby="job-detail-title"><div className="panel-heading"><div><div className="section-kicker">JOB DETAIL · {selectedJob.id.slice(0, 8)}</div><h2 id="job-detail-title">{selectedJob.repository}</h2><p className="job-summary">{selectedJob.failure_summary}</p></div><div className="panel-actions">{selectedJob.status === 'QUEUED' && selectedJob.error_message && <button className="subtle-button" onClick={() => retryQueuedJob(selectedJob.id)}>Retry queue</button>}{selectedJob.status === 'NEEDS_HUMAN_REVIEW' && !selectedJob.pr_url && selectedJob.validation_runs.at(-1)?.status === 'PASSED' && <button className="subtle-button" onClick={() => retryPullRequest(selectedJob.id)}>Retry PR creation</button>}{!['COMPLETED', 'CANCELLED', 'FAILED', 'PULL_REQUEST_CREATION'].includes(selectedJob.status) && <button className="subtle-button" onClick={() => cancelRepairJob(selectedJob.id)}>Cancel job</button>}<button className="subtle-button" onClick={() => setSelectedJob(null)}>Close</button></div></div>
            <div className="job-detail-meta"><span className="job-status-text">{selectedJob.status.replaceAll('_', ' ')}</span><span>{selectedJob.failure_category}</span><span>Repair attempts {selectedJob.retry_count + 1}</span><span>Commit {selectedJob.commit_sha.slice(0, 8)}</span><span>Base {selectedJob.base_branch}</span>{selectedJob.workflow_run_id && <span>Workflow run {selectedJob.workflow_run_id}</span>}{selectedJob.check_run_id && <span>Check run {selectedJob.check_run_id}</span>}{selectedJob.pull_request && <span>Pull request #{selectedJob.pull_request.number} · {selectedJob.pull_request.status.toLowerCase()}</span>}{selectedJob.pr_url && <a href={selectedJob.pr_url} target="_blank" rel="noreferrer">View pull request <ArrowUpRight size={13} /></a>}</div>
            {selectedJob.error_message && <div className="notice warning">{selectedJob.error_message}</div>}
            <div className="detail-columns"><div><h3>Execution timeline</h3>{selectedJob.events.length ? <ol className="job-timeline">{selectedJob.events.map((event, index) => <li key={`${event.to_state}-${index}`}><i /><div><strong>{event.to_state.replaceAll('_', ' ')}</strong><small>{event.to_state === 'CACHE_LOOKUP' && typeof event.detail.outcome === 'string' ? `Milisa ${event.detail.outcome.toLowerCase()} · ` : ''}{event.actor} · {new Date(event.created_at).toLocaleString()}</small></div></li>)}</ol> : <p className="detail-empty">No execution events recorded.</p>}</div>
              <div><h3>Agent runs</h3>{selectedJob.agent_runs.length ? selectedJob.agent_runs.map((run, index) => <div className="detail-record" key={`${run.agent}-${index}`}><strong>{run.agent} · {run.status}</strong><small>{run.model_id ?? 'No model recorded'}</small><pre>{JSON.stringify(run.output, null, 2)}</pre></div>) : <p className="detail-empty">Agents have not run for this job.</p>}</div></div>
            <div className="detail-section"><h3>Patch diff</h3>{selectedJob.patches.length ? selectedJob.patches.map((patch, index) => <div key={`${patch.patch_hash}-${index}`}><p className="job-summary">{patch.explanation}</p><pre className="diff-viewer">{patch.diff}</pre></div>) : <p className="detail-empty">No patch has been generated.</p>}</div>
            <div className="detail-section"><h3>Validation results</h3>{selectedJob.validation_runs.length ? selectedJob.validation_runs.map((run, index) => <div className="validation-block" key={`${run.attempt}-${index}`}><strong>Attempt {run.attempt} · {run.status}</strong>{run.results.map((result, resultIndex) => <div className="validation-result" key={`${result.check_name}-${resultIndex}`}><span>{result.check_name}</span><b>{result.outcome}</b></div>)}{run.log_excerpt && <pre className="diff-viewer">{run.log_excerpt}</pre>}</div>) : <p className="detail-empty">No validation has run.</p>}</div>
          </section>}

          {selectedMilisaPattern && <section className="panel job-detail-panel" aria-labelledby="milisa-detail-title">
            <div className="panel-heading"><div><div className="section-kicker">VERIFIED FIX · {selectedMilisaPattern.id.slice(0, 8)}</div><h2 id="milisa-detail-title">{selectedMilisaPattern.failure_category} · {selectedMilisaPattern.language}{selectedMilisaPattern.framework ? ` / ${selectedMilisaPattern.framework}` : ''}</h2></div><button className="subtle-button" onClick={() => setSelectedMilisaPattern(null)}>Close</button></div>
            <p className="job-summary">{selectedMilisaPattern.root_cause}</p>
            <div className="job-detail-meta"><span>{selectedMilisaPattern.verification_status}</span><span>{selectedMilisaPattern.success_count} validations passed</span><span>{selectedMilisaPattern.reuse_count} reuses</span><span>Created {new Date(selectedMilisaPattern.created_at).toLocaleString()}</span><span>Last used {selectedMilisaPattern.last_used_at ? new Date(selectedMilisaPattern.last_used_at).toLocaleString() : 'Never'}</span><span>Expires {selectedMilisaPattern.expires_at ? new Date(selectedMilisaPattern.expires_at).toLocaleDateString() : 'No expiry'}</span></div>
            <div className="detail-columns"><div><h3>Compatibility and provenance</h3><div className="detail-record"><strong>Regression reference</strong><small>{selectedMilisaPattern.regression_reference ?? 'Not recorded'}</small></div><div className="detail-record"><strong>Source revision</strong><small>{selectedMilisaPattern.source_revision ?? 'Not recorded'}</small></div><div className="detail-record"><strong>Target revision</strong><small>{selectedMilisaPattern.target_revision ?? 'Not recorded'}</small></div><div className="detail-record"><strong>Error signature</strong><small>{selectedMilisaPattern.error_signature}</small></div><div className="detail-record"><strong>Source context fingerprint</strong><small>{selectedMilisaPattern.context_fingerprint}</small></div></div><div><h3>Environment and checks</h3><pre className="diff-viewer">{JSON.stringify(selectedMilisaPattern.validation_metadata, null, 2)}</pre><h3>Version constraints</h3><pre className="diff-viewer">{JSON.stringify(selectedMilisaPattern.version_constraints, null, 2)}</pre></div></div>
            <div className="detail-section"><h3>Verified patch</h3><pre className="diff-viewer">{selectedMilisaPattern.patch_template}</pre></div>
          </section>}

          {repositories.length === 0 && <section className="content-grid">
            <div className="panel setup-panel">
              <div className="panel-heading"><div><div className="section-kicker">GETTING STARTED</div><h2>Set up your workspace</h2></div><span className="progress-label">Next steps</span></div>
              <div className="setup-list">
                <SetupItem number="01" title="Connect your GitHub App" description="Choose repositories and grant least-privilege access." status={repositories.length ? 'CONNECTED' : apiStatus?.github_app_configured ? 'NEXT UP' : 'NEEDS SETUP'} icon={<Code2 size={17} />} />
                <SetupItem number="02" title="Configure inference" description="Set your Nebius endpoint and Nemotron model." status={apiStatus?.nebius_configured ? 'READY' : 'NEEDS SETUP'} icon={<Waypoints size={17} />} />
                <SetupItem number="03" title="Enable a validation runner" description="Run candidate patches in a disposable sandbox." status={apiStatus?.validation_configured ? 'IMAGE SET' : 'NEEDS SETUP'} icon={<ShieldCheck size={17} />} />
                <SetupItem number="04" title="Run your first repair" description="Review a validated fix before opening its PR." status={apiStatus?.repair_jobs_enabled ? 'CONFIGURED' : 'WAITING'} icon={<GitBranch size={17} />} />
              </div>
            </div>
          </section>}
          <details className="advanced-workspace panel">
            <summary>Workspace diagnostics <span>Model settings, usage, and audit history</span></summary>
          <section id="settings" className="advanced-section">
            <div className="panel-heading"><div><div className="section-kicker">SERVER CONFIGURATION</div><h2>Model settings</h2></div><span className={`status-pill ${modelConfiguration?.configured ? 'online' : 'pending'}`}><i />{modelConfiguration?.configured ? 'CONFIGURED' : 'NEEDS SETUP'}</span></div>
            <div className="system-row"><span>Provider</span><b>{modelConfiguration?.provider ?? 'Loading...'}</b></div>
            <div className="system-row"><span>Model identifier</span><b>{modelConfiguration?.model_id ?? 'Not configured'}</b></div>
            <div className="system-row"><span>Endpoint host</span><b>{modelConfiguration?.endpoint_host ?? 'Not configured'}</b></div>
            <div className="system-row"><span>Coder provider</span><b>{modelConfiguration?.coder_provider ?? 'Loading...'}</b></div>
            <div className="system-row"><span>Coder model</span><b>{modelConfiguration?.coder_model_id ?? 'Not configured'}</b></div>
            <div className="system-row"><span>Input price / million tokens</span><b>{modelConfiguration?.input_price_per_million == null ? 'Not configured' : `$${modelConfiguration.input_price_per_million}`}</b></div>
            <div className="system-foot"><span>Output price / million tokens</span><b>{modelConfiguration?.output_price_per_million == null ? 'Not configured' : `$${modelConfiguration.output_price_per_million}`}</b></div>
            <p className="detail-empty">Credentials are configured on the backend and are never returned to the browser.</p>
          </section>
          <section className="advanced-section jobs-panel" aria-labelledby="model-usage-title">
            <div className="panel-heading"><div><div className="section-kicker">PERSISTED INFERENCE RECORDS</div><h2 id="model-usage-title">Model usage</h2></div><span className="progress-label">{modelUsage.length} recent calls</span></div>
            {modelUsage.length ? <div className="recent-list">{modelUsage.map((usage, index) => <div className="recent-row" key={`${usage.created_at}-${index}`}><span className="recent-dot" /><div><strong>{usage.model_id}</strong><p>{usage.provider} · {usage.prompt_tokens.toLocaleString()} input / {usage.completion_tokens.toLocaleString()} output tokens</p></div><time>{usage.estimated_cost == null ? 'Cost not configured' : `$${usage.estimated_cost.toFixed(4)}`}</time></div>)}</div> : <div className="repositories-empty"><Waypoints size={20} /><span>No model calls have been recorded yet.</span></div>}
          </section>
          <section id="audit-logs" className="advanced-section jobs-panel">
            <div className="panel-heading"><div><div className="section-kicker">ORGANIZATION ACTIVITY</div><h2>Audit logs</h2></div><span className="progress-label">Recent events</span></div>
            {auditLogs.length ? <div className="recent-list">{auditLogs.map((row) => <div className="recent-row" key={row.id}><span className="recent-dot" /><div><strong>{row.action}</strong><p>{row.entity_type}{row.entity_id ? ` · ${row.entity_id}` : ''}</p></div><time>{new Date(row.created_at).toLocaleString()}</time></div>)}</div> : <div className="repositories-empty"><Activity size={20} /><span>No audit events have been recorded yet.</span></div>}
          </section>
          </details>
          <footer className="footer-note"><ShieldCheck size={14} /> Proposed repairs remain subject to human review. Merging always stays in your hands.</footer>
        </div>
      </main>
    </div>
  );
}

function Metric({ label, value, caption, icon }: { label: string; value: string; caption: string; icon: React.ReactNode }) {
  return <article className="metric-card"><div className="metric-top"><span>{label}</span><span className="metric-icon">{icon}</span></div><div className="metric-value">{value}</div><div className="metric-caption">{caption}</div></article>;
}

function SetupItem({ number, title, description, status, icon }: { number: string; title: string; description: string; status: string; icon: React.ReactNode }) {
  return <div className="setup-item"><span className="step-number">{number}</span><span className={`step-icon ${status === 'NEXT UP' ? 'highlight' : ''}`}>{icon}</span><div className="step-copy"><strong>{title}</strong><p>{description}</p></div><span className={`step-status ${status === 'NEXT UP' ? 'next' : ''}`}>{status}</span></div>;
}

function AuthPanel({ apiUnavailable, onAuthenticated }: { apiUnavailable: boolean; onAuthenticated: (user: { display_name: string; organizations: { id: string; name: string; role: string }[] }) => void }) {
  const [registering, setRegistering] = useState(false);
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError('');
    const form = new FormData(event.currentTarget);
    const payload: Record<string, string> = {
      email: String(form.get('email') ?? ''),
      password: String(form.get('password') ?? ''),
    };
    if (registering) {
      payload.display_name = String(form.get('display_name') ?? '');
      payload.organization_name = String(form.get('organization_name') ?? '');
    }
    try {
      const response = await fetch(`${API}/api/v1/auth/${registering ? 'register' : 'login'}`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const result = await response.json().catch(() => ({}));
        const detail = result.detail;
        const message = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((item) => item.msg).join(' ') : 'Could not sign in. Please try again.';
        throw new Error(message);
      }
      const me = await fetch(`${API}/api/v1/auth/me`, { credentials: 'include' });
      if (!me.ok) throw new Error('Your session could not be loaded. Please try again.');
      onAuthenticated(await me.json());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'The API could not be reached.');
    } finally {
      setPending(false);
    }
  }

  return <main className="auth-screen"><section className="auth-card">
    <div className="brand auth-brand"><span className="brand-mark"><Waypoints size={20} /></span><span>patchwork<span className="brand-dot">.</span></span></div>
    <div className="section-kicker">CI REPAIR WORKSPACE</div>
    <h1>{registering ? 'Create your workspace' : 'Welcome back'}</h1>
    <p className="auth-subtitle">{registering ? 'Start with your account and organization.' : 'Sign in to manage your repair workspace.'}</p>
    {apiUnavailable && <div className="auth-error">The API is unavailable. Check the PHP API configuration and MySQL connection, then refresh this page.</div>}
    {error && <div className="auth-error" role="alert">{error}</div>}
    <form onSubmit={submit} className="auth-form">
      {registering && <><label>Display name<input name="display_name" autoComplete="name" required maxLength={120} /></label><label>Organization name<input name="organization_name" required maxLength={160} /></label></>}
      <label>Email<input name="email" type="email" autoComplete="email" required maxLength={320} /></label>
      <label>Password<input name="password" type="password" autoComplete={registering ? 'new-password' : 'current-password'} required minLength={registering ? 12 : 1} maxLength={256} />{registering && <small>Use at least 12 characters.</small>}</label>
      <button className="auth-submit" disabled={pending || apiUnavailable}>{pending ? 'Please wait...' : registering ? 'Create account' : 'Sign in'}</button>
    </form>
    <button className="auth-toggle" onClick={() => { setRegistering(!registering); setError(''); }}>{registering ? 'Already have an account? Sign in' : 'New here? Create a workspace'}</button>
    <p className="auth-security"><ShieldCheck size={14} /> Sessions use secure, HttpOnly cookies. Human review is required for every repair PR.</p>
  </section></main>;
}
