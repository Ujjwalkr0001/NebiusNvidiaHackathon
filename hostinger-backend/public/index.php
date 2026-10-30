<?php
declare(strict_types=1);

$stage = 'load_api_source';
try {
    $apiSource = is_file(__DIR__ . '/src/Api.php') ? __DIR__ . '/src/Api.php' : __DIR__ . '/../src/Api.php';
    require_once $apiSource;
    $stage = 'find_private_config';
    $configuredPath = getenv('HOSTINGER_APP_CONFIG') ?: '';
    $configCandidates = array_filter([
        $configuredPath,
        // Protected by the document-root .htaccess when deployed by the file sender.
        dirname(__DIR__) . '/private-settings.php',
        // When deployed below public_html/api, keep config.php above the document root.
        dirname(__DIR__, 2) . '/config.php',
        // Existing layout: hostinger-backend/config.php beside public/ and src/.
        dirname(__DIR__) . '/config.php',
    ]);
    $configPath = '';
    foreach ($configCandidates as $candidate) {
        if (is_file($candidate)) {
            $configPath = $candidate;
            break;
        }
    }
    if ($configPath === '') {
        http_response_code(503);
        header('Content-Type: application/json; charset=utf-8');
        echo json_encode(['detail' => 'PHP app config is missing outside the public document root']);
        exit;
    }

    $stage = 'load_private_config';
    $config = require $configPath;
    $stage = 'validate_private_config';
    if (!is_array($config)) {
        http_response_code(503);
        header('Content-Type: application/json; charset=utf-8');
        echo json_encode(['detail' => 'PHP app config must return a PHP array']);
        exit;
    }

    $stage = 'initialize_api';
    (new Api($config))->run();
} catch (Throwable $error) {
    $status = $error instanceof ApiError ? $error->status : 500;
    $message = $error instanceof ApiError
        ? $error->getMessage()
        : 'API startup failed during ' . $stage . '; check the PHP error log';
    error_log('Hostinger API bootstrap failed during ' . $stage . ': ' . get_class($error));
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode(['detail' => $message], JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE);
}
