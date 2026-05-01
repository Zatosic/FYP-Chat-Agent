from authlib.integrations.flask_client import OAuth
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date
import os
from dotenv import load_dotenv
from groq import Groq

# Load environment variables to keep the API key secure
load_dotenv()

app = Flask(__name__)
app.secret_key = "super_secret_fyp_key" # Required for session management and error flashing

# --- OAUTH SETUP ---
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# --- DATABASE SETUP ---
# Using SQLite for the FYP as it is lightweight and file-based
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///chat_app.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- LOGIN MANAGER SETUP ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login' # Redirect unauthenticated users to the login page

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# --- DATABASE MODELS ---
class User(db.Model, UserMixin): 
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False) 
    
    # FYP Requirement: Track daily messages for the free tier limit
    daily_message_count = db.Column(db.Integer, default=0)
    last_message_date = db.Column(db.Date, default=date.today)
    
    messages = db.relationship('Message', backref='user', lazy=True)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    sender = db.Column(db.String(50), nullable=False) 
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

class IntegrationSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    
    # WhatsApp Settings
    wa_number = db.Column(db.String(20), nullable=True)
    wa_api_key = db.Column(db.String(100), nullable=True)
    
    # E-commerce Settings
    store_type = db.Column(db.String(50), nullable=True) # e.g., Shopify, WooCommerce
    store_url = db.Column(db.String(200), nullable=True)
    store_api_key = db.Column(db.String(100), nullable=True)

# Initialize the database
with app.app_context():
    db.create_all()

# --- AI SETUP ---
API_KEY = os.getenv("API_KEY")
client = Groq(api_key=API_KEY)

# --- AUTHENTICATION ROUTES ---
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        password = request.form.get('password')

        # Prevent duplicate registrations
        user = User.query.filter_by(email=email).first()
        if user:
            flash('Email address already exists. Please login.')
            return redirect(url_for('signup'))

        # Hash the password before saving to the database for security
        new_user = User(name=name, email=email, password_hash=generate_password_hash(password))
        db.session.add(new_user)
        db.session.commit()
        
        login_user(new_user)
        return redirect(url_for('home'))

    return render_template('signup.html')

@app.route('/login/google')
def google_login():
    # This redirects the user to Google's official login screen
    redirect_uri = url_for('google_authorize', _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route('/login/google/authorize')
def google_authorize():
    # Google sends the user back here with their token and profile info
    token = google.authorize_access_token()
    user_info = token.get('userinfo')
    
    # Check if this user already exists in your database
    user = User.query.filter_by(email=user_info['email']).first()
    
    if not user:
        # Create a new user if they don't exist (no password needed since Google verified them)
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
        email = request.form.get('email')
        password = request.form.get('password')
        
        user = User.query.filter_by(email=email).first()
        
        # Authenticate user by checking the hashed password
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for('home'))
        else:
            flash('Please check your login details and try again.')
            return redirect(url_for('login'))
            
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# --- MAIN APP ROUTES ---
@app.route('/')
@login_required 
def home():
    # Fetch all past messages for this user from the database
    messages = Message.query.filter_by(user_id=current_user.id).order_by(Message.timestamp).all()
    
    # Format the history into a simple list of dictionaries to pass to JavaScript
    history = [{"sender": m.sender, "content": m.content} for m in messages]
    
    return render_template('index.html', name=current_user.name, history=history)

@app.route('/chat', methods=['POST'])
@login_required
def chat():
    user_message = request.json.get("message")

    # --- FYP REQUIREMENT: 100-MESSAGE LIMIT LOGIC ---
    # Reset counter if it is a new calendar day
    if current_user.last_message_date != date.today():
        current_user.daily_message_count = 0
        current_user.last_message_date = date.today()
        db.session.commit()

    # Enforce the daily free tier limit
    MAX_MESSAGES = 100 
    if current_user.daily_message_count >= MAX_MESSAGES:
        return jsonify({
            "limit_reached": True,
            "reply": "[Premium Required] You have used your 100 free messages for today. Please upgrade to our Premium Tier for unlimited AI access, WhatsApp integration, and E-commerce support."
        })
    # ------------------------------------------------

    # Log the user's message in the database
    new_msg = Message(content=user_message, sender='user', user_id=current_user.id)
    db.session.add(new_msg)
    db.session.commit()

    try:
        # Request response from the required FYP model
        chat_completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": "You are a helpful AI assistant."},
                {"role": "user", "content": user_message}
            ],
            model="openai/gpt-oss-120b", 
        )
        
        bot_reply = chat_completion.choices[0].message.content
        
        # Log the bot's reply in the database
        bot_msg = Message(content=bot_reply, sender='bot', user_id=current_user.id)
        db.session.add(bot_msg)
        
        # Increment the user's daily message count
        current_user.daily_message_count += 1
        db.session.commit()

        messages_left = MAX_MESSAGES - current_user.daily_message_count

        return jsonify({
            "limit_reached": False,
            "reply": bot_reply,
            "messages_left": messages_left
        })

    except Exception as e:
        print(f"Error: {e}") 
        return jsonify({"limit_reached": False, "reply": "Error connecting to AI."})

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    # Look for existing settings for this user
    user_settings = IntegrationSettings.query.filter_by(user_id=current_user.id).first()
    
    if request.method == 'POST':
        # If they don't have settings yet, create a new row
        if not user_settings:
            user_settings = IntegrationSettings(user_id=current_user.id)
            db.session.add(user_settings)
        
        # Update with the form data
        user_settings.wa_number = request.form.get('wa_number')
        user_settings.wa_api_key = request.form.get('wa_api_key')
        user_settings.store_type = request.form.get('store_type')
        user_settings.store_url = request.form.get('store_url')
        user_settings.store_api_key = request.form.get('store_api_key')
        
        db.session.commit()
        flash("Integration settings saved successfully!")
        return redirect(url_for('settings'))

    return render_template('settings.html', settings=user_settings)

if __name__ == '__main__':
    app.run(debug=True)