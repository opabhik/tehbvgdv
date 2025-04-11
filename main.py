import os
import asyncio
import requests
import time
from urllib.parse import urlparse
from pymongo import MongoClient
from dotenv import load_dotenv
from telethon import TelegramClient, events, types
from telethon.tl.functions.messages import ImportChatInviteRequest
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import logging
import mimetypes
import math
from datetime import datetime, timedelta
import signal

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Configuration
try:
    API_ID = int(os.getenv("TELEGRAM_API_ID"))
    API_HASH = os.getenv("TELEGRAM_API_HASH")
    BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
    MONGODB_URI = os.getenv("MONGODB_URI")
    ADMIN_CHANNEL = os.getenv("ADMIN_CHANNEL")  # Channel username or ID
    ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
    
    if not all([API_ID, API_HASH, BOT_TOKEN, MONGODB_URI]):
        raise ValueError("Missing required environment variables")
except Exception as e:
    logger.error(f"Configuration error: {str(e)}")
    raise

# Global state
RESTARTING = False
SHUTDOWN = False
ACTIVE_DOWNLOADS = {}

# Health check server
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if SHUTDOWN:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"Service shutting down")
        elif RESTARTING:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"Service restarting")
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

def start_health_server():
    server = HTTPServer(("0.0.0.0", 8000), HealthCheckHandler)
    logger.info("Health check server running on port 8000")
    while not SHUTDOWN:
        server.handle_request()
    server.server_close()

# Start health check server in background
health_thread = threading.Thread(target=start_health_server, daemon=True)
health_thread.start()

# MongoDB connection
try:
    mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    mongo_client.server_info()  # Test connection
    db = mongo_client.get_database("telegram_bot")
    downloads_collection = db.downloads
    users_collection = db.users
    logger.info("Connected to MongoDB")
except Exception as e:
    logger.error(f"MongoDB connection error: {str(e)}")
    raise

async def log_download(user_id, url, filename, size, status):
    download_data = {
        "user_id": user_id,
        "url": url,
        "filename": filename,
        "size_mb": size / (1024 * 1024) if size > 0 else 0,
        "timestamp": time.time(),
        "status": status
    }
    downloads_collection.insert_one(download_data)
    
    # Update user info
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"last_active": time.time()}, "$inc": {"download_count": 1}},
        upsert=True
    )
    return download_data

def get_progress_bar(percent):
    filled = '⬢' * int(percent / 5)
    empty = '⬡' * (20 - int(percent / 5))
    return f"{filled}{empty}"

def format_speed(speed):
    if speed < 1:
        return f"{speed*1024:.1f} KB/s"
    return f"{speed:.1f} MB/s"

def format_eta(seconds):
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds/60)}m {int(seconds%60)}s"
    else:
        return f"{int(seconds/3600)}h {int((seconds%3600)/60)}m"

async def download_file(url, filename, progress_callback=None, cancel_event=None):
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=50,
        pool_maxsize=50,
        max_retries=3
    )
    session.mount('http://', adapter)
    session.mount('https://', adapter)

    response = session.get(url, stream=True, timeout=30)
    response.raise_for_status()

    total_size = int(response.headers.get('content-length', 0))
    block_size = 1024 * 1024 * 4  # 4MB chunks
    downloaded = 0
    last_update = time.time()
    start_time = time.time()
    last_speed = 0
    speeds = []

    with open(filename, 'wb') as f:
        for data in response.iter_content(block_size):
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError("Download cancelled by restart")
                
            f.write(data)
            downloaded += len(data)
            
            current_time = time.time()
            elapsed = current_time - last_update
            
            if elapsed >= 1:  # Update at least once per second
                current_speed = (downloaded / (1024 * 1024)) / (current_time - start_time)
                speeds.append(current_speed)
                if len(speeds) > 5:
                    speeds.pop(0)
                avg_speed = sum(speeds) / len(speeds) if speeds else 0
                
                if progress_callback:
                    remaining = (total_size - downloaded) / (downloaded / (current_time - start_time)) if downloaded > 0 else 0
                    await progress_callback(
                        filename=filename,
                        downloaded=downloaded,
                        total=total_size,
                        speed=avg_speed,
                        eta=remaining
                    )
                last_update = current_time

    return total_size

async def get_terabox_info(url):
    try:
        api_url = f"https://true12g.in/api/terabox.php?url={url}"
        response = requests.get(api_url, timeout=30)
        data = response.json()
        
        if not data.get('response'):
            return None
            
        item = data['response'][0]
        return {
            'title': item.get('title', 'Untitled'),
            'thumbnail': item.get('thumbnail', ''),
            'hd_url': item['resolutions'].get('HD Video', ''),
            'sd_url': item['resolutions'].get('SD Video', '')
        }
    except Exception as e:
        logger.error(f"Error getting terabox info: {str(e)}")
        return None

async def create_progress_message(file_info, progress_data, phase="download"):
    title = file_info.get('title', 'Untitled')
    percent = (progress_data['downloaded'] / progress_data['total']) * 100 if progress_data['total'] > 0 else 0
    progress_bar = get_progress_bar(percent)
    speed = format_speed(progress_data['speed'])
    eta = format_eta(progress_data['eta'])
    
    if phase == "download":
        phase_text = "⬇️ Downloading"
    else:
        phase_text = "📤 Uploading"
    
    message = (
        f"<b>📁 {title}</b>\n\n"
        f"<b>{phase_text}:</b> <code>{progress_data['filename']}</code>\n"
        f"<b>📦 Size:</b> <code>{progress_data['downloaded']//(1024*1024)}MB / {progress_data['total']//(1024*1024)}MB</code>\n"
        f"<b>🚀 Speed:</b> <code>{speed}</code>\n"
        f"<b>⏳ ETA:</b> <code>{eta}</code>\n\n"
        f"<code>{progress_bar} {percent:.1f}%</code>\n\n"
        f"<i>🔄 Processing your request...</i>"
    )
    
    return message

async def process_download(event, url):
    global ACTIVE_DOWNLOADS
    
    user_id = event.sender_id
    processing_msg = None
    thumbnail_msg = None
    temp_filename = None
    last_update = time.time()
    file_info = None
    cancel_event = asyncio.Event()
    
    try:
        # Register active download
        download_id = f"{user_id}_{time.time()}"
        ACTIVE_DOWNLOADS[download_id] = {
            'event': event,
            'cancel': cancel_event,
            'start_time': time.time()
        }
        
        # Get file info
        file_info = await get_terabox_info(url)
        if not file_info or not file_info['hd_url']:
            await event.reply("❌ Could not get download link")
            return

        # Send thumbnail as separate photo message
        if file_info.get('thumbnail'):
            try:
                thumbnail_msg = await event.client.send_message(
                    event.chat_id,
                    "🔄 <b>Processing your download request...</b>",
                    file=file_info['thumbnail'],
                    parse_mode='html'
                )
            except Exception as e:
                logger.error(f"Error sending thumbnail: {str(e)}")
                thumbnail_msg = await event.reply("🔄 Processing your download request...", parse_mode='html')

        # Create initial processing message
        processing_data = {
            'filename': 'Initializing...',
            'downloaded': 0,
            'total': 1,
            'speed': 0,
            'eta': 0
        }
        progress_msg = await create_progress_message(file_info, processing_data)
        processing_msg = await event.reply(progress_msg, parse_mode='html')

        title = file_info['title']
        hd_url = file_info['hd_url']
        
        # Check file size
        head_response = requests.head(hd_url)
        file_size = int(head_response.headers.get('content-length', 0))
        
        MAX_FILE_SIZE = 2000 * 1024 * 1024  # 2GB
        if file_size > MAX_FILE_SIZE:
            await processing_msg.edit("⚠️ File too large (max 2GB supported)")
            return

        # Determine file extension
        content_type = head_response.headers.get('content-type', '')
        ext = mimetypes.guess_extension(content_type) or '.mp4'
        filename = f"{title[:50]}{ext}" if title != 'Untitled' else f"video_{int(time.time())}{ext}"
        temp_filename = f"temp_{filename}"

        # Download with progress
        download_data = await log_download(user_id, url, filename, file_size, 'downloading')
        
        async def progress_callback(filename, downloaded, total, speed, eta):
            nonlocal last_update
            current_time = time.time()
            if current_time - last_update >= 1:  # Throttle updates
                progress_data = {
                    'filename': filename,
                    'downloaded': downloaded,
                    'total': total,
                    'speed': speed,
                    'eta': eta
                }
                progress_msg = await create_progress_message(file_info, progress_data)
                try:
                    await processing_msg.edit(progress_msg, parse_mode='html')
                except Exception as e:
                    logger.error(f"Error updating progress: {str(e)}")
                last_update = current_time

        file_size = await download_file(hd_url, temp_filename, progress_callback, cancel_event)
        await log_download(user_id, url, filename, file_size, 'downloaded')

        # Upload with progress
        upload_start = time.time()
        last_upload_update = time.time()
        upload_speeds = []
        
        def upload_progress_callback(current, total):
            nonlocal last_upload_update, upload_speeds
            current_time = time.time()
            elapsed = current_time - upload_start
            current_speed = (current / (1024 * 1024)) / elapsed if elapsed > 0 else 0
            upload_speeds.append(current_speed)
            if len(upload_speeds) > 5:
                upload_speeds.pop(0)
            avg_speed = sum(upload_speeds) / len(upload_speeds) if upload_speeds else 0
            eta = (total - current) / (current / elapsed) if current > 0 else 0
            
            if current_time - last_upload_update >= 1:
                progress_data = {
                    'filename': filename,
                    'downloaded': current,
                    'total': total,
                    'speed': avg_speed,
                    'eta': eta
                }
                progress_msg = create_progress_message(file_info, progress_data, "upload")
                asyncio.create_task(
                    processing_msg.edit(progress_msg, parse_mode='html')
                )
                last_upload_update = current_time

        # Update message before starting upload
        initial_upload_msg = await create_progress_message(
            file_info,
            {
                'filename': filename,
                'downloaded': 0,
                'total': file_size,
                'speed': 0,
                'eta': 0
            },
            "upload"
        )
        await processing_msg.edit(initial_upload_msg, parse_mode='html')
        
        # Upload the file
        uploaded_file = await event.client.send_file(
            event.chat_id,
            temp_filename,
            caption=f"🎬 {title}",
            progress_callback=upload_progress_callback,
            attributes=[
                types.DocumentAttributeFilename(filename),
                types.DocumentAttributeVideo(
                    duration=0,
                    w=0,
                    h=0,
                    supports_streaming=True
                )
            ],
            part_size=1024*1024*2,  # 2MB chunks
            workers=4,              # Parallel uploads
            force_document=False,
            parse_mode='html'
        )
        
        # Forward to admin channel
        if ADMIN_CHANNEL:
            try:
                await event.client.send_message(
                    ADMIN_CHANNEL,
                    f"📤 New upload from user {user_id}\n"
                    f"📁 File: {filename}\n"
                    f"📦 Size: {file_size//(1024*1024)} MB",
                    file=uploaded_file
                )
            except Exception as e:
                logger.error(f"Error forwarding to admin channel: {str(e)}")
        
        upload_time = time.time() - upload_start
        upload_speed = file_size / (1024 * 1024) / upload_time if upload_time > 0 else 0
        
        # Delete progress messages
        if thumbnail_msg:
            try:
                await thumbnail_msg.delete()
            except:
                pass
        try:
            await processing_msg.delete()
        except:
            pass
        
        # Send final completion message
        await event.reply(
            f"✅ <b>Upload Complete!</b>\n\n"
            f"📁 <b>File:</b> <code>{filename}</code>\n"
            f"📊 <b>Size:</b> <code>{file_size//(1024*1024)} MB</code>\n"
            f"⚡ <b>Avg Speed:</b> <code>{upload_speed:.1f} MB/s</code>\n"
            f"⏱️ <b>Time:</b> <code>{timedelta(seconds=int(upload_time))}</code>",
            parse_mode='html'
        )
        await log_download(user_id, url, filename, file_size, 'uploaded')

    except asyncio.CancelledError:
        await event.reply("❌ Download was cancelled due to bot restart")
        if temp_filename:
            await log_download(user_id, url, temp_filename, 0, 'cancelled')
    except Exception as e:
        error_msg = f"❌ <b>Error:</b> <code>{str(e)}</code>"
        if processing_msg:
            await processing_msg.edit(error_msg, parse_mode='html')
        else:
            await event.reply(error_msg, parse_mode='html')
        logger.error(f"Error in process_download: {str(e)}")
        if temp_filename:
            await log_download(user_id, url, temp_filename, 0, f'failed: {str(e)}')
    finally:
        if temp_filename and os.path.exists(temp_filename):
            os.remove(temp_filename)
        if download_id in ACTIVE_DOWNLOADS:
            del ACTIVE_DOWNLOADS[download_id]

async def broadcast_message(client, message):
    try:
        users = users_collection.distinct("user_id")
        success = 0
        failed = 0
        
        for user_id in users:
            try:
                await client.send_message(user_id, message, parse_mode='html')
                success += 1
                await asyncio.sleep(0.5)  # Rate limiting
            except Exception as e:
                logger.error(f"Failed to send to {user_id}: {str(e)}")
                failed += 1
                
        return success, failed
    except Exception as e:
        logger.error(f"Broadcast error: {str(e)}")
        return 0, 0

async def cleanup_temp_files():
    for filename in os.listdir():
        if filename.startswith('temp_'):
            try:
                os.remove(filename)
                logger.info(f"Cleaned up temp file: {filename}")
            except Exception as e:
                logger.error(f"Error cleaning up temp file {filename}: {str(e)}")

async def cancel_active_downloads():
    global ACTIVE_DOWNLOADS
    
    for download_id, download_info in list(ACTIVE_DOWNLOADS.items()):
        try:
            download_info['cancel'].set()
            logger.info(f"Cancelled download {download_id}")
        except Exception as e:
            logger.error(f"Error cancelling download {download_id}: {str(e)}")

async def clear_temp_data():
    try:
        # Clear downloads collection but keep user data
        result = downloads_collection.delete_many({})
        logger.info(f"Cleared {result.deleted_count} download records")
        
        # Clean up temp files
        await cleanup_temp_files()
        
        return True
    except Exception as e:
        logger.error(f"Error clearing temp data: {str(e)}")
        return False

async def restart_bot(client):
    global RESTARTING
    
    try:
        RESTARTING = True
        
        # Notify active users
        for download_id, download_info in list(ACTIVE_DOWNLOADS.items()):
            try:
                await download_info['event'].reply("⚠️ Bot is restarting. Your download will be cancelled.")
            except Exception as e:
                logger.error(f"Error notifying user {download_id}: {str(e)}")
        
        # Cancel active downloads
        await cancel_active_downloads()
        
        # Clear temporary data
        await clear_temp_data()
        
        # Restart the bot
        logger.info("Restarting bot...")
        os.execl(sys.executable, sys.executable, *sys.argv)
        
    except Exception as e:
        logger.error(f"Error during restart: {str(e)}")
        RESTARTING = False

async def main():
    global RESTARTING, SHUTDOWN
    
    try:
        logger.info("Starting Telegram bot...")
        
        client = TelegramClient(
            'bot_session',
            API_ID,
            API_HASH,
            base_logger=logger,
            connection_retries=5,
            request_retries=5,
            flood_sleep_threshold=60
        )
        
        await client.start(bot_token=BOT_TOKEN)
        logger.info("Bot started successfully")
        
        @client.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            if RESTARTING:
                await event.reply("🔄 Bot is currently restarting. Please try again shortly.")
                return
                
            await event.reply(
                "🌟 <b>TeraBox Downloader Bot</b> 🌟\n\n"
                "Send me a TeraBox link to download and upload as video.\n\n"
                "⚡ <i>Fast downloads | HD quality | Progress tracking</i>\n"
                "🔹 <i>Multiple parallel downloads supported</i>\n\n"
                "Type /help for commands",
                parse_mode='html'
            )
            
        @client.on(events.NewMessage(pattern='/help'))
        async def help_handler(event):
            help_text = (
                "📜 <b>Available Commands:</b>\n\n"
                "/start - Start the bot\n"
                "/help - Show this help message\n"
                "/stats - Show your download stats\n"
                "/broadcast - (Admin only) Send message to all users\n"
                "/restart - (Admin only) Restart the bot\n\n"
                "📌 Just send a TeraBox link to start downloading"
            )
            await event.reply(help_text, parse_mode='html')
            
        @client.on(events.NewMessage(pattern='/stats'))
        async def stats_handler(event):
            if RESTARTING:
                await event.reply("🔄 Bot is currently restarting. Please try again shortly.")
                return
                
            user_id = event.sender_id
            count = downloads_collection.count_documents({"user_id": user_id})
            active_count = len([d for d in ACTIVE_DOWNLOADS.values() if d['event'].sender_id == user_id])
            
            await event.reply(
                f"📊 <b>Your Download Stats</b>\n\n"
                f"📂 <b>Total Files:</b> <code>{count}</code>\n"
                f"🔄 <b>Active Downloads:</b> <code>{active_count}</code>",
                parse_mode='html'
            )
            
        @client.on(events.NewMessage(pattern='/broadcast'))
        async def broadcast_handler(event):
            if event.sender_id not in ADMIN_IDS:
                await event.reply("❌ You are not authorized to use this command.")
                return
                
            if RESTARTING:
                await event.reply("🔄 Bot is currently restarting. Please try again shortly.")
                return
                
            if event.is_reply:
                reply = await event.get_reply_message()
                message = reply.text
            else:
                parts = event.text.split(' ', 1)
                if len(parts) < 2:
                    await event.reply("ℹ️ Usage: /broadcast <message> or reply to a message")
                    return
                message = parts[1]
                
            confirm = await event.reply(
                f"⚠️ <b>Are you sure you want to broadcast this message to all users?</b>\n\n"
                f"{message}\n\n"
                f"Type /confirm_broadcast to proceed or /cancel to abort",
                parse_mode='html'
            )
            
        @client.on(events.NewMessage(pattern='/confirm_broadcast'))
        async def confirm_broadcast_handler(event):
            if event.sender_id not in ADMIN_IDS:
                return
                
            if RESTARTING:
                await event.reply("🔄 Bot is currently restarting. Please try again shortly.")
                return
                
            reply = await event.get_reply_message()
            if not reply or not reply.text.startswith("⚠️ <b>Are you sure"):
                await event.reply("❌ Please reply to the broadcast confirmation message.")
                return
                
            message = '\n'.join(reply.text.split('\n')[3:-2])  # Extract the message
            await event.reply("📢 Starting broadcast... This may take some time.")
            
            success, failed = await broadcast_message(client, message)
            
            await event.reply(
                f"✅ Broadcast completed!\n\n"
                f"✓ Success: {success}\n"
                f"✗ Failed: {failed}",
                parse_mode='html'
            )
            
        @client.on(events.NewMessage(pattern='/restart'))
        async def restart_handler(event):
            global RESTARTING
            
            if event.sender_id not in ADMIN_IDS:
                await event.reply("❌ You are not authorized to use this command.")
                return
                
            if RESTARTING:
                await event.reply("🔄 Bot is already restarting.")
                return
                
            confirm = await event.reply(
                "⚠️ <b>Are you sure you want to restart the bot?</b>\n\n"
                "This will:\n"
                "1. Stop all active downloads\n"
                "2. Clear temporary download data\n"
                "3. Restart the bot\n\n"
                "Type /confirm_restart to proceed or /cancel to abort",
                parse_mode='html'
            )
            
        @client.on(events.NewMessage(pattern='/confirm_restart'))
        async def confirm_restart_handler(event):
            if event.sender_id not in ADMIN_IDS:
                return
                
            reply = await event.get_reply_message()
            if not reply or not reply.text.startswith("⚠️ <b>Are you sure"):
                await event.reply("❌ Please reply to the restart confirmation message.")
                return
                
            await event.reply("🔄 Starting restart process...")
            await restart_bot(client)
            
        @client.on(events.NewMessage(pattern='/cancel'))
        async def cancel_handler(event):
            if event.is_reply:
                reply = await event.get_reply_message()
                if reply.from_id == (await client.get_me()).id:
                    await event.reply("✅ Operation cancelled")
                    return
            await event.reply("ℹ️ No operation to cancel")
            
        @client.on(events.NewMessage())
        async def message_handler(event):
            if RESTARTING:
                await event.reply("🔄 Bot is currently restarting. Please try again shortly.")
                return
                
            if not event.text or 'http' not in event.text.lower():
                return
                
            user_id = event.sender_id
            url = event.text.strip()
            
            # Check if user already has this URL in progress
            for download_id, download_info in ACTIVE_DOWNLOADS.items():
                if (download_info['event'].sender_id == user_id and 
                    download_info['event'].text.strip() == url):
                    await event.reply("🔄 This link is already being processed. Please wait.")
                    return
            
            await process_download(event, url)
        
        logger.info("Bot is ready and listening...")
        await client.run_until_disconnected()
        
    except Exception as e:
        logger.error(f"Bot failed to start: {str(e)}")
        raise
    finally:
        SHUTDOWN = True
        if 'client' in locals():
            await client.disconnect()
        mongo_client.close()
        logger.info("Bot shutdown complete")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")
        time.sleep(5)
