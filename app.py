import os
import re
import logging
import telebot
from telebot.types import InlineQueryResultCachedDocument, InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask, request, render_template_string, jsonify, send_from_directory, url_for, Response
from pymongo import MongoClient
from pymongo.errors import PyMongoError
import uuid
from bson.objectid import ObjectId
from datetime import datetime, timezone
from html import escape
from urllib.parse import urlparse
from urllib.request import urlopen
# Environment Variables (Set these in Heroku Settings -> Config Vars)
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))
ADMIN_GROUP_ID = int(os.environ.get('ADMIN_GROUP_ID', 0))
BACKUP_GROUP_ID = int(os.environ.get('BACKUP_GROUP_ID', 0))
OTHERS_GROUP_ID = int(os.environ.get('OTHERS_GROUP_ID', -123456789))
MONGO_URI = os.environ.get('MONGO_URI')
URL = os.environ.get('HEROKU_APP_URL')
FORCE_CHANNEL_ID = os.environ.get('FORCE_CHANNEL_ID')  # e.g., "-100123456789"
FORCE_GROUP_ID = os.environ.get('FORCE_GROUP_ID')      # e.g., "-100987654321"
FORCE_CHANNEL_URL = os.environ.get('FORCE_CHANNEL_URL')
FORCE_GROUP_URL = os.environ.get('FORCE_GROUP_URL')
ADMIN_CHANNEL_ID = os.environ.get('ADMIN_CHANNEL_ID')
DISCUSSION_2025_MSG_ID = os.environ.get('DISCUSSION_2025_MSG_ID', '')
DISCUSSION_2026_MSG_ID = os.environ.get('DISCUSSION_2026_MSG_ID', '')

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Database Setup
client = MongoClient(MONGO_URI)
db = client['telegram_bot']
users_col = db['users']
files_col = db['files']
history_col = db['history']
messages_col = db['messages']
admins_col = db['admins']
tutor_buttons_col = db['tutor_buttons']

DEFAULT_TUTOR_BUTTONS = [
    {"name": "Anuradha Perera", "search_tag": "ap", "image_url": "/static/ap.jpg"},
    {"name": "Amila Dasanayaka", "search_tag": "ad", "image_url": "/static/ad.jpg"},
    {"name": "Sashanka Danujaya", "search_tag": "sd", "image_url": "/static/sd.jpg"},
]


# ================= QUERY HELPERS =================

def normalize_query(q):
    """Pad lone single digits (1-9) with a leading zero so that
    'full paper 1' matches 'full paper 01', 'mcq 3' matches 'mcq 03', etc.
    Multi-digit numbers like '10', '01', or '2022' are left unchanged."""
    return re.sub(r'\b([1-9])\b', r'0\1', q)


def is_admin_or_subadmin(user_id):
    return user_id == ADMIN_ID or admins_col.count_documents({"user_id": user_id}, limit=1) > 0


def build_miniapp_markup():
    if not URL:
        return None
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📚 Open PaperBot App", web_app=telebot.types.WebAppInfo(url=f"{URL}/miniapp")))
    return markup


def tutor_search_tag_from_name(name):
    words = [w for w in re.split(r'\s+', name.strip().lower()) if w]
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    return ''.join(w[0] for w in words[:3])


def tutor_key(name):
    return re.sub(r'\s+', ' ', name.strip().lower())


def get_tutor_buttons():
    tutors = []
    try:
        for t in tutor_buttons_col.find({}, {"_id": 1, "name": 1, "search_tag": 1, "image_file_id": 1, "image_url": 1}).sort("name", 1):
            image_url = t.get("image_url") or ""
            if not image_url and t.get("image_file_id"):
                image_url = f"/api/tutor-image/{str(t['_id'])}"
            tutors.append({
                "id": str(t["_id"]),
                "name": t.get("name", ""),
                "search_tag": t.get("search_tag", ""),
                "image_url": image_url
            })
    except Exception as e:
        logging.error("Failed to load tutor buttons: %s", e)
    return tutors or DEFAULT_TUTOR_BUTTONS


def _extract_msg_id_from_token(token):
    cleaned = token.strip()
    if not cleaned:
        return None
    if cleaned.lstrip('-').isdigit():
        return int(cleaned)
    try:
        parsed = urlparse(cleaned)
    except Exception:
        return None
    path_parts = [p for p in parsed.path.split('/') if p]
    if path_parts and path_parts[-1].isdigit():
        return int(path_parts[-1])
    return None


def send_discussion_messages(target_chat_id, year):
    refs_by_year = {"2025": DISCUSSION_2025_MSG_ID, "2026": DISCUSSION_2026_MSG_ID}
    raw_refs = refs_by_year.get(str(year), "")
    tokens = [t for t in re.split(r'[\s,]+', raw_refs.strip()) if t]
    if not tokens:
        return False, "⚠️ Discussion messages are not configured for this year yet."

    sent_any = False
    admin_channel = int(ADMIN_CHANNEL_ID) if ADMIN_CHANNEL_ID and str(ADMIN_CHANNEL_ID).lstrip('-').isdigit() else None
    for token in tokens:
        msg_id = _extract_msg_id_from_token(token)
        if msg_id is not None and admin_channel:
            try:
                bot.forward_message(target_chat_id, admin_channel, msg_id)
                sent_any = True
                continue
            except Exception as e:
                logging.error("Failed forwarding discussion message %s for %s: %s", msg_id, year, e)
        if token.startswith("http://") or token.startswith("https://"):
            bot.send_message(target_chat_id, token)
            sent_any = True

    if sent_any:
        return True, ""
    return False, "⚠️ Failed to send discussion materials. Please contact admin."


def send_discussion_year_buttons(chat_id, reply_to_message_id=None):
    markup = InlineKeyboardMarkup()
    markup.row_width = 2
    markup.add(
        InlineKeyboardButton("2025", callback_data="discussion_year:2025"),
        InlineKeyboardButton("2026", callback_data="discussion_year:2026")
    )
    bot.send_message(chat_id, "Please choose a discussion year:", reply_markup=markup, reply_to_message_id=reply_to_message_id)


# ================= FORCE SUBSCRIBE HELPERS =================

def get_subscription_status(user_id):
    if user_id == ADMIN_ID or admins_col.find_one({"user_id": user_id}):
        return {"channel": True, "group": True}
    if FORCE_CHANNEL_ID:
        try:
            status = bot.get_chat_member(int(FORCE_CHANNEL_ID), user_id).status
            channel_ok = status in ['member', 'administrator', 'creator']
        except Exception:
            channel_ok = False
    else:
        channel_ok = True
    if FORCE_GROUP_ID:
        try:
            status = bot.get_chat_member(int(FORCE_GROUP_ID), user_id).status
            group_ok = status in ['member', 'administrator', 'creator']
        except Exception:
            group_ok = False
    else:
        group_ok = True
    return {"channel": channel_ok, "group": group_ok}


def enforce_subscription(message):
    status = get_subscription_status(message.from_user.id)
    if status["channel"] and status["group"]:
        return True
    markup = InlineKeyboardMarkup()
    if not status["channel"] and FORCE_CHANNEL_URL:
        markup.add(InlineKeyboardButton("Join Our Channel", url=FORCE_CHANNEL_URL))
    if not status["group"] and FORCE_GROUP_URL:
        markup.add(InlineKeyboardButton("Join Our Group", url=FORCE_GROUP_URL))
    markup.add(InlineKeyboardButton("✅ Verify", callback_data="verify_sub"))
    bot.send_message(
        message.chat.id,
        "⚠️ You must join our required channel and group to use this bot.",
        reply_markup=markup
    )
    return False

# ================= TELEGRAM BOT LOGIC =================

@bot.message_handler(commands=['start'])
def start(message):
    if message.chat.type != 'private':
        return
    if not enforce_subscription(message):
        return
    user_id = message.from_user.id
    if not users_col.find_one({"user_id": user_id}):
        users_col.insert_one({"user_id": user_id, "username": message.from_user.username})
    first_name = message.from_user.first_name or "there"
    welcome_text = (
        f"👋 Hello {first_name},\n\n"
        "Welcome to Learn-X PaperBot 📚✨\n\n"
        "You can search for past papers and study materials by typing the subject, paper name, or teacher (e.g. AD S2 Paper 01).\n"
        "Simply type a keyword here or use our mini app to find what you need quickly ⚡️\n\n"
        "If you need assistance or would like to request a paper, simply use the /contact command followed by your message 💬\n\n"
        "🔐 To get full access, please complete the verification using the buttons below.\n\n"
        "Go ahead and start your search below 👇"
    )
    bot.reply_to(message, welcome_text)

@bot.message_handler(commands=['help'])
def help_command(message):
    if message.chat.type != 'private':
        return
    if not enforce_subscription(message):
        return
    help_text = (
        "📚 *PaperBot Help Guide*\n\n"
        "Here is how you can use this bot:\n\n"
        "🔍 *Searching for Papers:*\n"
        "Simply type the name of the subject, year, or keyword and send it to me as a normal message.\n"
        "*Example:* `biology` or `2023 physics`\n"
        "I will instantly search the database and send you the matching files!\n\n"
        "📩 *Contacting the Admin:*\n"
        "If you need help, have a specific request, or found an issue, you can send a message directly to the admins using the `/contact` command.\n"
        "Just type `/contact` followed by your message.\n"
        "*Example:* `/contact Hello, could you please upload the 2022 Chemistry paper?`\n\n"
        "Just type your search keyword below to get started!"
    )
    bot.reply_to(message, help_text, parse_mode='Markdown')

@bot.message_handler(commands=['app'])
def open_app(message):
    if message.chat.type != 'private':
        return
    if not enforce_subscription(message):
        return
    if not URL:
        bot.reply_to(message, "⚠️ Mini App URL is not configured.")
        return
    markup = build_miniapp_markup()
    bot.send_message(
        message.chat.id,
        "🎓 *PaperBot Mini App*\n\nSearch and download past papers directly from the app!",
        reply_markup=markup,
        parse_mode='Markdown'
    )

@bot.message_handler(commands=['contact'])
def contact(message):
    if message.chat.type != 'private':
        return
    if not enforce_subscription(message):
        return
    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "Usage: /contact <your message>")
        return

    user_message = parts[1].strip()
    user = message.from_user
    first_name = user.first_name or ""
    last_name = user.last_name or ""
    full_name = (first_name + " " + last_name).strip() or user.username or str(user.id)

    group_text = (
        f"📩 New Contact Message\n"
        f"User ID: {user.id}\n"
        f"Name: {full_name}\n"
        f"Message: {user_message}"
    )

    if ADMIN_GROUP_ID:
        try:
            bot.send_message(ADMIN_GROUP_ID, group_text)
            bot.reply_to(message, "Your message has been sent to the admins")
        except Exception:
            bot.reply_to(message, "Failed to send your message. Please try again later.")
    else:
        bot.reply_to(message, "Admin group is not configured.")

@bot.message_handler(commands=['addadmin'])
def add_admin(message):
    if message.chat.type != 'private':
        return
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "Usage: /addadmin <user_id>")
        return
    try:
        new_admin_id = int(parts[1].strip())
    except ValueError:
        bot.reply_to(message, "⚠️ Invalid user ID. Please provide a numeric user ID.")
        return
    if not admins_col.find_one({"user_id": new_admin_id}):
        admins_col.insert_one({"user_id": new_admin_id, "role": "subadmin"})
        bot.reply_to(message, f"✅ User {new_admin_id} added as a sub-admin.")
    else:
        bot.reply_to(message, f"ℹ️ User {new_admin_id} is already a sub-admin.")

@bot.message_handler(commands=['rmadmin'])
def remove_admin(message):
    if message.chat.type != 'private':
        return
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "Usage: /rmadmin <user_id>")
        return
    try:
        rm_admin_id = int(parts[1].strip())
    except ValueError:
        bot.reply_to(message, "⚠️ Invalid user ID. Please provide a numeric user ID.")
        return
    result = admins_col.delete_one({"user_id": rm_admin_id})
    if result.deleted_count > 0:
        bot.reply_to(message, f"✅ User {rm_admin_id} has been removed from sub-admins.")
    else:
        bot.reply_to(message, f"ℹ️ User {rm_admin_id} was not found in sub-admins.")


@bot.message_handler(func=lambda message: (message.text or message.caption or '').strip().lower().startswith('/addbutton'), content_types=['text', 'photo'])
def add_tutor_button(message):
    if message.chat.type != 'private':
        return
    if not is_admin_or_subadmin(message.from_user.id):
        return
    command_text = (message.text or message.caption or '').strip()
    parts = command_text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "Usage: /addbutton <Tutor Name> (attach a photo optionally)")
        return

    tutor_name = parts[1].strip()
    search_tag = tutor_search_tag_from_name(tutor_name)
    if not search_tag:
        bot.reply_to(message, "⚠️ Invalid tutor name.")
        return

    image_file_id = None
    if getattr(message, 'photo', None):
        image_file_id = message.photo[-1].file_id

    update = {
        "name": tutor_name,
        "name_key": tutor_key(tutor_name),
        "search_tag": search_tag
    }
    if image_file_id:
        update["image_file_id"] = image_file_id
        update["image_url"] = ""

    try:
        tutor_buttons_col.update_one(
            {"name_key": tutor_key(tutor_name)},
            {"$set": update, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
            upsert=True
        )
        bot.reply_to(message, f"✅ Tutor button saved: {tutor_name}")
    except Exception as e:
        logging.error("Failed to save tutor button '%s': %s", tutor_name, e)
        bot.reply_to(message, "⚠️ Failed to save tutor button. Please try again.")


@bot.message_handler(commands=['removebutton'])
def remove_tutor_button(message):
    if message.chat.type != 'private':
        return
    if not is_admin_or_subadmin(message.from_user.id):
        return
    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "Usage: /removebutton <Tutor Name>")
        return
    name = parts[1].strip()
    try:
        result = tutor_buttons_col.delete_one({"name_key": tutor_key(name)})
        if result.deleted_count:
            bot.reply_to(message, f"✅ Tutor button removed: {name}")
        else:
            bot.reply_to(message, f"⚠️ Tutor button not found: {name}")
    except Exception as e:
        logging.error("Failed to remove tutor button '%s': %s", name, e)
        bot.reply_to(message, "⚠️ Failed to remove tutor button. Please try again.")


def _forward_user_submission(message, file_name=None):
    """Forward a user's file/media submission to OTHERS_GROUP_ID with an info caption."""
    if not OTHERS_GROUP_ID:
        return
    user = message.from_user
    first = user.first_name or ""
    last = user.last_name or ""
    user_name = (first + " " + last).strip() or user.username or str(user.id)
    fn_line = f"File Name: {file_name}\n" if file_name else ""
    info_text = (
        f"📩 User Submission\n"
        f"From: {user_name} (ID: {user.id})\n"
        f"{fn_line}"
        f"\n*(Reply to this message to answer the user)*"
    )
    try:
        bot.forward_message(OTHERS_GROUP_ID, message.chat.id, message.message_id)
        bot.send_message(OTHERS_GROUP_ID, info_text)
    except Exception as e:
        logging.error("Failed to forward user submission: %s", e)


@bot.message_handler(content_types=['document'])
def handle_docs(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    is_admin = user_id == ADMIN_ID
    is_subadmin = admins_col.count_documents({"user_id": user_id}, limit=1) > 0
    if is_admin or is_subadmin:
        file_id = message.document.file_id
        file_name = message.document.file_name.lower()
        try:
            if files_col.find_one({"file_name": file_name}):
                bot.reply_to(message, f"⚠️ File '{file_name}' is already in the database. Upload rejected.")
            else:
                files_col.insert_one({"file_name": file_name, "file_id": file_id})
                bot.reply_to(message, f"✅ Saved '{file_name}' successfully.")
        except PyMongoError as e:
            logging.error("Failed to save file '%s': %s", file_name, e)
            bot.reply_to(message, "⚠️ Failed to save. Please try again.")
    else:
        file_name = message.document.file_name if message.document.file_name else "Unknown"
        _forward_user_submission(message, file_name=file_name)


@bot.message_handler(content_types=['photo'])
def handle_photos(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    is_admin = user_id == ADMIN_ID
    is_subadmin = admins_col.count_documents({"user_id": user_id}, limit=1) > 0
    if not is_admin and not is_subadmin:
        _forward_user_submission(message)


@bot.message_handler(content_types=['video', 'audio', 'voice', 'video_note'])
def handle_media(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    is_admin = user_id == ADMIN_ID
    is_subadmin = admins_col.count_documents({"user_id": user_id}, limit=1) > 0
    if not is_admin and not is_subadmin:
        _forward_user_submission(message)

@bot.message_handler(func=lambda message: (
    any([ADMIN_GROUP_ID, OTHERS_GROUP_ID, BACKUP_GROUP_ID]) and
    message.chat.id in [ADMIN_GROUP_ID, OTHERS_GROUP_ID, BACKUP_GROUP_ID] and
    message.reply_to_message is not None and
    message.reply_to_message.from_user is not None and
    message.reply_to_message.from_user.id == bot.get_me().id
), content_types=['text'])
def admin_reply_to_user(message):
    sender_id = message.from_user.id
    is_admin = sender_id == ADMIN_ID
    is_subadmin = admins_col.count_documents({"user_id": sender_id}, limit=1) > 0
    if not is_admin and not is_subadmin:
        bot.reply_to(message, "⚠️ You do not have permission to reply to users.")
        return
    replied = message.reply_to_message
    text_to_search = replied.text or replied.caption or ''
    match = re.search(r'ID: (\d+)', text_to_search)
    if match:
        user_id = int(match.group(1))
    elif replied.forward_from:
        user_id = replied.forward_from.id
    else:
        bot.reply_to(message, "⚠️ Could not find the User ID. Please reply to the info message that contains the user's ID.")
        return
    reply_text = f"👨‍💻 Admin Reply:\n{message.text}"
    try:
        bot.send_message(user_id, reply_text)
        bot.reply_to(message, "✅ Reply sent to the user.")
    except Exception:
        bot.reply_to(message, "❌ Failed to send reply. The user may have blocked the bot.")

@bot.message_handler(commands=['broadcast'])
def broadcast(message):
    if message.chat.type != 'private':
        return
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "⚠️ Usage: /broadcast <your message>")
        return

    broadcast_text = parts[1].strip()
    success = 0
    failed = 0

    for user in users_col.find():
        try:
            bot.send_message(user['user_id'], broadcast_text, parse_mode='Markdown')
            success += 1
        except telebot.apihelper.ApiTelegramException as e:
            if "parse" in str(e).lower() or "markdown" in str(e).lower():
                try:
                    # Fallback to plain text if markdown fails
                    bot.send_message(user['user_id'], broadcast_text)
                    success += 1
                except Exception as ex:
                    logging.warning("Broadcast fallback failed for user_id %s: %s", user.get('user_id'), ex)
                    failed += 1
            else:
                logging.warning("Broadcast failed for user_id %s: %s", user.get('user_id'), e)
                failed += 1
        except Exception as e:
            logging.warning("Broadcast failed for user_id %s: %s", user.get('user_id'), e)
            failed += 1

    bot.reply_to(
        message,
        f"✅ Broadcast complete!\nSuccessfully sent to: {success} users\nFailed: {failed} users"
    )

@bot.message_handler(commands=['cleardb'])
def cleardb(message):
    if message.chat.type != 'private':
        return
    if message.from_user.id != ADMIN_ID:
        return
    bot.reply_to(
        message,
        "⚠️ WARNING: This will delete ALL files from the database. This action cannot be undone.\n\nTo confirm, send the command: /confirmclear"
    )

@bot.message_handler(commands=['confirmclear'])
def confirmclear(message):
    if message.chat.type != 'private':
        return
    if message.from_user.id != ADMIN_ID:
        return
    files_col.delete_many({})
    bot.reply_to(message, "✅ Database cleared. All files have been deleted.")

@bot.message_handler(commands=['rmfile', 'deletefile'])
def remove_file(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    is_admin = user_id == ADMIN_ID
    is_subadmin = admins_col.count_documents({"user_id": user_id}, limit=1) > 0
    if not is_admin and not is_subadmin:
        return
    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "⚠️ Usage: /rmfile <exact_file_name>")
        return
    query = parts[1].strip().lower()
    result = files_col.delete_many({"file_name": query})
    if result.deleted_count > 0:
        bot.reply_to(message, f"✅ Successfully deleted {result.deleted_count} file(s) named '{query}'.")
    else:
        bot.reply_to(message, f"⚠️ No file found with the exact name '{query}'. Make sure to include any tutor tags if they exist.")


# Handler 1: When a user sends a text message (e.g., essay), return a list of matching files as buttons
@bot.message_handler(func=lambda message: True, content_types=['text'])
def search_files_text(message):
    if message.chat.type != 'private':
        return
    if not enforce_subscription(message):
        return
    if message.text.startswith('/'):
        return
        
    query = normalize_query(message.text.lower())
    user = message.from_user
    # Save the message to the messages collection
    messages_col.insert_one({
        "user_id": user.id,
        "username": user.username or user.first_name or str(user.id),
        "message": message.text,
        "timestamp": datetime.now(timezone.utc)
    })
    # Forward message to backup group if configured
    if BACKUP_GROUP_ID:
        try:
            username_display = f"@{user.username}" if user.username else str(user.id)
            backup_text = (
                f"[BACKUP] Message from User: {username_display} (ID: {user.id})\n"
                f"Message: {message.text}"
            )
            bot.send_message(BACKUP_GROUP_ID, backup_text)
        except Exception:
            pass

    lower_text = message.text.strip().lower()

    if lower_text == "past papers":
        markup = build_miniapp_markup()
        bot.reply_to(message, "use our mini app 👈", reply_markup=markup)
        return

    if re.fullmatch(r"p(?:a|e)?p{1,2}e?r?s?", lower_text):
        bot.reply_to(
            message,
            "Please send the paper you want.\nEg: `ap full paper 1` or `ad full paper 1`",
            parse_mode='Markdown'
        )
        return

    if re.search(r'\bdiscussions?\b', lower_text):
        year_match = re.search(r'\b(2025|2026)\b', lower_text)
        if year_match:
            ok, err = send_discussion_messages(message.chat.id, year_match.group(1))
            if not ok:
                bot.reply_to(message, err)
        else:
            send_discussion_year_buttons(message.chat.id, reply_to_message_id=message.message_id)
        return

    # Detect generic category phrases and prompt the user to include a paper number
    generic_category_pattern = re.compile(
        r'^(ad|ap)\s+full\s+papers?$', re.IGNORECASE
    )
    if generic_category_pattern.match(message.text.strip()):
        bot.reply_to(
            message,
            "Please send the paper you want.\nEg: `ap full paper 1`",
            parse_mode='Markdown'
        )
        return

    # Search the database for files matching the query (up to 10 results)
    results = list(files_col.find({"file_name": {"$regex": query}}).limit(10))
    
    if not results:
        bot.reply_to(message, "Sorry, no papers were found matching that name.")
        # Forward unmatched search to OTHERS group so admins can see what users are looking for
        user_id = message.from_user.id
        is_admin = user_id == ADMIN_ID
        is_subadmin = admins_col.count_documents({"user_id": user_id}, limit=1) > 0
        if not is_admin and not is_subadmin:
            _forward_user_submission(message)
        return
        
    # Build the inline keyboard with buttons for each result
    markup = InlineKeyboardMarkup()
    markup.row_width = 1
    for f in results:
        # Create a button with the file name
        btn = InlineKeyboardButton(f['file_name'], callback_data=str(f['_id']))
        markup.add(btn)
        
    bot.reply_to(message, "🔍 Here are the papers I found. Click on a paper below to download it:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == 'verify_sub')
def verify_subscription_callback(call):
    status = get_subscription_status(call.from_user.id)
    if status["channel"] and status["group"]:
        success_text = (
            "🎉 Access Granted\n\n"
            "You've joined the required channel and group successfully.\n"
            "You can now use all bot features."
        )
        bot.edit_message_text(success_text, call.message.chat.id, call.message.message_id)
    else:
        bot.answer_callback_query(call.id, "You haven't joined all required chats yet!", show_alert=True)
        markup = InlineKeyboardMarkup()
        if not status["channel"] and FORCE_CHANNEL_URL:
            markup.add(InlineKeyboardButton("Join Our Channel", url=FORCE_CHANNEL_URL))
        if not status["group"] and FORCE_GROUP_URL:
            markup.add(InlineKeyboardButton("Join Our Group", url=FORCE_GROUP_URL))
        markup.add(InlineKeyboardButton("✅ Verify", callback_data="verify_sub"))
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=markup)
        except Exception:
            pass


@bot.callback_query_handler(func=lambda call: call.data.startswith('discussion_year:'))
def discussion_year_callback(call):
    year = call.data.split(':', 1)[1] if ':' in call.data else ""
    if year not in ("2025", "2026"):
        bot.answer_callback_query(call.id, "Invalid year", show_alert=True)
        return
    ok, err = send_discussion_messages(call.message.chat.id, year)
    if ok:
        bot.answer_callback_query(call.id, f"Sending {year} discussions...")
    else:
        bot.answer_callback_query(call.id, "Failed to send discussions", show_alert=True)
        bot.send_message(call.message.chat.id, err)


def _build_backup_notification(source, full_name, username_display, user_id, file_name):
    label = "Mini App Download" if source == "miniapp" else "Bot Download"
    return (
        f"📥 *{label}*\n"
        f"User: {full_name}\n"
        f"Username: {username_display}\n"
        f"ID: `{user_id}`\n"
        f"File: `{file_name}`"
    )


@bot.callback_query_handler(func=lambda call: call.data != 'verify_sub' and len(call.data) == 24)
def send_file_callback(call):
    try:
        # Retrieve the selected file from the database
        file_data = files_col.find_one({"_id": ObjectId(call.data)})
        if file_data:
            bot.send_document(call.message.chat.id, file_data['file_id'])
            bot.answer_callback_query(call.id, "Sending file...")
            # Save to history
            history_col.insert_one({"user_id": call.from_user.id, "query": "button_click", "file_sent": file_data['file_name']})
            if BACKUP_GROUP_ID:
                try:
                    user = call.from_user
                    full_name = ' '.join(filter(None, [user.first_name, user.last_name])) or str(user.id)
                    username_display = f"@{user.username}" if user.username else "No username"
                    backup_text = _build_backup_notification("bot", full_name, username_display, user.id, file_data['file_name'])
                    bot.send_message(BACKUP_GROUP_ID, backup_text, parse_mode="Markdown")
                except Exception as e:
                    logging.error(f"Failed to send backup msg: {e}")
        else:
            bot.answer_callback_query(call.id, "File not found in the database!", show_alert=True)
    except Exception as e:
        bot.answer_callback_query(call.id, "An error occurred. Please try again.", show_alert=True)

# Legacy inline search handler (also available for inline queries)
@bot.inline_handler(lambda query: len(query.query) > 0)
def query_text(inline_query):
    query = normalize_query(inline_query.query.lower())
    results = files_col.find({"file_name": {"$regex": query}}).limit(10)
    
    inline_results = []
    for f in results:
        res = InlineQueryResultCachedDocument(
            id=str(uuid.uuid4()),
            title=f['file_name'],
            document_file_id=f['file_id']
        )
        inline_results.append(res)
    bot.answer_inline_query(inline_query.id, inline_results)

# ================= FLASK WEB PANEL (BOOTSTRAP) =================

@app.route('/webhook', methods=['POST'])
def webhook():
    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return 'OK', 200

@app.route('/')
def admin_panel():
    user_count = users_col.count_documents({})
    file_count = files_col.count_documents({})
    history_count = history_col.count_documents({})
    
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Bot Admin Dashboard</title>
        <!-- Bootstrap 5 CSS -->
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <!-- Bootstrap Icons -->
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css">
    </head>
    <body class="bg-light">
        <!-- Navbar -->
        <nav class="navbar navbar-dark bg-dark mb-4 shadow">
            <div class="container">
                <span class="navbar-brand mb-0 h1">
                    <i class="bi bi-robot"></i> Telegram Bot Dashboard
                </span>
            </div>
        </nav>

        <!-- Main Content -->
        <div class="container">
            <div class="row">
                <!-- Users Card -->
                <div class="col-md-4 mb-3">
                    <div class="card text-white bg-primary h-100 shadow-sm">
                        <div class="card-body text-center">
                            <h5 class="card-title"><i class="bi bi-people-fill"></i> Total Users</h5>
                            <h1 class="display-4 fw-bold">{{ u_count }}</h1>
                        </div>
                    </div>
                </div>
                
                <!-- Files Card -->
                <div class="col-md-4 mb-3">
                    <div class="card text-white bg-success h-100 shadow-sm">
                        <div class="card-body text-center">
                            <h5 class="card-title"><i class="bi bi-file-earmark-text-fill"></i> Hosted Papers</h5>
                            <h1 class="display-4 fw-bold">{{ f_count }}</h1>
                        </div>
                    </div>
                </div>

                <!-- Traffic/History Card -->
                <div class="col-md-4 mb-3">
                    <div class="card text-white bg-warning h-100 shadow-sm">
                        <div class="card-body text-center">
                            <h5 class="card-title"><i class="bi bi-activity"></i> Total Downloads</h5>
                            <h1 class="display-4 fw-bold text-dark">{{ h_count }}</h1>
                        </div>
                    </div>
                </div>
            </div>

            <div class="row mt-2">
                <!-- User Messages Card -->
                <div class="col-md-4 mb-3">
                    <div class="card text-white bg-info h-100 shadow-sm">
                        <div class="card-body text-center">
                            <h5 class="card-title"><i class="bi bi-chat-dots-fill"></i> User Messages</h5>
                            <p class="card-text">View all messages sent by users to the bot.</p>
                            <a href="/messages" class="btn btn-light fw-bold">View Messages</a>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="row mt-4">
                <div class="col-12 text-center text-muted">
                    <p>Powered by Flask, MongoDB & Bootstrap 5</p>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html, u_count=user_count, f_count=file_count, h_count=history_count)

@app.route('/messages')
def messages_page():
    messages = list(messages_col.find().sort("timestamp", -1).limit(200))

    row_list = []
    for m in messages:
        ts = m.get("timestamp")
        dt_str = escape(ts.strftime("%Y-%m-%d %H:%M:%S UTC")) if ts else "N/A"
        username = escape(str(m.get("username", "Unknown")))
        user_id = escape(str(m.get("user_id", "")))
        msg_text = escape(str(m.get("message", "")))
        row_list.append(f"""
        <tr>
            <td>{dt_str}</td>
            <td>{username} <small class="text-muted">({user_id})</small></td>
            <td>{msg_text}</td>
        </tr>""")
    rows = "".join(row_list)

    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>User Messages</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css">
    </head>
    <body class="bg-light">
        <nav class="navbar navbar-dark bg-dark mb-4 shadow">
            <div class="container">
                <span class="navbar-brand mb-0 h1">
                    <i class="bi bi-chat-dots-fill"></i> User Messages
                </span>
            </div>
        </nav>
        <div class="container">
            <a href="/" class="btn btn-secondary mb-3"><i class="bi bi-arrow-left"></i> Back to Dashboard</a>
            <div class="card shadow-sm">
                <div class="card-body">
                    <table class="table table-striped table-bordered table-hover">
                        <thead class="table-dark">
                            <tr>
                                <th>Date / Time</th>
                                <th>User</th>
                                <th>Message</th>
                            </tr>
                        </thead>
                        <tbody>
                            """ + (rows if rows else '<tr><td colspan="3" class="text-center text-muted">No messages yet.</td></tr>') + """
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)

# ================= TELEGRAM MINI APP =================

MINIAPP_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>PaperBot - Past Papers</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css">
    <style>
        :root {
            --tg-bg: #f5f7fa;
            --tg-accent: #2563eb;
            --tg-card: #ffffff;
        }
        body {
            background: var(--tg-bg);
            font-family: 'Segoe UI', sans-serif;
            min-height: 100vh;
            padding-bottom: 20px;
        }
        .app-header {
            background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%);
            color: white;
            padding: 18px 16px 14px;
            text-align: center;
        }
        .app-header h1 {
            font-size: 1.3rem;
            font-weight: 700;
            margin: 0;
        }
        .app-header p {
            font-size: 0.8rem;
            margin: 4px 0 0;
            opacity: 0.85;
        }
        .search-section {
            padding: 14px 16px;
        }
        .search-bar {
            border-radius: 12px;
            border: 2px solid #e2e8f0;
            padding: 10px 16px;
            font-size: 0.95rem;
            transition: border-color 0.2s;
        }
        .search-bar:focus {
            border-color: var(--tg-accent);
            box-shadow: 0 0 0 3px rgba(37,99,235,0.12);
            outline: none;
        }
        .search-btn {
            border-radius: 12px;
            background: var(--tg-accent);
            border: none;
            padding: 10px 16px;
            color: white;
            font-weight: 600;
        }
        .section-title {
            font-size: 0.85rem;
            font-weight: 700;
            color: #64748b;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            padding: 8px 16px 4px;
        }
        /* Square tutor button styles */
        .tutors-grid {
            display: flex;
            gap: 12px;
            padding: 8px 16px 12px;
            justify-content: center;
            flex-wrap: wrap;
        }
        .tutor-btn {
            display: flex;
            flex-direction: column;
            align-items: center;
            cursor: pointer;
            border: none;
            background: transparent;
            padding: 0;
            flex: 0 0 calc(33% - 10px);
            max-width: 110px;
        }
        .tutor-btn:active .tutor-img-wrap {
            transform: scale(0.95);
        }
        .tutor-img-wrap {
            width: 100%;
            aspect-ratio: 1 / 1;
            border-radius: 12px;
            overflow: hidden;
            border: 3px solid transparent;
            background: #e2e8f0;
            transition: border-color 0.2s, transform 0.15s;
            box-shadow: 0 2px 8px rgba(0,0,0,0.10);
        }
        .tutor-btn.active .tutor-img-wrap,
        .tutor-btn:hover .tutor-img-wrap {
            border-color: var(--tg-accent);
            box-shadow: 0 4px 14px rgba(37,99,235,0.25);
        }
        .tutor-img-wrap img {
            width: 100%;
            height: 100%;
            object-fit: cover;
            display: block;
        }
        .tutor-name {
            margin-top: 6px;
            font-size: 0.72rem;
            font-weight: 600;
            color: #1e3a8a;
            text-align: center;
            line-height: 1.3;
        }
        /* Results section */
        .results-section {
            padding: 0 16px;
        }
        .result-card {
            background: var(--tg-card);
            border-radius: 12px;
            padding: 12px 14px;
            margin-bottom: 8px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            box-shadow: 0 1px 4px rgba(0,0,0,0.08);
            border: 1px solid #e2e8f0;
        }
        .result-name {
            font-size: 0.88rem;
            font-weight: 500;
            color: #1e293b;
            flex: 1;
            margin-right: 10px;
            word-break: break-word;
        }
        .download-btn {
            background: var(--tg-accent);
            color: white;
            border: none;
            border-radius: 8px;
            padding: 6px 12px;
            font-size: 0.8rem;
            font-weight: 600;
            white-space: nowrap;
            cursor: pointer;
            transition: background 0.2s;
        }
        .download-btn:hover {
            background: #1d4ed8;
        }
        .empty-state {
            text-align: center;
            padding: 30px 20px;
            color: #94a3b8;
        }
        .empty-state i {
            font-size: 2.5rem;
            display: block;
            margin-bottom: 8px;
        }
        .loading-spinner {
            display: none;
            text-align: center;
            padding: 20px;
        }
        .toast-msg {
            position: fixed;
            bottom: 20px;
            left: 50%;
            transform: translateX(-50%);
            background: #1e293b;
            color: white;
            padding: 10px 20px;
            border-radius: 20px;
            font-size: 0.85rem;
            z-index: 9999;
            display: none;
            white-space: nowrap;
        }
        .sub-overlay {
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(15,23,42,0.96);
            z-index: 99999;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-direction: column;
            text-align: center;
            padding: 24px;
            color: white;
        }
        .sub-overlay h2 {
            font-size: 1.3rem;
            font-weight: 700;
            margin-bottom: 10px;
        }
        .sub-overlay p {
            font-size: 0.9rem;
            opacity: 0.85;
            margin-bottom: 20px;
        }
        .sub-overlay-btn {
            display: inline-block;
            background: #2563eb;
            color: white;
            border-radius: 10px;
            padding: 10px 22px;
            font-weight: 600;
            text-decoration: none;
            margin: 5px;
        }
    </style>
</head>
<body>
    <!-- Header -->
    <div class="app-header">
        <h1>📚 PaperBot</h1>
        <p>Find & Download Past Papers Instantly</p>
    </div>

    <!-- Search -->
    <div class="search-section">
        <div class="input-group">
            <input type="text" id="searchInput" class="form-control search-bar"
                   placeholder="Search papers (e.g. ap s2 paper 01)..."
                   autocomplete="off" autocorrect="off" spellcheck="false">
            <button class="search-btn" onclick="doSearch()">
                <i class="bi bi-search"></i>
            </button>
        </div>
    </div>

    <!-- Tutors section -->
    <div class="section-title">Browse by Tutor</div>
    <div class="tutors-grid" id="tutorsGrid"></div>

    <div class="section-title">Discussions</div>
    <div class="tutors-grid">
        <button class="search-btn" type="button" onclick="sendDiscussion('2025')">2025</button>
        <button class="search-btn" type="button" onclick="sendDiscussion('2026')">2026</button>
    </div>

    <!-- Results -->
    <div class="section-title" id="resultsTitle" style="display:none;">Results</div>
    <div class="loading-spinner" id="loadingSpinner">
        <div class="spinner-border text-primary" role="status"></div>
    </div>
    <div class="results-section" id="resultsContainer">
        <div class="empty-state">
            <i class="bi bi-search"></i>
            <p>Search for papers above or tap a tutor to browse their papers.</p>
        </div>
    </div>

    <!-- Subscription required overlay -->
    <div id="subOverlay" style="display:flex;" class="sub-overlay">
        <div style="font-size:2.5rem;margin-bottom:12px;">🔒</div>
        <h2>Access Restricted</h2>
        <p>You must join our official Channel &amp; Group to use PaperBot.</p>
        <div id="subOverlayLinks"></div>
        <div style="margin-top:18px;font-size:0.8rem;opacity:0.6;">After joining, reload the app.</div>
    </div>

    <!-- Toast notification -->
    <div class="toast-msg" id="toastMsg"></div>

    <script>
        // Init Telegram WebApp
        const tg = window.Telegram && window.Telegram.WebApp;
        if (tg) {
            tg.ready();
            tg.expand();
            document.body.style.background = tg.themeParams.bg_color || '#f5f7fa';
        }

        // Check subscription on load
        (function checkSubscription() {
            if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) {
                const userId = tg.initDataUnsafe.user.id;
                fetch('/api/verify_sub?user_id=' + encodeURIComponent(userId))
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        if (data.subscribed) {
                            document.getElementById('subOverlay').style.display = 'none';
                        } else {
                            const linksDiv = document.getElementById('subOverlayLinks');
                            linksDiv.innerHTML = '';
                            if (data.channel_url) {
                                const a = document.createElement('a');
                                a.className = 'sub-overlay-btn';
                                a.href = data.channel_url;
                                a.target = '_blank';
                                a.rel = 'noopener noreferrer';
                                a.textContent = '📢 Join Channel';
                                linksDiv.appendChild(a);
                            }
                            if (data.group_url) {
                                const a = document.createElement('a');
                                a.className = 'sub-overlay-btn';
                                a.href = data.group_url;
                                a.target = '_blank';
                                a.rel = 'noopener noreferrer';
                                a.textContent = '👥 Join Group';
                                linksDiv.appendChild(a);
                            }
                        }
                    })
                    .catch(function() { /* keep overlay shown on network error */ });
            } else {
                // Not opened from Telegram — keep overlay shown
            }
        })();

        let currentTag = null;
        let currentTutorLabel = null;

        function showToast(msg, dur) {
            const t = document.getElementById('toastMsg');
            t.textContent = msg;
            t.style.display = 'block';
            setTimeout(() => { t.style.display = 'none'; }, dur || 2000);
        }

        function setLoading(show) {
            document.getElementById('loadingSpinner').style.display = show ? 'block' : 'none';
        }

        function renderResults(files, emptyMsg) {
            const container = document.getElementById('resultsContainer');
            const title = document.getElementById('resultsTitle');
            if (!files || files.length === 0) {
                title.style.display = 'none';
                container.innerHTML = '<div class="empty-state"><i class="bi bi-inbox"></i><p>' + (emptyMsg || 'No papers found.') + '</p></div>';
                return;
            }
            title.style.display = 'block';
            container.innerHTML = files.map(function(f) {
                return '<div class="result-card">'
                    + '<span class="result-name"><i class="bi bi-file-earmark-pdf-fill text-danger me-2"></i>' + escapeHtml(f.file_name) + '</span>'
                    + '<button class="download-btn" onclick=\\'downloadFile(' + JSON.stringify(f.id) + ', ' + JSON.stringify(f.file_name).replace(/'/g, "&#39;") + ')\\'><i class="bi bi-download"></i> Get</button>'
                    + '</div>';
            }).join('');
        }

        function escapeHtml(str) {
            return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
        }

        function escapeAttr(str) {
            return escapeHtml(str).replace(/'/g, '&#39;');
        }

        function initials(name) {
            const parts = (name || '').trim().split(/\\s+/).filter(Boolean);
            if (!parts.length) return 'PB';
            if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
            return (parts[0][0] + parts[1][0]).toUpperCase();
        }

        function tutorFallbackSvg(label) {
            const txt = encodeURIComponent(label);
            return 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"><rect width="200" height="200" fill="%234f86c6"/><text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" fill="white" font-size="60">' + txt + '</text></svg>';
        }

        function loadTutorButtons() {
            fetch('/api/tutor-buttons')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    const grid = document.getElementById('tutorsGrid');
                    const tutors = (data && data.tutors) ? data.tutors : [];
                    if (!tutors.length) {
                        grid.innerHTML = '<div class="empty-state"><p>No tutors added yet.</p></div>';
                        return;
                    }
                    grid.innerHTML = tutors.map(function(t, idx) {
                        const id = 'tutor-btn-' + idx;
                        const name = t.name || t.search_tag || 'Tutor';
                        const tag = t.search_tag || '';
                        const img = t.image_url || '';
                        const init = initials(name);
                        return "<button class='tutor-btn' id='" + id + "' onclick='loadByTutor(" + JSON.stringify(tag) + ", " + JSON.stringify(id) + ", " + JSON.stringify(name) + ")'>"
                            + '<div class="tutor-img-wrap">'
                            + '<img src="' + escapeAttr(img) + '" alt="' + escapeAttr(name) + '" onerror="this.src=' + JSON.stringify(tutorFallbackSvg(init)).replace(/"/g, '&quot;') + '">'
                            + '</div>'
                            + '<span class="tutor-name">' + escapeHtml(name) + '</span>'
                            + '</button>';
                    }).join('');
                })
                .catch(function() {
                    showToast('Failed to load tutor buttons.');
                });
        }

        function doSearch() {
            const q = document.getElementById('searchInput').value.trim();
            if (!q) { showToast('Please enter a search keyword.'); return; }
            // Deactivate tutor buttons
            document.querySelectorAll('.tutor-btn').forEach(function(b) { b.classList.remove('active'); });
            currentTag = null;
            currentTutorLabel = null;
            setLoading(true);
            fetch('/api/search?q=' + encodeURIComponent(q))
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    setLoading(false);
                    renderResults(data.files, 'No papers found for "' + escapeHtml(q) + '".');
                })
                .catch(function() { setLoading(false); showToast('Search failed. Please try again.'); });
        }

        function loadByTutor(tag, btnId, displayName) {
            if (!tag) {
                showToast('Tutor is missing search tag.');
                return;
            }
            // Toggle: clicking the same active tutor clears results
            if (currentTag === tag) {
                currentTag = null;
                currentTutorLabel = null;
                document.getElementById(btnId).classList.remove('active');
                document.getElementById('resultsTitle').style.display = 'none';
                document.getElementById('resultsContainer').innerHTML = '<div class="empty-state"><i class="bi bi-search"></i><p>Search for papers above or tap a tutor to browse their papers.</p></div>';
                return;
            }
            currentTag = tag;
            currentTutorLabel = displayName || tag;
            document.querySelectorAll('.tutor-btn').forEach(function(b) { b.classList.remove('active'); });
            document.getElementById(btnId).classList.add('active');
            document.getElementById('searchInput').value = '';
            setLoading(true);
            fetch('/api/tutors?tag=' + encodeURIComponent(tag))
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    setLoading(false);
                    renderResults(data.files, 'No papers found for ' + (currentTutorLabel || tag) + '.');
                })
                .catch(function() { setLoading(false); showToast('Failed to load papers. Please try again.'); });
        }

        function sendDiscussion(year) {
            if (!(tg && tg.initDataUnsafe && tg.initDataUnsafe.user)) {
                showToast('Open this app from Telegram to request discussions.', 3000);
                return;
            }
            const userId = tg.initDataUnsafe.user.id;
            showToast('Sending ' + year + ' discussions...', 3000);
            fetch('/api/discussions/send', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({user_id: userId, year: year})
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.ok) {
                    showToast('✅ Discussions sent to your Telegram chat!', 3000);
                } else {
                    showToast(data.error || '❌ Failed to send discussions.', 3000);
                }
            })
            .catch(function() { showToast('❌ Network error. Please try again.', 3000); });
        }

        function downloadFile(fileId, fileName) {
            if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) {
                const user = tg.initDataUnsafe.user;
                const userId = user.id;
                const username = user.username || "";
                const firstName = user.first_name || "";
                const lastName = user.last_name || "";
                showToast('Sending to your chat...', 3000);
                fetch('/api/download', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({file_id: fileId, user_id: userId, file_name: fileName, username: username, first_name: firstName, last_name: lastName})
                })
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.ok) {
                        showToast('✅ Sent to your Telegram chat!', 3000);
                    } else if (data.error === 'subscription_required') {
                        showToast('⚠️ Please join our required Channel/Group to download files!', 4000);
                        document.getElementById('subOverlay').style.display = 'flex';
                    } else {
                        showToast('❌ Failed to send. Please try again.', 3000);
                    }
                })
                .catch(function() { showToast('❌ Network error. Please try again.', 3000); });
            } else {
                showToast('⚠️ Open this app from Telegram to download files.', 3000);
            }
        }

        // Allow pressing Enter in search box
        document.getElementById('searchInput').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') doSearch();
        });
        loadTutorButtons();
    </script>
</body>
</html>
"""


@app.route('/miniapp')
def miniapp():
    return render_template_string(MINIAPP_HTML)


@app.route('/api/search')
def api_search():
    q = normalize_query(request.args.get('q', '').strip().lower())
    if not q:
        return jsonify({"files": [], "error": "No query provided"})
    try:
        results = list(files_col.find(
            {"file_name": {"$regex": re.escape(q), "$options": "i"}}
        ).limit(20))
        files = [{"id": str(f['_id']), "file_name": f['file_name']} for f in results]
        return jsonify({"files": files})
    except Exception as e:
        logging.error("API search error: %s", e)
        return jsonify({"files": [], "error": "Database error"}), 500


@app.route('/api/tutors')
def api_tutors():
    tag = request.args.get('tag', '').strip().lower()
    if not tag:
        return jsonify({"files": [], "error": "Invalid tag"})
    try:
        results = list(files_col.find(
            {"file_name": {"$regex": re.escape(normalize_query(tag)), "$options": "i"}}
        ).limit(50))
        files = [{"id": str(f['_id']), "file_name": f['file_name']} for f in results]
        return jsonify({"files": files})
    except Exception as e:
        logging.error("API tutors error: %s", e)
        return jsonify({"files": [], "error": "Database error"}), 500


@app.route('/api/tutor-buttons')
def api_tutor_buttons():
    return jsonify({"tutors": get_tutor_buttons()})


@app.route('/api/tutor-image/<tutor_id>')
def api_tutor_image(tutor_id):
    try:
        tutor = tutor_buttons_col.find_one({"_id": ObjectId(tutor_id)}, {"image_file_id": 1})
        if not tutor or not tutor.get("image_file_id"):
            return "", 404
        tg_file = bot.get_file(tutor["image_file_id"])
        with urlopen(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file.file_path}") as r:
            data = r.read()
        return Response(data, mimetype="image/jpeg")
    except Exception as e:
        logging.error("Failed to fetch tutor image: %s", e)
        return "", 404


@app.route('/api/discussions/send', methods=['POST'])
def api_discussions_send():
    data = request.get_json(silent=True) or {}
    year = str(data.get('year', '')).strip()
    user_id = data.get('user_id')
    if year not in ("2025", "2026") or not str(user_id).strip():
        return jsonify({"ok": False, "error": "Invalid request"}), 400
    try:
        status = get_subscription_status(int(user_id))
        if not status["channel"] or not status["group"]:
            return jsonify({"ok": False, "error": "Please complete verification first."}), 403
        ok, err = send_discussion_messages(int(user_id), year)
        if ok:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": err}), 500
    except Exception as e:
        logging.error("API discussions send error: %s", e)
        return jsonify({"ok": False, "error": "Failed to send discussions"}), 500


@app.route('/api/verify_sub')
def api_verify_sub():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"subscribed": False, "error": "Missing user_id"}), 400
    try:
        status = get_subscription_status(int(user_id))
        subscribed = status["channel"] and status["group"]
        result = {"subscribed": subscribed}
        if not status["channel"] and FORCE_CHANNEL_URL:
            result["channel_url"] = FORCE_CHANNEL_URL
        if not status["group"] and FORCE_GROUP_URL:
            result["group_url"] = FORCE_GROUP_URL
        return jsonify(result)
    except Exception as e:
        logging.error("API verify_sub error: %s", e)
        return jsonify({"subscribed": False, "error": "Check failed"})


@app.route('/api/download', methods=['POST'])
def api_download():
    data = request.get_json(silent=True) or {}
    file_id = data.get('file_id', '').strip()
    user_id = data.get('user_id')
    file_name = data.get('file_name', '')
    username = data.get('username', '')
    first_name = data.get('first_name', '')
    last_name = data.get('last_name', '')
    if not file_id or not user_id:
        return jsonify({"ok": False, "error": "Missing file_id or user_id"})
    try:
        status = get_subscription_status(int(user_id))
        if not status["channel"] or not status["group"]:
            return jsonify({"ok": False, "error": "subscription_required"})
        file_data = files_col.find_one({"_id": ObjectId(file_id)})
        if not file_data:
            return jsonify({"ok": False, "error": "File not found"})
        bot.send_document(int(user_id), file_data['file_id'])
        history_col.insert_one({"user_id": int(user_id), "query": "miniapp_download", "file_sent": file_name})
        if BACKUP_GROUP_ID:
            try:
                full_name = ' '.join(filter(None, [first_name, last_name])) or str(user_id)
                username_display = f"@{username}" if username else "No username"
                backup_text = _build_backup_notification("miniapp", full_name, username_display, user_id, file_name)
                bot.send_message(BACKUP_GROUP_ID, backup_text, parse_mode="Markdown")
            except Exception as e:
                logging.error(f"Failed to send backup msg: {e}")
        return jsonify({"ok": True})
    except Exception as e:
        logging.error("API download error: %s", e)
        return jsonify({"ok": False, "error": "Failed to send file"})


if __name__ == '__main__':
    bot.remove_webhook()
    bot.set_webhook(url=f"{URL}/webhook")
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 5000)))
