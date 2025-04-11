from telethon import TelegramClient, events
import asyncio
import re
import requests
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

# Healthcheck server
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_dummy_server():
    server = HTTPServer(("0.0.0.0", 8000), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=start_dummy_server, daemon=True).start()

# Telegram credentials
api_id = 28241713
api_hash = '7ef0b559f6048c1396aa3d285665fbca'
client = TelegramClient('session', api_id, api_hash)

# Settings
CHANNEL_USERNAME = 'twsthsush'  # Channel username without @
JSON_URL = 'https://opabhik.serv00.net/track/data.json'  # JSON file URL
DELETE_API_URL = 'https://opabhik.serv00.net/track/delete.php?msg_id='  # Delete URL
ADMIN_ID = 1562465522  # Your Telegram user ID

@client.on(events.NewMessage)
async def handle_message(event):
    message = event.message.message.lower()
    if message == "hi":
        await event.reply("✅")

async def notify_admin(text):
    try:
        await client.send_message(ADMIN_ID, text)
    except Exception as e:
        print(f"Error sending message to admin: {e}")

async def check_posts():
    while True:
        try:
            print("Checking posts...")
            response = requests.get(JSON_URL)
            data = response.json()

            # Validate data
            if not isinstance(data, dict) or not data:
                print("No data found or data is invalid. Skipping this round.")
                await asyncio.sleep(35)
                continue

            batch_size = 30
            msg_ids = list(data.keys())

            for i in range(0, len(msg_ids), batch_size):
                batch = msg_ids[i:i + batch_size]
                posts = await client.get_messages(CHANNEL_USERNAME, ids=[int(mid) for mid in batch])

                for post in posts:
                    if post:
                        msg_id = str(post.id)
                        current_views = post.views
                        target_views = data.get(msg_id)

                        if target_views is None:
                            continue

                        try:
                            target_views = int(target_views)
                        except:
                            continue

                        if current_views > target_views:
                            try:
                                await client.delete_messages(CHANNEL_USERNAME, post.id)
                                requests.get(DELETE_API_URL + msg_id)
                                post_link = f"https://t.me/{CHANNEL_USERNAME}/{msg_id}"
                                await notify_admin(
                                    f"🗑 Deleted post: {post_link}\nMessage ID: {msg_id}\nTarget views: {target_views}\nActual views: {current_views}"
                                )
                                print(f"Deleted message {msg_id}")
                            except Exception as e:
                                await notify_admin(f"❌ Error deleting message ID {msg_id}: {str(e)}")
                                print(f"Error deleting message {msg_id}: {e}")

        except Exception as e:
            await notify_admin(f"❌ Error during check: {str(e)}")
            print("Error during check:", e)

        # Sleep based on quantity
        quantity = len(data)
        if quantity < 400:
            await asyncio.sleep(10)
        else:
            await asyncio.sleep(20)

async def main():
    await client.start()
    print("✅ Userbot started!")
    asyncio.create_task(check_posts())
    await client.run_until_disconnected()

asyncio.run(main())
