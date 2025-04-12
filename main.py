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
from pyrogram.types import (
    Message, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton,
    CallbackQuery
)
from http.server import BaseHTTPRequestHandler, HTTPServer

# Constants
ADMIN_ID = 1562465522
IST_OFFSET = timedelta(hours=5, minutes=30)  # IST is UTC+5:30
GROUP_LINK = "https://t.me/+hK0K5vZhV3owMmM1"
LOADING_STICKERS = [
    "CAACAgUAAxkBAAICrmcLfBNpxMV_A4j59womoatTkTlHAAIEAAPBJDExieUdbguzyBA2BA"
]
WELCOME_IMAGES = [
    "https://envs.sh/5OQ.jpg",
    "https://envs.sh/5OK.jpg",
    "https://envs.sh/zmX.jpg",
    "https://envs.sh/zm6.jpg"
]

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
users_collection = db.users
queue_collection = db.queue

# Helper functions for IST time
def get_ist_time():
    return datetime.utcnow() + IST_OFFSET

def format_ist_time(dt):
    if dt is None:
        return "N/A"
    return dt.strftime('%d-%m-%Y %I:%M %p')

def get_remaining_time(expires_at):
    if expires_at is None:
        return "N/A"
    remaining = expires_at - get_ist_time()
    hours, remainder = divmod(remaining.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{hours}h {minutes}m"

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
    shortened_url = await shorten_url(deep_link)
    return shortened_url

def is_user_verified(user_id):
    verification = verifications_collection.find_one({
        'user_id': user_id,
        'verified': True,
        'expires_at': {'$gt': get_ist_time()}
    })
    return verification is not None

def get_verification_status(user_id):
    verification = verifications_collection.find_one({
        'user_id': user_id,
        'verified': True
    }, sort=[('expires_at', -1)])
    
    if not verification:
        return None
    
    return {
        'created_at': verification.get('created_at'),
        'expires_at': verification.get('expires_at'),
        'remaining': get_remaining_time(verification.get('expires_at'))
    }

async def notify_admin_new_user(user):
    try:
        user_info = (
            f"👤 New User:\n\n"
            f"• Name: {user.first_name} {user.last_name or ''}\n"
            f"• Username: @{user.username}\n"
            f"• ID: {user.id}\n"
            f"• Joined at: {format_ist_time(get_ist_time())}"
        )
        await app.send_message(ADMIN_ID, user_info)
    except Exception as e:
        logger.error(f"Error notifying admin: {e}")

async def check_user_in_queue(user_id):
    active_downloads = downloads_collection.count_documents({
        'user_id': user_id,
        'status': {'$in': ['downloading', 'processing']}
    })
    return active_downloads >= 2

async def add_to_queue(user_id, url):
    queue_collection.insert_one({
        'user_id': user_id,
        'url': url,
        'added_at': get_ist_time(),
        'status': 'pending'
    })

async def cancel_user_downloads(user_id):
    result = downloads_collection.update_many(
        {
            'user_id': user_id,
            'status': {'$in': ['downloading', 'processing']}
        },
        {'$set': {'status': 'cancelled'}}
    )
    return result.modified_count

def get_user_stats(user_id):
    total_downloads = downloads_collection.count_documents({'user_id': user_id})
    verification_status = get_verification_status(user_id)
    return {
        'total_downloads': total_downloads,
        'verification_status': verification_status
    }

def get_system_stats():
    total_users = users_collection.count_documents({})
    total_downloads = downloads_collection.count_documents({})
    return {
        'total_users': total_users,
        'total_downloads': total_downloads
    }

async def process_queue():
    while True:
        try:
            queued_item = queue_collection.find_one_and_update(
                {'status': 'pending'},
                {'$set': {'status': 'processing'}},
                sort=[('added_at', 1)]
            )
            
            if queued_item:
                user_id = queued_item['user_id']
                url = queued_item['url']
                
                try:
                    user = await app.get_users(user_id)
                    await handle_download(user, url, is_from_queue=True)
                except Exception as e:
                    logger.error(f"Error processing queue item: {e}")
                    await app.send_message(
                        user_id,
                        "❌ Error processing your queued download. Please try again."
                    )
                
                queue_collection.delete_one({'_id': queued_item['_id']})
            
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Queue processing error: {e}")
            await asyncio.sleep(10)

async def download_file(url, filename, progress_callback=None, cancel_flag=None):
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total_size = int(r.headers.get('content-length', 0))
            downloaded = 0
            start_time = time.time()

            with open(filename, 'wb') as f:
                last_update = time.time()

                for chunk in r.iter_content(1024 * 1024):  # 1MB chunks
                    if cancel_flag and cancel_flag.is_set():
                        raise asyncio.CancelledError("Download cancelled by user")
                    
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
    except Exception as e:
        logger.error(f"Download error: {e}")
        raise

# Pyrogram client
app = Client(
    "koyeb_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

@app.on_message(filters.command("start"))
async def start_handler(client, message):
    # Check if new user
    user = message.from_user
    if not users_collection.find_one({'user_id': user.id}):
        users_collection.insert_one({
            'user_id': user.id,
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'joined_at': get_ist_time()
        })
        await notify_admin_new_user(user)
    
    if len(message.command) > 1 and message.command[1].startswith('verify-'):
        token = message.command[1][7:]
        verification = verifications_collection.find_one({
            'token': token,
            'expires_at': {'$gt': get_ist_time()},
            'used': False
        })
        
        if verification:
            verifications_collection.update_one(
                {'_id': verification['_id']},
                {'$set': {'verified': True, 'used': True}}
            )
            await message.reply("✅ Verification successful! You can now download videos.")
        else:
            await message.reply("❌ Invalid, expired or already used verification link.")
    else:
        welcome_image = random.choice(WELCOME_IMAGES)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("👨‍💻 Developer", url="https://t.me/Opabhik"),
                InlineKeyboardButton("💻 Source Code", url="https://t.me/True12G")
            ],
            [InlineKeyboardButton("📢 Join Group", url=GROUP_LINK)]
        ])
        
        try:
            await message.reply_photo(
                welcome_image,
                caption=(
                    "🚀 Welcome to the Download Bot!\n\n"
                    "Send me a TeraBox link to download and upload.\n"
                    "Use /status to check your verification status."
                ),
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Error sending welcome image: {e}")
            await message.reply(
                "🚀 Welcome to the Download Bot!\n\n"
                "Send me a TeraBox link to download and upload.\n"
                "Use /status to check your verification status.",
                reply_markup=keyboard
            )

@app.on_message(filters.command("status"))
async def status_handler(client, message):
    try:
        user_stats = get_user_stats(message.from_user.id)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 System Stats", callback_data="system_stats")]
        ])
        
        if user_stats['verification_status']:
            await message.reply(
                "🔍 Your Status:\n\n"
                f"• Verified: ✅\n"
                f"• Expires at: {format_ist_time(user_stats['verification_status']['expires_at'])}\n"
                f"• Remaining: {user_stats['verification_status']['remaining']}\n"
                f"• Total Downloads: {user_stats['total_downloads']}",
                reply_markup=keyboard
            )
        else:
            verification_link = await create_verification_link(message.from_user.id)
            keyboard.inline_keyboard.insert(0, [
                InlineKeyboardButton("✅ Verify Now", url=verification_link)
            ])
            await message.reply(
                "🔍 Your Status:\n\n"
                "• Verified: ❌\n"
                "• You need to verify to download files\n\n"
                f"Total Downloads: {user_stats['total_downloads']}",
                reply_markup=keyboard
            )
    except Exception as e:
        logger.error(f"Error in status handler: {e}")
        await message.reply("❌ Error fetching your status. Please try again.")

@app.on_message(filters.command("restart"))
async def restart_handler(client, message):
    try:
        cancelled = await cancel_user_downloads(message.from_user.id)
        await message.reply(f"♻️ Restarted! Cancelled {cancelled} active downloads.")
    except Exception as e:
        logger.error(f"Error in restart handler: {e}")
        await message.reply("❌ Error restarting. Please try again.")

@app.on_callback_query(filters.regex("^system_stats$"))
async def system_stats_callback(client, callback_query: CallbackQuery):
    try:
        stats = get_system_stats()
        await callback_query.edit_message_text(
            "📊 System Stats:\n\n"
            f"• Total Users: {stats['total_users']}\n"
            f"• Total Downloads: {stats['total_downloads']}"
        )
    except Exception as e:
        logger.error(f"Error in system stats callback: {e}")
        await callback_query.answer("❌ Error fetching system stats", show_alert=True)

@app.on_callback_query(filters.regex("^check_status$"))
async def check_status_callback(client, callback_query: CallbackQuery):
    try:
        user_stats = get_user_stats(callback_query.from_user.id)
        if user_stats['verification_status']:
            await callback_query.answer(
                f"✅ Verified (Expires: {format_ist_time(user_stats['verification_status']['expires_at'])})",
                show_alert=True
            )
        else:
            verification_link = await create_verification_link(callback_query.from_user.id)
            await callback_query.answer(
                "❌ You are not verified yet. Please verify first when you try to download.",
                show_alert=True
            )
            await callback_query.message.reply(
                "🔒 You need to verify before downloading:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Verify Now", url=verification_link)]
                ])
            )
    except Exception as e:
        logger.error(f"Error in check status callback: {e}")
        await callback_query.answer("❌ Error checking status", show_alert=True)

@app.on_callback_query(filters.regex("^cancel_download_"))
async def cancel_download_callback(client, callback_query: CallbackQuery):
    try:
        download_id = callback_query.data.split("_")[2]
        downloads_collection.update_one(
            {'download_id': download_id},
            {'$set': {'status': 'cancelled'}}
        )
        await callback_query.answer("Download cancellation requested")
        await callback_query.edit_message_text("❌ Download cancelled by user")
    except Exception as e:
        logger.error(f"Error in cancel download callback: {e}")
        await callback_query.answer("❌ Error cancelling download", show_alert=True)

async def handle_download(user, url, is_from_queue=False):
    message = user.message if hasattr(user, 'message') else None
    loading_sticker_msg = None
    progress_msg = None
    download_id = secrets.token_hex(4)
    
    try:
        # Send loading sticker
        loading_sticker = random.choice(LOADING_STICKERS)
        if message:
            loading_sticker_msg = await message.reply_sticker(loading_sticker)

        # Fetch video info
        api = f"https://true12g.in/api/terabox.php?url={url}"
        data = requests.get(api).json()

        if not data.get('response'):
            if message:
                await message.reply("❌ Failed to fetch download info.")
            return

        file_info = data['response'][0]
        dl_url = file_info['resolutions'].get('HD Video')
        thumbnail = file_info.get('thumbnail', '')
        title = file_info.get('title', f"video_{int(time.time())}")
        ext = mimetypes.guess_extension(requests.head(dl_url).headers.get('content-type', '')) or '.mp4'
        filename = f"{title[:50]}{ext}"
        temp_path = f"temp_{filename}"

        # Delete loading sticker and send thumbnail with progress
        if message and loading_sticker_msg:
            try:
                await loading_sticker_msg.delete()
            except Exception:
                pass

            # Send thumbnail with initial progress
            cancel_button = InlineKeyboardButton(
                "❌ Cancel Download", 
                callback_data=f"cancel_download_{download_id}"
            )
            progress_msg = await message.reply_photo(
                thumbnail,
                caption="🔄 Starting download... 0%",
                reply_markup=InlineKeyboardMarkup([[cancel_button]])
            )

        # Add to active downloads
        downloads_collection.insert_one({
            'user_id': user.id,
            'filename': filename,
            'url': url,
            'status': 'downloading',
            'started_at': get_ist_time(),
            'download_id': download_id
        })

        # Download with cancel support
        cancel_flag = asyncio.Event()
        
        async def progress_callback(dl, total, spd, eta):
            if message and progress_msg:
                percent = (dl / total) * 100 if total else 0
                bar = "⬢" * int(percent / 5) + "⬡" * (20 - int(percent / 5))
                text = (
                    f"⬇️ `{filename}`\n"
                    f"{bar} {percent:.1f}%\n"
                    f"⚡ {spd:.1f} MB/s • ⏳ {eta:.0f}s\n"
                    f"📦 {dl/(1024*1024):.1f}MB / {total/(1024*1024):.1f}MB"
                )
                try:
                    await progress_msg.edit_caption(
                        caption=text,
                        reply_markup=InlineKeyboardMarkup([[cancel_button]])
                    )
                except Exception as e:
                    logger.error(f"Progress update error: {e}")

        try:
            size = await download_file(dl_url, temp_path, progress_callback, cancel_flag)
            
            if message and progress_msg:
                await progress_msg.edit_caption("📤 Uploading to Telegram...")
                await message.reply_chat_action(enums.ChatAction.UPLOAD_VIDEO)

            await app.send_video(
                chat_id=user.id,
                video=temp_path,
                caption=f"✅ Upload complete!\nSize: {size / (1024 * 1024):.1f}MB",
                supports_streaming=True,
                reply_to_message_id=message.id if message else None
            )

            if message and progress_msg:
                await progress_msg.delete()

        except asyncio.CancelledError:
            if message:
                await message.reply("❌ Download cancelled successfully.")
            return
        except Exception as e:
            logger.error(f"Download error: {e}")
            if message:
                await message.reply(f"❌ Error during download: {e}")
            return
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            downloads_collection.update_one(
                {'download_id': download_id},
                {'$set': {'status': 'completed'}}
            )

    except Exception as e:
        logger.error(f"Error: {e}")
        if message:
            await message.reply(f"❌ Error: {e}")
        if loading_sticker_msg:
            try:
                await loading_sticker_msg.delete()
            except Exception:
                pass

@app.on_message(filters.text & ~filters.command(["start", "status", "restart"]))
async def handle_link(client, message):
    url = message.text.strip()
    if "terabox" not in url.lower():
        return
    
    # Set reaction to show processing
    try:
        await message.set_reaction("🔄")
    except Exception:
        pass
    
    # Check verification
    if not is_user_verified(message.from_user.id):
        verification_link = await create_verification_link(message.from_user.id)
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Verify Now", url=verification_link)],
            [InlineKeyboardButton("🔍 Check Status", callback_data="check_status")]
        ])
        
        await message.reply(
            "🔒 You need to verify before downloading.",
            reply_markup=keyboard,
            reply_to_message_id=message.id
        )
        return
    
    # Check if user already has 2 active downloads
    if await check_user_in_queue(message.from_user.id):
        await add_to_queue(message.from_user.id, url)
        await message.reply(
            "⏳ You already have 2 active downloads. Your request has been added to queue.",
            reply_to_message_id=message.id
        )
        return
    
    # Process download
    message.message = message  # Attach message to user object
    await handle_download(message.from_user, url)

async def main():
    await app.start()
    # Start queue processing in background
    asyncio.create_task(process_queue())
    await asyncio.Event().wait()  # Run forever

if __name__ == "__main__":
    # Properly start the asyncio event loop
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
