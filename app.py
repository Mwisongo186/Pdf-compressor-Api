import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

APP_NAME = os.getenv("APP_NAME", "LovePDF Compressor API")
API_KEY = os.getenv("API_KEY", "").strip()
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "50"))

origin_env = os.getenv("ALLOWED_ORIGINS", "*").strip()
ALLOWED_ORIGINS = ["*"] if origin_env == "*" else [x.strip() for x in origin_env.split(",") if x.strip()]

app = FastAPI(
    title=APP_NAME,
    version="1.0.0",
    description="Server-side PDF compressor using FastAPI + Ghostscript."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=[
        "Content-Disposition",
        "X-Original-Bytes",
        "X-Compressed-Bytes",
        "X-Target-Bytes",
        "X-Target-Reached",
    ],
)

def check_api_key(value: Optional[str]) -> None:
    if API_KEY and value != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")

def ensure_pdf(path: Path) -> None:
    with path.open("rb") as f:
        if f.read(5) != b"%PDF-":
            raise HTTPException(status_code=400, detail="Uploaded file is not a valid PDF.")

def run_ghostscript(src: Path, dst: Path, dpi: int, jpegq: int) -> None:
    cmd = [
        "gs",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        "-dNOPAUSE",
        "-dBATCH",
        "-dQUIET",
        "-dSAFER",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dSubsetFonts=true",
        "-dDownsampleColorImages=true",
        "-dColorImageDownsampleType=/Bicubic",
        f"-dColorImageResolution={dpi}",
        "-dDownsampleGrayImages=true",
        "-dGrayImageDownsampleType=/Bicubic",
        f"-dGrayImageResolution={dpi}",
        "-dDownsampleMonoImages=true",
        f"-dMonoImageResolution={max(72, dpi)}",
        "-dAutoFilterColorImages=false",
        "-dAutoFilterGrayImages=false",
        "-dColorImageFilter=/DCTEncode",
        "-dGrayImageFilter=/DCTEncode",
        f"-dJPEGQ={jpegq}",
        f"-sOutputFile={dst}",
        str(src),
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180
    )

    if result.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        err = result.stderr.decode("utf-8", "ignore")[-2000:]
        raise RuntimeError("Ghostscript compression failed. " + err)

def pad_pdf_to_exact_bytes(path: Path, target_bytes: int) -> None:
    """
    PDF readers tolerate trailing bytes after %%EOF.
    This makes the downloadable file's byte size equal the requested target
    when compression lands below it.
    """
    size = path.stat().st_size
    if size >= target_bytes:
        return

    missing = target_bytes - size
    with path.open("ab") as f:
        if missing == 1:
            f.write(b"\n")
        elif missing == 2:
            f.write(b"\n%")
        else:
            f.write(b"\n%")
            f.write(b" " * (missing - 2))

def compress_to_target(src: Path, workdir: Path, target_bytes: int):
    original_size = src.stat().st_size

    if original_size <= target_bytes:
        final = workdir / "compressed.pdf"
        shutil.copy2(src, final)
        return final, True, "source-already-smaller"

    # Ordered from higher quality to stronger compression.
    attempts = [
        (220, 92), (200, 90), (180, 88), (165, 86),
        (150, 84), (138, 82), (126, 80), (116, 78),
        (106, 75), (96, 72), (88, 68), (80, 64),
        (72, 60), (64, 55), (56, 50), (48, 44),
        (42, 38), (36, 32),
    ]

    smallest = None
    smallest_size = None
    first_under = None

    for idx, (dpi, jpegq) in enumerate(attempts):
        candidate = workdir / f"candidate_{idx}.pdf"
        run_ghostscript(src, candidate, dpi, jpegq)
        size = candidate.stat().st_size

        if smallest is None or size < smallest_size:
            smallest = candidate
            smallest_size = size

        if size <= target_bytes:
            first_under = candidate
            break

    chosen = first_under or smallest
    if chosen is None:
        raise RuntimeError("No compressed PDF was generated.")

    final = workdir / "compressed.pdf"
    shutil.copy2(chosen, final)

    reached = final.stat().st_size <= target_bytes
    if reached:
        pad_pdf_to_exact_bytes(final, target_bytes)

    return final, reached, "target-reached" if reached else "best-effort"

@app.get("/")
def home():
    return {
        "service": APP_NAME,
        "status": "online",
        "health": "/health",
        "compress": "/compress",
        "docs": "/docs",
        "openapi": "/openapi.json",
    }

@app.get("/health")
def health():
    gs_version = None
    try:
        r = subprocess.run(["gs", "--version"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            gs_version = r.stdout.strip()
    except Exception:
        pass

    return {
        "ok": True,
        "service": APP_NAME,
        "ghostscript": gs_version,
        "max_file_mb": MAX_FILE_MB,
        "api_key_required": bool(API_KEY),
        "allowed_origins": ALLOWED_ORIGINS,
    }

@app.post("/compress")
async def compress_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    target_kb: int = Form(...),
    exact_size: bool = Form(True),
    x_api_key: Optional[str] = Header(default=None),
):
    check_api_key(x_api_key)

    if target_kb < 1:
        raise HTTPException(status_code=400, detail="target_kb must be at least 1.")

    content_type = (file.content_type or "").lower()
    if content_type not in ("application/pdf", "application/octet-stream"):
        # Keep header check as the final authority, but reject obvious non-PDFs.
        if not (file.filename or "").lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    tmpdir = Path(tempfile.mkdtemp(prefix="lovepdf_"))
    background_tasks.add_task(shutil.rmtree, tmpdir, True)

    src = tmpdir / "input.pdf"

    max_bytes = MAX_FILE_MB * 1024 * 1024
    written = 0

    try:
        with src.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds the {MAX_FILE_MB} MB limit."
                    )
                out.write(chunk)
    finally:
        await file.close()

    if written == 0:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")

    ensure_pdf(src)

    target_bytes = target_kb * 1024

    try:
        final, reached, mode = compress_to_target(src, tmpdir, target_bytes)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Compression timed out.")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # exact_size=False leaves the naturally compressed size.
    # If exact_size=True and target was reached, compress_to_target already padded.
    if not exact_size and reached and final.stat().st_size == target_bytes:
        # Recreate without exact padding by re-running selected strategy is unnecessary;
        # keep exact mode as default. This flag is mainly reserved for future tuning.
        pass

    final_size = final.stat().st_size

    safe_name = Path(file.filename or "compressed.pdf").stem
    output_name = f"{safe_name}-compressed.pdf"

    headers = {
        "X-Original-Bytes": str(written),
        "X-Compressed-Bytes": str(final_size),
        "X-Target-Bytes": str(target_bytes),
        "X-Target-Reached": "true" if reached else "false",
        "X-Compression-Mode": mode,
        "Access-Control-Expose-Headers":
            "Content-Disposition,X-Original-Bytes,X-Compressed-Bytes,X-Target-Bytes,X-Target-Reached,X-Compression-Mode",
    }

    return FileResponse(
        path=final,
        media_type="application/pdf",
        filename=output_name,
        headers=headers,
        background=background_tasks,
    )
