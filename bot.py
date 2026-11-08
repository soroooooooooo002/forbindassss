import asyncio
import base64
import os
import threading
import time
import urllib.parse
from flask import Flask
from PIL import Image
import requests

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo


# ==========================================
# SAFE MOVIEPY IMPORT
# ==========================================

try:
    from moviepy.editor import VideoFileClip
except ImportError:
    from moviepy.video.io.VideoFileClip import VideoFileClip


# ==========================================
# 1. FLASK WEB SERVER
# ==========================================

app = Flask(__name__)


@app.route("/")
def health_check():
    return "Telegram Video Uploader is active and running!", 200


def run_web_server():
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# ==========================================
# 2. CONFIGURATION
# ==========================================

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")

CHANNEL_ID = os.getenv("CHANNEL_ID", "")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")

GROUP_LINK = os.getenv("GROUP_LINK", "https://t.me/+T_s1eAvQfykzNmM1").strip().strip('"').strip("'")
JOIN_BUTTON_TEXT = os.getenv("JOIN_BUTTON_TEXT", "Join Group")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "").strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip() or "main"
GITHUB_PROGRESS_FILE = "already_uploaded.txt"

MAX_RETRIES = max(1, int(os.getenv("MAX_RETRIES", "1")))
RETRY_DELAY = max(0, int(os.getenv("RETRY_DELAY", "20")))
START_FROM = max(1, int(os.getenv("START_FROM", "1")))

if str(CHANNEL_ID).startswith("-") or str(CHANNEL_ID).isdigit():
    CHANNEL_ID = int(CHANNEL_ID)

TEMP_FILE = "temp_video.mp4"
TEMP_WEBP = "temp_thumb.webp"
TEMP_JPG = "temp_thumb.jpg"


# ==========================================
# TELEGRAM CLIENT
# ==========================================

client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH
)


# ==========================================
# 3. KEEP ALIVE
# ==========================================

def keep_alive():
    while True:
        time.sleep(600)
        if RENDER_EXTERNAL_URL:
            try:
                requests.get(RENDER_EXTERNAL_URL, timeout=10)
            except Exception as e:
                print(f"[Keep-Alive] Ping failed: {e}", flush=True)


# ==========================================
# 4. DOWNLOAD FILE (Lightweight Sequential)
# ==========================================

def download_file_in_chunks(url, target_path):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        )
    }

    safe_url = urllib.parse.quote(url, safe=":/%?=&-")
    timeout = (20, 60)

    try:
        print("[Download] Connecting to server...", flush=True)

        with requests.get(safe_url, stream=True, headers=headers, timeout=timeout) as r:
            r.raise_for_status()
            print(f"[Download] HTTP {r.status_code}", flush=True)

            total_size = int(r.headers.get("content-length", 0))
            downloaded = 0

            if total_size:
                print(f"[Download] Size: {total_size / (1024 * 1024):.2f} MB", flush=True)
            else:
                print("[Download] Size: unknown", flush=True)

            with open(target_path, "wb") as f:
                last_report = time.time()

                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)

                    now = time.time()
                    if now - last_report >= 5:
                        downloaded_mb = downloaded / (1024 * 1024)
                        if total_size:
                            total_mb = total_size / (1024 * 1024)
                            percent = (downloaded / total_size) * 100
                            print(f"[Download] {downloaded_mb:.2f} MB / {total_mb:.2f} MB ({percent:.1f}%)", flush=True)
                        else:
                            print(f"[Download] {downloaded_mb:.2f} MB downloaded", flush=True)
                        last_report = now

            print(f"[Download] Completed: {downloaded / (1024 * 1024):.2f} MB", flush=True)

    except requests.exceptions.Timeout as e:
        raise RuntimeError(f"Download timeout: {e}")
    except requests.exceptions.HTTPError as e:
        status_code = getattr(e.response, "status_code", "unknown")
        raise RuntimeError(f"HTTP error {status_code}: {e}")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Download request failed: {e}")


# ==========================================
# 5. PARSE DATA.TXT
# ==========================================

def parse_blocks(file_path):
    if not os.path.exists(file_path):
        print(f"Error: Data file '{file_path}' not found!", flush=True)
        return []

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    delimiter = "================================================================================"
    if delimiter in content:
        raw_blocks = content.split(delimiter)
    else:
        raw_blocks = content.split("\n\n")

    parsed_items = []
    for block in raw_blocks:
        block = block.strip()
        if not block:
            continue
        data = {}
        for line in block.splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                data[key.strip()] = val.strip()
        if "file_url" in data and data["file_url"]:
            parsed_items.append(data)

    return parsed_items


# ==========================================
# 6. GITHUB PROGRESS LEDGER
# ==========================================

def github_headers():
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_progress_url():
    if not GITHUB_REPOSITORY or "/" not in GITHUB_REPOSITORY:
        return None
    owner, repo = GITHUB_REPOSITORY.split("/", 1)
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{urllib.parse.quote(GITHUB_PROGRESS_FILE)}"


def save_processed_item(index, status, title, details=""):
    url = github_progress_url()
    if not GITHUB_TOKEN or not url:
        print("[GitHub] Progress NOT saved: configure GITHUB_TOKEN and GITHUB_REPOSITORY.", flush=True)
        return False

    clean_title = " ".join(str(title).replace("\t", " ").replace("\r", " ").replace("\n", " ").split())
    clean_details = " ".join(str(details).replace("\t", " ").replace("\r", " ").replace("\n", " ").split())
    new_line = f"{int(index)}\t{status}\t{clean_title}"
    if clean_details:
        new_line += f"\t{clean_details}"

    try:
        response = requests.get(url, headers=github_headers(), params={"ref": GITHUB_BRANCH}, timeout=20)
        sha = None
        lines = []
        if response.status_code == 200:
            payload = response.json()
            sha = payload.get("sha")
            existing = base64.b64decode(payload.get("content", "")).decode("utf-8", errors="replace")
            lines = [line for line in existing.splitlines() if line.strip()]
        elif response.status_code != 404:
            response.raise_for_status()

        prefix = f"{int(index)}\t"
        lines = [line for line in lines if not line.startswith(prefix)]
        lines.append(new_line)
        content = ("\n".join(lines) + "\n").encode("utf-8")

        body = {
            "message": f"Track item {index}: {status}",
            "content": base64.b64encode(content).decode("ascii"),
            "branch": GITHUB_BRANCH,
        }
        if sha:
            body["sha"] = sha

        saved = requests.put(url, headers=github_headers(), json=body, timeout=20)
        saved.raise_for_status()
        print(f"[GitHub] Saved item {index}: {status}", flush=True)
        return True

    except Exception as e:
        print(f"[GitHub] Could not save item {index} progress: {e}", flush=True)
        return False


# ==========================================
# 7. TELEGRAM UPLOAD PROGRESS
# ==========================================

def telegram_upload_progress(current, total):
    if total <= 0:
        return

    percent = (current / total) * 100
    current_mb = current / (1024 * 1024)
    total_mb = total / (1024 * 1024)

    last_percent = getattr(telegram_upload_progress, "_last_percent", -1)
    current_step = int(percent // 5) * 5

    if current_step > last_percent or percent >= 100:
        telegram_upload_progress._last_percent = current_step
        print(f"[Upload] {current_mb:.2f} MB / {total_mb:.2f} MB ({percent:.1f}%)", flush=True)


# ==========================================
# 8. MAIN
# ==========================================

async def main():
    print("Connecting to Telegram...", flush=True)
    await client.connect()

    if not await client.is_user_authorized():
        print("CRITICAL AUTH ERROR: SESSION_STRING is invalid or expired!", flush=True)
        return

    print("Successfully authorized and connected to Telegram!", flush=True)

    threading.Thread(target=keep_alive, daemon=True).start()

    items = parse_blocks("final_data.txt")
    total_items = len(items)
    print(f"Loaded {total_items} items to upload.", flush=True)

    if total_items == 0:
        print("No items found in final_data.txt.", flush=True)
        return

    start_index = START_FROM
    print(f"[Resume] Manual START_FROM={START_FROM}; GitHub progress file: {GITHUB_PROGRESS_FILE}", flush=True)

    if start_index > total_items:
        print("==========================================", flush=True)
        print("ALL ITEMS ARE ALREADY COMPLETED.", flush=True)
        print("==========================================", flush=True)
        return

    print(f"Starting item: {start_index}/{total_items} - {items[start_index - 1].get('title', 'Untitled Video')}", flush=True)

    for index, item in enumerate(items, 1):
        if index < start_index:
            continue

        title = item.get("title", "Untitled Video").strip()
        file_url = item.get("file_url", "")
        thumb_url = item.get("thumbnail", item.get("thumb_url", ""))

        caption = f"**{title}**\n\n[{JOIN_BUTTON_TEXT} 🔗]({GROUP_LINK})"

        thumb_path = None
        duration = 0
        width = 0
        height = 0
        attempt = 0

        while True:
            thumb_path = None
            duration = 0
            width = 0
            height = 0

            try:
                print(f"[{index}/{total_items}] Downloading Video: {title}...", flush=True)
                download_file_in_chunks(file_url, TEMP_FILE)

                try:
                    print(f"[{index}/{total_items}] Reading video metadata...", flush=True)
                    with VideoFileClip(TEMP_FILE) as clip:
                        duration = int(clip.duration)
                        width = clip.w
                        height = clip.h
                    print(f"[{index}/{total_items}] Video metadata: {width}x{height}, {duration}s", flush=True)
                except Exception as e:
                    print(f"Failed to extract video metadata: {e}", flush=True)

                if thumb_url:
                    try:
                        print(f"[{index}/{total_items}] Downloading Thumbnail...", flush=True)
                        download_file_in_chunks(thumb_url, TEMP_WEBP)
                        with Image.open(TEMP_WEBP) as img:
                            rgb_img = img.convert("RGB")
                            rgb_img.save(TEMP_JPG, "JPEG")
                        thumb_path = TEMP_JPG
                        print(f"[{index}/{total_items}] Thumbnail ready.", flush=True)
                    except Exception as e:
                        print(f"Failed to process image thumbnail: {e}", flush=True)

                print(f"[{index}/{total_items}] Uploading to Telegram...", flush=True)
                telegram_upload_progress._last_percent = -1

                await client.send_file(
                    CHANNEL_ID,
                    file=TEMP_FILE,
                    thumb=thumb_path,
                    caption=caption,
                    parse_mode="md",
                    supports_streaming=True,
                    attributes=[
                        DocumentAttributeVideo(
                            duration=duration,
                            w=width,
                            h=height,
                            supports_streaming=True
                        )
                    ],
                    progress_callback=telegram_upload_progress
                )

                save_processed_item(index, "UPLOADED", title)
                print(f"[{index}/{total_items}] Uploaded successfully!", flush=True)
                break

            except FloodWaitError as e:
                attempt += 1
                print(f"Telegram FloodWait for item {index}. Attempt {attempt}/{MAX_RETRIES}.", flush=True)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(e.seconds + 5)
                    continue
                save_processed_item(index, "SKIPPED", title, str(e))
                break

            except Exception as e:
                attempt += 1
                print(f"Error processing item {index} ({title}) [attempt {attempt}/{MAX_RETRIES}]: {e}", flush=True)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                save_processed_item(index, "SKIPPED", title, str(e))
                break

            finally:
                for temp in [TEMP_FILE, TEMP_WEBP, TEMP_JPG]:
                    if os.path.exists(temp):
                        try:
                            os.remove(temp)
                        except Exception as e:
                            print(f"Could not remove {temp}: {e}", flush=True)


if __name__ == "__main__":
    if not SESSION_STRING:
        print("CRITICAL ERROR: 'SESSION_STRING' environment variable is missing!", flush=True)
        exit(1)

    threading.Thread(target=run_web_server, daemon=True).start()
    asyncio.run(main())
