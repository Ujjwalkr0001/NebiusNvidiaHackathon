<?php
declare(strict_types=1);

final class Api
{
    private array $config;
    private PDO $db;
    private string $method;
    private string $path;
    private array $body;
    private string $rawBody;
    private ?array $user = null;

    public function __construct(array $config)
    {
        $this->config = $config;
        $this->method = strtoupper($_SERVER['REQUEST_METHOD'] ?? 'GET');
        $this->path = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?: '/';
        $raw = file_get_contents('php://input', false, null, 0, 2_000_001);
        if ($raw === false || strlen($raw) > 2_000_000) {
            $this->fail(413, 'Request body is too large');
        }
        $this->rawBody = $raw;
        $decoded = $raw === '' ? [] : json_decode($raw, true);
        if (!is_array($decoded)) {
            $this->fail(400, 'Request body must be a JSON object');
        }
        $this->body = $decoded;
        $dsn = (string)($config['database_dsn'] ?? '');
        if ($dsn === '') {
            $this->fail(503, 'MySQL database is not configured');
        }
        try {
            $this->db = new PDO($dsn, (string)$config['database_user'], (string)$config['database_password'], [
                PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
                PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
                PDO::ATTR_EMULATE_PREPARES => false,
            ]);
        } catch (Throwable $e) {
            error_log('Hostinger API database connection failed: ' . get_class($e));
            $this->fail(503, 'Database is temporarily unavailable');
        }
    }

    public function run(): never
    {
        $origin = rtrim((string)($this->config['frontend_origin'] ?? ''), '/');
        $requestOrigin = rtrim((string)($_SERVER['HTTP_ORIGIN'] ?? ''), '/');
        if ($requestOrigin !== '' && $requestOrigin === $origin) {
            header('Access-Control-Allow-Origin: ' . $origin);
            header('Access-Control-Allow-Credentials: true');
            header('Vary: Origin');
        }
        header('Access-Control-Allow-Methods: GET, POST, PATCH, DELETE, OPTIONS');
        header('Access-Control-Allow-Headers: Content-Type, X-Hub-Signature-256, X-GitHub-Event, X-GitHub-Delivery');
        header('X-Content-Type-Options: nosniff');
        header('Cache-Control: no-store');
        if ($this->method === 'OPTIONS') {
            http_response_code(204);
            exit;
        }
        if ($this->method !== 'GET' && $this->path !== '/api/v1/webhooks/github') {
            if ($requestOrigin !== '' && $requestOrigin !== $origin) {
                $this->fail(403, 'Cross-origin request rejected');
            }
        }

        try {
            $result = $this->dispatch();
            if ($result instanceof RedirectResponse) {
                header('Location: ' . $result->url, true, $result->status);
                exit;
            }
            $status = http_response_code();
            $this->json($result['data'] ?? $result, $result['status'] ?? ($status ?: 200));
        } catch (ApiError $e) {
            $this->json(['detail' => $e->getMessage()], $e->status);
        } catch (Throwable $e) {
            error_log('Hostinger API request failed: ' . get_class($e));
            $this->json(['detail' => 'The request could not be completed'], 500);
        }
    }

    private function dispatch(): mixed
    {
        if ($this->method === 'GET' && $this->path === '/health') {
            return ['status' => 'ok'];
        }
        if ($this->method === 'GET' && $this->path === '/api/v1/status') {
            return $this->status();
        }
        if ($this->method === 'POST' && $this->path === '/api/v1/auth/register') return $this->register();
        if ($this->method === 'POST' && $this->path === '/api/v1/auth/login') return $this->login();
        if ($this->method === 'POST' && $this->path === '/api/v1/auth/logout') return $this->logout();
        if ($this->method === 'GET' && $this->path === '/api/v1/auth/me') return $this->me();
        if ($this->method === 'GET' && $this->path === '/api/v1/dashboard/overview') return $this->dashboard();
        if ($this->method === 'GET' && $this->path === '/api/v1/github/repositories') return $this->repositories();
        if ($this->method === 'POST' && $this->path === '/api/v1/github/installations/start') return $this->startGithubInstall();
        if ($this->method === 'GET' && $this->path === '/api/v1/github/setup') return $this->githubSetup();
        if ($this->method === 'GET' && $this->path === '/api/v1/github/callback') return $this->githubCallback();
        if ($this->method === 'POST' && $this->path === '/api/v1/webhooks/github') return $this->githubWebhook();
        if ($this->method === 'GET' && $this->path === '/api/v1/repair-jobs') return $this->repairJobs();
        if ($this->method === 'POST' && preg_match('~^/api/v1/github/repositories/([a-f0-9-]+)/repair$~i', $this->path, $m)) return $this->manualRepair($m[1]);
        if (preg_match('~^/api/v1/github/repositories/([a-f0-9-]+)/(monitor|triggers|checks|patch-scope)$~i', $this->path, $m)) return $this->repositorySetting($m[1], $m[2]);
        if ($this->method === 'GET' && $this->path === '/api/v1/model/config') return $this->modelConfig();
        if ($this->method === 'GET' && $this->path === '/api/v1/model/usage') return $this->modelUsage();
        if ($this->method === 'GET' && $this->path === '/api/v1/milisa/patterns') return $this->milisaPatterns();
        if ($this->method === 'GET' && $this->path === '/api/v1/audit-logs') return $this->auditLogs();
        if (preg_match('~^/api/v1/repair-jobs/([a-f0-9-]+)$~i', $this->path, $m) && $this->method === 'GET') return $this->repairJob($m[1]);
        if (preg_match('~^/api/v1/repair-jobs/([a-f0-9-]+)/(retry-queue|retry-pull-request|cancel)$~i', $this->path, $m) && $this->method === 'POST') return $this->jobAction($m[1], $m[2]);
        if (preg_match('~^/api/v1/milisa/patterns/([a-f0-9-]+)$~i', $this->path, $m) && $this->method === 'GET') return $this->milisaPattern($m[1]);
        if (preg_match('~^/api/v1/milisa/patterns/([a-f0-9-]+)$~i', $this->path, $m) && $this->method === 'DELETE') return $this->deleteMilisaPattern($m[1]);
        $this->fail(404, 'Route not found');
    }

    private function status(): array
    {
        $github = !empty($this->config['github_app_id']) && !empty($this->config['github_app_slug'])
            && !empty($this->config['github_client_id']) && !empty($this->config['github_client_secret'])
            && !empty($this->config['github_callback_url']);
        $webhook = !empty($this->config['github_webhook_secret']);
        $nebius = !empty($this->config['nebius_api_key']) && !empty($this->config['nebius_model']) && !empty($this->config['nebius_base_url']);
        return [
            'service' => 'self-healing-ci-api-php',
            'milestone' => 'hostinger-web-compatibility',
            'integrations_configured' => $github && $webhook && $nebius,
            'repair_jobs_enabled' => $github && $webhook && $nebius && !empty($this->config['repair_worker_enabled']),
            'github_app_configured' => $github,
            'github_webhook_configured' => $webhook,
            'nebius_configured' => $nebius,
            'validation_configured' => false,
        ];
    }

    private function register(): array
    {
        $email = strtolower(trim((string)($this->body['email'] ?? '')));
        $password = (string)($this->body['password'] ?? '');
        $name = trim((string)($this->body['display_name'] ?? ''));
        $org = trim((string)($this->body['organization_name'] ?? ''));
        if (!filter_var($email, FILTER_VALIDATE_EMAIL) || strlen($password) < 12 || strlen($password) > 256 || $name === '' || $org === '') {
            $this->fail(422, 'Enter a valid email, a password of at least 12 characters, and both names');
        }
        $id = self::uuid();
        $orgId = self::uuid();
        try {
            $this->db->beginTransaction();
            $s = $this->db->prepare('INSERT INTO users (id,email,display_name,password_hash) VALUES (?,?,?,?)');
            $s->execute([$id, $email, $name, password_hash($password, PASSWORD_DEFAULT)]);
            $this->db->prepare('INSERT INTO organizations (id,name) VALUES (?,?)')->execute([$orgId, $org]);
            $this->db->prepare("INSERT INTO organization_members (organization_id,user_id,role) VALUES (?,?,'owner')")->execute([$orgId, $id]);
            $this->audit($orgId, $id, 'account.registered', 'user', $id, []);
            $this->db->commit();
        } catch (PDOException $e) {
            if ($this->db->inTransaction()) $this->db->rollBack();
            if ((string)$e->getCode() === '23000') $this->fail(409, 'An account with this email already exists');
            throw $e;
        }
        $this->issueSession($id);
        http_response_code(201);
        return ['id' => $id, 'email' => $email, 'display_name' => $name];
    }

    private function login(): array
    {
        $email = strtolower(trim((string)($this->body['email'] ?? '')));
        $s = $this->db->prepare('SELECT id,email,display_name,password_hash,is_active FROM users WHERE email=? LIMIT 1');
        $s->execute([$email]);
        $user = $s->fetch();
        if (!$user || !(bool)$user['is_active'] || !password_verify((string)($this->body['password'] ?? ''), $user['password_hash'])) {
            $this->fail(401, 'Email or password is incorrect');
        }
        $this->issueSession($user['id']);
        return ['id' => $user['id'], 'email' => $user['email'], 'display_name' => $user['display_name']];
    }

    private function issueSession(string $userId): void
    {
        $token = bin2hex(random_bytes(32));
        $hours = max(1, (int)($this->config['session_ttl_hours'] ?? 24));
        $expires = gmdate('Y-m-d H:i:s', time() + 3600 * $hours);
        $s = $this->db->prepare('INSERT INTO user_sessions (token_hash,user_id,expires_at) VALUES (?,?,?)');
        $s->execute([hash('sha256', $token), $userId, $expires]);
        setcookie((string)($this->config['session_cookie_name'] ?? 'patchwork_session'), $token, [
            'expires' => time() + 3600 * $hours, 'path' => '/', 'secure' => true, 'httponly' => true, 'samesite' => 'Lax',
        ]);
    }

    private function logout(): array
    {
        $cookie = (string)($_COOKIE[(string)($this->config['session_cookie_name'] ?? 'patchwork_session')] ?? '');
        if ($cookie !== '') $this->db->prepare('DELETE FROM user_sessions WHERE token_hash=?')->execute([hash('sha256', $cookie)]);
        setcookie((string)($this->config['session_cookie_name'] ?? 'patchwork_session'), '', ['expires' => 1, 'path' => '/', 'secure' => true, 'httponly' => true, 'samesite' => 'Lax']);
        return ['status' => 'signed_out'];
    }

    private function me(): array
    {
        $user = $this->requireUser();
        $s = $this->db->prepare('SELECT o.id,o.name,m.role FROM organization_members m JOIN organizations o ON o.id=m.organization_id WHERE m.user_id=? ORDER BY o.created_at');
        $s->execute([$user['id']]);
        return ['id' => $user['id'], 'email' => $user['email'], 'display_name' => $user['display_name'], 'organizations' => $s->fetchAll()];
    }

    private function requireUser(): array
    {
        if ($this->user) return $this->user;
        $token = (string)($_COOKIE[(string)($this->config['session_cookie_name'] ?? 'patchwork_session')] ?? '');
        if ($token === '') $this->fail(401, 'Sign in is required');
        $s = $this->db->prepare('SELECT u.id,u.email,u.display_name,u.is_active FROM user_sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>UTC_TIMESTAMP() LIMIT 1');
        $s->execute([hash('sha256', $token)]);
        $user = $s->fetch();
        if (!$user || !(bool)$user['is_active']) $this->fail(401, 'Sign in is required');
        return $this->user = $user;
    }

    private function organizations(): array
    {
        $s = $this->db->prepare('SELECT organization_id FROM organization_members WHERE user_id=?');
        $s->execute([$this->requireUser()['id']]);
        return array_column($s->fetchAll(), 'organization_id');
    }

    private function dashboard(): array
    {
        $orgs = $this->organizations();
        $result = ['connected_repositories'=>0,'monitored_repositories'=>0,'failures_detected'=>0,'active_repairs'=>0,'validated_repairs'=>0,'pull_requests_opened'=>0,'needs_human_review'=>0,'cache_hit_rate'=>null,'safe_cache_reuse_rate'=>null,'validation_success_rate'=>null,'model_tokens'=>0,'estimated_tokens_saved'=>0,'estimated_cost'=>null,'average_pr_minutes'=>null,'recent_jobs'=>[]];
        if (!$orgs) return $result;
        $marks = implode(',', array_fill(0, count($orgs), '?'));
        foreach ([
            'connected_repositories' => ['SELECT COUNT(*) FROM repositories WHERE organization_id IN (' . $marks . ')', $orgs],
            'monitored_repositories' => ['SELECT COUNT(*) FROM repositories WHERE monitored=1 AND organization_id IN (' . $marks . ')', $orgs],
            'failures_detected' => ["SELECT COUNT(*) FROM repair_jobs WHERE organization_id IN ($marks)", $orgs],
            'active_repairs' => ["SELECT COUNT(*) FROM repair_jobs WHERE status IN ('RECEIVED','QUEUED','TRIAGING','CONTEXT_GATHERING','CACHE_LOOKUP','PLANNING','PATCH_GENERATION','SANDBOX_VALIDATION','POLICY_CHECK','PULL_REQUEST_CREATION','RETRYING') AND organization_id IN ($marks)", $orgs],
            'validated_repairs' => ["SELECT COUNT(*) FROM repair_jobs WHERE status IN ('PULL_REQUEST_CREATION','COMPLETED') AND organization_id IN ($marks)", $orgs],
            'needs_human_review' => ["SELECT COUNT(*) FROM repair_jobs WHERE status='NEEDS_HUMAN_REVIEW' AND organization_id IN ($marks)", $orgs],
        ] as $key => [$sql, $params]) { $q=$this->db->prepare($sql); $q->execute($params); $result[$key]=(int)$q->fetchColumn(); }
        $s=$this->db->prepare("SELECT r.full_name AS repository,j.id,j.status,j.failure_summary AS summary,j.created_at FROM repair_jobs j JOIN repositories r ON r.id=j.repository_id WHERE j.organization_id IN ($marks) ORDER BY j.created_at DESC LIMIT 8");
        $s->execute($orgs); $result['recent_jobs']=$s->fetchAll();
        $u=$this->db->prepare("SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) FROM model_usage WHERE organization_id IN ($marks)"); $u->execute($orgs); $result['model_tokens']=(int)$u->fetchColumn();
        return $result;
    }

    private function repositories(): array
    {
        $org = (string)($_GET['organization_id'] ?? '');
        if (!in_array($org, $this->organizations(), true)) $this->fail(403, 'Workspace access required');
        $s=$this->db->prepare('SELECT r.id,r.full_name,r.is_private AS private,r.monitored,r.default_branch,COALESCE(s.workflow_run_failure,0) AS workflow_run_failure,COALESCE(s.check_run_failure,0) AS check_run_failure,s.required_checks,s.allowed_paths FROM repositories r LEFT JOIN repository_settings s ON s.repository_id=r.id WHERE r.organization_id=? ORDER BY r.full_name');
        $s->execute([$org]); $rows=$s->fetchAll();
        foreach ($rows as &$row) { $row['private']=(bool)$row['private']; $row['monitored']=(bool)$row['monitored']; $row['triggers']=['workflow_run_failure'=>(bool)$row['workflow_run_failure'],'check_run_failure'=>(bool)$row['check_run_failure']]; $row['required_checks']=json_decode($row['required_checks'] ?: '[]',true) ?: []; $row['allowed_paths']=json_decode($row['allowed_paths'] ?: '[]',true) ?: []; unset($row['workflow_run_failure'],$row['check_run_failure']); }
        return $rows;
    }

    private function repositorySetting(string $id, string $kind): array
    {
        if ($this->method !== 'PATCH') $this->fail(405, 'Method not allowed');
        $repo=$this->repoForUser($id); $org=$repo['organization_id']; $this->ensureAdmin($org);
        $this->db->prepare('INSERT IGNORE INTO repository_settings (repository_id,required_checks,allowed_paths) VALUES (?,JSON_ARRAY(),JSON_ARRAY())')->execute([$id]);
        if ($kind==='monitor') {
            $value=(bool)($this->body['monitored'] ?? false);
            if ($value && empty($this->config['repair_worker_enabled'])) $this->fail(503,'The Hostinger PHP repair worker is not configured yet');
            $this->db->prepare('UPDATE repositories SET monitored=? WHERE id=?')->execute([$value?1:0,$id]);
            if ($value) $this->db->exec("UPDATE repository_settings SET workflow_run_failure=1,check_run_failure=1 WHERE repository_id=".$this->db->quote($id));
            return ['id'=>$id,'monitored'=>$value];
        }
        if ($kind==='triggers') {
            $wf=(bool)($this->body['workflow_run_failure']??false); $check=(bool)($this->body['check_run_failure']??false);
            if (($wf || $check) && empty($this->config['repair_worker_enabled'])) $this->fail(503,'The Hostinger PHP repair worker is not configured yet');
            $this->db->prepare('UPDATE repository_settings SET workflow_run_failure=?,check_run_failure=? WHERE repository_id=?')->execute([$wf?1:0,$check?1:0,$id]);
            return ['id'=>$id,'triggers'=>['workflow_run_failure'=>$wf,'check_run_failure'=>$check]];
        }
        if ($kind==='checks') {
            $checks=$this->body['required_checks']??[]; if (!is_array($checks) || array_diff($checks,['ruff','mypy','build'])) $this->fail(422,'Invalid validation check');
            $this->db->prepare('UPDATE repository_settings SET required_checks=? WHERE repository_id=?')->execute([json_encode(array_values(array_unique($checks)),JSON_THROW_ON_ERROR),$id]);
            return ['id'=>$id,'required_checks'=>array_values(array_unique($checks))];
        }
        $paths=$this->body['allowed_paths']??[]; if (!is_array($paths)||count($paths)>100) $this->fail(422,'Invalid patch scope');
        foreach ($paths as $p) if (!is_string($p)||str_contains($p,'..')||str_starts_with($p,'/')||!str_ends_with($p,'.py')) $this->fail(422,'Patch scope entries must be safe relative Python paths');
        $this->db->prepare('UPDATE repository_settings SET allowed_paths=? WHERE repository_id=?')->execute([json_encode(array_values(array_unique($paths)),JSON_THROW_ON_ERROR),$id]);
        return ['id'=>$id,'allowed_paths'=>array_values(array_unique($paths))];
    }

    private function repairJobs(): array
    {
        $orgs=$this->organizations(); if(!$orgs)return [];
        $marks=implode(',',array_fill(0,count($orgs),'?'));
        $s=$this->db->prepare("SELECT j.id,r.full_name AS repository,j.status,j.failure_category,j.failure_summary,j.commit_sha,j.base_branch,j.workflow_run_id,j.check_run_id,j.retry_count,j.created_at FROM repair_jobs j JOIN repositories r ON r.id=j.repository_id WHERE j.organization_id IN ($marks) ORDER BY j.created_at DESC LIMIT 100");
        $s->execute($orgs); return $s->fetchAll();
    }

    private function manualRepair(string $repositoryId): array
    {
        if (empty($this->config['repair_worker_enabled'])) $this->fail(503, 'The Hostinger PHP repair worker is not configured yet');
        $repo=$this->repoForUser($repositoryId); $this->ensureAdmin($repo['organization_id']);
        $sha=strtolower((string)($this->body['commit_sha']??'')); $category=(string)($this->body['failure_category']??'unknown'); $summary=trim((string)($this->body['failure_summary']??''));
        if(!preg_match('/^[0-9a-f]{40,64}$/',$sha)||strlen($summary)<3||strlen($summary)>5000||!in_array($category,['test','build','lint','dependency','configuration','runtime','unknown'],true))$this->fail(422,'Invalid repair request');
        $id=self::uuid(); $this->db->beginTransaction();
        $this->db->prepare("INSERT INTO repair_jobs (id,organization_id,repository_id,idempotency_key,status,commit_sha,base_branch,failure_category,failure_summary) VALUES (?,?,?,?,'QUEUED',?,?,?,?)")
            ->execute([$id,$repo['organization_id'],$repositoryId,'manual:'.$id,$sha,$repo['default_branch'],$category,'Manually requested repair: '.$summary]);
        $this->db->prepare("INSERT INTO repair_job_events (repair_job_id,from_state,to_state,actor,detail) VALUES (?, 'RECEIVED','QUEUED',?,?)")
            ->execute([$id,'user:'.$this->requireUser()['id'],json_encode(['source'=>'manual_request'])]);
        $this->audit($repo['organization_id'],$this->requireUser()['id'],'repair_job.manual_request_created','repair_job',$id,['repository_id'=>$repositoryId,'failure_category'=>$category]);
        $this->db->commit();
        return ['job_id'=>$id,'queued'=>true];
    }

    private function repoForUser(string $id): array
    {
        $orgs=$this->organizations(); if(!$orgs)$this->fail(404,'Repository not found');
        $marks=implode(',',array_fill(0,count($orgs),'?')); $s=$this->db->prepare("SELECT * FROM repositories WHERE id=? AND organization_id IN ($marks) LIMIT 1"); $s->execute(array_merge([$id],$orgs)); $r=$s->fetch();
        if(!$r)$this->fail(404,'Repository not found'); return $r;
    }

    private function ensureAdmin(string $org): void
    {
        $s=$this->db->prepare("SELECT role FROM organization_members WHERE organization_id=? AND user_id=?"); $s->execute([$org,$this->requireUser()['id']]);
        if(!in_array((string)$s->fetchColumn(),['owner','admin'],true))$this->fail(403,'Organization owner or admin access required');
    }

    private function modelConfig(): array
    {
        $this->requireUser(); $host=parse_url((string)($this->config['nebius_base_url']??''),PHP_URL_HOST);
        return ['provider'=>'Nebius Token Factory','configured'=>!empty($this->config['nebius_api_key'])&& !empty($this->config['nebius_model']),'model_id'=>$this->config['nebius_model']??null,'endpoint_host'=>$host,'input_price_per_million'=>null,'output_price_per_million'=>null];
    }

    private function modelUsage(): array
    {
        $orgs=$this->organizations(); if(!$orgs)return []; $marks=implode(',',array_fill(0,count($orgs),'?'));
        $s=$this->db->prepare("SELECT provider,model_id,prompt_tokens,completion_tokens,estimated_cost,created_at FROM model_usage WHERE organization_id IN ($marks) ORDER BY created_at DESC LIMIT 100"); $s->execute($orgs); return $s->fetchAll();
    }

    private function milisaPatterns(): array
    {
        $orgs=$this->organizations(); if(!$orgs)return []; $marks=implode(',',array_fill(0,count($orgs),'?'));
        $s=$this->db->prepare("SELECT id,failure_category,language,framework,root_cause,verification_status,success_count,reuse_count,last_used_at FROM milisa_patterns WHERE organization_id IN ($marks) ORDER BY created_at DESC LIMIT 200"); $s->execute($orgs); return $s->fetchAll();
    }

    private function milisaPattern(string $id): array
    {
        $orgs=$this->organizations(); if(!$orgs)$this->fail(404,'Pattern not found'); $marks=implode(',',array_fill(0,count($orgs),'?'));
        $s=$this->db->prepare("SELECT * FROM milisa_patterns WHERE id=? AND organization_id IN ($marks) LIMIT 1"); $s->execute(array_merge([$id],$orgs)); $p=$s->fetch();
        if(!$p)$this->fail(404,'Pattern not found');
        $p['patch_template']=$p['patch_template']??'';
        return $p;
    }

    private function deleteMilisaPattern(string $id): array
    {
        $orgs=$this->organizations(); if(!$orgs)$this->fail(404,'Pattern not found'); $marks=implode(',',array_fill(0,count($orgs),'?'));
        $q=$this->db->prepare("SELECT organization_id FROM milisa_patterns WHERE id=? AND organization_id IN ($marks) LIMIT 1"); $q->execute(array_merge([$id],$orgs)); $org=$q->fetchColumn();
        if(!$org)$this->fail(404,'Pattern not found'); $this->ensureAdmin((string)$org);
        $this->db->prepare('DELETE FROM milisa_patterns WHERE id=? AND organization_id=?')->execute([$id,$org]);
        $this->audit((string)$org,$this->requireUser()['id'],'milisa.pattern.invalidated','milisa_pattern',$id,[]);
        return ['deleted'=>true,'id'=>$id];
    }

    private function jobAction(string $id,string $action): array
    {
        $orgs=$this->organizations(); if(!$orgs)$this->fail(404,'Repair job not found'); $marks=implode(',',array_fill(0,count($orgs),'?'));
        $s=$this->db->prepare("SELECT * FROM repair_jobs WHERE id=? AND organization_id IN ($marks) LIMIT 1"); $s->execute(array_merge([$id],$orgs)); $job=$s->fetch();
        if(!$job)$this->fail(404,'Repair job not found'); $this->ensureAdmin($job['organization_id']);
        if($action==='retry-pull-request')$this->fail(409,'Pull request retries require the GitHub Actions worker, which is not configured on this deployment yet');
        $old=(string)$job['status'];
        if($action==='cancel'){
            if(in_array($old,['COMPLETED','CANCELLED','FAILED'],true))$this->fail(409,'This repair can no longer be cancelled');
            $next='CANCELLED'; $this->db->prepare("UPDATE repair_jobs SET status=?,updated_at=UTC_TIMESTAMP() WHERE id=?")->execute([$next,$id]);
        } else {
            if (empty($this->config['repair_worker_enabled'])) $this->fail(503,'The Hostinger PHP repair worker is not configured yet');
            if(!in_array($old,['FAILED','NEEDS_HUMAN_REVIEW','CANCELLED'],true))$this->fail(409,'Only stopped jobs can be queued again');
            $next='QUEUED'; $this->db->prepare("UPDATE repair_jobs SET status=?,retry_count=retry_count+1,error_message=NULL,updated_at=UTC_TIMESTAMP() WHERE id=?")->execute([$next,$id]);
        }
        $this->db->prepare('INSERT INTO repair_job_events (repair_job_id,from_state,to_state,actor,detail) VALUES (?,?,?,?,?)')->execute([$id,$old,$next,'user:'.$this->requireUser()['id'],json_encode(['action'=>$action],JSON_THROW_ON_ERROR)]);
        $this->audit($job['organization_id'],$this->requireUser()['id'],'repair_job.'.$action,'repair_job',$id,[]);
        return ['job_id'=>$id,'status'=>$next,'queued'=>$next==='QUEUED'];
    }

    private function auditLogs(): array
    {
        $orgs=$this->organizations(); if(!$orgs)return []; $marks=implode(',',array_fill(0,count($orgs),'?'));
        $s=$this->db->prepare("SELECT id,action,entity_type,entity_id,created_at FROM audit_logs WHERE organization_id IN ($marks) ORDER BY created_at DESC LIMIT 100"); $s->execute($orgs); return $s->fetchAll();
    }

    private function githubWebhook(): array
    {
        $raw=$this->rawBody; $secret=(string)($this->config['github_webhook_secret']??''); $sig=(string)($_SERVER['HTTP_X_HUB_SIGNATURE_256']??'');
        if($secret===''||!str_starts_with($sig,'sha256=')||!hash_equals('sha256='.hash_hmac('sha256',$raw,$secret),$sig))$this->fail(401,'Invalid webhook signature');
        $delivery=(string)($_SERVER['HTTP_X_GITHUB_DELIVERY']??''); $event=(string)($_SERVER['HTTP_X_GITHUB_EVENT']??'');
        if($delivery===''||strlen($delivery)>128||$event==='')$this->fail(400,'Invalid webhook headers');
        try { $p=json_decode($raw,true,512,JSON_THROW_ON_ERROR); } catch(Throwable) { $this->fail(400,'Invalid webhook JSON'); }
        try { $this->db->prepare('INSERT INTO webhook_events (delivery_id,event_type,payload_digest,outcome) VALUES (?,?,?,?)')->execute([$delivery,$event,hash('sha256',$raw),'received']); }
        catch(PDOException $e) { if((string)$e->getCode()==='23000')return ['accepted'=>true,'duplicate'=>true,'repair_job_id'=>null]; throw $e; }
        $failure=$this->failureFromWebhook($event,$p);
        if($failure===null){$this->db->prepare("UPDATE webhook_events SET outcome='ignored_non_failure' WHERE delivery_id=?")->execute([$delivery]);return ['accepted'=>true,'duplicate'=>false,'repair_job_id'=>null];}
        [$repoNumericId,$sha,$category,$summary,$runId,$checkId]=$failure;
        $repoQ=$this->db->prepare("SELECT r.* FROM repositories r JOIN repository_settings s ON s.repository_id=r.id WHERE r.github_repo_id=? AND r.monitored=1 AND ((?='workflow_run' AND s.workflow_run_failure=1) OR (?='check_run' AND s.check_run_failure=1)) LIMIT 1");
        $repoQ->execute([$repoNumericId,$event,$event]); $repo=$repoQ->fetch();
        if(!$repo){$this->db->prepare("UPDATE webhook_events SET outcome='ignored_unmonitored' WHERE delivery_id=?")->execute([$delivery]);return ['accepted'=>true,'duplicate'=>false,'repair_job_id'=>null];}
        $jobId=self::uuid(); $idempotency='github:'.$delivery;
        try {
            $this->db->beginTransaction();
            $this->db->prepare("INSERT INTO repair_jobs (id,organization_id,repository_id,idempotency_key,status,commit_sha,base_branch,failure_category,failure_summary,workflow_run_id,check_run_id) VALUES (?,?,?,?,'QUEUED',?,?,?,?,?,?)")
                ->execute([$jobId,$repo['organization_id'],$repo['id'],$idempotency,$sha,$repo['default_branch'],$category,$summary,$runId,$checkId]);
            $this->db->prepare("INSERT INTO repair_job_events (repair_job_id,from_state,to_state,actor,detail) VALUES (?,NULL,'QUEUED','github_webhook',?)")
                ->execute([$jobId,json_encode(['delivery_id'=>$delivery,'event'=>$event],JSON_THROW_ON_ERROR)]);
            $this->db->prepare("UPDATE webhook_events SET outcome='queued' WHERE delivery_id=?")->execute([$delivery]);
            $this->db->commit();
        } catch(Throwable $e) { if($this->db->inTransaction())$this->db->rollBack(); throw $e; }
        return ['accepted'=>true,'duplicate'=>false,'repair_job_id'=>$jobId];
    }

    private function failureFromWebhook(string $event, array $payload): ?array
    {
        $repo=(int)($payload['repository']['id']??0); if($repo<=0)return null;
        if($event==='workflow_run'){
            $run=$payload['workflow_run']??[];
            if(($payload['action']??'')!=='completed'||($run['conclusion']??'')!=='failure')return null;
            $sha=strtolower((string)($run['head_sha']??'')); if(!preg_match('/^[0-9a-f]{40,64}$/',$sha))return null;
            $name=substr((string)($run['name']??'workflow'),0,180); $url=(string)($run['html_url']??'');
            return [$repo,$sha,'build','Workflow failed: '.$name.($url!==''?' ('.$url.')':''),(int)($run['id']??0)?:null,null];
        }
        if($event==='check_run'){
            $check=$payload['check_run']??[];
            if(($payload['action']??'')!=='completed'||($check['conclusion']??'')!=='failure')return null;
            $sha=strtolower((string)($check['head_sha']??'')); if(!preg_match('/^[0-9a-f]{40,64}$/',$sha))return null;
            $name=substr((string)($check['name']??'check'),0,180); $url=(string)($check['html_url']??'');
            return [$repo,$sha,'test','Check failed: '.$name.($url!==''?' ('.$url.')':''),null,(int)($check['id']??0)?:null];
        }
        return null;
    }

    private function startGithubInstall(): array
    {
        $user=$this->requireUser(); $org=(string)($this->body['organization_id']??''); $this->ensureAdmin($org);
        foreach(['github_app_slug','github_client_id','github_client_secret','github_callback_url','github_app_id'] as $key) if(empty($this->config[$key]))$this->fail(503,'GitHub App OAuth configuration is incomplete');
        $state=bin2hex(random_bytes(32)); $verifier=rtrim(strtr(base64_encode(random_bytes(48)),'+/','-_'),'=');
        $this->db->prepare('INSERT INTO github_oauth_states (state_hash,user_id,organization_id,verifier,expires_at) VALUES (?,?,?,?,DATE_ADD(UTC_TIMESTAMP(), INTERVAL 10 MINUTE))')->execute([hash('sha256',$state),$user['id'],$org,$verifier]);
        $url='https://github.com/apps/'.rawurlencode((string)$this->config['github_app_slug']).'/installations/new?'.http_build_query(['state'=>$state]);
        return ['installation_url'=>$url];
    }

    private function githubSetup(): RedirectResponse
    {
        $installation=(int)($_GET['installation_id']??0); $state=(string)($_GET['state']??''); $user=$this->requireUser();
        $s=$this->db->prepare('SELECT * FROM github_oauth_states WHERE state_hash=? AND user_id=? AND expires_at>UTC_TIMESTAMP() AND consumed_at IS NULL LIMIT 1');
        $s->execute([hash('sha256',$state),$user['id']]); $row=$s->fetch(); if(!$row||$installation<=0)$this->fail(400,'GitHub connection state is invalid or expired');
        $this->db->prepare('UPDATE github_oauth_states SET installation_id=? WHERE state_hash=?')->execute([$installation,hash('sha256',$state)]);
        $challenge=rtrim(strtr(base64_encode(hash('sha256',$row['verifier'],true)),'+/','-_'),'=');
        $query=http_build_query(['client_id'=>$this->config['github_client_id'],'redirect_uri'=>$this->config['github_callback_url'],'state'=>$state,'code_challenge'=>$challenge,'code_challenge_method'=>'S256']);
        return new RedirectResponse('https://github.com/login/oauth/authorize?'.$query,303);
    }

    private function githubCallback(): RedirectResponse
    {
        $code=(string)($_GET['code']??''); $state=(string)($_GET['state']??''); $user=$this->requireUser();
        if($code===''||$state==='')$this->fail(400,'GitHub authorization response is incomplete');
        $s=$this->db->prepare('SELECT * FROM github_oauth_states WHERE state_hash=? AND user_id=? AND expires_at>UTC_TIMESTAMP() AND consumed_at IS NULL LIMIT 1');
        $s->execute([hash('sha256',$state),$user['id']]); $row=$s->fetch();
        if(!$row||empty($row['installation_id']))$this->fail(400,'GitHub connection state is invalid or expired');
        $this->ensureAdmin($row['organization_id']);
        $this->db->prepare('UPDATE github_oauth_states SET consumed_at=UTC_TIMESTAMP() WHERE state_hash=?')->execute([hash('sha256',$state)]);
        $tokenResponse=$this->githubRequest('POST','https://github.com/login/oauth/access_token',null,[
            'client_id'=>$this->config['github_client_id'],'client_secret'=>$this->config['github_client_secret'],
            'code'=>$code,'redirect_uri'=>$this->config['github_callback_url'],'code_verifier'=>$row['verifier'],
        ],true);
        $userToken=(string)($tokenResponse['access_token']??'');
        if($userToken==='')$this->fail(502,'GitHub did not return an OAuth token');
        $installId=(int)$row['installation_id'];
        $installs=$this->githubList('https://api.github.com/user/installations?per_page=100',$userToken);
        $installation=null;
        foreach(($installs['installations']??[]) as $candidate)if((int)($candidate['id']??0)===$installId){$installation=$candidate;break;}
        if(!$installation)$this->fail(403,'The signed-in GitHub user cannot access this App installation');
        $repos=$this->githubList('https://api.github.com/user/installations/'.$installId.'/repositories?per_page=100',$userToken);
        $account=$installation['account']??[]; $login=(string)($account['login']??''); $type=(string)($account['type']??'');
        if($login===''||$type==='')$this->fail(502,'GitHub installation account data is incomplete');
        $this->db->beginTransaction();
        try {
            $q=$this->db->prepare('SELECT organization_id FROM github_installations WHERE installation_id=? LIMIT 1');$q->execute([$installId]);$oldOrg=$q->fetchColumn();
            if($oldOrg && $oldOrg!==$row['organization_id'])$this->fail(409,'This GitHub installation is already linked to another workspace');
            $this->db->prepare('INSERT INTO github_installations (installation_id,organization_id,account_login,account_type,suspended) VALUES (?,?,?,?,?) ON DUPLICATE KEY UPDATE account_login=VALUES(account_login),account_type=VALUES(account_type),suspended=VALUES(suspended)')
                ->execute([$installId,$row['organization_id'],$login,$type,!empty($installation['suspended_at'])?1:0]);
            foreach(($repos['repositories']??[]) as $repo){
                $repoId=(int)($repo['id']??0); if($repoId<=0)continue;
                $name=(string)($repo['full_name']??$repo['name']??$repoId); $branch=(string)($repo['default_branch']??'main'); $private=!empty($repo['private'])?1:0;
                $repoUuid=self::uuid();
                $this->db->prepare('INSERT INTO repositories (id,organization_id,installation_id,github_repo_id,full_name,default_branch,is_private,monitored) VALUES (?,?,?,?,?,?,?,0) ON DUPLICATE KEY UPDATE full_name=VALUES(full_name),default_branch=VALUES(default_branch),is_private=VALUES(is_private)')
                    ->execute([$repoUuid,$row['organization_id'],$installId,$repoId,$name,$branch,$private]);
            }
            $this->audit($row['organization_id'],$user['id'],'github.installation.connected','github_installation',(string)$installId,[]);
            $this->db->commit();
        } catch(Throwable $e) { if($this->db->inTransaction())$this->db->rollBack(); throw $e; }
        return new RedirectResponse(rtrim((string)$this->config['frontend_origin'],'/').'/?github=connected',303);
    }

    private function githubRequest(string $method,string $url,?string $token,?array $body=null,bool $oauth=false): array
    {
        $ch=curl_init($url); if($ch===false)$this->fail(502,'Could not initialize GitHub request');
        $headers=$oauth?['Accept: application/json','Content-Type: application/x-www-form-urlencoded']:['Accept: application/vnd.github+json','X-GitHub-Api-Version: 2026-03-10'];
        if($token!==null)$headers[]='Authorization: Bearer '.$token;
        curl_setopt_array($ch,[CURLOPT_RETURNTRANSFER=>true,CURLOPT_FOLLOWLOCATION=>false,CURLOPT_CONNECTTIMEOUT=>5,CURLOPT_TIMEOUT=>20,CURLOPT_CUSTOMREQUEST=>$method,CURLOPT_HTTPHEADER=>$headers]);
        if($body!==null)curl_setopt($ch,CURLOPT_POSTFIELDS,$oauth?http_build_query($body):json_encode($body,JSON_THROW_ON_ERROR));
        $raw=curl_exec($ch); $status=(int)curl_getinfo($ch,CURLINFO_RESPONSE_CODE); $error=curl_errno($ch); curl_close($ch);
        if($raw===false||$error!==0)$this->fail(502,'GitHub could not be reached');
        $json=json_decode((string)$raw,true);
        if($status<200||$status>=300||!is_array($json))$this->fail(502,'GitHub rejected the connection request');
        return $json;
    }

    private function githubList(string $url,string $token): array
    {
        $result=[];
        for($page=1;$page<=10;$page++){
            $separator=str_contains($url,'?')?'&':'?';
            $data=$this->githubRequest('GET',$url.$separator.'page='.$page,$token);
            foreach($data as $key=>$value)if(is_array($value))$result[$key]=array_merge($result[$key]??[],$value);
            $arrays=array_filter($data,'is_array'); $items=$arrays?reset($arrays):[];
            if(count($items)<100)break;
        }
        return $result;
    }

    private function repairJob(string $id): array
    {
        $orgs=$this->organizations(); if(!$orgs)$this->fail(404,'Repair job not found'); $marks=implode(',',array_fill(0,count($orgs),'?'));
        $s=$this->db->prepare("SELECT j.*,r.full_name AS repository FROM repair_jobs j JOIN repositories r ON r.id=j.repository_id WHERE j.id=? AND j.organization_id IN ($marks) LIMIT 1"); $s->execute(array_merge([$id],$orgs)); $job=$s->fetch(); if(!$job)$this->fail(404,'Repair job not found');
        $e=$this->db->prepare('SELECT from_state,to_state,actor,detail,created_at FROM repair_job_events WHERE repair_job_id=? ORDER BY id'); $e->execute([$id]); $events=$e->fetchAll(); foreach($events as &$event)$event['detail']=json_decode($event['detail']?:'{}',true)?:[];
        return $job+['pull_request'=>null,'events'=>$events,'agent_runs'=>[],'patches'=>[],'validation_runs'=>[]];
    }

    private function audit(string $org, ?string $user, string $action, string $type, ?string $id, array $details): void
    {
        $this->db->prepare('INSERT INTO audit_logs (organization_id,user_id,action,entity_type,entity_id,details) VALUES (?,?,?,?,?,?)')->execute([$org,$user,$action,$type,$id,json_encode($details,JSON_THROW_ON_ERROR)]);
    }

    private function json(mixed $data, int $status = 200): never
    {
        http_response_code($status); header('Content-Type: application/json; charset=utf-8');
        echo json_encode($data, JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE); exit;
    }

    private function fail(int $status, string $message): never { throw new ApiError($status,$message); }
    private static function uuid(): string { $b=random_bytes(16); $b[6]=chr((ord($b[6])&0x0f)|0x40); $b[8]=chr((ord($b[8])&0x3f)|0x80); return vsprintf('%s%s-%s-%s-%s-%s%s%s',str_split(bin2hex($b),4)); }
}

final class ApiError extends RuntimeException { public function __construct(public int $status, string $message) { parent::__construct($message); } }
final class RedirectResponse { public function __construct(public string $url, public int $status=303) {} }
