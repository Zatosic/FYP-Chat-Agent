from authlib.integrations.flask_client import OAuth
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date
import os
from dotenv import load_dotenv
from groq import Groq
import uuid

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = "super_secret_fyp_key"

# --- KNOWLEDGE BASE CONFIG ---
UPLOAD_FOLDER = 'knowledge_base'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# OAuth Setup
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# Database Setup
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///chat_app.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Login Manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# --- MODELS (Matched to Design Document Section 5) ---
class User(db.Model, UserMixin): 
    id = db.Column(db.Integer, primary_key=True) # user_id
    name = db.Column(db.String(100), nullable=False) # username
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False) 
    daily_message_count = db.Column(db.Integer, default=0) # daily_msg_count
    last_message_date = db.Column(db.Date, default=date.today)
    # Added to match Design Doc exactly
    account_type = db.Column(db.String(20), default='Free') 
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    messages = db.relationship('Message', backref='user', lazy=True)

class Message(db.Model): # Table 2: Chat_Logs
    id = db.Column(db.Integer, primary_key=True) # chat_id
    content = db.Column(db.Text, nullable=False) # user_message / ai_response
    sender = db.Column(db.String(50), nullable=False) 
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    session_id = db.Column(db.String(100), nullable=False, default="default")

class IntegrationSettings(db.Model): # Consolidated Table 3 & 4
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    # WhatsApp Config Fields
    wa_number = db.Column(db.String(20), nullable=True) # phone_number
    wa_api_key = db.Column(db.String(100), nullable=True) # api_key
    # Store Config Fields
    store_type = db.Column(db.String(50), nullable=True) # platform_type
    store_url = db.Column(db.String(200), nullable=True) 
    store_api_key = db.Column(db.String(100), nullable=True)

with app.app_context():
    db.create_all()

# AI Client
API_KEY = os.getenv("API_KEY")
client = Groq(api_key=API_KEY)

# --- HELPER FOR SIDEBAR ---
def get_sidebar_sessions():
    if not current_user.is_authenticated:
        return []
    all_msgs = Message.query.filter_by(user_id=current_user.id).order_by(Message.timestamp.desc()).all()
    sessions = []
    seen_ids = set()
    for msg in all_msgs:
        if msg.session_id not in seen_ids:
            first_user_msg = Message.query.filter_by(session_id=msg.session_id, sender='user').order_by(Message.timestamp.asc()).first()
            title = (first_user_msg.content[:25] + "...") if first_user_msg else "New Chat"
            sessions.append({"id": msg.session_id, "title": title})
            seen_ids.add(msg.session_id)
    return sessions

# --- ROUTES ---

@app.route('/')
@login_required 
def home():
    return render_template('index.html', name=current_user.name, history=[], chat_sessions=get_sidebar_sessions(), active_session=None)

@app.route('/chat/<session_id>')
@login_required
def load_chat(session_id):
    messages = Message.query.filter_by(user_id=current_user.id, session_id=session_id).order_by(Message.timestamp.asc()).all()
    history = [{"sender": m.sender, "content": m.content} for m in messages]
    return render_template('index.html', name=current_user.name, history=history, chat_sessions=get_sidebar_sessions(), active_session=session_id)

@app.route('/upload_knowledge', methods=['POST'])
@login_required
def upload_knowledge():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file and file.filename.endswith('.txt'):
        filename = f"user_{current_user.id}_knowledge.txt"
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        return jsonify({"success": True})
    return jsonify({"error": "Only .txt files are allowed"}), 400

# --- WHATSAPP WEBHOOK (Scenario 2: TC-08) ---
@app.route('/whatsapp/webhook', methods=['POST'])
def whatsapp_webhook():
    data = request.get_json()
    incoming_msg = data.get('message', '').lower()
    wa_sender = data.get('from', 'Unknown')

    settings = IntegrationSettings.query.filter(IntegrationSettings.wa_api_key != None).first()
    if not settings:
        return jsonify({"status": "error", "msg": "No integrated business found"}), 404

    try:
        chat_completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": "You are a professional WhatsApp Auto-Reply Bot. Keep it short."},
                {"role": "user", "content": incoming_msg}
            ],
            model="llama-3.3-70b-versatile",
        )
        reply = chat_completion.choices[0].message.content
        return jsonify({"status": "success", "reply": reply})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

# --- MAIN CHAT LOGIC (Scenario 1 & 3: TC-04, TC-05, 
@app.route('/chat', methods=['POST'])
@login_required
def chat():
    try:
        user_message = request.json.get("message")
        session_id = request.json.get("session_id")
        
        # 1. Ensure Session ID exists
        if not session_id or session_id == "null" or session_id == "":
            session_id = uuid.uuid4().hex

        # 2. Daily Limit Reset Logic
        if current_user.last_message_date != date.today():
            current_user.daily_message_count = 0
            current_user.last_message_date = date.today()
            db.session.commit()

        # 3. Dynamic Limit Check (Free: 100, Premium: 1000)
        limit = 100 if current_user.account_type == 'Free' else 1000
        if current_user.daily_message_count >= limit:
            return jsonify({
                "limit_reached": True, 
                "reply": f"Daily limit reached ({limit}/{limit}). Please upgrade to Premium."
            })

        # 4. Gather Knowledge Base & Settings
        inventory = "MacBook Pro M2 (450k PKR), Dell XPS 13 (320k PKR), HP Spectre (Out of stock), Wireless Mouse (5k PKR), Mechanical Keyboard (12k PKR)."
        
        extra_knowledge = ""
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], f"user_{current_user.id}_knowledge.txt")
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                extra_knowledge = f.read()

        settings = IntegrationSettings.query.filter_by(user_id=current_user.id).first()
        store_name = settings.store_url if settings and settings.store_url else "our business"
        wa_num = settings.wa_number if settings and settings.wa_number else "not provided"
        
  # 5. Combined Professional & Passive System Prompt
        system_prompt = (
            f"You are a versatile AI Assistant for {store_name}. "
            f"Business Info: {inventory}. "
            f"Extra Context: {extra_knowledge}. "
            f"WhatsApp Link: https://wa.me/{wa_num}. "
            "INSTRUCTIONS: "
            "1. Be a friendly personal assistant. Talk about jokes, life, or weather freely. "
            f"2. ONLY if the user specifically asks about products, buying, or support, use the Business Info: {inventory}. "
            f"3. If they want to purchase or contact us, give them this link: https://wa.me/{wa_num} "
            "4. If the user says 'no' to products, drop the subject and ask what else they want to talk about."
        )
        
        # 6. Save User Message to Database
        db.session.add(Message(content=user_message, sender='user', user_id=current_user.id, session_id=session_id))
        db.session.commit()

        # 7. Call the AI Model
        chat_completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            model="llama-3.3-70b-versatile", 
        )
        bot_reply = chat_completion.choices[0].message.content
        
        # 8. Save Bot Reply & Increment Daily Count
        db.session.add(Message(content=bot_reply, sender='bot', user_id=current_user.id, session_id=session_id))
        current_user.daily_message_count += 1
        db.session.commit()

        return jsonify({
            "reply": bot_reply,
            "messages_left": limit - current_user.daily_message_count,
            "session_id": session_id
        })

    except Exception as e:
        print(f"CRITICAL ERROR: {str(e)}")
        return jsonify({"reply": "I'm having a bit of trouble right now. Please try again in a moment."})
    

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    user_settings = IntegrationSettings.query.filter_by(user_id=current_user.id).first()
    if request.method == 'POST':
        if not user_settings:
            user_settings = IntegrationSettings(user_id=current_user.id)
            db.session.add(user_settings)
        user_settings.wa_number = request.form.get('wa_number')
        user_settings.wa_api_key = request.form.get('wa_api_key')
        user_settings.store_type = request.form.get('store_type')
        user_settings.store_url = request.form.get('store_url')
        user_settings.store_api_key = request.form.get('store_api_key')
        db.session.commit()
        flash("Settings Saved!")
        return redirect(url_for('settings'))
    return render_template('settings.html', settings=user_settings)
@app.route('/upgrade_premium', methods=['POST'])
@login_required
def upgrade_premium():

    # Get current user from DB
    user = User.query.get(current_user.id)

    # Upgrade account type
    user.account_type = "Premium"

    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Upgraded to Premium successfully"
    })

@app.route('/clear_settings', methods=['POST'])
@login_required
def clear_settings():

    settings = IntegrationSettings.query.filter_by(
        user_id=current_user.id
    ).first()

    if settings:

        settings.wa_number = None
        settings.wa_api_key = None
        settings.store_type = None
        settings.store_url = None
        settings.store_api_key = None

        db.session.commit()

    return jsonify({
        "success": True
    })
# --- AUTH ROUTES (TC-01, TC-02, TC-03) ---

@app.route('/login/google')
def google_login():
    redirect_uri = url_for('google_authorize', _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route('/login/google/authorize')
def google_authorize():
    token = google.authorize_access_token()
    user_info = token.get('userinfo')
    user = User.query.filter_by(email=user_info['email']).first()
    if not user:
        user = User(
            name=user_info['name'],
            email=user_info['email'],
            password_hash="google_sso_no_password" 
        )
        db.session.add(user)
        db.session.commit()
    login_user(user)
    return redirect(url_for('home'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(email=request.form.get('email')).first()
        if user and check_password_hash(user.password_hash, request.form.get('password')):
            login_user(user)
            return redirect(url_for('home'))
    return render_template('login.html')

@app.route('/delete_chat/<session_id>', methods=['POST'])
@login_required
def delete_chat(session_id):
    Message.query.filter_by(user_id=current_user.id, session_id=session_id).delete()
    db.session.commit()
    return jsonify({"success": True})

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        email = request.form.get('email')
        if User.query.filter_by(email=email).first():
            flash('Email exists.')
            return redirect(url_for('signup'))
        new_user = User(name=request.form.get('name'), email=email, password_hash=generate_password_hash(request.form.get('password')))
        db.session.add(new_user)
        db.session.commit()
        login_user(new_user)
        return redirect(url_for('home'))
    return render_template('signup.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)