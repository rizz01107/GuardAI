import os
import re  # Added for link and pattern scanning
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import joblib
import webbrowser
from threading import Timer
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

# --- 1. ABSOLUTE PATH ENGINE ---
basedir = os.path.abspath(os.path.dirname(__file__))
template_dir = os.path.join(basedir, 'templates')

app = Flask(__name__, template_folder=template_dir)
app.config['SECRET_KEY'] = os.getenv('GUARDAI_SECRET_KEY', 'guardai-dev-change-me')
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024

# --- 2. DATABASE CONFIGURATION ---
db_path = os.path.join(basedir, 'users.db')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + db_path
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# --- 3. MODELS ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    scans = db.relationship('ScanHistory', backref='owner', lazy=True)

class ScanHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    message = db.Column(db.String(500), nullable=False)
    result = db.Column(db.String(50), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class Feedback(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    message = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- 4. AI MODEL LOADING ---
class DummyModel:
    def predict(self, X): return [0]
    def predict_proba(self, X): return [[0.99, 0.01]]

try:
    model_path = os.path.join(basedir, 'rizwan_scam_detector_v1.pkl')
    model = joblib.load(model_path)
    print("✅ GuardAI Model Loaded Successfully!")
except Exception as e:
    print(f"⚠️ Warning: Model not found ({e}). Using safe-mode.")
    model = DummyModel()

# --- 5. ROUTES ---

@app.route('/')
def home():
    history = []
    if current_user.is_authenticated:
        history = ScanHistory.query.filter_by(user_id=current_user.id).order_by(ScanHistory.timestamp.desc()).limit(5).all()
    return render_template('index.html', user_history=history)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated: return redirect(url_for('home'))
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        user = User.query.filter_by(username=username).first()
        if user and (check_password_hash(user.password, password) or user.password == password):
            # Backward compatibility: transparently migrate legacy plaintext passwords.
            if user.password == password:
                user.password = generate_password_hash(password)
                db.session.commit()
            login_user(user)
            return redirect(url_for('home'))
        flash('Invalid Username or Password!')
    return render_template('login.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''

        if len(username) < 3:
            flash('Username must be at least 3 characters long.')
            return redirect(url_for('signup'))
        if len(password) < 6:
            flash('Password must be at least 6 characters long.')
            return redirect(url_for('signup'))
        if User.query.filter_by(username=username).first():
            flash('Username already taken!')
            return redirect(url_for('signup'))
        new_user = User(
            username=username,
            password=generate_password_hash(password),
        )
        db.session.add(new_user)
        db.session.commit()
        flash('Account Created! Please Login.')
        return redirect(url_for('login'))
    return render_template('signup.html')

@app.route('/analyze', methods=['POST'])
@login_required 
def analyze():
    message = (request.form.get('message') or '').strip()
    if not message: return redirect(url_for('home'))
    if len(message) > 2000:
        flash('Message is too long. Please keep it under 2000 characters.')
        return redirect(url_for('home'))

    # --- REAL-TIME CONTENT HEURISTICS ---
    # 1. Link Detection (Check for unsecured links)
    links = re.findall(r'(https?://\S+|www\.\S+)', message)
    has_unsecured = any(link.startswith('http://') for link in links)
    
    # 2. Urgency/Scam Pattern Check
    urgent_patterns = ['urgent', 'immediately', 'verify', 'win', 'prize', 'block', 'account', 'gift', 'inam', 'nikalwaein']
    match_count = sum(1 for word in urgent_patterns if word in message.lower())

    # --- AI PREDICTION ---
    prediction = model.predict([message])[0]
    prob = model.predict_proba([message])[0][1] * 100
    
    # --- LOGIC INTEGRATION (Combining AI + Heuristics) ---
    if prediction == 1 or has_unsecured or (len(links) > 2 and match_count > 1):
        result = "SCAM DETECTED"
        color = "#ef4444"
        if has_unsecured:
            advice = "ALERT: Unsecured link (HTTP) detected. Phishing threat high!"
        elif match_count > 2:
            advice = "ALERT: High urgency patterns & suspicious requests identified."
        else:
            advice = "CRITICAL: Neural scan suggests this is a scam attempt."
    else:
        result = "SAFE MESSAGE"
        color = "#22c55e"
        advice = "SECURE: No immediate security threats found in the content."
    
    # SAVE TO HISTORY
    new_scan = ScanHistory(user_id=current_user.id, message=message[:100], result=result)
    db.session.add(new_scan)
    db.session.commit()

    history = ScanHistory.query.filter_by(user_id=current_user.id).order_by(ScanHistory.timestamp.desc()).limit(5).all()
    return render_template('index.html', result=result, score=f"{prob:.2f}%", color=color, advice=advice, original=message, user_history=history)

@app.route('/report_feedback', methods=['POST'])
@login_required
def report_feedback():
    message = request.form.get('message')
    if message:
        db.session.add(Feedback(user_id=current_user.id, message=message))
        db.session.commit()
        flash("Sample reported to GuardAI Intelligence for retraining.")
    return redirect(url_for('home'))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))

# --- 6. SERVER STARTUP ---
def start_browser():
    webbrowser.open("http://127.0.0.1:5050")

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    open_browser = os.getenv('GUARDAI_OPEN_BROWSER', 'false').lower() == 'true'
    if open_browser:
        Timer(1, start_browser).start()
    app.run(
        host='127.0.0.1',
        port=int(os.getenv('PORT', '5050')),
        debug=os.getenv('FLASK_DEBUG', 'false').lower() == 'true',
        use_reloader=False
    )