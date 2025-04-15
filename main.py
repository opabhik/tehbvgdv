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
from pyrogram.errors import BadRequest, FloodWait
from http.server import BaseHTTPRequestHandler, HTTPServer

# Constants
ADMIN_ID = 1562465522
ADMIN_CHANNEL_ID = -1002207398347  # Your dump channel ID
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
CHUNK_SIZE = 4 * 1024 * 1024  # 4MB chunks for faster download
VERIFY_TUTORIAL = "https://t.me/True12G_offical/96"
DOWNLOAD_TUTORIAL = "https://t.me/Eagle_Looterz/3189"
MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100MB

# Global variables
active_downloads = {}
user_download_tasks = {}
broadcast_posts = {}

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

# MongoDB Initialization
def initialize_mongodb():
    try:
        mongo_client = MongoClient(MONGODB_URI)
        db = mongo_client.get_database("telegram_bot")
        users_collection = db.users
        verifications_collection = db.verifications
        downloads_collection = db.downloads
        
        # Create indexes for users_collection
        users_collection.create_index([('user_id', 1)], unique=True)
        
        # Handle verifications_collection indexes
        verification_indexes = verifications_collection.index_information()
        if 'expires_at_1' in verification_indexes:
            existing_index = verification_indexes['expires_at_1']
            if existing_index.get('expireAfterSeconds') != 0:
                verifications_collection.drop_index('expires_at_1')
                verifications_collection.create_index(
                    [('expires_at', 1)],
                    expireAfterSeconds=0,
                    name='expires_at_1'
                )
        else:
            verifications_collection.create_index(
                [('expires_at', 1)],
                expireAfterSeconds=0,
                name='expires_at_1'
            )
        
        # Other indexes
        verifications_collection.create_index([('user_id', 1)], unique=True)
        verifications_collection.create_index([('token', 1)])
        downloads_collection.create_index([('user_id', 1)])
        
        return mongo_client, db, downloads_collection, verifications_collection, users_collection
        
    except Exception as e:
        logger.error(f"Failed to initialize MongoDB: {e}")
        raise

try:
    mongo_client, db, downloads_collection, verifications_collection, users_collection = initialize_mongodb()
except Exception as e:
    logger.error(f"Critical MongoDB initialization error: {e}")
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
    
    deep_link = f"https://telegram.me/TeraboxDownloader_5Bot?start=verify-{token}"
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
        return {'status': 'not_verified', 'message': "You haven't started verification yet"}
    
    if verification.get('verified') and verification.get('expires_at', datetime.min) > datetime.utcnow():
        remaining_time = verification['expires_at'] - datetime.utcnow()
        return {
            'status': 'verified',
            'message': "✅ Your account is verified , Check Status : /status",
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
        return {'status': 'invalid', 'message': "❌ Invalid verification status"}

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

async def send_to_dump_channel(file_path, filename, size, duration, time_taken, user, thumbnail_url=None):
    try:
        thumbnail_path = None
        if thumbnail_url:
            try:
                thumbnail_path = f"thumb_{user.id}.jpg"
                with requests.get(thumbnail_url, stream=True, timeout=10) as r:
                    r.raise_for_status()
                    with open(thumbnail_path, 'wb') as f:
                        for chunk in r.iter_content(1024):
                            f.write(chunk)
            except Exception as e:
                logger.error(f"Error downloading thumbnail: {e}")
                thumbnail_path = None
        
        caption = (
            f"<b>📥 Download Details</b>\n"
            
            f"<b>File:</b> <code>{filename}</code>\n"
            f"<b>Size:</b> {size/(1024*1024):.1f}MB\n"
            f"<b>Duration:</b> {duration}\n"
            f"<b>Time Taken:</b> {time_taken:.1f}s\n"
            f"<b>User:</b> {user.first_name} [<code>{user.id}</code>]\n"
            
        )
        
        with open(file_path, 'rb') as file:
            if file_path.endswith(('.mp4', '.mkv', '.mov')):
                await app.send_video(
                    chat_id=-1002301352491,
                    video=file,
                    caption=caption,
                    parse_mode=enums.ParseMode.HTML,
                    thumb=thumbnail_path,
                    supports_streaming=True,
                    disable_notification=True
                )
            else:
                await app.send_document(
                    chat_id=-1002301352491,
                    document=file,
                    caption=caption,
                    parse_mode=enums.ParseMode.HTML,
                    thumb=thumbnail_path,
                    disable_notification=True
                )
        
        if thumbnail_path and os.path.exists(thumbnail_path):
            os.remove(thumbnail_path)
            
    except Exception as e:
        logger.error(f"Error sending to dump channel: {e}")

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
                            if now - last_update >= 2:  # Update every 2 seconds
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
        user_download_tasks.pop(user_id, None)

def format_progress(filename, downloaded, total, speed, eta):
    percent = (downloaded / total) * 100
    filled = int(percent / 10)
    progress_bar = '▓' * filled + '░' * (10 - filled)
    
    speed_str = f"{speed/(1024*1024):.2f} MB/s" if speed > 1024*1024 else f"{speed/1024:.2f} KB/s"
    eta_str = f"{int(eta//3600)}h {int((eta%3600)//60)}m" if eta > 3600 else f"{int(eta//60)}m {int(eta%60)}s" if eta > 60 else f"{int(eta)}s"
    
    return (
        f"<b>📥 Downloading:</b> <code>{filename}</code>\n\n"
        f"<b>Progress:</b> [{progress_bar}] {percent:.2f}%\n"
        f"<b>Size:</b> {downloaded/(1024*1024):.1f}MB / {total/(1024*1024):.1f}MB\n"
        f"<b>Speed:</b> {speed_str}\n"
        f"<b>ETA:</b> {eta_str}\n\n"
        f"<i>🚀 Powered by @TempGmailTBot</i>"
    )

def is_valid_url(text):
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
                "<b>🚀 Welcome to Terabox Downloader Bot</b>\n\n"
                "📌 <i>Send me any TeraBox link</i>",
                parse_mode=enums.ParseMode.HTML
            )

@app.on_callback_query(filters.regex("^restart_bot$"))
async def restart_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if user_id in active_downloads:
        active_downloads[user_id] = False
        await callback_query.answer("All active downloads cancelled. Please try again.")
    else:
        await callback_query.answer("No active downloads to cancel.")
    await callback_query.message.delete()

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
    
    buttons = []
    if status['status'] != 'verified':
        buttons.append([InlineKeyboardButton("📹 Verify Tutorial", url=VERIFY_TUTORIAL)])
    buttons.append([InlineKeyboardButton("👥 Join Group", url=GROUP_LINK)])
    buttons.append([InlineKeyboardButton("♻️ Restart", callback_data="restart_bot")])
    
    await message.reply(
        response,
        parse_mode=enums.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@app.on_message(filters.command("broadcast") & filters.user(ADMIN_ID))
async def broadcast_handler(client, message):
    if len(message.command) < 2:
        await message.reply(
            "Please reply to a message with /broadcast to send it to all users\n"
            "Or use /broadcast <message> to send a text message",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Cancel", callback_data="cancel_broadcast")]
            ])
        )
        return
    
    if message.reply_to_message:
        broadcast_posts[message.from_user.id] = message.reply_to_message
        await message.reply(
            "You've selected a message to broadcast. Confirm?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes", callback_data="confirm_broadcast")],
                [InlineKeyboardButton("❌ No", callback_data="cancel_broadcast")]
            ])
        )
    else:
        broadcast_text = message.text.split(' ', 1)[1]
        broadcast_posts[message.from_user.id] = broadcast_text
        await message.reply(
            f"Broadcast this message to all users?\n\n{broadcast_text}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes", callback_data="confirm_broadcast")],
                [InlineKeyboardButton("❌ No", callback_data="cancel_broadcast")]
            ])
        )

@app.on_callback_query(filters.regex("^confirm_broadcast$"))
async def confirm_broadcast(client, callback_query):
    user_id = callback_query.from_user.id
    if user_id not in broadcast_posts:
        await callback_query.answer("No broadcast message found!")
        return
    
    broadcast_content = broadcast_posts.pop(user_id)
    all_users = users_collection.find({}, {'user_id': 1})
    user_ids = [user['user_id'] for user in all_users]
    
    processing_msg = await callback_query.message.edit_text(f"📢 Broadcasting to {len(user_ids)} users...")
    
    success = 0
    failed = 0
    
    for user_id in user_ids:
        try:
            if isinstance(broadcast_content, str):
                await app.send_message(user_id, broadcast_content)
            else:
                await broadcast_content.copy(user_id)
            success += 1
            await asyncio.sleep(0.1)  # Rate limiting
        except Exception as e:
            logger.error(f"Failed to send to {user_id}: {str(e)}")
            failed += 1
    
    await processing_msg.edit_text(
        f"📢 <b>Broadcast Completed</b>\n\n"
        f"✅ Success: {success}\n"
        f"❌ Failed: {failed}",
        parse_mode=enums.ParseMode.HTML
    )
    await callback_query.answer()

@app.on_callback_query(filters.regex("^cancel_broadcast$"))
async def cancel_broadcast(client, callback_query):
    user_id = callback_query.from_user.id
    if user_id in broadcast_posts:
        broadcast_posts.pop(user_id)
    await callback_query.message.delete()
    await callback_query.answer("Broadcast cancelled")

@app.on_message(filters.command("restart"))
async def restart_handler(client, message):
    user_id = message.from_user.id
    if user_id in active_downloads:
        active_downloads[user_id] = False
        await message.reply(
            "♻️ <b>Restarting...</b>\n\n"
            "⚠️ <i>All active downloads cancelled</i>\n"
            "Please try your download again",
            parse_mode=enums.ParseMode.HTML
        )
    else:
        await message.reply(
            "♻️ <b>No active downloads to cancel</b>\n\n"
            "You can start new downloads now",
            parse_mode=enums.ParseMode.HTML
        )

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
from pyrogram.errors import BadRequest, FloodWait
from http.server import BaseHTTPRequestHandler, HTTPServer

# Constants
ADMIN_ID = 1562465522
ADMIN_CHANNEL_ID = -1002207398347  # Your dump channel ID
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
CHUNK_SIZE = 4 * 1024 * 1024  # 4MB chunks for faster download
VERIFY_TUTORIAL = "https://t.me/True12G_offical/96"
DOWNLOAD_TUTORIAL = "https://t.me/Eagle_Looterz/3189"
MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100MB

# Global variables
active_downloads = {}
user_download_tasks = {}
broadcast_posts = {}

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

# [Previous helper functions remain the same until handle_link]

@app.on_message(filters.text & ~filters.command(["start", "status", "restart", "broadcast"]))
async def handle_link(client, message):
    user = message.from_user
    url = message.text.strip()
    
    if not is_valid_url(url):
        await message.reply(
            "❌ <b>Please send a valid URL</b>\n\n"
            "<i>Example: https://terabox.com/...</i>",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📹 Tutorial", url=VERIFY_TUTORIAL)]
            ])
        )
        return
    
    if user.id in user_download_tasks:
        await message.reply(
            "⏳ <b>You already have a download in progress</b>\n\n"
            "Please wait for it to complete or use /restart to cancel it",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("♻️ Restart", callback_data="restart_bot")]
            ])
        )
        return
    
    user_status = get_verification_status(user.id)
    if user_status['status'] != 'verified':
        verification_link = await create_verification_link(user.id)
        await message.reply(
            "🔒 <b>Verification Required</b>\n\n"
            "<i>Please verify to access download features</i>",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Verify Now", url=verification_link)],
                [InlineKeyboardButton("📹 Tutorial", url=VERIFY_TUTORIAL)]
            ])
        )
        return
    
    rocket_msg = await message.reply("🚀")
    
    try:
        try:
            api_url = f"https://true12g.in/api/terabox.php?url={url}"
            api_response = requests.get(api_url, timeout=15).json()
            
            if not api_response.get('response'):
                await rocket_msg.edit_text("❌ <b>Invalid link or content not available</b>", parse_mode=enums.ParseMode.HTML)
                return
                
            file_info = api_response['response'][0]
            dl_url = file_info['resolutions'].get('HD Video')
            thumbnail = file_info.get('thumbnail', '')
            title = file_info.get('title', url.split('/')[-1][:50])
            duration = file_info.get('duration', 'N/A')
            ext = mimetypes.guess_extension(requests.head(dl_url).headers.get('content-type', '')) or '.mp4'
            filename = f"{title[:50]}{ext}"
            temp_path = f"temp_{user.id}_{int(time.time())}{ext}"
            
            # Get file size before downloading
            head_response = requests.head(dl_url)
            file_size = int(head_response.headers.get('content-length', 0))
            
        except Exception as e:
            logger.error(f"API request failed: {str(e)}")
            await rocket_msg.edit_text("❌ <b>Failed to fetch download info</b>", parse_mode=enums.ParseMode.HTML)
            return
        
        try:
            if thumbnail:
                thumb_path = f"thumb_{user.id}.jpg"
                with requests.get(thumbnail, stream=True, timeout=10) as r:
                    r.raise_for_status()
                    with open(thumb_path, 'wb') as f:
                        for chunk in r.iter_content(1024):
                            f.write(chunk)
                
                await rocket_msg.delete()
                progress_msg = await message.reply_photo(
                    photo=thumb_path,
                    caption=(
                        f"<b>📥 Starting Download:</b> <code>{filename}</code>\n\n"
                        f"<b>👤 User:</b> {user.first_name} [<code>{user.id}</code>]\n"
                        f"<i>⚡ Connecting to high-speed server...</i>"
                    ),
                    parse_mode=enums.ParseMode.HTML,
                    has_spoiler=True
                )
                os.remove(thumb_path)
            else:
                await rocket_msg.edit_text(
                    f"<b>📥 Starting Download:</b> <code>{filename}</code>\n\n"
                    f"<b>👤 User:</b> {user.first_name} [<code>{user.id}</code>]\n"
                    f"<i>⚡ Connecting to high-speed server...</i>",
                    parse_mode=enums.ParseMode.HTML
                )
                progress_msg = rocket_msg

            # Define progress callback
            async def update_progress(downloaded, total, speed, eta):
                progress_text = format_progress(filename, downloaded, total, speed, eta)
                try:
                    await progress_msg.edit_text(
                        progress_text + 
                        f"\n\n<b>👤 User:</b> {user.first_name} [<code>{user.id}</code>]",
                        parse_mode=enums.ParseMode.HTML
                    )
                except Exception as e:
                    logger.error(f"Progress update error: {e}")

            try:
                user_download_tasks[user.id] = asyncio.create_task(
                    download_with_retry(dl_url, temp_path, update_progress, user.id)
                )

                start_time = time.time()
                size = await user_download_tasks[user.id]
                download_time = time.time() - start_time
                
                await progress_msg.edit_text(
                    "📤 <b>Processing file...</b>\n\n"
                    f"<b>File:</b> <code>{filename}</code>\n"
                    f"<b>Size:</b> {size/(1024*1024):.1f}MB\n"
                    f"<b>Download Time:</b> {download_time:.1f}s\n\n"
                    f"<b>👤 User:</b> {user.first_name} [<code>{user.id}</code>]",
                    parse_mode=enums.ParseMode.HTML
                )
                
                await send_to_dump_channel(temp_path, filename, size, duration, download_time, user, thumbnail)
                
                # Check file size and decide whether to upload or send links
                if size > MAX_UPLOAD_SIZE:
                    # File is too large for Telegram upload, send download links
                    online_stream_url = f"https://opabhik.serv00.net/Watch.php?url={dl_url}"
                    
                    await message.reply(
                        f"📁 <b>File is too large for Telegram upload ({size/(1024*1024):.1f}MB > 100MB)</b>\n\n"
                        f"<b>File Name:</b> <code>{filename}</code>\n"
                        f"<b>Size:</b> {size/(1024*1024):.1f}MB\n\n"
                        "🔗 <b>Download Links:</b>\n"
                        f"1. <a href='{dl_url}'>Direct Download Link</a>\n"
                        f"2. <a href='{online_stream_url}'>Online Stream Link</a>\n\n"
                        "<i>⚠️ Note: Large files can't be uploaded directly to Telegram</i>",
                        parse_mode=enums.ParseMode.HTML,
                        disable_web_page_preview=True,
                        reply_markup=InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton("📥 Direct Download", url=dl_url),
                                InlineKeyboardButton("▶️ Online Stream", url=online_stream_url)
                            ],
                            [
                                InlineKeyboardButton("👥 Join Group", url=GROUP_LINK)
                            ]
                        ])
                    )
                else:
                    # File is small enough, upload directly
                    await app.send_video(
                        chat_id=message.chat.id,
                        video=temp_path,
                        caption=(
                            f"✅ <b>Download Complete!</b>\n\n"
                            f"<b>File:</b> <code>{filename}</code>\n"
                            f"<b>Size:</b> {size/(1024*1024):.1f}MB\n"
                            f"<b>Time Taken:</b> {download_time:.1f}s\n\n"
                            f"<i>⚡ Downloaded via @TempGmailTBot</i>"
                        ),
                        supports_streaming=True,
                        parse_mode=enums.ParseMode.HTML,
                        reply_to_message_id=message.id,
                        has_spoiler=True
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
                user_download_tasks.pop(user.id, None)
                
        except Exception as e:
            logger.error(f"Error: {str(e)}")
            await message.reply(
                "❌ <b>An error occurred</b>\n\n"
                f"<i>{str(e)}</i>",
                parse_mode=enums.ParseMode.HTML
            )
            if os.path.exists(temp_path):
                os.remove(temp_path)
            user_download_tasks.pop(user.id, None)
    except Exception as e:
        logger.error(f"Error in handle_link: {str(e)}")
        await message.reply(
            "❌ <b>An unexpected error occurred</b>\n\n"
            f"<i>{str(e)}</i>",
            parse_mode=enums.ParseMode.HTML
        )

# [Rest of the code remains the same]

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
    asyncio.create_task(cleanup_expired_verifications())
    
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
