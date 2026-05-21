#!/usr/bin/env python3
"""
Reddit auto-posting bot.

Usage:
    python bot.py --account accounts/account1.env

Designed to be run via cron once daily, one invocation per account.
Each run processes all subreddit subfolders in the queue, posting the
oldest image from each folder that has pending files.
"""

import argparse
import base64
import json
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import praw
import requests
from dotenv import load_dotenv

SUBREDDITS_CONFIG = Path(__file__).parent / "subreddits.json"
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "moondream"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def configure_logging(account_name: str) -> logging.Logger:
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"{account_name}.log"

    logger = logging.getLogger(account_name)
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_file)
    fh.setFormatter(fmt)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def load_subreddits() -> dict:
    with open(SUBREDDITS_CONFIG) as f:
        return json.load(f)


def oldest_image(folder: Path) -> Path | None:
    """Return the oldest image file in folder, or None if empty."""
    files = [
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
    ]
    return min(files, key=lambda f: f.stat().st_mtime) if files else None


def folder_name_to_subreddit(name: str) -> str:
    """Convert queue folder name to subreddit name.

    r-dresses  -> r/dresses
    dresses    -> dresses
    """
    if name.startswith("r-"):
        return "r/" + name[2:]
    return name


def generate_title(image_path: Path, tone: str, example_title: str, log: logging.Logger) -> str:
    """Ask Ollama Moondream to generate a Reddit post title for the image."""
    with open(image_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode()

    prompt = (
        "Look at this image and write a single Reddit post title for it. "
        f"Tone: {tone}. "
        f'Example of the style to match: "{example_title}". '
        "Write only the title text with no quotes, no hashtags, and no explanation. "
        "Sound like a real person sharing something they like, not an advertisement."
    )

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "images": [encoded],
        "stream": False,
    }

    log.info(f"Calling Ollama ({OLLAMA_MODEL}) for {image_path.name}")
    resp = requests.post(OLLAMA_URL, json=payload, timeout=90)
    resp.raise_for_status()

    title = resp.json()["response"].strip().strip('"').strip("'")
    return title


def post_image(reddit: praw.Reddit, subreddit: str, title: str, image_path: Path) -> str:
    """Submit image post to Reddit. Returns the post permalink URL."""
    # Accept both "r/foo" and "foo" formats
    sub_name = subreddit.removeprefix("r/")
    submission = reddit.subreddit(sub_name).submit_image(
        title=title,
        image_path=str(image_path),
    )
    return f"https://reddit.com{submission.permalink}"


def archive_file(image_path: Path, queue_root: Path) -> Path:
    """Move posted image into posted/<subreddit_folder>/ with a timestamp prefix."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_dir = queue_root / "posted" / image_path.parent.name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{timestamp}_{image_path.name}"
    shutil.move(str(image_path), dest)
    return dest


def run(account_env: str) -> None:
    load_dotenv(account_env, override=True)

    account_name = os.environ.get("ACCOUNT_NAME", Path(account_env).stem)
    log = configure_logging(account_name)
    log.info(f"=== Bot run started for account: {account_name} ===")

    queue_root = Path(os.environ["QUEUE_FOLDER"])
    if not queue_root.exists():
        log.error(f"Queue folder not found: {queue_root}")
        sys.exit(1)

    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        username=os.environ["REDDIT_USERNAME"],
        password=os.environ["REDDIT_PASSWORD"],
        user_agent=os.environ.get(
            "REDDIT_USER_AGENT",
            f"script:reddit-autoposter:v1.0 (by /u/{os.environ['REDDIT_USERNAME']})",
        ),
    )

    subreddits_cfg = load_subreddits()

    # Walk subreddit subfolders; skip the /posted archive dir
    sub_folders = sorted(
        d for d in queue_root.iterdir()
        if d.is_dir() and d.name != "posted"
    )

    if not sub_folders:
        log.info("Queue is empty — no subreddit folders found.")
        return

    # Titles already generated this run, keyed by cluster name
    cluster_cache: dict[str, str] = {}
    posted = failed = skipped = 0

    for folder in sub_folders:
        subreddit_key = folder_name_to_subreddit(folder.name)

        cfg = subreddits_cfg.get(subreddit_key) or subreddits_cfg.get(folder.name)
        if cfg is None:
            log.warning(f"No subreddits.json entry for '{subreddit_key}' — skipping folder")
            skipped += 1
            continue

        image = oldest_image(folder)
        if image is None:
            log.info(f"No images pending in {folder.name}")
            continue

        log.info(f"Selected: {image} → {subreddit_key}")

        # Title: reuse from cluster cache or generate fresh
        cluster = cfg.get("cluster")
        if cluster and cluster in cluster_cache:
            title = cluster_cache[cluster]
            log.info(f"Reusing cluster '{cluster}' title: {title!r}")
        else:
            try:
                title = generate_title(image, cfg["tone"], cfg["example_title"], log)
                log.info(f"Generated title: {title!r}")
                if cluster:
                    cluster_cache[cluster] = title
            except Exception as exc:
                log.error(f"Title generation failed for {image.name}: {exc}")
                failed += 1
                continue

        # Post
        try:
            url = post_image(reddit, subreddit_key, title, image)
            log.info(f"Posted to {subreddit_key}: {url}")
        except Exception as exc:
            log.error(f"Reddit post failed for {image.name} → {subreddit_key}: {exc}")
            failed += 1
            continue

        # Archive
        try:
            dest = archive_file(image, queue_root)
            log.info(f"Archived to {dest}")
        except Exception as exc:
            log.warning(f"Archive failed for {image.name}: {exc}")

        posted += 1

    log.info(
        f"=== Run complete — posted: {posted}, failed: {failed}, skipped: {skipped} ==="
    )


def main():
    parser = argparse.ArgumentParser(description="Reddit image auto-poster")
    parser.add_argument(
        "--account",
        required=True,
        metavar="PATH",
        help="Path to account .env file (e.g. accounts/account1.env)",
    )
    args = parser.parse_args()

    if not Path(args.account).exists():
        print(f"Error: account file not found: {args.account}", file=sys.stderr)
        sys.exit(1)

    run(args.account)


if __name__ == "__main__":
    main()
