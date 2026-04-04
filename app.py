import os
import telebot
from telebot.types import InlineQueryResultCachedDocument, InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask, request, render_template_string
from pymongo import MongoClient
import uuid
from bson.objectid import ObjectId

# Environment Variables (Heroku Settings -> Config Vars walin danna)
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))
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

# ================= TELEGRAM BOT LOGIC =================

@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    if not users_col.find_one({"user_id": user_id}):
        users_col.insert_one({"user_id": user_id, "username": message.from_user.username})
    bot.reply_to(message, "Welcome! Mata ona paper eke nama (Udaharanayak: essay) type karala yawanna.")

@bot.message_handler(content_types=['document'])
def handle_docs(message):
    if message.from_user.id == ADMIN_ID:
        file_id = message.document.file_id
        file_name = message.document.file_name.lower()
        files_col.insert_one({"file_name": file_name, "file_id": file_id})
        bot.reply_to(message, f"File '{file_name}' saved successfully to MongoDB!")
    else:
        bot.reply_to(message, "You are not authorized to upload files.")

# ALUTH KALLA 1: User Text ekak (ex: essay) yawwama file list eka buttons widihata yawanawa
@bot.message_handler(func=lambda message: True, content_types=['text'])
def search_files_text(message):
    if message.text.startswith('/'):
        return
        
    query = message.text.lower()
    # Database eken e namata match wena files 10k hoyanawa
    results = list(files_col.find({"file_name": {"$regex": query}}).limit(10))
    
    if not results:
        bot.reply_to(message, "Sorry, oya namata adalawa papers mokuth hamba une naha.")
        return
        
    # Buttons list eka hadanawa
    markup = InlineKeyboardMarkup()
    markup.row_width = 1
    for f in results:
        # Button eka hadanawa file eke namath ekka
        btn = InlineKeyboardButton(f['file_name'], callback_data=str(f['_id']))
        markup.add(btn)
        
    bot.reply_to(message, "🔍 Menna mama hoyagaththa papers. Ona file eka uda click karanna:", reply_markup=markup)

# ALUTH KALLA 2: User button eka click kalama file eka send karanawa
@bot.callback_query_handler(func=lambda call: True)
def send_file_callback(call):
    try:
        # Click karapu file eka DB eken gannawa
        file_data = files_col.find_one({"_id": ObjectId(call.data)})
        if file_data:
            bot.send_document(call.message.chat.id, file_data['file_id'])
            bot.answer_callback_query(call.id, "File eka ewanawa...")
            # History ekata save karanawa
            history_col.insert_one({"user_id": call.from_user.id, "query": "button_click", "file_sent": file_data['file_name']})
        else:
            bot.answer_callback_query(call.id, "File eka database eke naha!", show_alert=True)
    except Exception as e:
        bot.answer_callback_query(call.id, "Error ekak awa!", show_alert=True)

# Parana inline search ekath thiyala thiyenne (Ona nam ekenuth ganna puluwan)
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

if __name__ == '__main__':
    bot.remove_webhook()
    bot.set_webhook(url=f"{URL}/webhook")
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 5000)))
