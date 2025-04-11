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

# Progress tracking
def format_size(size):
    return f"{size / (1024 * 1024):.1f} MB"

async def update_progress_message(event, msg, file_info, downloaded, total, speed, eta):
    percent = (downloaded / total) * 100 if total > 0 else 0
    progress_bar = "⬢" * int(percent / 5) + "⬡" * (20 - int(percent / 5))
    
    caption = (
        f"⬇️ **Downloading:** `{file_info['filename']}`\n"
        f"📦 **Progress:** `{progress_bar} {percent:.1f}%`\n"
        f"⚡ **Speed:** `{speed:.1f} MB/s`\n"
        f"⏳ **ETA:** `{format_eta(eta)}`\n\n"
        f"🔄 Processing..."
    )
    
    try:
        await msg.edit(caption, parse_mode='markdown')
    except Exception as e:
        logger.error(f"Failed to update progress: {e}")

def format_eta(seconds):
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds/60)}m {int(seconds%60)}s"
    else:
        return f"{int(seconds/3600)}h {int((seconds%3600)/60)}m"

# Download function
async def download_file(url, filename):
    response = requests.get(url, stream=True, timeout=30)
    response.raise_for_status()
    
    total_size = int(response.headers.get('content-length', 0))
    downloaded = 0
    start_time = time.time()
    
    with open(filename, 'wb') as f:
        for chunk in response.iter_content(1024 * 1024):  # 1MB chunks
            f.write(chunk)
            downloaded += len(chunk)
            
            # Calculate speed & ETA
            elapsed = time.time() - start_time
            speed = (downloaded / (1024 * 1024)) / elapsed if elapsed > 0 else 0
            eta = (total_size - downloaded) / (downloaded / elapsed) if downloaded > 0 else 0
            
            yield downloaded, total_size, speed, eta

# Main download handler
async def process_download(event):
    url = event.text.strip()
    
    try:
        # Get file info from Terabox
        api_url = f"https://true12g.in/api/terabox.php?url={url}"
        data = requests.get(api_url).json()
        
        if not data.get('response'):
            await event.reply("❌ Could not fetch download link.")
            return
        
        file_info = data['response'][0]
        hd_url = file_info['resolutions'].get('HD Video', '')
        thumbnail = file_info.get('thumbnail', '')
        title = file_info.get('title', 'video_' + str(int(time.time())))
        
        # Send thumbnail as photo with caption
        progress_msg = await event.reply(
            "🔄 **Starting download...**",
            file=thumbnail,
            parse_mode='markdown'
        )
        
        # Determine filename
        ext = mimetypes.guess_extension(requests.head(hd_url).headers.get('content-type', '')) or '.mp4'
        filename = f"{title[:50]}{ext}"
        
        # Download with progress
        async for downloaded, total, speed, eta in download_file(hd_url, filename):
            await update_progress_message(
                event, progress_msg, 
                {'filename': filename},
                downloaded, total, speed, eta
            )
        
        # Upload the file
        upload_start = time.time()
        await progress_msg.edit("📤 **Uploading to Telegram...**")
        
        await event.client.send_file(
            event.chat_id,
            filename,
            caption=f"✅ **Upload Complete!**\n\n"
                   f"📁 **File:** `{filename}`\n"
                   f"📦 **Size:** `{format_size(os.path.getsize(filename))}`\n"
                   f"⏱️ **Time Taken:** `{timedelta(seconds=int(time.time() - upload_start))}`",
            supports_streaming=True,
            parse_mode='markdown'
        )
        
        # Cleanup
        os.remove(filename)
        await progress_msg.delete()
        
    except Exception as e:
        await event.reply(f"❌ **Error:** `{str(e)}`")
        logger.error(f"Download failed: {e}")

# Bot setup
async def main():
    client = TelegramClient('bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)
    
    @client.on(events.NewMessage(pattern='/start'))
    async def start(event):
        await event.reply(
            "🚀 **TeraBox Downloader**\n\n"
            "Send a TeraBox link to download & stream.",
            parse_mode='markdown'
        )
    
    @client.on(events.NewMessage())
    async def handle_message(event):
        if 'terabox' in event.text.lower():
            await process_download(event)
    
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
