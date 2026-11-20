"""Import Instagram Reel shares and draft captions from a Google Sheet queue."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import gspread
from dotenv import load_dotenv
from openai import OpenAI


HEADERS = [
    "source_url",
    "content_description",
    "status",
    "caption",
    "hashtags",
    "scheduled_at",
    "posted_url",
    "error",
    "created_at",
    "updated_at",
    "source_message_id",
    "video_file",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect_sheet():
    credentials_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    worksheet_name = os.getenv("GOOGLE_WORKSHEET", "Reel Queue")
    if not sheet_id:
        raise RuntimeError("Set GOOGLE_SHEET_ID in .env")
    if not Path(credentials_file).is_file():
        raise RuntimeError(f"Google service account file not found: {credentials_file}")

    client = gspread.service_account(filename=credentials_file, http_client=gspread.BackOffHTTPClient)
    worksheet = client.open_by_key(sheet_id).worksheet(worksheet_name)
    existing = worksheet.row_values(1)
    if not existing:
        worksheet.append_row(HEADERS)
    else:
        for header in HEADERS:
            if header not in existing:
                existing.append(header)
                worksheet.update_cell(1, len(existing), header)
    return worksheet


def draft_caption(description: str, source_url: str) -> dict[str, str]:
    api_key = os.getenv("AI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Set AI_API_KEY in .env")

    client = OpenAI(
        api_key=api_key,
        base_url=os.getenv("AI_BASE_URL", "https://api.openai.com/v1"),
    )
    response = client.chat.completions.create(
        model=os.getenv("AI_MODEL", "gpt-6-luna"),
        reasoning_effort="none",
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "Write a clear, engaging social media caption for an Instagram reel. "
                    "Use only facts present in the description; do not invent claims. "
                    "Return JSON with string fields caption and hashtags. Put hashtags "
                    "as a space-separated string, using at most 8 relevant tags."
                ),
            },
            {
                "role": "user",
                "content": f"Reel description:\n{description}\n\nSource reference: {source_url}",
            },
        ],
    )
    content = response.choices[0].message.content or "{}"
    result = json.loads(content)
    caption = str(result.get("caption", "")).strip()
    hashtags = str(result.get("hashtags", "")).strip()
    if not caption:
        raise ValueError("AI returned an empty caption")
    return {"caption": caption, "hashtags": hashtags}


def draft_received_rows() -> tuple[int, int]:
    worksheet = connect_sheet()
    headers = worksheet.row_values(1)
    rows = worksheet.get_all_values()[1:]
    index = {name: headers.index(name) for name in HEADERS}
    processed = 0
    found = 0

    for row_number, row in enumerate(rows, start=2):
        values = row + [""] * max(0, len(headers) - len(row))
        if values[index["status"]].strip().upper() != "RECEIVED":
            continue
        found += 1

        try:
            description = values[index["content_description"]].strip()
            if not description:
                print(f"Row {row_number}: waiting for reel analysis or a description")
                continue
            result = draft_caption(description, values[index["source_url"]].strip())
            worksheet.update_cell(row_number, index["caption"] + 1, result["caption"])
            worksheet.update_cell(row_number, index["hashtags"] + 1, result["hashtags"])
            worksheet.update_cell(row_number, index["status"] + 1, "NEEDS_REVIEW")
            worksheet.update_cell(row_number, index["error"] + 1, "")
            worksheet.update_cell(row_number, index["updated_at"] + 1, now_iso())
            processed += 1
            print(f"Row {row_number}: draft ready for review")
        except Exception as exc:  # keep one bad row from blocking the rest
            worksheet.update_cell(row_number, index["status"] + 1, "FAILED")
            worksheet.update_cell(row_number, index["error"] + 1, str(exc)[:500])
            worksheet.update_cell(row_number, index["updated_at"] + 1, now_iso())
            print(f"Row {row_number}: failed: {exc}")

    return found, processed


def import_webhook_events() -> tuple[int, int, int]:
    """Compatibility command: route new DMs into the staged Reel Link workflow tab."""
    from pipeline import import_webhook_events as import_staged_events

    return import_staged_events()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Reel content workflow tools")
    parser.add_argument("command", choices=["draft", "sync-webhooks"], help="Workflow action to run")
    args = parser.parse_args()
    if args.command == "draft":
        found, drafted = draft_received_rows()
        if found == 0:
            print("No reel to post right now.")
        else:
            print(f"Created {drafted} caption draft(s) from {found} reel(s).")
    elif args.command == "sync-webhooks":
        seen, added, skipped = import_webhook_events()
        print(f"Checked {seen + skipped} captured event(s): added {added} Reel(s), skipped {skipped}.")


if __name__ == "__main__":
    main()
