# Instagram DM queue sender

For outreach from your dedicated account. Run only after approving the exact opener and main pitch text and target list. This uses Instagram's unofficial private API; there is no guaranteed safe sending rate. Instagram can limit, challenge, or disable a new account. Do not use the script to work around a block.

## Install

Python 3.10+ on Linux:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

`ffprobe` is useful to check media before running. Videos must be playable H.264 MP4 files (AAC audio if present), not merely renamed `.mp4` files. The script checks paths and extension, not codecs. Install `ffmpeg` if you need to transcode. Set restrictive file permissions for the account that runs the job.

## Queue

A JSON array, one unique username per target. Paths are absolute or relative to the queue file. `opener_text` and `message_text` are sent verbatim; review both templates and any brand-specific substitutions in the queue before a live run. The example below is illustrative, not approved copy.

```json
[
  {
    "ig_username": "example_jeweller",
    "video_path": "reels/example_jeweller.mp4",
    "opener_text": "Hi, I made a short sample reel for your jewellery. I'll send it here.",
    "message_text": "Here's the sample reel. If you'd like a set for your shop, I can share the details."
  }
]
```

The opener creates the thread. The video is sent next into that thread, then the main text into that same thread. This is **not video-first**: instagrapi 3.0.12 requires an existing thread for a direct video send. The server may still refuse a DM or a media attachment to a new contact. The sender stops on any ambiguous delivery, rather than risking a duplicate or sending a pitch without a reel.

## Run

Supply credentials through protected environment variables (not in the queue, command arguments, or a checked-in file):

```sh
export IG_USERNAME='your_dedicated_account'
export IG_PASSWORD='your_password'
python sender.py queue.json --dry-run
python sender.py queue.json
```

`--dry-run` validates login and paths, prints the exact opener, path and pitch per target, saves session settings, but **does not send**. If Instagram needs 2FA or a challenge, complete it in the official app/web on a trusted device and retry later. This script deliberately does not collect a code or solve a challenge unattended. Avoid swapping IP/device settings between runs. Do not blindly delete a STOPPED file and retry; inspect the account and thread first.

Optional flags/environment: `--session ig-session.json`, `--results queue.results.json`, `--daily-cap 12` (`IG_DAILY_CAP`), `--delay-min 240 --delay-max 540` in seconds (`IG_DELAY_MIN`, `IG_DELAY_MAX`), `--active-hours 09:00-18:00` (`IG_ACTIVE_HOURS`), `--timezone Asia/Kolkata` (`IG_TIMEZONE`). Delays are independently randomized before each send, including between opener, video, and pitch. Active hours can cross midnight. The cap counts **targets begun** per day in the configured timezone, not messages. It is a local limit, not a promise that Instagram accepts this volume.

Keep the queue, video, session and results on persistent storage. The session file contains sensitive authentication material; the script writes it mode 0600. Protect the parent directory and results file too. Do not commit them to Git. Results are bound to the account and a fingerprint of each recipient, video content, opener and pitch. Editing a recorded target causes a fail-closed exit until you reconcile it. Already done and failed recipients are never resent. Non-done targets are also not retried automatically, even when the interruption was after an unknown send outcome. On failure, `.STOPPED.json` records the stage; a `partial`/`in_flight` result also requires manual review. Only after checking the live thread for delivery should you reconcile results and clear the STOPPED file. A `done` or `failed` result is skipped; a partly sent target requires manual completion or explicit suppression, never just rerunning the same queue. Never restart through an Instagram action block.

Run a single process at a time. The script holds a local lock per results file. If you operate the account from other tools or phones, inspect those sends separately to avoid duplicating outreach. Log lines have timestamps; store stdout/stderr as desired.

## Cron / Hermes

Put secrets in a private env file with mode 0600 **outside the repository**, for example `/srv/ig-sender/ig.env`, using shell `export IG_USERNAME=...` and `export IG_PASSWORD=...`. Do not put the secrets directly in a crontab or a chat. Example cron (once daily, off-peak local server time):

```cron
15 10 * * * cd /srv/ig-sender && . ./ig.env && /srv/ig-sender/.venv/bin/python sender.py queue.json --active-hours 10:00-18:00 --timezone Asia/Kolkata >> sender.log 2>&1
```

Cron's scheduled time uses the host's timezone; `--timezone` only controls active hours and the daily cap. `flock` inside the script prevents simultaneous runs. A regular unattended cron run must not be used until first login and verification are complete and the exact queue has been approved. Hermes can invoke the same shell command and check its exit code: `0` means that run completed or deferred due to cap/hours, `2` means human review is needed. It should inspect results and `.STOPPED.json`, and **must not** repeatedly rerun on exit 2. It should never fill in or change approved text automatically.

## Library-specific notes

Pinned `instagrapi==3.0.12`. Its `Client.direct_send_video(path, thread_ids=[...])` sends an H.264 MP4 via the newer `raven_attachment` flow; `user_ids` on a new conversation raises `DirectThreadNotFound` because video requires an existing thread. The script first calls `direct_send(opener_text, user_ids=[...])`, gets its returned `thread_id`, then sends video and text with `thread_ids`. This may not work on every account or Instagram server revision. Login uses the newer CAA flow; some 2FA flows accept a `verification_code` argument, but unsupported Bloks/auth-platform challenges require manual verification. We intentionally exit for all challenge and 2FA types, with no automated workaround. The saved settings are read on subsequent runs and existing login is validated by the library. See upstream source: https://github.com/subzeroid/instagrapi/blob/3.0.12/instagrapi/mixins/direct.py and https://github.com/subzeroid/instagrapi/blob/3.0.12/instagrapi/mixins/auth.py .
