import os
import re
import logging
import telebot
from telebot.types import InlineQueryResultCachedDocument, InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask, request, render_template_string
from pymongo import MongoClient
from pymongo.errors import PyMongoError
import uuid
from bson.objectid import ObjectId
from datetime import datetime, timezone
from html import escape

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
    welcome_text = (
        "🤖 **Welcome to Lᵉᵃʳᶯ -X - PaperBot!**\n\n"
        "The ultimate place to find your past papers and study materials.\n\n"
        "🔍 **How to search:**\n"
        "Just type the name of the paper, subject, or Teacher (e.g:- `ad s2 paper 01`) and send it to me as a normal message.\n\n"
        "📩 **Contact Admin:**\n"
        "If you need help or want to request a paper, use the `/contact` command followed by your message.\n"
        "*Example:* `/contact Please add the AP Full Paper 09.`\n\n"
        "Just type your search keyword below to get started! 👇"
    )
    bot.reply_to(message, welcome_text, parse_mode='Markdown')

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
    ADMIN_GROUP_ID and
    message.chat.id == ADMIN_GROUP_ID and
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
    match = re.search(r'User ID: (\d+)', replied.text or '')
    if not match:
        bot.reply_to(message, "⚠️ Could not find a User ID in the replied message.")
        return
    user_id = int(match.group(1))
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
        
    query = message.text.lower()
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
        else:
            bot.answer_callback_query(call.id, "File not found in the database!", show_alert=True)
    except Exception as e:
        bot.answer_callback_query(call.id, "An error occurred. Please try again.", show_alert=True)

# Legacy inline search handler (also available for inline queries)
@bot.inline_handler(lambda query: len(query.query) > 0)
def query_text(inline_query):
    query = inline_query.query.lower()
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

if __name__ == '__main__':
    bot.remove_webhook()
    bot.set_webhook(url=f"{URL}/webhook")
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 5000)))
