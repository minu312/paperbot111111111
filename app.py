import os
import telebot
from telebot.types import InlineQueryResultCachedDocument, InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask, request, render_template_string
from pymongo import MongoClient
import uuid
from bson.objectid import ObjectId
from datetime import datetime, timezone
from html import escape

# Environment Variables (Set these in Heroku Settings -> Config Vars)
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))
ADMIN_GROUP_ID = int(os.environ.get('ADMIN_GROUP_ID', 0))
MONGO_URI = os.environ.get('MONGO_URI')
URL = os.environ.get('HEROKU_APP_URL')

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# Database Setup
client = MongoClient(MONGO_URI)
db = client['telegram_bot']
users_col = db['users']
files_col = db['files']
history_col = db['history']
messages_col = db['messages']

# ================= TELEGRAM BOT LOGIC =================

@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    if not users_col.find_one({"user_id": user_id}):
        users_col.insert_one({"user_id": user_id, "username": message.from_user.username})
    bot.reply_to(message, "Welcome! Please type the name of the paper you are looking for (e.g., essay).")

@bot.message_handler(commands=['contact'])
def contact(message):
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
        f"Message from User ID: {user.id}\n"
        f"Name: {full_name}\n"
        f"Message: {user_message}"
    )

    if ADMIN_GROUP_ID:
        try:
            bot.send_message(ADMIN_GROUP_ID, group_text)
            bot.reply_to(message, "Your message has been sent to the admin group.")
        except Exception:
            bot.reply_to(message, "Failed to send your message. Please try again later.")
    else:
        bot.reply_to(message, "Admin group is not configured.")

@bot.message_handler(content_types=['document'])
def handle_docs(message):
    if message.from_user.id == ADMIN_ID:
        file_id = message.document.file_id
        file_name = message.document.file_name.lower()
        files_col.insert_one({"file_name": file_name, "file_id": file_id})
        bot.reply_to(message, f"File '{file_name}' saved successfully to MongoDB!")
    else:
        bot.reply_to(message, "You are not authorized to upload files.")

# Handler 1: When a user sends a text message (e.g., essay), return a list of matching files as buttons
@bot.message_handler(func=lambda message: True, content_types=['text'])
def search_files_text(message):
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
    # Search the database for files matching the query (up to 10 results)
    results = list(files_col.find({"file_name": {"$regex": query}}).limit(10))
    
    if not results:
        bot.reply_to(message, "Sorry, no papers were found matching that name.")
        return
        
    # Build the inline keyboard with buttons for each result
    markup = InlineKeyboardMarkup()
    markup.row_width = 1
    for f in results:
        # Create a button with the file name
        btn = InlineKeyboardButton(f['file_name'], callback_data=str(f['_id']))
        markup.add(btn)
        
    bot.reply_to(message, "🔍 Here are the papers I found. Click on a paper below to download it:", reply_markup=markup)

# Handler 2: When a user clicks a button, send the corresponding file
@bot.callback_query_handler(func=lambda call: True)
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
