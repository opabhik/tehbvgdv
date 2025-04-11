import os
import asyncio
import requests
import time
from urllib.parse import urlparse
from pymongo import MongoClient
from dotenv import load_dotenv
from telethon import TelegramClient, events, types
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import logging
import mimetypes
import math
from datetime import datetime, timedelta

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
    
    if not all([API_ID, API_HASH, BOT_TOKEN, MONGODB_URI]):
        raise ValueError("Missing required environment variables")
except Exception as e:
    logger.error(f"Configuration error: {str(e)}")
    raise

# Health check server
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_health_server():
    server = HTTPServer(("0.0.0.0", 8000), HealthCheckHandler)
    logger.info("Health check server running on port 8000")
    server.serve_forever()

# Start health check server in background
health_thread = threading.Thread(target=start_health_server, daemon=True)
health_thread.start()

# MongoDB connection
try:
    mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    mongo_client.server_info()  # Test connection
    db = mongo_client.get_database("telegram_bot")
    downloads_collection = db.downloads
    logger.info("Connected to MongoDB")
except Exception as e:
    logger.error(f"MongoDB connection error: {str(e)}")
    raise

async def log_download(user_id, url, filename, size, status):
    downloads_collection.insert_one({
        "user_id": user_id,
        "url": url,
        "filename": filename,
        "size_mb": size / (1024 * 1024),
        "timestamp": time.time(),
        "status": status
    })

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

async def download_file(url, filename, progress_callback=None):
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
            f.write(data)
            downloaded += len(data)
            
            current_time = time.time()
            elapsed = current_time - last_update
            
            # Update at least once per second
            if elapsed >= 1:
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
    
    message = (
        f"<b>📁 {title}</b>\n\n"
        f"<b>⬇️ Downloading:</b> <code>{progress_data['filename']}</code>\n"
        f"<b>📦 Size:</b> <code>{progress_data['downloaded']//(1024*1024)}MB / {progress_data['total']//(1024*1024)}MB</code>\n"
        f"<b>🚀 Speed:</b> <code>{speed}</code>\n"
        f"<b>⏳ ETA:</b> <code>{eta}</code>\n\n"
        f"<code>{progress_bar} {percent:.1f}%</code>\n\n"
        f"<i>🔄 Processing your request...</i>"
    )
    
    return message

async def process_download(event, url):
    user_id = event.sender_id
    processing_msg = None
    temp_filename = None
    last_update = time.time()
    file_info = None
    
    try:
        # Get file info
        file_info = await get_terabox_info(url)
        if not file_info or not file_info['hd_url']:
            await event.reply("❌ Could not get download link")
            return

        # Create initial processing message
        processing_data = {
            'filename': 'Initializing...',
            'downloaded': 0,
            'total': 1,
            'speed': 0,
            'eta': 0
        }
        progress_msg = await create_progress_message(file_info, processing_data)
        
        # Send thumbnail as separate message if available
        if file_info.get('thumbnail'):
            try:
                await event.client.send_message(
                    event.chat_id,
                    "🔄 Processing your download request...",
                    file=file_info['thumbnail']
                )
            except Exception as e:
                logger.error(f"Error sending thumbnail: {str(e)}")

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
        await log_download(user_id, url, filename, file_size, 'downloading')
        
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

        file_size = await download_file(hd_url, temp_filename, progress_callback)
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

        # Upload the file
        await processing_msg.edit("📤 Starting upload...", parse_mode='html')
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
        await processing_msg.edit(
            f"✅ <b>Upload Complete!</b>\n\n"
            f"📁 <b>File:</b> <code>{filename}</code>\n"
            f"📊 <b>Size:</b> <code>{file_size//(1024*1024)} MB</code>\n"
            f"⚡ <b>Avg Speed:</b> <code>{upload_speed:.1f} MB/s</code>\n"
            f"⏱️ <b>Time:</b> <code>{timedelta(seconds=int(upload_time))}</code>",
            parse_mode='html'
        )
        await log_download(user_id, url, filename, file_size, 'uploaded')

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

async def main():
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
        
        # Track active downloads per user (not globally)
        user_active_downloads = {}
        
        @client.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            await event.reply(
                "🌟 <b>TeraBox Downloader Bot</b> 🌟\n\n"
                "Send me a TeraBox link to download and upload as video.\n\n"
                "⚡ <i>Fast downloads | HD quality | Progress tracking</i>\n"
                "🔹 <i>Multiple parallel downloads supported</i>",
                parse_mode='html'
            )
            
        @client.on(events.NewMessage(pattern='/stats'))
        async def stats_handler(event):
            user_id = event.sender_id
            count = downloads_collection.count_documents({"user_id": user_id})
            await event.reply(
                f"📊 <b>Your Download Stats</b>\n\n"
                f"📂 <b>Total Files:</b> <code>{count}</code>\n"
                f"🔄 <b>Active Downloads:</b> <code>{len(user_active_downloads.get(user_id, []))}</code>",
                parse_mode='html'
            )
            
        @client.on(events.NewMessage())
        async def message_handler(event):
            if not event.text or 'http' not in event.text.lower():
                return
                
            user_id = event.sender_id
            if user_id not in user_active_downloads:
                user_active_downloads[user_id] = []
                
            if event.text in user_active_downloads[user_id]:
                await event.reply("🔄 This link is already being processed. Please wait.")
                return
                
            try:
                user_active_downloads[user_id].append(event.text)
                await process_download(event, event.text)
            except Exception as e:
                logger.error(f"Error in message_handler: {str(e)}")
            finally:
                if event.text in user_active_downloads.get(user_id, []):
                    user_active_downloads[user_id].remove(event.text)
        
        logger.info("Bot is ready and listening...")
        await client.run_until_disconnected()
        
    except Exception as e:
        logger.error(f"Bot failed to start: {str(e)}")
        raise
    finally:
        if 'client' in locals():
            await client.disconnect()
        mongo_client.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")
        time.sleep(3)
