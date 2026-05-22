import json
import os
import random
import time
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mov", ".webm"}


def load_config():
    config_path = Path(__file__).parent / "config.json"
    with open(config_path) as f:
        return json.load(f)


def get_media_file(folder: str) -> Path:
    folder_path = Path(folder)
    files = [
        f for f in folder_path.rglob("*")
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    if not files:
        raise FileNotFoundError(f"No supported media files found in {folder}")
    return random.choice(files)


def generate_title(category: str, model: str) -> str:
    prompt = (
        f"Generate a short, catchy Reddit post title for a {category} subreddit. "
        "Make it natural and engaging. Return only the title, no quotes or explanation."
    )
    response = requests.post(
        "http://localhost:11434/api/generate",
        json={"model": model, "prompt": prompt, "stream": False},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["response"].strip().strip('"')


def post_to_reddit(config: dict, subreddit: dict, media_path: Path, title: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        # Login
        page.goto("https://www.reddit.com/login")
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)
        page.fill('input[id="login-username"]', config["reddit"]["username"])
        page.fill('input[id="login-password"]', config["reddit"]["password"])
        page.get_by_role("button", name="Log In").click()
        page.wait_for_load_state("networkidle")
        time.sleep(3)

        # Go to submit page
        page.goto(f"https://www.reddit.com/r/{subreddit['name']}/submit")
        page.wait_for_load_state("networkidle")
        time.sleep(3)

        # Click image/video tab
        for selector in [
            'a[href*="submit?type=IMAGE"]',
            'button:has-text("Images & Video")',
            '[role="tab"]:has-text("Image")',
            '[data-testid="image-and-video-tab"]',
        ]:
            try:
                page.click(selector, timeout=3000)
                break
            except Exception:
                continue
        time.sleep(2)

        # Upload file via file chooser
        try:
            with page.expect_file_chooser(timeout=5000) as fc_info:
                page.click('button:has-text("Upload")', timeout=5000)
            fc_info.value.set_files(str(media_path))
        except Exception:
            file_input = page.locator('input[type="file"]').first
            file_input.set_input_files(str(media_path))
        time.sleep(5)

        # Fill title
        for selector in ['textarea[placeholder*="Title"]', 'input[placeholder*="Title"]', '[data-testid="post-title-input"]']:
            try:
                page.fill(selector, title, timeout=3000)
                break
            except Exception:
                continue
        time.sleep(1)

        # Submit
        for selector in ['button:has-text("Post")', 'button:has-text("Submit")', '[data-testid="post-submit-button"]']:
            try:
                page.click(selector, timeout=5000)
                break
            except Exception:
                continue
        page.wait_for_load_state("networkidle")
        time.sleep(config.get("post_delay_seconds", 5))

        current_url = page.url
        browser.close()
        return current_url


def main():
    config = load_config()

    subreddit = random.choice(config["subreddits"])
    print(f"Target subreddit: r/{subreddit['name']} ({subreddit['category']})")

    media_path = get_media_file(config["drive_folder"])
    print(f"Media file: {media_path.name}")

    print("Generating title with Ollama...")
    title = generate_title(subreddit["category"], config["ollama_model"])
    print(f"Title: {title}")

    print("Posting to Reddit...")
    url = post_to_reddit(config, subreddit, media_path, title)
    print(f"Done! Post URL: {url}")


if __name__ == "__main__":
    main()
