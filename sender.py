#!/usr/bin/env python3
"""Fail-closed, resumable Instagram outreach queue. Review README before live use."""
import argparse
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from instagrapi import Client
from instagrapi.exceptions import (
    UserNotFound, ChallengeError, ClientError, ClientForbiddenError, ClientThrottledError,
    FeedbackRequired, LoginRequired, PleaseWaitFewMinutes, RateLimitError,
    SentryBlock, TwoFactorRequired,
)

LOG = logging.getLogger("sender")


class SenderClient(Client):
    def private_request(self, endpoint, *args, **kwargs):
        # No library-level replay of a DM POST after an uncertain timeout.
        if endpoint.startswith("direct_v2/threads/broadcast/"):
            self._send_private_request(endpoint, *args, **kwargs)
            return self.last_json
        return super().private_request(endpoint, *args, **kwargs)

USERNAME = re.compile(r"[A-Za-z0-9_.]{1,30}\Z")
BLOCK_TYPES = (ChallengeError, TwoFactorRequired, FeedbackRequired, SentryBlock,
               RateLimitError, PleaseWaitFewMinutes, ClientThrottledError,
               ClientForbiddenError, LoginRequired)
BLOCK_WORDS = ("action blocked", "action_blocked", "feedback_required",
               "challenge_required", "please wait", "rate limit", "spam",
               "try again later", "temporarily blocked", "checkpoint")


def now(tz):
    return datetime.now(tz).isoformat(timespec="seconds")


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    d = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(d)
    finally:
        os.close(d)


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate_queue(path):
    raw = load_json(path)
    if not isinstance(raw, list):
        raise ValueError("Queue must be a JSON array")
    seen = set()
    out = []
    for i, row in enumerate(raw, 1):
        if not isinstance(row, dict) or set(row) != {"ig_username", "video_path", "opener_text", "message_text"}:
            raise ValueError(f"Row {i}: expected exactly ig_username, video_path, opener_text, message_text")
        user = row["ig_username"]
        if not isinstance(user, str) or not USERNAME.fullmatch(user.strip().lstrip("@")):
            raise ValueError(f"Row {i}: invalid username")
        user = user.strip().lstrip("@").lower()
        if user in seen:
            raise ValueError(f"Duplicate username: {user}")
        seen.add(user)
        for field in ("opener_text", "message_text"):
            if not isinstance(row[field], str) or not row[field].strip():
                raise ValueError(f"Row {i}: {field} must be nonempty text")
        value = row["video_path"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Row {i}: invalid video_path")
        video = Path(value).expanduser()
        if not video.is_absolute():
            video = path.parent / video
        video = video.resolve()
        if not video.is_file() or video.suffix.lower() != ".mp4" or video.stat().st_size == 0:
            raise ValueError(f"Row {i}: video must be a readable, nonempty .mp4: {video}")
        h = hashlib.sha256()
        with video.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        digest = h.hexdigest()
        clean = dict(row, ig_username=user, video_path=str(video))
        fingerprint = hashlib.sha256(json.dumps([user, digest, row["opener_text"],
                            row["message_text"]], ensure_ascii=False).encode()).hexdigest()
        out.append((clean, fingerprint))
    return out


def blocked(exc):
    return isinstance(exc, BLOCK_TYPES) or (
        isinstance(exc, ClientError) and any(w in str(exc).lower() for w in BLOCK_WORDS))


def mark(state, path, user, tz, **fields):
    entry = state["targets"][user]
    entry.update(fields, updated_at=now(tz))
    save_json(path, state)
    LOG.info("@%s status=%s", user, entry["status"])


def stop(status_path, state, state_path, user, tz, reason, kind):
    entry = state["targets"][user]
    mark(state, state_path, user, tz, status=kind, error=reason)
    save_json(status_path, {"status": "STOPPED", "at": now(tz), "target": user,
                            "stage": entry.get("stage"), "reason": reason,
                            "instruction": "Inspect Instagram and reconcile the thread manually; do not auto-retry."})
    LOG.error("STOPPED @%s stage=%s: %s", user, entry.get("stage"), reason)


def send_step(client, state, state_path, user, tz, stage, action):
    # A crash or timeout may happen after Instagram accepted the message. An in-flight
    # marker requires human reconciliation; never automatically resend that step.
    mark(state, state_path, user, tz, status="in_flight", stage=stage)
    LOG.info("@%s attempting %s", user, stage)
    result = action()
    if not result or not getattr(result, "id", None):
        raise RuntimeError(f"{stage}: no message ID returned; delivery unknown")
    mark(state, state_path, user, tz, status="partial", stage=stage + "_sent",
         **{stage + "_id": str(result.id)})
    return result


def int_arg(name, default):
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def window(value):
    if not value:
        return None
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)", value)
    if not m:
        raise ValueError("--active-hours format: HH:MM-HH:MM (24-hour, in --timezone)")
    a, b = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
    if a == b:
        raise ValueError("Active-hours start and end cannot match")
    return a, b


def inside(hours, tz):
    if hours is None:
        return True
    d = datetime.now(tz)
    t = d.hour * 60 + d.minute
    a, b = hours
    return a <= t < b if a < b else t >= a or t < b


def pause(min_s, max_s, hours, tz):
    seconds = random.uniform(min_s, max_s)
    LOG.info("Waiting %.0f seconds before next send", seconds)
    time.sleep(seconds)
    if not inside(hours, tz):
        raise OutsideHours("Active-hours window ended; run again in the next window")


class OutsideHours(Exception):
    pass


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("queue", type=Path)
    p.add_argument("--dry-run", action="store_true", help="Validate queue and login; print sends, no DMs")
    p.add_argument("--results", type=Path, help="Default: QUEUE.results.json")
    p.add_argument("--session", type=Path, default=Path("ig-session.json"))
    p.add_argument("--daily-cap", type=int, default=int_arg("IG_DAILY_CAP", 12), help="Targets started per local calendar day")
    p.add_argument("--delay-min", type=float, default=float(os.getenv("IG_DELAY_MIN", "240")), help="Minimum seconds between messages")
    p.add_argument("--delay-max", type=float, default=float(os.getenv("IG_DELAY_MAX", "540")), help="Maximum seconds between messages")
    p.add_argument("--active-hours", default=os.getenv("IG_ACTIVE_HOURS", ""), help="HH:MM-HH:MM, optional")
    p.add_argument("--timezone", default=os.getenv("IG_TIMEZONE", "Asia/Kolkata"))
    args = p.parse_args()
    if args.daily_cap < 1 or args.delay_min < 1 or args.delay_max < args.delay_min:
        p.error("daily cap must be positive, delay min >= 1, max >= min")
    tz = ZoneInfo(args.timezone)
    hours = window(args.active_hours)
    queue_path = args.queue.expanduser().resolve()
    rows = validate_queue(queue_path)
    results = (args.results or Path(str(queue_path) + ".results.json")).expanduser().resolve()
    session = args.session.expanduser().resolve()
    status_path = Path(str(results) + ".STOPPED.json")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    username, password = os.getenv("IG_USERNAME", "").strip().lstrip("@"), os.getenv("IG_PASSWORD", "")
    if not username or not password:
        raise ValueError("Set IG_USERNAME and IG_PASSWORD in environment (never in queue or cron command)")
    lock_path = Path(str(results) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        os.fchmod(lock.fileno(), 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.error("Another run holds the results lock; exiting")
            return 2
        if not args.dry_run and status_path.exists():
            LOG.error("STOPPED file exists at %s; manually reconcile before resuming", status_path)
            return 2
        state = load_json(results) if results.exists() else {"version": 1, "account": username.lower(), "targets": {}}
        if state.get("version") != 1 or state.get("account") != username.lower() or not isinstance(state.get("targets"), dict):
            raise ValueError("Results file version/account mismatch; do not mix accounts")
        for row, fingerprint in rows:
            existing = state["targets"].get(row["ig_username"])
            if existing and existing.get("fingerprint") != fingerprint:
                raise ValueError(f"@{row['ig_username']}: queue/video changed since recorded; reconcile manually")
        if not args.dry_run:
            for user, entry in state["targets"].items():
                if entry.get("status") in ("in_flight", "partial", "blocked", "needs_review"):
                    LOG.error("@%s has unresolved %s at %s. Inspect thread and results manually; no auto-retry.",
                              user, entry["status"], entry.get("stage"))
                    return 2
        client = SenderClient(private_transport="requests", session_retry_total=0)
        # Never let the library auto-solve/replay a checkpoint or read a code/password
        # from an unattended terminal. The handler propagates the original exception.
        client.handle_exception = lambda _client, exc: (_ for _ in ()).throw(exc)
        client.challenge_code_handler = lambda *_: None
        client.change_password_handler = lambda *_: None
        if session.exists():
            client.load_settings(session)
            LOG.info("Loaded saved device/session settings from %s", session)
        try:
            LOG.info("Validating login for @%s", username)
            client.login(username, password)
            # Atomic private write instead of dump_settings()'s non-atomic, mode-dependent write.
            save_json(session, client.get_settings())
            LOG.info("Login validated; settings saved")
        except (ChallengeError, TwoFactorRequired) as exc:
            reason = "Instagram needs manual verification/2FA. Complete in official Instagram app/web on a trusted device, then rerun with the same session/IP; do not automate or retry rapidly. " + str(exc)
            if not args.dry_run:
                save_json(status_path, {"status": "STOPPED", "at": now(tz), "stage": "login", "reason": reason})
            LOG.error(reason)
            return 2
        except Exception as exc:
            reason = "Login failed: " + type(exc).__name__ + ": " + str(exc)
            if not args.dry_run:
                save_json(status_path, {"status": "STOPPED", "at": now(tz), "stage": "login", "reason": reason})
            LOG.error(reason)
            return 2
        if args.dry_run:
            for row, _ in rows:
                LOG.info("DRY RUN @%s: opener=%r; video=%s; pitch=%r; prior=%s",
                         row["ig_username"], row["opener_text"], row["video_path"],
                         row["message_text"], state["targets"].get(row["ig_username"], {}).get("status", "new"))
            return 0
        if not inside(hours, tz):
            LOG.info("Outside active hours; no sends")
            return 0
        sent_this_run = False
        for row, fingerprint in rows:
            user = row["ig_username"]
            if user in state["targets"]:
                LOG.info("Skipping @%s (%s)", user, state["targets"][user]["status"])
                continue
            today = datetime.now(tz).date().isoformat()
            starts = sum(1 for v in state["targets"].values() if v.get("started_date") == today)
            if starts >= args.daily_cap:
                LOG.info("Daily cap of %s target starts reached (%s)", args.daily_cap, today)
                break
            if not inside(hours, tz):
                LOG.info("Outside active hours; remaining queue pending")
                break
            if sent_this_run:
                try:
                    pause(args.delay_min, args.delay_max, hours, tz)
                except OutsideHours as exc:
                    LOG.info(str(exc))
                    break
            state["targets"][user] = {"fingerprint": fingerprint, "status": "pending",
                                      "started_date": today, "stage": "lookup", "updated_at": now(tz)}
            save_json(results, state)
            try:
                profile = client.user_info_by_username(user)
                if profile.username.lower() != user:
                    raise RuntimeError("Username lookup returned a different account; review manually")
                uid = int(profile.pk)
                mark(state, results, user, tz, user_id=str(uid))
                opener = send_step(client, state, results, user, tz, "opener",
                                   lambda: client.direct_send(row["opener_text"], user_ids=[uid]))
                sent_this_run = True
                thread_id = getattr(opener, "thread_id", None)
                if not thread_id:
                    raise RuntimeError("Opener returned no thread ID; delivery may have occurred. Inspect manually")
                mark(state, results, user, tz, thread_id=str(thread_id))
                pause(args.delay_min, args.delay_max, hours, tz)
                send_step(client, state, results, user, tz, "video",
                          lambda: client.direct_send_video(Path(row["video_path"]), thread_ids=[int(thread_id)]))
                pause(args.delay_min, args.delay_max, hours, tz)
                send_step(client, state, results, user, tz, "pitch",
                          lambda: client.direct_send(row["message_text"], thread_ids=[int(thread_id)]))
                mark(state, results, user, tz, status="done", stage="complete")
            except UserNotFound as exc:
                mark(state, results, user, tz, status="failed", error=str(exc))
                LOG.warning("@%s not found; marked failed, continuing", user)
            except OutsideHours as exc:
                mark(state, results, user, tz, status="partial", error=str(exc))
                LOG.warning("@%s paused mid-target at %s; manually inspect before continuing", user,
                            state["targets"][user]["stage"])
                return 2
            except Exception as exc:
                reason = type(exc).__name__ + ": " + str(exc)
                is_block = blocked(exc)
                stage = state["targets"][user].get("stage", "")
                kind = "blocked" if is_block else ("partial" if stage in
                       ("opener_sent", "video", "video_sent", "pitch", "pitch_sent") else "needs_review")
                stop(status_path, state, results, user, tz, reason, kind)
                if is_block:
                    LOG.error("Instagram restriction/challenge: stop all activity, resolve in official app. Never retry through a block.")
                else:
                    LOG.error("Unknown send/lookup outcome: inspect thread manually before any restart.")
                return 2
        LOG.info("Queue run finished")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)
