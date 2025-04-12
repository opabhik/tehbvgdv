#!/usr/bin/env python3
import os
import time
import mimetypes
import asyncio
import logging
import requests
import threading
import secrets
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pymongo import MongoClient
from pyrogram import Client, filters
from pyrogram.types import Message
from http.server import BaseHTTPRequestHandler, HTTPServer

# Dummy HTTP healthcheck server
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_dummy_server():
    server = HTTPServer(("0.0.0.0", 8000), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=start_dummy_server, daemon=True).start()

# Fallback for imghdr
try:
    import imghdr
except ImportError:
    import filetype as imghdr

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load .env
load_dotenv()
API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")

# MongoDB
mongo_client = MongoClient(MONGODB_URI)
db = mongo_client.get_database("telegram_bot")
downloads_collection = db.downloads
verifications_collection = db.verifications

# Pyrogram client
app = Client("koyeb_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

def generate_verification_token():
    return secrets.token_urlsafe(12)

async def create_verification_link(user_id):
    token = generate_verification_token()
    expires_at = datetime.now() + timedelta(hours=8)
    
    verifications_collection.insert_one({
        'user_id': user_id,
        'token': token,
        'expires_at': expires_at,
        'verified': False
    })
    
    deep_link = f"https://telegram.me/TempGmailTBot?start=verify-{token}"
    api_key = os.getenv("LINK4EARN_API")
    shortener_url = f"https://link4earn.com/api?api={api_key}&url={deep_link}"
    
    try:
        response = requests.get(shortener_url)
        if response.status_code == 200:
            return response.json().get('shortenedUrl', deep_link)
    except Exception:
        pass
    
    return deep_link

def is_user_verified(user_id):
    verification = verifications_collection.find_one({
        'user_id': user_id,
        'verified': True,
        'expires_at': {'$gt': datetime.now()}
    })
    return verification is not None

# Download helper
async def download_file(url, filename, progress_callback=None):
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total_size = int(r.headers.get('content-length', 0))
        downloaded = 0
        start_time = time.time()

        with open(filename, 'wb') as f:
            last_update = time.time()

            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)

                    now = time.time()
                    if now - last_update >= 1:
                        elapsed = now - start_time
                        speed = (downloaded / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                        eta = (total_size - downloaded) / (downloaded / elapsed) if downloaded > 0 else 0

                        if progress_callback:
                            await progress_callback(downloaded, total_size, speed, eta)

                        last_update = now
    return total_size

# Progress UI
async def show_progress(msg: Message, filename, downloaded, total, speed, eta):
    percent = (downloaded / total) * 100 if total else 0
    bar = "⬢" * int(percent / 5) + "⬡" * (20 - int(percent / 5))
    text = (
        f"⬇️ `{filename}`\n"
        f"{bar} {percent:.1f}%\n"
        f"⚡ {speed:.1f} MB/s • ⏳ {eta:.0f}s\n"
        f"📦 {downloaded/(1024*1024):.1f}MB / {total/(1024*1024):.1f}MB"
    )
    try:
        await msg.edit(text)
    except Exception:
        pass

# Commands
@app.on_message(filters.command("start"))
async def start_handler(client, message):
    if len(message.command) > 1 and message.command[1].startswith('verify-'):
        token = message.command[1][7:]
        verification = verifications_collection.find_one({
            'token': token,
            'expires_at': {'$gt': datetime.now()}
        })
        
        if verification:
            verifications_collection.update_one(
                {'_id': verification['_id']},
                {'$set': {'verified': True}}
            )
            await message.reply("✅ Verification successful! You can now download videos for the next 8 hours.")
        else:
            await message.reply("❌ Invalid or expired verification link.")
    else:
        await message.reply("🚀 Send me a TeraBox link to download and upload.")

@app.on_message(filters.text & ~filters.command(["start"]))
async def handle_link(client, message):
    url = message.text.strip()
    if "terabox" not in url.lower():
        return
    
    # Check verification
    if not is_user_verified(message.from_user.id):
        verification_link = await create_verification_link(message.from_user.id)
        await message.reply(
            "🔒 You need to verify before downloading. Please click this link:\n"
            f"{verification_link}\n"
            "This verification is valid for 8 hours.",
            disable_web_page_preview=True
        )
        return
    
    try:
        api = f"https://true12g.in/api/terabox.php?url={url}"
        data = requests.get(api).json()

        if not data.get('response'):
            return await message.reply("❌ Failed to fetch download info.")

        file_info = data['response'][0]
        dl_url = file_info['resolutions'].get('HD Video')
        thumbnail = file_info.get('thumbnail', '')
        title = file_info.get('title', f"video_{int(time.time())}")
        ext = mimetypes.guess_extension(requests.head(dl_url).headers.get('content-type', '')) or '.mp4'
        filename = f"{title[:50]}{ext}"
        temp_path = f"temp_{filename}"

        # Thumbnail
        progress_msg = await message.reply_photo(thumbnail, caption="🔄 Starting download...")

        # Download
        async def progress_callback(dl, total, spd, eta):
            await show_progress(progress_msg, filename, dl, total, spd, eta)

        size = await download_file(dl_url, temp_path, progress_callback)

        await progress_msg.edit("📤 Uploading to Telegram...")

        await client.send_video(
            chat_id=message.chat.id,
            video=temp_path,
            caption=f"✅ Upload complete!\nSize: {size / (1024 * 1024):.1f}MB\nTime: {timedelta(seconds=int(time.time() - message.date.timestamp()))}",
            supports_streaming=True
        )

        await progress_msg.delete()
        os.remove(temp_path)

    except Exception as e:
        logger.error(f"Error: {e}")
        await message.reply(f"❌ Error: {e}")

if __name__ == "__main__":
    app.run()
