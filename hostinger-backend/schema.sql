CREATE TABLE IF NOT EXISTS users (
  id CHAR(36) PRIMARY KEY,
  email VARCHAR(320) NOT NULL UNIQUE,
  display_name VARCHAR(120) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS organizations (
  id CHAR(36) PRIMARY KEY,
  name VARCHAR(160) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS organization_members (
  organization_id CHAR(36) NOT NULL,
  user_id CHAR(36) NOT NULL,
  role VARCHAR(30) NOT NULL DEFAULT 'member',
  PRIMARY KEY (organization_id, user_id),
  KEY ix_members_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_sessions (
  token_hash CHAR(64) PRIMARY KEY,
  user_id CHAR(36) NOT NULL,
  expires_at DATETIME NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_sessions_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS github_oauth_states (
  state_hash CHAR(64) PRIMARY KEY,
  user_id CHAR(36) NOT NULL,
  organization_id CHAR(36) NOT NULL,
  verifier TEXT NOT NULL,
  installation_id BIGINT NULL,
  expires_at DATETIME NOT NULL,
  consumed_at DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS github_installations (
  installation_id BIGINT PRIMARY KEY,
  organization_id CHAR(36) NOT NULL,
  account_login VARCHAR(255) NOT NULL,
  account_type VARCHAR(30) NOT NULL,
  suspended TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS repositories (
  id CHAR(36) PRIMARY KEY,
  organization_id CHAR(36) NOT NULL,
  installation_id BIGINT NOT NULL,
  github_repo_id BIGINT NOT NULL,
  full_name VARCHAR(512) NOT NULL,
  default_branch VARCHAR(255) NOT NULL,
  is_private TINYINT(1) NOT NULL DEFAULT 1,
  monitored TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_github_repo (installation_id, github_repo_id),
  KEY ix_repositories_org (organization_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS repository_settings (
  repository_id CHAR(36) PRIMARY KEY,
  workflow_run_failure TINYINT(1) NOT NULL DEFAULT 0,
  check_run_failure TINYINT(1) NOT NULL DEFAULT 0,
  required_checks JSON NOT NULL,
  allowed_paths JSON NOT NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS repair_jobs (
  id CHAR(36) PRIMARY KEY,
  organization_id CHAR(36) NOT NULL,
  repository_id CHAR(36) NOT NULL,
  idempotency_key VARCHAR(255) NOT NULL UNIQUE,
  status VARCHAR(40) NOT NULL DEFAULT 'RECEIVED',
  commit_sha CHAR(64) NOT NULL,
  base_branch VARCHAR(255) NOT NULL,
  failure_category VARCHAR(60) NOT NULL,
  failure_summary TEXT NOT NULL,
  workflow_run_id BIGINT NULL,
  check_run_id BIGINT NULL,
  retry_count INT NOT NULL DEFAULT 0,
  error_message TEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY ix_jobs_org_created (organization_id, created_at),
  KEY ix_jobs_queue (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS repair_job_events (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  repair_job_id CHAR(36) NOT NULL,
  from_state VARCHAR(40) NULL,
  to_state VARCHAR(40) NOT NULL,
  actor VARCHAR(120) NOT NULL,
  detail JSON NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_job_events (repair_job_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS webhook_events (
  delivery_id VARCHAR(128) PRIMARY KEY,
  event_type VARCHAR(100) NOT NULL,
  payload_digest CHAR(64) NOT NULL,
  outcome VARCHAR(40) NOT NULL DEFAULT 'received',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS audit_logs (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  organization_id CHAR(36) NOT NULL,
  user_id CHAR(36) NULL,
  action VARCHAR(160) NOT NULL,
  entity_type VARCHAR(100) NOT NULL,
  entity_id VARCHAR(160) NULL,
  details JSON NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_audit_org (organization_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS model_usage (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  organization_id CHAR(36) NOT NULL,
  repair_job_id CHAR(36) NULL,
  provider VARCHAR(100) NOT NULL,
  model_id VARCHAR(255) NOT NULL,
  prompt_tokens INT UNSIGNED NOT NULL DEFAULT 0,
  completion_tokens INT UNSIGNED NOT NULL DEFAULT 0,
  estimated_cost DECIMAL(14,8) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_model_usage_org (organization_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS milisa_patterns (
  id CHAR(36) PRIMARY KEY,
  organization_id CHAR(36) NOT NULL,
  failure_category VARCHAR(60) NOT NULL,
  language VARCHAR(80) NOT NULL DEFAULT 'python',
  framework VARCHAR(100) NULL,
  root_cause TEXT NOT NULL,
  patch_template MEDIUMTEXT NOT NULL,
  verification_status VARCHAR(40) NOT NULL,
  success_count INT NOT NULL DEFAULT 0,
  reuse_count INT NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_used_at DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
