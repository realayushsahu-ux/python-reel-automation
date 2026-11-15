# ReelFlow AI

**A permission-aware Instagram Reel pipeline that turns a shared Reel into a scheduled post.**

ReelFlow AI connects Instagram messaging, a Google Sheets queue, local video processing, AI-assisted caption writing, and Instagram publishing. It is designed to keep each Reel traceable from the incoming link to its final result, while allowing the operator to edit or reject a draft before it is published.

> Use this project only for Reels you have permission to repost. Instagram availability, account permissions, and platform limits can still affect downloads or publishing.

## What it does

- Receives Reel links from Instagram webhook events through a Cloudflare Worker relay.
- Filters incoming events to the configured sender and destination account, then deduplicates them.
- Downloads accessible Reels locally with `yt-dlp` and an optional signed-in Firefox session.
- Samples up to eight frames with OpenCV and transcribes available audio with FFmpeg plus the configured transcription API.
- Uses an OpenAI-compatible vision model to draft a caption and relevant hashtags.
- Schedules each ready Reel in the next available India-time slot: **11:00, 18:00, 22:00, or 23:00 Asia/Kolkata**.
- Publishes due Reels through Meta's Instagram Graph API and archives the result.
- Separates failed work and operator-rejected Reels so one problem does not block the rest of the queue.
- Runs as a local Windows background task; the computer must be awake and online for downloads and publishing.

## Workflow

```mermaid
flowchart LR
    A[Authorized Reel shared in DM] --> B[Meta webhook]
    B --> C[Cloudflare Worker relay]
    C --> D[Local Python worker]
    D --> E[Reel Link queue]
    E --> F[yt-dlp download]
    F -->|download error| X[Failed Links]
    F --> G[Video Convert]
    G --> H[Sample frames and optional audio transcript]
    H --> I[AI caption and hashtags]
    I -->|analysis error| X
    I --> J[Ready to Post]
    J -->|operator edits caption| J
    J -->|status: REJECTED_BY_AYUSH| R[Rejected Reels]
    J -->|scheduled time| K[Meta publishing API]
    K -->|publish error| X
    K --> L[Posted Reels]
```

### Queue lifecycle

| Sheet tab | Purpose |
| --- | --- |
| `Reel Link` | Incoming, deduplicated Reel links |
| `Video Convert` | Downloaded local videos waiting for analysis |
| `Ready to Post` | Captioned videos with the next available schedule |
| `Posted Reels` | Successfully published Reels and their post links |
| `Failed Links` | Download, analysis, scheduling, or publishing errors |
| `Rejected Reels` | Items explicitly rejected by the operator |

To reject a Reel, change its `status` in `Ready to Post` to the value configured by `REJECTION_STATUS` (default: `REJECTED`). The worker moves it to `Rejected Reels` on its next pass; it will not publish it. Caption edits in `Ready to Post` are picked up at publishing time.

## Setup

### Requirements

- Windows 10/11 and Python 3.10+
- Node.js/npm for deploying the optional Cloudflare Worker relay
- A Google Sheet and a Google service account with edit access to that sheet
- A Meta developer app with Instagram messaging/webhook and content-publishing setup
- An API key for the configured caption model; audio transcription is optional when a Reel has no usable audio

### Install the Python worker

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill the required values in `.env` on your computer. Save your Google service account credentials as `service-account.json`, grant that service account access to the spreadsheet, then create the workflow tabs:

```powershell
.\.venv\Scripts\python.exe pipeline.py setup
```

The real `.env` and credential file are intentionally excluded from Git. Never paste their contents into an issue or chat.

### Configure Meta and the relay

1. Create/configure a Meta app for the Instagram account and grant the required publishing and messaging permissions.
2. Deploy the relay from `cloudflare-relay/deploy.ps1`. The script creates a private local `wrangler.toml` from `wrangler.example.toml` when needed, provisions the KV binding, stores secrets with Wrangler, and deploys the Worker.
3. Set Meta's webhook callback to the deployed Worker endpoint ending in `/meta/webhook`, complete Meta's verify-token check, and subscribe to the `messages` field.
4. Configure the sender/destination filters and relay settings in `.env`.

The public template contains no account ID or KV namespace ID. Wrangler creates/updates the private local config during deployment; that local config and `.wrangler` state are ignored by Git.

### Run locally

For a foreground session:

```powershell
.\.venv\Scripts\python.exe pipeline.py run
```

For unattended Windows use, configure Task Scheduler to run the same command at user logon, with the project directory as the working directory and multiple instances set to **Ignore new instance**. Keep the PC awake and online for scheduled publishing. The deployed Worker can queue webhook events while the PC is offline; the local worker processes them when it runs again.

## Failure handling

- Downloads retry automatically up to the configured attempt limit. Unresolved failures are recorded in `Failed Links`; the worker continues with other Reels.
- After fixing a download/cookie problem, stop the current worker, then run:

  ```powershell
  .\.venv\Scripts\python.exe pipeline.py retry-downloads
  ```

- For an analysis failure where the video is already downloaded, run:

  ```powershell
  .\.venv\Scripts\python.exe pipeline.py retry-analysis
  ```

- Publishing failures are recorded for inspection. Do not blindly retry a publish: Meta may have accepted a post even if the client did not receive a successful response, which can create duplicates.
- If Google Sheets returns a quota error, avoid running multiple workers and wait before retrying.

## Privacy and security

- `.env`, service-account credentials, Meta tokens, webhook-event logs, local Wrangler config/state, cookies, media, and personal setup notes are excluded by `.gitignore`.
- `.env.example` contains variable names and placeholders only; put real values in the ignored `.env` file.
- The relay verifies Meta webhook signatures and stores only the event fields needed to retrieve and deduplicate shared Reel links.
- Video files stay on the local computer; the AI step receives sampled frames and, when available, a compressed audio transcription input—not a request to fetch the Instagram URL itself.
- Before pushing changes, inspect `git status` and run a secret scanner. Never commit `.env`, `service-account.json`, `meta-token.json`, `.wrangler/`, or files from `media/`.

## Limitations

- Instagram does not guarantee that every shared Reel can be downloaded. Private, removed, region-restricted, or otherwise inaccessible media may fail even when the sender has permission to repost it.
- Visual analysis samples up to eight frames, so brief events between samples can be missed. Audio transcription is optional and incurs provider usage when audio is available.
- The local computer performs downloads and publishing. Sleep, shutdown, internet loss, expired permissions, rate limits, and API budget limits can delay or stop work.
- This project uses platform APIs and local automation; it does not bypass Instagram access controls.

## Technology

Python · Google Sheets API · Meta Instagram Graph API · Cloudflare Workers/KV · yt-dlp · OpenCV · FFmpeg · OpenAI-compatible vision and transcription APIs
