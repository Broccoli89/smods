import base64
import json
import random
import time
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

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


def generate_title(category: str, model: str, image_path: Path) -> str:
    prompt = (
        f"You are writing a Reddit post title for the {category} subreddit. "
        "Look at this image and write a title that fits Reddit's casual, witty style. "
        "Rules: no emojis, no hashtags, no Instagram-style captions, no ALL CAPS. "
        "Keep it under 15 words. Make it feel like something a real Reddit user would post. "
        "Return only the title, nothing else."
    )
    payload = {"model": model, "prompt": prompt, "stream": False}
    if image_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif"}:
        with open(image_path, "rb") as f:
            payload["images"] = [base64.b64encode(f.read()).decode()]
    response = requests.post(
        "http://localhost:11434/api/generate",
        json=payload,
        timeout=300,
    )
    response.raise_for_status()
    return response.json()["response"].strip().strip('"')


def post_to_reddit(config: dict, subreddit: dict, media_path: Path, title: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        Stealth().use_sync(page)

        # Login
        page.goto("https://www.reddit.com/login")
        page.wait_for_load_state("domcontentloaded")
        time.sleep(3)
        username_input = page.locator('input[name="username"], input[id="login-username"], input[placeholder*="Username"], input[autocomplete="username"]').first
        username_input.fill(config["reddit"]["username"])
        password_input = page.locator('input[name="password"], input[id="login-password"], input[type="password"]').first
        password_input.fill(config["reddit"]["password"])
        page.locator('button[type="submit"], button:has-text("Log In"), button:has-text("Login")').first.click()
        time.sleep(5)

        # Go to submit page
        page.goto(f"https://www.reddit.com/r/{subreddit['name']}/submit?type=IMAGE")
        page.wait_for_load_state("domcontentloaded")
        time.sleep(4)
        page.screenshot(path="/tmp/step1_submit_page.png")
        print("Screenshot saved: step1_submit_page.png")

        # Upload file via file chooser
        print("Attempting file upload...")
        try:
            with page.expect_file_chooser(timeout=8000) as fc_info:
                page.click('button:has-text("Upload")', timeout=8000)
            fc_info.value.set_files(str(media_path))
            print("File uploaded via chooser")
        except Exception as e:
            print(f"File chooser failed ({e}), trying direct input...")
            file_input = page.locator('input[type="file"]').first
            file_input.set_input_files(str(media_path))
            print("File set via direct input")
        time.sleep(5)
        page.screenshot(path="/tmp/step2_after_upload.png")
        print("Screenshot saved: step2_after_upload.png")

        # Dump all inputs for debugging
        inputs = page.evaluate("""() => {
            const els = document.querySelectorAll('input, textarea, [contenteditable]');
            return Array.from(els).map(el => ({
                tag: el.tagName,
                placeholder: el.placeholder || '',
                name: el.name || '',
                id: el.id || '',
                ariaLabel: el.getAttribute('aria-label') || '',
                contenteditable: el.getAttribute('contenteditable') || ''
            }));
        }""")
        print("Form elements found:", inputs)

        # Fill title - click at visual coordinates of title field then type
        print("Filling title...")
        page.mouse.click(608, 271)
        time.sleep(1)
        page.keyboard.type(title)
        time.sleep(1)
        print("Title typed")
        time.sleep(2)
        page.screenshot(path="/tmp/step3_after_title.png")
        print("Screenshot saved: step3_after_title.png")

        # Submit
        print("Clicking Post button...")
        clicked = False
        for selector in ['button:has-text("Post")', 'button:has-text("Submit")', '[data-testid="post-submit-button"]']:
            try:
                page.click(selector, timeout=5000)
                clicked = True
                print(f"Post clicked with selector: {selector}")
                break
            except Exception:
                continue
        if not clicked:
            print("WARNING: Could not click Post button")
        time.sleep(config.get("post_delay_seconds", 5))
        page.screenshot(path="/tmp/step4_after_submit.png")
        print("Screenshot saved: step4_after_submit.png")

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
    title = generate_title(subreddit["category"], config["ollama_model"], media_path)
    print(f"Title: {title}")

    print("Posting to Reddit...")
    url = post_to_reddit(config, subreddit, media_path, title)
    print(f"Done! Post URL: {url}")


if __name__ == "__main__":
    main()
