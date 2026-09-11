<?php
$apiUrl = 'https://YOUR-RENDER-SERVICE.onrender.com/compress';
$apiKey = ''; // Optional. Same value as Render API_KEY.

if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_FILES['pdf'])) {
    $targetKb = isset($_POST['target_kb']) ? (int)$_POST['target_kb'] : 500;

    $ch = curl_init($apiUrl);

    $post = [
        'file' => new CURLFile(
            $_FILES['pdf']['tmp_name'],
            'application/pdf',
            $_FILES['pdf']['name']
        ),
        'target_kb' => $targetKb,
        'exact_size' => 'true'
    ];

    $headers = [];
    if ($apiKey !== '') {
        $headers[] = 'X-API-Key: ' . $apiKey;
    }

    curl_setopt_array($ch, [
        CURLOPT_POST => true,
        CURLOPT_POSTFIELDS => $post,
        CURLOPT_HTTPHEADER => $headers,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HEADER => true,
        CURLOPT_TIMEOUT => 240
    ]);

    $response = curl_exec($ch);

    if ($response === false) {
        http_response_code(500);
        exit('cURL error: ' . curl_error($ch));
    }

    $status = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $headerSize = curl_getinfo($ch, CURLINFO_HEADER_SIZE);
    $body = substr($response, $headerSize);
    curl_close($ch);

    if ($status !== 200) {
        http_response_code($status);
        header('Content-Type: application/json');
        echo $body;
        exit;
    }

    $filename = pathinfo($_FILES['pdf']['name'], PATHINFO_FILENAME) . '-compressed.pdf';

    header('Content-Type: application/pdf');
    header('Content-Disposition: attachment; filename="' . $filename . '"');
    header('Content-Length: ' . strlen($body));
    echo $body;
    exit;
}
?>
<!doctype html>
<html>
<body>
<form method="post" enctype="multipart/form-data">
    <input type="file" name="pdf" accept="application/pdf" required>
    <input type="number" name="target_kb" value="500" min="1" required>
    <button type="submit">Compress</button>
</form>
</body>
</html>
