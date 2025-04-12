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

# UI Elements
PROGRESS_BAR_LENGTH = 20
PROGRESS_FILLED = "⬢"
PROGRESS_EMPTY = "⬡"

def format_size(size):
    """Convert bytes to human-readable format"""
    if size < 1024*1024:
        return f"{size/1024:.1f} KB"
    return f"{size/(1024*1024):.1f} MB"

def format_eta(seconds):
    """Convert seconds to human-readable ETA"""
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {int(seconds)}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h {int(minutes)}m"

def create_progress_bar(percent):
    """Create visual progress bar"""
    filled = PROGRESS_FILLED * int(percent/100 * PROGRESS_BAR_LENGTH)
    empty = PROGRESS_EMPTY * (PROGRESS_BAR_LENGTH - len(filled))
    return f"{filled}{empty}"

async def download_with_progress(url, filename, progress_callback):
    """Download file with real-time progress updates"""
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        start_time = time.time()
        
        with open(filename, 'wb') as file:
            for chunk in response.iter_content(chunk_size=1024*1024):  # 1MB chunks
                if chunk:
                    file.write(chunk)
                    downloaded += len(chunk)
                    
                    # Calculate metrics
                    elapsed = time.time() - start_time
                    speed = (downloaded / (1024*1024)) / elapsed if elapsed > 0 else 0
                    eta = (total_size - downloaded) / (downloaded / elapsed) if downloaded > 0 else 0
                    
                    # Update progress
                    await progress_callback(downloaded, total_size, speed, eta)
    
    return total_size

async def process_download(event):
    """Handle download process from start to finish"""
    url = event.text.strip()
    user_id = event.sender_id
    temp_filename = None
    progress_msg = None
    
    try:
        # Fetch file info
        api_response = requests.get(f"https://true12g.in/api/terabox.php?url={url}").json()
        if not api_response.get('response'):
            await event.reply("❌ Invalid or expired link")
            return

        file_info = api_response['response'][0]
        download_url = file_info['resolutions'].get('HD Video', '')
        thumbnail = file_info.get('thumbnail', '')
        title = file_info.get('title', f"video_{int(time.time())}")
        
        # Determine file extension
        content_type = requests.head(download_url).headers.get('content-type', '')
        ext = mimetypes.guess_extension(content_type) or '.mp4'
        filename = f"{title[:50]}{ext}"
        temp_filename = f"temp_{filename}"
        
        # Send initial progress message with thumbnail
        progress_msg = await event.client.send_file(
            event.chat_id,
            thumbnail,
            caption="🔄 Preparing download...",
            parse_mode='markdown'
        )
        
        # Progress update handler
        async def update_progress(downloaded, total, speed, eta):
            percent = (downloaded / total) * 100 if total > 0 else 0
            progress_bar = create_progress_bar(percent)
            
            caption = (
                f"🎬 **{title}**\n\n"
                f"⬇️ Downloading: `{filename}`\n"
                f"{progress_bar} {percent:.1f}%\n"
                f"⚡ {speed:.1f} MB/s • ⏳ {format_eta(eta)}\n"
                f"📦 {format_size(downloaded)} / {format_size(total)}"
            )
            
            try:
                await progress_msg.edit(caption, parse_mode='markdown')
            except Exception as e:
                logger.error(f"Progress update failed: {e}")

        # Download file
        file_size = await download_with_progress(
            download_url,
            temp_filename,
            update_progress
        )
        
        # Prepare for upload
        await progress_msg.edit("📤 Preparing for ultra-fast upload...")
        
        # Upload with optimized settings
        upload_start = time.time()
        await event.client.send_file(
            event.chat_id,
            temp_filename,
            caption=(
                f"✅ **{title}**\n\n"
                f"📁 Size: {format_size(file_size)}\n"
                f"⏱️ Uploaded in {timedelta(seconds=int(time.time() - upload_start))}\n"
                f"🎥 Streamable: Yes"
            ),
            supports_streaming=True,
            attributes=[
                types.DocumentAttributeFilename(filename),
                types.DocumentAttributeVideo(
                    duration=0,
                    w=0,
                    h=0,
                    supports_streaming=True
                )
            ],
            part_size=1024*1024*10,  # 10MB chunks
            workers=8,               # Parallel uploads
            force_document=False
        )
        
    except Exception as e:
        error_msg = f"❌ Error processing your request:\n`{str(e)}`"
        if progress_msg:
            await progress_msg.edit(error_msg, parse_mode='markdown')
        else:
            await event.reply(error_msg, parse_mode='markdown')
        logger.error(f"Download failed: {e}")
        
    finally:
        # Cleanup
        if temp_filename and os.path.exists(temp_filename):
            os.remove(temp_filename)
        if progress_msg:
            try:
                await progress_msg.delete()
            except:
                pass

async def main():
    """Main bot setup"""
    client = TelegramClient(
        'koyeb_bot',
        API_ID,
        API_HASH,
        system_version="UltraFast/1.0",
        device_model="Koyeb Server"
    )
    
    await client.start(bot_token=BOT_TOKEN)
    logger.info("Bot started successfully")
    
    # Command handlers
    @client.on(events.NewMessage(pattern='/start'))
    async def start_handler(event):
        await event.reply(
            "🚀 **TeraBox Turbo Downloader**\n\n"
            "Send me any TeraBox link to get instant streaming-ready videos!\n\n"
            "⚡ Features:\n"
            "- Ultra-fast downloads\n"
            "- Real-time progress\n"
            "- HD quality\n"
            "- Instant playback",
            parse_mode='markdown'
        )
    
    # Main message handler
    @client.on(events.NewMessage())
    async def message_handler(event):
        if 'terabox' in event.text.lower():
            await process_download(event)
    
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
