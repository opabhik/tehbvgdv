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
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytz

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
logging.basicConfig(level=logging.INFO)
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

# Pyrogram client
app = Client("koyeb_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Helper functions
def generate_verification_token():
    return secrets.token_urlsafe(12)

async def shorten_url(url):
    api_url = f"https://link4earn.com/api?api={LINK4EARN_API}&url={url}&format=text"
    try:
        response = requests.get(api_url)
        if response.status_code == 200:
            return response.text.strip()
    except Exception as e:
        logger.error(f"Link shortening error: {e}")
    return url

async def create_verification_link(user_id):
    token = generate_verification_token()
    expires_at = datetime.now(pytz.timezone('Asia/Kolkata')) + timedelta(hours=8)
    
    verifications_collection.insert_one({
        'user_id': user_id,
        'token': token,
        'created_at': datetime.now(pytz.timezone('Asia/Kolkata')),
        'expires_at': expires_at,
        'verified': False
    })
    
    deep_link = f"https://telegram.me/iPopKorniaBot?start=verify-{token}"
    shortened_url = await shorten_url(deep_link)
    return shortened_url

def is_user_verified(user_id):
    verification = verifications_collection.find_one({
        'user_id': user_id,
        'verified': True,
        'expires_at': {'$gt': datetime.now(pytz.timezone('Asia/Kolkata'))}
    })
    return verification is not None

def get_verification_status(user_id):
    verification = verifications_collection.find_one({
        'user_id': user_id,
        'verified': True
    }, sort=[('expires_at', -1)])
    
    if not verification:
        return None
    
    india_tz = pytz.timezone('Asia/Kolkata')
    created_at = verification['created_at'].astimezone(india_tz)
    expires_at = verification['expires_at'].astimezone(india_tz)
    
    return {
        'created_at': created_at.strftime('%d-%m-%Y %I:%M %p'),
        'expires_at': expires_at.strftime('%d-%m-%Y %I:%M %p'),
        'remaining': expires_at - datetime.now(india_tz)
    }

# Download helper
async def download_file(url, filename, progress_callback=None):
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total_size = int(r.headers.get('content-length', 0))
        downloaded = 0
        start_time = time.time()

        with open(filename, 'wb') as f:
            last_update = time.time()

            for chunk in r.iter_content(1024 * 1024):  # 1MB chunks
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
            'expires_at': {'$gt': datetime.now(pytz.timezone('Asia/Kolkata'))}
        })
        
        if verification:
            verifications_collection.update_one(
                {'_id': verification['_id']},
                {'$set': {'verified': True}}
            )
            status = get_verification_status(verification['user_id'])
            await message.reply(
                "✅ Verification successful!\n\n"
                f"• Verified at: {status['created_at']}\n"
                f"• Expires at: {status['expires_at']}\n"
                f"• Remaining time: {str(status['remaining']).split('.')[0]}"
            )
        else:
            await message.reply("❌ Invalid or expired verification link.")
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Check Verification Status", callback_data="check_status")]
        ])
        await message.reply(
            "🚀 Welcome to the Download Bot!\n\n"
            "Send me a TeraBox link to download and upload.\n"
            "You need to verify first if you haven't already.",
            reply_markup=keyboard
        )

@app.on_message(filters.command("status"))
async def status_handler(client, message):
    status = get_verification_status(message.from_user.id)
    if status:
        await message.reply(
            "🔍 Your Verification Status:\n\n"
            f"• Verified at: {status['created_at']}\n"
            f"• Expires at: {status['expires_at']}\n"
            f"• Remaining time: {str(status['remaining']).split('.')[0]}"
        )
    else:
        await message.reply("❌ You are not verified yet. Please verify first when you try to download.")

@app.on_callback_query(filters.regex("^check_status$"))
async def check_status_callback(client, callback_query):
    status = get_verification_status(callback_query.from_user.id)
    if status:
        await callback_query.edit_message_text(
            "🔍 Your Verification Status:\n\n"
            f"• Verified at: {status['created_at']}\n"
            f"• Expires at: {status['expires_at']}\n"
            f"• Remaining time: {str(status['remaining']).split('.')[0]}"
        )
    else:
        await callback_query.edit_message_text("❌ You are not verified yet. Please verify first when you try to download.")

@app.on_message(filters.text & ~filters.command(["start", "status"]))
async def handle_link(client, message):
    url = message.text.strip()
    if "terabox" not in url.lower():
        return
    
    # Check verification
    if not is_user_verified(message.from_user.id):
        verification_link = await create_verification_link(message.from_user.id)
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Click to Verify", url=verification_link)],
            [InlineKeyboardButton("🔍 Check Status", callback_data="check_status")]
        ])
        
        await message.reply(
            "🔒 You need to verify before downloading.\n\n"
            "• Click the button below to verify\n"
            "• Verification is valid for 8 hours\n"
            "• All times are in IST (Indian Standard Time)",
            reply_markup=keyboard,
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
