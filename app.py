"""Private YouTube-to-R2 cache controller.

The controller is intended to sit behind Cloudflare Access.  Video delivery is
performed separately by an R2 custom domain, so this process never proxies
large media files.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from flask import Flask, jsonify, redirect, render_template, request

app = Flask(__name__)

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://video.example.com/videos").rstrip("/")
R2_REMOTE = os.environ.get("R2_REMOTE", "r2:mybucket/videos").rstrip("/")
WORK_DIR = Path(os.environ.get("WORK_DIR", "/var/tmp/ytcache"))
YTDLP_BIN = os.environ.get("YTDLP_BIN", "yt-dlp")
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
RCLONE_BIN = os.environ.get("RCLONE_BIN", "rclone")
MAX_WORKERS = max(1, int(os.environ.get("MAX_WORKERS", "1")))
MAX_PENDING = max(MAX_WORKERS, int(os.environ.get("MAX_PENDING", "3")))
MAX_DURATION_SECONDS = max(0, int(os.environ.get("MAX_DURATION_SECONDS", "5400")))
MAX_OUTPUT_BYTES = max(0, int(os.environ.get("MAX_OUTPUT_BYTES", str(8 * 1024**3))))
MIN_FREE_BYTES = max(0, int(os.environ.get("MIN_FREE_BYTES", str(12 * 1024**3))))
JOB_TTL_SECONDS = max(60, int(os.environ.get("JOB_TTL_SECONDS", "86400")))

WORK_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="ytcache")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


class JobError(RuntimeError):
    """An expected user-visible failure while processing a job."""


def video_url(video_id: str) -> str:
    return f"{PUBLIC_BASE_URL}/{video_id}.mp4"


def extract_video_id(raw_url: str) -> str | None:
    """Accept only standard YouTube URLs and return a valid 11-character ID."""
    try:
        parsed = urlparse(raw_url.strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower()
    candidate = None
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/", 1)[0]
    elif host in YOUTUBE_HOSTS:
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[0] in {"shorts", "embed", "live"} and len(parts) >= 2:
            candidate = parts[1]
        elif parsed.path.rstrip("/") == "/watch":
            candidate = parse_qs(parsed.query).get("v", [None])[0]
    return candidate if candidate and VIDEO_ID_RE.fullmatch(candidate) else None


def canonical_youtube_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def command_error(label: str, completed: subprocess.CompletedProcess) -> JobError:
    detail = (completed.stderr or completed.stdout or "unknown error").strip()
    return JobError(f"{label} に失敗しました: {detail[-1200:]}")


def run_checked(args: list[str], label: str, timeout: int | None = None) -> subprocess.CompletedProcess:
    try:
        completed = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise JobError(f"{label} の実行ファイルが見つかりません: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise JobError(f"{label} が時間切れになりました") from exc
    if completed.returncode != 0:
        raise command_error(label, completed)
    return completed


def is_cached(video_id: str) -> bool:
    """Ask R2 for precisely one key. A storage error is not treated as a miss."""
    completed = run_checked(
        [RCLONE_BIN, "lsf", f"{R2_REMOTE}/{video_id}.mp4", "--files-only"],
        "R2確認",
        timeout=30,
    )
    return bool(completed.stdout.strip())


def prune_old_jobs_locked() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    for video_id, job in list(jobs.items()):
        if job.get("status") in {"done", "error"} and job.get("updated_at", 0) < cutoff:
            del jobs[video_id]


def active_job_count_locked() -> int:
    return sum(job["status"] in {"queued", "running"} for job in jobs.values())


def set_job(video_id: str, **changes: object) -> None:
    with jobs_lock:
        jobs.setdefault(video_id, {}).update(changes, updated_at=time.time())


def ensure_capacity() -> None:
    free = shutil.disk_usage(WORK_DIR).free
    if free < MIN_FREE_BYTES:
        raise JobError("サーバーの空き容量が不足しているため開始できません")


def check_duration(source_url: str) -> None:
    if not MAX_DURATION_SECONDS:
        return
    completed = run_checked(
        [YTDLP_BIN, "--no-playlist", "--no-progress", "--print", "%(duration)s", source_url],
        "動画情報の取得",
        timeout=120,
    )
    try:
        duration = float(completed.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise JobError("動画の長さを取得できませんでした") from exc
    if duration > MAX_DURATION_SECONDS:
        raise JobError(f"動画が上限（{MAX_DURATION_SECONDS}秒）を超えています")


def run_job(video_id: str) -> None:
    job_dir: Path | None = None
    try:
        set_job(video_id, status="running", error=None)
        if is_cached(video_id):
            set_job(video_id, status="done")
            return

        ensure_capacity()
        source_url = canonical_youtube_url(video_id)
        check_duration(source_url)
        job_dir = Path(tempfile.mkdtemp(prefix=f"ytcache-{video_id}-", dir=WORK_DIR))
        source_file = job_dir / "source.mp4"
        output_file = job_dir / "output.mp4"

        run_checked(
            [
                YTDLP_BIN, "--no-playlist", "--no-progress", "-f", "bv*+ba/b",
                "--merge-output-format", "mp4", "-o", str(source_file), source_url,
            ],
            "YouTubeからの取得",
        )
        if not source_file.is_file():
            raise JobError("ダウンロード済みのMP4を見つけられませんでした")

        run_checked(
            [
                FFMPEG_BIN, "-y", "-i", str(source_file), "-map", "0:v:0", "-map", "0:a:0?",
                "-sn", "-dn", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
                "-preset", "veryfast", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                str(output_file),
            ],
            "H.264/AACへの変換",
        )
        if not output_file.is_file() or output_file.stat().st_size == 0:
            raise JobError("変換後のMP4を生成できませんでした")
        if MAX_OUTPUT_BYTES and output_file.stat().st_size > MAX_OUTPUT_BYTES:
            raise JobError("変換後ファイルが容量上限を超えています")

        run_checked(
            [RCLONE_BIN, "copyto", str(output_file), f"{R2_REMOTE}/{video_id}.mp4"],
            "R2へのアップロード",
        )
        if not is_cached(video_id):
            raise JobError("アップロード後のR2確認に失敗しました")
        set_job(video_id, status="done", error=None)
    except (JobError, OSError) as exc:
        app.logger.warning("job %s failed: %s", video_id, exc)
        set_job(video_id, status="error", error=str(exc))
    except Exception:
        app.logger.exception("unexpected failure for job %s", video_id)
        set_job(video_id, status="error", error="予期しないサーバーエラー")
    finally:
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)


def valid_id_or_404(video_id: str) -> bool:
    return bool(VIDEO_ID_RE.fullmatch(video_id))


@app.post("/api/cache")
def cache():
    payload = request.get_json(silent=True)
    raw_url = payload.get("url", "") if isinstance(payload, dict) else ""
    video_id = extract_video_id(raw_url) if isinstance(raw_url, str) else None
    if not video_id:
        return jsonify(error="対応するYouTube URLを入力してください"), 400

    try:
        if is_cached(video_id):
            return jsonify(video_id=video_id, status="done", url=video_url(video_id))
    except JobError as exc:
        return jsonify(error=str(exc)), 503

    with jobs_lock:
        prune_old_jobs_locked()
        current = jobs.get(video_id)
        if current and current["status"] in {"queued", "running"}:
            return jsonify(video_id=video_id, status=current["status"])
        if active_job_count_locked() >= MAX_PENDING:
            return jsonify(error="処理待ちが上限に達しています。しばらくしてから再試行してください"), 429
        jobs[video_id] = {"status": "queued", "error": None, "updated_at": time.time()}
        executor.submit(run_job, video_id)
    return jsonify(video_id=video_id, status="queued"), 202


@app.get("/api/status/<video_id>")
def status(video_id: str):
    if not valid_id_or_404(video_id):
        return jsonify(error="不正な動画IDです"), 404
    try:
        if is_cached(video_id):
            return jsonify(status="done", url=video_url(video_id))
    except JobError as exc:
        return jsonify(status="error", error=str(exc)), 503
    with jobs_lock:
        job = dict(jobs.get(video_id, {"status": "unknown"}))
    return jsonify({key: value for key, value in job.items() if key != "updated_at"})


@app.get("/<video_id>")
def resolve(video_id: str):
    if not valid_id_or_404(video_id):
        return "not found", 404
    try:
        if is_cached(video_id):
            return redirect(video_url(video_id), code=302)
    except JobError:
        return "storage unavailable", 503
    return "not cached yet", 404


@app.get("/")
def index():
    return render_template("index.html")
