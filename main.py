#!/usr/bin/env python3
import os
import time
import mimetypes
import asyncio
import logging
import requests
import threading
import secrets
import random
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pymongo import MongoClient
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from http.server import BaseHTTPRequestHandler, HTTPServer

# Constants
ADMIN_ID = 1562465522
IST_OFFSET = timedelta(hours=5, minutes=30)
GROUP_LINK = "https://t.me/+hK0K5vZhV3owMmM1"
WELCOME_IMAGES = [
    "https://envs.sh/5OQ.jpg",
    "https://envs.sh/5OK.jpg",
    "https://envs.sh/zmX.jpg",
    "https://envs.sh/zm6.jpg"
]
DOWNLOAD_TIMEOUT = 30
MAX_RETRIES = 1

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

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load .env
load_dotenv()
API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")
LINK4EARN_API = os.getenv("LINK4EARN_API")

# MongoDB
mongo_client = MongoClient(MONGODB_URI)
db = mongo_client.get_database("telegram_bot")
downloads_collection = db.downloads
verifications_collection = db.verifications
users_collection = db.users

# Helper functions
def get_ist_time():
    return datetime.utcnow() + IST_OFFSET

def format_ist_time(dt):
    return dt.strftime('%d-%m-%Y %I:%M %p') if dt else "N/A"

async def create_verification_link(user_id):
    token = secrets.token_urlsafe(12)
    expires_at = get_ist_time() + timedelta(hours=8)
    
    verifications_collection.insert_one({
        'user_id': user_id,
        'token': token,
        'created_at': get_ist_time(),
        'expires_at': expires_at,
        'verified': False,
        'used': False
    })
    
    deep_link = f"https://telegram.me/iPopKorniaBot?start=verify-{token}"
    return await shorten_url(deep_link)

async def shorten_url(url):
    api_url = f"https://link4earn.com/api?api={LINK4EARN_API}&url={url}&format=text"
    try:
        response = requests.get(api_url, timeout=10)
        return response.text.strip() if response.status_code == 200 else url
    except Exception:
        return url

def is_user_verified(user_id):
    return verifications_collection.find_one({
        'user_id': user_id,
        'verified': True,
        'expires_at': {'$gt': get_ist_time()}
    })

async def notify_admin_new_user(user):
    try:
        await app.send_message(
            ADMIN_ID,
            f"👤 New User:\n\nName: {user.first_name}\nUsername: @{user.username}\nID: {user.id}"
        )
    except Exception as e:
        logger.error(f"Admin notify error: {e}")

async def notify_admin_download(user, filename, size):
    try:
        await app.send_message(
            ADMIN_ID,
            f"📥 New Download:\n\nFile: {filename}\nSize: {size/(1024*1024):.1f}MB\n"
            f"User: {user.first_name} (@{user.username})\nID: {user.id}"
        )
    except Exception as e:
        logger.error(f"Download notify error: {e}")

async def download_with_retry(url, filename, progress_callback):
    for attempt in range(MAX_RETRIES + 1):
        try:
            with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
                r.raise_for_status()
                total_size = int(r.headers.get('content-length', 0))
                downloaded = 0
                start_time = time.time()
                last_update = start_time

                with open(filename, 'wb') as f:
                    for chunk in r.iter_content(1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        now = time.time()
                        if now - last_update >= 1:
                            elapsed = now - start_time
                            speed = downloaded / elapsed if elapsed > 0 else 0
                            eta = (total_size - downloaded) / speed if speed > 0 else 0
                            await progress_callback(downloaded, total_size, speed, eta)
                            last_update = now
                return total_size
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise
            logger.warning(f"Attempt {attempt + 1} failed: {str(e)}")
            await asyncio.sleep(1)

def format_progress(filename, downloaded, total, speed, eta):
    percent = (downloaded / total) * 100
    filled = int(percent / 5)
    bar = '▓' * filled + '░' * (20 - filled)
    
    hours, remainder = divmod(eta, 3600)
    minutes, seconds = divmod(remainder, 60)
    eta_str = f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}" if hours > 0 else f"{int(minutes):02d}:{int(seconds):02d} minutes"
    
    return (
        f"{filename}\n"
        f"[{bar}] {percent:.2f}%\n"
        f"Downloaded: {downloaded/(1024*1024):.2f} MB / {total/(1024*1024):.2f} MB\n"
        f"Status: Downloading\n"
        f"Speed: {speed/(1024*1024):.2f} MB/s\n"
        f"ETA: {eta_str}"
    )

# Pyrogram client
app = Client(
    "koyeb_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=20,
    max_concurrent_transmissions=5
)

@app.on_message(filters.command("start"))
async def start_handler(client, message):
    user = message.from_user
    if not users_collection.find_one({'user_id': user.id}):
        users_collection.insert_one({
            'user_id': user.id,
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name or '',
            'joined_at': get_ist_time()
        })
        await notify_admin_new_user(user)
    
    if len(message.command) > 1 and message.command[1].startswith('verify-'):
        token = message.command[1][7:]
        verification = verifications_collection.find_one_and_update(
            {'token': token, 'used': False, 'expires_at': {'$gt': get_ist_time()}},
            {'$set': {'verified': True, 'used': True}}
        )
        await message.reply("✅ Verified!" if verification else "❌ Invalid link")
    else:
        try:
            await message.reply_photo(
                random.choice(WELCOME_IMAGES),
                caption="🚀 Send me a TeraBox link to download",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("Developer", url="https://t.me/Opabhik"),
                        InlineKeyboardButton("Source", url="https://t.me/True12G")
                    ],
                    [InlineKeyboardButton("Join Group", url=GROUP_LINK)]
                ])
            )
        except Exception:
            await message.reply("🚀 Send me a TeraBox link to download")

@app.on_message(filters.text & ~filters.command(["start", "status", "restart"]))
async def handle_link(client, message):
    url = message.text.strip()
    if "terabox" not in url.lower():
        return
    
    # Set random reaction
    try:
        await message.set_reaction(random.choice(["❤️", "🧐"]))
    except Exception:
        pass
    
    if not is_user_verified(message.from_user.id):
        verification_link = await create_verification_link(message.from_user.id)
        await message.reply(
            "🔒 Please verify first:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Verify Now", url=verification_link)]
            ]),
            reply_to_message_id=message.id
        )
        return
    
    try:
        # Initial status message
        status_msg = await message.reply("🔍 Fetching download info...", reply_to_message_id=message.id)
        
        # Get video info
        api_url = f"https://true12g.in/api/terabox.php?url={url}"
        api_response = requests.get(api_url, timeout=10).json()
        
        if not api_response.get('response'):
            await status_msg.edit("❌ Failed to get download info")
            return
            
        file_info = api_response['response'][0]
        dl_url = file_info['resolutions'].get('HD Video')
        thumbnail = file_info.get('thumbnail', '')
        title = file_info.get('title', f"video_{int(time.time())}")
        ext = mimetypes.guess_extension(requests.head(dl_url).headers.get('content-type', '')) or '.mp4'
        filename = f"{title[:50]}{ext}"
        temp_path = f"temp_{filename}"
        
        # Update with thumbnail and progress
        progress_msg = await message.reply_photo(
            thumbnail,
            caption=format_progress(filename, 0, 1, 0, 0),
            reply_to_message_id=message.id
        )
        await status_msg.delete()
        
        # Download progress callback
        async def update_progress(downloaded, total, speed, eta):
            await progress_msg.edit_caption(
                format_progress(filename, downloaded, total, speed, eta) +
                f"\nUser: {message.from_user.first_name} (@{message.from_user.username})"
            )
        
        # Download file
        try:
            size = await download_with_retry(dl_url, temp_path, update_progress)
            
            # Upload to Telegram
            await progress_msg.edit_caption("📤 Uploading to Telegram...")
            
            async def upload_progress(current, total):
                percent = (current / total) * 100
                await progress_msg.edit_caption(
                    f"📤 Uploading: {current/(1024*1024):.1f}MB / {total/(1024*1024):.1f}MB ({percent:.1f}%)"
                )
            
            sent_message = await app.send_video(
                chat_id=message.chat.id,
                video=temp_path,
                caption=f"✅ {filename}\nSize: {size/(1024*1024):.1f}MB",
                supports_streaming=True,
                progress=upload_progress,
                reply_to_message_id=message.id
            )
            
            await progress_msg.delete()
            await notify_admin_download(message.from_user, filename, size)
            
        except Exception as e:
            logger.error(f"Download failed: {str(e)}")
            await progress_msg.edit_caption("❌ Download failed")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
                
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        try:
            await message.reply("❌ An error occurred")
        except Exception:
            pass

async def main():
    await app.start()
    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
