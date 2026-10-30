# Hostinger Web Hosting deployment

This guide targets Hostinger Web/Business/Cloud shared hosting, without a VPS. The frontend is a static Vite build, and the replacement API is PHP with MySQL. The original FastAPI/PostgreSQL/RQ/Docker backend remains for local development and is not deployable on this plan. Hostinger's support pages describe Python as VPS-only and Node.js as unavailable on Web/Cloud plans. A shared plan cannot run the existing Docker sandbox. The PHP API requires PHP 8.1 or newer with PDO MySQL, cURL, and OpenSSL enabled.

## Current deployment boundary

The PHP API currently supports account registration and login, dashboard queries, GitHub App installation/OAuth, repository settings, signed webhook verification, idempotent failure ingestion, repair-job listing/cancellation, model configuration/usage and Milisa pattern browsing. It records eligible failed workflow/check events as queued jobs. Manual repair and retry queue endpoints stay disabled until the worker is ready; PR retry reports that it requires the missing worker.

The PHP cron worker that calls Nemotron, generates a scoped patch, runs isolated validation, and opens a draft pull request has **not** been ported yet. The old Python worker performs those steps but cannot run on Hostinger Web. Until that worker is implemented and connected to a GitHub Actions validation runner, this is a deployable dashboard/API preview, not a working self-healing repair service. Do not enable failure triggers for a live repository expecting automatic repair yet. The status endpoint reports the configured integrations; it does not imply that a repair worker is running..

The app still calls Nebius Token Factory from the server. Hostinger hosting does not include inference credits; configure a valid Token Factory key and check account credits/billing before enabling requests. Keep that key out of frontend variables and source control.

Millisa Gateway is integrated with the Python/RQ worker for Coder calls, while the Repo Architect remains on Nebius Nemotron. The current PHP/MySQL Hostinger preview does not include that worker yet, so it will not call Millisa until the PHP worker port is completed.

## DNS and database

Use HTTPS on the frontend and API. A simple layout is `https://app.example.com` for the static dashboard and `https://api.example.com` for PHP. They should be sibling subdomains. Create a MySQL database and user in hPanel and import `hostinger-backend/schema.sql` using phpMyAdmin. Keep the database credentials private.

## Build and upload the frontend

From `frontend`, build with the public API origin. The API URL is public configuration; never put API keys or GitHub credentials in `VITE_*` variables.

```powershell
$env:VITE_API_URL = "https://api.example.com"
npm.cmd ci
npm.cmd run build
```

Upload the **contents** of `frontend/dist` to the Hostinger frontend website's document root (usually `public_html`). The build includes an Apache `.htaccess` with SPA fallback and static-asset caching.

## Upload the PHP API

Create the API website/subdomain in hPanel with a document root pointing to `hostinger-backend/public` if the plan permits selecting a subdirectory. Otherwise, put the contents of `hostinger-backend/public` in the API document root and place `src` and `config.php` in a sibling directory outside the public document root. Never expose `config.php`, the App private key, or `.env` through HTTP.

Copy `hostinger-backend/config.example.php` to `config.php` outside the document root and set:

- `frontend_origin` and `api_origin` to the exact HTTPS origins.
- `database_dsn`, `database_user`, and `database_password` to the MySQL database created in hPanel.
- GitHub App ID, slug, OAuth client ID/secret, callback URL, webhook secret, and private-key path.
- Nebius Token Factory base URL, API key, and the exact NVIDIA model ID enabled for the key.

Set the server environment variable `HOSTINGER_APP_CONFIG` to the absolute path of this private config file if it is not at `hostinger-backend/config.php` relative to the API document root. PHP must have PDO MySQL, cURL, and OpenSSL enabled. Point the GitHub App callback/setup/webhook URLs to the API domain:

- Homepage: `https://app.example.com/`
- Callback: `https://api.example.com/api/v1/github/callback`
- Setup URL: `https://api.example.com/api/v1/github/setup`
- Webhook: `https://api.example.com/api/v1/webhooks/github`

The webhook App should subscribe to `workflow_run` and `check_run`. Use a fresh webhook secret and rotate credentials if they have been shared in chat or committed. The PHP API checks `X-Hub-Signature-256` before recording a webhook.

## Verify the preview deployment

1. Open `https://api.example.com/health` and confirm it returns `{"status":"ok"}`.
2. Open the frontend, register a workspace, then sign out/in to confirm the secure session cookie works.
3. Connect the GitHub App, install it on a test repository, and confirm the repository list loads.
4. Keep repository monitoring/failure triggers off until the PHP worker and Actions validation runner are implemented.

## Remaining work for automatic repairs

1. Port the Python/RQ job worker to PHP CLI and schedule it with hPanel Cron Jobs.
2. Add the GitHub App token flow needed for repository reads and branch/PR writes.
3. Use Nebius Token Factory for bounded patch generation with the configured path allowlist.
4. Dispatch tests to GitHub Actions with read-only test permissions and receive signed results back at the API.
5. Create a draft PR only after checks and policy validation pass; never auto-merge.
6. Exercise the complete failure-to-draft-PR flow against a dedicated test repository.

Hostinger supports cron jobs through hPanel; a cron schedule is suitable for a PHP CLI worker once that worker exists. It cannot replace the isolated Docker environment. Do not run untrusted repository code in PHP/API requests or give a test job GitHub write credentials.
