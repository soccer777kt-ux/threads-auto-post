#!/usr/bin/env python3
"""Post the next queued text to Threads and advance the local queue state."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
POSTS_PATH = ROOT / "posts.json"
STATE_PATH = ROOT / "state.json"
API_BASE = "https://graph.threads.net"
JST = ZoneInfo("Asia/Tokyo")

# Monday=0 ... Sunday=6.  Each day deliberately uses slightly different times.
DAILY_TARGETS = {
    0: {"morning": (7, 23), "lunch": (12, 37), "evening": (20, 19)},
    1: {"morning": (8, 11), "lunch": (11, 53), "evening": (21, 7)},
    2: {"morning": (7, 41), "lunch": (12, 16), "evening": (19, 43)},
    3: {"morning": (8, 27), "lunch": (13, 4), "evening": (20, 51)},
    4: {"morning": (7, 8), "lunch": (12, 49), "evening": (21, 23)},
    5: {"morning": (8, 44), "lunch": (11, 38), "evening": (19, 18)},
    6: {"morning": (7, 56), "lunch": (13, 17), "evening": (20, 36)},
}
MIN_POST_INTERVAL = timedelta(minutes=90)


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def api_post(path: str, payload: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Threads API returned HTTP {error.code}: {detail}") from error


def scheduled_slot(state: dict, now: datetime) -> str | None:
    """Return the oldest due, not-yet-posted slot while avoiding rapid posts."""
    posted_slots = set(state.get("posted_slots", []))
    last_posted_at = state.get("last_posted_at")
    if last_posted_at:
        last_time = datetime.fromisoformat(last_posted_at)
        if now - last_time < MIN_POST_INTERVAL:
            return None

    targets = DAILY_TARGETS[now.weekday()]
    for slot_name in ("morning", "lunch", "evening"):
        hour, minute = targets[slot_name]
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        slot_key = f"{now.date().isoformat()}-{slot_name}"
        if now >= target and slot_key not in posted_slots:
            return slot_name
    return None


def next_unposted_target(state: dict, now: datetime) -> datetime | None:
    """Return the next unposted target today, if one still remains."""
    posted_slots = set(state.get("posted_slots", []))
    targets = DAILY_TARGETS[now.weekday()]
    for slot_name in ("morning", "lunch", "evening"):
        slot_key = f"{now.date().isoformat()}-{slot_name}"
        if slot_key in posted_slots:
            continue
        hour, minute = targets[slot_name]
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target > now:
            return target
    return None


def publish_with_retry(creation_id: str, token: str) -> dict:
    """Wait for Threads to finish preparing a container, then publish it."""
    last_error: Exception | None = None
    for attempt in range(6):
        if attempt:
            time.sleep(5)
        try:
            return api_post(
                "/me/threads_publish",
                {"creation_id": creation_id, "access_token": token},
            )
        except RuntimeError as error:
            last_error = error
            message = str(error)
            if "Media Not Found" not in message and "error_subcode\":4279009" not in message:
                raise
    raise RuntimeError(f"Threadsの投稿準備が時間内に完了しませんでした: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--index", type=int, help="Preview a specific post without advancing state")
    parser.add_argument("--scheduled", action="store_true", help="Post only when a daily slot is due")
    parser.add_argument(
        "--wait-minutes",
        type=int,
        default=0,
        help="When scheduled, wait for the next target if it is this many minutes away",
    )
    args = parser.parse_args()

    posts = load_json(POSTS_PATH)
    state = load_json(STATE_PATH)
    now = datetime.now(JST)

    slot_name = None
    if args.scheduled:
        slot_name = scheduled_slot(state, now)
        if slot_name is None and args.wait_minutes > 0:
            next_target = next_unposted_target(state, now)
            if next_target is not None:
                wait_seconds = (next_target - now).total_seconds()
                if 0 < wait_seconds <= args.wait_minutes * 60:
                    print(
                        f"次の投稿時刻 {next_target.strftime('%H:%M')} まで"
                        f" {int(wait_seconds)} 秒待機します。"
                    )
                    time.sleep(wait_seconds + 5)
                    now = datetime.now(JST)
                    slot_name = scheduled_slot(state, now)
        if slot_name is None:
            print("現在は投稿時刻ではないか、この時間帯は投稿済みです。")
            return 0

    next_index = state.get("next_index", 0) if args.index is None else args.index

    if not 0 <= next_index < len(posts):
        raise RuntimeError("投稿原稿を使い切りました。posts.json に新しい原稿を追加してください。")

    text = posts[next_index]["text"].strip()
    if not text or len(text) > 500:
        raise RuntimeError(f"投稿 {next_index + 1} の文字数が不正です: {len(text)}")

    print(f"投稿番号: {next_index + 1}/{len(posts)}")
    print(f"文字数: {len(text)}")
    if args.dry_run:
        print(text)
        return 0

    token = os.environ.get("THREADS_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("GitHub Secret THREADS_ACCESS_TOKEN が設定されていません。")

    container = api_post(
        "/me/threads",
        {"media_type": "TEXT", "text": text, "access_token": token},
    )
    creation_id = container.get("id")
    if not creation_id:
        raise RuntimeError("投稿コンテナIDを取得できませんでした。")

    published = publish_with_retry(creation_id, token)
    post_id = published.get("id")
    if not post_id:
        raise RuntimeError("公開済み投稿IDを取得できませんでした。")

    state["next_index"] = next_index + 1
    state["last_post_id"] = post_id
    state["last_posted_at"] = now.isoformat()
    if slot_name:
        slot_key = f"{now.date().isoformat()}-{slot_name}"
        posted_slots = [
            item for item in state.get("posted_slots", [])
            if item >= (now.date() - timedelta(days=14)).isoformat()
        ]
        posted_slots.append(slot_key)
        state["posted_slots"] = posted_slots
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"投稿成功: {post_id}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
