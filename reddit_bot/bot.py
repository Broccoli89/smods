import base64
import io
import json
import queue
import threading
import time
from pathlib import Path

import requests
from PIL import Image
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov", ".webm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
POLL_INTERVAL = 5  # seconds


def load_config():
    config_path = Path(__file__).parent / "config.json"
    with open(config_path) as f:
        return json.load(f)


def generate_title(image_path: Path, subreddit: dict, model: str) -> str:
    prompt = (
        f"You are writing a Reddit post title for r/{subreddit['name']}, a {subreddit['theme']} subreddit. "
        "Look at this image and write a title that fits Reddit's casual style for this community. "
        "Rules: no emojis, no hashtags, no Instagram-style captions, no ALL CAPS. "
        "Keep it under 15 words. Make it feel like something a real Reddit user would post. "
        "Return only the title, nothing else."
    )
    payload = {"model": model, "prompt": prompt, "stream": False}
    if image_path.suffix.lower() in IMAGE_EXTENSIONS:
        img = Image.open(image_path).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        payload["images"] = [base64.b64encode(buf.getvalue()).decode()]
    response = requests.post(
        "http://localhost:11434/api/generate",
        json=payload,
        timeout=300,
    )
    response.raise_for_status()
    return response.json()["response"].strip().strip('"')


def post_to_reddit(account: dict, media_path: Path, title: str, subreddit_name: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        Stealth().use_sync(page)

        page.goto("https://www.reddit.com/login")
        page.wait_for_load_state("domcontentloaded")
        time.sleep(3)
        page.locator('input[name="username"], input[id="login-username"], input[autocomplete="username"]').first.fill(account["username"])
        page.locator('input[name="password"], input[id="login-password"], input[type="password"]').first.fill(account["password"])
        page.locator('button[type="submit"], button:has-text("Log In"), button:has-text("Login")').first.click()
        time.sleep(5)

        page.goto(f"https://www.reddit.com/r/{subreddit_name}/submit?type=IMAGE" if subreddit_name else f"https://www.reddit.com/user/{account['username']}/submit?type=IMAGE")
        page.wait_for_load_state("domcontentloaded")
        time.sleep(4)

        try:
            with page.expect_file_chooser(timeout=8000) as fc_info:
                page.click('button:has-text("Upload")', timeout=8000)
            fc_info.value.set_files(str(media_path))
        except Exception:
            page.locator('input[type="file"]').first.set_input_files(str(media_path))
        time.sleep(5)

        page.mouse.click(608, 271)
        time.sleep(1)
        page.keyboard.type(title)
        time.sleep(2)

        for selector in ['button:has-text("Post")', 'button:has-text("Submit")', '[data-testid="post-submit-button"]']:
            try:
                page.click(selector, timeout=5000)
                break
            except Exception:
                continue
        time.sleep(5)

        url = page.url
        browser.close()
        return url


def process_upload(config: dict, media_path: Path, person: str, category: str):
    people = config.get("people", {})
    if person not in people:
        print(f"No account configured for '{person}' — skipping. Add them to config.json.")
        return

    account = people[person]
    categories = config["categories"]
    if category not in categories:
        print(f"Unknown category '{category}' — skipping. Add it to config.json to enable.")
        return

    subreddits = categories[category]["subreddits"]

    print(f"\n{'='*50}")
    print(f"Processing: {person}/{category}/{media_path.name}")
    print(f"Account: u/{account['username']}")
    print(f"{'='*50}")

    title = media_path.stem
    print(f"Title: {title}")

    if account.get("post_to_profile"):
        print(f"[TEST MODE] Posting to u/{account['username']} profile")
        try:
            url = post_to_reddit(account, media_path, title, None)
            print(f"Posted: {url}")
        except Exception as e:
            print(f"Failed to post to profile: {e}")
    else:
        if not subreddits:
            print(f"No subreddits configured for '{category}' — skipping.")
            return
        for i, subreddit in enumerate(subreddits):
            print(f"\n[{i+1}/{len(subreddits)}] Posting to r/{subreddit['name']}...")
            try:
                url = post_to_reddit(account, media_path, title, subreddit["name"])
                print(f"Posted: {url}")
            except Exception as e:
                print(f"Failed to post to r/{subreddit['name']}: {e}")

            if i < len(subreddits) - 1:
                mins = config.get("post_delay_minutes", 10)
                print(f"Waiting {mins} minutes before next post...")
                time.sleep(mins * 60)

    media_path.unlink()
    print(f"\nDeleted {media_path.name}")


def worker(config: dict, post_queue: queue.Queue):
    while True:
        media_path, person, category = post_queue.get()
        try:
            process_upload(config, media_path, person, category)
        except Exception as e:
            print(f"Error processing {media_path.name}: {e}")
        post_queue.task_done()


def scan_folder(drive_folder: Path, seen: set) -> list:
    new_files = []
    for path in drive_folder.rglob("*"):
        if path.is_dir():
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        if "archive" in path.parts:
            continue
        if str(path) in seen:
            continue
        try:
            parts = path.relative_to(drive_folder).parts
            if len(parts) < 3:
                continue
            new_files.append(path)
        except ValueError:
            continue
    return new_files


def main():
    config = load_config()
    drive_folder = Path(config["drive_folder"])

    post_queue = queue.Queue()
    threading.Thread(target=worker, args=(config, post_queue), daemon=True).start()

    # Build initial snapshot so existing files are not reprocessed
    seen = {str(p) for p in drive_folder.rglob("*") if p.is_file()}

    print(f"Bot is running. Watching: {drive_folder}")
    print(f"Polling every {POLL_INTERVAL} seconds.")
    print("Drop images into person/category folders to trigger posting.")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(POLL_INTERVAL)
            new_files = scan_folder(drive_folder, seen)
            for path in new_files:
                seen.add(str(path))
                parts = path.relative_to(drive_folder).parts
                person, category = parts[0], parts[1]
                print(f"\nNew file detected: {path.name} ({person}/{category})")
                time.sleep(2)  # let file finish syncing
                post_queue.put((path, person, category))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
