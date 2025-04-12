#!/usr/bin/env python3 import os import asyncio import requests import time import mimetypes from datetime import timedelta from pymongo import MongoClient from dotenv import load_dotenv from telethon import TelegramClient, events, types from fastapi import FastAPI import uvicorn import logging import threading

Fix imghdr removal in Python 3.13+

try: import imghdr except ImportError: import filetype as imghdr

Configure logging

logging.basicConfig( format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO ) logger = logging.getLogger(name)

Load environment variables

load_dotenv()

Config

API_ID = int(os.getenv("TELEGRAM_API_ID")) API_HASH = os.getenv("TELEGRAM_API_HASH") BOT_TOKEN = os.getenv("TELEGRAM_TOKEN") MONGODB_URI = os.getenv("MONGODB_URI") ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(',') if x]

MongoDB

mongo_client = MongoClient(MONGODB_URI) db = mongo_client.get_database("telegram_bot") downloads_collection = db.downloads

FastAPI for health check

app = FastAPI()

@app.get("/") def health_check(): return {"status": "ok"}

def run_health_server(): uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")

async def download_file(url, filename, progress_callback=None): with requests.get(url, stream=True, timeout=60) as r: r.raise_for_status() total_size = int(r.headers.get('content-length', 0)) downloaded = 0 start_time = time.time()

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

async def process_download(event): url = event.text.strip()

try:
    api_url = f"https://true12g.in/api/terabox.php?url={url}"
    data = requests.get(api_url).json()

    if not data.get('response'):
        await event.reply("❌ Could not fetch download link.")
        return

    file_info = data['response'][0]
    hd_url = file_info['resolutions'].get('HD Video', '')
    thumbnail = file_info.get('thumbnail', '')
    title = file_info.get('title', 'video_' + str(int(time.time())))

    progress_msg = await event.client.send_file(
        event.chat_id,
        thumbnail,
        caption="🔄 Starting download...",
        parse_mode='markdown'
    )

    ext = mimetypes.guess_extension(requests.head(hd_url).headers.get('content-type', '')) or '.mp4'
    filename = f"{title[:50]}{ext}"
    temp_filename = f"temp_{filename}"

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

    file_size = await download_file(hd_url, temp_filename, update_progress)

    upload_start = time.time()
    await progress_msg.edit("📤 Uploading to Telegram...")

    with open(temp_filename, "rb") as file:
        await event.client.send_file(
            event.chat_id,
            file,
            caption=f"✅ Upload complete!\nSize: {file_size/(1024*1024):.1f}MB\nTime: {timedelta(seconds=int(time.time() - upload_start))}",
            part_size=1024*1024*10,
            workers=8,
            force_document=False
        )

    os.remove(temp_filename)
    await progress_msg.delete()

except Exception as e:
    await event.reply(f"❌ Error: {str(e)}")
    logger.error(f"Download failed: {e}")

async def main(): threading.Thread(target=run_health_server).start()

client = TelegramClient('koyeb_bot', API_ID, API_HASH)
await client.start(bot_token=BOT_TOKEN)

@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    await event.reply("🚀 Send me a TeraBox link!", parse_mode='markdown')

@client.on(events.NewMessage(pattern='/restart'))
async def restart_bot(event):
    if event.sender_id in ADMIN_IDS:
        await event.reply("♻️ Restarting bot...")
        os._exit(1)

@client.on(events.NewMessage(pattern='/stats'))
async def stats(event):
    count = downloads_collection.count_documents({})
    await event.reply(f"📊 Total downloads tracked: `{count}`", parse_mode='markdown')

@client.on(events.NewMessage(pattern='/broadcast'))
async def broadcast(event):
    if event.sender_id in ADMIN_IDS:
        msg = event.text.split(None, 1)
        if len(msg) < 2:
            return await event.reply("Usage: /broadcast Your message")
        message = msg[1]
        async for user in client.iter_participants(event.chat_id):
            try:
                await client.send_message(user.id, message)
            except:
                continue
        await event.reply("✅ Broadcast sent.")

@client.on(events.NewMessage())
async def handler(event):
    if 'terabox' in event.text.lower():
        await process_download(event)

logger.info("Bot started successfully")
await client.run_until_disconnected()

if name == 'main': asyncio.run(main())

