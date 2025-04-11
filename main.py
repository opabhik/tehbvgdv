import os
import requests
import time
import asyncio
from telegram import Update, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler
)
from urllib.parse import urlparse
from pymongo import MongoClient
from dotenv import load_dotenv
import logging
from aiohttp import web
import threading

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Load environment variables
load_dotenv()

# Configuration
TOKEN = os.getenv("TELEGRAM_TOKEN")
API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
MONGODB_URI = os.getenv("MONGODB_URI")
MAX_FILE_SIZE = 2000 * 1024 * 1024  # 2GB (Telegram client API limit)

# MongoDB connection
client = MongoClient(MONGODB_URI)
db = client.get_database("telegram_bot")
downloads_collection = db.downloads

async def health_check(request):
    return web.Response(text="OK")

def run_health_check():
    app = web.Application()
    app.router.add_get('/health', health_check)
    web.run_app(app, port=8000)

def start_health_check_server():
    thread = threading.Thread(target=run_health_check)
    thread.daemon = True
    thread.start()

async def log_download(user_id, url, filename, size):
    downloads_collection.insert_one({
        "user_id": user_id,
        "url": url,
        "filename": filename,
        "size_mb": size / (1024 * 1024),
        "timestamp": time.time(),
        "status": "completed"
    })

async def download_file_with_progress(url, filename, msg):
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
            if time.time() - last_update > 5:
                percent = (downloaded / total_size) * 100
                speed = downloaded / (1024 * 1024) / (time.time() - last_update)
                await msg.edit_text(
                    f"⬇️ Downloading: {filename}\n"
                    f"Progress: {downloaded//(1024*1024)}MB / {total_size//(1024*1024)}MB\n"
                    f"Speed: {speed:.1f} MB/s"
                )
                last_update = time.time()

    return total_size

async def upload_large_file(update, filename, caption):
    try:
        # Using client API for larger files
        await update.message.reply_document(
            document=InputFile(filename),
            caption=caption,
            filename=os.path.basename(filename),
            read_timeout=300,
            write_timeout=300,
            connect_timeout=300
        )
        return True
    except Exception as e:
        logging.error(f"Upload failed: {str(e)}")
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('Welcome! Send me a TeraBox link to download and upload to Telegram.')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    message_text = update.message.text
    user_id = update.message.from_user.id

    if 'http' not in message_text.lower():
        await update.message.reply_text("Please send a valid HTTP URL")
        return

    processing_msg = await update.message.reply_text('🔍 Processing your link...')

    try:
        # Get file info from API
        api_url = f"https://true12g.in/api/terabox.php?url={message_text}"
        response = requests.get(api_url, timeout=30)
        data = response.json()

        if not data.get('response'):
            await processing_msg.edit_text("❌ No valid data found in the API response.")
            return

        item = data['response'][0]
        title = item.get('title', 'Untitled')
        hd_url = item['resolutions'].get('HD Video', '')

        if not hd_url:
            await processing_msg.edit_text("❌ No download link found.")
            return

        # Check file size
        head_response = requests.head(hd_url)
        file_size = int(head_response.headers.get('content-length', 0))
        
        if file_size > MAX_FILE_SIZE:
            await processing_msg.edit_text(
                f"⚠️ File is too large ({file_size//(1024*1024)}MB). "
                f"Max supported size is {MAX_FILE_SIZE//(1024*1024)}MB."
            )
            return

        filename = os.path.basename(urlparse(hd_url).path) or f"file_{int(time.time())}.mp4"
        temp_filename = f"temp_{filename}"

        # Download file
        await processing_msg.edit_text(f"⬇️ Starting download: {filename}")
        try:
            file_size = await download_file_with_progress(hd_url, temp_filename, processing_msg)
            await log_download(user_id, hd_url, filename, file_size)
        except Exception as e:
            await processing_msg.edit_text(f"❌ Download failed: {str(e)}")
            return

        # Upload file
        await processing_msg.edit_text(f"📤 Starting upload: {filename}")
        upload_success = await upload_large_file(update, temp_filename, f"📁 {title}")

        if upload_success:
            await processing_msg.edit_text("✅ Upload complete!")
        else:
            await processing_msg.edit_text("❌ Upload failed. The file might be too large.")

    except Exception as e:
        await processing_msg.edit_text(f"❌ Error: {str(e)}")
        logging.error(f"Error in handle_message: {str(e)}")
    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)

def main() -> None:
    start_health_check_server()
    
    application = Application.builder() \
        .token(TOKEN) \
        .api_id(API_ID) \
        .api_hash(API_HASH) \
        .build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logging.info("🤖 Bot is running...")
    application.run_polling()

if __name__ == '__main__':
    main()
