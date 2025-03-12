import os
import asyncio
import smtplib
import imaplib
import email
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.header import decode_header
import html2text
from telethon import TelegramClient, events, Button
import redis
from cryptography.fernet import Fernet, InvalidToken
import logging
from logging.handlers import RotatingFileHandler
import traceback
from datetime import datetime

# Configuration
API_ID = int(os.getenv('API_ID'))
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')
REDIS_URL = os.getenv('REDIS_URL')
ADMIN_IDS = [int(id) for id in os.getenv('ADMIN_IDS', '').split(',') if id]
MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024  # 20MB
LOG_FILE = 'email_bot.log'

bot = TelegramClient('email_bot', API_ID, API_HASH)
redis_client = redis.from_url(REDIS_URL)
h = html2text.HTML2Text()
h.ignore_links = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def setup_encryption():
    try:
        key = redis_client.get('fernet_key')
        if key:
            return Fernet(key)

        if os.getenv('ENCRYPTION_KEY'):
            return Fernet(os.getenv('ENCRYPTION_KEY').encode())

        new_key = Fernet.generate_key()
        redis_client.set('fernet_key', new_key)
        return Fernet(new_key)
    except Exception as e:
        logger.critical(f"Encryption setup failed: {str(e)}")
        raise

cipher_suite = setup_encryption()

# Redis Keys
def user_key(user_id):
    return f'user:{user_id}:email_config'

def temp_data_key(user_id):
    return f'temp:{user_id}'

def stats_key():
    return 'bot:stats'

# Encryption functions
def encrypt_data(data):
    try:
        return cipher_suite.encrypt(data.encode()).decode()
    except InvalidToken as e:
        logger.error(f"Encryption error: {str(e)}")
        raise

def decrypt_data(encrypted_data):
    try:
        return cipher_suite.decrypt(encrypted_data).decode()
    except InvalidToken as e:
        logger.error(f"Decryption error: {str(e)}")
        raise

async def handle_error(event, error_msg, user_msg=None):
    logger.error(f"{error_msg}\n{traceback.format_exc()}")
    if user_msg and event:
        await event.respond(f"❌ {user_msg}")

async def get_user_config(user_id):
    try:
        config = redis_client.hgetall(user_key(user_id))
        if not config:
            return None

        return {
            'smtp_server': config.get(b'smtp_server', b'').decode(),
            'smtp_port': int(config.get(b'smtp_port', 0)),
            'imap_server': config.get(b'imap_server', b'').decode(),
            'imap_port': int(config.get(b'imap_port', 0)),
            'email': decrypt_data(config.get(b'email', b'')) if config.get(b'email') else None,
            'password': decrypt_data(config.get(b'password', b'')) if config.get(b'password') else None,
            'mode': config.get(b'mode', b'').decode()
        }
    except Exception as e:
        await handle_error(None, f"Config error for {user_id}: {str(e)}")
        return None

async def save_temp_data(user_id, data):
    redis_client.hset(temp_data_key(user_id), mapping=data)

async def get_temp_data(user_id):
    return redis_client.hgetall(temp_data_key(user_id))

async def delete_temp_data(user_id):
    redis_client.delete(temp_data_key(user_id))

async def track_stat(metric):
    try:
        redis_client.incr(f'{stats_key()}:{metric}')
    except Exception as e:
        logger.error(f"Stat tracking error: {str(e)}")

# Email Functions
async def send_email(user_id, to, subject, body, attachments=None):
    config = await get_user_config(user_id)
    if not config or not config.get('smtp_server'):
        raise Exception("SMTP not configured")

    msg = MIMEMultipart()
    msg['Subject'] = subject
    msg['From'] = config['email']
    msg['To'] = to

    msg.attach(MIMEText(body, 'plain', 'utf-8'))

    if attachments:
        for file_info in attachments:
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(file_info['content'])
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename="{file_info["filename"]}"')
            msg.attach(part)

    with smtplib.SMTP(config['smtp_server'], config['smtp_port']) as server:
        server.starttls()
        server.login(config['email'], config['password'])
        server.send_message(msg)
    await track_stat('emails_sent')

@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    await track_stat('users_active')
    user = await event.get_sender()
    buttons = [
        [Button.inline('Configure Email', b'configure'),
         Button.inline('Send Email', b'send_email')],
        [Button.inline('Check Inbox', b'check_inbox'),
         Button.inline('Help', b'help')]
    ]
    await event.respond(
        f"Hi {user.first_name}! Welcome to Email Bot\n\n"
        "Features:\n"
        "• Send/Receive emails\n"
        "• Secure credential storage\n"
        "• Multiple email providers support\n"
        "• Afraid to log in? No worries, this bot is open source!",
        buttons=buttons
    )

@bot.on(events.NewMessage(pattern='/logs'))
async def logs_handler(event):
    try:
        if event.sender_id not in ADMIN_IDS:
            await event.respond("⛔ Unauthorized")
            return

        with open(LOG_FILE, 'rb') as f:
            await event.respond(file=f)

        logger.info(f"Admin {event.sender_id} downloaded logs")
    except Exception as e:
        await handle_error(event, f"Logs error: {str(e)}")

@bot.on(events.NewMessage(pattern='/users'))
async def users_handler(event):
    try:
        if event.sender_id not in ADMIN_IDS:
            await event.respond("⛔ Unauthorized")
            return

        users = await asyncio.get_event_loop().run_in_executor(None, redis_client.keys, 'user:*:email_config')
        stats = {
            'total_users': len(users),
            'active_today': redis_client.get(f'{stats_key()}:users_active') or 0,
            'emails_sent': redis_client.get(f'{stats_key()}:emails_sent') or 0
        }

        message = (
            "📊 Bot Statistics:\n\n"
            f"• Total Users: {stats['total_users']}\n"
            f"• Emails Sent: {stats['emails_sent']}\n"
            f"• Active Today: {stats['active_today']}"
        )
        await event.respond(message)
    except Exception as e:
        await handle_error(event, f"Users stats error: {str(e)}")

# Start bot
if __name__ == '__main__':
    try:
        logger.info("Starting Email Bot...")
        bot.start(bot_token=BOT_TOKEN)
        asyncio.get_event_loop().run_forever()
    except Exception as e:
        logger.critical(f"Bot crashed: {str(e)}\n{traceback.format_exc()}")
