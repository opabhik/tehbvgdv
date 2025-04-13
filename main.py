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
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pymongo import MongoClient
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import BadRequest
from http.server import BaseHTTPRequestHandler, HTTPServer

# Constants
ADMIN_ID = 1562465522
ADMIN_CHANNEL_ID = -1002512986775  # Your admin channel ID
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

# MongoDB Initialization with Error Handling
def initialize_mongodb():
    try:
        mongo_client = MongoClient(MONGODB_URI)
        db = mongo_client.get_database("telegram_bot")
        
        # Setup collections
        downloads_collection = db.downloads
        verifications_collection = db.verifications
        users_collection = db.users
        
        # Setup indexes with duplicate handling
        def setup_indexes():
            try:
                # Handle verifications collection
                existing_indexes = verifications_collection.index_information()
                
                if 'user_id_1' not in existing_indexes:
                    # Clean duplicates if they exist
                    duplicates = list(verifications_collection.aggregate([
                        {"$group": {
                            "_id": "$user_id",
                            "ids": {"$push": "$_id"},
                            "count": {"$sum": 1}
                        }},
                        {"$match": {"count": {"$gt": 1}}}
                    ]))
                    
                    for dup in duplicates:
                        # Keep the most recent record
                        most_recent = verifications_collection.find_one(
                            {"user_id": dup["_id"]},
                            sort=[("created_at", -1)]
                        )
                        if most_recent:
                            # Delete others
                            verifications_collection.delete_many({
                                "user_id": dup["_id"],
                                "_id": {"$ne": most_recent["_id"]}
                            })
                    
                    # Now create the index
                    verifications_collection.create_index([('user_id', 1)], unique=True)
                    logger.info("Created unique index on user_id")
                
                # Create other indexes
                verifications_collection.create_index([('token', 1)])
                verifications_collection.create_index([('expires_at', 1)])
                
                # Indexes for other collections
                users_collection.create_index([('user_id', 1)], unique=True)
                downloads_collection.create_index([('user_id', 1)])
                
            except Exception as e:
                logger.error(f"Error setting up indexes: {e}")
                # Continue without indexes if there's an error
        
        setup_indexes()
        return mongo_client, db, downloads_collection, verifications_collection, users_collection
        
    except Exception as e:
        logger.error(f"Failed to initialize MongoDB: {e}")
        raise

try:
    mongo_client, db, downloads_collection, verifications_collection, users_collection = initialize_mongodb()
except Exception as e:
    logger.error(f"Critical MongoDB initialization error: {e}")
    # Exit if we can't connect to MongoDB
    exit(1)

# Helper functions
def get_ist_time():
    return datetime.utcnow() + IST_OFFSET

def format_ist_time(dt):
    return dt.strftime('%d %b %Y, %I:%M %p') if dt else "N/A"

def format_timedelta(td):
    if not td:
        return "0 seconds"
    
    days = td.days
    hours, remainder = divmod(td.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes > 0:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds > 0 or not parts:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    
    return ", ".join(parts)

async def create_verification_link(user_id):
    # Delete any existing verification records for this user
    verifications_collection.delete_many({'user_id': user_id})
    
    token = secrets.token_urlsafe(12)
    expires_at = datetime.utcnow() + timedelta(hours=8)
    
    verifications_collection.insert_one({
        'user_id': user_id,
        'token': token,
        'created_at': datetime.utcnow(),
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

def get_verification_status(user_id):
    verification = verifications_collection.find_one({'user_id': user_id})
    
    if not verification:
        return {
            'status': 'not_verified',
            'message': "You haven't started verification yet"
        }
    
    if verification.get('verified') and verification.get('expires_at', datetime.min) > datetime.utcnow():
        remaining_time = verification['expires_at'] - datetime.utcnow()
        return {
            'status': 'verified',
            'message': "✅ Your account is verified",
            'verified_at': verification.get('created_at'),
            'expires_at': verification['expires_at'],
            'remaining_time': remaining_time
        }
    elif verification.get('verified') and verification.get('expires_at', datetime.min) <= datetime.utcnow():
        return {
            'status': 'expired',
            'message': "❌ Your verification has expired",
            'verified_at': verification.get('created_at'),
            'expires_at': verification['expires_at']
        }
    elif not verification.get('verified') and verification.get('token'):
        return {
            'status': 'pending',
            'message': "⏳ Verification link sent but not completed",
            'created_at': verification.get('created_at')
        }
    else:
        return {
            'status': 'invalid',
            'message': "❌ Invalid verification status"
        }

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

async def send_to_admin_channel(filename, size, duration, time_taken, user):
    try:
        # Upload file directly to admin channel with details in caption
        caption = (
            f"<b>📥 New Download</b>\n\n"
            f"<b>File:</b> <code>{filename}</code>\n"
            f"<b>Size:</b> {size/(1024*1024):.1f}MB\n"
            f"<b>Duration:</b> {duration}\n"
            f"<b>Time Taken:</b> {time_taken:.1f}s\n\n"
            f"<b>User:</b> {user.first_name} [<code>{user.id}</code>]"
        )
        
        with open(filename, 'rb') as file:
            await app.send_document(
                chat_id=ADMIN_CHANNEL_ID,
                document=file,
                caption=caption,
                parse_mode=enums.ParseMode.HTML,
                disable_notification=True  # This prevents notifications for other admins
            )
            
    except Exception as e:
        logger.error(f"Error sending to admin channel: {e}")

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

def is_valid_url(text):
    # Check if text looks like a URL
    url_pattern = re.compile(
        r'^(?:http|ftp)s?://'  # http:// or https://
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|'  # domain...
        r'localhost|'  # localhost...
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
        r'(?::\d+)?'  # optional port
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    return bool(url_pattern.match(text))

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
            {'token': token, 'used': False, 'expires_at': {'$gt': datetime.utcnow()}},
            {'$set': {'verified': True, 'used': True}}
        )
        await message.reply("✅ <b>Verified successfully!</b>", parse_mode=enums.ParseMode.HTML)
    else:
        try:
            await message.reply_photo(
                random.choice(WELCOME_IMAGES),
                caption=(
                    "<b>🚀 Welcome to iPopKornia Downloader Bot</b>\n\n"
                    "📌 <i>Send me any download link</i>\n\n"
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
                "📌 <i>Send me any download link</i>",
                parse_mode=enums.ParseMode.HTML
            )

@app.on_message(filters.command("status"))
async def status_handler(client, message):
    user = message.from_user
    status = get_verification_status(user.id)
    
    response = f"<b>🔍 Verification Status</b>\n\n"
    
    if status['status'] == 'verified':
        response += (
            f"🟢 <b>Status:</b> {status['message']}\n"
            f"📅 <b>Verified On:</b> {format_ist_time(status['verified_at'])}\n"
            f"⏳ <b>Time Remaining:</b> {format_timedelta(status['remaining_time'])}\n"
            f"⌛ <b>Expires On:</b> {format_ist_time(status['expires_at'])}\n\n"
            f"<i>To renew verification, simply verify again after expiration</i>"
        )
    elif status['status'] == 'expired':
        response += (
            f"🔴 <b>Status:</b> {status['message']}\n"
            f"📅 <b>Was Verified On:</b> {format_ist_time(status['verified_at'])}\n"
            f"⌛ <b>Expired On:</b> {format_ist_time(status['expires_at'])}\n\n"
            f"<i>Send any link to get a new verification</i>"
        )
    elif status['status'] == 'pending':
        response += (
            f"🟡 <b>Status:</b> {status['message']}\n"
            f"📅 <b>Link Sent On:</b> {format_ist_time(status['created_at'])}\n\n"
            f"<i>Complete verification by clicking the link sent to you</i>"
        )
    else:
        response += (
            f"🔴 <b>Status:</b> {status['message']}\n\n"
            f"<i>Send any link to start verification</i>"
        )
    
    # Add buttons based on status
    buttons = []
    if status['status'] != 'verified':
        buttons.append([InlineKeyboardButton("📹 Verify Tutorial", url=VERIFY_TUTORIAL)])
    buttons.append([InlineKeyboardButton("👥 Join Group", url=GROUP_LINK)])
    
    await message.reply(
        response,
        parse_mode=enums.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons)
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
            f"<b>By:</b> {message.from_user.first_name} [<code>{message.from_user.id}</code>]\n"
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

@app.on_message(filters.text & ~filters.command(["start", "status", "restart", "broadcast"]))
async def handle_link(client, message):
    url = message.text.strip()
    
    if not is_valid_url(url):
        await message.reply(
            "❌ <b>Please send a valid URL</b>\n\n"
            "<i>Example: https://example.com/file</i>",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📹 Tutorial", url=VERIFY_TUTORIAL)]
            ])
        )
        return
    
    user_status = get_verification_status(message.from_user.id)
    if user_status['status'] != 'verified':
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
        # Immediately show download starting
        filename = url.split('/')[-1][:50] or f"file_{int(time.time())}"
        progress_msg = await message.reply(
            f"<b>📥 Starting Download:</b> <code>{filename}</code>\n\n"
            f"<b>👤 User:</b> {message.from_user.first_name} [<code>{message.from_user.id}</code>]\n"
            f"<i>⚡ Connecting to high-speed server...</i>",
            parse_mode=enums.ParseMode.HTML
        )
        
        # Get video info
        api_url = f"https://true12g.in/api/terabox.php?url={url}"
        try:
            api_response = requests.get(api_url, timeout=15).json()
        except Exception as e:
            logger.error(f"API request failed: {str(e)}")
            await progress_msg.edit_text("❌ <b>Failed to fetch download info</b>", parse_mode=enums.ParseMode.HTML)
            return
            
        if not api_response.get('response'):
            await progress_msg.edit_text("❌ <b>Invalid link or content not available</b>", parse_mode=enums.ParseMode.HTML)
            return
            
        file_info = api_response['response'][0]
        dl_url = file_info['resolutions'].get('HD Video')
        thumbnail = file_info.get('thumbnail', '')
        title = file_info.get('title', filename)
        duration = file_info.get('duration', 'N/A')
        ext = mimetypes.guess_extension(requests.head(dl_url).headers.get('content-type', '')) or '.mp4'
        filename = f"{title[:50]}{ext}"
        temp_path = f"temp_{filename}"
        
        # Update with progress
        async def update_progress(downloaded, total, speed, eta):
            await progress_msg.edit_text(
                format_progress(filename, downloaded, total, speed, eta) +
                f"\n\n<b>👤 User:</b> {message.from_user.first_name} [<code>{message.from_user.id}</code>]",
                parse_mode=enums.ParseMode.HTML
            )
        
        # Download file
        try:
            start_time = time.time()
            size = await download_with_retry(dl_url, temp_path, update_progress, message.from_user.id)
            download_time = time.time() - start_time
            
            # Upload to Telegram
            await progress_msg.edit_text(
                "📤 <b>Uploading to Telegram...</b>\n\n"
                f"<b>File:</b> <code>{filename}</code>\n"
                f"<b>Size:</b> {size/(1024*1024):.1f}MB\n"
                f"<b>Download Time:</b> {download_time:.1f}s\n\n"
                f"<b>👤 User:</b> {message.from_user.first_name} [<code>{message.from_user.id}</code>]",
                parse_mode=enums.ParseMode.HTML
            )
            
            # Send file directly to admin channel with details in caption
            await send_to_admin_channel(temp_path, size, duration, download_time, message.from_user)
            
            # Send to user
            await app.send_video(
                chat_id=message.chat.id,
                video=temp_path,
                caption=(
                    f"✅ <b>Download Complete!</b>\n\n"
                    f"<b>File:</b> <code>{filename}</code>\n"
                    f"<b>Size:</b> {size/(1024*1024):.1f}MB\n"
                    f"<b>Time Taken:</b> {download_time:.1f}s\n\n"
                    f"<i>⚡ Downloaded via @iPopKorniaBot</i>"
                ),
                supports_streaming=True,
                parse_mode=enums.ParseMode.HTML,
                reply_to_message_id=message.id
            )
            
            await progress_msg.delete()
            
        except asyncio.CancelledError:
            await progress_msg.edit_text("❌ <b>Download cancelled</b>", parse_mode=enums.ParseMode.HTML)
        except Exception as e:
            logger.error(f"Download failed: {str(e)}")
            await progress_msg.edit_text(
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

async def cleanup_expired_verifications():
    while True:
        try:
            result = verifications_collection.delete_many({
                'expires_at': {'$lt': datetime.utcnow()}
            })
            logger.info(f"Cleaned up {result.deleted_count} expired verifications")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        
        await asyncio.sleep(3600)  # Run every hour

async def main():
    # Start cleanup task
    asyncio.create_task(cleanup_expired_verifications())
    
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
