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
from pyrogram.errors import BadRequest
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
DOWNLOAD_TIMEOUT = 45
MAX_RETRIES = 2
CHUNK_SIZE = 2 * 1024 * 1024  # 2MB chunks for upload
VERIFY_TUTORIAL = "https://t.me/True12G_offical/96"
DOWNLOAD_TUTORIAL = "https://t.me/Eagle_Looterz/3189"

# Global variable to track active downloads
active_downloads = {}

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
            f"<b>👤 New User</b>\n\n"
            f"<b>Name:</b> {user.first_name}\n"
            f"<b>Username:</b> @{user.username}\n"
            f"<b>ID:</b> <code>{user.id}</code>",
            parse_mode=enums.ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Admin notify error: {e}")

async def notify_admin_download(user, filename, size, video_file_id):
    try:
        # Forward the video to admin
        await app.forward_messages(
            chat_id=ADMIN_ID,
            from_chat_id=user.id,
            message_ids=[video_file_id]
        )
        
        # Send download info
        await app.send_message(
            ADMIN_ID,
            f"<b>📥 New Download</b>\n\n"
            f"<b>File:</b> <code>{filename}</code>\n"
            f"<b>Size:</b> {size/(1024*1024):.1f}MB\n"
            f"<b>User:</b> {user.first_name} (@{user.username})\n"
            f"<b>ID:</b> <code>{user.id}</code>",
            parse_mode=enums.ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Download notify error: {e}")

async def broadcast_message(user_ids, message):
    success = 0
    failed = 0
    for user_id in user_ids:
        try:
            await app.send_message(user_id, message)
            success += 1
            await asyncio.sleep(0.2)  # Rate limiting
        except Exception as e:
            logger.error(f"Failed to send to {user_id}: {str(e)}")
            failed += 1
    return success, failed

async def download_with_retry(url, filename, progress_callback, user_id):
    active_downloads[user_id] = True
    try:
        for attempt in range(MAX_RETRIES + 1):
            try:
                with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
                    r.raise_for_status()
                    total_size = int(r.headers.get('content-length', 0))
                    downloaded = 0
                    start_time = time.time()
                    last_update = start_time

                    with open(filename, 'wb') as f:
                        for chunk in r.iter_content(CHUNK_SIZE):
                            if not active_downloads.get(user_id, False):
                                raise asyncio.CancelledError("Download cancelled")
                                
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            now = time.time()
                            if now - last_update >= 1:  # Update every 1 second
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
    finally:
        active_downloads.pop(user_id, None)

def format_progress(filename, downloaded, total, speed, eta):
    percent = (downloaded / total) * 100
    filled = int(percent / 5)
    bar = '⬢' * filled + '⬡' * (20 - filled)
    
    # Format speed
    if speed > 1024*1024:
        speed_str = f"{speed/(1024*1024):.2f} MB/s"
    else:
        speed_str = f"{speed/1024:.2f} KB/s"
    
    # Format ETA
    if eta > 3600:
        eta_str = f"{int(eta//3600)}h {int((eta%3600)//60)}m"
    elif eta > 60:
        eta_str = f"{int(eta//60)}m {int(eta%60)}s"
    else:
        eta_str = f"{int(eta)}s"
    
    return (
        f"<b>📥 Downloading:</b> <code>{filename}</code>\n\n"
        f"<b>Progress:</b> {bar} {percent:.1f}%\n"
        f"<b>Size:</b> {downloaded/(1024*1024):.1f}MB / {total/(1024*1024):.1f}MB\n"
        f"<b>Speed:</b> {speed_str}\n"
        f"<b>ETA:</b> {eta_str}\n\n"
        f"<i>🚀 Powered by @iPopKorniaBot</i>"
    )

# Pyrogram client
app = Client(
    "koyeb_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=100,
    max_concurrent_transmissions=20
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
        await message.reply("✅ <b>Verified successfully!</b>", parse_mode=enums.ParseMode.HTML)
    else:
        try:
            await message.reply_photo(
                random.choice(WELCOME_IMAGES),
                caption=(
                    "<b>🚀 Welcome to iPopKornia Downloader Bot</b>\n\n"
                    "📌 <i>Send me any TeraBox link to download</i>\n\n"
                    "🔗 <i>Fastest downloads with premium speed</i>"
                ),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("📹 Tutorial", url=DOWNLOAD_TUTORIAL),
                        InlineKeyboardButton("👥 Join Group", url=GROUP_LINK)
                    ]
                ])
            )
        except Exception:
            await message.reply(
                "<b>🚀 Welcome to iPopKornia Downloader Bot</b>\n\n"
                "📌 <i>Send me any TeraBox link to download</i>",
                parse_mode=enums.ParseMode.HTML
            )

@app.on_message(filters.command("status"))
async def status_handler(client, message):
    user = message.from_user
    verification = verifications_collection.find_one({'user_id': user.id})
    
    if verification and verification.get('verified') and verification.get('expires_at', datetime.min) > get_ist_time():
        status_text = "✅ <b>Verified</b>"
        expiry_text = f"<b>Expires:</b> {format_ist_time(verification['expires_at'])}"
    else:
        status_text = "❌ <b>Not Verified</b>"
        expiry_text = ""
    
    await message.reply(
        f"<b>🔍 Your Verification Status</b>\n\n"
        f"{status_text}\n"
        f"{expiry_text}\n\n"
        f"<i>To verify, send any TeraBox link</i>",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📹 Verify Tutorial", url=VERIFY_TUTORIAL)]
        ])
    )

@app.on_message(filters.command("broadcast") & filters.user(ADMIN_ID))
async def broadcast_handler(client, message):
    if len(message.command) < 2:
        await message.reply("Usage: /broadcast <message>")
        return
    
    broadcast_text = message.text.split(' ', 1)[1]
    all_users = users_collection.find({}, {'user_id': 1})
    user_ids = [user['user_id'] for user in all_users]
    
    processing_msg = await message.reply(f"📢 Broadcasting to {len(user_ids)} users...")
    
    success, failed = await broadcast_message(user_ids, broadcast_text)
    
    await processing_msg.edit_text(
        f"📢 <b>Broadcast Completed</b>\n\n"
        f"✅ Success: {success}\n"
        f"❌ Failed: {failed}",
        parse_mode=enums.ParseMode.HTML
    )

@app.on_message(filters.command("restart"))
async def restart_handler(client, message):
    try:
        # Notify admin about restart
        await app.send_message(
            ADMIN_ID,
            f"♻️ <b>Bot Restarted</b>\n\n"
            f"<b>By:</b> {message.from_user.first_name} (@{message.from_user.username})\n"
            f"<b>ID:</b> <code>{message.from_user.id}</code>\n"
            f"<b>Time:</b> {format_ist_time(get_ist_time())}",
            parse_mode=enums.ParseMode.HTML
        )
        
        # Cancel all active downloads for this user
        if message.from_user.id in active_downloads:
            active_downloads[message.from_user.id] = False
            await message.reply("♻️ <b>Restarting...</b>\n\n⚠️ <i>Active downloads cancelled</i>", parse_mode=enums.ParseMode.HTML)
        else:
            await message.reply("♻️ <b>Bot restarted successfully!</b>", parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        logger.error(f"Restart error: {str(e)}")
        await message.reply("❌ <b>Error during restart</b>", parse_mode=enums.ParseMode.HTML)

def is_terabox_link(text):
    return any(domain in text for domain in ["terabox.com", "teraboxapp.com"])

@app.on_message(filters.text & ~filters.command(["start", "status", "restart", "broadcast"]))
async def handle_link(client, message):
    url = message.text.strip()
    
    if not is_terabox_link(url):
        await message.reply(
            "❌ <b>Please send a valid TeraBox link</b>\n\n"
            "<i>Example: https://www.terabox.com/...</i>",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📹 Tutorial", url=VERIFY_TUTORIAL)]
            ])
        )
        return
    
    if not is_user_verified(message.from_user.id):
        verification_link = await create_verification_link(message.from_user.id)
        await message.reply(
            "🔒 <b>Verification Required</b>\n\n"
            "<i>Please verify to access download features</i>",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Verify Now", url=verification_link)],
                [InlineKeyboardButton("📹 Tutorial", url=VERIFY_TUTORIAL)]
            ]),
            reply_to_message_id=message.id
        )
        return
    
    try:
        # Initial status message
        status_msg = await message.reply("🔍 <b>Fetching download info...</b>", parse_mode=enums.ParseMode.HTML, reply_to_message_id=message.id)
        
        # Get video info
        api_url = f"https://true12g.in/api/terabox.php?url={url}"
        try:
            api_response = requests.get(api_url, timeout=15).json()
        except Exception as e:
            logger.error(f"API request failed: {str(e)}")
            await status_msg.delete()
            await message.reply("❌ <b>Failed to fetch download info</b>", parse_mode=enums.ParseMode.HTML)
            return
            
        if not api_response.get('response'):
            await status_msg.delete()
            await message.reply("❌ <b>Invalid link or content not available</b>", parse_mode=enums.ParseMode.HTML)
            return
            
        file_info = api_response['response'][0]
        dl_url = file_info['resolutions'].get('HD Video')
        thumbnail = file_info.get('thumbnail', '')
        title = file_info.get('title', f"video_{int(time.time())}")
        ext = mimetypes.guess_extension(requests.head(dl_url).headers.get('content-type', '')) or '.mp4'
        filename = f"{title[:50]}{ext}"
        temp_path = f"temp_{filename}"
        
        # Update with thumbnail and progress - with spoiler
        progress_msg = await message.reply_photo(
            thumbnail,
            caption=(
                f"<b>📥 Preparing Download:</b> <code>{filename}</code>\n\n"
                f"<i>⚡ Initializing high-speed connection...</i>\n\n"
                f"<b>👤 User:</b> {message.from_user.first_name} (@{message.from_user.username})"
            ),
            parse_mode=enums.ParseMode.HTML,
            has_spoiler=True,
            reply_to_message_id=message.id
        )
        await status_msg.delete()
        
        # Download progress callback
        async def update_progress(downloaded, total, speed, eta):
            await progress_msg.edit_caption(
                format_progress(filename, downloaded, total, speed, eta) +
                f"\n\n<b>👤 User:</b> {message.from_user.first_name} (@{message.from_user.username})",
                parse_mode=enums.ParseMode.HTML
            )
        
        # Download file
        try:
            size = await download_with_retry(dl_url, temp_path, update_progress, message.from_user.id)
            
            # Upload to Telegram
            await progress_msg.edit_caption(
                "📤 <b>Uploading to Telegram...</b>\n\n"
                "<i>⚡ Using premium bandwidth for fast upload</i>",
                parse_mode=enums.ParseMode.HTML
            )
            
            async def upload_progress(current, total):
                percent = (current / total) * 100
                await progress_msg.edit_caption(
                    f"📤 <b>Uploading:</b> <code>{filename}</code>\n\n"
                    f"<b>Progress:</b> {current/(1024*1024):.1f}MB / {total/(1024*1024):.1f}MB ({percent:.1f}%)\n\n"
                    f"<i>🚀 Powered by @iPopKorniaBot</i>",
                    parse_mode=enums.ParseMode.HTML
                )
            
            sent_message = await app.send_video(
                chat_id=message.chat.id,
                video=temp_path,
                caption=(
                    f"✅ <b>Download Complete!</b>\n\n"
                    f"<b>File:</b> <code>{filename}</code>\n"
                    f"<b>Size:</b> {size/(1024*1024):.1f}MB\n\n"
                    f"<i>⚡ Downloaded via @iPopKorniaBot</i>"
                ),
                supports_streaming=True,
                progress=upload_progress,
                parse_mode=enums.ParseMode.HTML,
                has_spoiler=True,
                reply_to_message_id=message.id
            )
            
            await progress_msg.delete()
            await notify_admin_download(message.from_user, filename, size, sent_message.id)
            
        except asyncio.CancelledError:
            await progress_msg.edit_caption("❌ <b>Download cancelled</b>", parse_mode=enums.ParseMode.HTML)
        except Exception as e:
            logger.error(f"Download failed: {str(e)}")
            await progress_msg.edit_caption(
                "❌ <b>Download failed</b>\n\n"
                f"<i>Error: {str(e)}</i>",
                parse_mode=enums.ParseMode.HTML
            )
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
                
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        await message.reply(
            "❌ <b>An error occurred</b>\n\n"
            f"<i>{str(e)}</i>",
            parse_mode=enums.ParseMode.HTML
        )

async def main():
    # Notify admin about bot starting
    try:
        await app.start()
        print("Bot started successfully")
        await app.send_message(
            ADMIN_ID,
            "🤖 <b>Bot started successfully!</b>\n\n"
            f"<b>Time:</b> {format_ist_time(get_ist_time())}",
            parse_mode=enums.ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Startup error: {str(e)}")
    
    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user")
    finally:
        loop.close()
