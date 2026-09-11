# LovePDF Compressor API — Render + FastAPI

A public PDF compression API that can be called from JavaScript, PHP/cURL, Python, Blogger, WordPress, Wix, mobile apps, or other servers.

## API endpoints

- `GET /`
- `GET /health`
- `GET /docs` — interactive Swagger UI
- `POST /compress`

## POST /compress

Multipart form fields:

- `file` — PDF file
- `target_kb` — requested final size in KB
- `exact_size` — `true` by default

Optional request header:

- `X-API-Key: YOUR_SECRET_KEY`

The response is the compressed PDF itself.

Useful response headers:

- `X-Original-Bytes`
- `X-Compressed-Bytes`
- `X-Target-Bytes`
- `X-Target-Reached`
- `X-Compression-Mode`

## Important behavior

The service compresses using Ghostscript. When it can get at or below the requested target, it pads the trailing PDF bytes so the downloadable file size equals `target_kb * 1024` bytes exactly. The padding is a harmless trailing PDF comment and does not improve image quality; image quality is determined by the closest compression pass.

Some PDFs have structural/content limits and may not reach extremely small targets. In that case the API returns the smallest successful result and sets:

`X-Target-Reached: false`

## Deploy to Render

### 1. Put these files in a GitHub repository

At minimum:

- `app.py`
- `requirements.txt`
- `Dockerfile`
- `render.yaml`

### 2. Create the Render service

Option A — Blueprint:

1. Render Dashboard
2. New
3. Blueprint
4. Connect the GitHub repository
5. Render reads `render.yaml`
6. Deploy

Option B — Web Service:

1. New Web Service
2. Connect the repository
3. Runtime: Docker
4. Choose Free plan
5. Deploy

### 3. Optional environment variables

`APP_NAME`
: Display name.

`MAX_FILE_MB`
: Maximum upload size. Default `50`.

`ALLOWED_ORIGINS`
: `*` for all front-end sites, or comma-separated origins such as:
`https://www.example.com,https://example.blogspot.com`

`API_KEY`
: Optional secret. If set, every `/compress` request must include `X-API-Key`.

Do not put a valuable secret API key directly in public Blogger/Wix/browser JavaScript. Browser code is visible to visitors. If you need private authentication, call this API from your own backend.

## Example URLs after deployment

If Render assigns:

`https://lovepdf-compressor-api.onrender.com`

then:

- Health: `https://lovepdf-compressor-api.onrender.com/health`
- Swagger: `https://lovepdf-compressor-api.onrender.com/docs`
- Compression: `https://lovepdf-compressor-api.onrender.com/compress`

## JavaScript / Blogger / Wix / WordPress front-end

See `clients/javascript.html`.

Replace:

`https://YOUR-RENDER-SERVICE.onrender.com`

with your actual Render URL.

Blogger: place the HTML/JS in an HTML/JavaScript gadget, theme, or page that permits scripts.

WordPress: use a Custom HTML block or enqueue your script in a theme/plugin. Some hosted WordPress plans may strip scripts from post content.

Wix: use custom code/Velo or an HTML embed depending on your site setup.

## PHP

See `clients/php.php`.

Your PHP server sends the PDF to the Render API using cURL and streams the returned PDF to the visitor.

## cURL

See `clients/curl.txt`.

## Python

See `clients/python.py`.

Install dependency:

`pip install requests`

## Local test

Build:

`docker build -t lovepdf-api .`

Run:

`docker run --rm -p 10000:10000 lovepdf-api`

Open:

`http://localhost:10000/docs`

## Security suggestions

For public browser integrations, use rate limiting/proxy protection if the service becomes popular. A static API key inside JavaScript is not secret.

For production use, consider:
- restricting `ALLOWED_ORIGINS`
- adding rate limits
- adding Cloudflare or another reverse proxy
- logging only metadata, not PDF content
- deleting temporary files immediately after each response (this project already does so)
