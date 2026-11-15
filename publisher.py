"""Schedule and publish approved local MP4 reels to the connected Instagram account."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import gspread
from dotenv import load_dotenv

from app import HEADERS, connect_sheet, now_iso
from pipeline import FAILED_TAB, POSTED_TAB, READY_TAB, REJECTED_TAB, append_record, ensure_tabs, job_id_for, records


ROOT = Path(__file__).resolve().parent
INDIA = timezone(timedelta(hours=5, minutes=30))
PUBLISH_TIMES = (wall_time(11, 0), wall_time(18, 0), wall_time(22, 0), wall_time(23, 0))
MAX_VIDEO_BYTES = 1024 * 1024 * 1024
POLL_SECONDS = 3
POLL_ATTEMPTS = 30


class PublishError(RuntimeError):
    pass


def setting(name: str) -> str:
    return os.getenv(name, "").strip()


def graph_version() -> str:
    return setting("META_GRAPH_VERSION") or "v26.0"


def load_access_token() -> str:
    token_path = ROOT / "meta-token.json"
    try:
        token = json.loads(token_path.read_text(encoding="utf-8")).get("access_token", "")
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishError("Could not read the saved Meta access token") from exc
    if not token:
        raise PublishError("No Meta access token is saved; reconnect the Instagram account")
    return str(token)


def graph_call(method: str, endpoint: str, token: str, **kwargs: Any) -> dict[str, Any]:
    url = f"https://graph.facebook.com/{graph_version()}/{endpoint.lstrip('/')}"
    params = kwargs.pop("params", {})
    params["access_token"] = token
    try:
        response = requests.request(method, url, params=params, timeout=45, **kwargs)
    except requests.RequestException as exc:
        raise PublishError("Could not reach Meta's publishing API") from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise PublishError(f"Meta returned HTTP {response.status_code} with an unreadable response") from exc
    if not response.ok or "error" in data:
        message = data.get("error", {}).get("message", f"Meta returned HTTP {response.status_code}")
        raise PublishError(str(message)[:400])
    return data


def read_schedule(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        else:
            raise PublishError("scheduled_at must be a date/time such as 2026-11-08 18:00")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=INDIA)
    return parsed.astimezone(INDIA)


def row_maps(worksheet):
    sheet_values = worksheet.get_all_values()
    headers = sheet_values[0] if sheet_values else []
    missing = [name for name in HEADERS if name not in headers]
    for name in missing:
        headers.append(name)
        worksheet.update_cell(1, len(headers), name)
    for row_number, row in enumerate(sheet_values[1:], start=2):
        padded = row + [""] * max(0, len(headers) - len(row))
        yield row_number, dict(zip(headers, padded))


def update_fields(worksheet, headers: list[str], row_number: int, **fields: str) -> None:
    updates = []
    for name, value in fields.items():
        if name in headers:
            cell = gspread.utils.rowcol_to_a1(row_number, headers.index(name) + 1)
            updates.append({"range": cell, "values": [[value]]})
    if updates:
        worksheet.batch_update(updates, value_input_option="RAW")


def next_slot(after: datetime, occupied: set[datetime]) -> datetime:
    local = after.astimezone(INDIA)
    for day_offset in range(0, 370):
        day = (local + timedelta(days=day_offset)).date()
        for slot in PUBLISH_TIMES:
            candidate = datetime.combine(day, slot, INDIA)
            if candidate > local and candidate not in occupied:
                return candidate
    raise PublishError("Could not find the next posting slot")


def schedule_approved() -> int:
    worksheet = connect_sheet()
    headers = worksheet.row_values(1)
    rows = list(row_maps(worksheet))
    now = datetime.now(INDIA)
    occupied: set[datetime] = set()
    for _row_number, values in rows:
        if values.get("status", "").strip().upper() == "SCHEDULED":
            scheduled = read_schedule(values.get("scheduled_at", ""))
            if scheduled and scheduled > now:
                occupied.add(scheduled)
    scheduled_count = 0
    for row_number, values in rows:
        if values.get("status", "").strip().upper() != "APPROVED":
            continue
        if not values.get("caption", "").strip() or not values.get("video_file", "").strip():
            print(f"Row {row_number}: approved, but needs a caption and local video_file")
            continue
        slot = next_slot(now, occupied)
        update_fields(
            worksheet,
            headers,
            row_number,
            scheduled_at=slot.isoformat(timespec="minutes"),
            status="SCHEDULED",
            error="",
            updated_at=now_iso(),
        )
        print(f"Row {row_number}: scheduled for {slot.strftime('%Y-%m-%d %I:%M %p')} India time")
        occupied.add(slot)
        scheduled_count += 1
    return scheduled_count


def publish_one(worksheet, headers: list[str], row_number: int, values: dict[str, str]) -> None:
    token = load_access_token()
    instagram_id = setting("META_INSTAGRAM_ACCOUNT_ID")
    if not instagram_id:
        raise PublishError("Set META_INSTAGRAM_ACCOUNT_ID in .env")

    video_path = Path(values.get("video_file", "").strip()).expanduser()
    if not video_path.is_absolute():
        video_path = ROOT / video_path
    if not video_path.is_file():
        raise PublishError("video_file does not point to a local file")
    if video_path.suffix.lower() not in {".mp4", ".mov"}:
        raise PublishError("video_file must be an MP4 or MOV file")
    file_size = video_path.stat().st_size
    if not file_size or file_size > MAX_VIDEO_BYTES:
        raise PublishError("Video file is empty or exceeds Meta's 1 GB Reel limit")

    caption = values.get("caption", "").strip()
    hashtags = values.get("hashtags", "").strip()
    if hashtags:
        caption = f"{caption}\n\n{hashtags}" if caption else hashtags
    if not caption:
        raise PublishError("Caption is empty")

    # Mark in progress before making remote calls to avoid duplicate posts on a retry.
    update_fields(worksheet, headers, row_number, status="PUBLISHING", error="", updated_at=now_iso())
    container = graph_call(
        "POST",
        f"{instagram_id}/media",
        token,
        data={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "share_to_feed": "true",
        },
    )
    container_id = str(container.get("id", ""))
    upload_uri = str(container.get("uri", ""))
    if not container_id or not upload_uri:
        raise PublishError("Meta did not return the Reel container ID and upload URL")

    try:
        with video_path.open("rb") as video:
            upload_response = requests.post(
                upload_uri,
                data=video,
                headers={
                    "Authorization": f"OAuth {token}",
                    "offset": "0",
                    "file_size": str(file_size),
                    "Content-Type": "application/octet-stream",
                },
                timeout=180,
            )
        upload_response.raise_for_status()
    except requests.RequestException as exc:
        raise PublishError("Video upload to Meta failed") from exc

    status = ""
    for _ in range(POLL_ATTEMPTS):
        status_data = graph_call(
            "GET", container_id, token, params={"fields": "status_code,status"}
        )
        status = str(status_data.get("status_code", ""))
        if status == "FINISHED":
            break
        if status in {"ERROR", "EXPIRED"}:
            raise PublishError("Meta could not process the uploaded video")
        time.sleep(POLL_SECONDS)
    else:
        raise PublishError("Meta is still processing the video; no publish request was sent")

    published = graph_call(
        "POST",
        f"{instagram_id}/media_publish",
        token,
        data={"creation_id": container_id},
    )
    media_id = str(published.get("id", ""))
    permalink = ""
    if media_id:
        try:
            permalink = str(graph_call("GET", media_id, token, params={"fields": "permalink"}).get("permalink", ""))
        except PublishError:
            pass
    update_fields(
        worksheet,
        headers,
        row_number,
        status="POSTED",
        posted_url=permalink or media_id,
        error="",
        updated_at=now_iso(),
    )
    print(f"Row {row_number}: published successfully")


def publish_due_rows(tabs=None) -> int:
    tabs = tabs or ensure_tabs()
    # Also watch the original queue until its already-scheduled row(s) have been
    # published, so upgrading the workflow does not discard the existing 6 PM post.
    sources = [tabs[READY_TAB]]
    try:
        legacy = connect_sheet()
        if legacy.id != tabs[READY_TAB].id:
            sources.append(legacy)
    except Exception:
        pass
    now = datetime.now(INDIA)
    published_count = 0
    for worksheet in sources:
        headers = worksheet.row_values(1)
        for row_number, values in sorted(row_maps(worksheet), key=lambda item: item[0], reverse=True):
            current_status = values.get("status", "").strip().upper()
            if current_status == "POSTED":
                try:
                    archive_processed_row(worksheet, row_number, values, tabs[POSTED_TAB], posted=True)
                except Exception:
                    print(f"Row {row_number}: posted, but archive transfer will retry")
                continue
            rejection_status = (setting("REJECTION_STATUS") or "REJECTED").upper()
            if current_status == rejection_status:
                try:
                    archive_processed_row(worksheet, row_number, values, tabs[REJECTED_TAB], posted=False, rejected=True)
                    print(f"Row {row_number}: moved to Rejected Reels; it will not be published")
                except Exception:
                    print(f"Row {row_number}: rejected, but archive transfer will retry")
                continue
            if current_status == "FAILED":
                try:
                    archive_processed_row(worksheet, row_number, values, tabs[FAILED_TAB], posted=False,
                                          error=values.get("error", ""))
                except Exception:
                    print(f"Row {row_number}: failed, but archive transfer will retry")
                continue
            if current_status != "SCHEDULED":
                continue
            scheduled = read_schedule(values.get("scheduled_at", ""))
            if not scheduled or scheduled > now:
                continue
            try:
                publish_one(worksheet, headers, row_number, values)
                published_count += 1
            except Exception as exc:
                update_fields(
                    worksheet,
                    headers,
                    row_number,
                    status="FAILED",
                    error=str(exc)[:500],
                    updated_at=now_iso(),
                )
                try:
                    archive_processed_row(worksheet, row_number, values, tabs[FAILED_TAB], posted=False, error=str(exc))
                except Exception:
                    print(f"Row {row_number}: failed, but archive transfer will retry")
                print(f"Row {row_number}: publish failed and was moved to the failed tab")
                continue
            try:
                archive_processed_row(worksheet, row_number, values, tabs[POSTED_TAB], posted=True)
            except Exception:
                print(f"Row {row_number}: posted, but archive transfer will retry")
    return published_count


def archive_processed_row(source_ws, row_number: int, values: dict[str, str], destination_ws,
                          *, posted: bool, error: str = "", rejected: bool = False) -> None:
    source_headers = source_ws.row_values(1)
    raw = source_ws.row_values(row_number)
    padded = raw + [""] * max(0, len(source_headers) - len(raw))
    record = dict(zip(source_headers, padded))
    msg_id = record.get("source_message_id", "").strip()
    if not record.get("job_id", "").strip():
        fallback = hashlib.sha256(record.get("source_url", "").encode("utf-8")).hexdigest()[:12].upper()
        record["job_id"] = job_id_for(msg_id) if msg_id else f"LEGACY-{fallback}"
    record["status"] = str(values.get("status", "REJECTED")).strip().upper() if rejected else ("POSTED" if posted else "FAILED")
    record["stage"] = "REJECTED" if rejected else "PUBLISH"
    record["attempts"] = str(int(record.get("attempts", "0") or 0) + (0 if posted or rejected else 1))
    if rejected:
        record["error"] = ""
    elif error:
        record["error"] = error[:500]
    record["updated_at"] = now_iso()
    if posted:
        record["posted_at"] = now_iso()
    if rejected:
        record["rejected_at"] = now_iso()

    destination_headers = destination_ws.row_values(1)
    job_col = destination_headers.index("job_id") + 1
    existing_jobs = set(destination_ws.col_values(job_col)[1:])
    if record["job_id"] not in existing_jobs:
        append_record(destination_ws, record)
    source_ws.delete_rows(row_number)


def run_scheduler(poll_seconds: int) -> None:
    print("Publisher is watching scheduled Reel rows in India time. Press Ctrl+C to stop.")
    while True:
        publish_due_rows()
        time.sleep(max(15, poll_seconds))


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Schedule and publish approved Reel rows")
    parser.add_argument("command", choices=["schedule-approved", "publish-due", "run"])
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args()
    if args.command == "schedule-approved":
        count = schedule_approved()
        print(f"Scheduled {count} approved Reel(s) for the next 11 AM / 6 PM / 10 PM / 11 PM India slots.")
    elif args.command == "publish-due":
        count = publish_due_rows()
        if not count:
            print("No scheduled Reel is due right now.")
    else:
        run_scheduler(args.poll_seconds)


if __name__ == "__main__":
    main()
