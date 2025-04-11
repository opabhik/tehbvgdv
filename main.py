import os
import asyncio
import requests
import time
from urllib.parse import urlparse
from pymongo import MongoClient
from dotenv import load_dotenv
from telethon import TelegramClient, events
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import logging

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

async def connect_mongodb():
    try:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        # Verify connection
        client.server_info()
        db = client.get_database("telegram_bot")
        return db
    except Exception as e:
        logger.error(f"MongoDB connection error: {str(e)}")
        raise

async def main():
    try:
        logger.info("Starting Telegram bot...")
        
        # Connect to MongoDB
        db = await connect_mongodb()
        downloads_collection = db.downloads
        
        # Initialize Telegram client
        client = TelegramClient('bot_session', API_ID, API_HASH)
        await client.start(bot_token=BOT_TOKEN)
        logger.info("Bot started successfully")
        
        @client.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            await event.reply('Bot is working! Send me a TeraBox link.')
            
        @client.on(events.NewMessage())
        async def message_handler(event):
            if 'http' in event.text.lower():
                await event.reply("Link received! (This is a test response)")
            else:
                await event.reply("Please send a valid URL")
        
        logger.info("Bot is ready and listening...")
        await client.run_until_disconnected()
        
    except Exception as e:
        logger.error(f"Bot failed to start: {str(e)}")
        raise

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")
        time.sleep(5)  # Wait before exiting to see logs
