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
import json

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

async def download_file(url, filename, progress_callback=None):
    response = requests.get(url, stream=True)
    response.raise_for_status()

    total_size = int(response.headers.get('content-length', 0))
    block_size = 1024 * 1024  # 1MB chunks
    downloaded = 0
    last_update = time.time()

    with open(filename, 'wb') as f:
        for data in response.iter_content(block_size):
            f.write(data)
            downloaded += len(data)
            
            # Update progress every 5 seconds
            if time.time() - last_update > 5 and progress_callback:
                percent = (downloaded / total_size) * 100
                speed = downloaded / (1024 * 1024) / (time.time() - last_update)
                await progress_callback(filename, downloaded, total_size, speed)
                last_update = time.time()

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

async def process_download(event, url):
    user_id = event.sender_id
    message = await event.reply('🔍 Processing your link...')
    temp_filename = None

    try:
        # Get file info
        file_info = await get_terabox_info(url)
        if not file_info or not file_info['hd_url']:
            await message.edit("❌ Could not get download link")
            return

        title = file_info['title']
        hd_url = file_info['hd_url']
        
        # Check file size
        head_response = requests.head(hd_url)
        file_size = int(head_response.headers.get('content-length', 0))
        
        MAX_FILE_SIZE = 2000 * 1024 * 1024  # 2GB
        if file_size > MAX_FILE_SIZE:
            await message.edit(f"⚠️ File too large ({file_size//(1024*1024)}MB > {MAX_FILE_SIZE//(1024*1024)}MB)")
            return

        filename = os.path.basename(urlparse(hd_url).path) or f"file_{int(time.time())}.mp4"
        temp_filename = f"temp_{filename}"

        # Progress callback
        async def progress_callback(filename, downloaded, total, speed):
            await message.edit(
                f"⬇️ Downloading: {filename}\n"
                f"Progress: {downloaded//(1024*1024)}MB / {total//(1024*1024)}MB\n"
                f"Speed: {speed:.1f} MB/s"
            )

        # Download file
        await log_download(user_id, url, filename, file_size, 'downloading')
        await message.edit(f"⬇️ Starting download: {filename}")
        
        file_size = await download_file(hd_url, temp_filename, progress_callback)
        await log_download(user_id, url, filename, file_size, 'downloaded')

        # Upload file
        await message.edit(f"📤 Starting upload: {filename}")
        
        # Upload progress callback
        def upload_progress_callback(current, total):
            asyncio.create_task(
                message.edit(
                    f"📤 Uploading: {filename}\n"
                    f"Progress: {current//(1024*1024)}MB / {total//(1024*1024)}MB"
                )
            )

        # Send file with progress
        await event.client.send_file(
            event.chat_id,
            temp_filename,
            caption=f"📁 {title}",
            progress_callback=upload_progress_callback,
            attributes=[
                types.DocumentAttributeFilename(filename),
                types.DocumentAttributeVideo(
                    duration=0,
                    w=0,
                    h=0,
                    supports_streaming=True
                )
            ]
        )
        
        await message.edit("✅ Upload complete!")
        await log_download(user_id, url, filename, file_size, 'uploaded')

    except Exception as e:
        error_msg = f"❌ Error: {str(e)}"
        await message.edit(error_msg)
        logger.error(error_msg)
        if temp_filename:
            await log_download(user_id, url, temp_filename, 0, f'failed: {str(e)}')
    finally:
        if temp_filename and os.path.exists(temp_filename):
            os.remove(temp_filename)

async def main():
    try:
        logger.info("Starting Telegram bot...")
        
        # Initialize Telegram client
        client = TelegramClient('bot_session', API_ID, API_HASH)
        await client.start(bot_token=BOT_TOKEN)
        logger.info("Bot started successfully")
        
        @client.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            await event.reply('Welcome! Send me a TeraBox link to download and upload.')
            
        @client.on(events.NewMessage(pattern='/stats'))
        async def stats_handler(event):
            user_id = event.sender_id
            count = downloads_collection.count_documents({"user_id": user_id})
            await event.reply(f"You've downloaded {count} files so far.")
            
        @client.on(events.NewMessage())
        async def message_handler(event):
            if 'http' in event.text.lower():
                await process_download(event, event.text)
            else:
                await event.reply("Please send a valid TeraBox URL starting with http")
        
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
        time.sleep(5)  # Wait before exiting to see logs
