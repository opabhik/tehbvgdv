import os
import requests
import time
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# Load environment variables
load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")

# MongoDB connection
client = MongoClient(MONGODB_URI)
db = client.get_database("telegram_bot")
downloads_collection = db.downloads

async def log_download(user_id, url, filename, size):
    downloads_collection.insert_one({
        "user_id": user_id,
        "url": url,
        "filename": filename,
        "size_mb": size / (1024 * 1024),
        "timestamp": time.time()
    })

async def download_file_with_telegram_progress(url, filename, msg):
    response = requests.get(url, stream=True)
    response.raise_for_status()

    total_size = int(response.headers.get('content-length', 0))
    block_size = 1024 * 512  # 512KB chunks
    downloaded = 0
    last_percent = 0
    start_time = time.time()

    with open(filename, 'wb') as f:
        for data in response.iter_content(block_size):
            f.write(data)
            downloaded += len(data)

            percent = int((downloaded / total_size) * 100)
            elapsed_time = time.time() - start_time
            speed = downloaded / (1024 * 1024) / elapsed_time if elapsed_time > 0 else 0  # MB/s

            if percent != last_percent and percent % 5 == 0:
                progress_bar = f"{'█' * (percent // 10)}{' ' * (10 - (percent // 10))}"
                text = (
                    f"⬇️ Downloading: {filename}\n"
                    f"{percent}% |{progress_bar}| {downloaded//(1024*1024)}MB / {total_size//(1024*1024)}MB "
                    f"[{speed:.1f} MB/s]"
                )
                await msg.edit_text(text, parse_mode="Markdown")
                last_percent = percent

    return total_size

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('Welcome! Please send me a TeraBox link to process.')

async def retry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    original_message = query.message.reply_to_message
    if original_message:
        context.user_data['retry_count'] = context.user_data.get('retry_count', 0) + 1
        if context.user_data['retry_count'] > 3:
            await query.edit_message_text("❌ Maximum retry attempts reached. Please try again later.")
            return
        
        await query.edit_message_text(f"🔄 Retrying attempt {context.user_data['retry_count']}...")
        await handle_message(Update(message=original_message), context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message_text = update.message.text
    user_id = update.message.from_user.id

    if 'http' in message_text.lower():
        processing_msg = await update.message.reply_text('🔍 Processing your link...')

        try:
            api_url = f"https://true12g.in/api/terabox.php?url={message_text}"
            response = requests.get(api_url, timeout=10)
            data = response.json()

            if 'response' in data and len(data['response']) > 0:
                item = data['response'][0]
                title = item.get('title', 'No title')
                thumbnail = item.get('thumbnail', '')
                hd_url = item['resolutions'].get('HD Video', '')

                if thumbnail:
                    await update.message.reply_photo(photo=thumbnail, caption=f"📹 {title}")

                if hd_url:
                    filename = os.path.basename(urlparse(hd_url).path)
                    if not filename:
                        filename = f"video_{int(time.time())}.mp4"

                    print(f"\nStarting download: {hd_url}")

                    try:
                        # Download with Telegram progress
                        total_size = await download_file_with_telegram_progress(hd_url, filename, processing_msg)
                        file_size_mb = total_size / (1024 * 1024)

                        # Log to MongoDB
                        await log_download(user_id, hd_url, filename, total_size)

                        await processing_msg.edit_text(
                            f"📤 Uploading: {filename}\n0% |          | 0MB / {int(file_size_mb)}MB [0.0 MB/s]",
                            parse_mode="Markdown"
                        )

                        # Simulate upload progress
                        for i in range(0, 101, 10):
                            bar = f"{'█' * (i // 10)}{' ' * (10 - (i // 10))}"
                            await processing_msg.edit_text(
                                f"📤 Uploading: {filename}\n{i}% |{bar}| {int((i/100)*file_size_mb)}MB / {int(file_size_mb)}MB [~1.1 MB/s]",
                                parse_mode="Markdown"
                            )
                            await asyncio.sleep(0.5)

                        # Upload the video to Telegram
                        with open(filename, 'rb') as video_file:
                            await update.message.reply_video(
                                video=video_file,
                                caption=f"🎥 {title}",
                                supports_streaming=True
                            )

                        await processing_msg.edit_text("✅ Upload complete!")

                    except Exception as e:
                        await processing_msg.edit_text(f"❌ Error during upload: {str(e)}")
                        print(f"\nUpload error: {str(e)}")
                    finally:
                        if os.path.exists(filename):
                            os.remove(filename)

            else:
                await processing_msg.edit_text("❌ No valid data found in the API response.")

        except requests.exceptions.Timeout:
            keyboard = [
                [InlineKeyboardButton("🔄 Retry", callback_data="retry")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await processing_msg.edit_text(
                "⌛ The request timed out.",
                reply_markup=reply_markup
            )
        except Exception as e:
            await processing_msg.edit_text(f"❌ Error: {str(e)}")
    else:
        await update.message.reply_text("⚠️ Please send a valid URL starting with http or https.")

def main() -> None:
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(retry_callback, pattern="^retry$"))

    print("🤖 Bot is running... Press Ctrl+C to stop")
    application.run_polling()

if __name__ == '__main__':
    main()
