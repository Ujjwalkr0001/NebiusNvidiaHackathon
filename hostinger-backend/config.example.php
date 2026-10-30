<?php
// Copy to config.php and keep it outside public_html on the Hostinger account.
return [
    'app_env' => 'production',
    'frontend_origin' => 'https://app.example.com',
    'api_origin' => 'https://api.example.com',
    'database_dsn' => 'mysql:host=localhost;dbname=patchwork;charset=utf8mb4',
    'database_user' => 'replace-me',
    'database_password' => 'replace-me',
    'session_cookie_name' => 'patchwork_session',
    'session_ttl_hours' => 24,
    'github_app_id' => '',
    'github_app_slug' => '',
    'github_client_id' => '',
    'github_client_secret' => '',
    'github_callback_url' => 'https://api.example.com/api/v1/github/callback',
    'github_webhook_secret' => '',
    'github_private_key_path' => '/home/USER/private/github-app.pem',
    'nebius_base_url' => 'https://api.tokenfactory.nebius.com/v1/',
    'nebius_api_key' => '',
    'nebius_model' => 'nvidia/nemotron-3-super-120b-a12b',
    'worker_secret' => '',
    'repair_worker_enabled' => false,
];
