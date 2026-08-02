#!/usr/bin/env python3
"""Post the next queued text to Threads and advance the local queue state."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
POSTS_PATH = ROOT / "posts.json"
STATE_PATH = ROOT / "state.json"
API_BASE = "https://graph.threads.net"


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--index", type=int, help="Preview a specific post without advancing state")
    args = parser.parse_args()

    posts = load_json(POSTS_PATH)
    state = load_json(STATE_PATH)
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

    published = api_post(
        "/me/threads_publish",
        {"creation_id": creation_id, "access_token": token},
    )
    post_id = published.get("id")
    if not post_id:
        raise RuntimeError("公開済み投稿IDを取得できませんでした。")

    state["next_index"] = next_index + 1
    state["last_post_id"] = post_id
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
