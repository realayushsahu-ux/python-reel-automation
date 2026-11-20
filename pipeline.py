"""Automatically download DM-shared public Reels, analyze, schedule, and publish."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import gspread
import requests
from dotenv import load_dotenv
from openai import OpenAI

import reelmaker


ROOT = Path(__file__).resolve().parent
LOCAL_TZ = timezone(timedelta(hours=5, minutes=30))
PUBLISH_SLOTS = ((11, 0), (18, 0), (22, 0), (23, 0))
REEL_LINK_TAB = "Reel Link"
VIDEO_TAB = "Video Convert"
FAILED_TAB = "Failed Links"
READY_TAB = "Ready to Post"
POSTED_TAB = "Posted Reels"
REJECTED_TAB = "Rejected Reels"
YTDLP_MAX_VIDEO_BYTES = 300 * 1024 * 1024

COMMON_HEADERS = [
    "job_id", "source_url", "source_message_id", "video_file", "caption",
    "hashtags", "scheduled_at", "posted_url", "status", "stage", "attempts",
    "error", "created_at", "updated_at", "content_type", "creator", "topic",
]
# Only these kinds of Reels may be published; a raw re-upload of someone else's
# clip is what got the account removed from recommendations.
PUBLISHABLE_TYPES = {"COMMENTARY", "ORIGINAL"}
TAB_HEADERS = {
    REEL_LINK_TAB: ["job_id", "source_url", "source_message_id", "status", "stage", "attempts", "error", "created_at", "updated_at", "creator"],
    VIDEO_TAB: ["job_id", "source_url", "source_message_id", "video_file", "status", "stage", "attempts", "error", "created_at", "updated_at", "creator"],
    FAILED_TAB: COMMON_HEADERS + ["failed_at"],
    READY_TAB: COMMON_HEADERS,
    POSTED_TAB: COMMON_HEADERS + ["posted_at"],
    REJECTED_TAB: COMMON_HEADERS + ["rejected_at"],
}
_HEADER_CACHE: dict[int, list[str]] = {}
_LEGACY_MESSAGE_IDS: set[str] | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def setting(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def connect_book():
    credentials = Path(setting("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json"))
    if not credentials.is_absolute():
        credentials = ROOT / credentials
    sheet_id = setting("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("Set GOOGLE_SHEET_ID in .env")
    if not credentials.is_file():
        raise RuntimeError("Google service account JSON file was not found")
    return gspread.service_account(filename=str(credentials)).open_by_key(sheet_id)


def ensure_tabs(book=None) -> dict[str, Any]:
    book = book or connect_book()
    tabs: dict[str, Any] = {}
    for title, required_headers in TAB_HEADERS.items():
        try:
            ws = book.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = book.add_worksheet(title=title, rows=1000, cols=max(20, len(required_headers)))
        existing = ws.row_values(1)
        if not existing:
            ws.update(values=[required_headers], range_name="A1")
        else:
            for header in required_headers:
                if header not in existing:
                    existing.append(header)
                    ws.update_cell(1, len(existing), header)
        _HEADER_CACHE[ws.id] = existing
        tabs[title] = ws
    return tabs


def headers_for(ws) -> list[str]:
    headers = _HEADER_CACHE.get(ws.id)
    if headers is None:
        headers = ws.row_values(1)
        _HEADER_CACHE[ws.id] = headers
    return headers


def records(ws) -> list[tuple[int, dict[str, str]]]:
    values = ws.get_all_values()
    if not values:
        return []
    headers = values[0]
    _HEADER_CACHE[ws.id] = headers
    output = []
    for row_number, row in enumerate(values[1:], start=2):
        padded = row + [""] * max(0, len(headers) - len(row))
        output.append((row_number, dict(zip(headers, padded))))
    return output


def append_record(ws, values: dict[str, str]) -> None:
    headers = headers_for(ws)
    ws.append_row([str(values.get(h, "")) for h in headers], value_input_option="RAW")


def update_record(ws, row_number: int, **fields: str) -> None:
    headers = headers_for(ws)
    updates = []
    for field, value in fields.items():
        if field in headers:
            cell = gspread.utils.rowcol_to_a1(row_number, headers.index(field) + 1)
            updates.append({"range": cell, "values": [[str(value)]]})
    if updates:
        ws.batch_update(updates, value_input_option="RAW")


def job_id_for(message_id: str) -> str:
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:12].upper()
    return f"RV-{digest}"


def fetch_relay_events() -> list[dict[str, Any]]:
    relay_url = setting("WEBHOOK_RELAY_URL").rstrip("/")
    relay_token = setting("WEBHOOK_RELAY_TOKEN")
    if not relay_url or not relay_token:
        return []
    poll_seconds = max(60, int(setting("WEBHOOK_RELAY_POLL_SECONDS", "180")))
    marker = ROOT / ".relay-poll-at"
    if marker.exists():
        try:
            last_polled = float(marker.read_text(encoding="utf-8").strip())
            if time.time() - last_polled < poll_seconds:
                return []
        except (OSError, ValueError):
            pass
    events: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(10):
        params = {"cursor": cursor} if cursor else None
        response = requests.get(
            f"{relay_url}/internal/events",
            headers={"Authorization": f"Bearer {relay_token}"},
            params=params, timeout=(10, 30),
        )
        response.raise_for_status()
        payload = response.json()
        page_events = payload.get("events", [])
        if isinstance(page_events, list):
            events.extend(page_events)
        cursor = str(payload.get("cursor") or "")
        if not cursor:
            break
    marker.write_text(str(time.time()), encoding="utf-8")
    return events


def acknowledge_relay_events(events: list[dict[str, Any]]) -> None:
    relay_url = setting("WEBHOOK_RELAY_URL").rstrip("/")
    relay_token = setting("WEBHOOK_RELAY_TOKEN")
    message_ids = list(dict.fromkeys(
        str(event.get("message_id", "")).strip() for event in events
        if str(event.get("message_id", "")).strip()
    ))
    if not relay_url or not relay_token or not message_ids:
        return
    # The Worker limits each acknowledgement to 100 IDs.
    for offset in range(0, len(message_ids), 100):
        response = requests.post(
            f"{relay_url}/internal/ack",
            headers={"Authorization": f"Bearer {relay_token}"},
            json={"message_ids": message_ids[offset:offset + 100]}, timeout=(10, 30),
        )
        response.raise_for_status()


def import_webhook_events(tabs: dict[str, Any] | None = None) -> tuple[int, int, int]:
    global _LEGACY_MESSAGE_IDS
    event_file = ROOT / "meta-webhook-events.jsonl"
    expected_sender = setting("META_ALLOWED_SENDER_ID")
    expected_recipient = setting("META_INSTAGRAM_ACCOUNT_ID")
    if not expected_sender or not expected_recipient:
        raise RuntimeError("Set the allowed sender and destination account IDs in .env")

    tabs = tabs or ensure_tabs()
    link_ws = tabs[REEL_LINK_TAB]
    existing_ids = {r.get("source_message_id", "").strip() for _, r in records(link_ws)}
    # Load legacy message IDs only once per process, not once per polling cycle.
    if _LEGACY_MESSAGE_IDS is None:
        _LEGACY_MESSAGE_IDS = set()
        try:
            legacy_ws = connect_book().worksheet(setting("GOOGLE_WORKSHEET", "Reel Queue"))
            for _, old_row in records(legacy_ws):
                message_id = old_row.get("source_message_id", "").strip()
                if message_id:
                    _LEGACY_MESSAGE_IDS.add(message_id)
        except gspread.WorksheetNotFound:
            pass
    existing_ids.update(_LEGACY_MESSAGE_IDS)
    local_events: list[dict[str, Any]] = []
    seen = added = skipped = 0
    if event_file.is_file():
        with event_file.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    local_events.append(json.loads(line))
                except json.JSONDecodeError:
                    skipped += 1
    remote_events: list[dict[str, Any]] = []
    try:
        remote_events = fetch_relay_events()
    except requests.RequestException as exc:
        print(f"Webhook relay poll failed ({type(exc).__name__})")
    except (ValueError, RuntimeError) as exc:
        print(f"Webhook relay response failed ({type(exc).__name__})")

    for event in local_events + remote_events:
        message_id = str(event.get("message_id", "")).strip()
        urls = event.get("shared_media_urls") or []
        source_url = str(urls[0]).strip() if urls else ""
        if (not message_id or message_id in existing_ids
                or str(event.get("sender_id", "")) != expected_sender
                or str(event.get("recipient_id", "")) != expected_recipient
                or not source_url):
            skipped += 1
            continue
        created = str(event.get("received_at", "")) or now_iso()
        append_record(link_ws, {
            "job_id": job_id_for(message_id), "source_url": source_url,
            "source_message_id": message_id, "status": "WAITING_FOR_VIDEO",
            "stage": "VIDEO_DOWNLOAD", "attempts": "0", "error": "",
            "created_at": created, "updated_at": now_iso(),
        })
        existing_ids.add(message_id)
        added += 1
        seen += 1
    if remote_events:
        try:
            acknowledge_relay_events(remote_events)
        except requests.RequestException as exc:
            # The next poll will see the same events; source_message_id dedupes them.
            print(f"Webhook relay acknowledgement failed ({type(exc).__name__}); queued events will be retried")
    return seen, added, skipped


def import_reviewed_legacy_rows(tabs: dict[str, Any]) -> int:
    """Move reviewed rows from the old Reel Queue into the automatic video pipeline once."""
    try:
        legacy_ws = connect_book().worksheet(setting("GOOGLE_WORKSHEET", "Reel Queue"))
    except gspread.WorksheetNotFound:
        return 0
    link_ws = tabs[REEL_LINK_TAB]
    existing = records(link_ws)
    existing_urls = {row.get("source_url", "").strip() for _, row in existing}
    existing_jobs = {row.get("job_id", "").strip() for _, row in existing}
    imported = 0
    for row_number, row in records(legacy_ws):
        if row.get("status", "").strip().upper() not in {"REVIEWED", "APPROVED"}:
            continue
        source_url = row.get("source_url", "").strip()
        if not source_url or source_url in existing_urls:
            continue
        from urllib.parse import urlparse
        parsed = urlparse(source_url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {"instagram.com", "www.instagram.com", "m.instagram.com"}:
            continue
        digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:12].upper()
        job_id = f"LEGACY-{digest}"
        if job_id in existing_jobs:
            continue
        message_id = row.get("source_message_id", "").strip() or f"legacy-queue-{digest}"
        created = row.get("created_at", "").strip() or now_iso()
        video_file = row.get("video_file", "").strip()
        video_path = Path(video_file) if video_file else None
        if video_path and not video_path.is_absolute():
            video_path = ROOT / video_path
        has_video = bool(video_path and video_path.is_file())
        append_record(link_ws, {
            "job_id": job_id,
            "source_url": source_url,
            "source_message_id": message_id,
            "status": "VIDEO_CONVERTED" if has_video else "WAITING_FOR_VIDEO",
            "stage": "AI_ANALYSIS" if has_video else "VIDEO_DOWNLOAD",
            "attempts": "0",
            "error": "",
            "created_at": created,
            "updated_at": now_iso(),
        })
        if has_video and video_path:
            append_record(tabs[VIDEO_TAB], {
                "job_id": job_id,
                "source_url": source_url,
                "source_message_id": message_id,
                "video_file": str(video_path.relative_to(ROOT)) if video_path.is_relative_to(ROOT) else str(video_path),
                "status": "VIDEO_READY",
                "stage": "AI_ANALYSIS",
                "attempts": "0",
                "error": "",
                "created_at": created,
                "updated_at": now_iso(),
            })
        update_record(legacy_ws, row_number, status="MIGRATED_TO_PIPELINE", updated_at=now_iso())
        existing_urls.add(source_url)
        existing_jobs.add(job_id)
        imported += 1
    return imported


def fail_job(tabs: dict[str, Any], values: dict[str, str], stage: str, reason: str) -> None:
    failed_ws = tabs[FAILED_TAB]
    job_id = values.get("job_id", "")
    existing = next(((n, r) for n, r in records(failed_ws) if r.get("job_id") == job_id), None)
    failed = {key: str(values.get(key, "")) for key in COMMON_HEADERS}
    failed.update({
        "job_id": job_id, "status": "FAILED", "stage": stage,
        "attempts": str(int(values.get("attempts", "0") or 0) + 1),
        "error": reason[:500], "updated_at": now_iso(), "failed_at": now_iso(),
    })
    if existing:
        update_record(failed_ws, existing[0], **failed)
    else:
        append_record(failed_ws, failed)


def retry_failed_downloads() -> int:
    """Requeue failed download jobs so they can be retried with working browser cookies."""
    tabs = ensure_tabs()
    link_ws = tabs[REEL_LINK_TAB]
    failed_ws = tabs[FAILED_TAB]
    links = {row.get("job_id", ""): (number, row) for number, row in records(link_ws)}
    remove_failed_rows: list[int] = []
    requeued = 0
    for failed_number, failed in records(failed_ws):
        if failed.get("stage", "").strip().upper() != "VIDEO_DOWNLOAD":
            continue
        job_id = failed.get("job_id", "").strip()
        match = links.get(job_id)
        if not match or match[1].get("status", "").strip().upper() != "FAILED":
            continue
        link_number, _link = match
        update_record(
            link_ws,
            link_number,
            status="DOWNLOAD_RETRY",
            stage="VIDEO_DOWNLOAD",
            attempts="0",
            error="",
            updated_at=now_iso(),
        )
        remove_failed_rows.append(failed_number)
        requeued += 1
    for row_number in sorted(remove_failed_rows, reverse=True):
        failed_ws.delete_rows(row_number)
    return requeued


def retry_failed_analysis() -> int:
    """Requeue failed AI-analysis jobs without downloading the MP4s again."""
    tabs = ensure_tabs()
    video_ws = tabs[VIDEO_TAB]
    link_ws = tabs[REEL_LINK_TAB]
    failed_ws = tabs[FAILED_TAB]
    videos = {row.get("job_id", ""): (number, row) for number, row in records(video_ws)}
    links = {row.get("job_id", ""): (number, row) for number, row in records(link_ws)}
    remove_failed_rows: list[int] = []
    requeued = 0
    for failed_number, failed in records(failed_ws):
        if failed.get("stage", "").strip().upper() != "AI_ANALYSIS_OR_SCHEDULING":
            continue
        job_id = failed.get("job_id", "").strip()
        video_match = videos.get(job_id)
        if not video_match or video_match[1].get("status", "").strip().upper() != "FAILED":
            continue
        video_number, video_row = video_match
        video_path = Path(video_row.get("video_file", "").strip())
        if not video_path.is_absolute():
            video_path = ROOT / video_path
        if not video_path.is_file():
            continue
        update_record(
            video_ws,
            video_number,
            status="VIDEO_READY",
            stage="AI_ANALYSIS",
            attempts="0",
            error="",
            updated_at=now_iso(),
        )
        link_match = links.get(job_id)
        if link_match:
            update_record(
                link_ws,
                link_match[0],
                status="VIDEO_CONVERTED",
                stage="AI_ANALYSIS",
                error="",
                updated_at=now_iso(),
            )
        remove_failed_rows.append(failed_number)
        requeued += 1
    for row_number in sorted(remove_failed_rows, reverse=True):
        failed_ws.delete_rows(row_number)
    return requeued


def automatic_downloads(tabs: dict[str, Any]) -> tuple[int, int]:
    """Download waiting public Reel URLs locally through yt-dlp."""
    try:
        import yt_dlp
    except ImportError:
        print("Automatic Reel download needs yt-dlp; install it with .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        return 0, 0

    max_attempts = max(1, int(setting("DOWNLOAD_MAX_ATTEMPTS", "3")))
    retry_seconds = max(30, int(setting("DOWNLOAD_RETRY_SECONDS", "300")))
    video_dir = ROOT / "media" / "video_convert"
    video_dir.mkdir(parents=True, exist_ok=True)
    downloaded = failed = 0
    for row_number, row in records(tabs[REEL_LINK_TAB]):
        if row.get("status", "").upper() not in {"WAITING_FOR_VIDEO", "DOWNLOAD_RETRY", "DOWNLOADING"}:
            continue
        attempts = int(row.get("attempts", "0") or 0)
        if attempts and row.get("updated_at"):
            try:
                last = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last).total_seconds() < retry_seconds:
                    continue
            except ValueError:
                pass
        source_url = row.get("source_url", "").strip()
        from urllib.parse import urlparse
        parsed_source = urlparse(source_url)
        if parsed_source.scheme != "https" or (parsed_source.hostname or "").lower() not in {"instagram.com", "www.instagram.com", "m.instagram.com"}:
            fail_job(tabs, row, "VIDEO_DOWNLOAD", "The shared URL is not a supported Instagram HTTPS link")
            update_record(tabs[REEL_LINK_TAB], row_number, status="FAILED", stage="VIDEO_DOWNLOAD", error="Unsupported Instagram URL", updated_at=now_iso())
            failed += 1
            continue
        attempt = attempts + 1
        update_record(tabs[REEL_LINK_TAB], row_number, status="DOWNLOADING", stage="VIDEO_DOWNLOAD", attempts=str(attempt), error="", updated_at=now_iso())
        try:
            output_template = str(video_dir / f"{row['job_id']}.%(ext)s")
            options: dict[str, Any] = {
                "format": "best[ext=mp4]/best",
                "outtmpl": output_template,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 60,
                "retries": 2,
                "fragment_retries": 2,
                "max_filesize": YTDLP_MAX_VIDEO_BYTES,
                "overwrites": False,
            }
            browser = setting("YTDLP_BROWSER", "firefox").lower()
            if browser and browser != "none":
                options["cookiesfrombrowser"] = (browser, None, None, None)
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(source_url, download=True)
                filepath = ydl.prepare_filename(info)
                actual = Path(filepath)
                # Some extractors report the pre-merge extension; prefer the actual final file.
                if not actual.exists():
                    candidates = sorted(video_dir.glob(f"{row['job_id']}.*"))
                    candidates = [p for p in candidates if p.suffix.lower() not in {".part", ".ytdl"}]
                    if not candidates:
                        raise RuntimeError("yt-dlp finished without creating a video file")
                    actual = candidates[0]
                if actual.suffix.lower() not in {".mp4", ".mov"}:
                    raise RuntimeError(f"Downloaded format {actual.suffix} is not a publishable MP4/MOV")
                if actual.stat().st_size < 1024 or actual.stat().st_size > YTDLP_MAX_VIDEO_BYTES:
                    raise RuntimeError("Downloaded video is empty or exceeds the 300 MB limit")
            creator = str(info.get("channel") or "").strip()
            update_record(tabs[REEL_LINK_TAB], row_number, status="VIDEO_CONVERTED", stage="AI_ANALYSIS", creator=creator, error="", updated_at=now_iso())
            downloaded += 1
            print(f"{row.get('job_id')}: video downloaded automatically with yt-dlp")
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:450]
            # Remove incomplete chunks so a clean retry can proceed later.
            for partial in video_dir.glob(f"{row['job_id']}.*"):
                if partial.suffix.lower() in {".part", ".ytdl"}:
                    partial.unlink(missing_ok=True)
            if attempt >= max_attempts:
                fail_job(tabs, row, "VIDEO_DOWNLOAD", message)
                update_record(tabs[REEL_LINK_TAB], row_number, status="FAILED", stage="VIDEO_DOWNLOAD", attempts=str(attempt), error=message, updated_at=now_iso())
                failed += 1
                print(f"{row.get('job_id')}: download failed after {attempt} attempt(s); moved to Failed Links")
            else:
                update_record(tabs[REEL_LINK_TAB], row_number, status="DOWNLOAD_RETRY", stage="VIDEO_DOWNLOAD", attempts=str(attempt), error=message, updated_at=now_iso())
                print(f"{row.get('job_id')}: download attempt {attempt}/{max_attempts} failed; will retry while processing other Reels")
    return downloaded, failed


def import_converted_videos(tabs: dict[str, Any]) -> int:
    folder = ROOT / "media" / "video_convert"
    folder.mkdir(parents=True, exist_ok=True)
    video_ws = tabs[VIDEO_TAB]
    already = {r.get("job_id", "") for _, r in records(video_ws)}
    links = {
        r.get("job_id", ""): (n, r)
        for n, r in records(tabs[REEL_LINK_TAB])
        if r.get("status", "").upper() in {"WAITING_FOR_VIDEO", "VIDEO_CONVERTED"}
    }
    added = 0
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in {".mp4", ".mov"}:
            continue
        job_id = path.stem
        if job_id in already or job_id not in links:
            continue
        row_number, link = links[job_id]
        append_record(video_ws, {
            "job_id": job_id, "source_url": link.get("source_url", ""),
            "source_message_id": link.get("source_message_id", ""),
            "video_file": str(path.relative_to(ROOT)), "status": "VIDEO_READY",
            "stage": "AI_ANALYSIS", "attempts": "0", "error": "", "creator": link.get("creator", ""),
            "created_at": link.get("created_at", now_iso()), "updated_at": now_iso(),
        })
        update_record(tabs[REEL_LINK_TAB], row_number, status="VIDEO_CONVERTED", updated_at=now_iso())
        already.add(job_id)
        added += 1
    return added


def read_video_frames(video_path: Path, count: int = 8, max_width: int = 768) -> list[str]:
    import cv2

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError("Python could not open this video file")
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if total < 1:
        capture.release()
        raise RuntimeError("Video has no readable frames")
    frames: list[str] = []
    positions = sorted({round(i * (total - 1) / max(1, count - 1)) for i in range(count)})
    for position in positions:
        capture.set(cv2.CAP_PROP_POS_FRAMES, position)
        ok, frame = capture.read()
        if not ok:
            continue
        height, width = frame.shape[:2]
        if width > max_width:
            new_height = max(1, round(height * max_width / width))
            frame = cv2.resize(frame, (max_width, new_height), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 78])
        if ok:
            frames.append(base64.b64encode(encoded.tobytes()).decode("ascii"))
    capture.release()
    if not frames:
        raise RuntimeError("No video frames could be extracted")
    return frames


def extract_audio_transcript(video_path: Path, client: OpenAI) -> str:
    """Paid API transcription; only used when TRANSCRIBE_MODE=api."""
    try:
        import imageio_ffmpeg
    except ImportError:
        return ""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="reel-audio-") as temp_dir:
        audio_path = Path(temp_dir) / "audio.mp3"
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path),
             "-map", "0:a:0?", "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(audio_path)],
            capture_output=True, timeout=120, check=False,
        )
        if result.returncode != 0 or not audio_path.exists() or audio_path.stat().st_size < 1000:
            return ""
        model = setting("AI_TRANSCRIPTION_MODEL", "gpt-transcribe")
        with audio_path.open("rb") as audio:
            transcript = client.audio.transcriptions.create(model=model, file=audio)
        return str(getattr(transcript, "text", "")).strip()


def get_transcript(video_path: Path, client: OpenAI) -> str:
    """Speech in the clip, if any. Local Whisper (default) is free; 'api' is the old paid path."""
    mode = setting("TRANSCRIBE_MODE", "local").lower()
    if mode == "off":
        return ""
    if mode == "api":
        return extract_audio_transcript(video_path, client)
    return reelmaker.transcribe_local(video_path)


def ai_client() -> OpenAI:
    api_key = setting("AI_API_KEY")
    if not api_key:
        raise RuntimeError("Set AI_API_KEY in .env")
    # A failed request is not billed, so retrying a slow moment costs nothing extra.
    return OpenAI(api_key=api_key, base_url=setting("AI_BASE_URL", "https://api.openai.com/v1"),
                  timeout=float(setting("AI_TIMEOUT_SECONDS", "180") or 180), max_retries=3)


def ask_json(client: OpenAI, messages: list[dict[str, Any]]) -> dict[str, Any]:
    response = client.chat.completions.create(
        model=setting("AI_MODEL", "gpt-6-luna"),
        response_format={"type": "json_object"},
        messages=messages,
    )
    result = json.loads(response.choices[0].message.content or "{}")
    return result if isinstance(result, dict) else {}


def script_lines(value: Any) -> list[tuple[str, str]]:
    """(text the voice says, text shown on screen) for each spoken sentence."""
    items = value if isinstance(value, list) else re.split(r"(?<=[.!?।])\s+", str(value or ""))
    lines: list[tuple[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            say = reelmaker.clean_text(item.get("say") or item.get("line") or item.get("show") or "")
            show = reelmaker.clean_text(item.get("show") or item.get("line") or "") or say
        else:
            say = show = reelmaker.clean_text(str(item))
        if re.search(r"[A-Za-z]", show):
            # Hinglish captions sometimes pick up a stray Devanagari letter, which the
            # caption font cannot draw properly; drop it.
            show = reelmaker.clean_text(re.sub(r"[ऀ-ॿ]+", "", show))
        if say:
            lines.append((say, show))
    return lines


def language_rules() -> str:
    if reelmaker.is_hindi():
        return (
            "LANGUAGE: casual spoken Hindi, the way young Indians talk on Reels (English words mixed in are "
            'fine). For each spoken sentence give "say": the sentence fully in Devanagari script (write English '
            'words in Devanagari too, so a Hindi voice reads it naturally) and "show": the exact same sentence in '
            "Hinglish using Roman letters, for the on-screen captions. Write hook and caption in Hinglish "
            "(Roman letters)."
        )
    language = setting("COMMENTARY_LANGUAGE", "English")
    return f'LANGUAGE: {language}. For each spoken sentence give "say" and "show" as the same sentence.'


VOCAB_FILE = ROOT / "data" / "hindi_hinglish_vocabulary.json"
VOCAB_HISTORY = ROOT / "media" / "vocab-history.json"
INTENSITIES = ["clean", "mild", "medium", "strong"]
# Gaali about mothers/sisters and words that insult a person rather than the action
# hurt reach the most; blocked unless BLOCKED_TERMS is changed in .env.
DEFAULT_BLOCKED_TERMS = "bhenchod,madarchod,chutiya,gaand(u)"
SLANG_LIMITS = (
    "Gaali and slang go only in the spoken lines, never in the hook, caption or hashtags. React to the "
    "action or situation, never insult a real person in the clip. Never use slurs about caste, religion, "
    "region, gender, sexuality, disability or skin colour. If children appear, use no gaali at all."
)
_VOCAB: dict[str, Any] | None = None


def slang_level() -> str:
    """clean, mild, medium or strong (SLANG_LEVEL; 'off' means clean). Default mild."""
    level = setting("SLANG_LEVEL", "mild").lower()
    level = {"off": "clean", "none": "clean"}.get(level, level)
    return level if level in INTENSITIES else "mild"


def load_vocabulary() -> dict[str, Any]:
    global _VOCAB
    if _VOCAB is None:
        try:
            _VOCAB = json.loads(VOCAB_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _VOCAB = {"vocabulary": [], "selection_policy": {}, "scene_tag_map": {}}
    return _VOCAB


def blocked_terms() -> set[str]:
    return {t.strip().lower() for t in setting("BLOCKED_TERMS", DEFAULT_BLOCKED_TERMS).split(",") if t.strip()}


def allowed_vocabulary(level: str) -> list[dict[str, Any]]:
    """Vocabulary terms allowed at this intensity, minus blocked terms."""
    top = INTENSITIES.index(level)
    blocked = blocked_terms()
    return [v for v in load_vocabulary().get("vocabulary", [])
            if v.get("intensity") in INTENSITIES and INTENSITIES.index(v["intensity"]) <= top
            and str(v.get("term", "")).lower() not in blocked]


def _term_pattern(term: str) -> re.Pattern[str]:
    # "gaand(u)" in the pack means "gaand" or "gaandu".
    body = re.escape(term.lower()).replace(r"\(u\)", "u?")
    return re.compile(rf"(?<![a-z]){body}(?![a-z])")


def terms_in(text: str) -> list[dict[str, Any]]:
    """Vocabulary entries (any intensity) that appear in a Hinglish line."""
    lowered = text.lower()
    hits = []  # (start, end, entry) for every place a term appears
    for entry in load_vocabulary().get("vocabulary", []):
        for match in _term_pattern(str(entry.get("term", ""))).finditer(lowered):
            hits.append((match.start(), match.end(), entry))
    # "yeh kya bakchodi hai" contains "bakchodi", and "gaand phat gayi" contains
    # "gaand": a match inside a longer phrase's match is part of that phrase.
    found: list[dict[str, Any]] = []
    for start, end, entry in hits:
        inside = any(s <= start and end <= e and (e - s) > (end - start) for s, e, _ in hits)
        if not inside and entry not in found:
            found.append(entry)
    return found


def recent_terms() -> list[str]:
    n = int(load_vocabulary().get("selection_policy", {}).get("avoid_same_root_within_last_n_reels", 5))
    try:
        history = json.loads(VOCAB_HISTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [term for reel in history[-n:] for term in reel]


def remember_terms(terms: list[str]) -> None:
    """Record the terms one finished Reel used, so the next Reels pick different ones."""
    try:
        history = json.loads(VOCAB_HISTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        history = []
    history.append(sorted(set(terms)))
    VOCAB_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    VOCAB_HISTORY.write_text(json.dumps(history[-50:], ensure_ascii=False), encoding="utf-8")


def slang_rules(level: str, avoid: list[str]) -> str:
    if level == "clean":
        return "SWEARING: none; keep every line clean."
    policy = load_vocabulary().get("selection_policy", {})
    max_terms = int(setting("MAX_SLANG_PER_REEL") or policy.get("max_profanity_terms_per_clip", 1))
    bank = [{"term": v["term"], "intensity": v["intensity"], "tags": v.get("tags", [])}
            for v in allowed_vocabulary(level)]
    return (
        f"SWEARING (level: {level}): pick reactions ONLY from this vocabulary bank, matched to what is "
        f"happening at that moment (scene tags: {', '.join(load_vocabulary().get('scene_tag_map', {}))}). "
        f"Use at most {max_terms} non-clean term(s) in the whole Reel and never the same term twice. Unless "
        f"children are present, DO use at least one non-clean term (up to {level}), placed on the funniest "
        "moment - that is what this audience comes for. Do not use these recently used terms: "
        f"{', '.join(avoid) or 'none'}. Do not invent other gaalis. Vary how lines start; do not open "
        "every line with arey bhai or zara dhyaan se dekho.\n"
        f"BANK: {json.dumps(bank, ensure_ascii=False, separators=(',', ':'))}\n{SLANG_LIMITS}"
    )


def vet_line(line: tuple[str, str], level: str, used: list[str], kids: bool,
             recent: list[str] = ()) -> tuple[str, str] | None:
    """Check a spoken line's vocabulary against the JSON allowlist; None means drop it."""
    allowed = {v["term"].lower() for v in allowed_vocabulary(level)}
    policy = load_vocabulary().get("selection_policy", {})
    max_terms = int(setting("MAX_SLANG_PER_REEL") or policy.get("max_profanity_terms_per_clip", 1))
    found = terms_in(line[1])
    for entry in found:
        term = entry["term"].lower()
        rude = entry.get("intensity") != "clean"
        if term not in allowed or (rude and (kids or term in used or term in recent)):
            return None
    rude_terms = [e["term"].lower() for e in found if e.get("intensity") != "clean"]
    if len([t for t in used if t]) + len(rude_terms) > max_terms and rude_terms:
        return None
    used.extend(rude_terms)
    return line


def pick_gaali(scene_tags: list[str], recent: list[str]) -> dict[str, Any] | None:
    """A non-clean vocabulary term with a ready spoken line, matched to the scene's tags and
    not used in recent Reels; random among the best matches so it keeps varying."""
    import random

    tag_map = load_vocabulary().get("scene_tag_map", {})
    wanted = {t for tag in scene_tags for t in tag_map.get(tag, [tag])}
    # At medium or strong, "kya bakwaas hai" is not a real gaali: pick medium or above.
    floor = "medium" if INTENSITIES.index(slang_level()) >= INTENSITIES.index("medium") else "mild"
    candidates = [v for v in allowed_vocabulary(slang_level())
                  if INTENSITIES.index(v.get("intensity", "clean")) >= INTENSITIES.index(floor)
                  and v.get("line") and v["term"].lower() not in recent]
    if not candidates:
        return None
    best = max(len(wanted & set(v.get("tags", []))) for v in candidates)
    return random.choice([v for v in candidates if len(wanted & set(v.get("tags", []))) == best])


def credit_line(creator: str) -> str:
    creator = creator.strip().lstrip("@")
    return f"\U0001F3A5 Credit: @{creator}" if creator else "\U0001F3A5 Credit: original creator"


def draft_from_video(video_path: Path, creator: str = "") -> dict[str, Any]:
    """One AI call per Reel: caption, hashtags, hook, spoken intro, timed reaction beats,
    spoken ending and the narrator's voice; every gaali is checked against the vocabulary JSON."""
    client = ai_client()
    detail = setting("AI_IMAGE_DETAIL", "low").lower()
    frame_count = max(2, int(setting("AI_FRAME_COUNT", "6") or 6))
    frames = read_video_frames(video_path, frame_count, max_width=512 if detail == "low" else 768)
    transcript = get_transcript(video_path, client)
    duration, _has_audio = reelmaker.probe(video_path)
    level = slang_level()
    avoid = recent_terms()
    max_reactions = max(0, int(setting("MAX_REACTIONS", "2") or 2))
    by = f" (@{creator})" if creator else ""
    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": (
            f"These are {len(frames)} frames, in order, from a {duration:.0f}-second video made by another "
            f"creator{by}; each frame is labelled with its time. You run a desi commentary account. The Reel "
            "plays like this: your voice opens it, the clip plays with its own sound while you drop short "
            "reactions at the key moments, and your voice closes it.\n"
            "Return JSON with:\n"
            "- caption: 1-2 natural sentences for the post, no hashtags\n"
            "- hashtags: 3 to 5 relevant hashtags in one space-separated string\n"
            "- hook: on-screen title, at most 7 words, no emoji, no hashtags\n"
            '- voice: "male" or "female", whichever narrator makes this clip funnier\n'
            "- children_present: true if any child is visible\n"
            "- someone_hurt: true if anyone looks genuinely injured or it is a serious accident\n"
            '- intro: 1 or 2 spoken sentences as {"say", "show"} objects, 10-20 words in total. Tell what '
            "this video is about with curiosity and energy, but do NOT reveal the ending or punchline.\n"
            f'- reactions: 0 to {max_reactions} items {{"t": seconds, "scene_tag", "term", "confidence", '
            '"say", "show"}. A 2-8 word reaction spoken just after the action at time t becomes clear '
            "(never before the visual punchline), e.g. a fail, a shock or a surprise. Use different words "
            "for different moments. term is the exact bank term used, or empty.\n"
            '- outro: 1 or 2 spoken sentences as {"say", "show"} objects, 10-20 words in total, said over the '
            "end of the clip. React to what happened, then ask ONE specific, easy question people will want "
            "to answer in the comments (pick a side, rate it 1-10, what would you do, tag a friend). End by "
            "telling them to answer in the comments.\n"
            "Deliver it like a desi stand-up comedian reacting live, never like someone reading a script: "
            "short punchy beats separated by '...' for comic timing, reactions like arre bhai, oye, haha, "
            "exaggeration, and the punchline last. No emoji, no hashtags. Do not invent facts about the "
            "people shown and do not claim you filmed it.\n"
            f"{language_rules()}\n{slang_rules(level, avoid)}\n\n"
            f"Speech heard in the clip: {transcript or '[none]'}"
        ),
    }]
    for index, frame in enumerate(frames):
        seconds = duration * index / max(1, len(frames) - 1)
        content.append({"type": "text", "text": f"Frame at {seconds:.1f}s:"})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame}", "detail": detail}})
    result = ask_json(client, [
        {"role": "system", "content": "You write short, accurate, original, funny commentary for vertical videos."},
        {"role": "user", "content": content},
    ])
    # Children on screen, or someone really hurt: no gaali at all, only clean reactions.
    kids = bool(result.get("children_present")) or bool(result.get("someone_hurt"))
    recent = [t.lower() for t in avoid]
    used: list[str] = []  # non-clean terms this Reel has used so far
    fallback = ("अरे भाई... ये देखो!", "Arre bhai... ye dekho!") if reelmaker.is_hindi() else ("Wait for it...",) * 2
    intro = [line for line in (vet_line(x, level, used, kids, recent) for x in script_lines(result.get("intro"))) if line]
    min_confidence = float(load_vocabulary().get("selection_policy", {}).get("minimum_scene_confidence", 0.72))
    reactions: list[tuple[float, tuple[str, str]]] = []
    for item in (result.get("reactions") or [])[:max_reactions]:
        if not isinstance(item, dict):
            continue
        try:
            moment = float(item.get("t"))
            confidence = float(item.get("confidence", 1))
        except (TypeError, ValueError):
            continue
        lines = script_lines([item])
        if not lines or confidence < min_confidence or not 0 <= moment <= duration:
            continue
        line = vet_line(lines[0], level, used, kids, recent)
        if line:
            reactions.append((moment, line))
    outro = [line for line in (vet_line(x, level, used, kids, recent) for x in script_lines(result.get("outro"))) if line]
    real_gaali = [t for t in used if INTENSITIES.index(
        next((v["intensity"] for v in load_vocabulary().get("vocabulary", []) if v["term"].lower() == t), "mild"))
        >= INTENSITIES.index("medium")]
    wants_real = INTENSITIES.index(level) >= INTENSITIES.index("medium")
    if (not used or (wants_real and not real_gaali)) and not kids and level != "clean" and reelmaker.is_hindi():
        # The AI often plays safe; add one subtle gaali that fits the scene, at the funny moment.
        scene_tags = [str(item.get("scene_tag", "")) for item in (result.get("reactions") or []) if isinstance(item, dict)]
        extra = pick_gaali(scene_tags, recent)
        if extra:
            say, show = extra["line"]["say"], extra["line"]["show"]
            if reactions:
                moment, (r_say, r_show) = reactions[0]
                reactions[0] = (moment, (f"{say} {r_say}", f"{show} {r_show}"))
            else:
                outro.insert(0, (say, show))
    caption = str(result.get("caption", "")).strip()
    if not caption or not outro:
        raise RuntimeError("AI returned an empty caption or ending")
    voice = str(result.get("voice", "")).strip().lower()
    return {
        "caption": f"{caption}\n\n{credit_line(creator)}",
        "hashtags": str(result.get("hashtags", "")).strip(),
        "hook": str(result.get("hook", "")).strip(),
        "intro": intro or [fallback],
        "reactions": reactions,
        "outro": outro,
        "voice": voice if voice in {"male", "female"} else "",
        "kids": kids,
        "terms": [e["term"] for _m, line in reactions for e in terms_in(line[1])]
                 + [e["term"] for line in intro + outro for e in terms_in(line[1])],
    }


def parse_slots(value: str) -> tuple[tuple[int, int], ...]:
    slots: set[tuple[int, int]] = set()
    for part in value.split(","):
        part = part.strip()
        if part:
            hour, _, minute = part.partition(":")
            slots.add((int(hour), int(minute or 0)))
    return tuple(sorted(slots))


def original_slots() -> tuple[tuple[int, int], ...]:
    """Daily times kept for original Reels (needs a free Pexels or Pixabay key for stock footage)."""
    if not reelmaker.stock_provider():
        return ()
    return parse_slots(setting("ORIGINAL_SLOTS", "11:00,22:00"))


def commentary_slots() -> tuple[tuple[int, int], ...]:
    reserved = set(original_slots())
    return tuple(slot for slot in PUBLISH_SLOTS if slot not in reserved) or PUBLISH_SLOTS


def next_publish_slot(occupied: set[datetime], slots: tuple[tuple[int, int], ...] = PUBLISH_SLOTS) -> datetime:
    now = local_now()
    for day_offset in range(0, 370):
        day = (now + timedelta(days=day_offset)).date()
        for hour, minute in slots:
            candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=LOCAL_TZ)
            if candidate > now and candidate not in occupied:
                return candidate
    raise RuntimeError("Could not find an open publish slot")


def existing_slots(ready_ws) -> set[datetime]:
    occupied: set[datetime] = set()
    for _, row in records(ready_ws):
        value = row.get("scheduled_at", "").strip()
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=LOCAL_TZ)
            occupied.add(parsed.astimezone(LOCAL_TZ))
        except ValueError:
            continue
    return occupied


def analyze_converted_videos(tabs: dict[str, Any]) -> tuple[int, int]:
    video_ws = tabs[VIDEO_TAB]
    ready_ws = tabs[READY_TAB]
    link_rows = {r.get("job_id", ""): (n, r) for n, r in records(tabs[REEL_LINK_TAB])}
    occupied = existing_slots(ready_ws)
    already_ready = {r.get("job_id", "") for _, r in records(ready_ws)}
    completed = failed = 0
    rows_to_remove: list[int] = []
    for row_number, row in records(video_ws):
        if row.get("status", "").upper() != "VIDEO_READY":
            continue
        ready_path: Path | None = None
        try:
            if row.get("job_id", "") in already_ready:
                rows_to_remove.append(row_number)
                continue
            path = Path(row.get("video_file", ""))
            if not path.is_absolute():
                path = ROOT / path
            if not path.is_file():
                raise RuntimeError("Converted video file was not found")
            creator = row.get("creator", "").strip()
            if not creator:
                link_match = link_rows.get(row.get("job_id", ""))
                creator = (link_match[1].get("creator", "") if link_match else "").strip()
            draft = draft_from_video(path, creator)
            ready_dir = ROOT / "media" / "ready_to_post"
            ready_dir.mkdir(parents=True, exist_ok=True)
            destination = ready_dir / f"{row['job_id']}.mp4"
            ready_path = destination
            reelmaker.render_commentary(path, draft["intro"], draft["outro"], destination,
                                        hook=draft["hook"], credit=creator,
                                        reactions=draft["reactions"], voice=draft["voice"])
            remember_terms(draft["terms"])
            slot = next_publish_slot(occupied, commentary_slots())
            ready_record = {
                "job_id": row.get("job_id", ""), "source_url": row.get("source_url", ""),
                "source_message_id": row.get("source_message_id", ""),
                "video_file": str(destination.relative_to(ROOT)),
                "caption": draft["caption"], "hashtags": draft["hashtags"],
                "scheduled_at": slot.isoformat(timespec="minutes"), "posted_url": "",
                "status": "SCHEDULED", "stage": "PUBLISH", "attempts": "0",
                "error": "", "created_at": row.get("created_at", now_iso()), "updated_at": now_iso(),
                "content_type": "COMMENTARY", "creator": creator,
            }
            append_record(ready_ws, ready_record)
            already_ready.add(row.get("job_id", ""))
            occupied.add(slot)
            rows_to_remove.append(row_number)
            keep_source(row.get("job_id", ""), path)  # kept so the Reel can be re-edited until it posts
            link_match = link_rows.get(row.get("job_id", ""))
            if link_match:
                update_record(tabs[REEL_LINK_TAB], link_match[0], status="SCHEDULED", updated_at=now_iso())
            completed += 1
            print(f"{row.get('job_id')}: commentary Reel ready; scheduled {slot.strftime('%Y-%m-%d %I:%M %p')} India time")
        except Exception as exc:
            if row.get("job_id", "") in already_ready:
                # A sheet update after the Ready row was saved failed; preserve the
                # successful job and let the next pass clean up its source row.
                rows_to_remove.append(row_number)
                continue
            if ready_path:
                ready_path.unlink(missing_ok=True)  # the raw download stays for retry-analysis
            fail_job(tabs, row, "AI_ANALYSIS_OR_SCHEDULING", str(exc))
            update_record(video_ws, row_number, status="FAILED", error=str(exc)[:500], updated_at=now_iso())
            link_match = link_rows.get(row.get("job_id", ""))
            if link_match:
                update_record(tabs[REEL_LINK_TAB], link_match[0], status="FAILED", error=str(exc)[:500], updated_at=now_iso())
            failed += 1
            print(f"{row.get('job_id')}: failed; recorded in Failed Links")
    for row_number in sorted(rows_to_remove, reverse=True):
        video_ws.delete_rows(row_number)
    return completed, failed


SOURCES_DIR = ROOT / "media" / "sources"


def keep_source(job_id: str, path: Path) -> Path:
    """Move a downloaded source clip to media/sources; the publisher deletes it once posted."""
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    target = SOURCES_DIR / f"{job_id}{path.suffix.lower() or '.mp4'}"
    if path.resolve() != target.resolve():
        shutil.move(str(path), str(target))
    return target


def source_for(job_id: str, source_url: str) -> Path:
    """The kept source clip for a job, downloading it again from Instagram if it is gone."""
    for existing in sorted(SOURCES_DIR.glob(f"{job_id}.*")):
        if existing.suffix.lower() in {".mp4", ".mov"}:
            return existing
    import yt_dlp

    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {"format": "best[ext=mp4]/best", "outtmpl": str(SOURCES_DIR / f"{job_id}.%(ext)s"),
                               "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
                               "socket_timeout": 60, "max_filesize": YTDLP_MAX_VIDEO_BYTES}
    browser = setting("YTDLP_BROWSER", "firefox").lower()
    if browser and browser != "none":
        options["cookiesfrombrowser"] = (browser, None, None, None)
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.extract_info(source_url, download=True)
    for downloaded in sorted(SOURCES_DIR.glob(f"{job_id}.*")):
        if downloaded.suffix.lower() in {".mp4", ".mov"}:
            return downloaded
    raise RuntimeError("Source clip could not be downloaded again")


def rerender_queue(skip_newer_than: datetime | None = None) -> tuple[int, int]:
    """Re-edit every scheduled commentary Reel with the current style, keeping its slot.

    Safe to run while the worker is running: each row is looked up again by job_id
    before saving (posting a Reel deletes rows and shifts the rest), and Reels due
    within 15 minutes are left alone. To resume an interrupted run, pass the time it
    started: Reels already re-edited since then are skipped instead of paying again.
    """
    ready_ws = ensure_tabs()[READY_TAB]
    done = failed = 0
    for _row_number, row in records(ready_ws):
        if (row.get("status", "").strip().upper() != "SCHEDULED"
                or row.get("content_type", "").strip().upper() != "COMMENTARY"):
            continue
        try:
            due = datetime.fromisoformat(row.get("scheduled_at", "").replace("Z", "+00:00"))
            if due.tzinfo is None:
                due = due.replace(tzinfo=LOCAL_TZ)
            if due <= local_now() + timedelta(minutes=15):
                continue
        except ValueError:
            pass
        if skip_newer_than:
            try:
                updated = datetime.fromisoformat(row.get("updated_at", "").replace("Z", "+00:00"))
                if updated >= skip_newer_than:
                    continue
            except ValueError:
                pass
        job_id = row.get("job_id", "")
        try:
            source = source_for(job_id, row.get("source_url", ""))
            creator = row.get("creator", "").strip()
            draft = draft_from_video(source, creator)
            destination = ROOT / "media" / "ready_to_post" / f"{job_id}-{int(time.time())}.mp4"
            reelmaker.render_commentary(source, draft["intro"], draft["outro"], destination,
                                        hook=draft["hook"], credit=creator,
                                        reactions=draft["reactions"], voice=draft["voice"])
            current = next((n for n, r in records(ready_ws) if r.get("job_id") == job_id
                            and r.get("status", "").strip().upper() == "SCHEDULED"), None)
            if current is None:  # posted or rejected meanwhile
                destination.unlink(missing_ok=True)
                continue
            remember_terms(draft["terms"])
            update_record(ready_ws, current, video_file=str(destination.relative_to(ROOT)),
                          caption=draft["caption"], hashtags=draft["hashtags"], updated_at=now_iso())
            old = Path(row.get("video_file", "").strip())
            old = old if old.is_absolute() else ROOT / old
            if old.is_file() and old.resolve() != destination.resolve():
                old.unlink()
            done += 1
            print(f"{job_id}: re-edited for {row.get('scheduled_at', '')}")
        except Exception as exc:
            failed += 1
            print(f"{job_id}: kept the previous version ({type(exc).__name__}: {str(exc)[:150]})")
    return done, failed


def creator_from_url(source_url: str) -> str:
    """Look up the original creator's username without downloading the video again."""
    try:
        import yt_dlp

        options: dict[str, Any] = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 30}
        browser = setting("YTDLP_BROWSER", "firefox").lower()
        if browser and browser != "none":
            options["cookiesfrombrowser"] = (browser, None, None, None)
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(source_url, download=False)
        return str(info.get("channel") or "").strip()
    except Exception:
        return ""


def upgrade_raw_ready_rows(tabs: dict[str, Any]) -> int:
    """Add commentary to Reels queued before this step existed, keeping their scheduled slot."""
    ready_ws = tabs[READY_TAB]
    limit = max(1, int(setting("UPGRADE_PER_PASS", "2") or 2))
    pending = [
        (number, row) for number, row in records(ready_ws)
        if row.get("status", "").strip().upper() == "SCHEDULED"
        and row.get("content_type", "").strip().upper() not in PUBLISHABLE_TYPES
    ]
    pending.sort(key=lambda item: item[1].get("scheduled_at", ""))
    upgraded = 0
    for row_number, row in pending[:limit]:
        job_id = row.get("job_id", "")
        source = Path(row.get("video_file", "").strip())
        if not source.is_absolute():
            source = ROOT / source
        try:
            if not source.is_file():
                raise RuntimeError("Queued video file was not found")
            creator = row.get("creator", "").strip() or creator_from_url(row.get("source_url", ""))
            draft = draft_from_video(source, creator)
            destination = source.with_name(f"{job_id}-commentary.mp4")
            reelmaker.render_commentary(source, draft["intro"], draft["outro"], destination,
                                        hook=draft["hook"], credit=creator,
                                        reactions=draft["reactions"], voice=draft["voice"])
            remember_terms(draft["terms"])
            update_record(
                ready_ws, row_number, video_file=str(destination.relative_to(ROOT)),
                caption=draft["caption"], hashtags=draft["hashtags"], content_type="COMMENTARY",
                creator=creator, error="", updated_at=now_iso(),
            )
            keep_source(job_id, source)
            upgraded += 1
            print(f"{job_id}: commentary added; keeps its {row.get('scheduled_at', '')} slot")
        except Exception as exc:
            update_record(ready_ws, row_number, status="FAILED",
                          error=f"Commentary step failed: {exc}"[:500], updated_at=now_iso())
            print(f"{job_id}: commentary step failed; it will move to Failed Links")
    return upgraded


ORIGINAL_BACKOFF = ROOT / ".original-failed-at"


def plan_original(client: OpenAI, recent_topics: list[str]) -> dict[str, Any]:
    niche = setting("ORIGINAL_NICHE", "funny and surprising facts about animals, kids, food and everyday life")
    avoid = "; ".join(recent_topics[-40:]) or "none yet"
    result = ask_json(client, [
        {"role": "system", "content": "You plan short, original, factual vertical videos for Instagram Reels."},
        {"role": "user", "content": (
            f"Plan one original 20-40 second Reel for an account about: {niche}.\n"
            f"Pick a fresh topic that is not one of these recent topics: {avoid}.\n"
            "Return JSON with:\n"
            "- topic: 3-6 words\n"
            "- hook: on-screen title, at most 7 words, no emoji\n"
            "- caption: 1-2 sentences for the post, no hashtags\n"
            "- hashtags: 3 to 5 hashtags in one space-separated string\n"
            '- scenes: 5 to 7 items, each {"say", "show": one spoken sentence of 8-18 words, "search": a '
            "2-4 word English stock-footage search that shows that sentence literally}\n"
            "The first sentence is a strong hook and the last asks viewers a question. Use only well-known, "
            "true facts and no made-up numbers. No emoji. Keep search terms in English.\n"
            f"{language_rules()}\n{slang_rules(slang_level(), recent_terms())}"
        )},
    ])
    scenes = [s for s in result.get("scenes", []) if isinstance(s, dict) and (s.get("say") or s.get("line"))]
    if not str(result.get("caption", "")).strip() or len(scenes) < 3:
        raise RuntimeError("AI returned an incomplete original Reel plan")
    result["scenes"] = scenes
    return result


def produce_original_reels(tabs: dict[str, Any]) -> int:
    """Fill the next free original slot (within the lookahead) with a fully original Reel."""
    slots = original_slots()
    if not slots:
        return 0
    if ORIGINAL_BACKOFF.exists() and time.time() - ORIGINAL_BACKOFF.stat().st_mtime < 1800:
        return 0
    ready_ws = tabs[READY_TAB]
    slot = next_publish_slot(existing_slots(ready_ws), slots)
    lookahead = timedelta(hours=max(1, int(setting("ORIGINAL_LOOKAHEAD_HOURS", "36") or 36)))
    if slot > local_now() + lookahead:
        return 0
    recent = [row.get("topic", "").strip() for tab in (READY_TAB, POSTED_TAB)
              for _, row in records(tabs[tab]) if row.get("topic", "").strip()]
    destination: Path | None = None
    try:
        plan = plan_original(ai_client(), recent)
        topic = str(plan.get("topic", "")).strip()
        digest = hashlib.sha256(f"{slot.isoformat()}|{topic}".encode("utf-8")).hexdigest()[:12].upper()
        job_id = f"OG-{digest}"
        destination = ROOT / "media" / "ready_to_post" / f"{job_id}.mp4"
        reelmaker.render_original(plan, destination)
        append_record(ready_ws, {
            "job_id": job_id, "source_url": "", "source_message_id": "",
            "video_file": str(destination.relative_to(ROOT)),
            "caption": str(plan.get("caption", "")).strip(), "hashtags": str(plan.get("hashtags", "")).strip(),
            "scheduled_at": slot.isoformat(timespec="minutes"), "posted_url": "",
            "status": "SCHEDULED", "stage": "PUBLISH", "attempts": "0", "error": "",
            "created_at": now_iso(), "updated_at": now_iso(),
            "content_type": "ORIGINAL", "creator": "", "topic": topic,
        })
    except Exception as exc:
        if destination:
            destination.unlink(missing_ok=True)
        ORIGINAL_BACKOFF.write_text(now_iso(), encoding="utf-8")
        print(f"Original Reel failed ({type(exc).__name__}: {str(exc)[:200]}); will retry in 30 minutes")
        return 0
    ORIGINAL_BACKOFF.unlink(missing_ok=True)
    print(f"{job_id}: original Reel '{topic}' scheduled {slot.strftime('%Y-%m-%d %I:%M %p')} India time")
    return 1


def collect_declared_conversion_failures(tabs: dict[str, Any]) -> int:
    failed = 0
    for row_number, row in records(tabs[REEL_LINK_TAB]):
        if row.get("status", "").upper() not in {"CONVERSION_FAILED", "DOWNLOAD_FAILED"}:
            continue
        fail_job(tabs, row, "VIDEO_DOWNLOAD", row.get("error", "Video conversion did not complete") or "Video conversion did not complete")
        update_record(tabs[REEL_LINK_TAB], row_number, status="FAILED", stage="VIDEO_DOWNLOAD", updated_at=now_iso())
        failed += 1
    return failed


def run_once(tabs: dict[str, Any] | None = None, *, publish: bool = True) -> None:
    tabs = tabs or ensure_tabs()
    try:
        _seen, added, _skipped = import_webhook_events(tabs)
        if added:
            print(f"Reel Link: received {added} new Reel link(s)")
    except Exception as exc:
        print(f"DM sync failed ({type(exc).__name__})")
    try:
        declared_failed = collect_declared_conversion_failures(tabs)
        downloaded, download_failed = automatic_downloads(tabs)
        imported = import_converted_videos(tabs)
        completed, failed = analyze_converted_videos(tabs)
        if imported:
            print(f"Video Convert: found {imported} converted video(s)")
        if downloaded:
            print(f"Automatic downloads: {downloaded} video(s)")
        if completed or failed:
            print(f"Analysis: {completed} ready, {failed} failed")
        if declared_failed or download_failed:
            print(f"Download failures routed: {declared_failed + download_failed}")
    except Exception as exc:
        print(f"Pipeline pass failed ({type(exc).__name__})")
    try:
        upgraded = upgrade_raw_ready_rows(tabs)
        if upgraded:
            print(f"Commentary added to {upgraded} queued Reel(s)")
    except Exception as exc:
        print(f"Commentary upgrade pass failed ({type(exc).__name__})")
    try:
        produce_original_reels(tabs)
    except Exception as exc:
        print(f"Original Reel pass failed ({type(exc).__name__})")
    if not publish:
        return
    try:
        from publisher import publish_due_rows

        published = publish_due_rows(tabs)
        if published:
            print(f"Publisher: posted {published} Reel(s)")
    except Exception as exc:
        print(f"Publishing pass failed ({type(exc).__name__})")


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Process Reel links and converted videos")
    parser.add_argument("command", choices=[
        "setup", "setup-voice", "once", "prepare", "run", "retry-downloads", "retry-analysis",
        "preview", "preview-original", "rerender",
    ])
    parser.add_argument("--poll-seconds", type=int, default=180)
    parser.add_argument("--video", help="preview: local video to turn into a commentary Reel")
    parser.add_argument("--creator", default="", help="preview: original creator's Instagram username")
    parser.add_argument("--resume-from", help="rerender: skip Reels re-edited after this ISO time (e.g. 2026-11-19T20:40+05:30)")
    args = parser.parse_args()
    if args.command == "setup-voice":
        reelmaker.download_voice_model()
        print("Voice model is ready.")
    elif args.command == "preview":
        # Renders to media/preview without touching the sheet or Instagram.
        if not args.video:
            parser.error("preview needs --video PATH")
        source = Path(args.video).resolve()
        draft = draft_from_video(source, args.creator)
        out = reelmaker.render_commentary(
            source, draft["intro"], draft["outro"], ROOT / "media" / "preview" / f"{source.stem}-commentary.mp4",
            hook=draft["hook"], credit=args.creator, reactions=draft["reactions"], voice=draft["voice"],
        )
        shown = {"hook": draft["hook"], "voice": draft["voice"], "kids": draft["kids"], "terms": draft["terms"],
                 "reactions": [f"{t:.1f}s {line[1]}" for t, line in draft["reactions"]],
                 "intro": [s for _x, s in draft["intro"]], "outro": [s for _x, s in draft["outro"]],
                 "caption": draft["caption"], "hashtags": draft["hashtags"]}
        print(json.dumps(shown, indent=2, ensure_ascii=True))
        print(f"Preview saved: {out}")
    elif args.command == "rerender":
        since = datetime.fromisoformat(args.resume_from) if args.resume_from else None
        if since and since.tzinfo is None:
            since = since.replace(tzinfo=LOCAL_TZ)
        done, failed = rerender_queue(since)
        print(f"Re-edited {done} scheduled Reel(s); {failed} kept their previous version.")
    elif args.command == "preview-original":
        plan = plan_original(ai_client(), [])
        out = reelmaker.render_original(plan, ROOT / "media" / "preview" / "original-preview.mp4")
        print(json.dumps(plan, indent=2, ensure_ascii=True))
        print(f"Preview saved: {out}")
    elif args.command == "setup":
        ensure_tabs()
        for folder in (ROOT / "media" / "video_convert", ROOT / "media" / "ready_to_post"):
            folder.mkdir(parents=True, exist_ok=True)
        print("Workflow tabs and local video folders are ready.")
    elif args.command == "retry-downloads":
        count = retry_failed_downloads()
        print(f"Requeued {count} failed video download(s).")
    elif args.command == "retry-analysis":
        count = retry_failed_analysis()
        print(f"Requeued {count} downloaded video(s) for AI analysis.")
    elif args.command == "once":
        tabs = ensure_tabs()
        migrated = import_reviewed_legacy_rows(tabs)
        if migrated:
            print(f"Reel Queue: imported {migrated} reviewed Reel(s)")
        run_once(tabs)
    elif args.command == "prepare":
        tabs = ensure_tabs()
        migrated = import_reviewed_legacy_rows(tabs)
        if migrated:
            print(f"Reel Queue: imported {migrated} reviewed Reel(s)")
        run_once(tabs, publish=False)
    else:
        tabs = ensure_tabs()
        migrated = import_reviewed_legacy_rows(tabs)
        if migrated:
            print(f"Reel Queue: imported {migrated} reviewed Reel(s)")
        print("Reel pipeline is running. Press Ctrl+C to stop.")
        while True:
            run_once(tabs)
            time.sleep(max(30, args.poll_seconds))


if __name__ == "__main__":
    main()
