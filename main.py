#!/usr/bin/env python3
import os
import asyncio
import requests
import time
from pymongo import MongoClient
from dotenv import load_dotenv
from telethon import TelegramClient, events, types
import logging
import mimetypes
from datetime import timedelta

# Fix imghdr removal in Python 3.13+
try:
    import imghdr
except ImportError:
    import filetype as imghdr

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Config
API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")

# MongoDB
mongo_client = MongoClient(MONGODB_URI)
db = mongo_client.get_database("telegram_bot")
downloads_collection = db.downloads

# ... rest of your code is unchanged ...

# MongoDB
mongo_client = MongoClient(MONGODB_URI)
db = mongo_client.get_database("telegram_bot")
downloads_collection = db.downloads

async def download_file(url, filename, progress_callback=None):
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total_size = int(r.headers.get('content-length', 0))
        downloaded = 0
        start_time = time.time()
        
        with open(filename, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024*1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    
                    elapsed = time.time() - start_time
                    speed = (downloaded / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                    eta = (total_size - downloaded) / (downloaded / elapsed) if downloaded > 0 else 0
                    
                    if progress_callback:
                        await progress_callback(downloaded, total_size, speed, eta)
    
    return total_size

async def process_download(event):
    url = event.text.strip()
    
    try:
        # Get file info
        api_url = f"https://true12g.in/api/terabox.php?url={url}"
        data = requests.get(api_url).json()
        
        if not data.get('response'):
            await event.reply("❌ Could not fetch download link.")
            return
        
        file_info = data['response'][0]
        hd_url = file_info['resolutions'].get('HD Video', '')
        thumbnail = file_info.get('thumbnail', '')
        title = file_info.get('title', 'video_' + str(int(time.time())))
        
        # Send thumbnail as photo
        progress_msg = await event.client.send_file(
            event.chat_id,
            thumbnail,
            caption="🔄 Starting download...",
            parse_mode='markdown'
        )
        
        # Determine filename
        ext = mimetypes.guess_extension(requests.head(hd_url).headers.get('content-type', '')) or '.mp4'
        filename = f"{title[:50]}{ext}"
        temp_filename = f"temp_{filename}"
        
        # Progress callback
        async def update_progress(downloaded, total, speed, eta):
            percent = (downloaded / total) * 100 if total > 0 else 0
            progress_bar = "⬢" * int(percent / 5) + "⬡" * (20 - int(percent / 5))
            
            caption = (
                f"⬇️ Downloading: `{filename}`\n"
                f"{progress_bar} {percent:.1f}%\n"
                f"⚡ {speed:.1f} MB/s • ⏳ {eta:.0f}s\n"
                f"📦 {downloaded/(1024*1024):.1f}MB / {total/(1024*1024):.1f}MB"
            )
            
            try:
                await progress_msg.edit(caption, parse_mode='markdown')
            except Exception as e:
                logger.error(f"Progress update failed: {e}")
        
        # Download with progress
        file_size = await download_file(hd_url, temp_filename, update_progress)
        
        # Upload with optimized settings
        upload_start = time.time()
        await progress_msg.edit("📤 Uploading to Telegram...")
        
        await event.client.send_file(
            event.chat_id,
            temp_filename,
            caption=f"✅ Upload complete!\nSize: {file_size/(1024*1024):.1f}MB\nTime: {timedelta(seconds=int(time.time() - upload_start))}",
            part_size=1024*1024*10,
            workers=8,
            force_document=False
        )
        
        # Cleanup
        os.remove(temp_filename)
        await progress_msg.delete()
        
    except Exception as e:
        await event.reply(f"❌ Error: {str(e)}")
        logger.error(f"Download failed: {e}")

async def main():
    client = TelegramClient('koyeb_bot', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    
    @client.on(events.NewMessage(pattern='/start'))
    async def start(event):
        await event.reply("🚀 Send me a TeraBox link!", parse_mode='markdown')
    
    @client.on(events.NewMessage())
    async def handler(event):
        if 'terabox' in event.text.lower():
            await process_download(event)
    
    logger.info("Bot started successfully")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
