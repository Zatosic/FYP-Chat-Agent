from authlib.integrations.flask_client import OAuth
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
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

# --- NEW: KNOWLEDGE BASE CONFIG ---
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

# --- MODELS ---
class User(db.Model, UserMixin): 
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False) 
    daily_message_count = db.Column(db.Integer, default=0)
    last_message_date = db.Column(db.Date, default=date.today)
    messages = db.relationship('Message', backref='user', lazy=True)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    sender = db.Column(db.String(50), nullable=False) 
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    session_id = db.Column(db.String(100), nullable=False, default="default")

class IntegrationSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    wa_number = db.Column(db.String(20), nullable=True)
    wa_api_key = db.Column(db.String(100), nullable=True)
    store_type = db.Column(db.String(50), nullable=True)
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

# --- NEW: ROUTE TO HANDLE FILE UPLOAD ---
@app.route('/upload_knowledge', methods=['POST'])
@login_required
def upload_knowledge():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file and file.filename.endswith('.txt'):
        # Save file specifically for this user
        filename = f"user_{current_user.id}_knowledge.txt"
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        return jsonify({"success": True})
    return jsonify({"error": "Only .txt files are allowed"}), 400

@app.route('/chat', methods=['POST'])
@login_required
def chat():
    user_message = request.json.get("message")
    session_id = request.json.get("session_id")
    
    if not session_id or session_id == "null" or session_id == "":
        session_id = uuid.uuid4().hex

    # --- 1. MOCK INVENTORY (Static Knowledge) ---
    inventory = """
    Our Current Products:
    1. MacBook Pro M2 - Price: 450,000 PKR - Stock: 5 units
    2. Dell XPS 13 - Price: 320,000 PKR - Stock: 2 units
    3. HP Spectre x360 - Price: 290,000 PKR - Stock: Out of Stock
    4. Wireless Mouse - Price: 5,000 PKR - Stock: 15 units
    5. Mechanical Keyboard - Price: 12,000 PKR - Stock: 10 units
    """

    # --- NEW: DYNAMIC KNOWLEDGE (From Uploaded File) ---
    extra_knowledge = ""
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], f"user_{current_user.id}_knowledge.txt")
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            extra_knowledge = f.read()

    # --- 2. BUSINESS LOGIC INJECTION ---
    settings = IntegrationSettings.query.filter_by(user_id=current_user.id).first()
    store_name = settings.store_url if settings and settings.store_url else "our business"
    wa_num = settings.wa_number if settings and settings.wa_number else "not provided"
    
    system_prompt = f"""
    You are a professional Business Assistant for {store_name}. 
    Your WhatsApp contact is {wa_num}.
    
    KNOWLEDGE BASE (Products):
    {inventory}
    
    ADDITIONAL STORE INFO (Uploaded by User):
    {extra_knowledge}
    
    INSTRUCTIONS:
    - Use both the KNOWLEDGE BASE and ADDITIONAL STORE INFO to answer user queries.
    - If a user asks about products, prices, or stock, strictly use the provided data.
    - If an item is "Out of Stock," politely inform them and suggest an alternative.
    - If they ask for contact info, provide the WhatsApp number: {wa_num}.
    - Be polite, concise, and professional.
    """

    if current_user.last_message_date != date.today():
        current_user.daily_message_count = 0
        current_user.last_message_date = date.today()
        db.session.commit()

    if current_user.daily_message_count >= 100:
        return jsonify({"limit_reached": True, "reply": "Daily limit reached. Please upgrade."})

    db.session.add(Message(content=user_message, sender='user', user_id=current_user.id, session_id=session_id))
    db.session.commit()

    try:
        chat_completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            model="llama-3.3-70b-versatile", 
        )
        bot_reply = chat_completion.choices[0].message.content
        db.session.add(Message(content=bot_reply, sender='bot', user_id=current_user.id, session_id=session_id))
        current_user.daily_message_count += 1
        db.session.commit()

        return jsonify({
            "reply": bot_reply,
            "messages_left": 100 - current_user.daily_message_count,
            "session_id": session_id
        })
    except Exception as e:
        return jsonify({"reply": f"Error: {str(e)}"})

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

# --- AUTH ROUTES ---

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
        flash('Invalid login.')
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