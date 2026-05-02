import os, uuid, json, re
try:
    import stripe
    STRIPE_ENABLED = True
except ImportError:
    stripe = None
    STRIPE_ENABLED = False
from datetime import datetime, timedelta
from flask import Flask, request, Response, stream_with_context, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import anthropic
from analyse import analyser_audio, detecter_niveau_producteur

load_dotenv()
app = Flask(__name__)

# Config
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'iym-dev-secret-2026')
try: os.makedirs("/data", exist_ok=True)
except Exception: pass
_default_db = 'sqlite:////data/insideyourmix.db' if os.path.isdir("/data") else 'sqlite:///insideyourmix.db'
_db_url = os.environ.get('DATABASE_URL', _default_db)
if _db_url.startswith('postgres://'): _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

db = SQLAlchemy(app)
lm = LoginManager(app)
lm.login_view = 'login_page'

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
try: os.makedirs("/data", exist_ok=True)
except Exception: pass
_default_db = 'sqlite:////data/insideyourmix.db' if os.path.isdir("/data") else 'sqlite:///insideyourmix.db'
_db_url = os.environ.get('DATABASE_URL', _default_db)

PLAN_LIMITS = {'free': 3, 'starter': 20, 'pro': 100, 'studio': 999999}

# ── CONFIG STRIPE ─────────────────────────────────────────────────────────
if STRIPE_ENABLED:
    stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
STRIPE_PRICES = {
    'starter': os.environ.get('STRIPE_PRICE_STARTER', ''),
    'pro':     os.environ.get('STRIPE_PRICE_PRO', ''),
}

class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id                  = db.Column(db.Integer, primary_key=True)
    email               = db.Column(db.String(150), unique=True, nullable=False)
    password_hash       = db.Column(db.String(256), nullable=False)
    plan                = db.Column(db.String(20), default='free')
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)
    analyses_this_month = db.Column(db.Integer, default=0)
    quota_reset_at      = db.Column(db.DateTime, default=datetime.utcnow)
    stripe_customer_id   = db.Column(db.String(100), nullable=True)
    stripe_sub_id        = db.Column(db.String(100), nullable=True)
    analyses             = db.relationship('Analysis', backref='user', lazy=True)

    def can_analyse(self):
        now = datetime.utcnow()
        if now > self.quota_reset_at + timedelta(days=30):
            self.analyses_this_month = 0
            self.quota_reset_at = now
            db.session.commit()
        return self.analyses_this_month < PLAN_LIMITS.get(self.plan, 3)

    def remaining(self):
        now = datetime.utcnow()
        if now > self.quota_reset_at + timedelta(days=30):
            return PLAN_LIMITS.get(self.plan, 3)
        return max(0, PLAN_LIMITS.get(self.plan, 3) - self.analyses_this_month)

    def plan_label(self):
        return {'free': 'Gratuit', 'starter': 'Starter', 'pro': 'Pro', 'studio': 'Studio'}.get(self.plan, 'Gratuit')

class Analysis(db.Model):
    __tablename__ = 'analyses'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    genre      = db.Column(db.String(100))
    score      = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

@lm.user_loader
def load_user(uid): return User.query.get(int(uid))

with app.app_context():
    db.create_all()
    # Migration : ajouter les colonnes Stripe si absentes
    for col, coltype in [('stripe_customer_id', 'VARCHAR(100)'), ('stripe_sub_id', 'VARCHAR(100)')]:
        try:
            with db.engine.connect() as conn:
                conn.execute(db.text(f'ALTER TABLE users ADD COLUMN {col} {coltype}'))
                conn.commit()
                print(f"Migration OK: {col}")
        except Exception:
            pass  # Colonne déjà présente — normal

GENRES_CLUB = [
    # Famille Techno
    "techno","techno peak time","techno raw deep hypnotic","melodic techno","melodic house techno",
    "hard techno","industrial techno","dub techno","dark techno","minimal techno","acid techno",
    # Famille House
    "house","deep house","tech house","afro house","organic house","progressive house",
    "melodic house","jackin house","funky house","bass house","tribal house","amapiano",
    # Famille Trance
    "trance","trance main floor","trance raw deep hypnotic","uplifting trance","progressive trance",
    "hard trance","tech trance","psytrance","full-on psytrance","dark psytrance","goa trance",
    # Famille Bass
    "drum and bass","dnb","liquid dnb","neurofunk","jump up dnb","halftime","jungle",
    "dubstep","deep dubstep","uk garage","uk bass","grime","breakbeat","breaks",
    # Famille Electronic
    "hardstyle","hardcore","hard dance","neo rave","mainstage","electro",
    # Famille Amapiano
    "indie dance","nu disco","bass club",
]

PROFILS_GENRE = {
    "techno": {"lufs": -9, "sub": 24, "basses": 25, "mids": 22, "hauts_mids": 16, "aigus": 13, "bpm_min": 130, "bpm_max": 150, "stereo": 0.3, "reverb": 0.4, "crest": 8},
    "melodic techno": {"lufs": -10, "sub": 21, "basses": 24, "mids": 24, "hauts_mids": 17, "aigus": 14, "bpm_min": 120, "bpm_max": 135, "stereo": 0.4, "reverb": 0.5, "crest": 9},
    "hard techno": {"lufs": -8, "sub": 26, "basses": 26, "mids": 21, "hauts_mids": 15, "aigus": 12, "bpm_min": 140, "bpm_max": 160, "stereo": 0.25, "reverb": 0.3, "crest": 7},
    "industrial techno": {"lufs": -8, "sub": 25, "basses": 26, "mids": 22, "hauts_mids": 15, "aigus": 12, "bpm_min": 140, "bpm_max": 165, "stereo": 0.3, "reverb": 0.35, "crest": 7},
    "dub techno": {"lufs": -12, "sub": 23, "basses": 24, "mids": 24, "hauts_mids": 16, "aigus": 13, "bpm_min": 125, "bpm_max": 138, "stereo": 0.5, "reverb": 0.65, "crest": 10},
    "minimal techno": {"lufs": -11, "sub": 21, "basses": 23, "mids": 24, "hauts_mids": 17, "aigus": 15, "bpm_min": 128, "bpm_max": 140, "stereo": 0.35, "reverb": 0.45, "crest": 10},
    "house": {"lufs": -10, "sub": 19, "basses": 24, "mids": 25, "hauts_mids": 17, "aigus": 15, "bpm_min": 120, "bpm_max": 130, "stereo": 0.35, "reverb": 0.4, "crest": 9},
    "deep house": {"lufs": -12, "sub": 18, "basses": 23, "mids": 25, "hauts_mids": 18, "aigus": 16, "bpm_min": 118, "bpm_max": 126, "stereo": 0.4, "reverb": 0.5, "crest": 10},
    "tech house": {"lufs": -9, "sub": 21, "basses": 25, "mids": 23, "hauts_mids": 17, "aigus": 14, "bpm_min": 124, "bpm_max": 132, "stereo": 0.3, "reverb": 0.35, "crest": 8},
    "afro house": {"lufs": -10, "sub": 19, "basses": 23, "mids": 25, "hauts_mids": 17, "aigus": 16, "bpm_min": 118, "bpm_max": 126, "stereo": 0.4, "reverb": 0.45, "crest": 9},
    "organic house": {"lufs": -12, "sub": 17, "basses": 21, "mids": 27, "hauts_mids": 19, "aigus": 16, "bpm_min": 116, "bpm_max": 124, "stereo": 0.45, "reverb": 0.55, "crest": 11},
    "progressive house": {"lufs": -10, "sub": 18, "basses": 22, "mids": 26, "hauts_mids": 18, "aigus": 16, "bpm_min": 124, "bpm_max": 132, "stereo": 0.45, "reverb": 0.5, "crest": 9},
    "melodic house": {"lufs": -11, "sub": 16, "basses": 21, "mids": 27, "hauts_mids": 19, "aigus": 17, "bpm_min": 120, "bpm_max": 128, "stereo": 0.45, "reverb": 0.5, "crest": 10},
    "amapiano": {"lufs": -10, "sub": 21, "basses": 23, "mids": 25, "hauts_mids": 17, "aigus": 14, "bpm_min": 100, "bpm_max": 116, "stereo": 0.35, "reverb": 0.4, "crest": 9},
    "drum and bass": {"lufs": -8, "sub": 26, "basses": 24, "mids": 24, "hauts_mids": 15, "aigus": 11, "bpm_min": 160, "bpm_max": 180, "stereo": 0.35, "reverb": 0.3, "crest": 8},
    "dnb": {"lufs": -8, "sub": 26, "basses": 24, "mids": 24, "hauts_mids": 15, "aigus": 11, "bpm_min": 160, "bpm_max": 180, "stereo": 0.35, "reverb": 0.3, "crest": 8},
    "liquid dnb": {"lufs": -10, "sub": 22, "basses": 23, "mids": 25, "hauts_mids": 16, "aigus": 14, "bpm_min": 160, "bpm_max": 175, "stereo": 0.4, "reverb": 0.4, "crest": 9},
    "dubstep": {"lufs": -7, "sub": 25, "basses": 25, "mids": 24, "hauts_mids": 14, "aigus": 12, "bpm_min": 138, "bpm_max": 145, "stereo": 0.35, "reverb": 0.3, "crest": 7},
    "uk garage": {"lufs": -10, "sub": 19, "basses": 23, "mids": 25, "hauts_mids": 17, "aigus": 16, "bpm_min": 128, "bpm_max": 136, "stereo": 0.35, "reverb": 0.35, "crest": 9},
    "hip-hop": {"lufs": -9, "sub": 24, "basses": 24, "mids": 24, "hauts_mids": 15, "aigus": 13, "bpm_min": 70, "bpm_max": 100, "stereo": 0.3, "reverb": 0.35, "crest": 8},
    "trap": {"lufs": -8, "sub": 27, "basses": 25, "mids": 23, "hauts_mids": 14, "aigus": 11, "bpm_min": 130, "bpm_max": 160, "stereo": 0.35, "reverb": 0.3, "crest": 7},
    "drill": {"lufs": -8, "sub": 26, "basses": 25, "mids": 23, "hauts_mids": 14, "aigus": 12, "bpm_min": 130, "bpm_max": 150, "stereo": 0.3, "reverb": 0.3, "crest": 7},
    "boom bap": {"lufs": -11, "sub": 21, "basses": 23, "mids": 25, "hauts_mids": 16, "aigus": 15, "bpm_min": 85, "bpm_max": 100, "stereo": 0.3, "reverb": 0.4, "crest": 9},
    "lo-fi hip-hop": {"lufs": -14, "sub": 19, "basses": 22, "mids": 25, "hauts_mids": 17, "aigus": 17, "bpm_min": 70, "bpm_max": 90, "stereo": 0.35, "reverb": 0.5, "crest": 12},
    "trance": {"lufs": -9, "sub": 18, "basses": 21, "mids": 26, "hauts_mids": 19, "aigus": 16, "bpm_min": 136, "bpm_max": 145, "stereo": 0.5, "reverb": 0.55, "crest": 9},
    "psytrance": {"lufs": -8, "sub": 21, "basses": 23, "mids": 24, "hauts_mids": 17, "aigus": 15, "bpm_min": 140, "bpm_max": 150, "stereo": 0.4, "reverb": 0.45, "crest": 8},
    "hardstyle": {"lufs": -7, "sub": 25, "basses": 25, "mids": 22, "hauts_mids": 16, "aigus": 12, "bpm_min": 148, "bpm_max": 160, "stereo": 0.35, "reverb": 0.35, "crest": 7},
    "ambient": {"lufs": -16, "sub": 18, "basses": 20, "mids": 26, "hauts_mids": 19, "aigus": 17, "bpm_min": 60, "bpm_max": 100, "stereo": 0.6, "reverb": 0.7, "crest": 14},
    "pop": {"lufs": -9, "sub": 14, "basses": 23, "mids": 30, "hauts_mids": 19, "aigus": 15, "bpm_min": 90, "bpm_max": 130, "stereo": 0.4, "reverb": 0.4, "crest": 9},
    "default": {"lufs": -11, "sub": 20, "basses": 22, "mids": 26, "hauts_mids": 18, "aigus": 14, "bpm_min": 100, "bpm_max": 160, "stereo": 0.35, "reverb": 0.4, "crest": 9},
    # ── Aliases Beatport ──
    "techno peak time":              {"lufs": -9,  "sub": 25, "basses": 25, "mids": 21, "hauts_mids": 16, "aigus": 13, "bpm_min": 130, "bpm_max": 150, "stereo": 0.3,  "reverb": 0.4,  "crest": 8},
    "techno raw deep hypnotic":      {"lufs": -11, "sub": 18, "basses": 26, "mids": 27, "hauts_mids": 13, "aigus": 16, "bpm_min": 125, "bpm_max": 142, "stereo": 0.45, "reverb": 0.55, "crest": 10},
    "melodic house techno":          {"lufs": -10, "sub": 20, "basses": 23, "mids": 25, "hauts_mids": 18, "aigus": 14, "bpm_min": 120, "bpm_max": 132, "stereo": 0.45, "reverb": 0.5,  "crest": 9},
    "acid techno":                   {"lufs": -9,  "sub": 22, "basses": 24, "mids": 23, "hauts_mids": 17, "aigus": 14, "bpm_min": 128, "bpm_max": 145, "stereo": 0.3,  "reverb": 0.35, "crest": 8},
    # ── House étendu ──
    "jackin house":                  {"lufs": -10, "sub": 19, "basses": 24, "mids": 25, "hauts_mids": 17, "aigus": 15, "bpm_min": 124, "bpm_max": 130, "stereo": 0.35, "reverb": 0.4,  "crest": 9},
    "funky house":                   {"lufs": -10, "sub": 18, "basses": 23, "mids": 26, "hauts_mids": 17, "aigus": 16, "bpm_min": 120, "bpm_max": 128, "stereo": 0.4,  "reverb": 0.45, "crest": 9},
    "bass house":                    {"lufs": -8,  "sub": 22, "basses": 25, "mids": 23, "hauts_mids": 16, "aigus": 14, "bpm_min": 125, "bpm_max": 132, "stereo": 0.3,  "reverb": 0.3,  "crest": 8},
    "tribal house":                  {"lufs": -10, "sub": 19, "basses": 23, "mids": 25, "hauts_mids": 17, "aigus": 16, "bpm_min": 120, "bpm_max": 128, "stereo": 0.35, "reverb": 0.45, "crest": 9},
    "soulful house":                 {"lufs": -11, "sub": 17, "basses": 21, "mids": 27, "hauts_mids": 19, "aigus": 16, "bpm_min": 118, "bpm_max": 126, "stereo": 0.4,  "reverb": 0.5,  "crest": 10},
    "indie dance":                   {"lufs": -11, "sub": 16,  "basses": 21, "mids": 27, "hauts_mids": 19, "aigus": 17, "bpm_min": 120, "bpm_max": 132, "stereo": 0.45, "reverb": 0.5,  "crest": 10},
    "nu disco":                      {"lufs": -11, "sub": 16,  "basses": 21, "mids": 27, "hauts_mids": 19, "aigus": 17, "bpm_min": 110, "bpm_max": 124, "stereo": 0.45, "reverb": 0.5,  "crest": 10},
    "bass club":                     {"lufs": -8,  "sub": 22, "basses": 23, "mids": 25, "hauts_mids": 16, "aigus": 14, "bpm_min": 125, "bpm_max": 140, "stereo": 0.3,  "reverb": 0.3,  "crest": 8},
    # ── Trance étendu ──
    "trance main floor":             {"lufs": -9,  "sub": 19, "basses": 21, "mids": 25, "hauts_mids": 19, "aigus": 16, "bpm_min": 136, "bpm_max": 145, "stereo": 0.5,  "reverb": 0.55, "crest": 9},
    "trance raw deep hypnotic":      {"lufs": -11, "sub": 22, "basses": 23, "mids": 24, "hauts_mids": 17, "aigus": 14, "bpm_min": 136, "bpm_max": 148, "stereo": 0.55, "reverb": 0.6,  "crest": 10},
    "uplifting trance":              {"lufs": -9,  "sub": 17, "basses": 20, "mids": 26, "hauts_mids": 20, "aigus": 17, "bpm_min": 138, "bpm_max": 146, "stereo": 0.55, "reverb": 0.6,  "crest": 9},
    "progressive trance":            {"lufs": -10, "sub": 17, "basses": 21, "mids": 26, "hauts_mids": 20, "aigus": 16, "bpm_min": 132, "bpm_max": 140, "stereo": 0.5,  "reverb": 0.55, "crest": 9},
    "vocal trance":                  {"lufs": -9,  "sub": 16, "basses": 20, "mids": 26, "hauts_mids": 21, "aigus": 17, "bpm_min": 136, "bpm_max": 144, "stereo": 0.5,  "reverb": 0.6,  "crest": 9},
    "hard trance":                   {"lufs": -8,  "sub": 21, "basses": 23, "mids": 24, "hauts_mids": 17, "aigus": 15, "bpm_min": 145, "bpm_max": 155, "stereo": 0.45, "reverb": 0.45, "crest": 8},
    "tech trance":                   {"lufs": -9,  "sub": 20, "basses": 22, "mids": 24, "hauts_mids": 18, "aigus": 16, "bpm_min": 138, "bpm_max": 146, "stereo": 0.45, "reverb": 0.5,  "crest": 9},
    "full-on psytrance":             {"lufs": -8,  "sub": 21, "basses": 23, "mids": 24, "hauts_mids": 17, "aigus": 15, "bpm_min": 143, "bpm_max": 150, "stereo": 0.4,  "reverb": 0.45, "crest": 8},
    "dark psytrance":                {"lufs": -8,  "sub": 23, "basses": 24, "mids": 23, "hauts_mids": 16, "aigus": 14, "bpm_min": 143, "bpm_max": 152, "stereo": 0.35, "reverb": 0.4,  "crest": 7},
    "goa trance":                    {"lufs": -9,  "sub": 20, "basses": 22, "mids": 24, "hauts_mids": 18, "aigus": 16, "bpm_min": 136, "bpm_max": 150, "stereo": 0.45, "reverb": 0.5,  "crest": 9},
    # ── Bass Music étendu ──
    "jump up dnb":                   {"lufs": -8,  "sub": 26, "basses": 25, "mids": 23, "hauts_mids": 15, "aigus": 11, "bpm_min": 165, "bpm_max": 180, "stereo": 0.35, "reverb": 0.25, "crest": 7},
    "neurofunk":                     {"lufs": -8,  "sub": 24, "basses": 24, "mids": 24, "hauts_mids": 15, "aigus": 13, "bpm_min": 165, "bpm_max": 175, "stereo": 0.35, "reverb": 0.3,  "crest": 8},
    "halftime":                      {"lufs": -9,  "sub": 28, "basses": 25, "mids": 22, "hauts_mids": 14, "aigus": 11, "bpm_min": 65,  "bpm_max": 80,  "stereo": 0.4,  "reverb": 0.4,  "crest": 8},
    "jungle":                        {"lufs": -9,  "sub": 21, "basses": 23, "mids": 25, "hauts_mids": 16, "aigus": 15, "bpm_min": 155, "bpm_max": 175, "stereo": 0.35, "reverb": 0.35, "crest": 8},
    "deep dubstep":                  {"lufs": -10, "sub": 23, "basses": 24, "mids": 24, "hauts_mids": 15, "aigus": 14, "bpm_min": 138, "bpm_max": 142, "stereo": 0.4,  "reverb": 0.45, "crest": 9},
    "140 deep dubstep":              {"lufs": -9,  "sub": 24, "basses": 24, "mids": 24, "hauts_mids": 15, "aigus": 13, "bpm_min": 136, "bpm_max": 145, "stereo": 0.4,  "reverb": 0.4,  "crest": 9},
    "uk bass":                       {"lufs": -9,  "sub": 22, "basses": 23, "mids": 25, "hauts_mids": 16, "aigus": 14, "bpm_min": 128, "bpm_max": 140, "stereo": 0.4,  "reverb": 0.35, "crest": 9},
    "breakbeat":                     {"lufs": -9,  "sub": 20, "basses": 22, "mids": 25, "hauts_mids": 17, "aigus": 16, "bpm_min": 115, "bpm_max": 140, "stereo": 0.4,  "reverb": 0.4,  "crest": 9},
    "breaks":                        {"lufs": -9,  "sub": 20, "basses": 22, "mids": 25, "hauts_mids": 17, "aigus": 16, "bpm_min": 120, "bpm_max": 145, "stereo": 0.4,  "reverb": 0.4,  "crest": 9},
    # ── Electronic étendu ──
    "electronica":                   {"lufs": -13, "sub": 16,  "basses": 20, "mids": 27, "hauts_mids": 20, "aigus": 17, "bpm_min": 70,  "bpm_max": 140, "stereo": 0.55, "reverb": 0.55, "crest": 12},
    "electro":                       {"lufs": -9,  "sub": 20, "basses": 22, "mids": 25, "hauts_mids": 18, "aigus": 15, "bpm_min": 120, "bpm_max": 140, "stereo": 0.4,  "reverb": 0.4,  "crest": 9},
    "mainstage":                     {"lufs": -8,  "sub": 19, "basses": 22, "mids": 26, "hauts_mids": 18, "aigus": 15, "bpm_min": 128, "bpm_max": 138, "stereo": 0.55, "reverb": 0.55, "crest": 8},
    "hard dance":                    {"lufs": -7,  "sub": 24, "basses": 24, "mids": 23, "hauts_mids": 16, "aigus": 13, "bpm_min": 150, "bpm_max": 175, "stereo": 0.35, "reverb": 0.3,  "crest": 7},
    "neo rave":                      {"lufs": -8,  "sub": 22, "basses": 23, "mids": 24, "hauts_mids": 17, "aigus": 14, "bpm_min": 145, "bpm_max": 165, "stereo": 0.4,  "reverb": 0.35, "crest": 8},
    "phonk":                         {"lufs": -8,  "sub": 28, "basses": 25, "mids": 22, "hauts_mids": 14, "aigus": 11, "bpm_min": 130, "bpm_max": 160, "stereo": 0.35, "reverb": 0.35, "crest": 7},
    "future bass":                   {"lufs": -8,  "sub": 22, "basses": 23, "mids": 24, "hauts_mids": 17, "aigus": 14, "bpm_min": 130, "bpm_max": 150, "stereo": 0.5,  "reverb": 0.45, "crest": 8},
    "synthwave":                     {"lufs": -11, "sub": 19, "basses": 22, "mids": 25, "hauts_mids": 18, "aigus": 16, "bpm_min": 90,  "bpm_max": 120, "stereo": 0.45, "reverb": 0.55, "crest": 10},
    "downtempo":                     {"lufs": -13, "sub": 19, "basses": 21, "mids": 26, "hauts_mids": 18, "aigus": 16, "bpm_min": 60,  "bpm_max": 100, "stereo": 0.5,  "reverb": 0.55, "crest": 11},
    "jersey club":                   {"lufs": -8,  "sub": 23, "basses": 24, "mids": 24, "hauts_mids": 16, "aigus": 13, "bpm_min": 130, "bpm_max": 145, "stereo": 0.35, "reverb": 0.3,  "crest": 8},
    "afrobeats":                     {"lufs": -9,  "sub": 18, "basses": 22, "mids": 26, "hauts_mids": 18, "aigus": 16, "bpm_min": 95,  "bpm_max": 115, "stereo": 0.4,  "reverb": 0.45, "crest": 9},
    "grime":                         {"lufs": -8,  "sub": 21, "basses": 23, "mids": 25, "hauts_mids": 16, "aigus": 15, "bpm_min": 130, "bpm_max": 145, "stereo": 0.35, "reverb": 0.3,  "crest": 8},
    "rnb":                           {"lufs": -9,  "sub": 16, "basses": 21, "mids": 27, "hauts_mids": 19, "aigus": 17, "bpm_min": 60,  "bpm_max": 100, "stereo": 0.45, "reverb": 0.5,  "crest": 10},
    "hardcore":                      {"lufs": -7,  "sub": 24, "basses": 24, "mids": 23, "hauts_mids": 16, "aigus": 13, "bpm_min": 160, "bpm_max": 200, "stereo": 0.3,  "reverb": 0.25, "crest": 6},
    "rock":                          {"lufs": -10, "sub": 13,  "basses": 20, "mids": 29, "hauts_mids": 22, "aigus": 16, "bpm_min": 80,  "bpm_max": 160, "stereo": 0.45, "reverb": 0.45, "crest": 10},
    "jazz":                          {"lufs": -14, "sub": 15,  "basses": 17, "mids": 28, "hauts_mids": 23, "aigus": 17, "bpm_min": 60,  "bpm_max": 220, "stereo": 0.5,  "reverb": 0.5,  "crest": 14},
    "soul":                          {"lufs": -11, "sub": 15,  "basses": 20, "mids": 28, "hauts_mids": 20, "aigus": 17, "bpm_min": 60,  "bpm_max": 110, "stereo": 0.45, "reverb": 0.5,  "crest": 11},
    "funk":                          {"lufs": -10, "sub": 17, "basses": 22, "mids": 27, "hauts_mids": 18, "aigus": 16, "bpm_min": 80,  "bpm_max": 130, "stereo": 0.4,  "reverb": 0.4,  "crest": 10},
    "reggae":                        {"lufs": -11, "sub": 21, "basses": 23, "mids": 24, "hauts_mids": 17, "aigus": 15, "bpm_min": 60,  "bpm_max": 90,  "stereo": 0.4,  "reverb": 0.55, "crest": 11},
}

REFS_CULTURELLES = {
    "techno":          "Charlotte de Witte, Amelie Lens, Alignment — LUFS autour de -8/-9, kick tres compresse, sub mono serree",
    "melodic techno":  "Anyma, Afterlife label, Tale Of Us — LUFS -10/-11, reverb longue sur les synthes, stereo large sur les pads",
    "hard techno":     "Sara Landry, Alignment, Dax J — LUFS -7/-8, distorsion assumee, kicks satures",
    "house":           "Fisher, Chris Lake, Marshall Jefferson — LUFS -10, groove syncopé, basses chaleureuses",
    "deep house":      "Larry Heard, Floating Points, Bicep — LUFS -12/-13, atmosphere, peu de sidechain",
    "tech house":      "Fisher, Chris Lake, Skream — LUFS -9, groove minimal, basses percussives",
    "drum and bass":   "Chase & Status, Noisia, Andy C — LUFS -8, sub profond mono, breaks tres dynamiques",
    "dubstep":         "Skream, Benga, Digital Mystikz — LUFS -7/-8, wobble bass dominant, graves devastateurs",
    "hip-hop":         "Metro Boomin, DJ Premier, Kanye West — LUFS -9/-10, kick punchy, sample chaud",
    "trap":            "Travis Scott, Future, Mike Will Made It — LUFS -7/-8, 808 sub dominant, hi-hats triples",
    "drill":           "Pop Smoke, Central Cee — LUFS -8, 808 sombre, reverb longue sur la voix",
    "ambient":         "Brian Eno, Jon Hopkins, Nils Frahm — LUFS -16/-18, dynamique tres large, reverb infinie",
    "trance":          "Above & Beyond, Armin van Buuren — LUFS -9, reverb sur le lead, stereo tres large",
    "psytrance":       "Infected Mushroom, Astrix — LUFS -8, kick acid, groove hypnotique",
    "pop":             "Max Martin, Billie Eilish, Dua Lipa — LUFS -9, voix claire, production tres propre",
    "lo-fi hip-hop":   "Nujabes, j^p^n — LUFS -14/-16, vinyle crackle, graves ronds et chauds",
    "afro house":      "Black Coffee, Themba — LUFS -10, percussions riches, groove africain",
    "default":         "Standards industrie generaux — LUFS -14 Spotify, -9 Beatport/clubs",
    # Nouveaux genres
    "techno peak time":          "Drumcode, Adam Beyer, Enrico Sangiuliano — LUFS -8/-9, kick driving et percutant",
    "techno raw deep hypnotic":  "Perc, Surgeon, KI/KI — LUFS -10/-11, groove hypnotique, moins de percussion",
    "melodic house techno":      "Anyma, Tale Of Us, Bicep — LUFS -10/-11, fusion melodique techno/house",
    "acid techno":               "Plastikman, DJ Pierre, Aphex Twin — LUFS -9, TB-303 dominant, groove acide",
    "jackin house":              "DJ Sneak, Mark Farina, Cajmere — LUFS -10, groove funky et percussif",
    "funky house":               "Daft Punk, Roger Sanchez, Armand Van Helden — LUFS -10, funk samples, groove positif",
    "bass house":                "Walker & Royce, Chris Lorenzo, AC Slater — LUFS -8/-9, basses agressives, groove direct",
    "soulful house":             "Larry Heard, Ten City, MK — LUFS -11/-12, chants soul, emotion house",
    "indie dance":               "Hot Chip, LCD Soundsystem, Factory Floor — LUFS -11, groove indie alternatif",
    "nu disco":                  "Daft Punk, Chromeo, Todd Terje — LUFS -11, samples disco, synthes chauds",
    "trance main floor":         "Armin van Buuren, ATB, Paul van Dyk — LUFS -9, euphorie, builds epiques",
    "trance raw deep hypnotic":  "John 00 Fleming, KI/KI, Basil OGlue — LUFS -10/-11, hypnotique underground",
    "uplifting trance":          "Ferry Corsten, Aly & Fila, Giuseppe Ottaviani — LUFS -9, lead melancholique euphorique",
    "progressive trance":        "Solarstone, Lange, James Holden — LUFS -10, build progressif, moins commercial",
    "vocal trance":              "Dash Berlin, Armin feat. Nadia Ali — LUFS -9, voix en avant, tres melodique",
    "hard trance":               "Scooter, Clubbheads, Planet Punk — LUFS -8, hyper energique, kicks satures",
    "tech trance":               "BT, Art of Trance, Marco V — LUFS -9, elements techno dans la trance",
    "full-on psytrance":         "Infected Mushroom, Astrix, Vini Vici — LUFS -8, energetique et festif",
    "dark psytrance":            "Atriohm, Sonic Species, Megalodon — LUFS -8, sombre et interieur",
    "goa trance":                "Hallucinogen, Man With No Name, Etnica — LUFS -9, psychedelique vintage",
    "jump up dnb":               "DJ Fresh, Skibadee, Shy FX — LUFS -8, heavy bass, energie maximale",
    "neurofunk":                 "Noisia, Phace, Current Value — LUFS -8, complexite sonore extreme, futuriste",
    "halftime":                  "Shlohmo, Salva, Lapalux — LUFS -9, tempo lent ressenti, sub lourd",
    "jungle":                    "Goldie, LTJ Bukem, 4Hero — LUFS -9, amen break, atmoshere rave 90s",
    "deep dubstep":              "Mala, Coki, Loefah — LUFS -10, sub profond, espace et poids",
    "140 deep dubstep":          "Commodo, Gantz, Goth-Trad — LUFS -9, entre dubstep et techno",
    "uk bass":                   "Skream, Benga, Digital Mystikz — LUFS -9, basses UK, groove specifique",
    "breakbeat":                 "The Chemical Brothers, Fatboy Slim, Propellerheads — LUFS -9, breaks syncopés",
    "breaks":                    "Soul Slinger, Rennie Pilgrem, Adam Freeland — LUFS -9, breaks electroniques",
    "electronica":               "Boards of Canada, Aphex Twin, Autechre — LUFS -13/-14, experimental intelligent",
    "electro":                   "Anthony Rother, Dopplereffekt, Aux 88 — LUFS -9, Detroit electro classique",
    "mainstage":                 "Martin Garrix, Tiesto, Hardwell — LUFS -8, EDM festival, drops massifs",
    "hard dance":                "Coone, Da Tweekaz, Brennan Heart — LUFS -7/-8, energie brute, kicks distordus",
    "neo rave":                  "Shygirl, Absolute Valentine, Myd — LUFS -8, fusion rave moderne et pop",
    "phonk":                     "DJ Smokey, DJ Yung Vamp, Ghostemane — LUFS -8, bass 808, esthetique sombre",
    "future bass":               "Flume, Odesza, DROELOE — LUFS -8/-9, chillwave emotive, bass synths",
    "synthwave":                 "Kavinsky, Carpenter Brut, Perturbator — LUFS -11, esthetique 80s retro futur",
    "downtempo":                 "Massive Attack, Portishead, Tricky — LUFS -13, trip hop sombre, slow groove",
    "jersey club":               "DJ Sliink, TT the Artist, Uniiqu3 — LUFS -8, sampling vocal, drums rapides",
    "afrobeats":                 "Burna Boy, Wizkid, Davido — LUFS -9, afropop moderne, groove africain",
    "industrial techno":         "Surgeon, Blawan, Phase Fatale — LUFS -8/-9, distorsion metallique, kicks satures",
    "dub techno":                "Basic Channel, Deepchord, Monolake — LUFS -11/-12, reverb infinie, sub profond",
    "minimal techno":            "Richie Hawtin, Ricardo Villalobos, Plastikman — LUFS -10/-11, minimalisme radical",
    "organic house":             "Fur Coat, Sabo, Stimming — LUFS -11, instruments acoustiques, chaleur naturelle",
    "progressive house":         "deadmau5, Eric Prydz, Sasha — LUFS -10, builds lents, euphorie progressive",
    "melodic house":             "Kidnap, Lane 8, Tycho — LUFS -11, melodies douces, atmosphere positive",
    "amapiano":                  "DJ Maphorisa, Kabza De Small, MFR Souls — LUFS -9, log drum, groove sud-africain",
    "dnb":                       "Andy C, Chase Status, Sub Focus — LUFS -8, sub halftempo, breaks intenses",
    "liquid dnb":                "High Contrast, Logistics, Nu:Tone — LUFS -9, melodique et fluide, atmosphere positive",
    "uk garage":                 "Craig David, MJ Cole, So Solid Crew — LUFS -9, 2-step groove, voix syncopees",
    "boom bap":                  "DJ Premier, Pete Rock, 9th Wonder — LUFS -10, samples vinyle, kicks et snares live",
    "hardstyle":                 "Headhunterz, Noisecontrollers, Brennan Heart — LUFS -7, kicks distordus, melodie trance",
    "tribal house":              "Louie Vega, Kenny Dope, Kerri Chandler — LUFS -10, percussions africaines, groove tribal",
    "bass club":                 "Mumdance, Logos, Wen — LUFS -8, bass music UK underground, hybride experimental",
    "grime":                     "Skepta, Dizzee Rascal, Wiley — LUFS -8, 140 BPM, instrumentaux froids et metalliques",
    "rnb":                       "Frank Ocean, SZA, Bryson Tiller — LUFS -9/-10, voix veloutee, production moderne",
    "hardcore":                  "Angerfist, Endymion, Drokz — LUFS -7, kicks satures, distorsion totale",
    "rock":                      "Foo Fighters, Arctic Monkeys, Queens of the Stone Age — LUFS -10, guitares saturees",
    "jazz":                      "Miles Davis, John Coltrane, Herbie Hancock — LUFS -14/-16, dynamique naturelle",
    "soul":                      "Aretha Franklin, Marvin Gaye, Al Green — LUFS -11, voix expressives, cuivres chauds",
    "funk":                      "James Brown, Parliament, Sly Stone — LUFS -10, groove rythmique, basse syncopee",
    "reggae":                    "Bob Marley, Lee Scratch Perry, Burning Spear — LUFS -11, riddim, reverb dub",
}

# ─────────────────────────────────────────────────────────────────────────────
# BASE DE DONNÉES CONTEXTUELLE PAR GENRE
# Donne à Claude le savoir métier pour un coaching vraiment professionnel
# ─────────────────────────────────────────────────────────────────────────────
PROFILS_CONTEXTE = {

    "techno": {
        "son": "Groove industriel, hypnotique et sombre. Kick sec et percutant, ligne de basse répétitive, textures métalliques. L'espace et le silence sont aussi importants que les sons.",
        "kick": "Kick court et percutant, corps entre 80-100Hz, attaque rapide (<5ms), très peu de queue. Le kick EST le centre du mix — tout doit lui laisser de la place via sidechain.",
        "bass": "Sub mono strict sous 80Hz, basse mid-rangey entre 80-200Hz. Peu de chaleur, plutôt froide et mécanique. La basse ne doit jamais déborder en stéréo.",
        "stereo": "Sub et basses : mono absolu. Mids : légèrement stéréo pour les éléments de texture. Aigus et hi-hats : stéréo ouverte. Éviter le mid-side excessif qui fragilise le mix en club.",
        "compression": "Sidechain pumping assumé et rythmique (ratio 4:1 minimum, release calé sur le BPM). Kick traité avec transient shaper pour plus de punch. Mix global avec peu de compression, on préserve la dynamique club.",
        "reverb": "Réverbes courtes et industrielles sur les percussions. Longues réverbes sur les éléments d'atmosphère en fond. Jamais de reverb sur le kick ni sur les éléments de bass.",
        "eq_master": "High-pass global à 30Hz pour nettoyer le sub. Légère coupure autour de 300-400Hz pour éviter le boxy. Boost subtil de 8-12kHz pour l'air et la brillance.",
        "erreurs": [
            "Kick trop mou ou trop grave — manque de punch au-dessus de 90Hz",
            "Basses trop larges en stéréo — catastrophique sur les systèmes club",
            "Mix trop compressé et étouffé — la techno respire et a de la dynamique",
            "Trop de reverb sur le kick — nuit à la clarté rythmique",
            "LUFS trop bas pour le club — en dessous de -11 LUFS le mix semble mou",
        ],
        "contexte": "Clubs, festivals outdoor, sets de 6-8h. Systèmes RCF, d&b Audiotechnik, Funktion-One. Le mix doit tenir 8 heures sans fatiguer les oreilles. Priorité absolue : la grosse caisse et la basse.",
        "arrangement": "Intro très longue (3-5 min) pour le mix DJ, montée progressive par ajout de couches, breakdown minimaliste, drop discret mais puissant, outro longue (2-3 min). Pas de structure couplet/refrain.",
        "mastering": "Limiter à -9 LUFS intégré, True Peak -0.3 dBTP. Utilisation fréquente du M/S EQ pour garder les graves mono. Saturation analogique légère pour la chaleur.",
        "plateformes": "Beatport (priorité), Bandcamp, SoundCloud. Rarement Spotify — le genre est orienté clubs et vinyles.",
    },

    "melodic techno": {
        "son": "Fusion entre techno industrielle et musique électronique atmosphérique. Synthés pad ambiants, lignes de mélodie émotionnelles, reverbs longues, kick maintenu mais moins agressif que la techno pure.",
        "kick": "Kick plus rond que la techno classique, corps entre 70-90Hz, légère queue pour la profondeur. Moins percutant, plus enveloppant. Sidechain présent mais moins agressif.",
        "bass": "Basse plus mélodique et harmonique. Sub mono, mais la mid-bass peut avoir un peu plus de chaleur et de corps. Parfois une basse synthétique qui joue une ligne mélodique.",
        "stereo": "Image stéréo plus large que la techno classique. Les pads et atmosphères s'étendent largement en stéréo. Sub reste mono. L'espace stéréo est une caractéristique clé du genre.",
        "compression": "Sidechain subtil et musical, pas agressif. Compression douce sur le mixbus pour coller les éléments ensemble. Les dynamiques naturelles sont préservées pour l'émotion.",
        "reverb": "Longues réverbes sur les synthés et pads (jusqu'à 4-6 secondes). Reverb sur les percussions légères. Delay tempo-syncé sur les éléments mélodiques. La profondeur est essentielle.",
        "eq_master": "Peu de coupe dans les mids pour laisser les éléments mélodiques s'exprimer. Boost léger des hauts-mids pour la brillance des synthés. Contrôle soigné des basses pour éviter la boue.",
        "erreurs": [
            "Trop de reverb qui noie la clarté rythmique",
            "Éléments mélodiques trop forts par rapport aux éléments rythmiques",
            "Image stéréo trop large qui crée des problèmes de mono compatibility",
            "LUFS trop chaud qui écrase la dynamique émotionnelle du morceau",
            "Manque de contraste entre les sections intenses et les breakdowns",
        ],
        "contexte": "Afterlife, Cercle, Diynamic. Écoute en club mais aussi en headphones. Le genre est autant émotionnel que physique. Labels comme Afterlife, Anjunadeep, Kompakt.",
        "arrangement": "Structure émotionnelle avec montées et descentes progressives. Breakdown long et atmosphérique (2-3 min), puis drop émotionnel. L'arc narratif est crucial.",
        "mastering": "LUFS -10/-11, préserver la dynamique. True Peak -1.0 dBTP. Soigner particulièrement la clarté dans les hauts-mids pour les éléments mélodiques.",
        "plateformes": "Beatport, Spotify (le genre streame bien), SoundCloud. Présence forte sur YouTube/Mixcloud via les sets Cercle.",
    },

    "hard techno": {
        "son": "Techno extrême et agressive. Kick distordu et saturé, basses acides et percutantes, textures industrielles dures. Le clipping contrôlé est une esthétique assumée.",
        "kick": "Kick massif et distordu, souvent saturé intentionnellement. Corps 80-120Hz très présent. L'overdrive sur le kick est une signature du genre. Attaque ultra-rapide.",
        "bass": "Basse acide (souvent TB-303), distorsion et saturation assumées sur la basse. Sub très court et percutant. La basse est plus percussive que mélodique.",
        "stereo": "Mix généralement mono ou très peu stéréo sur les éléments rythmiques. La dureté du genre vient aussi de sa nature mono et centrée.",
        "compression": "Compression très agressive, souvent jusqu'à l'écrasement total. Limiteur au master poussé fort. Le clipping léger est artistique dans ce genre.",
        "reverb": "Très peu de reverb — le genre est sec et dur. Quelques effets métalliques sur les percussions légères. Delay court et rhythmique parfois utilisé.",
        "eq_master": "Boost fort des sub (30-60Hz) pour l'impact physique. Coupure marquée dans les mids pour éviter la boue. Les hauts-mids agressifs sont voulus.",
        "erreurs": [
            "Pas assez de distorsion/saturation — le genre assume la couleur sale",
            "Kick trop propre et clinique — il doit avoir de la gueule et de l'agressivité",
            "LUFS trop bas — le hard techno joue à -7/-8 LUFS minimum",
            "Trop de dynamique — le genre est compressé et intense",
        ],
        "contexte": "Raves, festivals underground, afterparties. Vitesse 140-160 BPM. Labels comme Filth on Acid, Partyfine. Publics très spécifiques et connaisseurs.",
        "arrangement": "Structure simple et directe. Peu de breakdowns complexes. L'énergie est maintenue haute en permanence. Build-ups via des filtres et des effets de distorsion.",
        "mastering": "LUFS -7/-8, True Peak peut aller jusqu'à -0.1 dBTP. La saturation légère au master est acceptable et même recherchée dans ce genre.",
        "plateformes": "Beatport, Bandcamp, SoundCloud. Rarement sur les grandes plateformes de streaming — genre très niché.",
    },

    "house": {
        "son": "Groove chaleureux et positif. Four-on-the-floor, ligne de basse chaleureuse, piano ou chords soul, voix sampling. La maison doit faire danser et sourire.",
        "kick": "Kick chaud et profond, corps entre 60-80Hz, légère queue pour la profondeur. Moins sec que la techno, plus organique. Le kick doit avoir de l'âme.",
        "bass": "Ligne de basse mélodique et groovante. Warm et ronde entre 80-200Hz. Octave bass classique ou ligne wandering. La basse joue avec le kick, pas contre lui.",
        "stereo": "Image stéréo modérée. Sub mono, basses légèrement stéréo. Chords et pads larges en stéréo pour la chaleur. Plus ouvert que la techno mais pas excessif.",
        "compression": "Sidechain musical qui donne le pompage caractéristique de la house. Compression douce et transparente sur la basse. Glue compressor sur le mixbus pour la cohésion.",
        "reverb": "Réverbes d'ambiance sur les éléments soul (voix, piano). Delay sur les stabs. Le groove doit rester clair et défini malgré l'ambiance.",
        "eq_master": "Chaleur dans les bas-mids (150-250Hz) assumée. Légère brillance dans les hauts-mids pour les voix et les percussions. Pas trop agressif dans les aigus.",
        "erreurs": [
            "Kick trop sec et sans chaleur — la house demande de l'organicité",
            "Ligne de basse monotone sans groove et sans mouvement",
            "Mix trop froid et clinique — la house a de l'âme et de la chaleur",
            "Sidechain trop agressif qui casse le groove naturel",
            "Manque de groove sur les percussions — le timing légèrement swingué est caractéristique",
        ],
        "contexte": "Clubs, terrasses, festivals. Sets de 4-6h. Le genre est plus accessible que la techno — il doit toucher un large public tout en restant dancefloor.",
        "arrangement": "Intro 1-2 min, groove qui s'installe progressivement, breakdown soul avec voix/piano, drop avec le groove complet, outro. Structure DJ-friendly.",
        "mastering": "LUFS -10, dynamique préservée pour le groove. True Peak -1.0 dBTP. Soigner la chaleur dans les bas-mids.",
        "plateformes": "Beatport, Spotify, Apple Music, SoundCloud. Le genre streame très bien.",
    },

    "deep house": {
        "son": "House introspective et atmosphérique. Tempos plus lents, basses profondes et organiques, chords jazz/soul, percussions discrètes, voix murmurées. L'ambiance prime sur l'énergie.",
        "kick": "Kick très doux et enveloppant, corps entre 50-70Hz, queue longue et naturelle. Presque comme un kick de jazz ou de soul. Très peu d'attaque transiente.",
        "bass": "Basse profonde, ronde et chaleureuse. Corps entre 60-150Hz. Souvent un sub très présent avec peu de mids. La basse respire et a de l'espace.",
        "stereo": "Stéréo modérément large pour les éléments d'ambiance. Sub et basses en mono. Les chords et atmosphères peuvent être très larges pour créer l'immersion.",
        "compression": "Très peu de compression — le genre respire. Sidechain subtil voire absent. La dynamique naturelle est une valeur essentielle de la deep house.",
        "reverb": "Longues réverbes ambiantes sur tous les éléments. Le genre vit dans la réverb. Delay tempo-syncé sur les voix et les éléments mélodiques.",
        "eq_master": "Chaleur prononcée dans les bas-mids. Peu de brillance excessive. L'objectif est un son chaud, organique et non fatigant.",
        "erreurs": [
            "Trop de compression qui enlève la respiration naturelle du groove",
            "Kick trop percutant qui casse l'atmosphère introspective",
            "Manque de profondeur et d'espace dans le mix",
            "LUFS trop élevé pour un genre qui vit de sa dynamique",
            "Trop de brillance — le genre est doux et chaleureux, pas brillant",
        ],
        "contexte": "Bars, lounges, terrasses, écoute au casque. Labels comme Innervisions, Defected, Kompakt. Le genre peut aussi vivre en dehors du dancefloor.",
        "arrangement": "Évolution très lente et subtile. Les éléments arrivent et partent doucement. Peu de drops marqués. L'émotion vient de l'accumulation progressive.",
        "mastering": "LUFS -12/-13, vraie dynamique préservée. True Peak -1.0 dBTP. Soigner la chaleur et la profondeur.",
        "plateformes": "Spotify, Apple Music, SoundCloud, Beatport. Très bien streamé — genre accessible et apprécié hors clubs.",
    },

    "drum and bass": {
        "son": "Breaks halftempo avec sub-bass dominant et breaks frénétiques. L'opposition physique entre le sub lent (halftempo) et les drums rapides (170 BPM) est l'essence du genre.",
        "kick": "Kick en halftempo (85 BPM ressenti), corps très fort entre 60-80Hz, sub qui bouge la salle. Le kick et la basse fonctionnent ensemble comme une seule unité.",
        "bass": "Sub bass halftempo dominant, très présent entre 40-80Hz, avec des mouvements expressifs (dips, swells, wobbles). La basse EST le protagoniste du morceau. Mono strict en dessous de 100Hz.",
        "stereo": "Sub et bass : mono absolu. Breaks : stéréo large pour l'énergie. Éléments atmosphériques : très larges. Le contraste mono/stéréo est un outil de construction.",
        "compression": "Transient shaping agressif sur les breaks pour le punch. Sidechain de la basse sur le kick. Limiteur master à -8 LUFS. Les transiantes des breaks doivent claquer fort.",
        "reverb": "Reverb sur les breaks pour l'ambiance (snare room, overhead). Peu de reverb sur la basse — elle doit rester propre et définie. Delays sur les éléments mélodiques.",
        "eq_master": "Sub très présent (40-80Hz). Coupure dans les 200-300Hz pour la clarté. Boost des hauts-mids pour que les breaks coupent fort. Brillance sur les cymbales.",
        "erreurs": [
            "Sub basse trop courte ou trop timide — c'est l'âme du genre",
            "Breaks sans punch ni caractère — ils doivent claquer comme des coups de fusil",
            "Basse trop large en stéréo — catastrophique pour le rendu club",
            "BPM mal détecté — le DnB est à 160-180 BPM mais ressenti en halftempo",
            "Manque de contraste dynamique entre les sections calmes et les drops",
        ],
        "contexte": "Clubs spécialisés DnB, raves. Labels Metalheadz, Ram Records, Hospital Records, Shogun Audio. Système de sound avec beaucoup de sub.",
        "arrangement": "Intro avec break atmosphérique, premier drop de basse, deuxième drop plus intense, rouleau final. Structure en 2 ou 3 sections principales.",
        "mastering": "LUFS -8, très dynamique sur les breaks. True Peak -0.3 dBTP. Soigner le sub pour qu'il soit propre et puissant.",
        "plateformes": "Beatport, Spotify, SoundCloud. Labels digitaux et vinyles.",
    },

    "hip-hop": {
        "son": "Production basée sur les samples, loops et beatmaking. Le boom bap classique vs le trap moderne. L'émotion vient autant du choix des sons que de la technique.",
        "kick": "Kick punchy et court, corps entre 80-120Hz, attaque transiente forte. Souvent un kick de boîte à rythmes (808, SP-1200) ou un sample de batterie naturelle traité.",
        "bass": "808 sub ou basse samplée, chaleur entre 80-150Hz. La basse et le kick doivent être complémentaires en fréquence. Le sub 808 est souvent la note la plus grave du morceau.",
        "stereo": "Image stéréo modérée. Le kick et la basse en mono. Les samples et atmosphères peuvent être plus larges. La voix est centrale (mono ou légèrement stéréo).",
        "compression": "Compression vintage sur les drums pour la couleur (style SSL, API). Saturation de bande sur le mixbus pour la chaleur. La voix est souvent très compressée pour la présence.",
        "reverb": "Reverb de room sur les drums pour la dimension. Reverb et delay sur la voix selon le style. Le boom bap utilise plus de reverb que le trap.",
        "eq_master": "Warmth dans les bas-mids (150-300Hz) pour la chaleur vintage. Présence dans les hauts-mids pour la voix. Coupure des sub très bas (<30Hz) pour la propreté.",
        "erreurs": [
            "Kick sans punch — le kick hip-hop doit frapper fort dans les mids",
            "808 qui entre en conflit avec le kick — il faut une complémentarité fréquentielle",
            "Sample non clearé ou mal traité qui trahit un manque de maîtrise technique",
            "Voix mal positionnée dans le mix — elle doit s'imposer naturellement",
            "Mix trop propre et clinique — le hip-hop a de la texture et du grain",
        ],
        "contexte": "Streaming (Spotify, Apple Music prioritaires), radio, car audio. Le mix doit être évalué sur toutes les surfaces d'écoute, y compris les enceintes de voiture.",
        "arrangement": "Intro 4-8 bars, couplet, hook/refrain, couplet, hook, bridge, outro. La structure est plus traditionnelle que l'électro.",
        "mastering": "LUFS -9/-10 pour le streaming. True Peak -1.0 dBTP. Équilibre entre chaleur vintage et clarté moderne.",
        "plateformes": "Spotify, Apple Music, Tidal, YouTube. Le streaming est la distribution principale.",
    },

    "trap": {
        "son": "808 sub-bass dominant, hi-hats triplés rapides, snare reverberant. Production cinématique et sombre. L'808 est la star du mix.",
        "kick": "Kick court et claquant, fréquemment une boîte à rythmes type TR-808. Corps entre 80-100Hz. Le kick et l'808 doivent se compléter — souvent sidechainés ensemble.",
        "bass": "808 qui couvre tout le spectre des graves — sub profond (40-60Hz) jusqu'aux mids (200Hz) avec le slide caractéristique. L'808 EST le son signature du genre. Accordé dans la tonalité du morceau.",
        "stereo": "808 strictement mono. Hi-hats et éléments percussifs larges en stéréo. Atmosphères et pads très larges. Le contraste crée l'espace.",
        "compression": "808 très peu compressé pour préserver le sustain et le punch. Hi-hats compressés pour la cohésion. Limiteur master agressif (-7/-8 LUFS).",
        "reverb": "Reverb longue sur la snare (caractéristique trap). Delay et reverb sur les mélodies et atmosphères. Peu ou pas sur l'808 pour garder sa pureté.",
        "eq_master": "808 sub très présent entre 40-80Hz. Coupure dans les 200-400Hz pour éviter la boue de l'808. Hi-hats brillants entre 8-16kHz.",
        "erreurs": [
            "808 trop court — il doit avoir du sustain et du mouvement",
            "808 pas accordé dans la tonalité du morceau — erreur critique",
            "Hi-hats trop plats et sans swing — le groove trap vient du timing des hi-hats",
            "808 en stéréo — il doit être strictement mono pour le punch",
            "Mix trop brillant qui masque la profondeur grave du genre",
        ],
        "contexte": "Streaming, radio, playlists. Collaboration avec des rappeurs. Les producteurs trap ont souvent leur signature sonore propre.",
        "arrangement": "Intro atmosphérique, puis arrivée de l'808 et du kick, hook avec mélodie, couplet plus sparse, hook final. Plus court que l'électro (2-3 min typiquement).",
        "mastering": "LUFS -8/-9. True Peak -1.0 dBTP. L'808 doit être fort et propre — c'est la priorité absolue.",
        "plateformes": "Spotify, Apple Music, YouTube, Tidal. Streaming en priorité absolue.",
    },

    "ambient": {
        "son": "Musique atmosphérique et contemplative. L'espace, le silence et la texture sont les matériaux principaux. Pas de structure rythmique conventionnelle. L'auditeur est immergé dans un paysage sonore.",
        "kick": "Absent ou très discret. Si présent, c'est une texture grave plutôt qu'un kick de danse. La pulsation est implicite, pas explicite.",
        "bass": "Sub très léger voire absent. Les fréquences graves viennent de drones et de pads. Pas de ligne de basse définie. Le grave est atmosphérique.",
        "stereo": "Image stéréo très large — c'est une caractéristique fondamentale. Les éléments se déplacent dans l'espace stéréo. Le binaural processing est souvent utilisé.",
        "compression": "Aucune compression dynamique conventionnelle. Le limiteur master est très doux. La dynamique naturelle peut aller de très doux à très fort sans limite.",
        "reverb": "Réverbes infinies et espaces acoustiques immenses. La réverb EST la matière sonore de l'ambient. Convolution reverb avec de vrais espaces (cathédrales, caves, forêts).",
        "eq_master": "Peu d'EQ correctif. L'accent est mis sur les textures naturelles. Attention à ne pas trop couper les sub — les fréquences graves très basses sont parte de l'expérience.",
        "erreurs": [
            "Mix trop compressé qui tue la dynamique émotionnelle",
            "Éléments trop mis en avant qui cassent l'immersion",
            "Réverbes trop courtes qui donnent un sentiment claustrophobique",
            "LUFS trop élevé — l'ambient doit respirer et avoir une vraie dynamique",
            "Stéréo pas assez large — l'immersion spatiale est fondamentale",
        ],
        "contexte": "Écoute au casque, méditation, concentration. Labels Warp, Ninja Tune, Deutsche Grammophon. Brian Eno est la référence absolue. Streaming et vinyles.",
        "arrangement": "Évolution très lente sur 5-15 minutes. Pas de structure conventionnelle. Les éléments émergent et disparaissent comme des nuages. La narrative est émotionnelle et non rythmique.",
        "mastering": "LUFS -16/-18 — laisser la dynamique naturelle. True Peak -1.0 dBTP. Ne pas sur-limiter — la dynamique est une valeur artistique.",
        "plateformes": "Spotify, Apple Music, Bandcamp, YouTube. Le genre streame bien pour la concentration et le travail.",
    },

    "trance": {
        "son": "Énergie montante et libératrice. Synthés lead émotionnels et mélodiques, arpeggios, breakdowns épiques, drops euphoriques. La montée émotionnelle est la structure du genre.",
        "kick": "Kick puissant et régulier, corps entre 70-90Hz, légèrement plus long que la techno. Il doit porter l'énergie pendant les 8 minutes sans fatiguer.",
        "bass": "Ligne de basse entraînante et mélodique. Souvent un bassline arpeggié qui soutient le lead. Sub propre et mono. La basse est moins dominante que dans la techno ou la house.",
        "stereo": "Stéréo très large — les synthés lead et pads s'étendent largement. Sub et kick en mono. L'image stéréo large est une signature du genre.",
        "compression": "Sidechain pumping prononcé sur les synthés. Compression sur le bus pour l'énergie et la cohésion. Le master est assez chaud pour le dancefloor.",
        "reverb": "Réverbes longues sur le synthé lead (hall). Delays tempo-syncé sur les arpèges. Le breakdown vit de sa réverbe et de ses delays épiques.",
        "eq_master": "Boost des hauts-mids pour la brillance des synthés. Contrôle des bas-mids pour éviter la confusion. Air à 12-16kHz pour l'ouverture.",
        "erreurs": [
            "Synthé lead trop timide ou mal présent dans le mix",
            "Breakdown sans suffisamment de build-up émotionnel",
            "Image stéréo pas assez large — le genre vit de son espace",
            "Manque de progression émotionnelle — la trance doit raconter une histoire",
            "LUFS trop bas pour le dancefloor — la trance joue à -9 LUFS",
        ],
        "contexte": "Festivals (Tomorrowland, EDC), clubs spécialisés. Sets de 2-3h avec des montées épiques. Le public est très réactif aux breakdowns et aux drops.",
        "arrangement": "Intro 1-2 min, groove installation, première montée, breakdown (point culminant émotionnel), drop euphorique, deuxième montée, outro. Structure en M.",
        "mastering": "LUFS -9, True Peak -1.0 dBTP. Soigner la clarté des synthés leads et la largeur stéréo.",
        "plateformes": "Beatport, Spotify, Apple Music. Le genre a une fanbase streaming importante.",
    },

    "lo-fi hip-hop": {
        "son": "Esthétique vintage et chaleureuse. Samples de vinyle avec craquements, boom bap ralenti, piano ou rhodes doux, basse ronde, atmosphère cosy et nostalgique.",
        "kick": "Kick doux et vintage, souvent issu d'un sample vinyle. Corps entre 80-150Hz, peu d'attaque transiente. Il doit sembler imparfait et humain.",
        "bass": "Basse chaude et ronde, souvent une contrebasse ou une basse électrique vintage. Corps entre 80-200Hz avec beaucoup de chaleur. Pas de sub trop fort — la chaleur prime.",
        "stereo": "Stéréo modérée. Les éléments ont souvent un léger déséquilibre L/R intentionnel pour l'effet vintage. Pas d'image stéréo parfaite — c'est voulu.",
        "compression": "Compression vintage douce (style LA-2A, 1176). Saturation de bande pour la chaleur. Le but est de sembler analogique et imparfait.",
        "reverb": "Réverbe de room courte pour le caractère vintage. Peu de reverb digitale propre. L'espace vient de la coloration et des imperfections du son.",
        "eq_master": "Boost prononcé dans les bas-mids (200-400Hz) pour la chaleur. Légère coupure des hauts (>8kHz) pour l'effet vintage. Pas trop de sub.",
        "erreurs": [
            "Mix trop propre et digital — le lo-fi vit de ses imperfections",
            "Manque de craquements vinyle ou de bruit de fond caractéristique",
            "Samples trop cuts et précis — l'humanité vient des timing légèrement off",
            "LUFS trop élevé qui tue l'atmosphère douce du genre",
            "Pas assez de chaleur dans les bas-mids — c'est la signature sonore",
        ],
        "contexte": "Écoute au casque, étude, travail, relaxation. YouTube (lo-fi girl), Spotify playlists, Bandcamp. Le genre existe surtout en streaming.",
        "arrangement": "Boucles de 1-4 minutes, évolution très subtile. Parfois de simples boucles sans structure conventionnelle. L'ambiance prime sur la progression.",
        "mastering": "LUFS -14/-16, laisser la dynamique. True Peak -1.0 dBTP. Le genre n'a pas besoin d'être fort — il doit être doux et chaleureux.",
        "plateformes": "Spotify, YouTube, SoundCloud, Bandcamp. Ecosystème streaming dominant.",
    },

    "pop": {
        "son": "Production moderne et accessible. Voix en avant, hook mémorable, production riche et stratifiée. La chanson prime sur la production — tout sert la mélodie vocale.",
        "kick": "Kick moderne et punchy, souvent traité numériquement. Corps entre 80-120Hz, transiente forte. Le kick soutient la voix sans jamais la dominer.",
        "bass": "Ligne de basse mélodique et simple. Elle soutient l'harmonie et la mélodie. Pas trop dominante — la voix est la star. Chaleur entre 80-200Hz.",
        "stereo": "Image stéréo moderne et large pour les éléments instrumentaux. La voix lead est centrale (mono ou très légèrement stéréo). Les BV (backing vocals) s'étalent largement.",
        "compression": "Compression moderne et transparente. La voix est très compressée pour la présence constante. Mix bus glue compressor. Multi-band parfois au master.",
        "reverb": "Reverb créative sur la voix (hall, plate). Delay tempo-syncé sur les éléments mélodiques. Chaque effet est au service de l'émotion de la chanson.",
        "eq_master": "Hauts-mids présents pour la clarté vocale. Légère brillance à 12kHz pour l'air. Peu de sub excessif — le genre doit sonner sur tous les systèmes.",
        "erreurs": [
            "Voix enterrée ou mal positionnée dans le mix — elle doit dominer",
            "Mix trop dense qui noie les éléments clés",
            "Manque de contraste dynamique entre les couplets et le refrain",
            "True Peak trop élevé — Spotify va normaliser et dégrader",
            "Production trop complexe qui distrait de la mélodie",
        ],
        "contexte": "Toutes les plateformes de streaming, radio, playlists éditoriales. La pop doit sonner bien partout : écouteurs, enceintes Bluetooth, voiture, système home cinéma.",
        "arrangement": "Intro courte, couplet 1, pré-refrain, refrain, couplet 2, pré-refrain, refrain, bridge, refrain final. Structure très codifiée pour la radio.",
        "mastering": "LUFS -9/-10 pour le streaming. True Peak strictement -1.0 dBTP. L'uniformité sur toutes les plateformes est essentielle.",
        "plateformes": "Spotify, Apple Music, Tidal, YouTube, radio. Distribution universelle.",
    },

    # ── Nouveaux genres ──────────────────────────────────────────────────────

    "techno raw deep hypnotic": {
        "son": "Techno hypnotique et atmospherique, moins aggressive que la Peak Time. Groove profond, textures organiques, elements dub et noise, mood introspectif et rituel.",
        "kick": "Kick plus doux et dub, corps entre 70-85Hz, plus de queue que la Peak Time. Le kick cree une pulsation hypnotique plutot qu un coup de poing.",
        "bass": "Sub profond et presque ambient, basses mid-low avec beaucoup de caracterE. Souvent modulees pour creer du mouvement hypnotique.",
        "stereo": "Plus de stereo que la techno Peak Time. Les elements de texture s etendent largement. Le mid-side est exploite pour la profondeur.",
        "compression": "Compression douce et organique. Peu de sidechain agressif. La dynamique naturelle cree la transe.",
        "reverb": "Reverbes longues et industrielles, delays chaotiques, echo infinis. La reverb EST l atmosphere du morceau.",
        "eq_master": "Moins de punch dans les aigus, plus de mid-bass warm. L accent est mis sur la profondeur grave et les textures de mid.",
        "erreurs": ["Trop de compression qui casse la transe", "Manque de profondeur sonore et d espace", "Kick trop percutant qui sort du mood hypnotique"],
        "contexte": "Clubs underground, festivals alternatifs. Labels Delsin, Semantica, Horizontal Ground. Sets en soiree tardive.",
        "arrangement": "Evolue tres lentement, cycles longs, peu de breaks francs. L hypnose vient de la repetition subtile.",
        "mastering": "LUFS -10/-11, dynamique preservee. True Peak -1.0 dBTP.",
        "plateformes": "Beatport, Bandcamp, SoundCloud.",
    },

    "trance raw deep hypnotic": {
        "son": "Cote obscur et underground de la trance. Hypnotique, sombre, rituel. Moins de leads euphoriques, plus de texture et de profondeur. La transe vient de la repetition et des couches.",
        "kick": "Kick regulier et moins agressif que la Main Floor. Corps entre 70-85Hz. Il doit porter la transe sans dominer.",
        "bass": "Basse profonde et atmospherique. Souvent une basse qui bouge lentement sous les kicks. Moins de basses mid-rangey saillantes.",
        "stereo": "Tres large sur les elements atmospheriques. Le mid-side est fondamental pour l immersion.",
        "compression": "Peu de compression conventionnelle. La dynamique naturelle cree la profondeur emotionnelle.",
        "reverb": "Reverbes infinies, delays chaotiques. L espace est immense. La reverb cree la dimension rituelle.",
        "eq_master": "Peu de brillance excessive. Accent sur la profondeur et les midranges atmospheriques.",
        "erreurs": ["Trop commercial et euphorique - perdre le cote sombre et hypnotique", "Manque de profondeur sonore", "Lead trance trop en avant - doit etre plus subtil"],
        "contexte": "Clubs underground, after parties. Labels JOOF, Stripped Digital. Public connaisseur et exigeant.",
        "arrangement": "Builds tres lents et progressifs, breakdowns longs et atmospheriques, drops subtils.",
        "mastering": "LUFS -10/-11. True Peak -1.0 dBTP. Soigner la largeur stereo.",
        "plateformes": "Beatport, Bandcamp.",
    },

    "uplifting trance": {
        "son": "Trance euphorique et emotionnelle. Leads melodiques intenses, breakdowns epiques, drops catharctiques. L emotion est poussee a son maximum. C est la pop de l electronique.",
        "kick": "Kick puissant et regulier, corpo entre 70-90Hz. Il doit soutenir l energie pendant les 8 minutes.",
        "bass": "Basse arpegiee et melodique, tres presente dans les sections energiques. Sub propre et fort.",
        "stereo": "Tres large sur les synthes leads et les pads. Le champ stereo large est une signature.",
        "compression": "Sidechain pumping prononce. Compression de bus pour la cohesion energetique.",
        "reverb": "Hall immense sur le lead. Delays tempo-syncs sur les arpages. Le breakdown vit de ses reverbes epiques.",
        "eq_master": "Boost des hauts-mids pour les synthes leads. Beaucoup d air a 12-16kHz. Controle soigne des bas-mids.",
        "erreurs": ["Lead pas assez present - il doit dominer le mix", "Breakdown pas assez emotionnel", "Pas assez de stereo sur les elements atmospheriques"],
        "contexte": "Festivals trance (A State of Trance, Tomorrowland), clubs specialises. Labels Armin van Buuren, Enhanced, Black Hole.",
        "arrangement": "Structure en M: montee, breakdown epique (moment de catharsis), drop euforique, deuxieme montee, outro.",
        "mastering": "LUFS -9. True Peak -1.0 dBTP. Les synthes leads doivent briller.",
        "plateformes": "Beatport, Spotify, Apple Music. La trance uplifting streame bien.",
    },

    "jackin house": {
        "son": "House percussive et fonky, grooves syncopees, bass lines rebondissantes, samples soul, energie directe et sans pretention. Le groove avant tout.",
        "kick": "Kick court et claquant, tres percussif. Corps entre 80-100Hz, tres peu de queue. Il doit faire danser immediatement.",
        "bass": "Basse rebondissante et rythmique, souvent syncopee. Corps chaleureux entre 100-200Hz. La basse groove avec le kick.",
        "stereo": "Moderee. Le kick et la basse en mono strict. Elements funky legeremet stereo.",
        "compression": "Sidechain tres present et musical. Compression vintage sur la basse pour le groove. Le pumping est une signature.",
        "reverb": "Peu de reverb - le genre est sec et direct. Quelques effets sur les stabs soul.",
        "eq_master": "Warmth dans les mids pour le groove. Peu de brillance excessive. L accent est mis sur le punch et le groove.",
        "erreurs": ["Trop propre et clinique - le jackin house a du caractere", "Groove trop regulier - il faut de la syncope", "Manque d elements soul et funky"],
        "contexte": "Clubs Chicago, dancefloors underground. Labels Cajual, Relief Records, Sneak Recordings.",
        "arrangement": "Minimaliste, evolue par couches, groove qui s installe progressivement.",
        "mastering": "LUFS -10. True Peak -1.0 dBTP.",
        "plateformes": "Beatport, Traxsource, SoundCloud.",
    },

    "bass house": {
        "son": "House avec des basses massives et agressives. Mix entre tech house et dubstep. Grooves house mais avec des basses qui font trembler les murs.",
        "kick": "Kick puissant et percutant, corps entre 80-100Hz, transiente forte. Il doit rivaliser avec les basses lourdes.",
        "bass": "Basses massives, souvent distordues ou saturees. Sub lourd entre 40-80Hz avec une basse mid-range prononcee. Les basses sont les stars.",
        "stereo": "Sub strict mono. Les basses mid-range peuvent etre legerement stereo. Elements de synth larges.",
        "compression": "Sidechain agressif. Compression forte sur la basse. Limiteur master pousse.",
        "reverb": "Peu de reverb - le genre est sec et direct. Quelques effets sur les synthes.",
        "eq_master": "Sub tres present. Coupure marquee dans les 200-400Hz pour la clarte. Boost des transitoires.",
        "erreurs": ["Basses pas assez lourdes ou pas assez presentes", "Mix trop propre - le genre a une certaine brutalite", "Sidechain pas assez present"],
        "contexte": "Festivals, clubs, mainstage. Labels Night Bass, Insomniac, Confession.",
        "arrangement": "Drops massifs apres des builds tendus. Structure directe et percutante.",
        "mastering": "LUFS -8/-9. True Peak -0.3 dBTP. Les basses doivent etre propres et puissantes.",
        "plateformes": "Beatport, Spotify, SoundCloud.",
    },

    "neurofunk": {
        "son": "DnB futuriste et experimentale. Sons robotiques et metalliques, basses complexes et evoluees, breaks intenses, textures cerebrales. Le genre le plus technique du DnB.",
        "kick": "Kick court et metallique, transiente ultra-rapide. Corps entre 80-100Hz, tres peu de sustain. Il doit claquer comme une machine.",
        "bass": "Basses neurofunk caracteristiques - sons robotiques complexes, modulations LFO rapides, textures metalliques. La basse EST la complexite du genre.",
        "stereo": "Sub strict mono. Les elements de texture et les basses complexes peuvent etre stereo. Les breaks sont larges.",
        "compression": "Transient shaping agressif sur les breaks. Compression forte pour le punch. Le genre est tres compresse.",
        "reverb": "Peu de reverb naturelle - le genre est sec et cyberpunk. Des delays courts et metalliques.",
        "eq_master": "Boost marque des hauts-mids pour la metallicite. Sub propre et fort. Coupure dans les 200-400Hz.",
        "erreurs": ["Sons trop conventionnels - le neurofunk demande des textures sonores uniques", "Breaks sans punch ni caractere", "Basses pas assez elaborees ou trop simples"],
        "contexte": "Clubs DnB specialises, festivals underground. Labels Subtitles, Ram Records, Metalheadz.",
        "arrangement": "Deux drops principaux tres intenses avec un breakdown technique au milieu.",
        "mastering": "LUFS -8. True Peak -0.3 dBTP.",
        "plateformes": "Beatport, Bandcamp, SoundCloud.",
    },

    "mainstage": {
        "son": "EDM grand public et spectaculaire. Drops massifs, builds euphoriques, synthes larges, energy maximale pour 50 000 personnes. Priorite a l impact emotionnel immediat.",
        "kick": "Kick massif et percutant, corps entre 80-120Hz, distorsion controlee. Il doit faire vibrer le festival.",
        "bass": "Basses epaisses et distordues pour les drops. Sub puissant. La basse sert le drop, pas une ligne continue.",
        "stereo": "Image stereo tres large sur les synthes et les pads. Tout est grand et immense.",
        "compression": "Sidechain pumping tres marque. Compression de bus forte. Le master est tres chaud.",
        "reverb": "Reverbe de grande salle sur les elements de build. Le drop est plus sec pour l impact.",
        "eq_master": "Hauts-mids brillants pour les synthes leads. Sub fort pour l impact physique. Master tres chaud.",
        "erreurs": ["Drop pas assez massif", "Build-up pas assez long ou pas assez euphorique", "Mix pas assez large et immense"],
        "contexte": "Festivals (Tomorrowland, EDC, Ultra). Labels Revealed, STMPD, Musical Freedom.",
        "arrangement": "Intro courte, build long et euphorique, drop massif, deuxieme drop encore plus gros, outro.",
        "mastering": "LUFS -8. True Peak -0.3 dBTP. Tres chaud et impactant.",
        "plateformes": "Spotify, Apple Music, YouTube, Beatport. Tres grand public.",
    },

    "electronica": {
        "son": "Musique electronique experimentale et artistique. Pas de contraintes de dancefloor. Sons abstraits, structures atypiques, textures cerebrales. L art prime sur la fonction.",
        "kick": "Optionnel et souvent remplace par des elements rythmiques abstraits. Si present, il est traite comme un element sonore parmi d autres.",
        "bass": "Basses experimentales et texturales. Peuvent aller du sub profond aux textures aigues selon la vision artistique.",
        "stereo": "Tres large et experimental. Le mid-side est un outil artistique fondamental.",
        "compression": "Aucune contrainte. La compression est utilisee comme effet artistique ou pas du tout.",
        "reverb": "Espaces acoustiques experimentaux. Convolution avec des impulse responses inhabituelles.",
        "eq_master": "Pas de standard - l EQ suit la vision artistique. Peut etre tres atypique.",
        "erreurs": ["Trop conventionnel pour ce genre - l electronica permet tout", "Manque de vision artistique claire", "Production trop propre et sans personnalite"],
        "contexte": "Labels Warp, Ninja Tune, Hyperdub, Planet Mu. Ecoute attentive, concerts. Pas de contrainte dancefloor.",
        "arrangement": "Libre et experimental. Peut durer 1 minute ou 20 minutes.",
        "mastering": "LUFS variable selon la vision. True Peak -1.0 dBTP.",
        "plateformes": "Bandcamp, Spotify, Apple Music. Genre d ecoute attentive.",
    },

    # ── Techno variants ──────────────────────────────────────────────────────
    "techno peak time": {
        "son": "Techno driving et percutante pour le pic de soiree. Kicks massifs, basslines repetitives, energie maximale maintenue sur la longueur.",
        "kick": "Kick tres percutant et court, corps entre 80-100Hz, attaque ultra-rapide. Doit maintenir l energie pendant 8h de set.",
        "bass": "Basse mid-range repetitive et hypnotique. Sub strict mono sous 80Hz. Moins de variation que la techno melodique.",
        "stereo": "Mix globalement mono sur les elements rythmiques. Quelques elements de texture en stereo pour la profondeur.",
        "compression": "Sidechain agressif et rythmique. Kick tres compresse pour tenir a fort volume. Limiteur master pousse.",
        "reverb": "Reverbes courtes et metalliques. Quelques delays courts sur les hi-hats. Kick et basse toujours secs.",
        "eq_master": "High-pass a 30Hz. Boost leger dans les mids hauts (3-5kHz) pour la presence. Controle des 200-400Hz.",
        "erreurs": ["Kick pas assez percutant pour tenir a fort volume", "Mix trop stereo qui fragilise le rendu club", "LUFS trop bas pour le dancefloor peak time"],
        "contexte": "Festivals, clubs peak time, sets apres minuit. Drumcode, Afterlife, Soma. Systemes Funktion-One et d&b.",
        "arrangement": "Intro longue 3-4 min, energie maintenue haute, peu de breakdowns, transitions subtiles.",
        "mastering": "LUFS -8/-9. True Peak -0.3 dBTP. Maximum de punch et de coherence.",
        "plateformes": "Beatport prioritaire, Bandcamp, SoundCloud.",
    },
    "industrial techno": {
        "son": "Techno extreme et brutaliste. Inspiree de la musique industrielle et du bruit. Textures metalliques, distorsions, ambiances sombres et menaçantes.",
        "kick": "Kick massif et souvent distordu ou sature. Corps entre 60-100Hz tres present. La saturation est une esthetique recherchee.",
        "bass": "Basses lourdes et distordues, souvent du bruit colore plutot qu une basse tonale. Sub mono tres present.",
        "stereo": "Mix souvent dense et mono sur les elements rythmiques. Textures industrielles peuvent s etendre en stereo.",
        "compression": "Compression extreme et limiteur tres pousse. La distorsion et la saturation sont volontaires.",
        "reverb": "Espaces metalliques et industriels. Reverbes de salle d usine ou de bunker. Delays chaotiques.",
        "eq_master": "Boost marque des frequences agressives (1-4kHz). Sub lourd. Hauts-mids coupants intentionnellement.",
        "erreurs": ["Trop propre et clinique - le genre assume la salet sonore", "Pas assez d agressivite et de textures industrielles"],
        "contexte": "Clubs underground tres specifiques, festivals experimentaux. Berghain, Tresor. Public niché et exigeant.",
        "arrangement": "Structures repetitives et hypnotiques. Peu de variation, l atmosphere prime sur la structure.",
        "mastering": "LUFS -8/-9. True Peak peut depasser -0.3 dBTP intentionnellement.",
        "plateformes": "Bandcamp, SoundCloud underground. Tres peu sur Beatport mainstream.",
    },
    "dub techno": {
        "son": "Fusion entre techno minimaliste et dub jamaicain. Sub profond, reverbes infinies, echos repetitifs, groove hypnotique et melancolique. L espace et le silence sont fondamentaux.",
        "kick": "Kick dub - plus doux et plus grave que la techno classique. Corps entre 60-80Hz avec une queue naturelle. Presque comme une pulsation cardiaque.",
        "bass": "Sub profond et modulé avec de lentes variations LFO. Très peu de mid-bass agressive. La basse est atmospherique.",
        "stereo": "Image stereo large sur les reverbes et delays. Les elements rythmiques restent plus centres. L espace stereo est exploite pour la profondeur.",
        "compression": "Tres peu de compression dynamique. La dynamique naturelle cree l atmosphere. Les reverbes et delays sont le traitement principal.",
        "reverb": "Reverbes infinies caracteristiques du dub - tres longues queues. Delays avec feedback eleve. L echo est la signature du genre.",
        "eq_master": "Sub tres present et profond. Coupure des frequences trop agressives. L objectif est un son chaud, profond et melancolique.",
        "erreurs": ["Pas assez de reverbe et d espace - le dub techno vit dans l echo", "Sub pas assez profond et present", "Trop de frequences agressives qui cassent l atmosphere"],
        "contexte": "Labels Basic Channel, Chain Reaction, Echospace. Ecoute au casque et en club, ambiance tardive et introspective.",
        "arrangement": "Cycles tres longs, evolution lente, apparition et disparition progressive des elements. Pas de structure conventionnelle.",
        "mastering": "LUFS -11/-12. Dynamique large preservee. True Peak -1.0 dBTP.",
        "plateformes": "Bandcamp, SoundCloud, Beatport minimal.",
    },
    "minimal techno": {
        "son": "Techno reduite a l essentiel. Chaque son a une raison d etre. La repetition subtile et les micro-variations creent l hypnose. Moins c est plus.",
        "kick": "Kick minimaliste et precis. Corps entre 70-90Hz, ni trop court ni trop long. Il doit instiller la transe par sa regularite impeccable.",
        "bass": "Basse minimaliste et repetetive. Sub mono sous 80Hz. Peu de variation, mais chaque variation est intentionnelle et signifiante.",
        "stereo": "Mix globalement etroit et concentre. L espace stereo est utilise avec parcimonie - chaque element stereo a du sens.",
        "compression": "Compression tres transparente. Sidechain subtil et musical. La dynamique naturelle est valorisee.",
        "reverb": "Reverbes tres courtes et precises. Quelques delays rythmes. L espace acoustique est minimal et controle.",
        "eq_master": "EQ tres precis et chirurgical. Chaque frequence est considered. High-pass a 30Hz, coupures chirurgicales dans les mids.",
        "erreurs": ["Trop d elements qui chargent le mix - le minimal doit respirer", "Manque de precision rythmique - le timing est crucial", "Trop de reverbe qui alourdit le mix"],
        "contexte": "Labels Perlon, Kompakt, M_nus. Clubs berlinois, public tres connaisseur. Sets tres longs.",
        "arrangement": "Evolution extremement lente sur 10-15 minutes. Micro-variations subtiles. La patience est une valeur artistique.",
        "mastering": "LUFS -10/-11. Dynamique preservee. Precision chirurgicale.",
        "plateformes": "Beatport minimal, Bandcamp, distribution tres selective.",
    },
    "acid techno": {
        "son": "Techno avec TB-303 dominant. Le son acide caracteristique - slides, glides, accent - est la signature. Groove hypnotique et acide, energie warehouse.",
        "kick": "Kick warehouse - percutant et direct. Corps entre 80-100Hz. Doit coexister avec l energie acide de la 303.",
        "bass": "TB-303 en star absolue - sub acide avec slides et glides caracteristiques. La ligne acide est le protagoniste.",
        "stereo": "Mix majoritairement mono pour l energie warehouse. La 303 peut avoir un leger widening pour la presence.",
        "compression": "Sidechain marque pour que la 303 respire sur le kick. Compression vintage pour la couleur.",
        "reverb": "Peu de reverbe - le genre est sec et direct. Quelques delays courts sur la 303 pour l espace.",
        "eq_master": "Boost des frequences acides (500Hz-2kHz) pour que la 303 coupe. Sub propre et puissant.",
        "erreurs": ["303 pas assez presente ou trop douce - elle doit dominer", "Manque de slides et glides caracteristiques", "Mix trop propre et sans caracterE"],
        "contexte": "Clubs underground, heritage warehouse anglais. Labels React, Evolution Records. Energie rave des annees 90.",
        "arrangement": "Builds avec la 303 qui monte en intensite. Breaks avec juste la 303 seule. Drops acides explosifs.",
        "mastering": "LUFS -9. True Peak -0.3 dBTP. L acid doit trancher et couper.",
        "plateformes": "Beatport, Bandcamp. Vinyles encore tres presents dans ce genre.",
    },
    # ── House variants ────────────────────────────────────────────────────────
    "tech house": {
        "son": "Fusion entre techno minimaliste et house groovante. Groove repetetif, basslines percussives, elements techno froids dans un cadre house dansant.",
        "kick": "Kick house mais plus sec et percutant que la deep house. Corps entre 80-100Hz. Doit groover et maintenir l energie.",
        "bass": "Basse percussive et repetitive, souvent syncopee. Corps entre 80-200Hz. Groove avec le kick en synergie.",
        "stereo": "Mix moderement stereo. Kick et basse en mono. Elements groove legeremet stereo pour la chaleur.",
        "compression": "Sidechain musical et groove. Compression vintage sur la basse. Glue compressor sur le bus pour la cohesion.",
        "reverb": "Peu de reverbe - le genre est groovy et direct. Quelques delays courts sur les elements percussifs.",
        "eq_master": "Warmth dans les bas-mids pour le groove. Boost leger des hauts-mids pour la presence. Pas trop brillant.",
        "erreurs": ["Trop froid et techno - perdre le groove house", "Basse sans groove ni syncope", "LUFS trop bas pour le dancefloor"],
        "contexte": "Labels DIRTYBIRD, Relief, Hot Creations. Clubs internationaux, festivals. Public large et dansant.",
        "arrangement": "Structure house classique avec elements techno. Buildups progressifs, quelques breakdowns courts.",
        "mastering": "LUFS -9/-10. True Peak -0.3 dBTP. Groove et punch avant tout.",
        "plateformes": "Beatport (categorie dominante), Spotify, SoundCloud.",
    },
    "afro house": {
        "son": "House africaine avec percussions riches et rythmiques complexes. Influence des musiques traditionnelles africaines, voix en swahili ou zulu, groove solaire.",
        "kick": "Kick profond et chaud, corps entre 60-80Hz avec une belle queue. Il respire et groove naturellement.",
        "bass": "Basse chaude et melodique, souvent avec des mouvements expressifs. Corps riche entre 80-200Hz.",
        "stereo": "Image stereo moderee a large. Les percussions s etendent en stereo pour la richesse rythmique.",
        "compression": "Compression douce et musicale. Le groove naturel des percussions est preserve.",
        "reverb": "Reverbes d ambiance naturelle sur les voix et percussions. Delays melodiques sur les elements electroniques.",
        "eq_master": "Chaleur dans les bas-mids. Presence des percussions dans les mids. Clarte dans les hauts.",
        "erreurs": ["Percussions trop digitales et froides - le genre demande de l organicite", "Manque de groove dans les rythmiques", "Voix pas assez mises en avant"],
        "contexte": "Labels Sondela, Afropulse, Black Coffee Music. Clubs de Johannesburg, Nairobi, Berlin. Marche en pleine expansion.",
        "arrangement": "Structure house avec percussions africaines progressives. Voix importantes dans le breakdown.",
        "mastering": "LUFS -10. True Peak -1.0 dBTP. Chaleur et groove primes.",
        "plateformes": "Beatport, Traxsource, Spotify (grosse audience en Afrique du Sud).",
    },
    "progressive house": {
        "son": "House qui evolue progressivement sur de longues structures. Builds epiques, melodies emotionnelles, energie montante. Entre house et trance dans l ambiance.",
        "kick": "Kick puissant et regulier, corps entre 70-90Hz. Il doit porter l energie pendant 8 minutes de build progressif.",
        "bass": "Basse melodique qui evolue avec l arrangement. Sub propre et fort. Mouvements harmoniques importants.",
        "stereo": "Image stereo large sur les synthes et pads. Sub et kick en mono. Le champ stereo s elargit avec l energie.",
        "compression": "Sidechain pumping marque sur les synthes. Compression de bus pour la cohesion energetique.",
        "reverb": "Grandes reverbes sur les synthes pour l ampleur. Delays tempo-synces sur les elements melodiques.",
        "eq_master": "Hauts-mids presents pour les synthes. Controle des bas-mids pour eviter la confusion. Air dans les aigus.",
        "erreurs": ["Build pas assez progressif - l energie doit monter graduellement", "Manque d ampleur sur les synthes", "LUFS trop bas pour le dancefloor"],
        "contexte": "Labels Anjunadeep, Anjunabeats, Armada. Clubs et festivals. Sets de 2h avec une narrative emotionnelle.",
        "arrangement": "Structure longue avec build de 4-6 minutes, breakdown emotionnel, drop progressif, deuxieme montee.",
        "mastering": "LUFS -9/-10. True Peak -1.0 dBTP. Ampleur et progression sont les valeurs cles.",
        "plateformes": "Beatport, Spotify, Apple Music. Grosse audience streaming.",
    },
    "melodic house": {
        "son": "House concentree sur la melodie et l emotion. Synthes chaleureux, lignes melodiques memorables, groove house en support. L emotion prime sur la technique.",
        "kick": "Kick chaud et enveloppant, corps entre 60-80Hz. Moins percutant que la house classique, plus emotionnel.",
        "bass": "Basse melodique et expressive. Elle participe a l harmonie du morceau autant qu au groove.",
        "stereo": "Large et immersif. Les melodies s etendent dans tout le champ stereo pour l emotion maximale.",
        "compression": "Sidechain subtil et musical. La dynamique naturelle est preservee pour l emotion.",
        "reverb": "Longues reverbes sur les synthes melodiques. Delays musicaux. La profondeur cree l emotion.",
        "eq_master": "Hauts-mids doux pour les melodies. Chaleur dans les bas-mids. Pas trop brillant ni agressif.",
        "erreurs": ["Melodie pas assez mise en avant - elle doit etre la star", "Trop de groove techno qui ecrase l emotion", "Manque de profondeur et de reverbe"],
        "contexte": "Labels Anjunadeep, Cercle releases, Get Physical. Ecoute casque et clubs atmospheriques.",
        "arrangement": "Arc emotionnel avec montees et descentes. Breakdown melodique intense, drop emotionnel.",
        "mastering": "LUFS -10/-11. True Peak -1.0 dBTP. L emotion prime sur le volume.",
        "plateformes": "Spotify, Beatport, Apple Music. Tres bien streame.",
    },
    "organic house": {
        "son": "House avec des sons acoustiques et organiques. Guitares, percussions live, voix naturelles melangees avec des elements electroniques. Chaleur et humanite.",
        "kick": "Kick tres organique - souvent une grosse caisse live ou fortement processée pour sembler naturelle. Corps entre 60-80Hz avec beaucoup de naturel.",
        "bass": "Basse organique - souvent une vraie basse electrique ou contrebasse. Chaleur et humanite avant tout.",
        "stereo": "Stereo naturelle et organique. Les instruments acoustiques ont leur placement naturel dans le champ.",
        "compression": "Compression analogique douce pour preserver le naturel. Pas de sidechain agressif.",
        "reverb": "Reverbes de salle naturelles. Espaces acoustiques reels (live room). Peu de reverbe digitale froide.",
        "eq_master": "Chaleur analogique. Peu de correction digitale froide. L objectif est un son naturel et vivant.",
        "erreurs": ["Sons trop digitaux et froids qui cassent l organicite", "Compression trop agressive qui enleve l humanite", "Manque de naturel et de vie dans les sons"],
        "contexte": "Labels Sounds Of Earth, Cafe De Anatolia, One Of A Kind. Terrasses, bars, ecoute attentive.",
        "arrangement": "Evolution naturelle et progressive. Instruments qui arrivent comme dans une vraie session de jam.",
        "mastering": "LUFS -11/-12. Dynamique naturelle preservee. True Peak -1.0 dBTP.",
        "plateformes": "Spotify, Bandcamp, Apple Music. Tres bien streame pour la concentration.",
    },
    "amapiano": {
        "son": "Genre sud-africain fusion de deep house, jazz et kwaito. Log drum caracteristique, voix en zulu/sotho, groove unique et festif. Tempo lent mais tres dansant.",
        "kick": "Kick profond et chaud, corps entre 50-70Hz avec beaucoup de sustain. Il groove lentement mais puissamment.",
        "bass": "Log drum - percussion basse caracteristique de l amapiano. Corps tres profond entre 40-80Hz avec un son unique de log drum synthetique.",
        "stereo": "Image stereo moderee. Le log drum en mono. Voix et elements melodiques plus larges en stereo.",
        "compression": "Compression douce et groove. Le log drum doit avoir sa dynamique naturelle preservee.",
        "reverb": "Reverbes d ambiance sur les voix et elements melodiques. Le log drum reste relativement sec.",
        "eq_master": "Sub tres present pour le log drum. Chaleur dans les mids pour les voix. Clarte dans les hauts.",
        "erreurs": ["Log drum pas assez present ou caracteristique", "Tempo trop rapide - l amapiano est lent (~115 BPM)", "Manque de voix ou de chants caracteristiques"],
        "contexte": "Origine Johannesburg et Pretoria. Labels Ambitiouz Entertainment, Afrotainment. Marche en expansion mondiale.",
        "arrangement": "Structure avec log drum en permanence, voix qui arrivent et repartent, sections de piano jazz.",
        "mastering": "LUFS -9. True Peak -0.3 dBTP. Le log drum doit etre fort et propre.",
        "plateformes": "Spotify (enorme en Afrique du Sud), Apple Music, Beatport Afro House.",
    },
    "funky house": {
        "son": "House avec une forte influence funk. Samples de funk et soul, basses slap ou groove, claviers vintage, energie positive et dansante. La house avec de l ame.",
        "kick": "Kick house groovy avec un caractere funk. Corps entre 70-90Hz, plus organique que la tech house.",
        "bass": "Basse funk - slap ou groove expressif. Syncopee et tres mobile. Elle danse autant qu elle soutient.",
        "stereo": "Image stereo modere a large. Les claviers et cuivres s etalent en stereo pour la chaleur funk.",
        "compression": "Compression vintage style LA-2A ou 1176 pour la couleur funk. Sidechain musical et groove.",
        "reverb": "Reverbes de salle vintage sur les percussions. Delays courts sur les claviers. Ambiance studio annees 70.",
        "eq_master": "Chaleur funk dans les bas-mids. Presence des cuivres et claviers. Pas trop brilliant ou digital.",
        "erreurs": ["Basse trop statique et sans groove funk", "Sons trop digitaux qui cassent le vibe vintage", "Manque de claviers ou d elements funk caracteristiques"],
        "contexte": "Labels Defected, Salsoul, Trax Records heritage. Clubs house classiques, public qui connait le funk.",
        "arrangement": "Structure house avec breakdowns funk, solos de claviers ou cuivres, groove maintenu en permanence.",
        "mastering": "LUFS -10. True Peak -1.0 dBTP. Le groove et la chaleur priment.",
        "plateformes": "Beatport, Traxsource, Spotify. Large audience house.",
    },
    "soulful house": {
        "son": "House avec une ame profondement soul. Voix gospel ou soul en avant, chord progressions emotionnelles, groove house soutenu. La house qui touche le coeur.",
        "kick": "Kick chaud et profond, tres humain. Corps entre 60-80Hz. Doit soutenir sans jamais dominer les voix.",
        "bass": "Basse melodique et expressive. Elle dialogue avec les voix et les accords. Chaleur et sensibilite.",
        "stereo": "Large et enveloppant. Les voix s etendent en stereo avec des harmonies larges. Les instruments soutiennent.",
        "compression": "Compression douce qui preserve les dynamiques vocales. Les voix doivent respirer et s exprimer.",
        "reverb": "Reverbe de church ou de grande salle sur les voix. Delay sur les reponses vocales. Gospel room.",
        "eq_master": "Presence vocale dans les hauts-mids. Chaleur dans les bas-mids. Les voix doivent briller.",
        "erreurs": ["Voix pas assez presentes ou mises en avant", "Manque d ame et d expression dans les voix", "Production trop froide et mecanique"],
        "contexte": "Labels Defected, Nervous, Strictly Rhythm. Clubs new-yorkais, ambiance Sunday brunch.",
        "arrangement": "Introduction instrumentale, arrivee des voix, breakdowns gospel, montee emotionnelle finale.",
        "mastering": "LUFS -11/-12. Dynamique vocale preservee. True Peak -1.0 dBTP.",
        "plateformes": "Beatport, Traxsource, Spotify. Tres apprecie pour la relaxation et la contemplation.",
    },
    "indie dance": {
        "son": "Fusion entre musique indie alternative et dancefloor electronique. Guitares, synthetiseurs, boom bap electronique. L alternative qui fait danser.",
        "kick": "Kick indie - moins percutant que l electronica pure, plus organique. Corps entre 80-120Hz avec un caractere indie.",
        "bass": "Basse indie avec du caractere et de la personnalite. Souvent une vraie basse electrique ou un synthbass vintage.",
        "stereo": "Large et indie. Les guitares s etalent en stereo comme dans un mix rock indie. L espace est genereux.",
        "compression": "Compression vintage et caracterielle. Pas de sidechain trop evident. L humanite du son prime.",
        "reverb": "Reverbes de salle live pour les guitares et percussions. Delays creates pour l espace indie.",
        "eq_master": "Hauts-mids presents pour les guitares. Pas trop de sub - le genre est plus haut du spectre. Brillance indie.",
        "erreurs": ["Trop electronique et sans caractere indie", "Guitares absentes ou trop en retrait", "Manque de personnalite et d identite sonore"],
        "contexte": "Labels DFA, Kompakt, Modular. Festivals indie et clubs alternatifs. Public eclectique.",
        "arrangement": "Structure plus proche de la chanson indie avec couplet, refrain, mais sur un dancefloor.",
        "mastering": "LUFS -11. True Peak -1.0 dBTP. Caractere et personnalite avant la loudness.",
        "plateformes": "Spotify, Beatport Indie Dance, Apple Music. Tres bien streame.",
    },
    "nu disco": {
        "son": "Disco revisitee avec des elements modernes. Samples et interpolations disco, synthetiseurs 80s, voix glamour, production contemporaine. Nostalgie festive.",
        "kick": "Kick disco - rond, chaud et avec un fort sustain. Corps entre 60-80Hz avec une belle queue. Tres groove.",
        "bass": "Basse disco - profonde, melodique et mobile. Elle groove en permanence avec une ligne expressive et syncopee.",
        "stereo": "Large et glamour. Les strings et cuivres disco s etalent largement. Image stereo tres ouverte.",
        "compression": "Compression douce et analogique. Le groove naturel des instruments disco est preserve.",
        "reverb": "Reverbes vintage de salle de disco ou de studio 70s. Largeur stereo augmentee par des doubles.",
        "eq_master": "Chaleur disco dans les bas-mids. Strings brillantes dans les hauts-mids. Clarte et glamour.",
        "erreurs": ["Manque de references et de couleur disco", "Basse trop moderne et sans groove vintage", "Production trop froide pour le genre festif"],
        "contexte": "Labels Eskimo Recordings, Gomma, Kitsuné. Clubs festifs, ambiance nocturne glamour.",
        "arrangement": "Intro avec strings ou cuivres, montee groove avec kick, breakdown avec voix, outro dancefloor.",
        "mastering": "LUFS -11. True Peak -1.0 dBTP. Chaleur et glamour sont les valeurs.",
        "plateformes": "Beatport Nu Disco, Spotify, Apple Music.",
    },
    # ── Trance variants ───────────────────────────────────────────────────────
    "trance main floor": {
        "son": "Trance euphorique et massive pour les grandes salles et festivals. Builds enormes, drops catharctiques, leads melodiques intenses. L emotion a son maximum.",
        "kick": "Kick puissant et regulier, corps entre 70-90Hz. Doit tenir pendant 8 minutes sans faiblir.",
        "bass": "Basse arpegiee et melodique. Sub propre et fort. Elle porte l energie entre les temps forts.",
        "stereo": "Tres large - les synthes leads et pads s etendent au maximum. Sub et kick en mono strict.",
        "compression": "Sidechain pumping prononce sur les synthes. Compression de bus pour l energie et la cohesion.",
        "reverb": "Hall immense sur le lead. Delays tempo-synces sur les arpages. Le breakdown vit de sa reverbe.",
        "eq_master": "Boost des hauts-mids pour les synthes leads. Beaucoup d air a 12-16kHz. Controle des bas-mids.",
        "erreurs": ["Lead pas assez present - il doit dominer le mix", "Breakdown pas assez emotionnel", "Stereo pas assez large pour les festivals"],
        "contexte": "Festivals (ASOT, Tomorrowland), clubs trance. Labels Armin van Buuren, Enhanced, Black Hole.",
        "arrangement": "Structure en M - montee, breakdown epique, drop euphorique, deuxieme montee, outro.",
        "mastering": "LUFS -9. True Peak -1.0 dBTP. Les leads doivent briller au-dessus de tout.",
        "plateformes": "Beatport Trance, Spotify, Apple Music.",
    },
    "vocal trance": {
        "son": "Trance avec une voix lead feminine en avant. La melodie vocale est la star absolue. Production trance en support emotionnel de la voix.",
        "kick": "Kick puissant mais qui laisse de l espace pour la voix. Corps entre 70-90Hz. Ne doit jamais masquer les frequences vocales.",
        "bass": "Basse melodique qui soutient la voix harmoniquement. Sub propre. La voix est toujours prioritaire.",
        "stereo": "Large sur les elements instrumentaux. La voix lead est centrale et presente. Les BV s etalent largement.",
        "compression": "Voix tres compressee pour la presence constante. Sidechain sur les elements instrumentaux.",
        "reverb": "Reverbe flattering sur la voix. Delay tempo-synce pour l echo vocal caracteristique.",
        "eq_master": "Presence vocale dans les hauts-mids (2-5kHz). Les frequences instrumentales laissent de l espace a la voix.",
        "erreurs": ["Voix enterree sous l instrumental - elle doit dominer", "Manque d emotion dans le traitement vocal", "Instrumental trop dense qui etouffe la voix"],
        "contexte": "Labels Armada, VANDIT, Xtravaganza. Public tres fan et emotionnel. Radio trance.",
        "arrangement": "Intro instrumentale, arrivee de la voix, refrain puissant, breakdown vocal, drop final.",
        "mastering": "LUFS -9. True Peak -1.0 dBTP. La voix doit etre claire et presente.",
        "plateformes": "Beatport, Spotify (audience radio importante), Apple Music.",
    },
    "progressive trance": {
        "son": "Trance qui evolue progressivement et subtilement. Moins commerciale que la main floor, plus artistique et profonde. Builds tres longs et nuances.",
        "kick": "Kick regulier et moins agressif que la main floor. Corps entre 70-85Hz. Il instille la transe progressivement.",
        "bass": "Basse progressive qui evolue lentement. Sub propre. Les changements sont subtils et progressifs.",
        "stereo": "Large et progressivement de plus en plus immersif. L image stereo s elargit avec le build.",
        "compression": "Sidechain subtil et progressif. La compression est transparente et ne doit pas etre evidente.",
        "reverb": "Grandes reverbes progressives. Les espaces acoustiques s agrandissent avec le build.",
        "eq_master": "EQ evolutif avec la progression. Les hauts-mids apparaissent progressivement. Controle des bas-mids.",
        "erreurs": ["Build trop rapide pour un genre qui demande de la patience", "Trop commercial et proche de la main floor", "Manque de subtilite et de nuance dans la progression"],
        "contexte": "Labels Solarstone Pure Trance, Lange Recordings, Movement Recordings. DJs sets long format.",
        "arrangement": "Builds extremement lents sur 6-8 minutes. Breakdowns contemplatives. Drops subtils.",
        "mastering": "LUFS -10. True Peak -1.0 dBTP. La subtilite prime sur la loudness.",
        "plateformes": "Beatport, SoundCloud. Moins mainstream que la main floor.",
    },
    "hard trance": {
        "son": "Trance a tempo eleve et tres energetique. Kicks plus durs, energie brute, moins de melodie que la main floor mais plus d agressivite.",
        "kick": "Kick tres percutant et rapide, corps entre 80-100Hz. Doit tenir a 150+ BPM sans perdre en punch.",
        "bass": "Basse synthetique et percussive. Sub fort et court. L energie prime sur la melodie.",
        "stereo": "Mix plus etroit que la trance classique. L energie vient de la densite et non de la largeur.",
        "compression": "Compression forte et sidechain agressif. Le mix doit etre compresse pour tenir a fort tempo.",
        "reverb": "Reverbes courtes et energiques. Moins de reverbe que la main floor pour garder la clarte a haut BPM.",
        "eq_master": "Hauts-mids presents et agressifs. Sub fort. Coupure dans les 200-400Hz pour la clarte.",
        "erreurs": ["Kick pas assez puissant pour tenir a 150+ BPM", "Trop melodique et pas assez energique", "Mix trop ouvert qui perd de l energie"],
        "contexte": "Clubs trance speciaux, festivals hard trance. Labels Schranzkommando, Planet Punk.",
        "arrangement": "Structure directe avec peu de breakdowns longs. L energie est maintenue haute en permanence.",
        "mastering": "LUFS -8. True Peak -0.3 dBTP. Maximum d energie et de punch.",
        "plateformes": "Beatport Trance, SoundCloud. Niche mais loyal.",
    },
    "tech trance": {
        "son": "Fusion entre trance et techno. Groove plus sombre et moins euphorique que la trance classique, mais plus melodique que la techno. Un hybride sophistique.",
        "kick": "Kick entre techno et trance - percutant comme la techno mais avec le corps de la trance.",
        "bass": "Basse entre les deux genres - plus melodique que la techno, plus percussive que la trance.",
        "stereo": "Image stereo moderee. Plus etroite que la trance pure, plus large que la techno.",
        "compression": "Sidechain present mais pas agressif. Compression techno transparente avec couleur trance.",
        "reverb": "Reverbes moyennes - plus longues que la techno, plus courtes que la trance. Equilibre entre les deux.",
        "eq_master": "Hauts-mids du lead trance, punch du kick techno. Equilibre entre les deux esthetiques.",
        "erreurs": ["Trop proche d un genre - perdre l hybridation", "Manque d identite propre entre les deux influences", "LUFS inadapte pour ni l un ni l autre genre"],
        "contexte": "Labels BT, Art of Trance, Marco V. Festivals hybrides, DJs qui mixent les deux genres.",
        "arrangement": "Structure trance avec la sobriete de la techno. Breakdowns moins epiques que la trance pure.",
        "mastering": "LUFS -9. True Peak -0.3 dBTP. Equilibre entre les deux genres.",
        "plateformes": "Beatport Trance et Techno. Audience de niche mais connaisseur.",
    },
    "full-on psytrance": {
        "son": "Psytrance energetique et festif. BPM eleve (143-148), kicks percutants, lignes acides, ambiance de festival de plein air. Energie maximale et solaire.",
        "kick": "Kick psytrance characteristique - tres percutant, corps entre 80-100Hz, attaque ultra-rapide. Doit claquer sur des systemes en plein air.",
        "bass": "Basse acide et robotique sous le kick. Sub mono tres present. La basse psytrance est tres caracteristique.",
        "stereo": "Mix energetique et relativement centre. L energie vient de la densite plutot que de la largeur.",
        "compression": "Compression forte pour tenir a haut BPM et fort volume. Sidechain entre kick et basse.",
        "reverb": "Peu de reverbe - le genre est direct et energetique. Quelques effets psychedeliques specifiques.",
        "eq_master": "Sub fort pour le kick et la basse. Hauts-mids pour la presence des synths acides.",
        "erreurs": ["Kick pas assez present pour les systemes plein air", "Manque d energie et de momentum", "BPM trop lent pour le genre"],
        "contexte": "Festivals psytrance (Ozora, Boom, Universo Parallelo). Systemes plein air, ambiance chamanique.",
        "arrangement": "Structure repetitive et hypnotique. Peu de breakdowns longs. L energie est maintenue.",
        "mastering": "LUFS -8. True Peak -0.3 dBTP. Maximum de punch pour les systemes plein air.",
        "plateformes": "Beatport Psy-Trance, Bandcamp, SoundCloud. Distribution tres communautaire.",
    },
    "dark psytrance": {
        "son": "Psytrance sombre et interieur. Ambiances plus lourdes et sinistres, BPM plus eleve, moins festif que le full-on. Experience interieure et introspective.",
        "kick": "Kick massif et sombre, corps entre 70-90Hz avec une presence lourde. Plus grave que le full-on.",
        "bass": "Basse sombre et menaçante. Sub tres profond. L atmospheRe sombre est renforcee par les graves.",
        "stereo": "Mix souvent etroit et lourd. L atmospheRe sombre vient de la densite dans les graves.",
        "compression": "Compression tres agressive. Le mix est dense et lourd. Limiteur tres pousse.",
        "reverb": "Espaces sombres et industriels. Reverbes longues sur les effets atmospheriques.",
        "eq_master": "Sub tres present et lourd. Moins de hauts que le full-on. Atmoshere sombre et pesante.",
        "erreurs": ["Trop energetique et festif - perdre le cote sombre", "Sub pas assez lourd et present", "AtmospheRe pas assez sombre et interieure"],
        "contexte": "Festivals psytrance underground, stages dark. Labels Ektoplazm, DAT Records.",
        "arrangement": "Structure hypnotique et repetitive. Lente evolution dans le darkness. Peu d accalmies.",
        "mastering": "LUFS -8/-9. True Peak -0.3 dBTP. Lourdeur et sombre sont les valeurs.",
        "plateformes": "Bandcamp, SoundCloud, Ektoplazm (gratuit dans cette scene). Tres communautaire.",
    },
    "goa trance": {
        "son": "Psytrance original des annees 90. Heritage de Goa en Inde. Lignes melodiques complexes, psychedelisme musical, atmosphere spirituelle et cosmique.",
        "kick": "Kick goa caracteristique - moins percutant que le psytrance moderne, plus rond et avec de la texture.",
        "bass": "Basse melodique et psychedelique. Lignes plus complexes que le psytrance moderne.",
        "stereo": "Stereo moderee avec des elements psychedeliques qui voyagent dans l espace.",
        "compression": "Compression vintage et moins agressive que le psytrance moderne. Plus dynamique.",
        "reverb": "Reverbes cosmiques et spatiales. L atmosphere psychedelique vient des reverbes et delays.",
        "eq_master": "Couleurs vintage et psychedeliques. Moins de sub que le psytrance moderne. Mids melodiques presents.",
        "erreurs": ["Trop moderne et sans l heritage goa vintage", "Manque de complexite melodique caracteristique", "Production trop propre pour l esthetique psychedelique"],
        "contexte": "Heritage de Goa Inde. Festivals retro-psytrance. Labels Dragonfly, TIP Records, Blue Room Released.",
        "arrangement": "Structures complexes avec de multiples lignes melodiques entrelacees. Psychedelisme narratif.",
        "mastering": "LUFS -9. True Peak -1.0 dBTP. L atmosphere et la couleur priment.",
        "plateformes": "Bandcamp, SoundCloud, Ektoplazm. Tres communautaire et nostalgique.",
    },
    # ── DnB / Bass variants ───────────────────────────────────────────────────
    "liquid dnb": {
        "son": "DnB melodique, fluide et atmospherique. Contrastant avec le neuro ou le jump up, le liquid est emotionnel et positif. Breaks fluides, melodie importante.",
        "kick": "Kick halftempo doux et fluid, corps entre 60-80Hz avec une belle queue. Moins percutant que le neurofunk.",
        "bass": "Sub melodique et expressif. Les mouvements du sub sont musicaux et emotionnels, pas agressifs.",
        "stereo": "Sub mono. Breaks et atmospheres peuvent etre tres larges en stereo. L espace stereo cree l emotion.",
        "compression": "Compression douce et musicale. Les dynamiques naturelles des breaks sont preservees.",
        "reverb": "Reverbes d ambiance sur les synthes et voix. L atmosphere liquide vient des reverbes et delays.",
        "eq_master": "Hauts-mids doux pour les melodies. Sub propre et expressif. Pas trop agressif dans les aigus.",
        "erreurs": ["Trop agressif et neurofunk - perdre le cote liquid", "Melodie pas assez presente et emotionnelle", "Manque d atmospheRe et de fluidite"],
        "contexte": "Labels Hospital Records, Metalheadz liquid, Nu:Tone. Clubs DnB mais aussi Spotify.",
        "arrangement": "Introduction atmospherique, premier drop fluid, breakdown emotionnel, deuxieme drop.",
        "mastering": "LUFS -9. True Peak -0.5 dBTP. Fluidite et emotion avant la loudness.",
        "plateformes": "Beatport DnB, Spotify (tres bien streame), Apple Music.",
    },
    "halftime": {
        "son": "DnB experimental au tempo halftime ressenti. BPM autour de 70-75 mais avec des breaks halftempo tres lourds. Sub massif, ambiance sombre et pesante.",
        "kick": "Kick halftempo massif, corps entre 50-70Hz, avec beaucoup de sustain. Il sonne lourd et lent malgre le BPM.",
        "bass": "Sub enormes et expressifs. Les basses bougent lentement mais avec une force massive.",
        "stereo": "Sub mono strict. Breaks et textures peuvent etre tres larges pour l ambiance.",
        "compression": "Compression lourde sur le sub. Transient shaping sur les breaks.",
        "reverb": "Reverbes lourdes et atmospheriques. L espace acoustique est grand et pesant.",
        "eq_master": "Sub tres present et lourd (40-70Hz). Coupure dans les 200-400Hz pour la clarte.",
        "erreurs": ["Sub pas assez lourd - le halftime vit par ses graves massifs", "Tempo mal compris - 70-75 BPM ressenti pas 140-150", "Manque de lourdeur et de poids"],
        "contexte": "Labels Metalheadz, Loxy & Resound, Deep Medi. Clubs DnB speciaux, ambiance sombre.",
        "arrangement": "Structure lente avec des sections tres lourdes. Peu de variation - l atmosphere sombre domine.",
        "mastering": "LUFS -9. True Peak -0.3 dBTP. La lourdeur du sub est prioritaire.",
        "plateformes": "Beatport DnB, SoundCloud, Bandcamp.",
    },
    "jungle": {
        "son": "Heritage DnB original des annees 90. Amen break en avant, samples de reggae/ragga, voix MC, sous-culture rave britannique. Rawness et energie.",
        "kick": "Kick souvent dans un break sample (Amen break en tete). Corps entre 80-120Hz avec le caractere du sample.",
        "bass": "Sub reggae et ragga, parfois avec des riddims samples. Basse chaude et organique.",
        "stereo": "Breaks souvent en stereo pour la largeur rave. Sub mono. L image stereo reflete l heritage sample.",
        "compression": "Compression vintage sur les breaks pour le punch. Saturation de bande pour la couleur.",
        "reverb": "Reverbes de salle live sur les breaks. Delays sur les voix ragga. Heritage sonore des raves 90s.",
        "eq_master": "Hauts-mids pour que les breaks coupent. Sub pour la basse reggae. Couleur vintage assumee.",
        "erreurs": ["Trop propre et numerique - le jungle est raw et vintage", "Manque de breaks caracteristiques", "Amen break pas assez mis en avant"],
        "contexte": "Heritage rave britannique 1992-1994. Labels Moving Shadow, Reinforced Records. Scene retro-jungle.",
        "arrangement": "Structure rave avec de longues sections de breaks. Voix MC ragga. Energie rave maintenue.",
        "mastering": "LUFS -9. True Peak -0.5 dBTP. Le caractere vintage prime sur la perfection technique.",
        "plateformes": "Bandcamp, SoundCloud, Beatport DnB. Scene tres communautaire.",
    },
    "deep dubstep": {
        "son": "Dubstep profond et atmospherique. Loin du brostep, le deep dubstep est sombre, espace et melancolique. Heritage dub et grime, ambiance nocturne.",
        "kick": "Kick dub, profond et enveloppant. Corps entre 50-70Hz avec une belle queue. Pulsation cardiaque.",
        "bass": "Sub wobble lent et expressif. Les basses se deplacent lentement avec beaucoup de poids et d expression.",
        "stereo": "Sub mono strict. L atmosphere stereo large cree le contraste avec la basse mono.",
        "compression": "Peu de compression - la dynamique naturelle cree le poids du sub. Limiteur doux.",
        "reverb": "Grandes reverbes atmospheriques caracteristiques du dub. Delays avec du feedback. L espace est immense.",
        "eq_master": "Sub tres profond et present. Peu de brillance excessive. L atmosphere sombre et profonde prime.",
        "erreurs": ["Trop agressif - perdre l atmosphere profonde et melancolique", "Sub pas assez present et expressif", "Trop proche du brostep commercial"],
        "contexte": "Labels Digital Mystikz, Deep Medi, Hessle Audio. Clubs bass music underground.",
        "arrangement": "Structures lentes et atmospheriques. Les basses arrivent et repartent avec beaucoup d espace.",
        "mastering": "LUFS -10. True Peak -0.5 dBTP. L atmosphere et le poids du sub priment.",
        "plateformes": "Beatport Dubstep, Bandcamp, SoundCloud.",
    },
    "140 deep dubstep": {
        "son": "Zone de transition entre dubstep (140 BPM) et techno. Plus percutant que le deep dubstep classique, avec des elements des deux genres. Hybride underground.",
        "kick": "Kick entre dubstep et techno - percutant mais avec le poids du dubstep. Corps entre 70-90Hz.",
        "bass": "Basses dubstep a 140 BPM. Plus percussives que le deep dubstep classique mais avec de la profondeur.",
        "stereo": "Mix etroit sur les elements rythmiques. L atmosphere peut etre plus large.",
        "compression": "Compression entre les deux genres. Plus percutant que le deep dubstep, moins que la techno.",
        "reverb": "Reverbes courtes a moyennes. Moins que le deep dubstep, plus que la techno.",
        "eq_master": "Sub profond du dubstep avec le punch de la techno. Equilibre entre les deux esthetiques.",
        "erreurs": ["Trop proche d un seul genre - perdre l hybridation", "BPM inadapte - doit etre exactement autour de 138-142 BPM"],
        "contexte": "Labels Hessle Audio, Hemlock, Tectonic. Clubs bass music underground UK.",
        "arrangement": "Structure hybride entre les deux genres. Ni trop dubstep ni trop techno.",
        "mastering": "LUFS -9. True Peak -0.3 dBTP.",
        "plateformes": "Beatport, Bandcamp, SoundCloud.",
    },
    "uk bass": {
        "son": "Musique bass music britannique qui fusionne grime, dubstep, UK garage et R&B. Sons tres specifiquement britanniques, voix et MCs, ambiance urbaine.",
        "kick": "Kick UK bass caracteristique - entre le garage et le dubstep. Corps entre 80-100Hz avec swing.",
        "bass": "Basses UK bass - wobbly et expressifs mais plus subtils que le dubstep commercial. Heritage grime.",
        "stereo": "Image stereo moderee. Le UK bass est plus centre que le dubstep commercial.",
        "compression": "Compression subtile et musicale. Les dynamiques UK bass sont importantes.",
        "reverb": "Reverbes courts et urbains. Quelques delays caracteristiques du grime.",
        "eq_master": "Presence des hauts-mids pour les voix et MCs. Sub present mais pas ecrasant.",
        "erreurs": ["Trop proche du dubstep commercial", "Manque de caracteristiques UK specifiques", "Voix et MCs pas assez mis en avant"],
        "contexte": "Labels Night Slugs, Hyperdub, Numbers. Scene UK underground, clubs et festivals.",
        "arrangement": "Structures ouvertes pour les MCs. Espace pour les voix et les flows.",
        "mastering": "LUFS -9. True Peak -0.5 dBTP.",
        "plateformes": "Beatport, Bandcamp, SoundCloud. Tres scene communautaire UK.",
    },
    "breakbeat": {
        "son": "Musique electronique centree sur les breaks de batterie. Groove syncopé, basses funky, samples, heritage rave des annees 90. Entre funk et electronique.",
        "kick": "Kick dans un break sample ou electronique avec le groove du break. Syncopé et groovant.",
        "bass": "Basse funk et groovante. Elle dialogue avec le break en permanence. Tres syncopee.",
        "stereo": "Breaks souvent larges en stereo pour le groove. Sub mono. Image stereo ouverte.",
        "compression": "Compression vintage sur les breaks. Saturation pour la couleur. Glue compressor sur le bus.",
        "reverb": "Reverbes de salle live sur les breaks. Delays courts. Ambiance rave des 90s.",
        "eq_master": "Hauts-mids pour que les breaks coupent. Sub groovant. Couleur vintage assumee.",
        "erreurs": ["Breaks sans caractere et sans groove", "Basse trop statique", "Manque de syncopation et de funk"],
        "contexte": "Labels Skint, Wall of Sound, Finger Lickin. Festival et clubs breaks. Heritage Chemical Brothers.",
        "arrangement": "Structure centree sur les breaks avec buildups et drops. Energie break maintenue.",
        "mastering": "LUFS -9/-10. True Peak -0.5 dBTP. Le groove et le caractere priment.",
        "plateformes": "Beatport Breaks, Bandcamp, SoundCloud.",
    },
    "breaks": {
        "son": "Version moderne du breakbeat. Plus electronique et actuelle que le breakbeat classique. Fusion avec la techno, la bass music et l electronica.",
        "kick": "Kick modern breaks - entre le break sample et le kick electronique. Percutant et groovant.",
        "bass": "Basses modernes avec influence bass music. Plus electroniques que le breakbeat classique.",
        "stereo": "Image stereo moderne et large. Plus large que le breakbeat classique.",
        "compression": "Compression moderne et transparente. Plus propre que le breakbeat vintage.",
        "reverb": "Reverbes modernes et propres. Moins vintage que le breakbeat classique.",
        "eq_master": "Mix moderne et propre. Heritage breaks mais avec les standards de production actuels.",
        "erreurs": ["Trop vintage quand vise un son moderne", "Trop moderne quand vise l heritage breaks"],
        "contexte": "Labels modernes de breaks et electronica. Festivals hybrides.",
        "arrangement": "Structure moderne avec heritage breaks. Plus flexible que le breakbeat classique.",
        "mastering": "LUFS -9. True Peak -0.5 dBTP.",
        "plateformes": "Beatport, SoundCloud, Bandcamp.",
    },
    # ── Electronic variants ───────────────────────────────────────────────────
    "electro": {
        "son": "Electro au sens heritage - Detroit electro, electroclash, EBM. Sons robots et metalliques, boites a rythmes vintage (808, 909), atmosphere froide et futuriste.",
        "kick": "Kick 808 ou 909 vintage. Corps entre 80-120Hz avec le caractere de la boite a rythmes. Pas trop modern.",
        "bass": "Basse synth froide et metallique. Souvent un synth bass analogique ou emulation. Poco melodique.",
        "stereo": "Mix souvent etroit et centre. Le Detroit electro est mono et direct.",
        "compression": "Peu de compression - les boites a rythmes ont leur dynamique naturelle. Limiteur doux.",
        "reverb": "Reverbes metalliques et espacees. Delays precis sur les snares. Ambiance froide et futuriste.",
        "eq_master": "Hauts-mids metalliques pour le caractere. Sub pour le kick. Couleur analogique vintage.",
        "erreurs": ["Trop moderne et numerique - perdre le caractere analogique vintage", "Manque de froideur et d atmosphere futuriste", "Sons trop organiques pour le genre"],
        "contexte": "Heritage Detroit electro, Dusseldorf, Berlin. Labels Tresor, Underground Resistance, Gigolo.",
        "arrangement": "Structure minimaliste et repetitive. L ambiance prime sur la progression.",
        "mastering": "LUFS -9/-10. True Peak -0.5 dBTP. Caractere analogique vintage prime.",
        "plateformes": "Beatport Electronica, Bandcamp, Discogs (vinyles importants).",
    },
    "hard dance": {
        "son": "Musique de danse electronique extreme - hardstyle, hardcore, gabber, uptempo. Kicks tres distordus, energie explosive, BPM tres eleve. Brutalite assumee.",
        "kick": "Kick distordu et sature - la signature du hard dance. Corps entre 80-120Hz avec une distorsion intentionnelle et caracterielle.",
        "bass": "Basses courtes et percussives. Sub tres present mais court. L energie vient du kick plus que de la basse.",
        "stereo": "Mix souvent dense mais pas trop large. L energie vient de la densite pas de la largeur.",
        "compression": "Compression et limiteur tres agressifs. Le clipping controle est souvent recherche.",
        "reverb": "Peu de reverbe - le genre est sec et brutal. Quelques effets de delay sur les elements melodiques.",
        "eq_master": "Kick tres present et distordu dans les mids. Sub court et fort. Aigus selon le sous-genre.",
        "erreurs": ["Kick pas assez distordu et caracteriel", "Pas assez energetique et brutal", "BPM trop lent pour le genre"],
        "contexte": "Festivals Decibel, Defqon.1, Q-dance. Public tres specifique et passionne.",
        "arrangement": "Drops massifs apres des builds tres tendus. Structure directe et explosive.",
        "mastering": "LUFS -7/-8. True Peak peut aller haut intentionnellement. Maximum de brutalite.",
        "plateformes": "Beatport Hard Dance, Q-dance Music, Bandcamp.",
    },
    "neo rave": {
        "son": "Rave contemporaine qui fusionne techno, EBM, industrial et pop experimentale. Esthetique Y2K et post-internet. Energique, futuriste et hybride.",
        "kick": "Kick entre la techno et l EBM. Percutant et avec une touche industrielle. Corps entre 80-100Hz.",
        "bass": "Basses hybrides entre synth industriel et bass electronique. Futuristes et caracterielle.",
        "stereo": "Mix large et futuriste. L esthetique neo rave est immersive et spatiale.",
        "compression": "Compression hybride selon les influences. Entre la techno et l EBM.",
        "reverb": "Espaces futuristes et metalliques. Reverbes industrielles avec des elements pop.",
        "eq_master": "Hauts-mids futuristes et metalliques. Sub energique. Brillance contemporaine.",
        "erreurs": ["Trop proche d un seul genre - perdre l hybridation neo rave", "Manque d esthetique futuriste et contemporaine"],
        "contexte": "Clubs avant-garde, festivals experimentaux. Labels PAN, Codes, Bromance. Scene tres actuelle.",
        "arrangement": "Structures hybrides et experimentales. Peut emprunter a la pop autant qu a la techno.",
        "mastering": "LUFS -8. True Peak -0.3 dBTP. Futurisme et energie priment.",
        "plateformes": "SoundCloud, Bandcamp, Spotify (niche mais present).",
    },
    "phonk": {
        "son": "Phonk issu du rap de Memphis, remixe avec des elements trap et cloud rap. 808 tres present, hi-hats trap, atmosphere sombre et vintage, samples de rap des annees 90.",
        "kick": "Kick trap court mais avec une couleur vintage Memphis. Corps entre 80-120Hz avec un caractere lo-fi.",
        "bass": "808 sub dominant - la signature absolue du phonk. Sub tres profond et avec beaucoup de sustain.",
        "stereo": "808 strictement mono. Elements atmospheriques et samples plus larges en stereo.",
        "compression": "Compression trap agressive. 808 peu comprime pour le sustain. Limiteur fort.",
        "reverb": "Peu de reverbe sur le rythme. Samples vintage avec de la saturation et du lo-fi intentionnel.",
        "eq_master": "808 sub tres present (40-70Hz). Hauts-mids pour les hi-hats. Couleur lo-fi et vintage.",
        "erreurs": ["808 pas assez present ou trop court", "Manque de couleur vintage et Memphis", "Trop clean et moderne pour l esthetique phonk"],
        "contexte": "Origin Memphis rap 90s remixe. TikTok, Spotify playlists. Labels SoundCloud underground.",
        "arrangement": "Courtes structures repetitives. Boucles atmospheriques. Pas de structure conventionnelle.",
        "mastering": "LUFS -8. True Peak -1.0 dBTP. La couleur lo-fi et le 808 priment.",
        "plateformes": "SoundCloud, Spotify (tres viral sur TikTok), Apple Music.",
    },
    "future bass": {
        "son": "Musique electronique emotionnelle avec des elements trap et EDM. Chord stabs chillwave, voix pitch-shifties, drops massifs, atmosphere nostalgique et emotionnelle.",
        "kick": "Kick trap mais dans un contexte emotionnel. Corps entre 80-120Hz avec du sustain pour l emotion.",
        "bass": "Synthbass expressifs et emotionnels. Souvent des chord stabs de basse. Tres melodique.",
        "stereo": "Tres large - les chord stabs et pads s etendent au maximum pour l emotion.",
        "compression": "Sidechain pumping caracteristique sur les chord stabs. Compression douce sur la melodie.",
        "reverb": "Longues reverbes emotionnelles sur les synthes et voix. L atmosphere nostalgique vient des reverbes.",
        "eq_master": "Hauts-mids pour les chord stabs. Sub pour le kick trap. Brillance emotionnelle.",
        "erreurs": ["Chord stabs pas assez presents et emotionnels", "Manque d emotion et d atmosphere nostalgique", "Trop proche de l EDM commercial"],
        "contexte": "Labels Monstercat, Odesza Music Group, OWSLA. Festivals EDM et publics jeunes.",
        "arrangement": "Drop avec chord stabs massifs apres un build emotionnel. Structure EDM adaptee.",
        "mastering": "LUFS -8/-9. True Peak -1.0 dBTP. L emotion et les chord stabs priment.",
        "plateformes": "Spotify, Apple Music, SoundCloud. Tres bien streame.",
    },
    "synthwave": {
        "son": "Esthetique retro-futuriste des annees 80. Synthes analogiques ou emulations, boites a rythmes vintages, ambiance cyberpunk ou horror 80s. Nostalgie assumee.",
        "kick": "Kick 808 ou emulation vintage. Corps entre 80-120Hz avec le caractere retro des annees 80.",
        "bass": "Synth bass analogique des annees 80. Chaleur et couleur vintage. Souvent une ligne de basse melodique.",
        "stereo": "Stereo large et cinematique. Les synthes 80s s etalent largement avec du chorus et flanger vintage.",
        "compression": "Compression vintage style SSL ou neve. Saturation de bande pour la chaleur analogique.",
        "reverb": "Grandes reverbes 80s caracteristiques - gated reverb sur les snares, hall sur les synthes.",
        "eq_master": "Chaleur vintage dans les bas-mids. Presence des synthes dans les hauts-mids. Pas trop moderne.",
        "erreurs": ["Trop moderne et numerique - perdre l esthetique 80s", "Manque de reverbe et d ambiance vintage", "Sons trop contemporains pour l esthetique retro"],
        "contexte": "Labels Carpenter Brut Records, FiXT Neon. Communaute synthwave, gamers, retro-futuristes.",
        "arrangement": "Structure souvent proche de la chanson 80s. Introduction, couplet, refrain, solo. Narrative cinematique.",
        "mastering": "LUFS -11. True Peak -1.0 dBTP. L esthetique vintage prime sur la loudness moderne.",
        "plateformes": "Spotify, Bandcamp, Apple Music. Tres bien streame pour le gaming et la concentration.",
    },
    "downtempo": {
        "son": "Musique electronique lente et atmospherique. Trip hop, chillout, ambient electronique. Groove lent, basses profondes, textures riches. Ecoute attentive.",
        "kick": "Kick doux et enveloppant. Corps entre 60-80Hz avec beaucoup de naturel. Pulsation lente et organique.",
        "bass": "Basses profondes et expressives. Souvent une vraie contrebasse ou basse electrique. Chaleur et humanite.",
        "stereo": "Image stereo large et immersive. Les textures s etalent dans tout le champ pour l atmospheRe.",
        "compression": "Compression douce et transparente. La dynamique naturelle est valorisee. Peu de sidechain.",
        "reverb": "Grandes reverbes atmospheriques. L ambiance et la profondeur viennent des reverbes et delays.",
        "eq_master": "Chaleur dans les bas-mids. Pas de brillance excessive. L objectif est doux, chaud et profond.",
        "erreurs": ["Trop energetique pour le genre lent et contemplatif", "Manque d atmospheRe et de profondeur", "LUFS trop eleve qui detruit la dynamique"],
        "contexte": "Labels Ninja Tune, Warp, Mo Wax. Ecoute casque, bars, concentration. Heritage trip hop.",
        "arrangement": "Evolution tres lente et subtile. Pas de structure dancefloor. Narrative atmospherique.",
        "mastering": "LUFS -13/-14. Dynamique naturelle preservee. True Peak -1.0 dBTP.",
        "plateformes": "Spotify (tres bien streame pour le travail), Bandcamp, Apple Music.",
    },
    "jersey club": {
        "son": "Genre de club electronique de New Jersey. Sampling vocal caracteristique (coupures vocales rythmiques), drums rapides et syncopees, bass trap. Genre de danse.",
        "kick": "Kick trap dans un pattern jersey club caracteristique (pas sur le 1 et le 3 comme d habitude). Tres syncopé.",
        "bass": "Basse trap 808 ou sub court et percutant. Elle dialogue avec les patterns rhythmiques complexes.",
        "stereo": "Mix direct et centre. L energie vient du groove des drums pas de la largeur.",
        "compression": "Compression trap agressive. Les drums sont tres compresses pour le punch.",
        "reverb": "Peu de reverbe - le genre est direct et dansant. Quelques effets courts sur les samples vocaux.",
        "eq_master": "Hauts-mids pour les samples vocaux. Sub pour la basse. Punch des drums en avant.",
        "erreurs": ["Pattern de drums pas assez characteristique jersey club", "Samples vocaux pas assez mis en avant", "Trop proche du trap classique sans le groove jersey"],
        "contexte": "Heritage New Jersey et New York. Labels Sneak Recordings, Jersey Club Music. Dancefloors urbains.",
        "arrangement": "Structures courtes et directes. Tres centre sur les patterns de drums et les samples vocaux.",
        "mastering": "LUFS -8. True Peak -0.5 dBTP. Le punch des drums est prioritaire.",
        "plateformes": "SoundCloud, Spotify (viral sur TikTok), Beatport.",
    },
    "bass club": {
        "son": "Bass music experimentale pour les clubs. Fusion d influences - grime, dubstep, techno - dans une approche tres avant-garde et underground UK.",
        "kick": "Kick entre les genres - percutant et avec du caractere bass music. Corps entre 70-100Hz.",
        "bass": "Basses experimentales et hybrides. L innovation sonore est valorisee.",
        "stereo": "Mix selon l approche artistique. Peut etre tres large ou tres centre selon le morceau.",
        "compression": "Approche variable selon l artiste. Pas de standard fixe pour ce genre experimental.",
        "reverb": "Variable selon l approche artistique et le sous-style.",
        "eq_master": "Pas de standard fixe. L innovation sonore est plus importante que les conventions.",
        "erreurs": ["Trop conventionnel pour ce genre experimental", "Manque de vision artistique claire"],
        "contexte": "Labels Hyperdub, Night Slugs, PAN. Clubs underground londoniens. Public tres connaisseur.",
        "arrangement": "Tres variable et experimental. Pas de structure conventionnelle imposee.",
        "mastering": "LUFS -9/-10. True Peak -0.5 dBTP. Variable selon l approche artistique.",
        "plateformes": "Bandcamp, SoundCloud. Tres underground et communautaire.",
    },
    "dnb": {
        "son": "Drum and Bass general - le terme umbrella pour tout le genre. Sub halftempo dominant, breaks a 170 BPM, energie dancefloor. Tres large palette de sous-genres.",
        "kick": "Kick halftempo dominant, corps entre 60-80Hz. Il EST le centre du mix avec la basse.",
        "bass": "Sub halftempo massif et expressif. La basse et le kick fonctionnent comme une unite indissociable.",
        "stereo": "Sub mono strict. Breaks larges en stereo. Contraste fort entre la basse mono et les breaks stereo.",
        "compression": "Transient shaping sur les breaks. Sidechain basse sur le kick. Limiteur a -8 LUFS.",
        "reverb": "Reverbe de salle sur les breaks. Peu sur la basse. Delays sur les elements melodiques.",
        "eq_master": "Sub tres present. Coupure dans les 200-300Hz pour la clarte. Hauts-mids pour les breaks.",
        "erreurs": ["Sub trop court ou trop timide", "Breaks sans punch ni caractere", "BPM pas dans la plage DnB (165-180)"],
        "contexte": "Labels Metalheadz, Ram Records, Hospital Records. Clubs DnB speciaux, festivals.",
        "arrangement": "Deux drops principaux, breakdown au milieu. Structure DnB classique.",
        "mastering": "LUFS -8. True Peak -0.3 dBTP.",
        "plateformes": "Beatport DnB, Spotify, SoundCloud.",
    },
    "uk garage": {
        "son": "Genre britannique de club des annees 90-2000. 2-step groove caracteristique, voix R&B, swing unique, basses UK profondes. Heritage direct du grime et du dubstep.",
        "kick": "Kick 2-step caracteristique - pas sur les temps conventionnels. Swing et groove UK garage unique.",
        "bass": "Basse profonde et melodique avec swing UK. Corps entre 60-150Hz avec la chaleur du garage.",
        "stereo": "Image stereo moderee. Le UK garage est centre mais avec de la chaleur dans les mids.",
        "compression": "Compression douce sur le bus. Le swing naturel des drums est preserve.",
        "reverb": "Reverbes de salle sur les voix et percussions. Delays sur les elements melodiques.",
        "eq_master": "Chaleur dans les bas-mids. Presence vocale dans les hauts-mids. Clarte du swing.",
        "erreurs": ["Groove 2-step pas assez caracteristique", "Manque de chaleur et d ame UK garage", "Voix pas assez mises en avant"],
        "contexte": "Heritage UK 1995-2001. Labels Defected, Locked On, Sour. Clubs UK, renaissance recente.",
        "arrangement": "Structure proche de la chanson pop mais avec le groove 2-step. Voix importantes.",
        "mastering": "LUFS -9. True Peak -1.0 dBTP.",
        "plateformes": "Beatport UK Garage, Spotify, Apple Music.",
    },
    "drill": {
        "son": "Evolution sombre du trap. Origines Chicago puis UK drill. 808 sinistres, hi-hats glissantes, ambiance menaçante, voix rap agressives.",
        "kick": "Kick trap court avec une couleur sombre et menaçante. Corps entre 80-120Hz avec une presence intimidante.",
        "bass": "808 sub sombre et glissant. Plus sinistre et menaçant que le trap classique. Slides lents.",
        "stereo": "808 mono strict. Elements atmospheriques et sombres plus larges en stereo.",
        "compression": "Compression trap agressive. Limiteur fort. Le drill est compresse et dense.",
        "reverb": "Reverbe longue sur le snare - caracteristique. Delay sur la voix. Atmosphere sombre.",
        "eq_master": "808 sub tres present. Hauts-mids pour les hi-hats glissantes. Atmosphere sombre.",
        "erreurs": ["808 pas assez sinistre et menaçant", "Manque d atmosphere sombre et froide", "Hi-hats glissantes pas assez presentes"],
        "contexte": "Origins Chicago, puis UK drill (Londres). Labels OVO Sound (UK drill). Hip-hop underground.",
        "arrangement": "Structures rap classiques - couplets, hook. Plus lent et plus sombre que le trap.",
        "mastering": "LUFS -8. True Peak -1.0 dBTP. L atmosphere sombre prime.",
        "plateformes": "Spotify, Apple Music, YouTube. Tres grand public.",
    },
    "boom bap": {
        "son": "Hip-hop classique des annees 90. Kick et snare tres presents (boom = kick, bap = snare), samples de jazz et soul, flow de rap conscient. L hip-hop original.",
        "kick": "Kick boom caracteristique - court et percutant avec un corps entre 80-120Hz. Il doit claquer.",
        "bass": "Basse de contrebasse ou de basse electrique sampled. Chaleur vintage et groove naturel.",
        "stereo": "Image stereo moderee. Le boom bap est centre et focus. Pas de stereo excessive.",
        "compression": "Compression vintage (style SSL ou Neve). Saturation de bande pour la chaleur. Glue compressor.",
        "reverb": "Reverbe vintage de salle sur les drums. Peu de reverbe digitale froide. Chaleur analogique.",
        "eq_master": "Chaleur vintage dans les bas-mids. Presence du kick et snare. Warmth analogique.",
        "erreurs": ["Kick et snare pas assez presents et impactants", "Manque de chaleur vintage et analogique", "Samples trop propres et sans caractere"],
        "contexte": "Heritage New York 1988-1998. Labels Rawkus, Loud Records, Columbia. Hip-hop conscient et underground.",
        "arrangement": "Couplet, hook, couplet, hook, bridge, hook. Structure rap classique avec samples en boucle.",
        "mastering": "LUFS -10. True Peak -1.0 dBTP. La chaleur vintage prime.",
        "plateformes": "Spotify, Apple Music, Bandcamp. Large audience hip-hop.",
    },
    "hardstyle": {
        "son": "Musique electronique de danse extreme neerlandaise. Kicks tres distordus et caracteristiques, melodie trance par-dessus, energie explosive de festival.",
        "kick": "Kick hardstyle - le son le plus caracteristique du genre. Tres distordu, long, avec un corps massif entre 80-150Hz. C est le coeur du genre.",
        "bass": "Sub tres court entre les kicks. L energie vient du kick pas de la basse continue.",
        "stereo": "Mix energetique et large sur les melodics. Kick central et mono.",
        "compression": "Compression et saturation tres agressives sur le kick. Limiteur fort sur le master.",
        "reverb": "Reverbe sur les elements melodiques trance. Le kick reste relativement sec pour la clarte.",
        "eq_master": "Kick tres present et distordu dans les mids. Melodie trance dans les hauts-mids. Sub court.",
        "erreurs": ["Kick pas assez distordu et caracteriel - c est la signature du genre", "Melodie trance pas assez presente", "Pas assez energetique pour les festivals"],
        "contexte": "Festivals Defqon.1, Qlimax, Decibel (Pays-Bas). Labels Scantraxx, Dirty Workz.",
        "arrangement": "Builds tres tendus, drops massifs, peu de breakdowns longs. Energie maintenue.",
        "mastering": "LUFS -7/-8. True Peak peut aller tres haut. Maximum d energie.",
        "plateformes": "Beatport Hard Dance, Q-dance Music, Spotify (niche mais present).",
    },
    # ── Autres genres ─────────────────────────────────────────────────────────
    "grime": {
        "son": "Genre britannique urbain ne a Londres Est. Instrumentaux froids et metalliques a 140 BPM, voix MC agressives, basses profondes, esthetique urbaine et sombre.",
        "kick": "Kick grime caracteristique - entre le garage et l electronique. Corps entre 80-100Hz avec un caractere froid.",
        "bass": "Basses froides et metalliques. Souvent des synthbass ou des subs sinistres. Tres specifiquement britanniques.",
        "stereo": "Mix souvent etroit et centre. L energie vient de la densite des elements pas de la largeur.",
        "compression": "Compression directe et sans fioriture. Le grime est brut et sans artifice.",
        "reverb": "Peu de reverbe - le grime est sec et urbain. Quelques effets courts caracteristiques.",
        "eq_master": "Presence dans les mids pour les MCing. Basses froides dans les graves. Aigus metalliques.",
        "erreurs": ["Trop poli et lisse - le grime est brut et urbain", "Manque de froideur et d esthetique UK", "BPM inadequat - doit etre autour de 140"],
        "contexte": "Heritage East London 2000-2005. Labels XL Recordings, Boy Better Know. Grime revivalisme actuel.",
        "arrangement": "Structures courtes avec de l espace pour les MCs. Repetitif et hypnotique.",
        "mastering": "LUFS -8. True Peak -0.5 dBTP. Le caractere urbain prime sur la polishness.",
        "plateformes": "Spotify, Apple Music, YouTube. Grand public UK et international.",
    },
    "rnb": {
        "son": "R&B contemporain. Voix ultra-presente et expressive, production luxueuse, 808 sub, melodies simples et accrocheuses. L emotion vocale avant tout.",
        "kick": "Kick doux et enveloppant, corps entre 80-100Hz. Il soutient la voix sans jamais la dominer.",
        "bass": "808 sub ou basse electrique expressive. Chaleur et profondeur. Elle sert la voix harmoniquement.",
        "stereo": "Large et luxueux. Les BV s etalent largement. La voix lead est centrale et presente.",
        "compression": "Voix tres compressee pour une presence constante. Production tres soignee et polie.",
        "reverb": "Reverbes creatives et expressives sur la voix. L espace sert l emotion vocale.",
        "eq_master": "Presence vocale dans les hauts-mids (2-6kHz). Sub pour le 808. Brillance luxueuse.",
        "erreurs": ["Voix pas assez presente - elle doit dominer absolument", "Production trop froide et mecanique", "808 qui entre en conflit avec la voix"],
        "contexte": "Labels Columbia, Def Jam, Atlantic. Radio, Spotify, Apple Music. Grand public mondial.",
        "arrangement": "Intro, couplet, pre-refrain, refrain, couplet, refrain, bridge, refrain final. Structure radio.",
        "mastering": "LUFS -9/-10. True Peak -1.0 dBTP. La voix doit etre claire et belle.",
        "plateformes": "Spotify, Apple Music, Tidal, YouTube. Distribution maximale.",
    },
    "hardcore": {
        "son": "Hardcore electronique - gabber, speedcore, terrorcore. Tempo extremement eleve (160-200+ BPM), kicks distordus et satures, energie brute et extremiste.",
        "kick": "Kick gabber caracteristique - tres distordu, sature, massif. Corps entre 80-150Hz avec distorsion totale. Sature intentionnellement.",
        "bass": "Basse tres courte entre les kicks. L energie vient entierement du kick.",
        "stereo": "Mix tres dense et souvent etroit. L energie vient de la densite et de la saturation.",
        "compression": "Compression et saturation poussees a l extreme. Le clipping est une esthetique.",
        "reverb": "Peu de reverbe - le genre est brutal et direct. Quelques effets chaotiques.",
        "eq_master": "Kick distordu tres present dans tous les mids. Sub court et brutal. Aigus agressifs.",
        "erreurs": ["Kick pas assez brutal et distordu", "Pas assez extreme en energie et en BPM", "Trop poli pour le genre"],
        "contexte": "Festivals Thunderdome, Masters of Hardcore. Labels Rotterdam Records, Mokum Records.",
        "arrangement": "Structure directe et brutale. L energie est maintenue au maximum en permanence.",
        "mastering": "LUFS -7. True Peak au maximum intentionnellement. Brutalite absolue.",
        "plateformes": "Beatport Hard Dance, Bandcamp, SoundCloud. Tres niche.",
    },
    "rock": {
        "son": "Rock contemporain. Guitares electriques saturees, batterie live puissante, basse electrique, voix rock. Entre heritage classique et modernite de production.",
        "kick": "Kick de batterie live - souvent une Kick naturelle treitement. Corps entre 80-120Hz avec le caractere du live.",
        "bass": "Basse electrique - profonde et melodique. Elle groove avec le kick en permanence.",
        "stereo": "Guitares en stereo large (gauche-droite). Voix centrale. Batterie large. Image stereo rock classique.",
        "compression": "Compression vintage de rock. Saturation naturelle des guitares. Glue compressor sur le bus.",
        "reverb": "Reverbe de salle live sur la batterie. Plate sur la voix. Delay sur les guitares solos.",
        "eq_master": "Hauts-mids pour les guitares (2-5kHz). Presence de la voix. Sub pour le kick live.",
        "erreurs": ["Sons trop digitaux et sans energie rock", "Guitares pas assez presentes", "Manque de dynamique et d energie live"],
        "contexte": "Labels Island, Atlantic, Interscope. Concerts, radio, Spotify. Large audience mondiale.",
        "arrangement": "Couplet, refrain, couplet, refrain, pont, refrain. Structure rock et pop classique.",
        "mastering": "LUFS -10/-11. True Peak -1.0 dBTP. Energie et caractere rock priment.",
        "plateformes": "Spotify, Apple Music, YouTube, radio. Distribution universelle.",
    },
    "jazz": {
        "son": "Jazz contemporain ou classique. Instruments acoustiques (piano, contrebasse, batterie, saxo, trompette), improvisation, dynamique naturelle preservee. La liberté artistique.",
        "kick": "Grosse caisse de batterie acoustique. Corps entre 80-150Hz avec toute la naturete du live. Pas d intervention electronique.",
        "bass": "Contrebasse ou basse electrique jazz. Walking bass caracteristique. Chaleur naturelle et organique.",
        "stereo": "Image stereo naturelle du placement des instruments en studio. Pas de stereo artificielle.",
        "compression": "Tres peu ou pas de compression. La dynamique naturelle des musiciens est la valeur supreme.",
        "reverb": "Reverbe de salle de studio live ou de concert. Naturelle et non artificielle.",
        "eq_master": "Tres peu d EQ - l objectif est de preserver le son naturel des instruments. Correction minimale.",
        "erreurs": ["Trop de compression qui enleve la dynamique naturelle", "Sons trop artificiels et electroniques", "Manque de naturel et d organicite"],
        "contexte": "Labels Blue Note, ECM, Verve. Clubs de jazz, concerts, ecoute attentive. Audience sophistiquee.",
        "arrangement": "Forme AABA ou blues ou libre. Improvisations importantes. Pas de structure electronique.",
        "mastering": "LUFS -14/-16. Dynamique naturelle absolument preservee. True Peak -1.0 dBTP.",
        "plateformes": "Spotify (grosse audience jazz), Tidal (qualite lossless appreciee), Apple Music.",
    },
    "soul": {
        "son": "Soul contemporaine ou classique. Voix gospel expressives, instruments live, production chaude et emotionnelle. La musique de l ame noire americaine.",
        "kick": "Kick de batterie live chaud et naturel. Corps entre 70-90Hz avec l organicite du live.",
        "bass": "Basse electrique groove et melodique. Dialogue constant avec la voix et les accords.",
        "stereo": "Large et chaud. Les cuivres et cordes s etalent en stereo. La voix lead est centrale.",
        "compression": "Compression douce et vintage qui preserve les dynamiques vocales. LA-2A ou 1176.",
        "reverb": "Reverbe de church ou de grande salle sur les voix. Ambiance soul et gospel.",
        "eq_master": "Chaleur dans les bas-mids. Presence vocale dans les hauts-mids. Clarte et emotion.",
        "erreurs": ["Voix pas assez expressive et presente", "Production trop froide et mecanique", "Manque de chaleur et d ame"],
        "contexte": "Labels Motown, Atlantic, Stax. Radio, Spotify, Apple Music. Audience emotionnelle large.",
        "arrangement": "Couplet, refrain, pont avec montee emotionnelle, refrain final. Structure soul classique.",
        "mastering": "LUFS -11. True Peak -1.0 dBTP. La chaleur et l emotion vocale priment.",
        "plateformes": "Spotify, Apple Music, Tidal. Large audience emotionnelle.",
    },
    "funk": {
        "son": "Funk classique ou contemporain. Groove rythmique extreme, basse slap et syncopee, guitares rythmiques staccato, cuivres, batterie tres groovy. Le groove avant tout.",
        "kick": "Kick funk - court, percutant, avec du groove naturel. Corps entre 80-120Hz. Il doit danser.",
        "bass": "Basse funk slap - la star du genre. Mobile, syncopee, expressive. Elle groove en permanence.",
        "stereo": "Guitares rythmiques en stereo. Cuivres larges. Voix centrale. Image stereo funk classique.",
        "compression": "Compression vintage qui accentue le groove. Saturation de bande pour la chaleur.",
        "reverb": "Reverbe de studio live sur la batterie. Ambiance studio analogique annees 70.",
        "eq_master": "Presence de la basse slap. Guitars rythmiques dans les mids. Cuivres dans les hauts-mids.",
        "erreurs": ["Basse pas assez groovante et syncopee", "Manque de groove et de swing dans la batterie", "Sons trop modernes et digitaux"],
        "contexte": "Heritage James Brown, Motown. Labels Daptone, Stones Throw (neo-soul). Concerts, clubs.",
        "arrangement": "Structure centree sur le groove. Vamps et breaks instrumentaux. Voix au service du groove.",
        "mastering": "LUFS -10. True Peak -1.0 dBTP. Le groove prime sur tout.",
        "plateformes": "Spotify, Apple Music, Bandcamp. Large audience groove et soul.",
    },
    "reggae": {
        "son": "Reggae jamaicain. Riddim caracteristique (accent sur le 2 et le 4), basse profonde et melodique, guitares on-beat, voix rastafarienne. Message et spiritualite.",
        "kick": "Kick reggae sur les 1 et 3 mais souvent moins present que dans d autres genres. Corps entre 60-80Hz doux.",
        "bass": "Basse reggae - la plus importante du genre. Melodique, profonde, avec beaucoup de sustain. Elle EST le coeur.",
        "stereo": "Image stereo moderate. Les guitares rhythm (skank) en stereo. Voix centree.",
        "compression": "Peu de compression - le reggae respire naturellement. Quelques effets dub.",
        "reverb": "Delay et echo caracteristiques du reggae (heritage dub). Reverbe naturelle de salle.",
        "eq_master": "Basse tres presente et melodique. Presence des guitares rythmiques. Clarte vocale.",
        "erreurs": ["Basse pas assez presente et melodique - elle est la star", "Manque de riddim et de swing caracteristique", "Accent rythmique pas sur le 2 et le 4"],
        "contexte": "Heritage jamaicain. Labels Greensleeves, VP Records, Tuff Gong. International et universel.",
        "arrangement": "Verse, chorus, verse, chorus, rythm break, chorus. Structure reggae classique.",
        "mastering": "LUFS -11. True Peak -1.0 dBTP. La basse et le riddim priment.",
        "plateformes": "Spotify, Apple Music, YouTube. Audience mondiale et internationale.",
    },

    "melodic house techno": {
        "son": "Fusion entre melodic techno et house. Synthes emotionnels, kick techno, groove house. Le genre phare d Anyma, Tale Of Us, Afterlife.",
        "kick": "Kick entre techno et house - percutant mais avec de la rondeur house. Corps entre 70-90Hz.",
        "bass": "Basse melodique et groovante, plus house que techno. Sub mono propre.",
        "stereo": "Large sur les pads et synthes melodiques. Sub et kick en mono.",
        "compression": "Sidechain subtil et musical. Compression douce qui preserve l emotion.",
        "reverb": "Longues reverbes sur les synthes. Profondeur et emotion sont les mots cles.",
        "eq_master": "Hauts-mids doux pour les melodies. Controle des bas-mids pour la clarte.",
        "erreurs": ["Trop froid et techno - perdre le groove house", "Melodies pas assez emotionnelles", "Mix trop compresse qui etouffe l emotion"],
        "contexte": "Labels Afterlife, ANYMA Music, Afterlife. Cercle, festivals premium. Public international.",
        "arrangement": "Arc emotionnel - montee progressive, breakdown intense, drop emotionnel.",
        "mastering": "LUFS -10/-11. True Peak -1.0 dBTP. L emotion et la melodie priment.",
        "plateformes": "Beatport, Spotify, Apple Music. Tres bien streame.",
    },
    "tribal house": {
        "son": "House avec percussions tribales et ethniques. Congas, djembes, percussions africaines ou amerindiennes melangees avec une production house moderne.",
        "kick": "Kick house profond et organique. Corps entre 60-80Hz avec beaucoup de naturel.",
        "bass": "Basse melodique et expressive. Elle dialogue avec les percussions tribales.",
        "stereo": "Les percussions tribales s etalent largement en stereo pour la richesse rythmique.",
        "compression": "Compression douce qui preserve le naturel des percussions live.",
        "reverb": "Reverbes naturelles sur les percussions. Ambiance de ceremonie ou de festival.",
        "eq_master": "Richesse dans les mids pour les percussions. Chaleur dans les bas-mids.",
        "erreurs": ["Percussions trop digitales et froides", "Manque de groove tribal et organique"],
        "contexte": "Labels Nervous Records, Strictly Rhythm, Tribal America. Clubs new-yorkais, ibiza.",
        "arrangement": "Structure house avec percussions progressives. Les rythmes tribaux s accumulent.",
        "mastering": "LUFS -10. True Peak -1.0 dBTP.",
        "plateformes": "Beatport, Traxsource, SoundCloud.",
    },
    "psytrance": {
        "son": "Psytrance general - le terme umbrella pour tout le genre. Kicks percutants a 143-148 BPM, lignes acides, elements psychedeliques, festivals plein air.",
        "kick": "Kick psytrance caracteristique - tres percutant, corps entre 80-100Hz, attaque ultra-rapide.",
        "bass": "Basse acide et robotique. Sub mono tres present sous le kick.",
        "stereo": "Mix energetique et dense. L energie vient de la densite plus que de la largeur.",
        "compression": "Compression forte. Sidechain entre kick et basse. Limiteur pousse.",
        "reverb": "Peu de reverbe directe. Elements psychedeliques dans les textures.",
        "eq_master": "Sub fort pour le kick. Hauts-mids pour les elements acides.",
        "erreurs": ["Kick pas assez percutant", "BPM trop lent pour le genre", "Manque d energie et de momentum"],
        "contexte": "Festivals Ozora, Boom, Rainbow Serpent. Systemes plein air. Communaute mondiale.",
        "arrangement": "Structure repetitive et hypnotique. Energie maintenue haute.",
        "mastering": "LUFS -8. True Peak -0.3 dBTP.",
        "plateformes": "Beatport Psy-Trance, Bandcamp, SoundCloud.",
    },
    "jump up dnb": {
        "son": "DnB maximaliste et festif. Heavy bass wobbles, breaks percutants, energie maximale, crowd-pleasing. Le DnB de festival par excellence.",
        "kick": "Kick massif et court, corps entre 70-90Hz. Doit claquer fort sur les systemes.",
        "bass": "Basses wobble massives et expressives. Halftempo dominant. L impact physique est la priorite.",
        "stereo": "Sub mono strict. Breaks larges. Tres fort contraste pour l energie maximale.",
        "compression": "Transient shaping tres agressif sur les breaks. Sidechain fort. Limiteur pousse.",
        "reverb": "Peu de reverbe - le jump up est sec et impactant. Direct et sans fioritures.",
        "eq_master": "Sub massif. Hauts-mids pour les breaks qui coupent. Maximum de punch.",
        "erreurs": ["Basses pas assez massives", "Breaks sans punch ni energie", "Trop subtil pour ce genre maximaliste"],
        "contexte": "Labels Renegade Hardware, Playaz, Ganja Records. Clubs DnB, festivals. Public festif.",
        "arrangement": "Drops massifs, peu de breakdowns, energie maximale en permanence.",
        "mastering": "LUFS -8. True Peak -0.3 dBTP. Maximum de punch et d impact.",
        "plateformes": "Beatport DnB, SoundCloud, Spotify.",
    },
    "dubstep": {
        "son": "Dubstep general - wobble bass caracteristique, kicks percutants a 140 BPM, drops massifs. Entre brostep commercial et deep dubstep underground.",
        "kick": "Kick percutant et profond. Corps entre 70-90Hz avec de la presence. Doit soutenir le wobble.",
        "bass": "Wobble bass caracteristique - le son signature du dubstep. LFO sur le filtre synth.",
        "stereo": "Sub mono strict. Wobble bass peut etre legerement stereo. Breaks larges.",
        "compression": "Sidechain entre kick et wobble. Compression forte pour le punch.",
        "reverb": "Variable selon le sous-style. Plus long pour le deep dubstep, plus court pour le brostep.",
        "eq_master": "Sub profond. Hauts-mids pour la presence du wobble. Coupure dans les 200-400Hz.",
        "erreurs": ["Wobble bass pas assez presente ou caracteristique", "Sub pas assez profond et present"],
        "contexte": "Labels Never Say Die, OWSLA, Deep Medi. Clubs bass music, festivals.",
        "arrangement": "Build tendu, drop massif avec wobble, breakdown, deuxieme drop.",
        "mastering": "LUFS -8/-9. True Peak -0.3 dBTP.",
        "plateformes": "Beatport Dubstep, SoundCloud, Spotify.",
    },
    "afrobeats": {
        "son": "Pop africaine moderne. Fusion de highlife, fuji, juju et production contemporaine. Voix africaines, percussions riches, groove solaire. Marche mondial en pleine explosion.",
        "kick": "Kick chaud et groove, corps entre 70-90Hz. Il participe au groove africain tres caracteristique.",
        "bass": "Basse melodique et groovante avec influence africaine. Elle dialogue avec les percussions.",
        "stereo": "Large et solaire. Les percussions et voix s etalent pour la richesse rythmique.",
        "compression": "Compression douce et musicale. Le groove naturel est preserve.",
        "reverb": "Reverbes d ambiance sur les voix et percussions. Atmosphere festive et solaire.",
        "eq_master": "Chaleur dans les mids. Presence vocale. Clarte des percussions dans les hauts.",
        "erreurs": ["Groove pas assez africain et caracteristique", "Voix pas assez mises en avant", "Production trop froide et europeenne"],
        "contexte": "Labels Afrobeats Intelligence, Spaceship Entertainment. Marche mondial en explosion.",
        "arrangement": "Structure pop avec des elements africains. Voix tres importantes.",
        "mastering": "LUFS -9. True Peak -1.0 dBTP.",
        "plateformes": "Spotify (enorme en Afrique et diaspora), Apple Music, YouTube.",
    },

    "default": {
        "son": "Production électronique générique. Applique les standards de l'industrie.",
        "kick": "Kick propre et punchy, corps entre 80-100Hz, transiente définie.",
        "bass": "Basse claire et mono sous 100Hz, corps chaleureux entre 100-200Hz.",
        "stereo": "Sub et basses en mono. Mids et aigus peuvent être stéréo.",
        "compression": "Compression transparente et douce. Préserver la dynamique naturelle.",
        "reverb": "Réverbe appropriée au contexte. Ne pas noyer le mix.",
        "eq_master": "Équilibre spectral neutre. High-pass à 30Hz, égalisation corrective minimale.",
        "erreurs": ["Déséquilibre fréquentiel, saturation involontaire, stéréo mal gérée."],
        "contexte": "Distribution multi-plateformes.",
        "arrangement": "Structure adaptée au genre et au contexte de diffusion.",
        "mastering": "LUFS -14 Spotify, -9 Beatport. True Peak -1.0 dBTP.",
        "plateformes": "Spotify, Apple Music, Beatport selon le genre.",
    },
}

def analyser_coherence_refs(refs_analyse, donnees_mix, genre, profil):
    """
    Analyse la cohérence entre les références, détecte les outliers,
    et calcule la pondération dynamique pour le mode hybride.
    """
    if not refs_analyse:
        return None

    n = len(refs_analyse)

    # ── Extraire les vecteurs de chaque référence ─────────────────────────
    def get_vec(rd):
        dyn = rd["dynamique"]; freq = rd["frequentiel"]; ster = rd["stereo"]; ryt = rd["rythme"]
        return {
            "lufs":    dyn.get("lufs_integrated", dyn.get("lufs_approx", -11)),
            "sub":     freq["sub_basses_pct"],
            "basses":  freq["basses_pct"],
            "mids":    freq["mids_pct"],
            "stereo":  ster["largeur_stereo"],
            "bpm":     ryt["bpm"],
            "centroid":freq["centroide_hz"],
            "crest":   dyn.get("crest_factor_db", 8),
        }

    vecs = [get_vec(rd) for rd in refs_analyse]
    dims = list(vecs[0].keys())

    # ── Score de cohérence par dimension (1 - CV normalisé) ───────────────
    import statistics
    dim_scores = {}
    for d in dims:
        vals = [v[d] for v in vecs]
        mean = statistics.mean(vals)
        if mean == 0 or n < 2:
            dim_scores[d] = 1.0
            continue
        cv = statistics.stdev(vals) / abs(mean) if n > 1 else 0
        dim_scores[d] = max(0.0, min(1.0, 1.0 - cv))

    coherence_globale = round(sum(dim_scores.values()) / len(dim_scores), 3)

    # ── Détection d'outlier (si 3 refs) ───────────────────────────────────
    outlier_idx = None
    outlier_raison = ""
    if n >= 3:
        # Distance de chaque ref au centroïde des autres
        distances = []
        for i, v in enumerate(vecs):
            autres = [vecs[j] for j in range(n) if j != i]
            dist = 0
            for d in dims:
                mean_autres = sum(a[d] for a in autres) / len(autres)
                std_autres  = max(1e-6, abs(mean_autres) * 0.3)
                dist += ((v[d] - mean_autres) / std_autres) ** 2
            distances.append(dist ** 0.5)

        max_dist = max(distances)
        if max_dist > 2.5:  # seuil : 2.5 écarts-types normalisés
            outlier_idx = distances.index(max_dist)
            # Identifier pourquoi
            v_out = vecs[outlier_idx]
            autres = [vecs[j] for j in range(n) if j != outlier_idx]
            raisons_out = []
            for d in ["bpm", "lufs", "sub", "centroid"]:
                mean_a = sum(a[d] for a in autres) / len(autres)
                ecart = abs(v_out[d] - mean_a)
                seuils = {"bpm": 20, "lufs": 3, "sub": 8, "centroid": 1000}
                if ecart > seuils.get(d, 99):
                    raisons_out.append(f"{d}={round(v_out[d],1)} vs cluster={round(mean_a,1)}")
            outlier_raison = " | ".join(raisons_out) if raisons_out else "style global différent"

    # ── Cluster principal (refs sans outlier) ─────────────────────────────
    cluster_indices = [i for i in range(n) if i != outlier_idx]
    cluster_refs    = [vecs[i] for i in cluster_indices]

    def cluster_mean(d):
        return sum(v[d] for v in cluster_refs) / len(cluster_refs)

    # ── Tendances communes (présentes sur ≥ 66% des refs du cluster) ──────
    tendances = []
    if cluster_mean("sub") > 16:
        tendances.append(f"sub-basses prononcées (>{round(cluster_mean('sub'),0)}%)")
    if cluster_mean("lufs") > -8:
        tendances.append(f"mix chaud/compressé (LUFS {round(cluster_mean('lufs'),1)})")
    if cluster_mean("lufs") < -13:
        tendances.append(f"mix dynamique/aéré (LUFS {round(cluster_mean('lufs'),1)})")
    if cluster_mean("stereo") > 0.5:
        tendances.append(f"image stéréo large (>{round(cluster_mean('stereo'),2)})")
    if cluster_mean("stereo") < 0.15:
        tendances.append(f"mix concentré/mono (stéréo {round(cluster_mean('stereo'),2)})")
    if cluster_mean("bpm") > 0:
        tendances.append(f"tempo {round(cluster_mean('bpm'),0)} BPM")

    # ── Écart refs vs standards du genre ──────────────────────────────────
    ecarts_genre = {}
    for d in ["lufs", "sub", "basses", "mids", "stereo"]:
        cible_map = {"lufs": profil["lufs"], "sub": profil["sub"],
                     "basses": profil["basses"], "mids": profil["mids"],
                     "stereo": profil["stereo"]}
        cible = cible_map.get(d)
        if cible and cible != 0:
            ecart_pct = abs(cluster_mean(d) - cible) / abs(cible) * 100
            ecarts_genre[d] = round(ecart_pct, 1)

    ecart_moyen_genre = round(sum(ecarts_genre.values()) / len(ecarts_genre), 1) if ecarts_genre else 0

    # ── Pondération dynamique hybride ─────────────────────────────────────
    # Genre pèse plus si refs hétérogènes ou proches du genre
    # Refs pèsent plus si cohérentes et éloignées du genre (intention artistique)
    if coherence_globale >= 0.75 and ecart_moyen_genre > 30:
        poids_refs, poids_genre = 75, 25
        ponderation_label = "Références prioritaires (cohérentes et style distinct)"
    elif coherence_globale >= 0.75 and ecart_moyen_genre <= 15:
        poids_refs, poids_genre = 50, 50
        ponderation_label = "Équilibré (refs cohérentes et alignées avec le genre)"
    elif coherence_globale >= 0.75:
        poids_refs, poids_genre = 65, 35
        ponderation_label = "Références légèrement prioritaires (cohérentes)"
    elif coherence_globale < 0.50:
        poids_refs, poids_genre = 30, 70
        ponderation_label = "Genre prioritaire (références hétérogènes)"
    else:
        poids_refs, poids_genre = 50, 50
        ponderation_label = "Équilibré (cohérence moyenne)"

    # Label cohérence
    if coherence_globale >= 0.80:
        coherence_label = "Forte — les refs partagent le même ADN sonore"
    elif coherence_globale >= 0.60:
        coherence_label = "Moyenne — quelques différences stylistiques"
    else:
        coherence_label = "Faible — références hétérogènes"

    return {
        "coherence_globale":  coherence_globale,
        "coherence_label":    coherence_label,
        "outlier_idx":        outlier_idx,
        "outlier_raison":     outlier_raison,
        "cluster_indices":    cluster_indices,
        "tendances":          tendances,
        "ecart_moyen_genre":  ecart_moyen_genre,
        "ecarts_genre":       ecarts_genre,
        "poids_refs":         poids_refs,
        "poids_genre":        poids_genre,
        "ponderation_label":  ponderation_label,
        "cluster_means":      {d: round(cluster_mean(d), 2) for d in dims},
        "dim_scores":         dim_scores,
    }


def diag(valeur, cible, label, genre, unite=""):
    if cible == 0:
        return f"{label}: {valeur}{unite} (reference {genre}: {cible}{unite})"
    diff = valeur - cible
    pct = abs(diff) / abs(cible) * 100
    if pct < 15:
        statut = "OK"
    elif diff > 0:
        statut = "ELEVE"
    else:
        statut = "BAS"
    return f"{label}: {valeur}{unite} (reference {genre}: {cible}{unite}) -> {statut}"

BPM_CONTEXTES = {
    "jazz": [
        (0,   100, "slow jazz / ballad"),
        (100, 140, "jazz standard / medium swing"),
        (140, 180, "uptempo jazz"),
        (180, 999, "bebop / very fast - c'est un tempo extreme meme pour le bebop"),
    ],
    "techno": [
        (100, 128, "techno lente / industrielle"),
        (128, 145, "techno standard"),
        (145, 999, "hard techno / gabber territory"),
    ],
    "house": [
        (110, 120, "slow house / deep"),
        (120, 130, "house standard"),
        (130, 999, "tech house rapide"),
    ],
    "hip-hop": [
        (60,  80, "lo-fi / slow hip-hop"),
        (80,  100, "hip-hop standard"),
        (100, 140, "hip-hop rapide / trap territory"),
        (140, 999, "trap / drill - plus du hip-hop traditionnel"),
    ],
    "drum and bass": [
        (150, 165, "liquid dnb / jump-up"),
        (165, 180, "dnb standard"),
        (180, 999, "neurofunk / dark dnb extremement rapide"),
    ],
}

def detecter_contexte_bpm(genre, bpm, profil):
    bpm_min = profil["bpm_min"]
    bpm_max = profil["bpm_max"]
    genre_key = genre.lower()

    # Cherche le contexte specifique au genre
    for key, contextes in BPM_CONTEXTES.items():
        if key in genre_key:
            for (cmin, cmax, label) in contextes:
                if cmin <= bpm < cmax:
                    if bpm < bpm_min or bpm > bpm_max:
                        return f"BPM {bpm} detecte = {label}. C'est EN DEHORS de la plage {genre} standard ({bpm_min}-{bpm_max} BPM) mais c'est un sous-style reconnu : adapte ton coaching a ce contexte specifique, ne dis pas juste 'hors norme'."
                    else:
                        return f"BPM {bpm} detecte = {label}. C'est dans la norme {genre} ({bpm_min}-{bpm_max} BPM)."

    # Pas de contexte specifique trouve
    if bpm < bpm_min:
        ecart = bpm_min - bpm
        return f"BPM {bpm} detecte est {ecart} BPM en dessous de la plage {genre} ({bpm_min}-{bpm_max} BPM). Mix plus lent qu'attendu pour ce genre."
    elif bpm > bpm_max:
        ecart = bpm - bpm_max
        return f"BPM {bpm} detecte est {ecart} BPM au-dessus de la plage {genre} ({bpm_min}-{bpm_max} BPM). Mix plus rapide qu'attendu pour ce genre."
    else:
        return f"BPM {bpm} detecte est dans la norme {genre} ({bpm_min}-{bpm_max} BPM)."

def detecter_contexte_score(score_global, scores):
    if score_global >= 80:
        return "NIVEAU AVANCE", "Ce mix est deja de tres bonne qualite. Concentre-toi sur la finition et les details qui feront la difference au niveau professionnel. Sois precis et exigeant sur les details."
    elif score_global >= 60:
        return "NIVEAU INTERMEDIAIRE", "Ce mix a de bonnes bases avec quelques points a ameliorer. Donne des conseils concrets et actionnables sur les 4-5 dimensions les moins bonnes."
    elif score_global >= 40:
        return "NIVEAU EN PROGRESSION", "Ce mix montre du potentiel mais a besoin de travail sur plusieurs dimensions. Concentre-toi sur les 3 points les plus impactants et sois tres encourageant. Ne submerge pas avec trop de corrections."
    else:
        return "DEBUTANT / PREMIER MIX", "Ce producteur debute ou est en phase d'apprentissage. CONCENTRE-TOI UNIQUEMENT sur les 2 points les plus importants. Sois TRES encourageant, valorise chaque point positif. L'objectif est de motiver, pas d'accabler."

def calculer_scores(donnees, genre):
    freq = donnees["frequentiel"]
    dyn  = donnees["dynamique"]
    ster = donnees["stereo"]
    ryt  = donnees["rythme"]
    esp  = donnees["espace"]
    profil    = PROFILS_GENRE.get(genre.lower(), PROFILS_GENRE["default"])
    measured  = [freq["sub_basses_pct"], freq["basses_pct"], freq["mids_pct"],
                 freq["hauts_mids_pct"], freq["aigus_pct"]]
    targets   = [profil["sub"], profil["basses"], profil["mids"],
                 profil["hauts_mids"], profil["aigus"]]
    # Écart absolu total entre les 5 bandes mesurées et les cibles genre
    # 0% d'écart total → 100 points ; chaque point d'écart enlève 1.5 pts
    total_dev = sum(abs(m - t) for m, t in zip(measured, targets))
    score_freq = max(0, min(100, int(100 - total_dev * 1.5)))
    est_club  = any(g in genre.lower() for g in GENRES_CLUB)
    target = -9 if est_club else -14
    score_dyn = max(0, min(100, int(100 - abs(dyn["lufs_approx"] - target) * 5)))
    # Score stéréo : basé sur les corrélations par bande
    # Récompense le "textbook stereo" : sub mono, basses étroites, mids larges, hauts très larges
    # Évite que les sub mono tirent injustement le score global vers le bas
    cs = ster.get("corr_sub",   1.0)  # sub : doit être MONO (corr > 0.85 = bien)
    cb = ster.get("corr_bass",  1.0)  # basses : doivent être étroites (corr > 0.6 = bien)
    cm_s = ster.get("corr_mids",  0.5)  # mids : modérément larges (corr 0.2-0.6 = bien)
    ch = ster.get("corr_highs", 0.3)  # hauts : larges (corr < 0.6 = bien)

    # Points par bande (chaque bande vaut 25 pts)
    # Sub : 25 pts si mono (corr > 0.85), proportionnel sinon — très large sub est pénalisé
    pt_sub  = 25 if cs >= 0.85 else (15 if cs >= 0.65 else 5)
    # Basses : 25 pts si étroites (corr > 0.60)
    pt_bass = 25 if cb >= 0.60 else (15 if cb >= 0.40 else 5)
    # Mids : 25 pts si dans la plage idéale (0.20-0.70)
    pt_mids = 25 if 0.20 <= cm_s <= 0.70 else (12 if 0.0 <= cm_s <= 0.85 else 5)
    # Hauts : 25 pts si larges (corr < 0.65) — très large = bon
    pt_high = 25 if ch < 0.50 else (18 if ch < 0.65 else (10 if ch < 0.80 else 5))

    score_stereo = min(100, pt_sub + pt_bass + pt_mids + pt_high)
    score_rythme = min(100, max(0, int((ryt["regularite_beat"] + 1) / 2 * 100)))
    # Score espace : combinaison reverb_score (qualité d'espace) + densité normalisée
    # densite_mix tourne entre 0.15 et 0.35 sur un vrai mix → on normalise dans [0,1]
    densite_norm = max(0.0, min(1.0, (esp["densite_mix"] - 0.10) / 0.30))
    reverb_norm  = esp["reverb_score"]  # déjà entre 0 et 1
    score_espace = min(100, int((reverb_norm * 0.6 + densite_norm * 0.4) * 100))
    score_global = int((score_freq + score_dyn + score_stereo + score_rythme + score_espace) / 5)
    return {
        "global": score_global,
        "frequentiel": score_freq,
        "dynamique": score_dyn,
        "stereo": score_stereo,
        "rythme": score_rythme,
        "espace": score_espace
    }

GENRES_SIDECHAIN = {
    # Genres où le sidechain est attendu/habituel
    "attendu": {
        "techno", "techno peak time", "techno raw deep hypnotic", "melodic techno",
        "melodic house techno", "hard techno", "industrial techno", "minimal techno",
        "acid techno", "house", "deep house", "tech house", "bass house", "jackin house",
        "funky house", "progressive house", "melodic house", "afro house",
        "drum and bass", "dnb", "liquid dnb", "neurofunk", "jump up dnb", "halftime",
        "dubstep", "deep dubstep", "trance", "trance main floor", "uplifting trance",
        "progressive trance", "hard trance", "tech trance", "trap", "future bass",
        "mainstage", "hardstyle", "hard dance", "neo rave", "bass club",
        "indie dance", "nu disco", "electronica", "electro",
    },
    # Genres où le sidechain est rare/non pertinent
    "rare": {
        "ambient", "jazz", "soul", "funk", "rock", "reggae", "lo-fi hip-hop",
        "downtempo", "synthwave", "afrobeats", "rnb",
    }
}

def _build_punch_lines(pk, genre):
    if not pk or pk.get("nb_kicks", 0) == 0:
        return ["Punch kick : non mesurable (peu de kicks détectés ou bande silencieuse)"]

    lines = []
    punch  = pk.get("punch_db", 0)
    score  = pk.get("punch_score", 0)
    verdict= pk.get("verdict", "?")
    att    = pk.get("attaque_ms", 0)
    sust   = pk.get("sustain_ratio", 0)
    nb     = pk.get("nb_kicks", 0)

    lines.append(f"Punch kick : {punch} dB ({verdict})")
    lines.append(f"  Score punch : {score}/100 | Vitesse attaque : {att}ms | Sustain ratio : {sust}")
    lines.append(f"  {nb} kicks analysés dans la bande 80-120Hz")

    if punch >= 12:
        lines.append("  → Kick très impactant — mentionne-le comme point fort")
    elif punch >= 8:
        lines.append("  → Bon punch — kick solide, quelques pistes d'optimisation possibles")
    elif punch >= 5:
        lines.append("  → Punch moyen — kick présent mais manque d'impact physique")
        lines.append("  → Conseils : vérifier la compression (sidechain trop fort ?), l'EQ 80-120Hz, le transient shaper")
    else:
        lines.append("  → Kick peu défini — priorité à traiter")
        lines.append("  → Conseils : boost 80-100Hz, transient shaper (attack rapide < 5ms), vérifier la saturation du kick")

    if att > 30:
        lines.append(f"  → Attaque lente ({att}ms) — le kick manque de définition initiale (limiter trop agressif ?)")
    elif att < 10:
        lines.append(f"  → Attaque très rapide ({att}ms) — kick très défini et percutant")

    if sust > 0.60:
        lines.append(f"  → Sustain élevé ({sust}) — kick qui traîne, peut boucher le mix")
    elif sust < 0.15:
        lines.append(f"  → Sustain très court ({sust}) — kick très sec, peut manquer de corps")

    return lines


def _build_sections_lines(sec):
    if not sec or sec.get("nb_sections", 0) == 0:
        return ["Structure : analyse impossible (morceau trop court ou BPM non détecté)"]

    lines = []
    sections   = sec.get("sections", [])
    nb_drops   = sec.get("nb_drops", 0)
    nb_breaks  = sec.get("nb_breakdowns", 0)
    nb_builds  = sec.get("nb_builds", 0)
    obs        = sec.get("observations", [])

    lines.append(f"Structure détectée : {sec.get('nb_sections')} sections — "
                 f"{nb_drops} drop(s) | {nb_breaks} breakdown(s) | {nb_builds} build(s)")

    # Timeline des sections
    for s in sections:
        def ts(sec_secs):
            m = int(sec_secs // 60)
            s2 = int(sec_secs % 60)
            return f"{m}:{s2:02d}"
        lines.append(f"  [{ts(s['t_start'])} → {ts(s['t_end'])}] {s['type'].upper()} "
                     f"({s['duree']}s) — énergie {s['energie']:.2f}")

    # Observations
    if obs:
        lines.append("")
        for o in obs:
            lines.append(f"  ⚡ {o}")

    # Conseils selon la structure
    lines.append("")
    if nb_drops == 0:
        lines.append("  → Aucun drop clair détecté — structure plate, peut manquer d'impact")
    elif nb_drops >= 3:
        lines.append(f"  → {nb_drops} drops — structure très fragmentée, vérifier la progression")

    if nb_breaks == 0 and nb_drops > 0:
        lines.append("  → Pas de breakdown — le drop arrive sans préparation ni contraste")
    elif nb_breaks > 0 and nb_drops > 0:
        lines.append("  → Structure drop/breakdown présente — mentionne le contraste dans le rapport")

    lines.append("  → Compare les niveaux d'énergie entre les sections dans tes conseils")
    lines.append("  → Si le drop n'est pas plus fort que le build, c'est un point d'amélioration clé")

    return lines


def _build_tonalite_lines(ton, donnees):
    """
    Construit les lignes du prompt pour la tonalité.
    Adapte la formulation selon le niveau de confiance.
    """
    if not ton or ton.get("confiance", 0) == 0:
        return ["Tonalité : analyse impossible (signal insuffisant ou trop peu harmonique)"]

    lines   = []
    cle     = ton.get("cle", "?")
    camelot = ton.get("camelot", "?")
    conf    = ton.get("confiance", 0)
    fond_hz = ton.get("fondamentale_hz", 0)
    top3    = ton.get("top3", [])
    corr    = ton.get("correlation", 0)

    if conf >= 0.65:
        lines.append(f"Tonalité détectée : {cle} (Camelot : {camelot}) — confiance {round(conf*100)}%")
        lines.append(f"  Fondamentale de référence : {fond_hz} Hz")
        if top3:
            lines.append(f"  Alternatives proches : {top3[0]}")

        # Cohérence kick / tonalité
        freq = donnees.get("frequentiel", {})
        centroide = freq.get("centroide_hz", 0)
        if fond_hz > 0 and centroide > 0:
            # Vérifier si les basses graves sont proches de la fondamentale ou de ses harmoniques
            harmoniques = [fond_hz * m for m in [0.5, 1, 1.5, 2, 2.5, 3]]
            tolerance = fond_hz * 0.12  # 12% de tolérance
            coherence_kick = any(abs(centroide - h) < tolerance * 3 for h in harmoniques)
            if coherence_kick:
                lines.append(f"  ✓ Centroïde spectral ({centroide}Hz) cohérent avec la tonalité {cle}")
            else:
                lines.append(f"  ⚠ Centroïde spectral ({centroide}Hz) potentiellement dissonant avec {fond_hz}Hz ({cle})")

        lines.append(f"  → Mentionne la tonalité dans le rapport avec sa notation Camelot pour les DJs")
        lines.append(f"  → Si le kick/basse sonnent 'flottant', c'est peut-être lié à l'accord avec la tonalité")

    elif conf >= 0.45:
        lines.append(f"Tonalité probable : {cle} (Camelot : {camelot}) — confiance modérée {round(conf*100)}%")
        lines.append(f"  → Mentionne-la avec prudence : 'le mix semble pencher vers {cle}'")
        lines.append(f"  → Ne pas affirmer catégoriquement — la détection est incertaine sur ce type de mix")
        if top3:
            lines.append(f"  → Alternatives possibles : {top3[0]}")
    else:
        lines.append(f"Tonalité : confiance trop faible ({round(conf*100)}%) — mix atonal, très bruité ou purement rythmique")
        lines.append(f"  → NE PAS mentionner la tonalité dans le rapport — risque d'information incorrecte")
        lines.append(f"  → Normal pour : techno industrielle, noise, ambient atonal, percussion pure")

    return lines


def _build_sidechain_lines(sc, genre):
    """
    Construit les lignes du prompt pour le sidechain.
    Formulation prudente selon la certitude et la pertinence du genre.
    """
    if not sc or sc.get("certitude") == "impossible":
        return ["Sidechain : analyse impossible (signal trop court ou silencieux)"]

    genre_lower = genre.lower()
    genre_attendu = genre_lower in GENRES_SIDECHAIN["attendu"]
    genre_rare    = genre_lower in GENRES_SIDECHAIN["rare"]

    lines = []
    detected  = sc.get("detected", False)
    certitude = sc.get("certitude", "faible")
    prof_db   = sc.get("profondeur_db", 0)
    interp    = sc.get("profondeur_interp", "indéterminé")
    regul     = sc.get("regularite", 0)
    release   = sc.get("release_ms", 0)
    score     = sc.get("score_certitude", 0)
    raison    = sc.get("raison", "")

    if detected:
        lines.append(f"Sidechain DÉTECTÉ (certitude: {certitude}, score: {score})")
        lines.append(f"  Profondeur: {abs(prof_db):.1f} dB ({interp})")
        lines.append(f"  Régularité rythmique: {regul:.2f}/1.0")
        lines.append(f"  Release estimée: {release:.0f} ms")
        if certitude == "forte":
            lines.append(f"  → Sidechain clairement présent et musical.")
            if genre_attendu:
                if abs(prof_db) < 3:
                    lines.append(f"  → Pour du {genre}, un sidechain plus prononcé (4-8dB) est souvent attendu.")
                elif abs(prof_db) > 12:
                    lines.append(f"  → Pumping très agressif pour du {genre} — peut-être trop extrême.")
        elif certitude == "moderee":
            lines.append(f"  → Sidechain probable mais subtil — mention avec nuance dans le rapport.")
        else:  # faible
            lines.append(f"  → Signal de sidechain détecté mais incertain — ne pas affirmer catégoriquement.")
    else:
        if genre_attendu:
            if certitude == "faible":
                lines.append(f"Sidechain : NON DÉTECTÉ (certitude faible — {raison})")
                lines.append(f"  → En {genre}, l'absence de sidechain est notable. Mentionne-le avec prudence.")
                lines.append(f"  → NE PAS affirmer catégoriquement l'absence — l'analyse peut être imprécise.")
            else:
                lines.append(f"Sidechain : Absent ou très subtil ({raison})")
                lines.append(f"  → En {genre}, le sidechain est généralement attendu. C'est un point à mentionner.")
        elif genre_rare:
            lines.append(f"Sidechain : Non applicable pour le genre {genre}.")
            lines.append(f"  → Ne pas mentionner le sidechain dans le rapport pour ce genre.")
        else:
            lines.append(f"Sidechain : Non détecté ({raison})")
            lines.append(f"  → Selon le genre et l'intention artistique, c'est normal ou non.")

    # Règle de prudence globale
    if certitude in ("faible", "non detecte"):
        lines.append("  ⚠ PRUDENCE : certitude faible — formule avec des conditionnels dans le rapport.")

    return lines


def calculer_plateformes(donnees):
    dyn  = donnees["dynamique"]
    lufs = dyn.get("lufs_integrated", dyn.get("lufs_approx", -11))
    tp   = dyn.get("true_peak_db", dyn.get("peak_db", -0.5))

    result = {}

    # ── SPOTIFY / APPLE / YOUTUBE ── target -14 LUFS, TP ≤ -1.0 dBTP
    if tp > -1.0:
        result["spotify"] = {
            "status": "red",
            "label": "Spotify / Apple",
            "verdict": f"True Peak {tp} dBTP depasse -1.0",
            "detail": f"Le streaming va limiter et degrader le son. Baisser de {round(tp + 1.0, 1)} dB.",
        }
    elif lufs > -8:
        result["spotify"] = {
            "status": "red",
            "label": "Spotify / Apple",
            "verdict": f"LUFS {lufs} — sur-compresse",
            "detail": "Le mix va perdre en qualite apres normalisation Spotify.",
        }
    elif lufs > -14:
        result["spotify"] = {
            "status": "orange",
            "label": "Spotify / Apple",
            "verdict": f"LUFS {lufs} — sera normalise a -14",
            "detail": f"Spotify va baisser de {round(lufs + 14, 1)} dB. Verifier que ca reste bien.",
        }
    else:
        result["spotify"] = {
            "status": "green",
            "label": "Spotify / Apple",
            "verdict": f"LUFS {lufs} — pret",
            "detail": "Aucune normalisation. Livraison optimale pour le streaming.",
        }

    # ── BEATPORT / CLUBS ── target -9 LUFS, TP ≤ -0.3 dBTP
    if tp > -0.3:
        result["beatport"] = {
            "status": "red",
            "label": "Beatport / Clubs",
            "verdict": f"True Peak {tp} dBTP — risque clip",
            "detail": f"Saturation possible sur les systemes club. Limiter a -0.3 dBTP.",
        }
    elif lufs < -13:
        result["beatport"] = {
            "status": "orange",
            "label": "Beatport / Clubs",
            "verdict": f"LUFS {lufs} — trop doux pour le club",
            "detail": f"Monter de {round(abs(lufs + 9), 1)} dB pour un rendu optimal en club.",
        }
    elif lufs > -6:
        result["beatport"] = {
            "status": "orange",
            "label": "Beatport / Clubs",
            "verdict": f"LUFS {lufs} — sur-compresse",
            "detail": "Le kick va perdre son punch. Relacher la compression master.",
        }
    else:
        result["beatport"] = {
            "status": "green",
            "label": "Beatport / Clubs",
            "verdict": f"LUFS {lufs} — pret",
            "detail": "Niveau optimal pour les systemes club et Beatport.",
        }

    # ── SOUNDCLOUD ── target -8 LUFS, plus tolerant
    if tp > -0.1:
        result["soundcloud"] = {
            "status": "red",
            "label": "SoundCloud",
            "verdict": f"True Peak {tp} dBTP — saturera",
            "detail": "SoundCloud encode en MP3 128k — le True Peak va saturer. Baisser.",
        }
    elif lufs > -6:
        result["soundcloud"] = {
            "status": "orange",
            "label": "SoundCloud",
            "verdict": f"LUFS {lufs} — tres compresse",
            "detail": "L'encodage MP3 SoundCloud va accentuer les artefacts de compression.",
        }
    else:
        result["soundcloud"] = {
            "status": "green",
            "label": "SoundCloud",
            "verdict": f"LUFS {lufs} — pret",
            "detail": "Bon pour l'upload SoundCloud. L'encodage MP3 sera propre.",
        }

    return result

def get_color(score):
    if score >= 75:
        return "#00FF88"
    if score >= 50:
        return "#00E5FF"
    return "#7B2FFF"

def build_score_card(dim, label, scores, donnees=None, featured=False):
    v = scores[dim]
    c = get_color(v)
    cls = "sc feat" if featured else "sc"
    if featured:
        val_style = "background:linear-gradient(135deg,#7B2FFF,#00E5FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent"
        bar_bg = "background:linear-gradient(90deg,#7B2FFF,#00E5FF)"
    else:
        val_style = "color:" + c
        bar_bg = "background:" + c

    # Construire le tooltip avec les données chiffrées
    tip = ""
    if donnees:
        dyn  = donnees.get("dynamique", {})
        freq = donnees.get("frequentiel", {})
        ster = donnees.get("stereo", {})
        ryt  = donnees.get("rythme", {})
        esp  = donnees.get("espace", {})
        pk   = donnees.get("punch_kick", {})
        if dim == "global":
            tip = "Score moyen de toutes les dimensions"
        elif dim == "frequentiel":
            tip = ("Sub: "+str(freq.get("sub_basses_pct","?"))+"%  "
                   "Basses: "+str(freq.get("basses_pct","?"))+"%  "
                   "Mids: "+str(freq.get("mids_pct","?"))+"%  "
                   "HM: "+str(freq.get("hauts_mids_pct","?"))+"%  "
                   "Aigus: "+str(freq.get("aigus_pct","?"))+"%")
        elif dim == "dynamique":
            tip = ("LUFS: "+str(dyn.get("lufs_integrated","?"))+" dB  "
                   "True Peak: "+str(dyn.get("true_peak_db","?"))+" dBTP  "
                   "Crest: "+str(dyn.get("crest_factor_db","?"))+" dB  "
                   "Dyn Range: "+str(dyn.get("dynamic_range_db","?"))+" dB")
        elif dim == "stereo":
            tip = ("Corr Sub: "+str(ster.get("corr_sub","?"))+"  "
                   "Corr Bass: "+str(ster.get("corr_bass","?"))+"  "
                   "Corr Mids: "+str(ster.get("corr_mids","?"))+"  "
                   "Corr Highs: "+str(ster.get("corr_highs","?"))+"  "
                   "Largeur: "+str(ster.get("largeur_stereo","?")))
        elif dim == "rythme":
            tip = ("BPM: "+str(ryt.get("bpm","?"))+"  "
                   "Regularite: "+str(ryt.get("regularite_beat","?"))+"  "
                   "Punch kick: "+str(pk.get("punch_db","?"))+" dB ("+str(pk.get("verdict","?"))+")")
        elif dim == "espace":
            tip = ("Reverb: "+str(esp.get("reverb_score","?"))+"  "
                   "Densite: "+str(esp.get("densite_mix","?"))+"  "
                   "Release kick: "+str(pk.get("release_ms","?"))+" ms")

    tooltip_attr = ' data-iym-tip="' + tip.replace('"', '&quot;') + '"' if tip else ''

    parts = []
    parts.append('<div class="' + cls + '"' + tooltip_attr + '>')
    parts.append('<div class="sclabel">' + label + '</div>')
    parts.append('<div class="scval" style="' + val_style + '" data-score="' + str(v) + '">0%</div>')
    parts.append('<div class="sbbg"><div class="sbf" data-width="' + str(v) + '" style="width:0%;' + bar_bg + ';transition:width 1.2s cubic-bezier(0.4,0,0.2,1)"></div></div>')
    if tip:
        parts.append('<div style="font-size:9px;color:#8888AA;margin-top:5px;font-family:DM Sans,sans-serif;letter-spacing:0.5px">&#8505; Passe la souris pour les details</div>')
    parts.append('</div>')
    return "".join(parts)

def build_platform_badges(plateformes):
    STATUS_COLORS = {
        "green":  {"bg": "rgba(0,255,136,.08)",  "border": "rgba(0,255,136,.3)",  "dot": "#00FF88", "icon": "✓"},
        "orange": {"bg": "rgba(255,180,0,.08)",   "border": "rgba(255,180,0,.35)", "dot": "#FFB400", "icon": "⚠"},
        "red":    {"bg": "rgba(255,60,60,.08)",   "border": "rgba(255,60,60,.35)", "dot": "#FF3C3C", "icon": "✕"},
    }
    PLATFORM_ICONS = {
        "spotify":    "Spotify",
        "beatport":   "Beatport",
        "soundcloud": "SoundCloud",
    }
    html = '<div class="plat-grid">'
    for key, data in plateformes.items():
        s  = STATUS_COLORS[data["status"]]
        html += (
            f'<div class="plat-card" style="background:{s["bg"]};border:1px solid {s["border"]}">'
            f'<div class="plat-header">'
            f'<span class="plat-dot" style="background:{s["dot"]}">{s["icon"]}</span>'
            f'<span class="plat-name">{data["label"]}</span>'
            f'</div>'
            f'<div class="plat-verdict">{data["verdict"]}</div>'
            f'<div class="plat-detail">{data["detail"]}</div>'
            f'</div>'
        )
    html += '</div>'
    return html



import math

def build_radar_chart(scores):
    dims = [
        ("Global",      scores["global"]),
        ("Freq",        scores["frequentiel"]),
        ("Dynamique",   scores["dynamique"]),
        ("Stereo",      scores["stereo"]),
        ("Rythme",      scores["rythme"]),
        ("Espace",      scores["espace"]),
    ]
    n  = len(dims)
    cx, cy, r = 210, 175, 100
    LABEL_R = 130
    angles = [math.pi/2 + i * 2*math.pi/n for i in range(n)]
    # ViewBox 410×350 — marges asymétriques pour les labels gauche
    parts = ['<svg viewBox="0 0 410 350" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:290px;height:auto">']
    parts.append('<defs><linearGradient id="rg" x1="0%" y1="0%" x2="100%" y2="100%">')
    parts.append('<stop offset="0%" style="stop-color:#7B2FFF;stop-opacity:0.7"/>')
    parts.append('<stop offset="100%" style="stop-color:#00E5FF;stop-opacity:0.5"/>')
    parts.append('</linearGradient><filter id="glow"><feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>')
    # Grilles
    for pct in [0.25, 0.5, 0.75, 1.0]:
        pts = [str(round(cx+r*pct*math.cos(a),1))+","+str(round(cy-r*pct*math.sin(a),1)) for a in angles]
        col = "rgba(255,255,255,0.06)" if pct < 1 else "rgba(255,255,255,0.16)"
        sw  = "1.5" if pct == 1 else "0.6"
        parts.append('<polygon points="'+' '.join(pts)+'" fill="none" stroke="'+col+'" stroke-width="'+sw+'"/>')
    # Axes
    for a in angles:
        parts.append('<line x1="'+str(cx)+'" y1="'+str(cy)+'" x2="'+str(round(cx+r*math.cos(a),1))+'" y2="'+str(round(cy-r*math.sin(a),1))+'" stroke="rgba(255,255,255,0.09)" stroke-width="1"/>')
    # Polygone
    pts_s = [str(round(cx+r*(v/100)*math.cos(a),1))+","+str(round(cy-r*(v/100)*math.sin(a),1)) for (_,v),a in zip(dims,angles)]
    parts.append('<polygon points="'+' '.join(pts_s)+'" fill="url(#rg)" stroke="#7B2FFF" stroke-width="2" stroke-linejoin="round" filter="url(#glow)"/>')
    # Points + labels
    for (lbl,val),a in zip(dims,angles):
        nv = val/100
        px = round(cx+r*nv*math.cos(a),1); py = round(cy-r*nv*math.sin(a),1)
        lx = round(cx+LABEL_R*math.cos(a),1); ly = round(cy-LABEL_R*math.sin(a),1)
        c  = "#00FF88" if val>=80 else ("#FFB400" if val>=60 else "#FF6B6B")
        cos_a = math.cos(a); sin_a = math.sin(a)
        # Alignement strict
        if cos_a > 0.3:   anch = "start"
        elif cos_a < -0.3: anch = "end"
        else:              anch = "middle"
        # Décalage vertical selon si on est en haut ou en bas
        if sin_a > 0.3:   dy1, dy2 = -14, 2
        elif sin_a < -0.3: dy1, dy2 = 13, 27
        else:              dy1, dy2 = -6, 10
        parts.append('<circle cx="'+str(px)+'" cy="'+str(py)+'" r="5" fill="'+c+'" stroke="#07070F" stroke-width="1.5"/>')
        parts.append('<text x="'+str(lx)+'" y="'+str(ly)+'" dy="'+str(dy1)+'" text-anchor="'+anch+'" fill="rgba(240,240,248,0.9)" font-size="13" font-family="Syne,sans-serif" font-weight="700">'+lbl+'</text>')
        parts.append('<text x="'+str(lx)+'" y="'+str(ly)+'" dy="'+str(dy2)+'" text-anchor="'+anch+'" fill="'+c+'" font-size="15" font-family="Syne,sans-serif" font-weight="900">'+str(val)+'%</text>')
    parts.append('</svg>')
    return ''.join(parts)


def build_freq_chart(freq, profil, genre):
    bandes = [
        ("Sub",    freq["sub_basses_pct"],  profil.get("sub",   28), "20-80Hz",    "Graves profonds — kick et sub bass"),
        ("Basses", freq["basses_pct"],      profil.get("basses",25), "80-250Hz",   "Corps basses — kick body et bassline"),
        ("Mids",   freq["mids_pct"],        profil.get("mids",  24), "250Hz-2kHz", "Mids — chaleur et corps du mix"),
        ("H-Mids", freq["hauts_mids_pct"], profil.get("hauts_mids",13), "2-6kHz",  "Hauts-mids — presence et mordant"),
        ("Aigus",  freq["aigus_pct"],       profil.get("aigus", 10), "6kHz+",      "Aigus — air et brillance"),
    ]
    max_val = max(max(v,t) for _,v,t,_,_ in bandes) + 10
    # Barres plus larges, plus d espace — suppression des Hz labels qui se chevauchent
    bar_w=64; gap=18; total_w=len(bandes)*(bar_w+gap)+gap; h=200
    parts = ['<svg viewBox="0 0 '+str(total_w)+' '+str(h)+'" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">']

    for i,(label,val,cible,plage,desc) in enumerate(bandes):
        x    = gap + i*(bar_w+gap)
        cx_b = x + bar_w//2
        # Barre cible (fond)
        ht_c = max(3, int((cible/max_val)*(h-58)))
        y_c  = h-44-ht_c
        parts.append('<rect x="'+str(x)+'" y="'+str(y_c)+'" width="'+str(bar_w)+'" height="'+str(ht_c)+'" fill="rgba(255,255,255,0.1)" rx="5" stroke="rgba(255,255,255,0.16)" stroke-width="1"/>')
        # % cible centré dans la barre fond si assez haute
        if ht_c > 16:
            parts.append('<text x="'+str(cx_b)+'" y="'+str(y_c+ht_c//2+4)+'" text-anchor="middle" fill="rgba(255,255,255,0.4)" font-size="10" font-family="Syne,sans-serif">'+str(cible)+'%</text>')
        # Barre mix
        ht_v = max(3, int((val/max_val)*(h-58)))
        y_v  = h-44-ht_v
        diff = val-cible
        col  = "#FF6B6B" if diff>5 else ("#FFB400" if diff<-5 else "#00FF88")
        # Tooltip complet
        diff_s = ("+" if diff>=0 else "")+str(round(diff,1))
        tip = label+" ("+plage+") — Mix: "+str(val)+"% | Cible "+genre+": "+str(cible)+"% | Ecart: "+diff_s+"% — "+desc
        parts.append('<rect class="freq-bar" data-tip="'+tip+'" x="'+str(x+8)+'" y="'+str(y_v)+'" width="'+str(bar_w-16)+'" height="'+str(ht_v)+'" fill="'+col+'" rx="5" opacity="0.9" style="cursor:pointer"/>')
        # Ligne de comparaison haut de cible
        parts.append('<line x1="'+str(x+1)+'" y1="'+str(y_c)+'" x2="'+str(x+bar_w-1)+'" y2="'+str(y_c)+'" stroke="rgba(255,255,255,0.3)" stroke-width="1" stroke-dasharray="3,2"/>')
        # Label bande (grand, lisible)
        parts.append('<text x="'+str(cx_b)+'" y="'+str(h-26)+'" text-anchor="middle" fill="rgba(240,240,248,0.85)" font-size="13" font-family="Syne,sans-serif" font-weight="700">'+label+'</text>')
        # Valeur mesurée (grand, coloré)
        parts.append('<text x="'+str(cx_b)+'" y="'+str(h-9)+'" text-anchor="middle" fill="'+col+'" font-size="15" font-family="Syne,sans-serif" font-weight="900">'+str(val)+'%</text>')
        # Ecart si notable (en petit, discret, au-dessus barre mix)
        if abs(diff) > 3:
            ecart_col = "#FF6B6B" if diff>5 else ("#FFB400" if diff>0 else "#00E5FF")
            parts.append('<text x="'+str(cx_b)+'" y="'+str(y_v-4)+'" text-anchor="middle" fill="'+ecart_col+'" font-size="9" font-family="DM Sans,sans-serif" font-weight="700">'+("+"+str(round(diff,0)) if diff>0 else str(round(diff,0)))+'%</text>')
    # Légende discrète en bas à droite
    parts.append('<rect x="'+str(total_w-120)+'" y="6" width="10" height="8" fill="rgba(255,255,255,0.1)" rx="2" stroke="rgba(255,255,255,0.2)" stroke-width="0.5"/>')
    parts.append('<text x="'+str(total_w-107)+'" y="14" fill="rgba(240,240,248,0.4)" font-size="9" font-family="DM Sans,sans-serif">Cible genre</text>')
    parts.append('</svg>')
    return ''.join(parts)


def build_freq_chart(freq, profil, genre):
    bandes = [
        ("Sub",    freq["sub_basses_pct"],  profil.get("sub",   14), "20-80Hz",    "Graves profonds. Doit etre mono."),
        ("Basses", freq["basses_pct"],      profil.get("basses",24), "80-250Hz",   "Corps basses. Kick + bassline."),
        ("Mids",   freq["mids_pct"],        profil.get("mids",  32), "250Hz-2kHz", "Chaleur du mix. Zone la plus chargee."),
        ("H-Mids", freq["hauts_mids_pct"], profil.get("hauts_mids",17), "2-6kHz",  "Presence et mordant. Leads et voix."),
        ("Aigus",  freq["aigus_pct"],       profil.get("aigus", 13), "6kHz+",      "Air et brillance. Hi-hats, shimmer."),
    ]
    max_val = max(max(v,t) for _,v,t,_,_ in bandes) + 8
    # Barres plus larges avec espace entre elles
    bar_w=58; gap=16; total_w=len(bandes)*(bar_w+gap)+gap*2; h=210
    parts = ['<svg viewBox="0 0 '+str(total_w)+' '+str(h)+'" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">']
    for i,(label,val,cible,plage,desc) in enumerate(bandes):
        x    = gap + i*(bar_w+gap)
        # Barre cible (fond)
        ht_c = max(2, int((cible/max_val)*(h-65)))
        y_c  = h-50-ht_c
        parts.append('<rect x="'+str(x)+'" y="'+str(y_c)+'" width="'+str(bar_w)+'" height="'+str(ht_c)+'" fill="rgba(255,255,255,0.09)" rx="4"/>')
        # % cible en haut de la barre fond
        parts.append('<text x="'+str(x+bar_w//2)+'" y="'+str(y_c-5)+'" text-anchor="middle" fill="rgba(255,255,255,0.35)" font-size="11" font-family="Syne,sans-serif">'+str(cible)+'%</text>')
        # Barre mix
        ht_v = max(2, int((val/max_val)*(h-65)))
        y_v  = h-50-ht_v
        diff = val-cible
        col  = "#FF6B6B" if diff>4 else ("#FFB400" if diff<-4 else "#00FF88")
        tip  = label+" ("+plage+") — Mix: "+str(val)+"% | Cible: "+str(cible)+"% | Ecart: "+("+" if diff>=0 else "")+str(round(diff,1))+"% — "+desc
        parts.append('<rect class="freq-bar" data-tip="'+tip+'" x="'+str(x+7)+'" y="'+str(y_v)+'" width="'+str(bar_w-14)+'" height="'+str(ht_v)+'" fill="'+col+'" rx="4" opacity="0.9" style="cursor:pointer;transition:opacity .15s" onmouseover="this.style.opacity=1" onmouseout="this.style.opacity=0.9"/>')
        # Signe + ou - de l'écart en mini au-dessus de la barre mix
        diff_str = ("+" if diff>=0 else "")+str(round(diff,1))+"%"
        diff_col = col
        parts.append('<text x="'+str(x+bar_w//2)+'" y="'+str(y_v-5)+'" text-anchor="middle" fill="'+diff_col+'" font-size="10" font-family="Syne,sans-serif" font-weight="700">'+diff_str+'</text>')
        # Label bande
        parts.append('<text x="'+str(x+bar_w//2)+'" y="'+str(h-30)+'" text-anchor="middle" fill="rgba(240,240,248,0.8)" font-size="13" font-family="Syne,sans-serif" font-weight="700">'+label+'</text>')
        # Plage Hz
        parts.append('<text x="'+str(x+bar_w//2)+'" y="'+str(h-16)+'" text-anchor="middle" fill="rgba(136,136,170,0.6)" font-size="9" font-family="DM Sans,sans-serif">'+plage+'</text>')
        # % mix
        parts.append('<text x="'+str(x+bar_w//2)+'" y="'+str(h-3)+'" text-anchor="middle" fill="'+col+'" font-size="15" font-family="Syne,sans-serif" font-weight="900">'+str(val)+'%</text>')
    # Légende
    parts.append('<rect x="6" y="6" width="14" height="9" fill="rgba(255,255,255,0.09)" rx="2"/>')
    parts.append('<text x="23" y="14" fill="rgba(240,240,248,0.45)" font-size="10" font-family="Syne,sans-serif">Cible genre</text>')
    parts.append('<rect x="120" y="6" width="14" height="9" fill="#7B2FFF" rx="2" opacity="0.8"/>')
    parts.append('<text x="137" y="14" fill="rgba(240,240,248,0.45)" font-size="10" font-family="Syne,sans-serif">Ton mix</text>')
    parts.append('</svg>')
    return ''.join(parts)


def build_freq_chart(freq, profil, genre):
    bandes = [
        ("Sub",    freq["sub_basses_pct"],  profil.get("sub",14),             "20-80Hz",    "Graves profonds. Doit etre mono en studio."),
        ("Basses", freq["basses_pct"],      profil.get("basses",22),          "80-250Hz",   "Corps des basses. Kick et basse instrument."),
        ("Mids",   freq["mids_pct"],        profil.get("mids",28),            "250Hz-2kHz", "Chaleur du mix. Zone critique et dense."),
        ("H-Mids", freq["hauts_mids_pct"], profil.get("hauts_mids",16),      "2-6kHz",     "Presence et mordant. Voix, leads, attaque."),
        ("Aigus",  freq["aigus_pct"],       profil.get("aigus",12),           "6kHz+",      "Air et brillance. Hi-hats, cymbales, shimmer."),
    ]
    max_val = max(max(v,t) for _,v,t,_,_ in bandes) + 8
    bar_w=54; gap=14; total_w=len(bandes)*(bar_w+gap)+gap; h=190
    parts = ['<svg viewBox="0 0 '+str(total_w)+' '+str(h)+'" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">']
    for i,(label,val,cible,plage,desc) in enumerate(bandes):
        x    = gap + i*(bar_w+gap)
        ht_c = max(2, int((cible/max_val)*(h-58)))
        y_c  = h-42-ht_c
        parts.append('<rect x="'+str(x)+'" y="'+str(y_c)+'" width="'+str(bar_w)+'" height="'+str(ht_c)+'" fill="rgba(255,255,255,0.09)" rx="4" stroke="rgba(255,255,255,0.14)" stroke-width="1"/>')
        parts.append('<text x="'+str(x+bar_w//2)+'" y="'+str(y_c-4)+'" text-anchor="middle" fill="rgba(255,255,255,0.32)" font-size="10" font-family="Syne,sans-serif">'+str(cible)+'%</text>')
        ht_v = max(2, int((val/max_val)*(h-58)))
        y_v  = h-42-ht_v
        diff = val-cible
        col  = "#FF6B6B" if diff>4 else ("#FFB400" if diff<-4 else "#00FF88")
        tip  = label+" ("+plage+") — Mix: "+str(val)+"% | Cible "+genre+": "+str(cible)+"% | Ecart: "+("+" if diff>=0 else "")+str(round(diff,1))+"% — "+desc
        parts.append('<rect class="freq-bar" data-tip="'+tip+'" x="'+str(x+7)+'" y="'+str(y_v)+'" width="'+str(bar_w-14)+'" height="'+str(ht_v)+'" fill="'+col+'" rx="4" opacity="0.88" style="cursor:pointer"/>')
        parts.append('<text x="'+str(x+bar_w//2)+'" y="'+str(h-22)+'" text-anchor="middle" fill="rgba(240,240,248,0.75)" font-size="12" font-family="Syne,sans-serif" font-weight="700">'+label+'</text>')
        parts.append('<text x="'+str(x+bar_w//2)+'" y="'+str(h-6)+'" text-anchor="middle" fill="'+col+'" font-size="14" font-family="Syne,sans-serif" font-weight="900">'+str(val)+'%</text>')
    parts.append('<rect x="5" y="5" width="14" height="9" fill="rgba(255,255,255,0.09)" rx="2"/>')
    parts.append('<text x="22" y="13" fill="rgba(240,240,248,0.4)" font-size="10" font-family="Syne,sans-serif">Cible genre</text>')
    parts.append('<rect x="118" y="5" width="14" height="9" fill="#7B2FFF" rx="2" opacity="0.8"/>')
    parts.append('<text x="135" y="13" fill="rgba(240,240,248,0.4)" font-size="10" font-family="Syne,sans-serif">Ton mix</text>')
    parts.append('</svg>')
    return ''.join(parts)



def build_sections_timeline(sec_data, duree_totale):
    if not sec_data or not sec_data.get("sections"):
        return ''
    sections = sec_data["sections"]
    if not duree_totale or duree_totale <= 0:
        duree_totale = sections[-1]["t_end"] if sections else 180
    COLORS = {
        "intro":"#8888AA","build":"#00E5FF","drop":"#7B2FFF",
        "breakdown":"#00FF88","transition":"#FFB400","outro":"#8888AA",
    }
    LABELS = {
        "intro":"INTRO","build":"BUILD","drop":"DROP",
        "breakdown":"BREAK","transition":"TRANS","outro":"OUTRO",
    }
    def ts(sc): return str(int(sc//60))+':'+str(int(sc%60)).zfill(2)
    html = '<div style="margin:16px 0 8px">'
    html += '<div style="font-size:10px;letter-spacing:2px;text-transform:uppercase;color:#8888AA;margin-bottom:6px;font-family:Syne,sans-serif">STRUCTURE DU MORCEAU</div>'
    html += '<div style="position:relative;height:30px;background:rgba(255,255,255,0.04);border-radius:6px;overflow:hidden">'
    for s in sections:
        left  = round(s["t_start"]/duree_totale*100,1)
        width = round(max(0.5,(s["t_end"]-s["t_start"])/duree_totale*100),1)
        col   = COLORS.get(s["type"],"#8888AA")
        label = LABELS.get(s["type"],s["type"].upper())
        tip_html = (label+' — '+ts(s["t_start"])+' → '+ts(s["t_end"])+
                   '<br>Durée : '+str(s["duree"])+'s'+
                   '<br>Énergie relative : '+str(round(s.get("energie",0)*100))+'%')
        tip_safe = tip_html.replace('"', '&quot;')
        html += ('<div class="sec-block" data-tip="'+tip_safe+'" style="position:absolute;left:'+str(left)+'%;width:'+str(width)+'%;'
                 'height:100%;background:'+col+';opacity:0.78;border-right:1px solid rgba(7,7,15,0.4);'
                 'display:flex;align-items:center;justify-content:center;cursor:pointer;transition:opacity .2s;"'
                 ' onmouseover="this.style.opacity=\'1\'" onmouseout="this.style.opacity=\'0.78\'">'
                 '<span style="font-size:7px;font-family:Syne,sans-serif;font-weight:800;'
                 'color:rgba(255,255,255,0.95);white-space:nowrap;padding:0 3px;overflow:hidden">'+label+'</span>'
                 '</div>')
    html += '</div>'
    obs = sec_data.get("observations",[])
    for o in obs[:2]:
        html += '<div style="font-size:11px;color:#8888AA;margin-top:5px">&#9889; '+o+'</div>'
    html += '</div>'
    return html




LIENS_RESSOURCES = {
    "sidechain": {
        "titre": "Sidechain compression",
        "tutos": [
            ("Sidechain dans Ableton (officiel)", "https://www.ableton.com/en/packs/sidechain-compression/"),
            ("How to sidechain in Logic Pro", "https://www.musictech.net/tutorials/logic-pro/logic-pro-sidechain-compression/"),
        ],
        "plugins": [
            ("Kickstart — Nicky Romero (one-knob)", "https://www.nicky-romero.com/kickstart"),
            ("VolumeShaper — Cableguys", "https://www.cableguys.com/volumeshaper.html"),
            ("LFOtool — Xfer Records", "https://xferrecords.com/products/lfotool"),
        ],
    },
    "sub_stereo": {
        "titre": "Mono-iser les sub-basses",
        "tutos": [
            ("Why bass should be mono", "https://www.producelikeapro.com/blog/low-end-mono/"),
            ("Low end mono tutorial", "https://www.izotope.com/en/learn/mid-side-processing.html"),
        ],
        "plugins": [
            ("Ozone Imager — iZotope (gratuit)", "https://www.izotope.com/en/products/imager.html"),
            ("MSED — Voxengo (gratuit)", "https://www.voxengo.com/product/msed/"),
            ("S1 Stereo Imager — Waves", "https://www.waves.com/plugins/s1-stereo-imager"),
        ],
    },
    "clipping": {
        "titre": "Éliminer le clipping",
        "tutos": [
            ("What is audio clipping — iZotope", "https://www.izotope.com/en/learn/what-is-audio-clipping.html"),
            ("How to avoid clipping — Waves", "https://www.waves.com/academy/6-tips-to-avoid-clipping"),
        ],
        "plugins": [
            ("FabFilter Pro-L 2 (limiter reference)", "https://www.fabfilter.com/products/pro-l-2-limiter-plug-in"),
            ("TDR Limitless — Tokyo Dawn (gratuit)", "https://www.tokyodawn.net/tdr-limitless/"),
            ("Youlean Loudness Meter (gratuit)", "https://youlean.co/youlean-loudness-meter/"),
        ],
    },
    "lufs_bas": {
        "titre": "Augmenter le niveau (LUFS trop bas)",
        "tutos": [
            ("Mastering loudness for streaming — iZotope", "https://www.izotope.com/en/learn/mastering-loudness-for-streaming.html"),
            ("What is LUFS — Waves Academy", "https://www.waves.com/academy/what-is-lufs"),
        ],
        "plugins": [
            ("FabFilter Pro-L 2", "https://www.fabfilter.com/products/pro-l-2-limiter-plug-in"),
            ("Youlean Loudness Meter (gratuit)", "https://youlean.co/youlean-loudness-meter/"),
            ("LUFS Meter — Klangfreund (gratuit)", "https://www.klangfreund.com/lufsmeter/"),
        ],
    },
    "sub_bas": {
        "titre": "Renforcer les sub-basses",
        "tutos": [
            ("How to add sub bass to your mix", "https://www.native-instruments.com/en/specials/massive/tutorial-sub-bass/"),
            ("Making bass hit harder", "https://www.producelikeapro.com/blog/bass-frequencies/"),
        ],
        "plugins": [
            ("Waves MaxxBass (renforceur sub)", "https://www.waves.com/plugins/maxxbass"),
            ("SubLab — Future Audio Workshop", "https://futureaudioworkshop.com/sublab/"),
            ("Infected Mushroom Pusher — Waves", "https://www.waves.com/plugins/infected-mushroom-pusher"),
        ],
    },
    "mids_encombres": {
        "titre": "Dégager les médiums",
        "tutos": [
            ("How to EQ midrange — iZotope", "https://www.izotope.com/en/learn/how-to-eq-midrange-frequencies.html"),
            ("Fixing muddy mixes — Sweetwater", "https://www.sweetwater.com/insync/fix-muddy-mix/"),
        ],
        "plugins": [
            ("FabFilter Pro-Q 3 (EQ reference)", "https://www.fabfilter.com/products/pro-q-3-equalizer-plug-in"),
            ("TDR Nova (EQ dynamique, gratuit)", "https://www.tokyodawn.net/tdr-nova/"),
            ("Neutron 4 — iZotope (EQ intelligent)", "https://www.izotope.com/en/products/neutron.html"),
        ],
    },
    "punch_kick": {
        "titre": "Améliorer le punch du kick",
        "tutos": [
            ("How to make your kick drum punch harder", "https://www.waves.com/academy/kick-drum-mixing"),
            ("Transient shaping tutorial", "https://www.sweetwater.com/insync/transient-shaping/"),
        ],
        "plugins": [
            ("Transient Master — NI (gratuit)", "https://www.native-instruments.com/en/products/komplete/effects/transient-master/"),
            ("Kickass — Venomode", "https://venomode.com/kickass"),
            ("Spiff — Oeksound", "https://oeksound.com/plugins/spiff/"),
        ],
    },
    "reverb": {
        "titre": "Travailler la reverbe et espace",
        "tutos": [
            ("Understanding reverb in mixing", "https://www.sweetwater.com/insync/reverb-fundamentals/"),
            ("How to use reverb in electronic music", "https://www.ableton.com/en/packs/convolution-reverb/"),
        ],
        "plugins": [
            ("Valhalla VintageVerb (reference)", "https://valhalladsp.com/shop/reverb/valhalla-vintage-verb/"),
            ("Valhalla Room (version lite gratuite)", "https://valhalladsp.com/shop/reverb/valhalla-room/"),
            ("OrilRiver — Denis Tihanov (gratuit)", "https://www.kvraudio.com/product/orilriver-by-denis-tihanov"),
        ],
    },
}



def build_oscilloscope(donnees, duree_totale):
    bot = donnees.get("balance_over_time", {})
    segs = bot.get("segments", [])
    if not segs or len(segs) < 3:
        return ""
    rms_v = [s["rms_db"] for s in segs]
    times = [s["t"] for s in segs]
    mn, mx = min(rms_v), max(rms_v)
    rng = (mx - mn) if mx != mn else 1
    W, H, px, py = 500, 80, 8, 8
    iw, ih = W-2*px, H-2*py
    pts = []
    dur = duree_totale or times[-1] or 180
    for t, v in zip(times, rms_v):
        x = round(px + (t/dur)*iw, 1)
        y = round(py + ih - ((v-mn)/rng)*ih, 1)
        pts.append((x, y))
    pl = " ".join(str(a)+","+str(b) for a,b in pts)
    ap = pl+" "+str(pts[-1][0])+","+str(H)+" "+str(pts[0][0])+","+str(H)
    events = bot.get("events", [])
    def ts2(s): return str(int(s//60))+":"+str(int(s%60)).zfill(2)
    out = []
    out.append('<div style="margin:20px 0">')
    out.append('<div style="font-size:10px;letter-spacing:2px;text-transform:uppercase;color:#8888AA;margin-bottom:8px;font-family:Syne,sans-serif">DYNAMIQUE — COURBE RMS SUR LE TEMPS</div>')
    out.append('<div style="background:rgba(15,15,26,0.8);border:1px solid rgba(255,255,255,0.07);border-radius:12px;overflow:hidden;padding:4px 0">')
    out.append('<svg viewBox="0 0 500 80" xmlns="http://www.w3.org/2000/svg" style="width:100%;display:block">')
    out.append('<defs><linearGradient id="oG" x1="0" y1="0" x2="0" y2="1">')
    out.append('<stop offset="0%" style="stop-color:#7B2FFF;stop-opacity:0.45"/>')
    out.append('<stop offset="100%" style="stop-color:#7B2FFF;stop-opacity:0.03"/>')
    out.append('</linearGradient></defs>')
    for pct in [0.25, 0.5, 0.75]:
        gy = round(py + ih*(1-pct), 1)
        out.append('<line x1="8" y1="'+str(gy)+'" x2="492" y2="'+str(gy)+'" stroke="rgba(255,255,255,0.05)" stroke-width="0.5" stroke-dasharray="4,4"/>')
    out.append('<polygon points="'+ap+'" fill="url(#oG)"/>')
    out.append('<polyline points="'+pl+'" fill="none" stroke="#7B2FFF" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    for ev in events[:6]:
        t_ev = ev.get("t", 0)
        ex = round(px + (t_ev/dur)*iw, 1)
        et = ev.get("type","")
        ec = "#00E5FF" if "drop" in et else ("#00FF88" if "break" in et else "#FFB400")
        el = "DROP" if "drop" in et else ("BREAK" if "break" in et else "EVT")
        out.append('<line x1="'+str(ex)+'" y1="8" x2="'+str(ex)+'" y2="72" stroke="'+ec+'" stroke-width="1" stroke-dasharray="3,3" opacity="0.7"/>')
        out.append('<text x="'+str(ex+2)+'" y="17" fill="'+ec+'" font-size="7" font-family="Syne,sans-serif" font-weight="800">'+el+'</text>')
    for tpct in [0, 0.5, 1.0]:
        tx = round(px + tpct*iw, 1)
        tl = ts2(dur*tpct)
        anch = "start" if tpct==0 else ("end" if tpct==1 else "middle")
        out.append('<text x="'+str(tx)+'" y="79" text-anchor="'+anch+'" fill="rgba(136,136,170,0.5)" font-size="8" font-family="DM Sans,sans-serif">'+tl+'</text>')
    out.append('</svg></div></div>')
    return "".join(out)



def build_ressources_section(donnees, scores, genre):
    problemes = []
    dyn  = donnees.get("dynamique", {})
    freq = donnees.get("frequentiel", {})
    ster = donnees.get("stereo", {})
    clip = donnees.get("clipping", {})
    sc_d = donnees.get("sidechain", {})
    pk   = donnees.get("punch_kick", {})
    esp  = donnees.get("espace", {})
    lufs  = dyn.get("lufs_integrated", dyn.get("lufs_approx", -20))
    sub_p = freq.get("sub_basses_pct", 0)
    mids_p= freq.get("mids_pct", 0)
    corr_s= ster.get("corr_sub", 1.0)
    punch = pk.get("punch_db", 0)
    rev   = esp.get("reverb_score", 0)
    if clip.get("severite","aucun") in ("modere","severe"):
        problemes.append("clipping")
    if lufs > -5:
        problemes.append("clipping")
    elif lufs < -18:
        problemes.append("lufs_bas")
    if corr_s < 0.70:
        problemes.append("sub_stereo")
    if sub_p < 8:
        problemes.append("sub_bas")
    if mids_p > 38:
        problemes.append("mids_encombres")
    if punch < 5:
        problemes.append("punch_kick")
    if rev < 0.35:
        problemes.append("reverb")
    GENRES_SC = {"techno","house","deep house","tech house","drum and bass","dnb","dubstep","trap","trance","hardstyle"}
    if not sc_d.get("detected", False) and genre.lower() in GENRES_SC:
        problemes.append("sidechain")
    problemes = list(dict.fromkeys(problemes))[:3]
    if not problemes:
        return ""

    html = '<div style="margin:28px 0;background:rgba(15,15,26,0.85);border:1px solid rgba(123,47,255,0.25);border-radius:16px;padding:24px">'
    html += '<div style="font-size:10px;letter-spacing:2px;text-transform:uppercase;color:#7B2FFF;margin-bottom:14px;font-family:Syne,sans-serif">RESSOURCES — TUTOS ET PLUGINS</div>'
    html += '<div style="font-size:12px;color:#8888AA;margin-bottom:18px">Ressources ciblees selon les points detectes dans ton mix :</div>'

    for pb in problemes:
        res = LIENS_RESSOURCES.get(pb)
        if not res:
            continue
        html += '<div style="margin-bottom:22px;padding-bottom:18px;border-bottom:1px solid rgba(255,255,255,0.05)">'
        html += '<div style="font-size:13px;font-weight:700;color:#F0F0F8;font-family:Syne,sans-serif;margin-bottom:10px">&#128279; '+res["titre"]+'</div>'
        html += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">'
        for name, url in res.get("tutos",[])[:2]:
            html += ('<a href="'+url+'" target="_blank" rel="noopener noreferrer" '
                     'style="display:inline-flex;align-items:center;gap:5px;padding:7px 13px;'
                     'background:rgba(0,229,255,0.08);border:1px solid rgba(0,229,255,0.2);'
                     'border-radius:20px;text-decoration:none;font-size:11px;'
                     'color:#00E5FF;font-family:DM Sans,sans-serif">'
                     '&#127916; '+name+'</a>')
        html += '</div>'
        html += '<div style="display:flex;gap:8px;flex-wrap:wrap">'
        for name, url in res.get("plugins",[])[:3]:
            html += ('<a href="'+url+'" target="_blank" rel="noopener noreferrer" '
                     'style="display:inline-flex;align-items:center;gap:5px;padding:7px 13px;'
                     'background:rgba(123,47,255,0.08);border:1px solid rgba(123,47,255,0.2);'
                     'border-radius:20px;text-decoration:none;font-size:11px;'
                     'color:#B088FF;font-family:DM Sans,sans-serif">'
                     '&#127381; '+name+'</a>')
        html += '</div></div>'
    html += '</div>'
    return html


def build_clipping_html(clip, duree_totale):
    """Construit le bloc HTML de détection de clipping."""
    if not clip["has_clipping"]:
        return (
            '<div class="clip-section">'
            '<div class="bottit" style="margin-bottom:12px">Detection de clipping</div>'
            '<div class="clip-ok">Aucun clipping detecte — mix propre ✓</div>'
            '</div>'
        )

    count    = clip["count"]
    severite = clip["severite"]
    pct      = clip["total_pct"]
    events   = clip["events"]

    badge_label = {"leger": "Leger", "modere": "Modere", "severe": "Severe"}.get(severite, severite)

    # Timeline proportionnelle
    timeline_markers = ""
    if duree_totale > 0 and events:
        for e in events:
            left_pct  = round(e["t"] / duree_totale * 100, 2)
            # Largeur proportionnelle à la durée, minimum 2px via min-width CSS
            width_pct = max(0.15, round(e["duration_ms"] / duree_totale / 10, 3))
            cls       = "fort" if e["severity"] == "fort" else "leger"
            title     = f"{e['ts']} — {e['duration_ms']}ms ({e['peak_db']} dBFS)"
            timeline_markers += (
                f'<div class="clip-marker {cls}" '
                f'style="left:{left_pct}%;width:{width_pct}%" '
                f'title="{title}"></div>'
            )

    # Tags timestamps
    tags = ""
    for e in events[:20]:
        cls = "fort" if e["severity"] == "fort" else "leger"
        tags += f'<span class="clip-tag {cls}">{e["ts"]}</span>'

    return (
        '<div class="clip-section">'
        '<div class="clip-header">'
        f'<div class="clip-title bottit">Clipping detecte</div>'
        f'<span class="clip-badge {severite}">{badge_label} · {count} event{"s" if count > 1 else ""} · {pct}% du signal</span>'
        '</div>'
        '<div class="clip-timeline">' + timeline_markers + '</div>'
        '<div class="clip-list">' + tags + ('...' if count > 20 else '') + '</div>'
        f'<div style="margin-top:10px;font-size:12px;color:var(--gr)">Seuil de detection : {clip["seuil_db"]} dBFS — '
        f'les zones rouges depassent -0.1 dBFS (saturation franche), les zones oranges depassent {clip["seuil_db"]} dBFS</div>'
        '</div>'
    )


    v = scores[dim]
    c = get_color(v)
    cls = "sc feat" if featured else "sc"
    if featured:
        val_style = "background:linear-gradient(135deg,#7B2FFF,#00E5FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent"
        bar_bg = "background:linear-gradient(90deg,#7B2FFF,#00E5FF)"
    else:
        val_style = "color:" + c
        bar_bg = "background:" + c
    parts = []
    parts.append('<div class="' + cls + '">')
    parts.append('<div class="sclabel">' + label + '</div>')
    parts.append('<div class="scval" style="' + val_style + '" data-score="' + str(v) + '">0%</div>')
    parts.append('<div class="sbbg"><div class="sbf" data-width="' + str(v) + '" style="width:0%;' + bar_bg + ';transition:width 1.2s cubic-bezier(0.4,0,0.2,1)"></div></div>')
    parts.append('</div>')
    return "".join(parts)

def build_bot_bar(h):
    return '<div class="botbar" style="height:' + str(h) + 'px;background:linear-gradient(to top,#7B2FFF,#00E5FF)"></div>'

def build_bot_event(e):
    is_drop = "DROP" in e["type"]
    cls = "drop" if is_drop else "bd"
    label = "Drop" if is_drop else "Breakdown"
    return '<span class="bev ' + cls + '">' + label + ' ' + str(e["t"]) + 's (' + str(e["delta_db"]) + ' dB)</span>'

CSS_STYLES = """
*{margin:0;padding:0;box-sizing:border-box}
:root{--v:#7B2FFF;--c:#00E5FF;--g:#00FF88;--n:#07070F;--n2:#0F0F1A;--w:#F0F0F8;--gr:#8888AA}
body{background:var(--n);color:var(--w);font-family:'DM Sans',sans-serif;min-height:100vh}
#portalOverlay{position:fixed;inset:0;z-index:99999;pointer-events:none;opacity:0}
#portalOverlay.active{pointer-events:all}
#portalCanvas{position:absolute;inset:0;width:100%;height:100%}
#analyseCanvas{position:fixed;inset:0;z-index:0;opacity:0;pointer-events:none;transition:opacity .6s ease}
#analyseCanvas.active{opacity:0.32}
nav{display:flex;align-items:center;justify-content:space-between;padding:20px 40px;border-bottom:1px solid rgba(255,255,255,0.05);position:relative;z-index:10}
.logo{font-family:'Syne',sans-serif;font-size:22px;font-weight:800;background:linear-gradient(135deg,var(--v),var(--c));-webkit-background-clip:text;-webkit-text-fill-color:transparent;text-decoration:none}
.badge{font-size:11px;background:rgba(123,47,255,0.15);border:1px solid rgba(123,47,255,0.3);color:var(--c);padding:4px 12px;border-radius:100px;letter-spacing:2px}
.hero{text-align:center;padding:60px 20px 40px}
.hero h1{font-family:'Syne',sans-serif;font-size:clamp(36px,6vw,64px);font-weight:800;letter-spacing:-2px;margin-bottom:16px}
.hero h1 span{background:linear-gradient(135deg,var(--v),var(--c));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.hero p{color:var(--gr);font-size:17px;max-width:500px;margin:0 auto}
.waveform{display:flex;align-items:center;justify-content:center;gap:3px;margin:30px auto;height:40px}
.wb{width:3px;background:linear-gradient(to top,var(--v),var(--c));border-radius:10px;animation:wave 1.5s ease-in-out infinite;opacity:.6;transition:background 0.4s}
@keyframes wave{0%,100%{transform:scaleY(.3)}50%{transform:scaleY(1)}}
.wb.fast{animation-duration:.5s!important}
.wb.analyzing{background:linear-gradient(to top,var(--c),var(--g))!important;animation-duration:.25s!important;opacity:1!important}
.main{max-width:860px;margin:0 auto;padding:0 20px 80px}
.modes{display:flex;gap:8px;margin-bottom:24px;background:var(--n2);padding:6px;border-radius:14px;border:1px solid rgba(255,255,255,0.06)}
.mb{flex:1;padding:12px;background:transparent;border:none;color:var(--gr);font-family:'Syne',sans-serif;font-size:13px;font-weight:600;border-radius:10px;cursor:pointer;transition:all .2s}
.mb.active{background:linear-gradient(135deg,rgba(123,47,255,.3),rgba(0,229,255,.1));color:var(--w);border:1px solid rgba(123,47,255,.4)}
.upload-zone{border:2px dashed rgba(123,47,255,.4);border-radius:20px;padding:50px 30px;text-align:center;background:rgba(123,47,255,.04);cursor:pointer;margin-bottom:20px;position:relative;transition:all .25s}
.upload-zone:hover{border-color:var(--v);background:rgba(123,47,255,.08)}
.upload-zone.dragover{border-color:var(--c);border-style:solid;background:rgba(0,229,255,.08);transform:scale(1.015);box-shadow:0 0 40px rgba(0,229,255,.15)}
.upload-zone.dragover h3{color:var(--c)}
.upload-zone.has-file{border-color:var(--g);border-style:solid;background:rgba(0,255,136,.04)}
.upload-zone input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.upload-icon{font-size:40px;margin-bottom:12px;transition:transform .2s}
.upload-zone.dragover .upload-icon{transform:scale(1.2) translateY(-4px)}
.upload-zone h3{font-family:'Syne',sans-serif;font-size:18px;margin-bottom:8px;transition:color .2s}
.upload-zone p{color:var(--gr);font-size:14px}
.formats{display:flex;gap:8px;justify-content:center;margin-top:16px}
.fmt{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);padding:4px 10px;border-radius:6px;font-size:12px;color:var(--gr)}
.fsel{color:var(--g);font-weight:500;margin-top:10px;font-size:14px}
.mp{display:none}.mp.active{display:block}
.slabel{font-family:'Syne',sans-serif;font-size:11px;letter-spacing:3px;text-transform:uppercase;color:var(--v);margin-bottom:10px}
.families{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.fb{padding:6px 14px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:100px;color:var(--gr);font-size:12px;cursor:pointer;transition:all .2s}
.fb.active,.fb:hover{background:rgba(123,47,255,.15);border-color:rgba(123,47,255,.4);color:var(--w)}
.gsel{width:100%;padding:14px 16px;background:var(--n2);border:1px solid rgba(255,255,255,.08);border-radius:12px;color:var(--w);font-size:15px;cursor:pointer}
.ref-zone{background:var(--n2);border:1px solid rgba(255,255,255,.06);border-radius:16px;padding:20px;margin-bottom:20px}
.ref-slot{display:flex;align-items:center;gap:12px;padding:14px;background:rgba(255,255,255,.03);border:1px dashed rgba(255,255,255,.1);border-radius:10px;margin-bottom:10px;cursor:pointer;position:relative;transition:all .2s}
.ref-slot:hover{border-color:rgba(123,47,255,.4);background:rgba(123,47,255,.05)}
.ref-slot input{position:absolute;inset:0;opacity:0;cursor:pointer}
.rnum{width:28px;height:28px;border-radius:50%;background:rgba(123,47,255,.2);border:1px solid rgba(123,47,255,.4);display:flex;align-items:center;justify-content:center;font-size:12px;color:var(--v);flex-shrink:0}
.rinfo{flex:1}.rtitle{font-size:13px;color:var(--w)}.rsub{font-size:11px;color:var(--gr);margin-top:2px}
.btn-go{margin-top:40px;width:100%;padding:18px;background:linear-gradient(135deg,var(--v),#5020CC);border:none;border-radius:14px;color:white;font-family:'Syne',sans-serif;font-size:16px;font-weight:700;cursor:pointer;letter-spacing:1px;transition:all .2s}
.btn-go:hover{transform:translateY(-2px);box-shadow:0 10px 40px rgba(123,47,255,.4)}
.btn-go:disabled{opacity:.5;cursor:not-allowed;transform:none}
.psteps{display:none;text-align:center;padding:50px 20px}
.psteps.active{display:block}
.psteps-title{font-family:'Syne',sans-serif;font-size:18px;font-weight:700;margin-bottom:8px}
.psteps-sub{color:var(--gr);font-size:14px;margin-bottom:30px}
.progress-wrap{margin:16px 0 24px;background:rgba(255,255,255,0.06);border-radius:20px;height:6px;overflow:hidden}
.progress-bar{height:100%;border-radius:20px;background:linear-gradient(90deg,var(--v),var(--c));width:0%;transition:width 0.4s ease}
.progress-pct{font-family:'Syne',sans-serif;font-size:22px;font-weight:800;background:linear-gradient(90deg,var(--v),var(--c));-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:4px}
.progress-label{font-size:12px;color:var(--gr);letter-spacing:2px;text-transform:uppercase;margin-bottom:20px}
.pstep{display:flex;align-items:center;gap:14px;padding:13px 20px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);border-radius:12px;margin:0 auto 8px;max-width:380px;opacity:.35;transition:all .4s}
.pstep.done{opacity:1;border-color:rgba(0,255,136,.3);background:rgba(0,255,136,.04)}
.pstep.active-step{opacity:1;border-color:rgba(123,47,255,.5);background:rgba(123,47,255,.08)}
.pstep-dot{width:28px;height:28px;border-radius:50%;background:rgba(255,255,255,.06);display:flex;align-items:center;justify-content:center;font-size:13px;flex-shrink:0;transition:all .3s}
.pstep.done .pstep-dot{background:rgba(0,255,136,.2)}
.pstep.active-step .pstep-dot{background:rgba(123,47,255,.3);animation:pdot .7s ease-in-out infinite}
@keyframes pdot{0%,100%{transform:scale(1)}50%{transform:scale(1.25)}}
.pstep-label{font-size:13px;font-family:'Syne',sans-serif;font-weight:600;text-align:left}
.loading{display:none;text-align:center;padding:60px 20px}.loading.active{display:block}
.lwave{display:flex;align-items:center;justify-content:center;gap:4px;margin-bottom:20px}
.lb{width:4px;height:30px;background:linear-gradient(to top,var(--v),var(--c));border-radius:10px;animation:wave .8s ease-in-out infinite}
.loading h3{font-family:'Syne',sans-serif;font-size:18px;margin-bottom:8px}
.loading p{color:var(--gr);font-size:14px}
@keyframes slideUp{from{opacity:0;transform:translateY(28px)}to{opacity:1;transform:translateY(0)}}
.result{display:none}.result.active{display:block;animation:slideUp .5s cubic-bezier(.22,1,.36,1) forwards;padding-bottom:320px}
.rheader{display:flex;align-items:center;justify-content:space-between;margin-bottom:30px;flex-wrap:wrap;gap:16px}
.rtit{font-family:'Syne',sans-serif;font-size:24px;font-weight:700}
.rgenre{font-size:11px;letter-spacing:3px;text-transform:uppercase;color:var(--c)}
.sgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:30px}
.sc{background:var(--n2);border:1px solid rgba(255,255,255,.06);border-radius:14px;padding:18px;position:relative;cursor:help;transition:transform .2s,box-shadow .2s}
.sc:hover{transform:translateY(-3px);box-shadow:0 8px 30px rgba(123,47,255,.2);border-color:rgba(123,47,255,.3)}
.sc.feat{background:linear-gradient(135deg,rgba(123,47,255,.15),rgba(0,229,255,.05));border-color:rgba(123,47,255,.3)}
.sc.feat:hover{box-shadow:0 8px 36px rgba(123,47,255,.35)}
/* Tooltip custom */
.iym-tip{position:fixed;z-index:9999;background:rgba(10,10,22,0.97);border:1px solid rgba(123,47,255,.4);border-radius:12px;padding:12px 16px;font-size:12px;color:#F0F0F8;font-family:'DM Sans',sans-serif;line-height:1.6;max-width:300px;pointer-events:none;opacity:0;transition:opacity .15s;box-shadow:0 8px 32px rgba(0,0,0,.6)}
.sclabel{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--gr);margin-bottom:10px}
.scval{font-family:'Syne',sans-serif;font-size:32px;font-weight:800;line-height:1;margin-bottom:10px}
.sbbg{height:4px;background:rgba(255,255,255,.08);border-radius:10px;overflow:hidden}
.sbf{height:100%;border-radius:10px}
.rbox{background:var(--n2);border:1px solid rgba(255,255,255,.06);border-radius:16px;padding:32px;margin-bottom:24px;line-height:1.8;font-size:15px;color:#CCCCDD}
.rbox h2{font-family:'Syne',sans-serif;font-size:16px;color:var(--w);margin:24px 0 12px;padding-bottom:8px;border-bottom:1px solid rgba(255,255,255,.06)}
.rbox h2:first-child{margin-top:0}
.rbox h3{font-family:'Syne',sans-serif;font-size:13px;color:var(--c);margin:16px 0 8px}
.rbox strong{color:var(--w)}.rbox li{margin:6px 0 6px 20px}
.bots{background:var(--n2);border:1px solid rgba(255,255,255,.06);border-radius:16px;padding:24px;margin-bottom:24px}
.bottit{font-family:'Syne',sans-serif;font-size:12px;letter-spacing:3px;text-transform:uppercase;color:var(--v);margin-bottom:16px}
.botbars{display:flex;gap:4px;align-items:flex-end;height:60px}
.botbar{flex:1;border-radius:4px 4px 0 0;min-height:8px}
.bev{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:100px;font-size:11px;margin:4px 4px 0 0}
.bev.drop{background:rgba(0,255,136,.1);border:1px solid rgba(0,255,136,.3);color:var(--g)}
.bev.bd{background:rgba(0,229,255,.1);border:1px solid rgba(0,229,255,.3);color:var(--c)}
.btn-back{display:inline-flex;align-items:center;gap:8px;padding:14px 28px;background:rgba(123,47,255,.15);border:1px solid rgba(123,47,255,.3);border-radius:12px;color:var(--w);font-family:'Syne',sans-serif;font-size:14px;cursor:pointer;margin-top:8px;margin-bottom:40px;text-decoration:none;transition:all .2s}
.info-icon{display:inline-flex;align-items:center;justify-content:center;width:15px;height:15px;border-radius:50%;background:rgba(255,255,255,0.12);color:#8888AA;font-size:9px;font-weight:700;cursor:help;margin-left:5px;position:relative;vertical-align:middle;transition:background .2s;font-family:'DM Sans',sans-serif;line-height:1}
.info-icon:hover{background:rgba(123,47,255,0.4);color:#F0F0F8}
.info-icon::after{content:attr(data-tip);position:absolute;bottom:calc(100% + 10px);left:50%;transform:translateX(-50%);background:rgba(10,10,22,0.98);border:1px solid rgba(123,47,255,0.3);color:#F0F0F8;font-size:12px;font-weight:400;padding:10px 14px;border-radius:10px;white-space:nowrap;max-width:260px;white-space:normal;width:220px;opacity:0;pointer-events:none;transition:opacity .2s;z-index:200;line-height:1.5;box-shadow:0 8px 32px rgba(0,0,0,0.5)}
.info-icon:hover::after{opacity:1}
.info-icon::before{content:'';position:absolute;bottom:calc(100% + 4px);left:50%;transform:translateX(-50%);border:5px solid transparent;border-top-color:rgba(123,47,255,0.3);opacity:0;transition:opacity .2s;z-index:201}
.info-icon:hover::before{opacity:1}
.plat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:24px}
.plat-card{border-radius:14px;padding:18px;transition:transform .2s}
.plat-card:hover{transform:translateY(-2px)}
.plat-header{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.plat-dot{width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:800;flex-shrink:0;color:var(--n)}
.plat-name{font-family:'Syne',sans-serif;font-size:13px;font-weight:700;color:var(--w)}
.plat-verdict{font-size:13px;font-weight:600;color:var(--w);margin-bottom:6px}
.plat-detail{font-size:12px;color:var(--gr);line-height:1.5}
.clip-section{background:var(--n2);border:1px solid rgba(255,255,255,.06);border-radius:16px;padding:24px;margin-bottom:24px}
.clip-ok{display:flex;align-items:center;gap:10px;color:var(--g);font-size:14px;font-weight:600}
.clip-ok::before{content:"✓";width:22px;height:22px;border-radius:50%;background:rgba(0,255,136,.15);display:flex;align-items:center;justify-content:center;font-size:12px;flex-shrink:0}
.clip-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:8px}
.clip-title{font-family:'Syne',sans-serif;font-size:12px;letter-spacing:3px;text-transform:uppercase;color:var(--v)}
.clip-badge{font-size:11px;font-weight:700;padding:3px 10px;border-radius:100px}
.clip-badge.severe{background:rgba(255,60,60,.15);color:#FF3C3C;border:1px solid rgba(255,60,60,.3)}
.clip-badge.modere{background:rgba(255,180,0,.12);color:#FFB400;border:1px solid rgba(255,180,0,.3)}
.clip-badge.leger{background:rgba(255,180,0,.08);color:#FFB400;border:1px solid rgba(255,180,0,.2)}
.clip-timeline{position:relative;height:32px;background:rgba(255,255,255,.04);border-radius:8px;overflow:hidden;margin-bottom:12px}
.clip-marker{position:absolute;top:0;height:100%;min-width:2px;border-radius:2px;cursor:default}
.clip-marker.leger{background:rgba(255,180,0,.7)}
.clip-marker.fort{background:rgba(255,60,60,.85)}
.clip-list{display:flex;flex-wrap:wrap;gap:6px}
.clip-tag{font-size:11px;padding:3px 9px;border-radius:6px;font-weight:600;font-family:'Syne',sans-serif}
.clip-tag.leger{background:rgba(255,180,0,.1);color:#FFB400;border:1px solid rgba(255,180,0,.2)}
.clip-tag.fort{background:rgba(255,60,60,.1);color:#FF3C3C;border:1px solid rgba(255,60,60,.2)}
.dropdown{position:relative}
.menu-btn{background:none;border:1px solid rgba(255,255,255,0.2);border-radius:8px;padding:8px 12px;cursor:pointer;display:flex;flex-direction:column;gap:5px;transition:border-color .2s}
.menu-btn:hover{border-color:rgba(123,47,255,.5)}
.menu-btn span{display:block;width:22px;height:2px;background:#F0F0F8;border-radius:2px;transition:all .3s}
.menu-btn.open span:nth-child(1){transform:translateY(7px) rotate(45deg)}
.menu-btn.open span:nth-child(2){opacity:0;transform:scaleX(0)}
.menu-btn.open span:nth-child(3){transform:translateY(-7px) rotate(-45deg)}
.dropdown-menu{position:absolute;top:52px;right:0;background:rgba(15,15,25,0.97);backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,0.1);border-radius:16px;padding:12px;min-width:240px;display:none;flex-direction:column;gap:4px;z-index:1000;box-shadow:0 20px 60px rgba(0,0,0,0.5)}
.dropdown-menu.open{display:flex}
.dropdown-item{color:#F0F0F8;text-decoration:none;padding:12px 16px;border-radius:10px;font-size:15px;font-weight:500;transition:background 0.2s}
.dropdown-item:hover{background:rgba(123,47,255,0.2)}
.dropdown-divider{height:1px;background:rgba(255,255,255,0.08);margin:8px 0}
.lang-selector{display:flex;gap:8px;padding:8px 16px;justify-content:center}
.lang-flag{font-size:22px;cursor:pointer;opacity:0.7;transition:all 0.2s;border-radius:4px;padding:4px}
.lang-flag:hover{opacity:1;transform:scale(1.2)}
.confetti-piece{position:fixed;width:8px;height:8px;border-radius:2px;pointer-events:none;z-index:9999;animation:confettiFall linear forwards}
@keyframes confettiFall{0%{transform:translateY(-10px) rotate(0deg);opacity:1}100%{transform:translateY(100vh) rotate(720deg);opacity:0}}
.bg-blob-a{position:fixed;top:-25%;left:-10%;width:75%;height:75%;background:radial-gradient(ellipse,rgba(123,47,255,0.2) 0%,transparent 60%);pointer-events:none;z-index:0;filter:blur(48px);animation:bgBlob1 16s ease-in-out infinite}
.bg-blob-b{position:fixed;bottom:-20%;right:-8%;width:65%;height:65%;background:radial-gradient(ellipse,rgba(0,229,255,0.14) 0%,transparent 58%);pointer-events:none;z-index:0;filter:blur(55px);animation:bgBlob2 21s ease-in-out infinite}
.bg-blob-c{position:fixed;top:35%;left:25%;width:50%;height:50%;background:radial-gradient(ellipse,rgba(0,255,136,0.08) 0%,transparent 65%);pointer-events:none;z-index:0;filter:blur(70px);animation:bgBlob1 13s ease-in-out infinite reverse}
.bg-blob-d{position:fixed;top:10%;right:15%;width:35%;height:35%;background:radial-gradient(ellipse,rgba(123,47,255,0.1) 0%,transparent 60%);pointer-events:none;z-index:0;filter:blur(40px);animation:bgBlob2 18s ease-in-out infinite reverse}
.bg-beam-a{position:fixed;top:-40%;left:35%;width:18%;height:180%;background:linear-gradient(180deg,transparent 0%,rgba(123,47,255,0.05) 25%,rgba(0,229,255,0.07) 50%,rgba(0,255,136,0.04) 75%,transparent 100%);transform-origin:top center;pointer-events:none;z-index:0;filter:blur(28px);animation:bgBeam 28s ease-in-out infinite}
.bg-grid-a{position:fixed;inset:0;z-index:0;pointer-events:none;background-image:linear-gradient(rgba(123,47,255,0.025) 1px,transparent 1px),linear-gradient(90deg,rgba(123,47,255,0.025) 1px,transparent 1px);background-size:64px 64px;-webkit-mask-image:radial-gradient(ellipse 85% 85% at 50% 50%,transparent 25%,black 100%);mask-image:radial-gradient(ellipse 85% 85% at 50% 50%,transparent 25%,black 100%)}
@keyframes bgBlob1{0%,100%{transform:translate(0,0) scale(1);opacity:.8}25%{transform:translate(7%,-9%) scale(1.18);opacity:1}55%{transform:translate(3%,7%) scale(.88);opacity:.6}75%{transform:translate(-5%,2%) scale(1.08);opacity:.9}}
@keyframes bgBlob2{0%,100%{transform:translate(0,0) scale(1);opacity:.7}33%{transform:translate(-8%,6%) scale(1.14);opacity:1}66%{transform:translate(6%,-7%) scale(.82);opacity:.5}}
@keyframes bgBeam{0%,100%{left:15%;opacity:.35;transform:rotate(-22deg) scaleX(1)}20%{left:45%;opacity:.7;transform:rotate(-4deg) scaleX(1.3)}45%{left:65%;opacity:.45;transform:rotate(12deg) scaleX(.8)}70%{left:30%;opacity:.85;transform:rotate(-16deg) scaleX(1.15)}}
@media(max-width:640px){
  nav{padding:14px 18px}
  .hero{padding:40px 16px 24px}
  .hero h1{font-size:clamp(28px,9vw,44px);letter-spacing:-1px}
  .modes{flex-direction:column;gap:4px}
  .mb{padding:10px 14px;text-align:left}
  .sgrid{grid-template-columns:repeat(2,1fr)}
  .rheader{flex-direction:column;align-items:flex-start}
  .rbox{padding:20px}
  .btn-back{width:100%;justify-content:center}
  .upload-zone{padding:36px 20px}
}
"""

JS_SCRIPT = """
// ── CANVAS TOILE D'ONDES (animation pendant l'analyse) ──────────────────
var _canvas=document.getElementById('analyseCanvas');
var _ctx=_canvas?_canvas.getContext('2d'):null;
var _nodes=[],_animFrame=null,_tick=0;
var _COLORS=['#7B2FFF','#00E5FF','#00FF88'];
function _initCanvas(){
  if(!_canvas)return;
  _canvas.width=window.innerWidth;_canvas.height=window.innerHeight;
  _nodes=[];
  for(var i=0;i<120;i++){
    _nodes.push({x:Math.random()*_canvas.width,y:Math.random()*_canvas.height,
      vx:(Math.random()-.5)*.6,vy:(Math.random()-.5)*.6,
      r:Math.random()*2.5+.8,col:_COLORS[Math.floor(Math.random()*3)],
      ph:Math.random()*Math.PI*2,fr:.018+Math.random()*.022});
  }
}
function _drawFrame(){
  if(!_ctx)return;
  _tick++;
  _ctx.clearRect(0,0,_canvas.width,_canvas.height);
  var t=_tick;
  _nodes.forEach(function(n){
    n.x+=n.vx+Math.sin(t*n.fr+n.ph)*.55;
    n.y+=n.vy+Math.cos(t*n.fr*.7+n.ph)*.4;
    if(n.x<0||n.x>_canvas.width)n.vx*=-1;
    if(n.y<0||n.y>_canvas.height)n.vy*=-1;
    n.x=Math.max(0,Math.min(_canvas.width,n.x));
    n.y=Math.max(0,Math.min(_canvas.height,n.y));
  });
  for(var i=0;i<_nodes.length;i++){
    for(var j=i+1;j<_nodes.length;j++){
      var dx=_nodes[i].x-_nodes[j].x,dy=_nodes[i].y-_nodes[j].y;
      var d=Math.sqrt(dx*dx+dy*dy);
      if(d<160){
        var pulse=(Math.sin(t*.04+_nodes[i].ph)+1)/2;
        var a=(1-d/160)*(.25+pulse*.55);
        var hex=Math.floor(a*255).toString(16).padStart(2,'0');
        _ctx.beginPath();_ctx.moveTo(_nodes[i].x,_nodes[i].y);_ctx.lineTo(_nodes[j].x,_nodes[j].y);
        _ctx.strokeStyle=_nodes[i].col+hex;_ctx.lineWidth=.4+pulse*.7;_ctx.stroke();
      }
    }
  }
  _nodes.forEach(function(n){
    var pulse=(Math.sin(t*n.fr*2.2+n.ph)+1)/2;
    var r=n.r*(1+pulse*.7);
    var g=_ctx.createRadialGradient(n.x,n.y,0,n.x,n.y,r*5);
    g.addColorStop(0,n.col+'BB');g.addColorStop(1,n.col+'00');
    _ctx.beginPath();_ctx.arc(n.x,n.y,r*5,0,Math.PI*2);_ctx.fillStyle=g;_ctx.fill();
    _ctx.beginPath();_ctx.arc(n.x,n.y,r,0,Math.PI*2);_ctx.fillStyle=n.col;_ctx.fill();
  });
  _animFrame=requestAnimationFrame(_drawFrame);
}
function startAnalysisCanvas(){_initCanvas();if(_canvas){_canvas.classList.add('active');_tick=0;_drawFrame();}}
function stopAnalysisCanvas(){if(_canvas)_canvas.classList.remove('active');if(_animFrame){cancelAnimationFrame(_animFrame);_animFrame=null;}}
window.addEventListener('resize',function(){if(_canvas&&_canvas.classList.contains('active'))_initCanvas();});

// ── WAVEFORM ─────────────────────────────────────────────────────────────
const hw=document.getElementById("hw");
for(let i=0;i<36;i++){
  const b=document.createElement("div");b.className="wb";
  b.style.height=(Math.random()*28+8)+"px";
  b.style.animationDelay=(Math.random()*1.5).toFixed(2)+"s";
  hw.appendChild(b);
}

// ── MUTATION OBSERVER scores ──────────────────────────────────────────────
function animateScore(el){
  if(el._animated)return;el._animated=true;
  var target=parseInt(el.getAttribute("data-score")),start=null,duration=1200;
  function step(ts){if(!start)start=ts;var p=Math.min((ts-start)/duration,1),ease=1-Math.pow(1-p,3);el.textContent=Math.round(ease*target)+"%";if(p<1)requestAnimationFrame(step);}
  requestAnimationFrame(step);
}
function scanForScores(root){
  if(!root.querySelectorAll)return;
  root.querySelectorAll(".scval[data-score]").forEach(animateScore);
  root.querySelectorAll(".sbf[data-width]").forEach(function(b){if(!b._animated){b._animated=true;setTimeout(function(){b.style.width=b.getAttribute("data-width")+"%"},60);}});
  root.querySelectorAll(".sc.feat .scval[data-score]").forEach(function(el){if(parseInt(el.getAttribute("data-score"))>=80)launchConfetti();});
}
var resDiv=document.getElementById("result");
new MutationObserver(function(mutations){mutations.forEach(function(m){m.addedNodes.forEach(function(n){if(n.nodeType===1)scanForScores(n);});});}).observe(resDiv,{childList:true,subtree:true});

// ── CONFETTI ──────────────────────────────────────────────────────────────
function launchConfetti(){
  if(window._confettiFired)return;window._confettiFired=true;
  var colors=["#7B2FFF","#00E5FF","#00FF88","#FF8C00","#FF4488"];
  for(var i=0;i<80;i++){(function(i){setTimeout(function(){var el=document.createElement("div");el.className="confetti-piece";el.style.left=Math.random()*100+"vw";el.style.background=colors[Math.floor(Math.random()*colors.length)];el.style.animationDuration=(2+Math.random()*2).toFixed(2)+"s";el.style.animationDelay=(Math.random()*.8).toFixed(2)+"s";el.style.width=(6+Math.random()*8)+"px";el.style.height=(6+Math.random()*8)+"px";document.body.appendChild(el);setTimeout(function(){el.remove()},4000);},i*30);})(i);}
}

// ── FILE INPUT + DRAG & DROP ──────────────────────────────────────────────
document.getElementById("fi").addEventListener("change",function(){
  var uz=document.querySelector(".upload-zone");
  if(this.files.length){document.getElementById("fs").textContent="Fichier: "+this.files[0].name;if(uz)uz.classList.add("has-file");document.querySelectorAll(".wb").forEach(function(b){b.classList.add("fast")});}
});
[["ref1","r1n"],["ref2","r2n"],["ref3","r3n"],["href1","h1n"],["href2","h2n"]].forEach(function(p){
  var inp=document.querySelector("input[name='"+p[0]+"']");
  if(inp)inp.addEventListener("change",function(){if(this.files.length)document.getElementById(p[1]).textContent="OK: "+this.files[0].name;});
});
var uz=document.querySelector(".upload-zone");
if(uz){
  ["dragenter","dragover"].forEach(function(ev){uz.addEventListener(ev,function(e){e.preventDefault();uz.classList.add("dragover");});});
  ["dragleave","drop"].forEach(function(ev){uz.addEventListener(ev,function(e){e.preventDefault();uz.classList.remove("dragover");if(ev==="drop"&&e.dataTransfer.files.length){var fi=document.getElementById("fi");var dt=new DataTransfer();dt.items.add(e.dataTransfer.files[0]);fi.files=dt.files;fi.dispatchEvent(new Event("change"));}});});
}

// ── PROGRESS STEPS + FORM SUBMIT ──────────────────────────────────────────
var stepTimings=[0,1200,3500,7000,12000,20000];

// Pourcentages et labels par étape
var stepData=[
  {pct:5,  label:"Upload en cours..."},
  {pct:18, label:"Lecture du fichier audio..."},
  {pct:35, label:"Analyse frequentielle..."},
  {pct:52, label:"Analyse dynamique et stereo..."},
  {pct:68, label:"Analyse rythme, espace et sidechain..."},
  {pct:80, label:"Creation du rapport IA..."},
];

var _progressInterval=null;
var _currentPct=0;
var _targetPct=0;

function setProgress(pct, label){
  _targetPct=pct;
  document.getElementById("progressLabel").textContent=label||"";
}

function _animateProgress(){
  if(_currentPct<_targetPct){
    // Avancer vite jusqu'à 95% de la cible, puis ralentir
    var gap=_targetPct-_currentPct;
    var step=gap>20?3:gap>5?1:0.3;
    _currentPct=Math.min(_targetPct, _currentPct+step);
    var bar=document.getElementById("progressBar");
    var pct=document.getElementById("progressPct");
    if(bar)bar.style.width=_currentPct+"%";
    if(pct)pct.textContent=Math.round(_currentPct)+"%";
  } else if(_currentPct>=80 && _currentPct<95){
    // Pendant la création du rapport : montée lente automatique vers 95%
    _currentPct+=0.05;
    var bar=document.getElementById("progressBar");
    var pct=document.getElementById("progressPct");
    if(bar)bar.style.width=_currentPct+"%";
    if(pct)pct.textContent=Math.round(_currentPct)+"%";
  }
}

function activateStep(i){
  document.querySelectorAll(".pstep").forEach(function(s,idx){
    s.classList.remove("active-step");
    if(idx<i)s.classList.add("done");
    else s.classList.remove("done");
  });
  var steps=document.querySelectorAll(".pstep");
  if(steps[i])steps[i].classList.add("active-step");
  if(stepData[i])setProgress(stepData[i].pct, stepData[i].label);
}

document.getElementById("mf").addEventListener("submit",async function(e){
  e.preventDefault();var fd=new FormData(this);this.style.display="none";
  var ps=document.getElementById("psteps");ps.classList.add("active");
  document.querySelectorAll(".wb").forEach(function(b){b.classList.remove("fast");b.classList.add("analyzing");});
  startAnalysisCanvas();
  if(window._analysePortal){var btn2=document.querySelector('.upload-btn,[type=submit]');var br2=btn2?btn2.getBoundingClientRect():{left:window.innerWidth/2,top:window.innerHeight/2,width:0,height:0};window._analysePortal(br2.left+br2.width/2,br2.top+br2.height/2);}
  _currentPct=0;_targetPct=0;
  _progressInterval=setInterval(_animateProgress, 40);
  var timers=stepTimings.map(function(t,i){return setTimeout(function(){activateStep(i);},t);});
  try{
    var r=await fetch("/analyser",{method:"POST",body:fd});
    timers.forEach(clearTimeout);
    // Rapport reçu : monter à 100%
    _targetPct=100;
    activateStep(5);
    await new Promise(function(resolve){setTimeout(resolve,600);});
    clearInterval(_progressInterval);
    var bar=document.getElementById("progressBar");
    var pct=document.getElementById("progressPct");
    if(bar)bar.style.width="100%";
    if(pct)pct.textContent="100%";
    if(document.getElementById("progressLabel"))document.getElementById("progressLabel").textContent="Rapport pret !";
    await new Promise(function(resolve){setTimeout(resolve,400);});
    ps.classList.remove("active");stopAnalysisCanvas();
    document.querySelectorAll(".wb").forEach(function(b){b.classList.remove("analyzing");});
    var res=document.getElementById("result");res.classList.add("active");
    var reader=r.body.getReader(),decoder=new TextDecoder();
    while(true){var chunk=await reader.read();if(chunk.done)break;res.insertAdjacentHTML("beforeend",decoder.decode(chunk.value));}
  }catch(err){timers.forEach(clearTimeout);clearInterval(_progressInterval);ps.classList.remove("active");stopAnalysisCanvas();document.getElementById("mf").style.display="block";alert("Erreur lors de l analyse");}
});

// ── MODE SWITCH ───────────────────────────────────────────────────────────
function switchMode(mode,btn){document.querySelectorAll(".mb").forEach(function(b){b.classList.remove("active");});document.querySelectorAll(".mp").forEach(function(p){p.classList.remove("active");});btn.classList.add("active");document.getElementById("panel-"+mode).classList.add("active");document.getElementById("mi").value=mode;}
function ff(family,btn){document.querySelectorAll(".fb").forEach(function(b){b.classList.remove("active");});btn.classList.add("active");var sel=document.getElementById("gs");sel.querySelectorAll("optgroup").forEach(function(g){g.style.display=(family==="all"||g.dataset.f===family)?"":"none";});}
function toggleMenu(){var m=document.getElementById('dropdownMenu'),btn=document.querySelector('.menu-btn');m.classList.toggle('open');if(btn)btn.classList.toggle('open');}
document.addEventListener('click',function(e){if(!e.target.closest('.dropdown')){document.getElementById('dropdownMenu').classList.remove('open');var btn=document.querySelector('.menu-btn');if(btn)btn.classList.remove('open');}});
function setLang(l){alert('Langue '+l+' - bientot disponible !');}

// ── Tooltip custom universel ──────────────────────────────────────────────
(function(){
  var tip=document.createElement('div');tip.className='iym-tip';tip.id='iymTip';document.body.appendChild(tip);
  function showTip(txt,x,y){tip.innerHTML=txt;tip.style.opacity='1';moveTip(x,y);}
  function moveTip(x,y){var tw=tip.offsetWidth,th=tip.offsetHeight;var lft=Math.min(x+14,window.innerWidth-tw-10);var top=y-th-12;if(top<8)top=y+22;tip.style.left=lft+'px';tip.style.top=top+'px';}
  function hideTip(){tip.style.opacity='0';}
  document.addEventListener('mouseover',function(e){
    var sc=e.target.closest('.sc[data-iym-tip]');if(sc){showTip(sc.getAttribute('data-iym-tip'),e.clientX,e.clientY);return;}
    var fb=e.target.closest('[data-tip]');if(fb){showTip(fb.getAttribute('data-tip'),e.clientX,e.clientY);return;}
  });
  document.addEventListener('mousemove',function(e){if(tip.style.opacity==='1')moveTip(e.clientX,e.clientY);});
  document.addEventListener('mouseout',function(e){if(!document.querySelector('.sc[data-iym-tip]:hover,[data-tip]:hover'))hideTip();});
})();

// Mise à jour nav selon statut connexion
(function(){
  fetch('/api/me').then(function(r){return r.json();}).then(function(data){
    var nr = document.querySelector('.nav-right');
    if(!nr) return;
    if(data.logged){
      var remaining = data.remaining;
      var color = remaining > 5 ? '#00FF88' : remaining > 0 ? '#FFB400' : '#FF3C3C';
      nr.innerHTML = '<a href="/account" style="color:#8888AA;font-size:13px;text-decoration:none;font-family:DM Sans,sans-serif;margin-right:4px">'+data.email.split('@')[0]+'</a>'
        +'<a href="/account" style="background:linear-gradient(135deg,#7B2FFF,#5020CC);color:white;padding:10px 24px;border-radius:24px;text-decoration:none;font-size:13px;font-weight:600;font-family:Syne,sans-serif;box-shadow:0 4px 20px rgba(123,47,255,.3)"><span style="color:'+color+'">'+remaining+'</span> analyses</a>';
    } else {
      // Non connecté : bouton Analyser déjà en place
    }
  }).catch(function(){});
})();
"""

TRANSITION_HTML = """<div id="portalOverlay"><canvas id="portalCanvas"></canvas></div>
<style>
#portalOverlay{position:fixed;inset:0;z-index:99999;pointer-events:none;opacity:0;background:#07070F;transition:none}
#portalOverlay.active{pointer-events:all}
#portalCanvas{position:absolute;inset:0;width:100%;height:100%}
</style>
<script>(function(){
var ov=document.getElementById('portalOverlay');
var cv=document.getElementById('portalCanvas');
var ctx=cv.getContext('2d');
var W,H,aid=null,dest=null;
// Couleurs portail analyse : bleu/violet uniquement, pas d'arc-en-ciel
var BLUES=['#7B2FFF','#5B4FFF','#00E5FF','#4488FF','#0099FF','#3366FF'];

function resize(){W=cv.width=window.innerWidth;H=cv.height=window.innerHeight;}
window.addEventListener('resize',resize);resize();

// ── MODE SIMPLE : fondu noir vers la page suivante ─────────────────────
window._fadeTo=function(href){if(window._fadeActive)return;window._fadeActive=true;
  if(aid)return;
  dest=href;
  ov.style.transition='opacity .38s ease';
  ov.style.opacity='1';
  ov.classList.add('active');
  setTimeout(function(){if(dest)window.location.href=dest;},400);
};

// ── MODE PORTAIL : effets bleu/violet pour "Analyser mon mix" ──────────
var rings=[],beams=[],parts=[],t0=0,DUR=820;
function initPortal(ox,oy){
  var cx=ox||W/2,cy=oy||H/2;
  rings=[];beams=[];parts=[];
  for(var i=0;i<12;i++)rings.push({cx:cx,cy:cy,d:i*48,col:BLUES[i%BLUES.length],w:1.5+i*.5});
  for(var i=0;i<48;i++){var a=i/48*Math.PI*2,dist=Math.sqrt(W*W+H*H)*.75;beams.push({cx:cx,cy:cy,a:a,dist:dist,col:BLUES[i%BLUES.length],w:.5+Math.random()*1.8,d:i*14});}
  for(var i=0;i<80;i++){var a=Math.random()*Math.PI*2,s=4+Math.random()*9;parts.push({x:cx,y:cy,vx:Math.cos(a)*s,vy:Math.sin(a)*s,r:1.5+Math.random()*3,life:1,col:BLUES[Math.floor(Math.random()*BLUES.length)]});}
  return {cx:cx,cy:cy};
}
function drawPortal(t,cx,cy){
  var p=Math.min(t/DUR,1),ease=p<.5?2*p*p:1-Math.pow(-2*p+2,2)/2;
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle='rgba(7,7,15,'+Math.min(ease*.96,.96)+')';ctx.fillRect(0,0,W,H);
  // Faisceaux convergents
  beams.forEach(function(b){
    var bt=Math.max(0,(t-b.d)/DUR);if(!bt)return;
    var alpha=Math.min(bt*3,.9)*(1-ease*.3),startD=b.dist*(1-Math.min(bt*2.2,1));
    var sx=b.cx+Math.cos(b.a)*b.dist,sy=b.cy+Math.sin(b.a)*b.dist;
    var ex=b.cx+Math.cos(b.a)*startD,ey=b.cy+Math.sin(b.a)*startD;
    var g=ctx.createLinearGradient(sx,sy,ex,ey);
    g.addColorStop(0,b.col+'00');
    g.addColorStop(.5,b.col+Math.floor(alpha*140).toString(16).padStart(2,'0'));
    g.addColorStop(1,b.col+Math.floor(alpha*255).toString(16).padStart(2,'0'));
    ctx.beginPath();ctx.moveTo(sx,sy);ctx.lineTo(ex,ey);ctx.strokeStyle=g;ctx.lineWidth=b.w*(1+ease*2.5);ctx.stroke();
  });
  // Ondes concentriques
  var maxR=Math.sqrt(W*W+H*H);
  rings.forEach(function(r){
    var rt=Math.max(0,(t-r.d)/DUR);if(!rt)return;
    var alpha=Math.min(rt*4,1)*(1-rt*.8);if(alpha<.01)return;
    var radius=maxR*rt*.88;
    ctx.beginPath();ctx.arc(r.cx,r.cy,radius,0,Math.PI*2);
    ctx.strokeStyle=r.col+Math.floor(alpha*220).toString(16).padStart(2,'0');
    ctx.lineWidth=r.w*(1+ease*2);ctx.stroke();
    if(radius>20){ctx.beginPath();ctx.arc(r.cx,r.cy,radius*.85,0,Math.PI*2);ctx.strokeStyle=r.col+Math.floor(alpha*60).toString(16).padStart(2,'0');ctx.lineWidth=r.w*.3;ctx.stroke();}
  });
  // Particules
  parts.forEach(function(pt){pt.x+=pt.vx*(1+ease*4);pt.y+=pt.vy*(1+ease*4);pt.life-=.015;if(pt.life<.04)return;ctx.beginPath();ctx.arc(pt.x,pt.y,pt.r*pt.life,0,Math.PI*2);ctx.fillStyle=pt.col+Math.floor(pt.life*185).toString(16).padStart(2,'0');ctx.fill();});
  // Flash bleu central
  if(p>.5&&p<.8){var fp=(p-.5)/.3,fa=fp<.5?fp*2:(1-fp)*2;var gr=ctx.createRadialGradient(cx,cy,0,cx,cy,fa*180);gr.addColorStop(0,'rgba(80,100,255,'+fa*.75+')');gr.addColorStop(.3,'rgba(0,180,255,'+fa*.35+')');gr.addColorStop(1,'rgba(0,50,180,0)');ctx.fillStyle=gr;ctx.fillRect(0,0,W,H);}
  // Trou aspirant central
  if(p>.28){var vp=(p-.28)/.72,vr=vp*90;var vg=ctx.createRadialGradient(cx,cy,0,cx,cy,vr);vg.addColorStop(0,'rgba(0,0,10,'+Math.min(vp*.92,.92)+')');vg.addColorStop(.6,'rgba(7,7,15,'+Math.min(vp*.4,.4)+')');vg.addColorStop(1,'rgba(7,7,15,0)');ctx.fillStyle=vg;ctx.beginPath();ctx.arc(cx,cy,vr,0,Math.PI*2);ctx.fill();}
}
window._analysePortal=function(ox,oy){
  if(aid)return;
  ov.style.transition='none';ov.style.opacity='1';ov.classList.add('active');
  var pos=initPortal(ox,oy);
  t0=performance.now();
  function run(ts){var t=ts-t0;drawPortal(t,pos.cx,pos.cy);if(t<DUR){aid=requestAnimationFrame(run);}else{ctx.fillStyle='#07070F';ctx.fillRect(0,0,W,H);aid=null;ov.style.transition='opacity .3s ease';ov.style.opacity='0';setTimeout(function(){ov.classList.remove('active');},350);}}
  aid=requestAnimationFrame(run);
};

// ── Intercepter les liens → fondu noir simple ─────────────────────────
document.addEventListener('click',function(e){
  // Laisser passer tous les clics dans le form (file input, boutons, etc.)
  if(e.target.tagName==='INPUT'||e.target.tagName==='BUTTON'||e.target.tagName==='LABEL'||e.target.tagName==='SELECT'||e.target.tagName==='OPTION') return;
  if(e.target.closest('form') || e.target.closest('.upload-zone')) return;
  var a=e.target.closest('a[href]');if(!a)return;
  var h=a.getAttribute('href');
  if(!h||h[0]==='#'||h.indexOf('://')>0||h.startsWith('mailto')||h.startsWith('javascript'))return;
  e.preventDefault();
  window._fadeTo(h);
});

// Apparition en fondu de la page courante
document.documentElement.style.opacity='0';
document.documentElement.style.transition='opacity .36s ease';
setTimeout(function(){document.documentElement.style.opacity='1';},30);
window.addEventListener('load',function(){document.documentElement.style.opacity='1';});

// Mise à jour nav selon statut connexion (toutes les pages)
fetch('/api/me').then(function(r){return r.json();}).then(function(d){
  if(d.logged){
    // Dans le dropdown : remplacer les items auth
    document.querySelectorAll('a[href="/login"]').forEach(function(a){
      a.href='/account';a.textContent='Mon compte';
      a.removeAttribute('style');
    });
    document.querySelectorAll('a[href="/register"]').forEach(function(a){
      a.href='/logout';a.textContent='Se déconnecter';
      a.removeAttribute('style');a.style.color='#8888AA';
    });
    // Bouton principal nav (landing + autres pages) : style nav-cta
    var inscBtn=document.querySelector('.nav-cta[href="/register"]');
    if(inscBtn){
      inscBtn.href='/account';
      inscBtn.textContent='Mon compte →';
    }
    // Bouton Analyser des pages secondaires
    var tryBtn=document.querySelector('.nav-cta[href="/analyze"]');
    if(tryBtn){
      tryBtn.style.cssText='background:linear-gradient(135deg,#7B2FFF,#5020CC);color:white;padding:10px 24px;border-radius:24px;text-decoration:none;font-weight:600;font-size:14px;box-shadow:0 4px 20px rgba(123,47,255,.3)';
    }
  }
}).catch(function(){});
})();</script>"""

HTML_BODY = """
<canvas id="analyseCanvas"></canvas>
<div class="bg-blob-a"></div>
<div class="bg-blob-b"></div>
<div class="bg-blob-c"></div>
<div class="bg-blob-d"></div>
<div class="bg-beam-a"></div>
<div class="bg-grid-a"></div>
<nav><a href="/" class="logo">InsideYourMix</a><div style="display:flex;gap:24px;align-items:center"><div class="dropdown"><button class="menu-btn" onclick="toggleMenu()"><span></span><span></span><span></span></button><div class="dropdown-menu" id="dropdownMenu"><a href="/how-it-works" class="dropdown-item">How it works</a><a href="/why" class="dropdown-item">Why InsideYourMix</a><a href="/abonnements" class="dropdown-item">Abonnements</a><a href="/contact" class="dropdown-item">Contact</a><div class="dropdown-divider"></div><a href="/login" class="dropdown-item">→ Se connecter</a><a href="/register" class="dropdown-item" style="color:#00FF88">✦ Creer un compte</a><div class="dropdown-divider"></div><div class="lang-selector"><span onclick="setLang('fr')" class="lang-flag">🇫🇷</span><span onclick="setLang('en')" class="lang-flag">🇬🇧</span><span onclick="setLang('es')" class="lang-flag">🇪🇸</span><span onclick="setLang('de')" class="lang-flag">🇩🇪</span><span onclick="setLang('pt')" class="lang-flag">🇵🇹</span></div></div></div><a href="/login" style="color:#8888AA;font-size:13px;text-decoration:none;font-family:DM Sans,sans-serif">Se connecter</a><a href="/register" style="background:linear-gradient(135deg,#7B2FFF,#5020CC);color:white;padding:8px 20px;border-radius:20px;text-decoration:none;font-size:13px;font-weight:600;font-family:DM Sans,sans-serif">S'inscrire</a></div></nav>
<div class="hero"><h1>Inside<span>Your</span>Mix</h1>
<p>Upload ton mix. Choisis ton style ou tes references. On analyse et on te guide vers le son que tu vises.</p>
<div class="waveform" id="hw"></div></div>
<div class="main">
<form id="mf" enctype="multipart/form-data">
<div class="upload-zone">
<input type="file" name="fichier" id="fi" accept=".mp3,.wav,.flac,.aiff" required>
<div class="upload-icon">MIX</div>
<h3>Depose ton mix ici</h3>
<p>ou clique pour choisir un fichier</p>
<div class="formats"><span class="fmt">MP3</span><span class="fmt">WAV</span><span class="fmt">FLAC</span><span class="fmt">AIFF</span></div>
<div class="fsel" id="fs"></div>
</div>
<div class="modes">
<button type="button" class="mb active" onclick="switchMode('genre',this)">Par Genre <span class="info-icon" data-tip="Choisis ton genre musical. L'IA compare ton mix aux standards professionnels de ce genre spécifique.">?</span></button>
<button type="button" class="mb" onclick="switchMode('reference',this)">Par Référence <span class="info-icon" data-tip="Upload jusqu'à 3 morceaux de référence. L'IA compare ton mix directement à ces tracks.">?</span></button>
<button type="button" class="mb" onclick="switchMode('hybride',this)">Mode Hybride <span class="info-icon" data-tip="Le meilleur des deux mondes : genre + références. Analyse la plus précise et personnalisée.">?</span></button>
</div>
<div class="mp active" id="panel-genre">
<div class="slabel">Famille musicale</div>
<div class="families">
<button type="button" class="fb active" onclick="ff('all',this)">Tous</button>
<button type="button" class="fb" onclick="ff('techno',this)">Techno</button>
<button type="button" class="fb" onclick="ff('house',this)">House</button>
<button type="button" class="fb" onclick="ff('trance',this)">Trance</button>
<button type="button" class="fb" onclick="ff('bass',this)">Bass</button>
<button type="button" class="fb" onclick="ff('hiphop',this)">Hip-Hop</button>
<button type="button" class="fb" onclick="ff('elec',this)">Electronic</button>
<button type="button" class="fb" onclick="ff('other',this)">Autres</button>
</div>
<select name="genre" id="gs" class="gsel">
<optgroup label="── TECHNO ──" data-f="techno">
<option>Techno</option>
<option>Techno Peak Time</option>
<option>Techno Raw Deep Hypnotic</option>
<option>Melodic Techno</option>
<option>Hard Techno</option>
<option>Industrial Techno</option>
<option>Dub Techno</option>
<option>Minimal Techno</option>
<option>Acid Techno</option>
</optgroup>
<optgroup label="── MELODIC HOUSE & TECHNO ──" data-f="techno">
<option>Melodic House Techno</option>
<option>Melodic House</option>
<option>Progressive House</option>
<option>Organic House</option>
</optgroup>
<optgroup label="── HOUSE ──" data-f="house">
<option>House</option>
<option>Deep House</option>
<option>Tech House</option>
<option>Afro House</option>
<option>Jackin House</option>
<option>Funky House</option>
<option>Bass House</option>
<option>Soulful House</option>
<option>Tribal House</option>
<option>Indie Dance</option>
<option>Nu Disco</option>
<option>Amapiano</option>
</optgroup>
<optgroup label="── TRANCE ──" data-f="trance">
<option>Trance</option>
<option>Trance Main Floor</option>
<option>Trance Raw Deep Hypnotic</option>
<option>Uplifting Trance</option>
<option>Progressive Trance</option>
<option>Vocal Trance</option>
<option>Hard Trance</option>
<option>Tech Trance</option>
</optgroup>
<optgroup label="── PSYTRANCE ──" data-f="trance">
<option>Psytrance</option>
<option>Full-On Psytrance</option>
<option>Dark Psytrance</option>
<option>Goa Trance</option>
</optgroup>
<optgroup label="── DRUM & BASS ──" data-f="bass">
<option>Drum and Bass</option>
<option>Liquid DnB</option>
<option>Neurofunk</option>
<option>Jump Up DnB</option>
<option>Halftime</option>
<option>Jungle</option>
</optgroup>
<optgroup label="── DUBSTEP / 140 ──" data-f="bass">
<option>Dubstep</option>
<option>Deep Dubstep</option>
<option>140 Deep Dubstep</option>
</optgroup>
<optgroup label="── UK BASS ──" data-f="bass">
<option>UK Garage</option>
<option>UK Bass</option>
<option>Grime</option>
<option>Breakbeat</option>
<option>Breaks</option>
</optgroup>
<optgroup label="── HIP-HOP ──" data-f="hiphop">
<option>Hip-Hop</option>
<option>Trap</option>
<option>Drill</option>
<option>Boom Bap</option>
<option>Phonk</option>
<option>Lo-fi Hip-Hop</option>
<option>Future Bass</option>
<option>Jersey Club</option>
<option>RnB</option>
<option>Afrobeats</option>
</optgroup>
<optgroup label="── ELECTRONIC ──" data-f="elec">
<option>Electronica</option>
<option>Electro</option>
<option>Synthwave</option>
<option>Mainstage</option>
<option>Hard Dance</option>
<option>Hardcore</option>
<option>Hardstyle</option>
<option>Neo Rave</option>
<option>Ambient</option>
<option>Downtempo</option>
<option>Bass Club</option>
</optgroup>
<optgroup label="── AUTRES ──" data-f="other">
<option>Pop</option>
<option>Rock</option>
<option>Jazz</option>
<option>Soul</option>
<option>Funk</option>
<option>Reggae</option>
</optgroup>
</select>
</div>
<div class="mp" id="panel-reference">
<div class="ref-zone"><div class="slabel">Tes morceaux de reference (1 a 3)</div>
<div class="ref-slot"><input type="file" name="ref1" accept=".mp3,.wav,.flac">
<div class="rnum">1</div><div class="rinfo"><div class="rtitle" id="r1n">Reference 1</div>
<div class="rsub">Upload un morceau qui t inspire</div></div></div>
<div class="ref-slot"><input type="file" name="ref2" accept=".mp3,.wav,.flac">
<div class="rnum">2</div><div class="rinfo"><div class="rtitle" id="r2n">Reference 2</div>
<div class="rsub">Optionnel</div></div></div>
<div class="ref-slot"><input type="file" name="ref3" accept=".mp3,.wav,.flac">
<div class="rnum">3</div><div class="rinfo"><div class="rtitle" id="r3n">Reference 3</div>
<div class="rsub">Optionnel</div></div></div>
</div></div>
<div class="mp" id="panel-hybride">
<div style="margin-bottom:16px"><div class="slabel">Genre de base</div>
<select name="genre_hybride" class="gsel">
<option>Melodic Techno</option><option>Deep House</option><option>Techno</option>
<option>Hip-Hop</option><option>Drum and Bass</option><option>Trap</option>
</select></div>
<div class="ref-zone"><div class="slabel">+ Tes references personnelles</div>
<div class="ref-slot"><input type="file" name="href1" accept=".mp3,.wav,.flac">
<div class="rnum">1</div><div class="rinfo"><div class="rtitle" id="h1n">Reference hybride 1</div>
<div class="rsub">Le morceau qui inspire ton son</div></div></div>
<div class="ref-slot"><input type="file" name="href2" accept=".mp3,.wav,.flac">
<div class="rnum">2</div><div class="rinfo"><div class="rtitle" id="h2n">Reference hybride 2</div>
<div class="rsub">Optionnel</div></div></div>
</div></div>
<input type="hidden" name="mode" id="mi" value="genre">
<button type="submit" class="btn-go">Analyser mon mix</button>
</form>
<div class="loading" id="loading" style="display:none"></div>
<div class="psteps" id="psteps">
<div class="psteps-title">Analyse en cours...</div>
<div class="psteps-sub">Notre IA examine ton mix en profondeur</div>
<div class="progress-pct" id="progressPct">0%</div>
<div class="progress-label" id="progressLabel">Initialisation...</div>
<div class="progress-wrap"><div class="progress-bar" id="progressBar"></div></div>
<div class="pstep" id="ps1"><div class="pstep-dot">01</div><div class="pstep-label">Upload en cours</div></div>
<div class="pstep" id="ps2"><div class="pstep-dot">02</div><div class="pstep-label">Lecture du fichier audio</div></div>
<div class="pstep" id="ps3"><div class="pstep-dot">03</div><div class="pstep-label">Analyse frequentielle</div></div>
<div class="pstep" id="ps4"><div class="pstep-dot">04</div><div class="pstep-label">Analyse dynamique et stereo</div></div>
<div class="pstep" id="ps5"><div class="pstep-dot">05</div><div class="pstep-label">Analyse rythme et espace</div></div>
<div class="pstep" id="ps6"><div class="pstep-dot">06</div><div class="pstep-label">Analyse des donnees et creation du rapport</div></div>
<div class="pstep" id="ps7"><div class="pstep-dot">07</div><div class="pstep-label">Rapport pret !</div></div>
</div>
<div class="result" id="result"></div>
</div>
"""

ANALYZE_PAGE = (
    '<!DOCTYPE html><html lang="fr"><head>'
    '<meta charset="UTF-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
    '<title>InsideYourMix</title>'
    '<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">'
    '<style>' + CSS_STYLES + '</style>'
    ''
    '</head><body>'
    + HTML_BODY +
    '<script>' + JS_SCRIPT + '</script>'
    + TRANSITION_HTML +
    '</body></html>'
)

@app.route("/analyser", methods=["POST"])
def analyser():
    # Vérification quota
    if not current_user.is_authenticated:
        # Anonymes : 3 analyses gratuites via session
        count = session.get('anon_count', 0)
        if count >= 3:
            html = ('<div style="text-align:center;padding:60px 20px">'
                    '<div style="font-size:32px;margin-bottom:12px">🎚️</div>'
                    '<div style="font-family:Syne,sans-serif;font-size:20px;font-weight:700;margin-bottom:8px">3 analyses gratuites utilisées</div>'
                    '<p style="color:#8888AA;margin-bottom:24px">Crée un compte gratuit pour continuer — 3 analyses offertes chaque mois.</p>'
                    '<a href="/register" style="display:inline-block;padding:14px 32px;background:linear-gradient(135deg,#7B2FFF,#5020CC);color:white;border-radius:12px;font-weight:700;text-decoration:none;font-family:Syne,sans-serif">Créer mon compte gratuitement →</a>'
                    '<p style="margin-top:16px;font-size:13px;color:#8888AA">Déjà inscrit ? <a href="/login" style="color:#00E5FF">Se connecter</a></p>'
                    '</div>')
            return Response(html, mimetype='text/html')
        session['anon_count'] = count + 1
    else:
        if not current_user.can_analyse():
            plan = current_user.plan
            upgrade_msg = ''
            if plan == 'free':
                upgrade_msg = '<a href="/abonnements" style="display:inline-block;padding:14px 32px;background:linear-gradient(135deg,#7B2FFF,#5020CC);color:white;border-radius:12px;font-weight:700;text-decoration:none;font-family:Syne,sans-serif">Passer au Starter — 2,99€/mois →</a>'
            elif plan == 'starter':
                upgrade_msg = '<a href="/abonnements" style="display:inline-block;padding:14px 32px;background:linear-gradient(135deg,#7B2FFF,#5020CC);color:white;border-radius:12px;font-weight:700;text-decoration:none;font-family:Syne,sans-serif">Passer au Pro — 4,99€/mois →</a>'
            html = ('<div style="text-align:center;padding:60px 20px">'
                    '<div style="font-size:32px;margin-bottom:12px">🎚️</div>'
                    '<div style="font-family:Syne,sans-serif;font-size:20px;font-weight:700;margin-bottom:8px">Quota mensuel atteint</div>'
                    '<p style="color:#8888AA;margin-bottom:24px">Tu as utilisé toutes tes analyses ce mois-ci. Upgrade pour continuer.</p>'
                    + upgrade_msg +
                    '</div>')
            return Response(html, mimetype='text/html')

    if "fichier" not in request.files:
        return "<p>Aucun fichier</p>", 400
    fichier = request.files["fichier"]
    genre = request.form.get("genre") or request.form.get("genre_hybride", "Techno")
    mode = request.form.get("mode", "genre")
    ext = fichier.filename.split(".")[-1]
    chemin = os.path.join(UPLOAD_FOLDER, str(uuid.uuid4()) + "." + ext)
    fichier.save(chemin)
    refs = []
    for rname in ["ref1", "ref2", "ref3", "href1", "href2"]:
        if rname in request.files:
            rf = request.files[rname]
            if rf.filename:
                rp = os.path.join(UPLOAD_FOLDER, str(uuid.uuid4()) + "_" + rf.filename)
                rf.save(rp)
                refs.append(rp)

    # ── Incrémenter le quota ICI avant le streaming ──────────────────────
    user_id_for_gen = None
    if current_user.is_authenticated:
        current_user.analyses_this_month += 1
        db.session.commit()
        user_id_for_gen = current_user.id

    def generate():
        yield '<div style="display:none">start</div>'
        try:
            donnees = analyser_audio(chemin, genre=genre)
            scores      = calculer_scores(donnees, genre)
            plateformes = calculer_plateformes(donnees)
            niveau_prod = detecter_niveau_producteur(donnees)
            clip        = donnees.get("clipping", {"has_clipping": False, "count": 0, "total_pct": 0, "severite": "aucun", "events": [], "seuil_db": -0.3})
            # Durée totale estimée depuis les segments BOT
            segments_bot  = donnees["balance_over_time"].get("segments", [])
            # Durée totale : max entre fin du dernier segment BOT et fin de la dernière section
            dur_bot = segments_bot[-1]["t"] + 8 if segments_bot else 180
            dur_sec = donnees.get("sections", {}).get("sections", [{}])
            dur_sec = dur_sec[-1].get("t_end", 0) if dur_sec else 0
            duree_totale = max(dur_bot, dur_sec, 30)

            os.remove(chemin)
            # Enregistrer dans l'historique (quota déjà incrémenté avant le streaming)
            if user_id_for_gen:
                try:
                    analysis_record = Analysis(user_id=user_id_for_gen, genre=genre, score=scores.get('global', 0))
                    db.session.add(analysis_record)
                    db.session.commit()
                except Exception:
                    pass

            bot = donnees["balance_over_time"]
            bot_bars = ""
            if bot["segments"]:
                vals = [s["rms_db"] for s in bot["segments"]]
                mn, mx = min(vals), max(vals)
                rng = mx - mn if mx != mn else 1
                for seg in bot["segments"]:
                    h = int(((seg["rms_db"] - mn) / rng) * 50 + 10)
                    bot_bars += build_bot_bar(h)
            bot_events = ""
            for e in bot["events"]:
                bot_events += build_bot_event(e)
            if not bot_events:
                bot_events = '<span style="color:#8888AA">Aucun evenement majeur</span>'

            scores_html = (
                '<div class="sgrid">'
                + build_score_card("global",      "Score Global", scores, donnees, True)
                + build_score_card("frequentiel", "Frequentiel",  scores, donnees)
                + build_score_card("dynamique",   "Dynamique",    scores, donnees)
                + build_score_card("stereo",      "Stereo",       scores, donnees)
                + build_score_card("rythme",      "Rythme",       scores, donnees)
                + build_score_card("espace",      "Espace",       scores, donnees)
                + '</div>'
            )

            plat_html  = build_platform_badges(plateformes)
            clip_html  = build_clipping_html(clip, duree_totale)

            freq = donnees["frequentiel"]
            dyn  = donnees["dynamique"]
            ster = donnees["stereo"]
            ryt  = donnees["rythme"]
            esp  = donnees["espace"]
            bot2 = donnees["balance_over_time"]
            profil = PROFILS_GENRE.get(genre.lower(), PROFILS_GENRE["default"])

            radar_svg    = build_radar_chart(scores)
            freq_svg     = build_freq_chart(freq, profil, genre)
            sections_html= build_sections_timeline(donnees.get("sections", {}), duree_totale)
            oscillo_html = build_oscilloscope(donnees, duree_totale)
            ressources_html = build_ressources_section(donnees, scores, genre)
            ton_data     = donnees.get("tonalite", {})
            punch_data   = donnees.get("punch_kick", {})

            yield (
                '<div class="rheader">'
                '<div><div class="rgenre">' + mode.upper() + ' - ' + genre + '</div>'
                '<div class="rtit">Ton rapport de mix</div></div>'
                '<button class="btn-back" onclick="location.reload()">Nouveau mix</button>'
                '</div>'
                + scores_html
                # ── Viz bloc : radar + spectrogramme côte à côte ──
                + '<div style="display:flex;gap:20px;flex-wrap:wrap;margin:24px 0;align-items:flex-start">'
                # Radar
                + '<div style="flex:0 0 auto;background:rgba(15,15,26,0.7);border:1px solid rgba(255,255,255,0.07);border-radius:16px;padding:20px;text-align:center">'
                + '<div style="font-size:10px;letter-spacing:2px;text-transform:uppercase;color:#8888AA;margin-bottom:12px;font-family:Syne,sans-serif">RADAR DES SCORES</div>'
                + radar_svg
                + '</div>'
                # Spectrogramme
                + '<div style="flex:1;min-width:220px;background:rgba(15,15,26,0.7);border:1px solid rgba(255,255,255,0.07);border-radius:16px;padding:20px">'
                + '<div style="font-size:10px;letter-spacing:2px;text-transform:uppercase;color:#8888AA;margin-bottom:8px;font-family:Syne,sans-serif">BALANCE SPECTRALE — MIX vs CIBLE (passe la souris sur les barres)</div>'
                + freq_svg
                + '<div style="font-size:11px;color:#8888AA;margin-top:8px">&#9646; Fond = cible genre &nbsp; &#9646; Barre = ton mix &nbsp; <span style="color:#00FF88">vert</span>=ok &nbsp; <span style="color:#FFB400">orange</span>=trop bas &nbsp; <span style="color:#FF6B6B">rouge</span>=trop haut</div>'
                + '</div>'
                + '</div>'
                # Timeline des sections
                + sections_html
                + oscillo_html
                + clip_html
                + '<div class="bots">'
                '<div class="bottit">Balance over Time</div>'
                '<div class="botbars">' + bot_bars + '</div>'
                '<div style="margin-top:10px">' + bot_events + '</div>'
                '</div>'
                '<div class="rbox" id="streamBox">'
            )

            # Analyse des references si mode reference ou hybride
            refs_analyse = []
            if refs:
                for rp in refs:
                    if os.path.exists(rp):
                        try:
                            rd = analyser_audio(rp, genre=genre)
                            refs_analyse.append(rd)
                            os.remove(rp)
                        except:
                            pass

            lines = [
                "=== ANALYSE COMPLETE DU MIX ===",
                f"Genre cible: {genre}",
                f"BPM detecte: {ryt['bpm']} (plage normale {genre}: {profil['bpm_min']}-{profil['bpm_max']} BPM)",
                "",
                "--- LOUDNESS & DYNAMIQUE ---",
                diag(round(dyn['lufs_integrated'], 1), profil['lufs'], "LUFS integre (BS.1770)", genre),
                f"LUFS Short-Term: {dyn['lufs_short_term']} LUFS",
                f"True Peak: {dyn['true_peak_db']} dBTP (seuil club/pro: -0.3 dBTP)",
                f"RMS: {dyn['rms_db']} dB | Peak: {dyn['peak_db']} dB",
                f"Crest Factor: {dyn['crest_factor_db']} dB (reference {genre}: {profil['crest']} dB)",
                f"Dynamic Range: {dyn['dynamic_range_db']} dB",
                "",
                "--- BALANCE FREQUENTIELLE ---",
                diag(round(freq['sub_basses_pct'], 1), profil['sub'], "Sub-basses (20-80Hz)", genre, "%"),
                diag(round(freq['basses_pct'], 1), profil['basses'], "Basses (80-250Hz)", genre, "%"),
                diag(round(freq['mids_pct'], 1), profil['mids'], "Mids (250Hz-2kHz)", genre, "%"),
                diag(round(freq['hauts_mids_pct'], 1), profil['hauts_mids'], "Hauts-mids (2-6kHz)", genre, "%"),
                diag(round(freq['aigus_pct'], 1), profil['aigus'], "Aigus (6-20kHz)", genre, "%"),
                f"Centroide spectral: {freq['centroide_hz']} Hz",
                "",
                "--- CHAMP STEREO PAR BANDE ---",
                diag(round(ster['largeur_stereo'], 3), profil['stereo'], "Largeur stereo globale", genre),
                f"Correlation globale L/R: {ster['correlation']} (>0.7=compatible mono, <0.5=risque phase)",
                f"Balance: {ster['balance_lr']} (0=parfaitement centre)",
                f"Sub-basses (20-80Hz): correlation={ster['corr_sub']} → {ster['stereo_sub']} (standard: MONO recommande)",
                f"Basses (80-250Hz): correlation={ster['corr_bass']} → {ster['stereo_bass']} (standard: ETROIT a MONO)",
                f"Mids (250Hz-2kHz): correlation={ster['corr_mids']} → {ster['stereo_mids']} (standard: NORMAL)",
                f"Aigus (2kHz+): correlation={ster['corr_highs']} → {ster['stereo_highs']} (standard: NORMAL a LARGE)",
                "",
                "--- ESPACE & PROFONDEUR ---",
                diag(round(esp['reverb_score'], 2), profil['reverb'], "Reverb", genre),
                f"Densite mix: {esp['densite_mix']} (0=creux / 1=tres dense)",
                "",
                "--- SCORES GLOBAUX ---",
                f"Score global: {scores['global']}%",
                f"Freq: {scores['frequentiel']}% | Dyn: {scores['dynamique']}% | Stereo: {scores['stereo']}% | Rythme: {scores['rythme']}%",
                "",
                "--- BALANCE OVER TIME ---",
                f"Evenements: {json.dumps(bot2['events'])}",
                f"Segments analyses: {len(bot2.get('segments', []))} x 8s",
                "",
                "--- CLIPPING & SATURATION ---",
                f"Severite: {clip['severite']} | Evenements: {clip['count']} | Pourcentage signal sature: {clip['total_pct']}%",
                f"Timestamps: {', '.join([e['ts'] + '(' + e['severity'] + ')' for e in clip['events'][:10]])}",
                "",
                "--- PUNCH DU KICK (80-120Hz sur temps forts) ---",
            ] + _build_punch_lines(donnees.get("punch_kick", {}), genre) + [
                "",
                "--- STRUCTURE & SECTIONS ---",
            ] + _build_sections_lines(donnees.get("sections", {})) + [
                "",
                "--- TONALITÉ & CLÉ MUSICALE ---",
            ] + _build_tonalite_lines(donnees.get("tonalite", {}), donnees) + [
                "",
                "--- SIDECHAIN ---",
                "",
                "--- COMPATIBILITE PLATEFORMES ---",
                f"Spotify/Apple/YouTube: {plateformes['spotify']['verdict']} — {plateformes['spotify']['detail']}",
                f"Beatport/Clubs: {plateformes['beatport']['verdict']} — {plateformes['beatport']['detail']}",
                f"SoundCloud: {plateformes['soundcloud']['verdict']} — {plateformes['soundcloud']['detail']}",
            ]

            if refs_analyse:
                coherence = analyser_coherence_refs(refs_analyse, donnees, genre, profil)
                lines.append("")
                lines.append("--- ANALYSE DE COHÉRENCE DES RÉFÉRENCES ---")
                lines.append(f"Nombre de références : {len(refs_analyse)}")
                lines.append(f"Cohérence globale : {coherence['coherence_globale']} — {coherence['coherence_label']}")

                # Détail de cohérence par dimension
                dim_details = []
                for d, score in coherence['dim_scores'].items():
                    if score < 0.60:
                        dim_details.append(f"{d} (divergente, score={round(score,2)})")
                if dim_details:
                    lines.append(f"Dimensions divergentes entre refs : {' | '.join(dim_details)}")

                # Outlier
                if coherence['outlier_idx'] is not None:
                    lines.append(f"⚠ OUTLIER : Référence {coherence['outlier_idx']+1} très différente ({coherence['outlier_raison']})")
                    lines.append(f"  → Cluster principal = Refs {[i+1 for i in coherence['cluster_indices']]}")
                    lines.append(f"  → IGNORE la Ref {coherence['outlier_idx']+1} dans ta comparaison principale")
                    lines.append(f"  → Tu peux la mentionner brièvement comme 'référence atypique'")
                else:
                    lines.append(f"Pas d'outlier — toutes les références sont cohérentes entre elles")

                # Tendances communes du cluster
                if coherence['tendances']:
                    lines.append(f"ADN sonore commun : {' | '.join(coherence['tendances'])}")
                else:
                    lines.append("Pas de tendances communes marquées entre les références")

                # Écart refs vs genre — analyse de compatibilité
                lines.append(f"Écart moyen refs vs standards {genre} : {coherence['ecart_moyen_genre']}%")
                ecart = coherence['ecart_moyen_genre']
                if ecart > 40:
                    lines.append(f"  ⚠ INCOMPATIBILITÉ STYLISTIQUE FORTE : Les références ciblent un son radicalement différent du genre '{genre}'")
                    lines.append(f"  → Dis clairement au producteur que ses refs et son genre sont très éloignés")
                    lines.append(f"  → Demande-lui (dans le rapport) s'il cherche à fusionner les deux styles ou à choisir l'un d'eux")
                elif ecart > 25:
                    lines.append(f"  → Références dans un style adjacent/différent du genre déclaré — intention artistique hybride probable")
                    lines.append(f"  → Mentionne cet écart et ce que ça implique musicalement")
                elif ecart > 12:
                    lines.append(f"  → Références légèrement en dehors des standards du genre — interprétation créative")
                else:
                    lines.append(f"  → Références très alignées avec les standards du genre — cohérence totale")

                # Détail des écarts par dimension pour le cluster
                cm = coherence['cluster_means']
                lm = dyn.get('lufs_integrated', dyn.get('lufs_approx', 0))
                lines.append("")
                lines.append("--- DONNÉES RÉFÉRENCE PAR RÉFÉRENCE ---")
                for i, rd in enumerate(refs_analyse):
                    rf  = rd["frequentiel"]; rd2 = rd["dynamique"]
                    rs  = rd["stereo"];      rr  = rd["rythme"]
                    re  = rd["espace"]
                    is_outlier = (i == coherence['outlier_idx'])
                    tag = " ⚠ [OUTLIER]" if is_outlier else " ✓ [Cluster]"
                    lines.append(f"=== REF {i+1}{tag} ===")
                    lines.append(f"  BPM={rr['bpm']} | LUFS={rd2.get('lufs_integrated', rd2.get('lufs_approx','?'))} | Peak={rd2.get('true_peak_db','?')}dBTP | Crest={rd2.get('crest_factor_db','?')}dB")
                    lines.append(f"  Freq: Sub={rf['sub_basses_pct']}% Bass={rf['basses_pct']}% Mids={rf['mids_pct']}% HMids={rf['hauts_mids_pct']}% Aigus={rf['aigus_pct']}%")
                    lines.append(f"  Centroide={rf['centroide_hz']}Hz | Stereo={rs['largeur_stereo']} | Sub={rs.get('stereo_sub','?')} | Bass={rs.get('stereo_bass','?')}")
                    lines.append(f"  Reverb={re['reverb_score']} | Densité={re['densite_mix']}")

                lines.append("")
                lines.append("=== ÉCARTS MIX vs CLUSTER PRINCIPAL ===")
                ecart_lufs   = round(lm - cm['lufs'], 1)
                ecart_sub    = round(freq['sub_basses_pct'] - cm['sub'], 1)
                ecart_basses = round(freq['basses_pct'] - cm['basses'], 1)
                ecart_mids   = round(freq['mids_pct'] - cm['mids'], 1)
                ecart_stereo = round(ster['largeur_stereo'] - cm['stereo'], 3)
                ecart_bpm    = round(ryt['bpm'] - cm['bpm'], 1)

                def sens(v): return f"+{v}" if v > 0 else str(v)
                lines.append(f"  LUFS   : mix={round(lm,1)} | refs={cm['lufs']} | {sens(ecart_lufs)}dB {'→ mix plus fort que les refs' if ecart_lufs > 1 else '→ mix plus doux' if ecart_lufs < -1 else '→ aligné'}")
                lines.append(f"  Sub    : mix={freq['sub_basses_pct']}% | refs={cm['sub']}% | {sens(ecart_sub)}% {'→ plus de sub que les refs' if ecart_sub > 2 else '→ moins de sub' if ecart_sub < -2 else '→ aligné'}")
                lines.append(f"  Basses : mix={freq['basses_pct']}% | refs={cm['basses']}% | {sens(ecart_basses)}%")
                lines.append(f"  Mids   : mix={freq['mids_pct']}% | refs={cm['mids']}% | {sens(ecart_mids)}%")
                lines.append(f"  Stéréo : mix={ster['largeur_stereo']} | refs={cm['stereo']} | {sens(ecart_stereo)}")
                lines.append(f"  BPM    : mix={ryt['bpm']} | refs={cm['bpm']} | {sens(ecart_bpm)} BPM {'→ tempos très différents — mentionne-le' if abs(ecart_bpm) > 15 else ''}")
                lines.append("")

                # Instructions spécifiques selon le mode
                if mode == 'reference':
                    lines.append("=== INSTRUCTIONS MODE RÉFÉRENCE PURE ===")
                    lines.append("1. Base-toi PRINCIPALEMENT sur les écarts chiffrés ci-dessus vs le cluster principal")
                    lines.append("2. Les standards du genre sont secondaires — l'objectif c'est de ressembler aux refs")
                    if coherence['outlier_idx'] is not None:
                        lines.append(f"3. Ne cite pas la Ref {coherence['outlier_idx']+1} comme cible — c'est l'outlier")
                    if ecart > 30:
                        lines.append(f"4. IMPORTANT : tes refs et le genre '{genre}' sont très éloignés. Commence le rapport en le signalant")
                    lines.append("5. Chaque conseil doit citer l'écart exact : 'ton sub à X% vs {ref} à Y% — une différence de Z%'")

                elif mode == 'hybride':
                    lines.append("=== PONDÉRATION HYBRIDE DYNAMIQUE ===")
                    lines.append(f"Poids : Références={coherence['poids_refs']}% | Genre={coherence['poids_genre']}%")
                    lines.append(f"Logique : {coherence['ponderation_label']}")
                    if coherence['poids_refs'] >= 65:
                        lines.append("→ Tes conseils s'appuient PRINCIPALEMENT sur les écarts avec les refs")
                        lines.append("→ Les standards du genre servent de contexte secondaire seulement")
                    elif coherence['poids_genre'] >= 65:
                        lines.append("→ Tes conseils s'appuient PRINCIPALEMENT sur les standards du genre")
                        lines.append("→ Les refs confirment ou nuancent, mais ne dictent pas les conseils")
                    else:
                        lines.append("→ Équilibre réel : utilise refs ET genre de manière égale")
                    if coherence['outlier_idx'] is not None:
                        lines.append(f"→ Réf {coherence['outlier_idx']+1} exclue du calcul hybride (outlier)")



            resume = "\n".join(lines)

            # Contexte BPM et niveau
            contexte_bpm = detecter_contexte_bpm(genre, ryt["bpm"], profil)
            niveau_label, niveau_instruction = (
                niveau_prod["label"],
                niveau_prod["instruction"]
            )
            # Enrichir le prompt avec les points forts/faibles détectés
            niveau_detail = (
                f"Score technique automatique : {niveau_prod['score_technique']}%\n"
                f"Points forts détectés : {' | '.join(niveau_prod['points_forts']) if niveau_prod['points_forts'] else 'Aucun détecté'}\n"
                f"Points d'amélioration détectés : {' | '.join(niveau_prod['points_faibles']) if niveau_prod['points_faibles'] else 'Aucun détecté'}"
            )

            refs_genre    = REFS_CULTURELLES.get(genre.lower(), REFS_CULTURELLES["default"])
            ctx_genre     = PROFILS_CONTEXTE.get(genre.lower(), PROFILS_CONTEXTE["default"])

            prompt_lines = [
                f"Tu es un coach bienveillant et encourageant specialise en production musicale.",
                f"Tu parles a un producteur passionne qui a travaille dur sur ce mix. Ton role est de l'aider a progresser, pas de le decourager.",
                f"",
                f"=== MODE D'ANALYSE : {mode.upper()} ===",
                f"Genre selectionne : {genre}" if mode != 'reference' else f"Genre selectionne : {genre} (ATTENTION : en mode reference pure, base-toi principalement sur les ecarts avec les references uploadees, pas sur les standards du genre)",
                f"=== BASE DE DONNEES GENRE : {genre.upper()} ===",
                f"Son caracteristique : {ctx_genre['son']}",
                f"Standard kick : {ctx_genre['kick']}",
                f"Standard bass : {ctx_genre['bass']}",
                f"Standard stereo : {ctx_genre['stereo']}",
                f"Compression typique : {ctx_genre['compression']}",
                f"Reverb typique : {ctx_genre['reverb']}",
                f"EQ master : {ctx_genre['eq_master']}",
                f"Erreurs courantes dans ce genre : {' | '.join(ctx_genre['erreurs']) if isinstance(ctx_genre['erreurs'], list) else ctx_genre['erreurs']}",
                f"Contexte de diffusion : {ctx_genre['contexte']}",
                f"Arrangement typique : {ctx_genre['arrangement']}",
                f"Standards mastering : {ctx_genre['mastering']}",
                f"Plateformes prioritaires : {ctx_genre['plateformes']}",
                f"References artistes : {refs_genre}",
                f"",
                f"Voici l'analyse complete de son mix :",
                resume,
                "",
                f"=== CONTEXTE BPM ===",
                contexte_bpm,
                "",
                f"=== NIVEAU DU PRODUCTEUR : {niveau_label} ===",
                niveau_instruction,
                niveau_detail,
                "",
                "REGLES DE TON - ABSOLUMENT OBLIGATOIRES :",
                "- Tu es un MENTOR, pas un juge. Chaque probleme est une opportunite de progresser.",
                "- JAMAIS de mots comme catastrophique, dramatique, critique, desastre, manque cruel, terrible, flagrant.",
                "- Reformule TOUJOURS positivement : pas 'manque de graves' mais 'en ajoutant du corps dans les graves tu vas...'",
                "- Commence chaque point d'amelioration par ce que ca va apporter, pas par le probleme.",
                "- Garde un ton chaleureux et motivant tout au long du rapport.",
                "",
                "REGLES DE PRECISION - OBLIGATOIRES :",
                "1. Cite les valeurs exactes mesurees (ex: ton LUFS integre de -11.2 est 2.2 dB au-dessus de la reference)",
                "2. Donne des corrections chiffrees et precises (ex: un boost de +4dB autour de 80Hz va apporter...)",
                "3. Pour le STEREO PAR BANDE : les sub-basses doivent etre MONO (corr > 0.85), les basses ETROIT (corr 0.7-0.9), les mids peuvent etre plus larges, les aigus encore plus larges. Commente chaque bande.",
                "4. Pour le TRUE PEAK : si > -0.3 dBTP, risque de saturation sur les systemes club et certains encodeurs — mentionne-le avec la valeur exacte",
                "5. Si des references ont ete fournies, compare les valeurs precisement",
                f"6. Utilise les references culturelles du genre {genre} fournies ci-dessus pour ancrer tes conseils dans la scene",
                "7. Mentionne le BPM detecte et son contexte dans le genre",
                "",
                "Structure OBLIGATOIRE (respecte exactement ces titres) :",
                "## Resume",
                "## Ce qui fonctionne bien",
                "## Pour aller plus loin",
                "## Tes 3 priorites",
                "## Pret pour le streaming ?",
                "## Synthese",
                "",
                "Pour la section ## Synthese : 3-4 phrases inspirantes qui resument le potentiel du mix, ce que le producteur a deja reussi, et vers quoi il se dirige. Termine sur une note positive et motivante. Pas de chiffres dans cette section, que de l'humain.",
            ]
            prompt = "\n".join(prompt_lines)

            import re
            buffer = ""
            with client.messages.stream(
                model="claude-sonnet-4-5",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}]
            ) as stream:
                for text in stream.text_stream:
                    buffer += text
                    lines_buf = buffer.split("\n")
                    for line in lines_buf[:-1]:
                        if line.startswith("## "):
                            yield "<h2>" + line[3:] + "</h2>"
                        elif line.startswith("### "):
                            yield "<h3>" + line[4:] + "</h3>"
                        elif line.startswith("- ") or line.startswith("* "):
                            clean = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', line[2:])
                            yield "<li>" + clean + "</li>"
                        elif line.strip():
                            clean = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', line)
                            yield "<p>" + clean + "</p>"
                    buffer = lines_buf[-1]

            if buffer.strip():
                clean = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', buffer)
                yield "<p>" + clean + "</p>"

            yield ressources_html + '</div>'
            yield '<p style="text-align:center;font-size:11px;font-style:italic;color:rgba(136,136,170,0.45);padding:40px 20px 24px;line-height:1.8">J\'ai conçu cet outil avec passion dans une démarche purement technique. Le principal reste de s\'amuser et de rester créatif. Loïc</p><button class="btn-back" onclick="location.reload()">Analyser un autre mix</button>'

        except Exception as e:
            if os.path.exists(chemin): os.remove(chemin)
            for r in refs:
                if os.path.exists(r): os.remove(r)
            yield "<p>Erreur: " + str(e) + "</p>"

    return Response(stream_with_context(generate()), mimetype='text/html')

HOW_IT_WORKS_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>InsideYourMix — Comment ca marche</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237B2FFF'/%3E%3Crect x='5' y='18' width='3' height='9' rx='1.5' fill='white'/%3E%3Crect x='10' y='12' width='3' height='15' rx='1.5' fill='%2300E5FF'/%3E%3Crect x='15' y='8' width='3' height='19' rx='1.5' fill='white'/%3E%3Crect x='20' y='14' width='3' height='13' rx='1.5' fill='%2300FF88'/%3E%3Crect x='25' y='10' width='3' height='17' rx='1.5' fill='white'/%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Sans:wght@300;400;500&family=Space+Grotesk:wght@700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--v:#7B2FFF;--c:#00E5FF;--g:#00FF88;--n:#07070F;--n2:#0F0F1A;--w:#F0F0F8;--gr:#8888AA}
body{background:var(--n);color:var(--w);font-family:'DM Sans',sans-serif;min-height:100vh;overflow-x:hidden}
.bg-blob-1{position:fixed;top:-25%;left:-10%;width:75%;height:75%;background:radial-gradient(ellipse,rgba(123,47,255,0.22) 0%,transparent 60%);pointer-events:none;z-index:0;filter:blur(48px);animation:blob1 16s ease-in-out infinite}
.bg-blob-2{position:fixed;bottom:-20%;right:-8%;width:65%;height:65%;background:radial-gradient(ellipse,rgba(0,229,255,0.16) 0%,transparent 58%);pointer-events:none;z-index:0;filter:blur(55px);animation:blob2 21s ease-in-out infinite}
.bg-blob-3{position:fixed;top:35%;left:25%;width:50%;height:50%;background:radial-gradient(ellipse,rgba(0,255,136,0.09) 0%,transparent 65%);pointer-events:none;z-index:0;filter:blur(70px);animation:blob3 13s ease-in-out infinite}
.bg-blob-4{position:fixed;top:10%;right:15%;width:35%;height:35%;background:radial-gradient(ellipse,rgba(123,47,255,0.12) 0%,transparent 60%);pointer-events:none;z-index:0;filter:blur(40px);animation:blob4 18s ease-in-out infinite reverse}
.bg-beam{position:fixed;top:-40%;left:35%;width:18%;height:180%;background:linear-gradient(180deg,transparent 0%,rgba(123,47,255,0.05) 25%,rgba(0,229,255,0.07) 50%,rgba(0,255,136,0.04) 75%,transparent 100%);transform-origin:top center;pointer-events:none;z-index:0;filter:blur(28px);animation:beam 28s ease-in-out infinite}
.bg-grid{position:fixed;inset:0;z-index:0;pointer-events:none;background-image:linear-gradient(rgba(123,47,255,0.03) 1px,transparent 1px),linear-gradient(90deg,rgba(123,47,255,0.03) 1px,transparent 1px);background-size:64px 64px;-webkit-mask-image:radial-gradient(ellipse 85% 85% at 50% 50%,transparent 25%,black 100%);mask-image:radial-gradient(ellipse 85% 85% at 50% 50%,transparent 25%,black 100%)}
@keyframes blob1{0%,100%{transform:translate(0,0) scale(1);opacity:.8}25%{transform:translate(7%,-9%) scale(1.18);opacity:1}55%{transform:translate(3%,7%) scale(.88);opacity:.6}75%{transform:translate(-5%,2%) scale(1.08);opacity:.9}}
@keyframes blob2{0%,100%{transform:translate(0,0) scale(1);opacity:.7}33%{transform:translate(-8%,6%) scale(1.14);opacity:1}66%{transform:translate(6%,-7%) scale(.82);opacity:.5}}
@keyframes blob3{0%,100%{transform:translate(0,0) scale(1);opacity:.5}30%{transform:translate(-12%,-10%) scale(1.35);opacity:.9}60%{transform:translate(8%,5%) scale(.75);opacity:.4}}
@keyframes blob4{0%,100%{transform:translate(0,0) scale(1);opacity:.6}40%{transform:translate(10%,8%) scale(1.2);opacity:.9}70%{transform:translate(-5%,-6%) scale(.85);opacity:.5}}
@keyframes beam{0%,100%{left:15%;opacity:.35;transform:rotate(-22deg) scaleX(1)}20%{left:45%;opacity:.7;transform:rotate(-4deg) scaleX(1.3)}45%{left:65%;opacity:.45;transform:rotate(12deg) scaleX(.8)}70%{left:30%;opacity:.85;transform:rotate(-16deg) scaleX(1.15)}}
.nav{position:fixed;top:0;left:0;right:0;padding:20px 48px;display:flex;justify-content:space-between;align-items:center;z-index:100;background:rgba(7,7,15,0.6);backdrop-filter:blur(24px);border-bottom:1px solid rgba(255,255,255,0.05)}
.logo{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:20px;background:linear-gradient(90deg,#F0F0F8 0%,#7B2FFF 50%,#00E5FF 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;letter-spacing:-0.03em;text-decoration:none}
.nav-right{display:flex;gap:28px;align-items:center}
.nav-cta{background:linear-gradient(135deg,var(--v),#5020CC);color:white;padding:10px 24px;border-radius:24px;text-decoration:none;font-weight:600;font-size:14px;transition:all .2s;box-shadow:0 4px 20px rgba(123,47,255,.3)}
.nav-cta:hover{transform:translateY(-1px);box-shadow:0 8px 30px rgba(123,47,255,.5)}
.dropdown{position:relative}
.menu-btn{background:none;border:1px solid rgba(255,255,255,.18);border-radius:8px;padding:8px 12px;cursor:pointer;display:flex;flex-direction:column;gap:5px;transition:border-color .2s}
.menu-btn:hover{border-color:rgba(123,47,255,.5)}
.menu-btn span{display:block;width:22px;height:2px;background:#F0F0F8;border-radius:2px;transition:all .3s}
.menu-btn.open span:nth-child(1){transform:translateY(7px) rotate(45deg)}
.menu-btn.open span:nth-child(2){opacity:0;transform:scaleX(0)}
.menu-btn.open span:nth-child(3){transform:translateY(-7px) rotate(-45deg)}
.dropdown-menu{position:absolute;top:52px;right:0;background:rgba(12,12,22,.97);backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,.1);border-radius:18px;padding:12px;min-width:240px;display:none;flex-direction:column;gap:3px;z-index:1000;box-shadow:0 24px 60px rgba(0,0,0,.6)}
.dropdown-menu.open{display:flex}
.dropdown-item{color:#F0F0F8;text-decoration:none;padding:12px 16px;border-radius:10px;font-size:15px;font-weight:500;transition:background .2s}
.dropdown-item:hover{background:rgba(123,47,255,.2)}
.dropdown-divider{height:1px;background:rgba(255,255,255,.07);margin:8px 0}
.lang-selector{display:flex;gap:8px;padding:8px 16px;justify-content:center}
.lang-flag{font-size:22px;cursor:pointer;opacity:.65;transition:all .2s;border-radius:4px;padding:4px}
.lang-flag:hover{opacity:1;transform:scale(1.2)}
.page-wrap{position:relative;z-index:1}
.hero{text-align:center;padding:100px 20px 40px;max-width:700px;margin:0 auto}
.hero-label{font-size:11px;letter-spacing:3px;text-transform:uppercase;color:var(--v);margin-bottom:12px;font-family:'Syne',sans-serif}
.hero h1{font-family:'Space Grotesk',sans-serif;font-size:clamp(32px,5vw,52px);font-weight:800;letter-spacing:-0.03em;margin-bottom:16px;background:linear-gradient(135deg,var(--v),var(--c));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.hero p{color:var(--gr);font-size:16px;line-height:1.6}
.main{max-width:960px;margin:0 auto;padding:0 20px 100px}
.section-label{font-size:11px;letter-spacing:3px;text-transform:uppercase;color:var(--v);margin-bottom:16px;font-family:'Syne',sans-serif;text-align:center}
.flow{display:flex;align-items:center;justify-content:center;gap:8px;margin-bottom:60px;flex-wrap:wrap}
.step{background:var(--n2);border:1px solid rgba(255,255,255,0.06);border-radius:14px;padding:18px 22px;text-align:center;flex:1;min-width:130px;max-width:180px;transition:all 0.3s}
.step:hover{border-color:rgba(123,47,255,0.4);transform:translateY(-2px)}
.step-num{font-size:10px;letter-spacing:2px;color:var(--v);font-weight:600;margin-bottom:6px;font-family:'Syne',sans-serif}
.step-label{font-size:14px;font-weight:600;color:var(--w);font-family:'Syne',sans-serif;margin-bottom:3px}
.step-sub{font-size:11px;color:var(--gr)}
.arrow{color:var(--v);font-size:20px;flex-shrink:0}
.dims-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:30px}
.dim-card{background:var(--n2);border:1px solid rgba(255,255,255,0.06);border-radius:14px;padding:20px;cursor:pointer;transition:all 0.2s}
.dim-card:hover{border-color:rgba(123,47,255,0.4);transform:translateY(-2px)}
.detail-box{background:var(--n2);border:1px solid rgba(123,47,255,0.3);border-radius:16px;padding:28px;margin-bottom:50px;display:none}
.detail-title{font-size:18px;font-weight:700;font-family:'Space Grotesk',sans-serif;margin-bottom:6px}
.detail-desc{color:var(--gr);font-size:14px;line-height:1.7;margin-bottom:20px}
.detail-metrics-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
.report-flow{display:flex;flex-direction:column;gap:10px;margin-bottom:50px}
.report-step{display:flex;align-items:flex-start;gap:14px;padding:18px;background:var(--n2);border:1px solid rgba(255,255,255,0.06);border-radius:12px}
.rs-dot{width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0;font-family:'Syne',sans-serif}
.rs-title{font-size:14px;font-weight:600;color:var(--w);font-family:'Syne',sans-serif;margin-bottom:4px}
.rs-desc{font-size:13px;color:var(--gr);line-height:1.6}
.cta{text-align:center;padding:40px 20px}
.cta-btn{display:inline-block;padding:18px 40px;background:linear-gradient(135deg,var(--v),#5020CC);border:none;border-radius:14px;color:white;font-family:'Space Grotesk',sans-serif;font-size:16px;font-weight:700;cursor:pointer;letter-spacing:1px;text-decoration:none;transition:all .2s}
.cta-btn:hover{transform:translateY(-2px);box-shadow:0 10px 40px rgba(123,47,255,.4)}
@media(max-width:640px){.nav{padding:14px 20px}.hero{padding:80px 16px 30px}}
</style>
</head>
<body>
<div class="bg-blob-1"></div>
<div class="bg-blob-2"></div>
<div class="bg-blob-3"></div>
<div class="bg-blob-4"></div>
<div class="bg-beam"></div>
<div class="bg-grid"></div>
<nav class="nav">
<a href="/" class="logo">InsideYourMix</a>
<div class="nav-right">
<div class="dropdown">
<button class="menu-btn" id="menuBtn" onclick="toggleMenu()"><span></span><span></span><span></span></button>
<div class="dropdown-menu" id="dropdownMenu">
<a href="/how-it-works" class="dropdown-item">✦ How it works</a>
<a href="/why" class="dropdown-item">✦ Why InsideYourMix</a>
<a href="/abonnements" class="dropdown-item">✦ Abonnements</a>
<a href="/contact" class="dropdown-item">✦ Contact</a>
<div class="dropdown-divider"></div>
<a href="/login" class="dropdown-item" style="color:#00E5FF">→ Se connecter</a>
<a href="/register" class="dropdown-item" style="color:#00FF88;font-weight:700">✦ Creer un compte</a>
<div class="dropdown-divider"></div>
<div class="lang-selector">
<span onclick="setLang('fr')" class="lang-flag">🇫🇷</span>
<span onclick="setLang('en')" class="lang-flag">🇬🇧</span>
<span onclick="setLang('es')" class="lang-flag">🇪🇸</span>
<span onclick="setLang('de')" class="lang-flag">🇩🇪</span>
<span onclick="setLang('pt')" class="lang-flag">🇵🇹</span>
</div>
</div>
</div>
<a href="/analyze" class="nav-cta">Analyser →</a>
</div>
</nav>
<div class="page-wrap">
<div class="hero">
  <div class="hero-label">Comment ca marche</div>
  <h1>L'IA decortique ton mix</h1>
  <p>Ton morceau analyse en profondeur sur 7 dimensions techniques. Notre coach IA traduit les donnees en conseils concrets et actionnables.</p>
</div>
<div class="main">
  <div class="section-label">Le parcours en 4 etapes</div>
  <div class="flow">
    <div class="step"><div class="step-num">01</div><div class="step-label">Upload</div><div class="step-sub">MP3 - WAV - FLAC</div></div>
    <div class="arrow">→</div>
    <div class="step"><div class="step-num">02</div><div class="step-label">Analyse</div><div class="step-sub">7 dimensions</div></div>
    <div class="arrow">→</div>
    <div class="step"><div class="step-num">03</div><div class="step-label">Coach personnalise</div><div class="step-sub">IA analyse</div></div>
    <div class="arrow">→</div>
    <div class="step" style="border-color:rgba(123,47,255,0.4);background:rgba(123,47,255,0.08)"><div class="step-num">04</div><div class="step-label">Rapport</div><div class="step-sub">Actions concretes</div></div>
  </div>
  <div class="section-label">Les 7 dimensions analysees — clique pour explorer</div>
  <div class="dims-grid" id="dimGrid"></div>
  <div class="detail-box" id="detailBox">
    <div class="detail-title" id="dTitle"></div>
    <div class="detail-desc" id="dDesc"></div>
    <div class="detail-metrics-grid" id="dMetrics"></div>
  </div>
  <div class="section-label">Ce que genere ton rapport</div>
  <div class="report-flow">
    <div class="report-step"><div class="rs-dot" style="background:rgba(123,47,255,0.2);color:#7B2FFF">1</div><div><div class="rs-title">Resume global</div><div class="rs-desc">2-3 phrases positives sur le travail et le potentiel du mix</div></div></div>
    <div class="report-step"><div class="rs-dot" style="background:rgba(0,229,255,0.15);color:#00E5FF">2</div><div><div class="rs-title">Ce qui fonctionne bien</div><div class="rs-desc">Points forts concrets avec les valeurs techniques mesurees</div></div></div>
    <div class="report-step"><div class="rs-dot" style="background:rgba(0,255,136,0.15);color:#00FF88">3</div><div><div class="rs-title">Pour aller plus loin</div><div class="rs-desc">Opportunites de progression positivement formulees avec valeurs cibles</div></div></div>
    <div class="report-step"><div class="rs-dot" style="background:rgba(255,140,0,0.15);color:#FF8C00">4</div><div><div class="rs-title">Tes 3 priorites</div><div class="rs-desc">Actions concretes et immediates, du plus impactant au moins impactant</div></div></div>
    <div class="report-step"><div class="rs-dot" style="background:rgba(123,47,255,0.2);color:#7B2FFF">5</div><div><div class="rs-title">Pret pour le streaming ?</div><div class="rs-desc">Verdict Spotify et Beatport avec ajustements precis en dB</div></div></div>
    <div class="report-step"><div class="rs-dot" style="background:rgba(0,255,136,0.15);color:#00FF88">6</div><div><div class="rs-title">Synthese</div><div class="rs-desc">3-4 phrases inspirantes sur le potentiel du mix et la progression</div></div></div>
  </div>
  <div class="cta"><a href="/analyze" class="cta-btn">Analyser mon mix</a></div>
</div>
</div>
<script>
function toggleMenu(){
  var m=document.getElementById('dropdownMenu');
  var b=document.getElementById('menuBtn');
  m.classList.toggle('open');
  b.classList.toggle('open');
}
document.addEventListener('click',function(e){
  if(!e.target.closest('.dropdown')){
    document.getElementById('dropdownMenu').classList.remove('open');
    document.getElementById('menuBtn').classList.remove('open');
  }
});
function setLang(l){alert('Langue '+l+' - bientot disponible !');}
const dims=[
  {num:"01",name:"Analyse Frequentielle",color:"#7B2FFF",bg:"rgba(123,47,255,0.15)",
   desc:"Decompose ton mix en 5 bandes frequentielles (20Hz-20kHz). Revele l'equilibre spectral et compare aux standards de ton genre.",
   examples:[{label:"Sub-basses",val:"17%",desc:"20-80Hz - grave profond"},{label:"Basses",val:"32%",desc:"80-250Hz - corps du mix"},{label:"Mids",val:"29%",desc:"250Hz-2kHz - presence"},{label:"Hauts-mids",val:"14%",desc:"2-6kHz - brillance"},{label:"Aigus",val:"8%",desc:"6-20kHz - air"},{label:"Centroide",val:"3354 Hz",desc:"Brillance globale"}]},
  {num:"02",name:"Dynamique & Loudness",color:"#00E5FF",bg:"rgba(0,229,255,0.12)",
   desc:"True LUFS integre BS.1770-4, Short-Term LUFS, True Peak inter-sample. Compare aux standards Spotify (-14) et Beatport (-9).",
   examples:[{label:"LUFS integre",val:"-8.4",desc:"Volume percu reel BS.1770"},{label:"True Peak",val:"-0.3 dBTP",desc:"Crete inter-sample"},{label:"RMS",val:"-7.7 dB",desc:"Niveau moyen"},{label:"Crest Factor",val:"7.4 dB",desc:"Punch"},{label:"Dynamic Range",val:"7.6 dB",desc:"Respiration"}]},
  {num:"03",name:"Stereo par bande",color:"#00FF88",bg:"rgba(0,255,136,0.12)",
   desc:"Correlation L/R sur 4 bandes de frequences : sub, basses, mids, aigus. Detecte les problemes de phase par frequence.",
   examples:[{label:"Corr. Sub",val:"0.97",desc:"MONO — standard industrie"},{label:"Corr. Basses",val:"0.82",desc:"ETROIT — normal"},{label:"Corr. Mids",val:"0.54",desc:"NORMAL — bon"},{label:"Corr. Aigus",val:"0.21",desc:"LARGE — air et espace"}]},
  {num:"04",name:"Rythme & Tempo",color:"#FF8C00",bg:"rgba(255,140,0,0.12)",
   desc:"Detecte le BPM par autocorrelation avec contexte de sous-genre. Mesure la puissance rythmique et la regularite.",
   examples:[{label:"BPM",val:"129.2",desc:"Tempo detecte"},{label:"Onset strength",val:"37.27",desc:"Puissance attaques"},{label:"Regularite",val:"-0.34",desc:"Stabilite groove"}]},
  {num:"05",name:"Timbre & Texture",color:"#FF4488",bg:"rgba(255,68,136,0.12)",
   desc:"Coefficients spectraux capturent la couleur sonore. La flatness mesure le rapport bruit/tonal.",
   examples:[{label:"Flatness",val:"0.10",desc:"0=tonal / 1=bruit blanc"}]},
  {num:"06",name:"Espace & Profondeur",color:"#7B2FFF",bg:"rgba(123,47,255,0.15)",
   desc:"Estime la reverberation et la densite spectrale. Revele la profondeur percue et la plenitude du mix.",
   examples:[{label:"Reverb",val:"0.641",desc:"Presence reverb (0-1)"},{label:"Densite",val:"0.829",desc:"Plenitude spectrale"}]},
  {num:"07",name:"Balance over Time",color:"#00E5FF",bg:"rgba(0,229,255,0.12)",
   desc:"Decoupe le morceau en segments de 8s et analyse l'evolution. Detecte automatiquement drops et breakdowns.",
   examples:[{label:"Segment 0-8s",val:"-7.3 dB",desc:"Graves 49% Mids 38%"},{label:"Drop",val:"+5.4 dB",desc:"A 120s"},{label:"Breakdown",val:"-4.6 dB",desc:"A 112s"}]}
];
var grid=document.getElementById('dimGrid');
var detailBox=document.getElementById('detailBox');
var dTitle=document.getElementById('dTitle');
var dDesc=document.getElementById('dDesc');
var dMetrics=document.getElementById('dMetrics');
dims.forEach(function(d){
  var card=document.createElement('div');
  card.className='dim-card';
  card.style.cssText='background:'+d.bg+';border:1px solid '+d.color+'40;border-radius:16px;padding:20px;cursor:pointer;transition:all 0.3s';
  card.innerHTML='<div style="font-size:11px;color:'+d.color+';font-weight:700;letter-spacing:.1em;margin-bottom:8px">'+d.num+'</div><div style="font-size:15px;font-weight:600;color:#F0F0F8">'+d.name+'</div>';
  card.addEventListener('click',function(){
    dTitle.textContent=d.num+' — '+d.name;
    dTitle.style.color=d.color;
    dDesc.textContent=d.desc;
    dMetrics.innerHTML=d.examples.map(function(e){
      return '<div style="background:rgba(255,255,255,0.04);border-radius:10px;padding:12px 16px;border:1px solid rgba(255,255,255,0.06)">'
        +'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">'
        +'<span style="font-size:13px;color:#F0F0F8;font-weight:500">'+e.label+'</span>'
        +'<span style="font-size:14px;font-weight:700;color:'+d.color+'">'+e.val+'</span></div>'
        +'<div style="font-size:11px;color:#8888AA">'+e.desc+'</div></div>';
    }).join('');
    detailBox.style.display='block';
    detailBox.style.borderColor=d.color+'66';
    detailBox.scrollIntoView({behavior:'smooth',block:'nearest'});
  });
  grid.appendChild(card);
});
</script>

<div id="portalOverlay"><canvas id="portalCanvas"></canvas></div>
<script>
(function(){
var overlay=document.getElementById('portalOverlay');
var canvas=document.getElementById('portalCanvas');
var ctx=canvas.getContext('2d');
var W,H,cx,cy,rings=[],beams=[],particles=[];
var animId=null,startTime=0,navigateTo=null;
var COLORS=['#7B2FFF','#00E5FF','#00FF88','#B44FFF','#00FFCC'];
var DURATION=820;

function resize(){W=canvas.width=window.innerWidth;H=canvas.height=window.innerHeight;}
window.addEventListener('resize',resize);resize();

function initPortal(ox,oy){
  cx=ox||W/2; cy=oy||H/2;
  rings=[]; beams=[]; particles=[];
  // Ondes sonores concentriques
  for(var i=0;i<8;i++){
    rings.push({delay:i*60,r:0,maxR:Math.sqrt(W*W+H*H),col:COLORS[i%COLORS.length],w:2+i*.5});
  }
  // Faisceaux convergents vers le centre (vortex)
  for(var i=0;i<32;i++){
    var angle=i/32*Math.PI*2;
    var dist=Math.sqrt(W*W+H*H)*.6;
    beams.push({angle:angle,dist:dist,col:COLORS[i%COLORS.length],w:.5+Math.random()*1.5,delay:i*18});
  }
  // Particules explosives
  for(var i=0;i<60;i++){
    var angle=Math.random()*Math.PI*2;
    var spd=2+Math.random()*6;
    particles.push({x:cx,y:cy,vx:Math.cos(angle)*spd,vy:Math.sin(angle)*spd,
      r:2+Math.random()*3,life:1,col:COLORS[Math.floor(Math.random()*COLORS.length)]});
  }
}

function drawFrame(t){
  var p=Math.min(t/DURATION,1);
  var ease=p<.5?2*p*p:1-Math.pow(-2*p+2,2)/2;

  ctx.clearRect(0,0,W,H);

  // Fond qui assombrit progressivement
  ctx.fillStyle='rgba(7,7,15,'+(ease*.92)+')';
  ctx.fillRect(0,0,W,H);

  // Faisceaux vortex convergeant vers cx,cy
  beams.forEach(function(b,i){
    var bt=Math.max(0,(t-b.delay)/DURATION);
    if(bt<=0)return;
    var alpha=Math.min(bt*3,.85)*(1-ease*.4);
    var startDist=b.dist*(1-Math.min(bt*2,1));
    var sx=cx+Math.cos(b.angle)*b.dist;
    var sy=cy+Math.sin(b.angle)*b.dist;
    var ex=cx+Math.cos(b.angle)*startDist;
    var ey=cy+Math.sin(b.angle)*startDist;
    var grad=ctx.createLinearGradient(sx,sy,ex,ey);
    grad.addColorStop(0,b.col+'00');
    grad.addColorStop(.6,b.col+Math.floor(alpha*180).toString(16).padStart(2,'0'));
    grad.addColorStop(1,b.col+Math.floor(alpha*255).toString(16).padStart(2,'0'));
    ctx.beginPath();ctx.moveTo(sx,sy);ctx.lineTo(ex,ey);
    ctx.strokeStyle=grad;ctx.lineWidth=b.w*(1+ease*2);ctx.stroke();
  });

  // Ondes sonores concentriques depuis cx,cy
  rings.forEach(function(r){
    var rt=Math.max(0,(t-r.delay)/DURATION);
    if(rt<=0)return;
    var alpha=Math.min(rt*4,1)*(1-rt*.7);
    if(alpha<.01)return;
    var radius=r.maxR*rt*.8;
    ctx.beginPath();
    ctx.arc(cx,cy,radius,0,Math.PI*2);
    ctx.strokeStyle=r.col+Math.floor(alpha*220).toString(16).padStart(2,'0');
    ctx.lineWidth=r.w*(1+ease*1.5);
    ctx.stroke();
    // Second anneau plus fin légèrement en retard
    if(radius>30){
      ctx.beginPath();
      ctx.arc(cx,cy,radius*.88,0,Math.PI*2);
      ctx.strokeStyle=r.col+Math.floor(alpha*80).toString(16).padStart(2,'0');
      ctx.lineWidth=r.w*.4;ctx.stroke();
    }
  });

  // Particules
  particles.forEach(function(p2){
    p2.x+=p2.vx*(1+ease*3);p2.y+=p2.vy*(1+ease*3);
    p2.life-=.018;
    if(p2.life<0)return;
    ctx.beginPath();ctx.arc(p2.x,p2.y,p2.r*p2.life,0,Math.PI*2);
    ctx.fillStyle=p2.col+Math.floor(p2.life*180).toString(16).padStart(2,'0');
    ctx.fill();
  });

  // Flash blanc central au moment du pic
  if(p>.55&&p<.75){
    var flashP=(p-.55)/.2;
    var flashAlpha=flashP<.5?flashP*2:(1-flashP)*2;
    var flashR=Math.min(flashAlpha*120,120);
    var grd=ctx.createRadialGradient(cx,cy,0,cx,cy,flashR*8);
    grd.addColorStop(0,'rgba(180,100,255,'+(flashAlpha*.6)+')');
    grd.addColorStop(.3,'rgba(0,229,255,'+(flashAlpha*.3)+')');
    grd.addColorStop(1,'rgba(0,255,136,0)');
    ctx.fillStyle=grd;
    ctx.fillRect(0,0,W,H);
  }

  // Grille scanlines finales
  if(p>.4){
    var scanAlpha=(p-.4)/.6*.12;
    for(var y=0;y<H;y+=4){
      ctx.fillStyle='rgba(0,0,0,'+scanAlpha+')';
      ctx.fillRect(0,y,W,1);
    }
  }
}

function animate(ts){
  var t=ts-startTime;
  drawFrame(t);
  if(t<DURATION){
    animId=requestAnimationFrame(animate);
  } else {
    ctx.fillStyle='#07070F';ctx.fillRect(0,0,W,H);
    if(navigateTo) window.location.href=navigateTo;
  }
}

window._portalGo=function(href,ox,oy){
  if(animId)return;
  navigateTo=href;
  overlay.style.opacity='1';
  overlay.classList.add('active');
  initPortal(ox,oy);
  startTime=performance.now();
  animId=requestAnimationFrame(animate);
};

// Intercepter tous les liens internes (sauf ancres et externes)
document.addEventListener('click',function(e){
  var a=e.target.closest('a[href]');
  if(!a)return;
  var href=a.getAttribute('href');
  if(!href||href.startsWith('http')||href.startsWith('mailto')||href.startsWith('#')||href.startsWith('javascript'))return;
  e.preventDefault();
  var rect=a.getBoundingClientRect();
  var ox=rect.left+rect.width/2, oy=rect.top+rect.height/2;
  if(window._fadeTo){window._fadeTo(href);}else{window.location.href=href;}
});

// Apparition de la page courante en fondu entrant
document.documentElement.style.opacity='0';
document.documentElement.style.transition='opacity .35s ease';
requestAnimationFrame(function(){requestAnimationFrame(function(){document.documentElement.style.opacity='1';});});
})();
</script>


<div id="portalOverlay"><canvas id="portalCanvas"></canvas></div>
<script>
(function(){
var overlay=document.getElementById('portalOverlay');
var canvas=document.getElementById('portalCanvas');
var ctx=canvas.getContext('2d');
var W,H,cx,cy,rings=[],beams=[],particles=[];
var animId=null,startTime=0,navigateTo=null;
var COLORS=['#7B2FFF','#00E5FF','#00FF88','#B44FFF','#00FFCC'];
var DURATION=820;

function resize(){W=canvas.width=window.innerWidth;H=canvas.height=window.innerHeight;}
window.addEventListener('resize',resize);resize();

function initPortal(ox,oy){
  cx=ox||W/2; cy=oy||H/2;
  rings=[]; beams=[]; particles=[];
  // Ondes sonores concentriques
  for(var i=0;i<8;i++){
    rings.push({delay:i*60,r:0,maxR:Math.sqrt(W*W+H*H),col:COLORS[i%COLORS.length],w:2+i*.5});
  }
  // Faisceaux convergents vers le centre (vortex)
  for(var i=0;i<32;i++){
    var angle=i/32*Math.PI*2;
    var dist=Math.sqrt(W*W+H*H)*.6;
    beams.push({angle:angle,dist:dist,col:COLORS[i%COLORS.length],w:.5+Math.random()*1.5,delay:i*18});
  }
  // Particules explosives
  for(var i=0;i<60;i++){
    var angle=Math.random()*Math.PI*2;
    var spd=2+Math.random()*6;
    particles.push({x:cx,y:cy,vx:Math.cos(angle)*spd,vy:Math.sin(angle)*spd,
      r:2+Math.random()*3,life:1,col:COLORS[Math.floor(Math.random()*COLORS.length)]});
  }
}

function drawFrame(t){
  var p=Math.min(t/DURATION,1);
  var ease=p<.5?2*p*p:1-Math.pow(-2*p+2,2)/2;

  ctx.clearRect(0,0,W,H);

  // Fond qui assombrit progressivement
  ctx.fillStyle='rgba(7,7,15,'+(ease*.92)+')';
  ctx.fillRect(0,0,W,H);

  // Faisceaux vortex convergeant vers cx,cy
  beams.forEach(function(b,i){
    var bt=Math.max(0,(t-b.delay)/DURATION);
    if(bt<=0)return;
    var alpha=Math.min(bt*3,.85)*(1-ease*.4);
    var startDist=b.dist*(1-Math.min(bt*2,1));
    var sx=cx+Math.cos(b.angle)*b.dist;
    var sy=cy+Math.sin(b.angle)*b.dist;
    var ex=cx+Math.cos(b.angle)*startDist;
    var ey=cy+Math.sin(b.angle)*startDist;
    var grad=ctx.createLinearGradient(sx,sy,ex,ey);
    grad.addColorStop(0,b.col+'00');
    grad.addColorStop(.6,b.col+Math.floor(alpha*180).toString(16).padStart(2,'0'));
    grad.addColorStop(1,b.col+Math.floor(alpha*255).toString(16).padStart(2,'0'));
    ctx.beginPath();ctx.moveTo(sx,sy);ctx.lineTo(ex,ey);
    ctx.strokeStyle=grad;ctx.lineWidth=b.w*(1+ease*2);ctx.stroke();
  });

  // Ondes sonores concentriques depuis cx,cy
  rings.forEach(function(r){
    var rt=Math.max(0,(t-r.delay)/DURATION);
    if(rt<=0)return;
    var alpha=Math.min(rt*4,1)*(1-rt*.7);
    if(alpha<.01)return;
    var radius=r.maxR*rt*.8;
    ctx.beginPath();
    ctx.arc(cx,cy,radius,0,Math.PI*2);
    ctx.strokeStyle=r.col+Math.floor(alpha*220).toString(16).padStart(2,'0');
    ctx.lineWidth=r.w*(1+ease*1.5);
    ctx.stroke();
    // Second anneau plus fin légèrement en retard
    if(radius>30){
      ctx.beginPath();
      ctx.arc(cx,cy,radius*.88,0,Math.PI*2);
      ctx.strokeStyle=r.col+Math.floor(alpha*80).toString(16).padStart(2,'0');
      ctx.lineWidth=r.w*.4;ctx.stroke();
    }
  });

  // Particules
  particles.forEach(function(p2){
    p2.x+=p2.vx*(1+ease*3);p2.y+=p2.vy*(1+ease*3);
    p2.life-=.018;
    if(p2.life<0)return;
    ctx.beginPath();ctx.arc(p2.x,p2.y,p2.r*p2.life,0,Math.PI*2);
    ctx.fillStyle=p2.col+Math.floor(p2.life*180).toString(16).padStart(2,'0');
    ctx.fill();
  });

  // Flash blanc central au moment du pic
  if(p>.55&&p<.75){
    var flashP=(p-.55)/.2;
    var flashAlpha=flashP<.5?flashP*2:(1-flashP)*2;
    var flashR=Math.min(flashAlpha*120,120);
    var grd=ctx.createRadialGradient(cx,cy,0,cx,cy,flashR*8);
    grd.addColorStop(0,'rgba(180,100,255,'+(flashAlpha*.6)+')');
    grd.addColorStop(.3,'rgba(0,229,255,'+(flashAlpha*.3)+')');
    grd.addColorStop(1,'rgba(0,255,136,0)');
    ctx.fillStyle=grd;
    ctx.fillRect(0,0,W,H);
  }

  // Grille scanlines finales
  if(p>.4){
    var scanAlpha=(p-.4)/.6*.12;
    for(var y=0;y<H;y+=4){
      ctx.fillStyle='rgba(0,0,0,'+scanAlpha+')';
      ctx.fillRect(0,y,W,1);
    }
  }
}

function animate(ts){
  var t=ts-startTime;
  drawFrame(t);
  if(t<DURATION){
    animId=requestAnimationFrame(animate);
  } else {
    ctx.fillStyle='#07070F';ctx.fillRect(0,0,W,H);
    if(navigateTo) window.location.href=navigateTo;
  }
}

window._portalGo=function(href,ox,oy){
  if(animId)return;
  navigateTo=href;
  overlay.style.opacity='1';
  overlay.classList.add('active');
  initPortal(ox,oy);
  startTime=performance.now();
  animId=requestAnimationFrame(animate);
};

// Intercepter tous les liens internes (sauf ancres et externes)
document.addEventListener('click',function(e){
  var a=e.target.closest('a[href]');
  if(!a)return;
  var href=a.getAttribute('href');
  if(!href||href.startsWith('http')||href.startsWith('mailto')||href.startsWith('#')||href.startsWith('javascript'))return;
  e.preventDefault();
  var rect=a.getBoundingClientRect();
  var ox=rect.left+rect.width/2, oy=rect.top+rect.height/2;
  if(window._fadeTo){window._fadeTo(href);}else{window.location.href=href;}
});

// Apparition de la page courante en fondu entrant
document.documentElement.style.opacity='0';
document.documentElement.style.transition='opacity .35s ease';
requestAnimationFrame(function(){requestAnimationFrame(function(){document.documentElement.style.opacity='1';});});
})();
</script>

<p style="text-align:center;font-size:11px;font-style:italic;color:rgba(136,136,170,0.45);padding:40px 20px 20px;line-height:1.8">J'ai conçu cet outil avec passion dans une démarche purement technique. Le principal reste de s'amuser et de rester créatif. <em>— Loïc</em> · <a href="/privacy" style="color:rgba(136,136,170,0.5);text-decoration:none">Privacy Policy</a></p>
<div id="portalOverlay"><canvas id="portalCanvas"></canvas></div>
<style>#portalOverlay{position:fixed;inset:0;z-index:99999;pointer-events:none;opacity:0}#portalOverlay.active{pointer-events:all}#portalCanvas{position:absolute;inset:0;width:100%;height:100%}</style>
<script>
(function(){
var ov=document.getElementById('portalOverlay'),cv=document.getElementById('portalCanvas'),ctx=cv.getContext('2d');
var W,H,cx,cy,rings=[],beams=[],parts=[],aid=null,t0=0,dest=null,DUR=780;
var C=['#7B2FFF','#00E5FF','#00FF88','#B44FFF','#00FFCC','#FF4488'];
function resize(){W=cv.width=window.innerWidth;H=cv.height=window.innerHeight;}
window.addEventListener('resize',resize);resize();
function init(ox,oy,intense){
  cx=ox||W/2;cy=oy||H/2;rings=[];beams=[];parts=[];
  var nb=intense?12:8,nb2=intense?48:32,nb3=intense?90:60;
  for(var i=0;i<nb;i++)rings.push({d:i*50,col:C[i%C.length],w:1.5+i*.6});
  for(var i=0;i<nb2;i++){var a=i/nb2*Math.PI*2,dist=Math.sqrt(W*W+H*H)*(intense?.75:.6);beams.push({a:a,dist:dist,col:C[i%C.length],w:.5+Math.random()*1.8,d:i*14});}
  for(var i=0;i<nb3;i++){var a=Math.random()*Math.PI*2,s=3+Math.random()*(intense?10:6);parts.push({x:cx,y:cy,vx:Math.cos(a)*s,vy:Math.sin(a)*s,r:1.5+Math.random()*3.5,life:1,col:C[Math.floor(Math.random()*C.length)]});}
}
function frame(ts){
  var t=ts-t0,p=Math.min(t/DUR,1),ease=p<.5?2*p*p:1-Math.pow(-2*p+2,2)/2;
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle='rgba(7,7,15,'+Math.min(ease*.95,.95)+')';ctx.fillRect(0,0,W,H);
  beams.forEach(function(b){
    var bt=Math.max(0,(t-b.d)/DUR);if(!bt)return;
    var alpha=Math.min(bt*3,.88)*(1-ease*.35),startD=b.dist*(1-Math.min(bt*2.2,1));
    var sx=cx+Math.cos(b.a)*b.dist,sy=cy+Math.sin(b.a)*b.dist,ex=cx+Math.cos(b.a)*startD,ey=cy+Math.sin(b.a)*startD;
    var g=ctx.createLinearGradient(sx,sy,ex,ey);
    g.addColorStop(0,b.col+'00');g.addColorStop(.55,b.col+Math.floor(alpha*160).toString(16).padStart(2,'0'));g.addColorStop(1,b.col+Math.floor(alpha*255).toString(16).padStart(2,'0'));
    ctx.beginPath();ctx.moveTo(sx,sy);ctx.lineTo(ex,ey);ctx.strokeStyle=g;ctx.lineWidth=b.w*(1+ease*2.5);ctx.stroke();
  });
  var maxR=Math.sqrt(W*W+H*H);
  rings.forEach(function(r){
    var rt=Math.max(0,(t-r.d)/DUR);if(!rt)return;
    var alpha=Math.min(rt*4,1)*(1-rt*.75);if(alpha<.01)return;
    var radius=maxR*rt*.85;
    ctx.beginPath();ctx.arc(cx,cy,radius,0,Math.PI*2);ctx.strokeStyle=r.col+Math.floor(alpha*230).toString(16).padStart(2,'0');ctx.lineWidth=r.w*(1+ease*2);ctx.stroke();
    if(radius>20){ctx.beginPath();ctx.arc(cx,cy,radius*.86,0,Math.PI*2);ctx.strokeStyle=r.col+Math.floor(alpha*70).toString(16).padStart(2,'0');ctx.lineWidth=r.w*.35;ctx.stroke();}
  });
  parts.forEach(function(p2){p2.x+=p2.vx*(1+ease*4);p2.y+=p2.vy*(1+ease*4);p2.life-=.016;if(p2.life<.05)return;ctx.beginPath();ctx.arc(p2.x,p2.y,p2.r*p2.life,0,Math.PI*2);ctx.fillStyle=p2.col+Math.floor(p2.life*190).toString(16).padStart(2,'0');ctx.fill();});
  if(p>.52&&p<.78){var fp=(p-.52)/.26,fa=fp<.5?fp*2:(1-fp)*2,gr=ctx.createRadialGradient(cx,cy,0,cx,cy,fa*160);gr.addColorStop(0,'rgba(160,80,255,'+fa*.7+')');gr.addColorStop(.35,'rgba(0,229,255,'+fa*.35+')');gr.addColorStop(1,'rgba(0,255,136,0)');ctx.fillStyle=gr;ctx.fillRect(0,0,W,H);}
  if(p>.3){var vp=(p-.3)/.7,vr=vp*80,vg=ctx.createRadialGradient(cx,cy,0,cx,cy,vr);vg.addColorStop(0,'rgba(0,0,0,'+Math.min(vp*.9,.9)+')');vg.addColorStop(.5,'rgba(7,7,15,'+Math.min(vp*.5,.5)+')');vg.addColorStop(1,'rgba(7,7,15,0)');ctx.fillStyle=vg;ctx.beginPath();ctx.arc(cx,cy,vr,0,Math.PI*2);ctx.fill();}
  if(p>.35){var sa=(p-.35)/.65*.14;for(var y=0;y<H;y+=4){ctx.fillStyle='rgba(0,0,0,'+sa+')';ctx.fillRect(0,y,W,1);}}
  if(t<DUR){aid=requestAnimationFrame(frame);}else{ctx.fillStyle='#07070F';ctx.fillRect(0,0,W,H);if(dest&&dest!==null&&dest!=="null"){window.location.href=dest;}else{ov.style.opacity='0';ov.classList.remove('active');aid=null;dest=null;}}
}
window._portalGo=function(href,ox,oy,intense){if(aid)return;dest=href;ov.style.opacity='1';ov.classList.add('active');init(ox,oy,intense);t0=performance.now();aid=requestAnimationFrame(frame);};
document.addEventListener('click',function(e){
  var a=e.target.closest('a[href]');if(!a)return;
  var h=a.getAttribute('href');
  if(!h||h[0]==='#'||h.indexOf('://')>0||h.startsWith('mailto')||h.startsWith('javascript'))return;
  e.preventDefault();var r=a.getBoundingClientRect();if(window._fadeTo){window._fadeTo(h);}else{window.location.href=h;}
});
document.documentElement.style.opacity='0';document.documentElement.style.transition='opacity .38s ease';
window.addEventListener('load',function(){document.documentElement.style.opacity='1';});
setTimeout(function(){document.documentElement.style.opacity='1';},50);
})();
</script>
</body>
</html>"""

WHY_PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pourquoi InsideYourMix ?</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#07070F;color:#F0F0F8;font-family:'DM Sans',sans-serif}
.bg-gradient{position:fixed;top:0;left:0;width:100%;height:100%;background:radial-gradient(ellipse at top,rgba(123,47,255,0.15) 0%,transparent 50%);z-index:0;pointer-events:none}
.nav{position:fixed;top:0;left:0;right:0;padding:20px 48px;display:flex;justify-content:space-between;align-items:center;z-index:100;background:rgba(7,7,15,0.6);backdrop-filter:blur(24px);border-bottom:1px solid rgba(255,255,255,0.05)}
.logo{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:20px;background:linear-gradient(90deg,#F0F0F8 0%,#7B2FFF 50%,#00E5FF 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;text-decoration:none}
.nav-cta{background:linear-gradient(90deg,#7B2FFF,#00E5FF);color:white;padding:10px 24px;border-radius:24px;text-decoration:none;font-weight:600;font-size:14px}
.content{max-width:900px;margin:0 auto;padding:160px 48px 120px;position:relative;z-index:1}
.badge{display:inline-block;padding:8px 16px;background:rgba(123,47,255,0.15);border:1px solid rgba(123,47,255,0.3);border-radius:24px;font-size:12px;font-weight:600;color:#00E5FF;margin-bottom:32px;letter-spacing:0.05em;text-transform:uppercase}
h1{font-family:'Syne',sans-serif;font-size:clamp(40px,6vw,72px);font-weight:800;line-height:1.05;margin-bottom:80px;letter-spacing:-0.03em}
h1 span{background:linear-gradient(90deg,#7B2FFF,#00E5FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.block{margin-bottom:80px}.block-label{font-size:12px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:#7B2FFF;margin-bottom:24px}
.block h2{font-family:'Syne',sans-serif;font-size:clamp(24px,3vw,36px);font-weight:700;margin-bottom:20px;line-height:1.2}
.block p{opacity:0.75;line-height:1.8;font-size:17px;max-width:700px}
.arrow{font-size:48px;text-align:center;margin:40px 0;opacity:0.5}
.solution-block{background:linear-gradient(135deg,rgba(123,47,255,0.1),rgba(0,229,255,0.05));border:1px solid rgba(123,47,255,0.2);border-radius:24px;padding:48px;margin-bottom:80px}
.solution-block h2{font-family:'Syne',sans-serif;font-size:clamp(24px,3vw,36px);font-weight:700;margin-bottom:20px;background:linear-gradient(90deg,#7B2FFF,#00E5FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.solution-block p{opacity:0.8;line-height:1.8;font-size:17px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:24px;margin:64px 0}
.stat{text-align:center;padding:32px 24px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);border-radius:16px}
.stat-number{font-family:'Syne',sans-serif;font-size:48px;font-weight:800;background:linear-gradient(90deg,#7B2FFF,#00E5FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:8px}
.stat p{opacity:0.65;font-size:14px}
.cta-section{text-align:center;margin-top:80px}
.cta-section h3{font-family:'Syne',sans-serif;font-size:36px;font-weight:700;margin-bottom:24px}
.hero-cta{display:inline-flex;align-items:center;gap:12px;background:linear-gradient(90deg,#7B2FFF,#00E5FF);color:white;padding:20px 48px;border-radius:32px;text-decoration:none;font-weight:700;font-size:18px;box-shadow:0 10px 40px rgba(123,47,255,0.4)}

.nav{position:fixed;top:0;left:0;right:0;padding:20px 48px;display:flex;justify-content:space-between;align-items:center;z-index:100;background:rgba(7,7,15,0.6);backdrop-filter:blur(24px);border-bottom:1px solid rgba(255,255,255,0.05)}
.logo{font-family:'Syne',sans-serif;font-weight:800;font-size:20px;background:linear-gradient(90deg,#F0F0F8 0%,#7B2FFF 50%,#00E5FF 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;text-decoration:none}
.nav-right{display:flex;gap:28px;align-items:center}
.nav-cta{background:linear-gradient(135deg,#7B2FFF,#5020CC);color:white;padding:10px 24px;border-radius:24px;text-decoration:none;font-weight:600;font-size:14px;transition:all .2s;box-shadow:0 4px 20px rgba(123,47,255,.3)}
.nav-cta:hover{transform:translateY(-1px);box-shadow:0 8px 30px rgba(123,47,255,.5)}
.dropdown{position:relative}
.menu-btn{background:none;border:1px solid rgba(255,255,255,.18);border-radius:8px;padding:8px 12px;cursor:pointer;display:flex;flex-direction:column;gap:5px;transition:border-color .2s}
.menu-btn:hover{border-color:rgba(123,47,255,.5)}
.menu-btn span{display:block;width:22px;height:2px;background:#F0F0F8;border-radius:2px;transition:all .3s}
.menu-btn.open span:nth-child(1){transform:translateY(7px) rotate(45deg)}
.menu-btn.open span:nth-child(2){opacity:0;transform:scaleX(0)}
.menu-btn.open span:nth-child(3){transform:translateY(-7px) rotate(-45deg)}
.dropdown-menu{position:absolute;top:52px;right:0;background:rgba(12,12,22,.97);backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,.1);border-radius:18px;padding:12px;min-width:240px;display:none;flex-direction:column;gap:3px;z-index:1000;box-shadow:0 24px 60px rgba(0,0,0,.6)}
.dropdown-menu.open{display:flex}
.dropdown-item{color:#F0F0F8;text-decoration:none;padding:12px 16px;border-radius:10px;font-size:15px;font-weight:500;transition:background .2s}
.dropdown-item:hover{background:rgba(123,47,255,.2)}
.dropdown-divider{height:1px;background:rgba(255,255,255,.07);margin:8px 0}
.lang-selector{display:flex;gap:8px;padding:8px 16px;justify-content:center}
.lang-flag{font-size:22px;cursor:pointer;opacity:.65;transition:all .2s;border-radius:4px;padding:4px}
.lang-flag:hover{opacity:1;transform:scale(1.2)}
@media(max-width:640px){.nav{padding:12px 16px !important}.logo{font-size:17px !important}.nav-cta{padding:8px 16px;font-size:13px}}
</style>
</head>
<body>
<div class="bg-gradient"></div>
<nav class="nav">
<a href="/" class="logo">InsideYourMix</a>
<div class="nav-right">
<div class="dropdown">
<button class="menu-btn" id="menuBtn" onclick="toggleMenu()"><span></span><span></span><span></span></button>
<div class="dropdown-menu" id="dropdownMenu">
<a href="/how-it-works" class="dropdown-item">✦ How it works</a>
<a href="/why" class="dropdown-item">✦ Why InsideYourMix</a>
<a href="/abonnements" class="dropdown-item">✦ Abonnements</a>
<a href="/contact" class="dropdown-item">✦ Contact</a>
<div class="dropdown-divider"></div>
<a href="/login" class="dropdown-item" style="color:#00E5FF">→ Se connecter</a>
<a href="/register" class="dropdown-item" style="color:#00FF88;font-weight:700">✦ Creer un compte</a>
<div class="dropdown-divider"></div>
<div class="lang-selector">
<span onclick="setLang('fr')" class="lang-flag">🇫🇷</span>
<span onclick="setLang('en')" class="lang-flag">🇬🇧</span>
<span onclick="setLang('es')" class="lang-flag">🇪🇸</span>
<span onclick="setLang('de')" class="lang-flag">🇩🇪</span>
<span onclick="setLang('pt')" class="lang-flag">🇵🇹</span>
</div>
</div>
</div>
<a href="/analyze" class="nav-cta">Analyser →</a>
</div>
</nav>
<div class="content">
<div class="badge">Notre histoire</div>
<h1>Pourquoi<br><span>InsideYourMix</span> ?</h1>
<div class="stats">
<div class="stat"><div class="stat-number">50M+</div><p>Producteurs dans le monde</p></div>
<div class="stat"><div class="stat-number">300€+</div><p>Le cout d'une session ingenieur son</p></div>
<div class="stat"><div class="stat-number">7</div><p>Dimensions analysees dans chaque mix</p></div>
<div class="stat"><div class="stat-number">100+</div><p>Genres musicaux references</p></div>
</div>
<div class="block"><div class="block-label">Le probleme</div>
<h2>Tu mixes dans le vide.</h2>
<p>50 millions de producteurs travaillent leurs sons seuls, sans feedback technique precis. Un ingenieur son coute plusieurs centaines d'euros la session. Et a force d'ecouter ton propre mix en boucle, tu perds toute perspective.</p></div>
<div class="arrow">↓</div>
<div class="solution-block"><div class="block-label">La solution</div>
<h2>Un coach technique disponible 24h/24, au prix d'un cafe.</h2>
<p>InsideYourMix analyse ton mix sur 7 dimensions techniques precises et te livre un rapport coaching personnalise, concret et actionnable. En quelques minutes, tu sais exactement sur quoi travailler.</p></div>
<div class="block"><div class="block-label">Pour qui ?</div>
<h2>Pour tous les producteurs qui veulent progresser vite.</h2>
<p>Debutant ou producteur experimente — InsideYourMix te donne un regard exterieur objectif et technique a chaque fois que tu en as besoin.</p></div>
<div class="cta-section"><h3>Pret a decouvrir ce que cache ton mix ?</h3>
<a href="/analyze" class="hero-cta">Analyser mon mix gratuitement</a></div>
</div><script>
function toggleMenu(){
  var m=document.getElementById('dropdownMenu');
  var b=document.getElementById('menuBtn');
  m.classList.toggle('open');
  b.classList.toggle('open');
}
document.addEventListener('click',function(e){
  if(!e.target.closest('.dropdown')){
    var d=document.getElementById('dropdownMenu');
    var b=document.getElementById('menuBtn');
    if(d)d.classList.remove('open');
    if(b)b.classList.remove('open');
  }
});
function setLang(l){alert('Langue '+l+' - bientot disponible !');}
</script>

<div id="portalOverlay"><canvas id="portalCanvas"></canvas></div>
<script>
(function(){
var overlay=document.getElementById('portalOverlay');
var canvas=document.getElementById('portalCanvas');
var ctx=canvas.getContext('2d');
var W,H,cx,cy,rings=[],beams=[],particles=[];
var animId=null,startTime=0,navigateTo=null;
var COLORS=['#7B2FFF','#00E5FF','#00FF88','#B44FFF','#00FFCC'];
var DURATION=820;

function resize(){W=canvas.width=window.innerWidth;H=canvas.height=window.innerHeight;}
window.addEventListener('resize',resize);resize();

function initPortal(ox,oy){
  cx=ox||W/2; cy=oy||H/2;
  rings=[]; beams=[]; particles=[];
  // Ondes sonores concentriques
  for(var i=0;i<8;i++){
    rings.push({delay:i*60,r:0,maxR:Math.sqrt(W*W+H*H),col:COLORS[i%COLORS.length],w:2+i*.5});
  }
  // Faisceaux convergents vers le centre (vortex)
  for(var i=0;i<32;i++){
    var angle=i/32*Math.PI*2;
    var dist=Math.sqrt(W*W+H*H)*.6;
    beams.push({angle:angle,dist:dist,col:COLORS[i%COLORS.length],w:.5+Math.random()*1.5,delay:i*18});
  }
  // Particules explosives
  for(var i=0;i<60;i++){
    var angle=Math.random()*Math.PI*2;
    var spd=2+Math.random()*6;
    particles.push({x:cx,y:cy,vx:Math.cos(angle)*spd,vy:Math.sin(angle)*spd,
      r:2+Math.random()*3,life:1,col:COLORS[Math.floor(Math.random()*COLORS.length)]});
  }
}

function drawFrame(t){
  var p=Math.min(t/DURATION,1);
  var ease=p<.5?2*p*p:1-Math.pow(-2*p+2,2)/2;

  ctx.clearRect(0,0,W,H);

  // Fond qui assombrit progressivement
  ctx.fillStyle='rgba(7,7,15,'+(ease*.92)+')';
  ctx.fillRect(0,0,W,H);

  // Faisceaux vortex convergeant vers cx,cy
  beams.forEach(function(b,i){
    var bt=Math.max(0,(t-b.delay)/DURATION);
    if(bt<=0)return;
    var alpha=Math.min(bt*3,.85)*(1-ease*.4);
    var startDist=b.dist*(1-Math.min(bt*2,1));
    var sx=cx+Math.cos(b.angle)*b.dist;
    var sy=cy+Math.sin(b.angle)*b.dist;
    var ex=cx+Math.cos(b.angle)*startDist;
    var ey=cy+Math.sin(b.angle)*startDist;
    var grad=ctx.createLinearGradient(sx,sy,ex,ey);
    grad.addColorStop(0,b.col+'00');
    grad.addColorStop(.6,b.col+Math.floor(alpha*180).toString(16).padStart(2,'0'));
    grad.addColorStop(1,b.col+Math.floor(alpha*255).toString(16).padStart(2,'0'));
    ctx.beginPath();ctx.moveTo(sx,sy);ctx.lineTo(ex,ey);
    ctx.strokeStyle=grad;ctx.lineWidth=b.w*(1+ease*2);ctx.stroke();
  });

  // Ondes sonores concentriques depuis cx,cy
  rings.forEach(function(r){
    var rt=Math.max(0,(t-r.delay)/DURATION);
    if(rt<=0)return;
    var alpha=Math.min(rt*4,1)*(1-rt*.7);
    if(alpha<.01)return;
    var radius=r.maxR*rt*.8;
    ctx.beginPath();
    ctx.arc(cx,cy,radius,0,Math.PI*2);
    ctx.strokeStyle=r.col+Math.floor(alpha*220).toString(16).padStart(2,'0');
    ctx.lineWidth=r.w*(1+ease*1.5);
    ctx.stroke();
    // Second anneau plus fin légèrement en retard
    if(radius>30){
      ctx.beginPath();
      ctx.arc(cx,cy,radius*.88,0,Math.PI*2);
      ctx.strokeStyle=r.col+Math.floor(alpha*80).toString(16).padStart(2,'0');
      ctx.lineWidth=r.w*.4;ctx.stroke();
    }
  });

  // Particules
  particles.forEach(function(p2){
    p2.x+=p2.vx*(1+ease*3);p2.y+=p2.vy*(1+ease*3);
    p2.life-=.018;
    if(p2.life<0)return;
    ctx.beginPath();ctx.arc(p2.x,p2.y,p2.r*p2.life,0,Math.PI*2);
    ctx.fillStyle=p2.col+Math.floor(p2.life*180).toString(16).padStart(2,'0');
    ctx.fill();
  });

  // Flash blanc central au moment du pic
  if(p>.55&&p<.75){
    var flashP=(p-.55)/.2;
    var flashAlpha=flashP<.5?flashP*2:(1-flashP)*2;
    var flashR=Math.min(flashAlpha*120,120);
    var grd=ctx.createRadialGradient(cx,cy,0,cx,cy,flashR*8);
    grd.addColorStop(0,'rgba(180,100,255,'+(flashAlpha*.6)+')');
    grd.addColorStop(.3,'rgba(0,229,255,'+(flashAlpha*.3)+')');
    grd.addColorStop(1,'rgba(0,255,136,0)');
    ctx.fillStyle=grd;
    ctx.fillRect(0,0,W,H);
  }

  // Grille scanlines finales
  if(p>.4){
    var scanAlpha=(p-.4)/.6*.12;
    for(var y=0;y<H;y+=4){
      ctx.fillStyle='rgba(0,0,0,'+scanAlpha+')';
      ctx.fillRect(0,y,W,1);
    }
  }
}

function animate(ts){
  var t=ts-startTime;
  drawFrame(t);
  if(t<DURATION){
    animId=requestAnimationFrame(animate);
  } else {
    ctx.fillStyle='#07070F';ctx.fillRect(0,0,W,H);
    if(navigateTo) window.location.href=navigateTo;
  }
}

window._portalGo=function(href,ox,oy){
  if(animId)return;
  navigateTo=href;
  overlay.style.opacity='1';
  overlay.classList.add('active');
  initPortal(ox,oy);
  startTime=performance.now();
  animId=requestAnimationFrame(animate);
};

// Intercepter tous les liens internes (sauf ancres et externes)
document.addEventListener('click',function(e){
  var a=e.target.closest('a[href]');
  if(!a)return;
  var href=a.getAttribute('href');
  if(!href||href.startsWith('http')||href.startsWith('mailto')||href.startsWith('#')||href.startsWith('javascript'))return;
  e.preventDefault();
  var rect=a.getBoundingClientRect();
  var ox=rect.left+rect.width/2, oy=rect.top+rect.height/2;
  if(window._fadeTo){window._fadeTo(href);}else{window.location.href=href;}
});

// Apparition de la page courante en fondu entrant
document.documentElement.style.opacity='0';
document.documentElement.style.transition='opacity .35s ease';
requestAnimationFrame(function(){requestAnimationFrame(function(){document.documentElement.style.opacity='1';});});
})();
</script>


<div id="portalOverlay"><canvas id="portalCanvas"></canvas></div>
<script>
(function(){
var overlay=document.getElementById('portalOverlay');
var canvas=document.getElementById('portalCanvas');
var ctx=canvas.getContext('2d');
var W,H,cx,cy,rings=[],beams=[],particles=[];
var animId=null,startTime=0,navigateTo=null;
var COLORS=['#7B2FFF','#00E5FF','#00FF88','#B44FFF','#00FFCC'];
var DURATION=820;

function resize(){W=canvas.width=window.innerWidth;H=canvas.height=window.innerHeight;}
window.addEventListener('resize',resize);resize();

function initPortal(ox,oy){
  cx=ox||W/2; cy=oy||H/2;
  rings=[]; beams=[]; particles=[];
  // Ondes sonores concentriques
  for(var i=0;i<8;i++){
    rings.push({delay:i*60,r:0,maxR:Math.sqrt(W*W+H*H),col:COLORS[i%COLORS.length],w:2+i*.5});
  }
  // Faisceaux convergents vers le centre (vortex)
  for(var i=0;i<32;i++){
    var angle=i/32*Math.PI*2;
    var dist=Math.sqrt(W*W+H*H)*.6;
    beams.push({angle:angle,dist:dist,col:COLORS[i%COLORS.length],w:.5+Math.random()*1.5,delay:i*18});
  }
  // Particules explosives
  for(var i=0;i<60;i++){
    var angle=Math.random()*Math.PI*2;
    var spd=2+Math.random()*6;
    particles.push({x:cx,y:cy,vx:Math.cos(angle)*spd,vy:Math.sin(angle)*spd,
      r:2+Math.random()*3,life:1,col:COLORS[Math.floor(Math.random()*COLORS.length)]});
  }
}

function drawFrame(t){
  var p=Math.min(t/DURATION,1);
  var ease=p<.5?2*p*p:1-Math.pow(-2*p+2,2)/2;

  ctx.clearRect(0,0,W,H);

  // Fond qui assombrit progressivement
  ctx.fillStyle='rgba(7,7,15,'+(ease*.92)+')';
  ctx.fillRect(0,0,W,H);

  // Faisceaux vortex convergeant vers cx,cy
  beams.forEach(function(b,i){
    var bt=Math.max(0,(t-b.delay)/DURATION);
    if(bt<=0)return;
    var alpha=Math.min(bt*3,.85)*(1-ease*.4);
    var startDist=b.dist*(1-Math.min(bt*2,1));
    var sx=cx+Math.cos(b.angle)*b.dist;
    var sy=cy+Math.sin(b.angle)*b.dist;
    var ex=cx+Math.cos(b.angle)*startDist;
    var ey=cy+Math.sin(b.angle)*startDist;
    var grad=ctx.createLinearGradient(sx,sy,ex,ey);
    grad.addColorStop(0,b.col+'00');
    grad.addColorStop(.6,b.col+Math.floor(alpha*180).toString(16).padStart(2,'0'));
    grad.addColorStop(1,b.col+Math.floor(alpha*255).toString(16).padStart(2,'0'));
    ctx.beginPath();ctx.moveTo(sx,sy);ctx.lineTo(ex,ey);
    ctx.strokeStyle=grad;ctx.lineWidth=b.w*(1+ease*2);ctx.stroke();
  });

  // Ondes sonores concentriques depuis cx,cy
  rings.forEach(function(r){
    var rt=Math.max(0,(t-r.delay)/DURATION);
    if(rt<=0)return;
    var alpha=Math.min(rt*4,1)*(1-rt*.7);
    if(alpha<.01)return;
    var radius=r.maxR*rt*.8;
    ctx.beginPath();
    ctx.arc(cx,cy,radius,0,Math.PI*2);
    ctx.strokeStyle=r.col+Math.floor(alpha*220).toString(16).padStart(2,'0');
    ctx.lineWidth=r.w*(1+ease*1.5);
    ctx.stroke();
    // Second anneau plus fin légèrement en retard
    if(radius>30){
      ctx.beginPath();
      ctx.arc(cx,cy,radius*.88,0,Math.PI*2);
      ctx.strokeStyle=r.col+Math.floor(alpha*80).toString(16).padStart(2,'0');
      ctx.lineWidth=r.w*.4;ctx.stroke();
    }
  });

  // Particules
  particles.forEach(function(p2){
    p2.x+=p2.vx*(1+ease*3);p2.y+=p2.vy*(1+ease*3);
    p2.life-=.018;
    if(p2.life<0)return;
    ctx.beginPath();ctx.arc(p2.x,p2.y,p2.r*p2.life,0,Math.PI*2);
    ctx.fillStyle=p2.col+Math.floor(p2.life*180).toString(16).padStart(2,'0');
    ctx.fill();
  });

  // Flash blanc central au moment du pic
  if(p>.55&&p<.75){
    var flashP=(p-.55)/.2;
    var flashAlpha=flashP<.5?flashP*2:(1-flashP)*2;
    var flashR=Math.min(flashAlpha*120,120);
    var grd=ctx.createRadialGradient(cx,cy,0,cx,cy,flashR*8);
    grd.addColorStop(0,'rgba(180,100,255,'+(flashAlpha*.6)+')');
    grd.addColorStop(.3,'rgba(0,229,255,'+(flashAlpha*.3)+')');
    grd.addColorStop(1,'rgba(0,255,136,0)');
    ctx.fillStyle=grd;
    ctx.fillRect(0,0,W,H);
  }

  // Grille scanlines finales
  if(p>.4){
    var scanAlpha=(p-.4)/.6*.12;
    for(var y=0;y<H;y+=4){
      ctx.fillStyle='rgba(0,0,0,'+scanAlpha+')';
      ctx.fillRect(0,y,W,1);
    }
  }
}

function animate(ts){
  var t=ts-startTime;
  drawFrame(t);
  if(t<DURATION){
    animId=requestAnimationFrame(animate);
  } else {
    ctx.fillStyle='#07070F';ctx.fillRect(0,0,W,H);
    if(navigateTo) window.location.href=navigateTo;
  }
}

window._portalGo=function(href,ox,oy){
  if(animId)return;
  navigateTo=href;
  overlay.style.opacity='1';
  overlay.classList.add('active');
  initPortal(ox,oy);
  startTime=performance.now();
  animId=requestAnimationFrame(animate);
};

// Intercepter tous les liens internes (sauf ancres et externes)
document.addEventListener('click',function(e){
  var a=e.target.closest('a[href]');
  if(!a)return;
  var href=a.getAttribute('href');
  if(!href||href.startsWith('http')||href.startsWith('mailto')||href.startsWith('#')||href.startsWith('javascript'))return;
  e.preventDefault();
  var rect=a.getBoundingClientRect();
  var ox=rect.left+rect.width/2, oy=rect.top+rect.height/2;
  if(window._fadeTo){window._fadeTo(href);}else{window.location.href=href;}
});

// Apparition de la page courante en fondu entrant
document.documentElement.style.opacity='0';
document.documentElement.style.transition='opacity .35s ease';
requestAnimationFrame(function(){requestAnimationFrame(function(){document.documentElement.style.opacity='1';});});
})();
</script>


<div id="portalOverlay"><canvas id="portalCanvas"></canvas></div>
<script>
(function(){
var overlay=document.getElementById('portalOverlay');
var canvas=document.getElementById('portalCanvas');
var ctx=canvas.getContext('2d');
var W,H,cx,cy,rings=[],beams=[],particles=[];
var animId=null,startTime=0,navigateTo=null;
var COLORS=['#7B2FFF','#00E5FF','#00FF88','#B44FFF','#00FFCC'];
var DURATION=820;

function resize(){W=canvas.width=window.innerWidth;H=canvas.height=window.innerHeight;}
window.addEventListener('resize',resize);resize();

function initPortal(ox,oy){
  cx=ox||W/2; cy=oy||H/2;
  rings=[]; beams=[]; particles=[];
  // Ondes sonores concentriques
  for(var i=0;i<8;i++){
    rings.push({delay:i*60,r:0,maxR:Math.sqrt(W*W+H*H),col:COLORS[i%COLORS.length],w:2+i*.5});
  }
  // Faisceaux convergents vers le centre (vortex)
  for(var i=0;i<32;i++){
    var angle=i/32*Math.PI*2;
    var dist=Math.sqrt(W*W+H*H)*.6;
    beams.push({angle:angle,dist:dist,col:COLORS[i%COLORS.length],w:.5+Math.random()*1.5,delay:i*18});
  }
  // Particules explosives
  for(var i=0;i<60;i++){
    var angle=Math.random()*Math.PI*2;
    var spd=2+Math.random()*6;
    particles.push({x:cx,y:cy,vx:Math.cos(angle)*spd,vy:Math.sin(angle)*spd,
      r:2+Math.random()*3,life:1,col:COLORS[Math.floor(Math.random()*COLORS.length)]});
  }
}

function drawFrame(t){
  var p=Math.min(t/DURATION,1);
  var ease=p<.5?2*p*p:1-Math.pow(-2*p+2,2)/2;

  ctx.clearRect(0,0,W,H);

  // Fond qui assombrit progressivement
  ctx.fillStyle='rgba(7,7,15,'+(ease*.92)+')';
  ctx.fillRect(0,0,W,H);

  // Faisceaux vortex convergeant vers cx,cy
  beams.forEach(function(b,i){
    var bt=Math.max(0,(t-b.delay)/DURATION);
    if(bt<=0)return;
    var alpha=Math.min(bt*3,.85)*(1-ease*.4);
    var startDist=b.dist*(1-Math.min(bt*2,1));
    var sx=cx+Math.cos(b.angle)*b.dist;
    var sy=cy+Math.sin(b.angle)*b.dist;
    var ex=cx+Math.cos(b.angle)*startDist;
    var ey=cy+Math.sin(b.angle)*startDist;
    var grad=ctx.createLinearGradient(sx,sy,ex,ey);
    grad.addColorStop(0,b.col+'00');
    grad.addColorStop(.6,b.col+Math.floor(alpha*180).toString(16).padStart(2,'0'));
    grad.addColorStop(1,b.col+Math.floor(alpha*255).toString(16).padStart(2,'0'));
    ctx.beginPath();ctx.moveTo(sx,sy);ctx.lineTo(ex,ey);
    ctx.strokeStyle=grad;ctx.lineWidth=b.w*(1+ease*2);ctx.stroke();
  });

  // Ondes sonores concentriques depuis cx,cy
  rings.forEach(function(r){
    var rt=Math.max(0,(t-r.delay)/DURATION);
    if(rt<=0)return;
    var alpha=Math.min(rt*4,1)*(1-rt*.7);
    if(alpha<.01)return;
    var radius=r.maxR*rt*.8;
    ctx.beginPath();
    ctx.arc(cx,cy,radius,0,Math.PI*2);
    ctx.strokeStyle=r.col+Math.floor(alpha*220).toString(16).padStart(2,'0');
    ctx.lineWidth=r.w*(1+ease*1.5);
    ctx.stroke();
    // Second anneau plus fin légèrement en retard
    if(radius>30){
      ctx.beginPath();
      ctx.arc(cx,cy,radius*.88,0,Math.PI*2);
      ctx.strokeStyle=r.col+Math.floor(alpha*80).toString(16).padStart(2,'0');
      ctx.lineWidth=r.w*.4;ctx.stroke();
    }
  });

  // Particules
  particles.forEach(function(p2){
    p2.x+=p2.vx*(1+ease*3);p2.y+=p2.vy*(1+ease*3);
    p2.life-=.018;
    if(p2.life<0)return;
    ctx.beginPath();ctx.arc(p2.x,p2.y,p2.r*p2.life,0,Math.PI*2);
    ctx.fillStyle=p2.col+Math.floor(p2.life*180).toString(16).padStart(2,'0');
    ctx.fill();
  });

  // Flash blanc central au moment du pic
  if(p>.55&&p<.75){
    var flashP=(p-.55)/.2;
    var flashAlpha=flashP<.5?flashP*2:(1-flashP)*2;
    var flashR=Math.min(flashAlpha*120,120);
    var grd=ctx.createRadialGradient(cx,cy,0,cx,cy,flashR*8);
    grd.addColorStop(0,'rgba(180,100,255,'+(flashAlpha*.6)+')');
    grd.addColorStop(.3,'rgba(0,229,255,'+(flashAlpha*.3)+')');
    grd.addColorStop(1,'rgba(0,255,136,0)');
    ctx.fillStyle=grd;
    ctx.fillRect(0,0,W,H);
  }

  // Grille scanlines finales
  if(p>.4){
    var scanAlpha=(p-.4)/.6*.12;
    for(var y=0;y<H;y+=4){
      ctx.fillStyle='rgba(0,0,0,'+scanAlpha+')';
      ctx.fillRect(0,y,W,1);
    }
  }
}

function animate(ts){
  var t=ts-startTime;
  drawFrame(t);
  if(t<DURATION){
    animId=requestAnimationFrame(animate);
  } else {
    ctx.fillStyle='#07070F';ctx.fillRect(0,0,W,H);
    if(navigateTo) window.location.href=navigateTo;
  }
}

window._portalGo=function(href,ox,oy){
  if(animId)return;
  navigateTo=href;
  overlay.style.opacity='1';
  overlay.classList.add('active');
  initPortal(ox,oy);
  startTime=performance.now();
  animId=requestAnimationFrame(animate);
};

// Intercepter tous les liens internes (sauf ancres et externes)
document.addEventListener('click',function(e){
  var a=e.target.closest('a[href]');
  if(!a)return;
  var href=a.getAttribute('href');
  if(!href||href.startsWith('http')||href.startsWith('mailto')||href.startsWith('#')||href.startsWith('javascript'))return;
  e.preventDefault();
  var rect=a.getBoundingClientRect();
  var ox=rect.left+rect.width/2, oy=rect.top+rect.height/2;
  if(window._fadeTo){window._fadeTo(href);}else{window.location.href=href;}
});

// Apparition de la page courante en fondu entrant
document.documentElement.style.opacity='0';
document.documentElement.style.transition='opacity .35s ease';
requestAnimationFrame(function(){requestAnimationFrame(function(){document.documentElement.style.opacity='1';});});
})();
</script>

<p style="text-align:center;font-size:11px;font-style:italic;color:rgba(136,136,170,0.45);padding:40px 20px 20px;line-height:1.8">J'ai conçu cet outil avec passion dans une démarche purement technique. Le principal reste de s'amuser et de rester créatif. <em>— Loïc</em> · <a href="/privacy" style="color:rgba(136,136,170,0.5);text-decoration:none">Privacy Policy</a></p>
<div id="portalOverlay"><canvas id="portalCanvas"></canvas></div>
<style>#portalOverlay{position:fixed;inset:0;z-index:99999;pointer-events:none;opacity:0}#portalOverlay.active{pointer-events:all}#portalCanvas{position:absolute;inset:0;width:100%;height:100%}</style>
<script>
(function(){
var ov=document.getElementById('portalOverlay'),cv=document.getElementById('portalCanvas'),ctx=cv.getContext('2d');
var W,H,cx,cy,rings=[],beams=[],parts=[],aid=null,t0=0,dest=null,DUR=780;
var C=['#7B2FFF','#00E5FF','#00FF88','#B44FFF','#00FFCC','#FF4488'];
function resize(){W=cv.width=window.innerWidth;H=cv.height=window.innerHeight;}
window.addEventListener('resize',resize);resize();
function init(ox,oy,intense){
  cx=ox||W/2;cy=oy||H/2;rings=[];beams=[];parts=[];
  var nb=intense?12:8,nb2=intense?48:32,nb3=intense?90:60;
  for(var i=0;i<nb;i++)rings.push({d:i*50,col:C[i%C.length],w:1.5+i*.6});
  for(var i=0;i<nb2;i++){var a=i/nb2*Math.PI*2,dist=Math.sqrt(W*W+H*H)*(intense?.75:.6);beams.push({a:a,dist:dist,col:C[i%C.length],w:.5+Math.random()*1.8,d:i*14});}
  for(var i=0;i<nb3;i++){var a=Math.random()*Math.PI*2,s=3+Math.random()*(intense?10:6);parts.push({x:cx,y:cy,vx:Math.cos(a)*s,vy:Math.sin(a)*s,r:1.5+Math.random()*3.5,life:1,col:C[Math.floor(Math.random()*C.length)]});}
}
function frame(ts){
  var t=ts-t0,p=Math.min(t/DUR,1),ease=p<.5?2*p*p:1-Math.pow(-2*p+2,2)/2;
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle='rgba(7,7,15,'+Math.min(ease*.95,.95)+')';ctx.fillRect(0,0,W,H);
  beams.forEach(function(b){
    var bt=Math.max(0,(t-b.d)/DUR);if(!bt)return;
    var alpha=Math.min(bt*3,.88)*(1-ease*.35),startD=b.dist*(1-Math.min(bt*2.2,1));
    var sx=cx+Math.cos(b.a)*b.dist,sy=cy+Math.sin(b.a)*b.dist,ex=cx+Math.cos(b.a)*startD,ey=cy+Math.sin(b.a)*startD;
    var g=ctx.createLinearGradient(sx,sy,ex,ey);
    g.addColorStop(0,b.col+'00');g.addColorStop(.55,b.col+Math.floor(alpha*160).toString(16).padStart(2,'0'));g.addColorStop(1,b.col+Math.floor(alpha*255).toString(16).padStart(2,'0'));
    ctx.beginPath();ctx.moveTo(sx,sy);ctx.lineTo(ex,ey);ctx.strokeStyle=g;ctx.lineWidth=b.w*(1+ease*2.5);ctx.stroke();
  });
  var maxR=Math.sqrt(W*W+H*H);
  rings.forEach(function(r){
    var rt=Math.max(0,(t-r.d)/DUR);if(!rt)return;
    var alpha=Math.min(rt*4,1)*(1-rt*.75);if(alpha<.01)return;
    var radius=maxR*rt*.85;
    ctx.beginPath();ctx.arc(cx,cy,radius,0,Math.PI*2);ctx.strokeStyle=r.col+Math.floor(alpha*230).toString(16).padStart(2,'0');ctx.lineWidth=r.w*(1+ease*2);ctx.stroke();
    if(radius>20){ctx.beginPath();ctx.arc(cx,cy,radius*.86,0,Math.PI*2);ctx.strokeStyle=r.col+Math.floor(alpha*70).toString(16).padStart(2,'0');ctx.lineWidth=r.w*.35;ctx.stroke();}
  });
  parts.forEach(function(p2){p2.x+=p2.vx*(1+ease*4);p2.y+=p2.vy*(1+ease*4);p2.life-=.016;if(p2.life<.05)return;ctx.beginPath();ctx.arc(p2.x,p2.y,p2.r*p2.life,0,Math.PI*2);ctx.fillStyle=p2.col+Math.floor(p2.life*190).toString(16).padStart(2,'0');ctx.fill();});
  if(p>.52&&p<.78){var fp=(p-.52)/.26,fa=fp<.5?fp*2:(1-fp)*2,gr=ctx.createRadialGradient(cx,cy,0,cx,cy,fa*160);gr.addColorStop(0,'rgba(160,80,255,'+fa*.7+')');gr.addColorStop(.35,'rgba(0,229,255,'+fa*.35+')');gr.addColorStop(1,'rgba(0,255,136,0)');ctx.fillStyle=gr;ctx.fillRect(0,0,W,H);}
  if(p>.3){var vp=(p-.3)/.7,vr=vp*80,vg=ctx.createRadialGradient(cx,cy,0,cx,cy,vr);vg.addColorStop(0,'rgba(0,0,0,'+Math.min(vp*.9,.9)+')');vg.addColorStop(.5,'rgba(7,7,15,'+Math.min(vp*.5,.5)+')');vg.addColorStop(1,'rgba(7,7,15,0)');ctx.fillStyle=vg;ctx.beginPath();ctx.arc(cx,cy,vr,0,Math.PI*2);ctx.fill();}
  if(p>.35){var sa=(p-.35)/.65*.14;for(var y=0;y<H;y+=4){ctx.fillStyle='rgba(0,0,0,'+sa+')';ctx.fillRect(0,y,W,1);}}
  if(t<DUR){aid=requestAnimationFrame(frame);}else{ctx.fillStyle='#07070F';ctx.fillRect(0,0,W,H);if(dest&&dest!==null&&dest!=="null"){window.location.href=dest;}else{ov.style.opacity='0';ov.classList.remove('active');aid=null;dest=null;}}
}
window._portalGo=function(href,ox,oy,intense){if(aid)return;dest=href;ov.style.opacity='1';ov.classList.add('active');init(ox,oy,intense);t0=performance.now();aid=requestAnimationFrame(frame);};
document.addEventListener('click',function(e){
  var a=e.target.closest('a[href]');if(!a)return;
  var h=a.getAttribute('href');
  if(!h||h[0]==='#'||h.indexOf('://')>0||h.startsWith('mailto')||h.startsWith('javascript'))return;
  e.preventDefault();var r=a.getBoundingClientRect();if(window._fadeTo){window._fadeTo(h);}else{window.location.href=h;}
});
document.documentElement.style.opacity='0';document.documentElement.style.transition='opacity .38s ease';
window.addEventListener('load',function(){document.documentElement.style.opacity='1';});
setTimeout(function(){document.documentElement.style.opacity='1';},50);
})();
</script>
</body></html>"""

CONTACT_PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Contact — InsideYourMix</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#07070F;color:#F0F0F8;font-family:'DM Sans',sans-serif;min-height:100vh;display:flex;flex-direction:column}
.bg-gradient{position:fixed;top:0;left:0;width:100%;height:100%;background:radial-gradient(ellipse at top,rgba(123,47,255,0.15) 0%,transparent 50%);z-index:0;pointer-events:none}
.nav{position:fixed;top:0;left:0;right:0;padding:20px 48px;display:flex;justify-content:space-between;align-items:center;z-index:100;background:rgba(7,7,15,0.6);backdrop-filter:blur(24px);border-bottom:1px solid rgba(255,255,255,0.05)}
.logo{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:20px;background:linear-gradient(90deg,#F0F0F8 0%,#7B2FFF 50%,#00E5FF 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;text-decoration:none}
.nav-cta{background:linear-gradient(90deg,#7B2FFF,#00E5FF);color:white;padding:10px 24px;border-radius:24px;text-decoration:none;font-weight:600;font-size:14px}
.content{max-width:700px;margin:0 auto;padding:160px 48px 120px;position:relative;z-index:1;flex:1}
.badge{display:inline-block;padding:8px 16px;background:rgba(123,47,255,0.15);border:1px solid rgba(123,47,255,0.3);border-radius:24px;font-size:12px;font-weight:600;color:#00E5FF;margin-bottom:32px;letter-spacing:0.05em;text-transform:uppercase}
h1{font-family:'Syne',sans-serif;font-size:clamp(40px,6vw,72px);font-weight:800;margin-bottom:24px;letter-spacing:-0.03em}
h1 span{background:linear-gradient(90deg,#7B2FFF,#00E5FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.intro{opacity:0.75;font-size:18px;line-height:1.7;margin-bottom:64px}
.contact-card{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:20px;padding:32px;margin-bottom:20px;display:flex;align-items:center;gap:24px;text-decoration:none;color:#F0F0F8;transition:all 0.3s}
.contact-card:hover{border-color:rgba(123,47,255,0.4);background:rgba(123,47,255,0.08);transform:translateX(4px)}
.contact-icon{width:56px;height:56px;border-radius:16px;background:linear-gradient(135deg,#7B2FFF,#00E5FF);display:flex;align-items:center;justify-content:center;font-size:28px;flex-shrink:0}
.contact-info h3{font-family:'Syne',sans-serif;font-size:18px;font-weight:700;margin-bottom:4px}
.contact-info p{opacity:0.6;font-size:14px}

.nav{position:fixed;top:0;left:0;right:0;padding:20px 48px;display:flex;justify-content:space-between;align-items:center;z-index:100;background:rgba(7,7,15,0.6);backdrop-filter:blur(24px);border-bottom:1px solid rgba(255,255,255,0.05)}
.logo{font-family:'Syne',sans-serif;font-weight:800;font-size:20px;background:linear-gradient(90deg,#F0F0F8 0%,#7B2FFF 50%,#00E5FF 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;text-decoration:none}
.nav-right{display:flex;gap:28px;align-items:center}
.nav-cta{background:linear-gradient(135deg,#7B2FFF,#5020CC);color:white;padding:10px 24px;border-radius:24px;text-decoration:none;font-weight:600;font-size:14px;transition:all .2s;box-shadow:0 4px 20px rgba(123,47,255,.3)}
.nav-cta:hover{transform:translateY(-1px);box-shadow:0 8px 30px rgba(123,47,255,.5)}
.dropdown{position:relative}
.menu-btn{background:none;border:1px solid rgba(255,255,255,.18);border-radius:8px;padding:8px 12px;cursor:pointer;display:flex;flex-direction:column;gap:5px;transition:border-color .2s}
.menu-btn:hover{border-color:rgba(123,47,255,.5)}
.menu-btn span{display:block;width:22px;height:2px;background:#F0F0F8;border-radius:2px;transition:all .3s}
.menu-btn.open span:nth-child(1){transform:translateY(7px) rotate(45deg)}
.menu-btn.open span:nth-child(2){opacity:0;transform:scaleX(0)}
.menu-btn.open span:nth-child(3){transform:translateY(-7px) rotate(-45deg)}
.dropdown-menu{position:absolute;top:52px;right:0;background:rgba(12,12,22,.97);backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,.1);border-radius:18px;padding:12px;min-width:240px;display:none;flex-direction:column;gap:3px;z-index:1000;box-shadow:0 24px 60px rgba(0,0,0,.6)}
.dropdown-menu.open{display:flex}
.dropdown-item{color:#F0F0F8;text-decoration:none;padding:12px 16px;border-radius:10px;font-size:15px;font-weight:500;transition:background .2s}
.dropdown-item:hover{background:rgba(123,47,255,.2)}
.dropdown-divider{height:1px;background:rgba(255,255,255,.07);margin:8px 0}
.lang-selector{display:flex;gap:8px;padding:8px 16px;justify-content:center}
.lang-flag{font-size:22px;cursor:pointer;opacity:.65;transition:all .2s;border-radius:4px;padding:4px}
.lang-flag:hover{opacity:1;transform:scale(1.2)}
@media(max-width:640px){.nav{padding:12px 16px !important}.logo{font-size:17px !important}.nav-cta{padding:8px 16px;font-size:13px}}
</style>
</head>
<body>
<div class="bg-gradient"></div>
<nav class="nav">
<a href="/" class="logo">InsideYourMix</a>
<div class="nav-right">
<div class="dropdown">
<button class="menu-btn" id="menuBtn" onclick="toggleMenu()"><span></span><span></span><span></span></button>
<div class="dropdown-menu" id="dropdownMenu">
<a href="/how-it-works" class="dropdown-item">✦ How it works</a>
<a href="/why" class="dropdown-item">✦ Why InsideYourMix</a>
<a href="/abonnements" class="dropdown-item">✦ Abonnements</a>
<a href="/contact" class="dropdown-item">✦ Contact</a>
<div class="dropdown-divider"></div>
<a href="/login" class="dropdown-item" style="color:#00E5FF">→ Se connecter</a>
<a href="/register" class="dropdown-item" style="color:#00FF88;font-weight:700">✦ Creer un compte</a>
<div class="dropdown-divider"></div>
<div class="lang-selector">
<span onclick="setLang('fr')" class="lang-flag">🇫🇷</span>
<span onclick="setLang('en')" class="lang-flag">🇬🇧</span>
<span onclick="setLang('es')" class="lang-flag">🇪🇸</span>
<span onclick="setLang('de')" class="lang-flag">🇩🇪</span>
<span onclick="setLang('pt')" class="lang-flag">🇵🇹</span>
</div>
</div>
</div>
<a href="/analyze" class="nav-cta">Analyser →</a>
</div>
</nav>
<div class="content">
<div class="badge">Nous contacter</div>
<h1>On est la pour <span>t'aider</span>.</h1>
<p class="intro">Une question ? Un bug ? Une idee ? On repond a tout.</p>
<a href="mailto:insideyourmix.contact@gmail.com" class="contact-card">
<div class="contact-icon">✉️</div>
<div class="contact-info"><h3>Email</h3><p>insideyourmix.contact@gmail.com</p></div>
</a>
<a href="https://instagram.com/insideyourmix" target="_blank" class="contact-card">
<div class="contact-icon">📸</div>
<div class="contact-info"><h3>Instagram</h3><p>@insideyourmix · Actualites, tips, coulisses</p></div>
</a>
</div><script>
function toggleMenu(){
  var m=document.getElementById('dropdownMenu');
  var b=document.getElementById('menuBtn');
  m.classList.toggle('open');
  b.classList.toggle('open');
}
document.addEventListener('click',function(e){
  if(!e.target.closest('.dropdown')){
    var d=document.getElementById('dropdownMenu');
    var b=document.getElementById('menuBtn');
    if(d)d.classList.remove('open');
    if(b)b.classList.remove('open');
  }
});
function setLang(l){alert('Langue '+l+' - bientot disponible !');}
</script>
<p style="text-align:center;font-size:11px;font-style:italic;color:rgba(136,136,170,0.45);padding:40px 20px 20px;line-height:1.8">J'ai conçu cet outil avec passion dans une démarche purement technique. Le principal reste de s'amuser et de rester créatif. <em>— Loïc</em> · <a href="/privacy" style="color:rgba(136,136,170,0.5);text-decoration:none">Privacy Policy</a></p>
<div id="portalOverlay"><canvas id="portalCanvas"></canvas></div>
<style>#portalOverlay{position:fixed;inset:0;z-index:99999;pointer-events:none;opacity:0}#portalOverlay.active{pointer-events:all}#portalCanvas{position:absolute;inset:0;width:100%;height:100%}</style>
<script>
(function(){
var ov=document.getElementById('portalOverlay'),cv=document.getElementById('portalCanvas'),ctx=cv.getContext('2d');
var W,H,cx,cy,rings=[],beams=[],parts=[],aid=null,t0=0,dest=null,DUR=780;
var C=['#7B2FFF','#00E5FF','#00FF88','#B44FFF','#00FFCC','#FF4488'];
function resize(){W=cv.width=window.innerWidth;H=cv.height=window.innerHeight;}
window.addEventListener('resize',resize);resize();
function init(ox,oy,intense){
  cx=ox||W/2;cy=oy||H/2;rings=[];beams=[];parts=[];
  var nb=intense?12:8,nb2=intense?48:32,nb3=intense?90:60;
  for(var i=0;i<nb;i++)rings.push({d:i*50,col:C[i%C.length],w:1.5+i*.6});
  for(var i=0;i<nb2;i++){var a=i/nb2*Math.PI*2,dist=Math.sqrt(W*W+H*H)*(intense?.75:.6);beams.push({a:a,dist:dist,col:C[i%C.length],w:.5+Math.random()*1.8,d:i*14});}
  for(var i=0;i<nb3;i++){var a=Math.random()*Math.PI*2,s=3+Math.random()*(intense?10:6);parts.push({x:cx,y:cy,vx:Math.cos(a)*s,vy:Math.sin(a)*s,r:1.5+Math.random()*3.5,life:1,col:C[Math.floor(Math.random()*C.length)]});}
}
function frame(ts){
  var t=ts-t0,p=Math.min(t/DUR,1),ease=p<.5?2*p*p:1-Math.pow(-2*p+2,2)/2;
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle='rgba(7,7,15,'+Math.min(ease*.95,.95)+')';ctx.fillRect(0,0,W,H);
  beams.forEach(function(b){
    var bt=Math.max(0,(t-b.d)/DUR);if(!bt)return;
    var alpha=Math.min(bt*3,.88)*(1-ease*.35),startD=b.dist*(1-Math.min(bt*2.2,1));
    var sx=cx+Math.cos(b.a)*b.dist,sy=cy+Math.sin(b.a)*b.dist,ex=cx+Math.cos(b.a)*startD,ey=cy+Math.sin(b.a)*startD;
    var g=ctx.createLinearGradient(sx,sy,ex,ey);
    g.addColorStop(0,b.col+'00');g.addColorStop(.55,b.col+Math.floor(alpha*160).toString(16).padStart(2,'0'));g.addColorStop(1,b.col+Math.floor(alpha*255).toString(16).padStart(2,'0'));
    ctx.beginPath();ctx.moveTo(sx,sy);ctx.lineTo(ex,ey);ctx.strokeStyle=g;ctx.lineWidth=b.w*(1+ease*2.5);ctx.stroke();
  });
  var maxR=Math.sqrt(W*W+H*H);
  rings.forEach(function(r){
    var rt=Math.max(0,(t-r.d)/DUR);if(!rt)return;
    var alpha=Math.min(rt*4,1)*(1-rt*.75);if(alpha<.01)return;
    var radius=maxR*rt*.85;
    ctx.beginPath();ctx.arc(cx,cy,radius,0,Math.PI*2);ctx.strokeStyle=r.col+Math.floor(alpha*230).toString(16).padStart(2,'0');ctx.lineWidth=r.w*(1+ease*2);ctx.stroke();
    if(radius>20){ctx.beginPath();ctx.arc(cx,cy,radius*.86,0,Math.PI*2);ctx.strokeStyle=r.col+Math.floor(alpha*70).toString(16).padStart(2,'0');ctx.lineWidth=r.w*.35;ctx.stroke();}
  });
  parts.forEach(function(p2){p2.x+=p2.vx*(1+ease*4);p2.y+=p2.vy*(1+ease*4);p2.life-=.016;if(p2.life<.05)return;ctx.beginPath();ctx.arc(p2.x,p2.y,p2.r*p2.life,0,Math.PI*2);ctx.fillStyle=p2.col+Math.floor(p2.life*190).toString(16).padStart(2,'0');ctx.fill();});
  if(p>.52&&p<.78){var fp=(p-.52)/.26,fa=fp<.5?fp*2:(1-fp)*2,gr=ctx.createRadialGradient(cx,cy,0,cx,cy,fa*160);gr.addColorStop(0,'rgba(160,80,255,'+fa*.7+')');gr.addColorStop(.35,'rgba(0,229,255,'+fa*.35+')');gr.addColorStop(1,'rgba(0,255,136,0)');ctx.fillStyle=gr;ctx.fillRect(0,0,W,H);}
  if(p>.3){var vp=(p-.3)/.7,vr=vp*80,vg=ctx.createRadialGradient(cx,cy,0,cx,cy,vr);vg.addColorStop(0,'rgba(0,0,0,'+Math.min(vp*.9,.9)+')');vg.addColorStop(.5,'rgba(7,7,15,'+Math.min(vp*.5,.5)+')');vg.addColorStop(1,'rgba(7,7,15,0)');ctx.fillStyle=vg;ctx.beginPath();ctx.arc(cx,cy,vr,0,Math.PI*2);ctx.fill();}
  if(p>.35){var sa=(p-.35)/.65*.14;for(var y=0;y<H;y+=4){ctx.fillStyle='rgba(0,0,0,'+sa+')';ctx.fillRect(0,y,W,1);}}
  if(t<DUR){aid=requestAnimationFrame(frame);}else{ctx.fillStyle='#07070F';ctx.fillRect(0,0,W,H);if(dest&&dest!==null&&dest!=="null"){window.location.href=dest;}else{ov.style.opacity='0';ov.classList.remove('active');aid=null;dest=null;}}
}
window._portalGo=function(href,ox,oy,intense){if(aid)return;dest=href;ov.style.opacity='1';ov.classList.add('active');init(ox,oy,intense);t0=performance.now();aid=requestAnimationFrame(frame);};
document.addEventListener('click',function(e){
  var a=e.target.closest('a[href]');if(!a)return;
  var h=a.getAttribute('href');
  if(!h||h[0]==='#'||h.indexOf('://')>0||h.startsWith('mailto')||h.startsWith('javascript'))return;
  e.preventDefault();var r=a.getBoundingClientRect();if(window._fadeTo){window._fadeTo(h);}else{window.location.href=h;}
});
document.documentElement.style.opacity='0';document.documentElement.style.transition='opacity .38s ease';
window.addEventListener('load',function(){document.documentElement.style.opacity='1';});
setTimeout(function(){document.documentElement.style.opacity='1';},50);
})();
</script>
</body></html>"""

ABONNEMENTS_PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Abonnements — InsideYourMix</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#07070F;color:#F0F0F8;font-family:'DM Sans',sans-serif}
.bg-gradient{position:fixed;top:0;left:0;width:100%;height:100%;background:radial-gradient(ellipse at top,rgba(123,47,255,0.15) 0%,transparent 50%);z-index:0;pointer-events:none}
.nav{position:fixed;top:0;left:0;right:0;padding:20px 48px;display:flex;justify-content:space-between;align-items:center;z-index:100;background:rgba(7,7,15,0.6);backdrop-filter:blur(24px);border-bottom:1px solid rgba(255,255,255,0.05)}
.logo{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:20px;background:linear-gradient(90deg,#F0F0F8 0%,#7B2FFF 50%,#00E5FF 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;text-decoration:none}
.nav-cta{background:linear-gradient(90deg,#7B2FFF,#00E5FF);color:white;padding:10px 24px;border-radius:24px;text-decoration:none;font-weight:600;font-size:14px}
.content{max-width:1100px;margin:0 auto;padding:160px 48px 120px;position:relative;z-index:1}
h1{font-family:'Syne',sans-serif;font-size:clamp(40px,6vw,72px);font-weight:800;margin-bottom:24px;letter-spacing:-0.03em;text-align:center}
h1 span{background:linear-gradient(90deg,#7B2FFF,#00E5FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.intro{opacity:0.75;font-size:18px;line-height:1.7;margin-bottom:80px;text-align:center;max-width:600px;margin-left:auto;margin-right:auto}
.plans{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:24px;margin-bottom:64px}
.plan{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:24px;padding:40px 32px;position:relative}
.plan.featured{background:linear-gradient(135deg,rgba(123,47,255,0.15),rgba(0,229,255,0.05));border-color:rgba(123,47,255,0.4)}
.plan-badge{position:absolute;top:-12px;left:50%;transform:translateX(-50%);background:linear-gradient(90deg,#7B2FFF,#00E5FF);color:white;padding:4px 16px;border-radius:12px;font-size:12px;font-weight:700;white-space:nowrap}
.plan-name{font-family:'Syne',sans-serif;font-size:22px;font-weight:700;margin-bottom:8px}
.plan-price{font-family:'Syne',sans-serif;font-size:40px;font-weight:800;margin-bottom:4px;background:linear-gradient(90deg,#7B2FFF,#00E5FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.plan-period{opacity:0.5;font-size:14px;margin-bottom:24px}
.plan-features{list-style:none;margin-bottom:32px}
.plan-features li{padding:8px 0;opacity:0.75;font-size:15px;border-bottom:1px solid rgba(255,255,255,0.05)}
.plan-features li:before{content:"✓ ";color:#00FF88;font-weight:700}
.plan-cta{display:block;text-align:center;padding:14px 24px;border-radius:16px;font-weight:700;font-size:15px;text-decoration:none;background:linear-gradient(90deg,#7B2FFF,#00E5FF);color:white;opacity:0.7;cursor:default}
.plan-cta.coming{background:rgba(255,255,255,0.05);color:#F0F0F8}
.coming-soon{text-align:center;margin-top:32px;padding:24px;background:rgba(123,47,255,0.1);border:1px solid rgba(123,47,255,0.2);border-radius:16px;font-size:15px;opacity:0.8}

.nav{position:fixed;top:0;left:0;right:0;padding:20px 48px;display:flex;justify-content:space-between;align-items:center;z-index:100;background:rgba(7,7,15,0.6);backdrop-filter:blur(24px);border-bottom:1px solid rgba(255,255,255,0.05)}
.logo{font-family:'Syne',sans-serif;font-weight:800;font-size:20px;background:linear-gradient(90deg,#F0F0F8 0%,#7B2FFF 50%,#00E5FF 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;text-decoration:none}
.nav-right{display:flex;gap:28px;align-items:center}
.nav-cta{background:linear-gradient(135deg,#7B2FFF,#5020CC);color:white;padding:10px 24px;border-radius:24px;text-decoration:none;font-weight:600;font-size:14px;transition:all .2s;box-shadow:0 4px 20px rgba(123,47,255,.3)}
.nav-cta:hover{transform:translateY(-1px);box-shadow:0 8px 30px rgba(123,47,255,.5)}
.dropdown{position:relative}
.menu-btn{background:none;border:1px solid rgba(255,255,255,.18);border-radius:8px;padding:8px 12px;cursor:pointer;display:flex;flex-direction:column;gap:5px;transition:border-color .2s}
.menu-btn:hover{border-color:rgba(123,47,255,.5)}
.menu-btn span{display:block;width:22px;height:2px;background:#F0F0F8;border-radius:2px;transition:all .3s}
.menu-btn.open span:nth-child(1){transform:translateY(7px) rotate(45deg)}
.menu-btn.open span:nth-child(2){opacity:0;transform:scaleX(0)}
.menu-btn.open span:nth-child(3){transform:translateY(-7px) rotate(-45deg)}
.dropdown-menu{position:absolute;top:52px;right:0;background:rgba(12,12,22,.97);backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,.1);border-radius:18px;padding:12px;min-width:240px;display:none;flex-direction:column;gap:3px;z-index:1000;box-shadow:0 24px 60px rgba(0,0,0,.6)}
.dropdown-menu.open{display:flex}
.dropdown-item{color:#F0F0F8;text-decoration:none;padding:12px 16px;border-radius:10px;font-size:15px;font-weight:500;transition:background .2s}
.dropdown-item:hover{background:rgba(123,47,255,.2)}
.dropdown-divider{height:1px;background:rgba(255,255,255,.07);margin:8px 0}
.lang-selector{display:flex;gap:8px;padding:8px 16px;justify-content:center}
.lang-flag{font-size:22px;cursor:pointer;opacity:.65;transition:all .2s;border-radius:4px;padding:4px}
.lang-flag:hover{opacity:1;transform:scale(1.2)}
@media(max-width:640px){.nav{padding:12px 16px !important}.logo{font-size:17px !important}.nav-cta{padding:8px 16px;font-size:13px}}
</style>
</head>
<body>
<div class="bg-gradient"></div>
<nav class="nav">
<a href="/" class="logo">InsideYourMix</a>
<div class="nav-right">
<div class="dropdown">
<button class="menu-btn" id="menuBtn" onclick="toggleMenu()"><span></span><span></span><span></span></button>
<div class="dropdown-menu" id="dropdownMenu">
<a href="/how-it-works" class="dropdown-item">✦ How it works</a>
<a href="/why" class="dropdown-item">✦ Why InsideYourMix</a>
<a href="/abonnements" class="dropdown-item">✦ Abonnements</a>
<a href="/contact" class="dropdown-item">✦ Contact</a>
<div class="dropdown-divider"></div>
<a href="/login" class="dropdown-item" style="color:#00E5FF">→ Se connecter</a>
<a href="/register" class="dropdown-item" style="color:#00FF88;font-weight:700">✦ Creer un compte</a>
<div class="dropdown-divider"></div>
<div class="lang-selector">
<span onclick="setLang('fr')" class="lang-flag">🇫🇷</span>
<span onclick="setLang('en')" class="lang-flag">🇬🇧</span>
<span onclick="setLang('es')" class="lang-flag">🇪🇸</span>
<span onclick="setLang('de')" class="lang-flag">🇩🇪</span>
<span onclick="setLang('pt')" class="lang-flag">🇵🇹</span>
</div>
</div>
</div>
<a href="/analyze" class="nav-cta">Analyser →</a>
</div>
</nav>
<div class="content">
<h1>Simple, <span>transparent</span>.</h1>
<p class="intro">Commence gratuitement. Upgrade quand tu es pret.</p>
<div style="text-align:center;margin-bottom:48px">%%BANNER%%</div>
<div class="plans">

<div class="plan">
<div class="plan-name">Gratuit</div>
<div class="plan-price">0<span style="font-size:20px">€</span></div>
<div class="plan-period">pour toujours</div>
<ul class="plan-features">
<li>3 analyses par mois</li>
<li>Rapport IA complet</li>
<li>84 genres disponibles</li>
<li>Historique 10 analyses</li>
</ul>
<a href="/register" style="display:block;text-align:center;padding:14px 24px;border-radius:16px;font-weight:700;font-size:15px;text-decoration:none;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);color:#F0F0F8">Commencer gratuitement</a>
</div>

<div class="plan featured">
<div class="plan-badge">⚡ LANCEMENT</div>
<div class="plan-name">Starter</div>
<div class="plan-price">2,99<span style="font-size:20px">€</span></div>
<div class="plan-period">par mois · sans engagement</div>
<ul class="plan-features">
<li>20 analyses par mois</li>
<li>Rapport IA complet</li>
<li>84 genres + sous-genres</li>
<li>Mode références</li>
<li>Historique illimité</li>
</ul>
%%BTN_STARTER%%
</div>

<div class="plan featured">
<div class="plan-badge">🔥 POPULAIRE</div>
<div class="plan-name">Pro</div>
<div class="plan-price">4,99<span style="font-size:20px">€</span></div>
<div class="plan-period">par mois · sans engagement</div>
<ul class="plan-features">
<li>100 analyses par mois</li>
<li>Rapport IA complet</li>
<li>84 genres + sous-genres</li>
<li>Mode références avancé</li>
<li>Historique illimité</li>
<li>Priorité support</li>
</ul>
%%BTN_PRO%%
</div>

<div class="plan" style="opacity:0.6">
<div class="plan-badge" style="background:rgba(255,255,255,0.1);color:#8888AA">BIENTÔT</div>
<div class="plan-name">Studio</div>
<div class="plan-price">?<span style="font-size:20px">€</span></div>
<div class="plan-period">analyses illimitées</div>
<ul class="plan-features">
<li>Analyses illimitées</li>
<li>Multi-utilisateurs</li>
<li>Acces API</li>
<li>Support prioritaire</li>
<li>InsideYourMaster inclus</li>
</ul>
<span style="display:block;text-align:center;padding:14px 24px;border-radius:16px;background:rgba(255,255,255,0.04);color:#8888AA;font-weight:600;font-size:14px">À venir prochainement</span>
</div>

</div>
%%MANAGE_BTN%%
<div class="coming-soon">Systeme de comptes et paiement en ligne — <strong>Bientot disponible</strong></div>
</div><script>
function toggleMenu(){
  var m=document.getElementById('dropdownMenu');
  var b=document.getElementById('menuBtn');
  m.classList.toggle('open');
  b.classList.toggle('open');
}
document.addEventListener('click',function(e){
  if(!e.target.closest('.dropdown')){
    var d=document.getElementById('dropdownMenu');
    var b=document.getElementById('menuBtn');
    if(d)d.classList.remove('open');
    if(b)b.classList.remove('open');
  }
});
function setLang(l){alert('Langue '+l+' - bientot disponible !');}
</script>
<p style="text-align:center;font-size:11px;font-style:italic;color:rgba(136,136,170,0.45);padding:40px 20px 20px;line-height:1.8">J'ai conçu cet outil avec passion dans une démarche purement technique. Le principal reste de s'amuser et de rester créatif. <em>— Loïc</em> · <a href="/privacy" style="color:rgba(136,136,170,0.5);text-decoration:none">Privacy Policy</a></p>
<div id="portalOverlay"><canvas id="portalCanvas"></canvas></div>
<style>#portalOverlay{position:fixed;inset:0;z-index:99999;pointer-events:none;opacity:0}#portalOverlay.active{pointer-events:all}#portalCanvas{position:absolute;inset:0;width:100%;height:100%}</style>
<script>
(function(){
var ov=document.getElementById('portalOverlay'),cv=document.getElementById('portalCanvas'),ctx=cv.getContext('2d');
var W,H,cx,cy,rings=[],beams=[],parts=[],aid=null,t0=0,dest=null,DUR=780;
var C=['#7B2FFF','#00E5FF','#00FF88','#B44FFF','#00FFCC','#FF4488'];
function resize(){W=cv.width=window.innerWidth;H=cv.height=window.innerHeight;}
window.addEventListener('resize',resize);resize();
function init(ox,oy,intense){
  cx=ox||W/2;cy=oy||H/2;rings=[];beams=[];parts=[];
  var nb=intense?12:8,nb2=intense?48:32,nb3=intense?90:60;
  for(var i=0;i<nb;i++)rings.push({d:i*50,col:C[i%C.length],w:1.5+i*.6});
  for(var i=0;i<nb2;i++){var a=i/nb2*Math.PI*2,dist=Math.sqrt(W*W+H*H)*(intense?.75:.6);beams.push({a:a,dist:dist,col:C[i%C.length],w:.5+Math.random()*1.8,d:i*14});}
  for(var i=0;i<nb3;i++){var a=Math.random()*Math.PI*2,s=3+Math.random()*(intense?10:6);parts.push({x:cx,y:cy,vx:Math.cos(a)*s,vy:Math.sin(a)*s,r:1.5+Math.random()*3.5,life:1,col:C[Math.floor(Math.random()*C.length)]});}
}
function frame(ts){
  var t=ts-t0,p=Math.min(t/DUR,1),ease=p<.5?2*p*p:1-Math.pow(-2*p+2,2)/2;
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle='rgba(7,7,15,'+Math.min(ease*.95,.95)+')';ctx.fillRect(0,0,W,H);
  beams.forEach(function(b){
    var bt=Math.max(0,(t-b.d)/DUR);if(!bt)return;
    var alpha=Math.min(bt*3,.88)*(1-ease*.35),startD=b.dist*(1-Math.min(bt*2.2,1));
    var sx=cx+Math.cos(b.a)*b.dist,sy=cy+Math.sin(b.a)*b.dist,ex=cx+Math.cos(b.a)*startD,ey=cy+Math.sin(b.a)*startD;
    var g=ctx.createLinearGradient(sx,sy,ex,ey);
    g.addColorStop(0,b.col+'00');g.addColorStop(.55,b.col+Math.floor(alpha*160).toString(16).padStart(2,'0'));g.addColorStop(1,b.col+Math.floor(alpha*255).toString(16).padStart(2,'0'));
    ctx.beginPath();ctx.moveTo(sx,sy);ctx.lineTo(ex,ey);ctx.strokeStyle=g;ctx.lineWidth=b.w*(1+ease*2.5);ctx.stroke();
  });
  var maxR=Math.sqrt(W*W+H*H);
  rings.forEach(function(r){
    var rt=Math.max(0,(t-r.d)/DUR);if(!rt)return;
    var alpha=Math.min(rt*4,1)*(1-rt*.75);if(alpha<.01)return;
    var radius=maxR*rt*.85;
    ctx.beginPath();ctx.arc(cx,cy,radius,0,Math.PI*2);ctx.strokeStyle=r.col+Math.floor(alpha*230).toString(16).padStart(2,'0');ctx.lineWidth=r.w*(1+ease*2);ctx.stroke();
    if(radius>20){ctx.beginPath();ctx.arc(cx,cy,radius*.86,0,Math.PI*2);ctx.strokeStyle=r.col+Math.floor(alpha*70).toString(16).padStart(2,'0');ctx.lineWidth=r.w*.35;ctx.stroke();}
  });
  parts.forEach(function(p2){p2.x+=p2.vx*(1+ease*4);p2.y+=p2.vy*(1+ease*4);p2.life-=.016;if(p2.life<.05)return;ctx.beginPath();ctx.arc(p2.x,p2.y,p2.r*p2.life,0,Math.PI*2);ctx.fillStyle=p2.col+Math.floor(p2.life*190).toString(16).padStart(2,'0');ctx.fill();});
  if(p>.52&&p<.78){var fp=(p-.52)/.26,fa=fp<.5?fp*2:(1-fp)*2,gr=ctx.createRadialGradient(cx,cy,0,cx,cy,fa*160);gr.addColorStop(0,'rgba(160,80,255,'+fa*.7+')');gr.addColorStop(.35,'rgba(0,229,255,'+fa*.35+')');gr.addColorStop(1,'rgba(0,255,136,0)');ctx.fillStyle=gr;ctx.fillRect(0,0,W,H);}
  if(p>.3){var vp=(p-.3)/.7,vr=vp*80,vg=ctx.createRadialGradient(cx,cy,0,cx,cy,vr);vg.addColorStop(0,'rgba(0,0,0,'+Math.min(vp*.9,.9)+')');vg.addColorStop(.5,'rgba(7,7,15,'+Math.min(vp*.5,.5)+')');vg.addColorStop(1,'rgba(7,7,15,0)');ctx.fillStyle=vg;ctx.beginPath();ctx.arc(cx,cy,vr,0,Math.PI*2);ctx.fill();}
  if(p>.35){var sa=(p-.35)/.65*.14;for(var y=0;y<H;y+=4){ctx.fillStyle='rgba(0,0,0,'+sa+')';ctx.fillRect(0,y,W,1);}}
  if(t<DUR){aid=requestAnimationFrame(frame);}else{ctx.fillStyle='#07070F';ctx.fillRect(0,0,W,H);if(dest&&dest!==null&&dest!=="null"){window.location.href=dest;}else{ov.style.opacity='0';ov.classList.remove('active');aid=null;dest=null;}}
}
window._portalGo=function(href,ox,oy,intense){if(aid)return;dest=href;ov.style.opacity='1';ov.classList.add('active');init(ox,oy,intense);t0=performance.now();aid=requestAnimationFrame(frame);};
document.addEventListener('click',function(e){
  var a=e.target.closest('a[href]');if(!a)return;
  var h=a.getAttribute('href');
  if(!h||h[0]==='#'||h.indexOf('://')>0||h.startsWith('mailto')||h.startsWith('javascript'))return;
  e.preventDefault();var r=a.getBoundingClientRect();if(window._fadeTo){window._fadeTo(h);}else{window.location.href=h;}
});
document.documentElement.style.opacity='0';document.documentElement.style.transition='opacity .38s ease';
window.addEventListener('load',function(){document.documentElement.style.opacity='1';});
setTimeout(function(){document.documentElement.style.opacity='1';},50);
})();
</script>
</body></html>"""

HTML_PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>InsideYourMix - Analyse ton mix</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237B2FFF'/%3E%3Crect x='5' y='18' width='3' height='9' rx='1.5' fill='white'/%3E%3Crect x='10' y='12' width='3' height='15' rx='1.5' fill='%2300E5FF'/%3E%3Crect x='15' y='8' width='3' height='19' rx='1.5' fill='white'/%3E%3Crect x='20' y='14' width='3' height='13' rx='1.5' fill='%2300FF88'/%3E%3Crect x='25' y='10' width='3' height='17' rx='1.5' fill='white'/%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=DM+Sans:wght@400;500;600&family=Space+Grotesk:wght@700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--v:#7B2FFF;--c:#00E5FF;--g:#00FF88;--o:#FF6B35;--p:#FF4488;--n:#07070F;--n2:#0F0F1A;--w:#F0F0F8;--gr:#8888AA}
body{background:var(--n);color:var(--w);font-family:'DM Sans',sans-serif;overflow-x:hidden}

/* BACKGROUNDS */
.bg-glow-a{position:fixed;top:-25%;left:-10%;width:75%;height:75%;background:radial-gradient(ellipse,rgba(123,47,255,0.22) 0%,transparent 60%);z-index:0;pointer-events:none;filter:blur(48px);animation:floatA 16s ease-in-out infinite}
.bg-glow-b{position:fixed;bottom:-20%;right:-8%;width:65%;height:65%;background:radial-gradient(ellipse,rgba(0,229,255,0.16) 0%,transparent 58%);z-index:0;pointer-events:none;filter:blur(55px);animation:floatB 21s ease-in-out infinite}
.bg-glow-c{position:fixed;top:35%;left:25%;width:50%;height:50%;background:radial-gradient(ellipse,rgba(0,255,136,0.09) 0%,transparent 65%);z-index:0;pointer-events:none;filter:blur(70px);animation:floatA 13s ease-in-out infinite reverse}
.bg-glow-d{position:fixed;top:10%;right:15%;width:35%;height:35%;background:radial-gradient(ellipse,rgba(123,47,255,0.12) 0%,transparent 60%);z-index:0;pointer-events:none;filter:blur(40px);animation:floatB 18s ease-in-out infinite reverse}
.bg-beam{position:fixed;top:-40%;left:35%;width:18%;height:180%;background:linear-gradient(180deg,transparent 0%,rgba(123,47,255,0.05) 25%,rgba(0,229,255,0.07) 50%,rgba(0,255,136,0.04) 75%,transparent 100%);transform-origin:top center;pointer-events:none;z-index:0;filter:blur(28px);animation:beam 28s ease-in-out infinite}
@keyframes floatA{0%,100%{transform:translate(0,0) scale(1);opacity:.8}25%{transform:translate(7%,-9%) scale(1.18);opacity:1}55%{transform:translate(3%,7%) scale(.88);opacity:.6}75%{transform:translate(-5%,2%) scale(1.08);opacity:.9}}
@keyframes floatB{0%,100%{transform:translate(0,0) scale(1);opacity:.7}33%{transform:translate(-8%,6%) scale(1.14);opacity:1}66%{transform:translate(6%,-7%) scale(.82);opacity:.5}}
@keyframes beam{0%,100%{left:15%;opacity:.35;transform:rotate(-22deg) scaleX(1)}20%{left:45%;opacity:.7;transform:rotate(-4deg) scaleX(1.3)}45%{left:65%;opacity:.45;transform:rotate(12deg) scaleX(.8)}70%{left:30%;opacity:.85;transform:rotate(-16deg) scaleX(1.15)}}
.freq-grid{position:fixed;inset:0;z-index:0;pointer-events:none;background-image:linear-gradient(rgba(123,47,255,0.025) 1px,transparent 1px),linear-gradient(90deg,rgba(123,47,255,0.025) 1px,transparent 1px);background-size:64px 64px;-webkit-mask-image:radial-gradient(ellipse 80% 80% at 50% 50%,transparent 30%,black 100%);mask-image:radial-gradient(ellipse 80% 80% at 50% 50%,transparent 30%,black 100%)}

/* NAV */
.nav{position:fixed;top:0;left:0;right:0;padding:20px 48px;display:flex;justify-content:space-between;align-items:center;z-index:100;background:rgba(7,7,15,0.6);backdrop-filter:blur(24px);border-bottom:1px solid rgba(255,255,255,0.05);transition:background .3s}
.logo{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:20px;background:linear-gradient(90deg,#F0F0F8 0%,#7B2FFF 50%,#00E5FF 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;letter-spacing:-0.03em;text-decoration:none}
.nav-right{display:flex;gap:28px;align-items:center}
.nav-cta{background:linear-gradient(135deg,var(--v),#5020CC);color:white;padding:10px 24px;border-radius:24px;text-decoration:none;font-weight:600;font-size:14px;transition:all .2s;box-shadow:0 4px 20px rgba(123,47,255,0.3)}
.nav-cta:hover{transform:translateY(-1px);box-shadow:0 8px 30px rgba(123,47,255,0.5)}

/* HERO */
.hero{min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:120px 24px 80px;position:relative;z-index:1}
.badge{display:inline-flex;align-items:center;gap:8px;padding:8px 18px;background:rgba(123,47,255,0.12);border:1px solid rgba(123,47,255,0.35);border-radius:100px;font-size:12px;font-weight:600;color:var(--c);margin-bottom:36px;letter-spacing:0.08em;text-transform:uppercase}
.badge-dot{width:6px;height:6px;border-radius:50%;background:var(--g);animation:blink 2s ease-in-out infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.hero h1{font-family:'Space Grotesk',sans-serif;font-size:clamp(44px,8vw,96px);font-weight:800;line-height:1.02;margin-bottom:28px;letter-spacing:-0.04em;max-width:1100px}
.hero h1 .accent{background:linear-gradient(135deg,var(--v),var(--c));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hero h1 .accent-warm{background:linear-gradient(135deg,var(--o),var(--p));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hero p{font-size:clamp(16px,1.5vw,19px);max-width:620px;margin-bottom:48px;opacity:0.72;line-height:1.65;font-weight:400}
.hero-cta{display:inline-flex;align-items:center;gap:12px;background:linear-gradient(135deg,var(--v),#5020CC);color:white;padding:20px 48px;border-radius:32px;text-decoration:none;font-weight:700;font-size:18px;box-shadow:0 10px 40px rgba(123,47,255,0.45);transition:all .3s;position:relative;overflow:hidden}
.hero-cta::before{content:"";position:absolute;inset:0;background:linear-gradient(135deg,rgba(255,107,53,0.15),rgba(255,68,136,0.1));opacity:0;transition:opacity .3s}
.hero-cta:hover{transform:translateY(-2px);box-shadow:0 20px 60px rgba(123,47,255,0.6)}
.hero-cta:hover::before{opacity:1}
.hero-cta svg{width:20px;height:20px}
.hero-note{margin-top:20px;font-size:13px;opacity:0.45;display:flex;align-items:center;gap:6px;justify-content:center}
.hero-note::before{content:"";width:4px;height:4px;border-radius:50%;background:var(--g)}

/* STATS BAND */
.stats-band{position:relative;z-index:1;padding:0 48px 80px;max-width:600px;margin:0 auto}
.stats-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}
.stat-card{padding:32px 24px;text-align:center;background:rgba(123,47,255,.07);border:1px solid rgba(123,47,255,.2);border-radius:20px;transition:all .3s}
.stat-card:hover{background:rgba(123,47,255,.12);transform:translateY(-2px);box-shadow:0 8px 30px rgba(123,47,255,.15)}
.stat-num{font-family:'Space Grotesk',sans-serif;font-size:48px;font-weight:800;display:block;margin-bottom:8px;background:linear-gradient(135deg,var(--v),var(--c));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat-label{font-size:13px;opacity:.6;line-height:1.4}

/* MODES */
.modes{padding:80px 48px 120px;position:relative;z-index:1;max-width:1400px;margin:0 auto}
.section-label{font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:var(--v);font-family:'Syne',sans-serif;font-weight:700;margin-bottom:12px;text-align:center}
.section-title{font-family:'Space Grotesk',sans-serif;font-size:clamp(30px,5vw,52px);font-weight:800;text-align:center;margin-bottom:20px;letter-spacing:-0.03em;line-height:1.1}
.section-subtitle{text-align:center;opacity:.55;font-size:17px;margin-bottom:64px;max-width:580px;margin-left:auto;margin-right:auto;line-height:1.6}
.modes-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px}
.mode-card{border-radius:24px;padding:40px 32px;transition:all .3s;position:relative;overflow:hidden;border:1px solid rgba(255,255,255,.07)}
.mode-card::before{content:"";position:absolute;inset:0;opacity:0;transition:opacity .3s;border-radius:inherit}
.mode-card:hover{transform:translateY(-5px)}
.mode-card:hover::before{opacity:1}
.mode-card-1{background:linear-gradient(160deg,rgba(123,47,255,.1),rgba(123,47,255,.03))}
.mode-card-1::before{background:linear-gradient(160deg,rgba(123,47,255,.18),rgba(0,229,255,.06))}
.mode-card-1:hover{border-color:rgba(123,47,255,.5);box-shadow:0 12px 50px rgba(123,47,255,.2)}
.mode-card-2{background:linear-gradient(160deg,rgba(0,229,255,.08),rgba(0,229,255,.02))}
.mode-card-2::before{background:linear-gradient(160deg,rgba(0,229,255,.15),rgba(0,255,136,.06))}
.mode-card-2:hover{border-color:rgba(0,229,255,.4);box-shadow:0 12px 50px rgba(0,229,255,.15)}
.mode-card-3{background:linear-gradient(160deg,rgba(255,107,53,.08),rgba(255,68,136,.03))}
.mode-card-3::before{background:linear-gradient(160deg,rgba(255,107,53,.15),rgba(255,68,136,.08))}
.mode-card-3:hover{border-color:rgba(255,107,53,.4);box-shadow:0 12px 50px rgba(255,107,53,.15)}
.mode-icon-wrap{display:none}
.mode-num{font-family:'Space Grotesk',sans-serif;font-size:11px;font-weight:700;letter-spacing:.2em;text-transform:uppercase;margin-bottom:24px;opacity:.5}
.mode-card-1 .mode-num{color:var(--v)}
.mode-card-2 .mode-num{color:var(--c)}
.mode-card-3 .mode-num{color:var(--o)}
.mode-card::after{content:"";position:absolute;top:0;left:0;right:0;height:2px;border-radius:2px 2px 0 0;opacity:.6}
.mode-card-1::after{background:linear-gradient(90deg,var(--v),var(--c))}
.mode-card-2::after{background:linear-gradient(90deg,var(--c),var(--g))}
.mode-card-3::after{background:linear-gradient(90deg,var(--o),var(--p))}
.mode-card h3{font-family:'Space Grotesk',sans-serif;font-size:22px;font-weight:700;margin-bottom:12px}
.mode-card p{opacity:.65;line-height:1.65;font-size:15px}
.mode-tag{display:inline-block;margin-top:20px;font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;padding:4px 10px;border-radius:100px}
.mode-card-1 .mode-tag{color:var(--v);background:rgba(123,47,255,.1);border:1px solid rgba(123,47,255,.2)}
.mode-card-2 .mode-tag{color:var(--c);background:rgba(0,229,255,.08);border:1px solid rgba(0,229,255,.2)}
.mode-card-3 .mode-tag{color:var(--o);background:rgba(255,107,53,.08);border:1px solid rgba(255,107,53,.2)}

/* WHY */
.why{padding:0 48px 120px;position:relative;z-index:1;max-width:1200px;margin:0 auto}
.why-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:4px;background:rgba(255,255,255,.03);border-radius:20px;overflow:hidden;border:1px solid rgba(255,255,255,.05)}
.why-item{padding:40px 32px;background:var(--n);transition:background .3s;position:relative}
.why-item:hover{background:rgba(123,47,255,.05)}
.why-accent{width:36px;height:3px;border-radius:2px;margin-bottom:20px}
.why-item:nth-child(1) .why-accent{background:linear-gradient(90deg,var(--v),var(--c))}
.why-item:nth-child(2) .why-accent{background:linear-gradient(90deg,var(--c),var(--g))}
.why-item:nth-child(3) .why-accent{background:linear-gradient(90deg,var(--o),var(--p))}
.why-item:nth-child(4) .why-accent{background:linear-gradient(90deg,var(--g),var(--c))}
.why-item h4{font-family:'Space Grotesk',sans-serif;font-size:19px;margin-bottom:12px;font-weight:700}
.why-item p{opacity:.6;line-height:1.65;font-size:15px}

/* FINAL CTA */
.final-cta{padding:100px 48px;text-align:center;position:relative;z-index:1}
.final-cta-inner{max-width:700px;margin:0 auto;background:linear-gradient(135deg,rgba(123,47,255,.1),rgba(0,229,255,.05));border:1px solid rgba(123,47,255,.2);border-radius:32px;padding:64px 48px}
.final-cta h2{font-family:'Space Grotesk',sans-serif;font-size:clamp(32px,5vw,56px);font-weight:800;margin-bottom:20px;letter-spacing:-0.03em;line-height:1.1}
.final-cta p{opacity:.65;font-size:17px;margin-bottom:40px;line-height:1.6}

/* FOOTER */
footer{padding:48px;text-align:center;position:relative;z-index:1;border-top:1px solid rgba(255,255,255,.05)}
.footer-logo{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:18px;background:linear-gradient(90deg,var(--v),var(--c));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:10px}
.footer-tagline{font-size:14px;opacity:.5;margin-bottom:24px;font-style:italic}
.footer-links{display:flex;gap:24px;justify-content:center;flex-wrap:wrap;margin-bottom:24px}
.footer-links a{color:var(--gr);text-decoration:none;font-size:13px;transition:color .2s}
.footer-links a:hover{color:var(--w)}
.footer-copy{font-size:12px;opacity:.3}

/* SCROLL REVEAL */
.reveal{opacity:0;transform:translateY(24px);transition:opacity .7s ease,transform .7s ease}
.reveal.visible{opacity:1;transform:translateY(0)}
.reveal-delay-1{transition-delay:.1s}
.reveal-delay-2{transition-delay:.2s}
.reveal-delay-3{transition-delay:.3s}

/* DROPDOWN */
.dropdown{position:relative}
.menu-btn{background:none;border:1px solid rgba(255,255,255,.18);border-radius:8px;padding:8px 12px;cursor:pointer;display:flex;flex-direction:column;gap:5px;transition:border-color .2s}
.menu-btn:hover{border-color:rgba(123,47,255,.5)}
.menu-btn span{display:block;width:22px;height:2px;background:#F0F0F8;border-radius:2px;transition:all .3s}
.menu-btn.open span:nth-child(1){transform:translateY(7px) rotate(45deg)}
.menu-btn.open span:nth-child(2){opacity:0;transform:scaleX(0)}
.menu-btn.open span:nth-child(3){transform:translateY(-7px) rotate(-45deg)}
.dropdown-menu{position:absolute;top:52px;right:0;background:rgba(12,12,22,.97);backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,.1);border-radius:18px;padding:12px;min-width:240px;display:none;flex-direction:column;gap:3px;z-index:1000;box-shadow:0 24px 60px rgba(0,0,0,.6)}
.dropdown-menu.open{display:flex}
.dropdown-item{color:#F0F0F8;text-decoration:none;padding:12px 16px;border-radius:10px;font-size:15px;font-weight:500;transition:background .2s}
.dropdown-item:hover{background:rgba(123,47,255,.2)}
.dropdown-divider{height:1px;background:rgba(255,255,255,.07);margin:8px 0}
.lang-selector{display:flex;gap:8px;padding:8px 16px;justify-content:center}
.lang-flag{font-size:22px;cursor:pointer;opacity:.65;transition:all .2s;border-radius:4px;padding:4px}
.lang-flag:hover{opacity:1;transform:scale(1.2)}

/* MOBILE */
@media(max-width:768px){.nav{padding:16px 20px}.modes,.why,.final-cta{padding-left:20px;padding-right:20px}.stats-band{padding-left:20px;padding-right:20px}}
@media(max-width:640px){
  .hero h1{font-size:clamp(30px,10vw,44px)!important}
  .hero p{font-size:15px}
  .hero-cta{padding:16px 32px;font-size:16px}
  .stats-grid{grid-template-columns:repeat(2,1fr)}
  .stat-card::after{display:none}
  .modes-grid{grid-template-columns:1fr}
  .why-grid{grid-template-columns:1fr}
  .final-cta-inner{padding:40px 24px}
  .footer-links{gap:16px}
}
</style>
</head>
<body>
<div class="bg-glow-a"></div>
<div class="bg-glow-b"></div>
<div class="bg-glow-c"></div>
<div class="bg-glow-d"></div>
<div class="bg-beam"></div>
<div class="freq-grid"></div>
<video autoplay muted loop playsinline style="position:fixed;top:0;left:0;width:100%;height:100%;object-fit:cover;z-index:0;opacity:0.12;pointer-events:none">
<source src="https://videos.pexels.com/video-files/7087635/7087635-uhd_1440_2732_25fps.mp4" type="video/mp4">
</video>

<nav class="nav">
<a href="/" class="logo">InsideYourMix</a>
<div class="nav-right">
<div class="dropdown">
<button class="menu-btn" id="menuBtn" onclick="toggleMenu()"><span></span><span></span><span></span></button>
<div class="dropdown-menu" id="dropdownMenu">
<a href="/how-it-works" class="dropdown-item">✦ How it works</a>
<a href="/why" class="dropdown-item">✦ Why InsideYourMix</a>
<a href="/abonnements" class="dropdown-item">✦ Abonnements</a>
<a href="/contact" class="dropdown-item">✦ Contact</a>
<div class="dropdown-divider"></div>
<a href="/login" class="dropdown-item" style="color:#00E5FF">→ Se connecter</a>
<a href="/register" class="dropdown-item" style="color:#00FF88;font-weight:700">✦ Creer un compte</a>
<div class="dropdown-divider"></div>
<div class="lang-selector">
<span onclick="setLang('fr')" class="lang-flag">🇫🇷</span>
<span onclick="setLang('en')" class="lang-flag">🇬🇧</span>
<span onclick="setLang('es')" class="lang-flag">🇪🇸</span>
<span onclick="setLang('de')" class="lang-flag">🇩🇪</span>
<span onclick="setLang('pt')" class="lang-flag">🇵🇹</span>
</div>
</div>
</div>
<a href="/analyze" class="nav-cta">Analyser →</a>
</div>
</nav>

<section class="hero">
<div class="badge"><span class="badge-dot"></span>AI Mix Analysis</div>
<h1>Analyse ton <span class="accent">MIX</span>.<br>Perfectionne ton <span class="accent">SON</span>.</h1>
<p>Upload ton mix, choisis ton style. Recois un rapport technique ultra-precis qui te dit exactement sur quoi travailler pour atteindre les standards de l'industrie.</p>
<a href="/analyze" class="hero-cta">
Analyser mon mix
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
</a>
<div class="hero-note">Gratuit · Aucune inscription requise · 3 analyses offertes</div>
</section>

<div class="stats-band reveal">
<div class="stats-grid">
<div class="stat-card"><span class="stat-num" data-count="7" data-suffix="">0</span><span class="stat-label">Dimensions analysees</span></div>
<div class="stat-card"><span class="stat-num" data-count="100" data-suffix="+">0</span><span class="stat-label">Genres de references</span></div>
</div>
</div>

<section class="modes reveal">
<div class="section-label">Comment ca marche</div>
<h2 class="section-title">3 modes d'analyse</h2>
<p class="section-subtitle">Choisis l'approche qui correspond a ton workflow et ton objectif</p>
<div class="modes-grid">
<div class="mode-card mode-card-1">
<div class="mode-num">Mode 01</div>
<h3>Mode Genre</h3>
<p>Compare ton mix aux standards techniques de ton style musical. Plus de 100 genres analyses — Techno, House, Hip-Hop, Drum & Bass, et bien plus.</p>
<span class="mode-tag">100+ genres</span>
</div>
<div class="mode-card mode-card-2 reveal-delay-1">
<div class="mode-num">Mode 02</div>
<h3>Mode Reference</h3>
<p>Upload tes morceaux preferes et recois une analyse comparative detaillee. Notre coach te montre exactement ce qui separe ton mix de tes references.</p>
<span class="mode-tag">Jusqu a 3 refs</span>
</div>
<div class="mode-card mode-card-3 reveal-delay-2">
<div class="mode-num">Mode 03</div>
<h3>Mode Hybride</h3>
<p>Le meilleur des deux mondes. Combine standards de genre et morceaux de reference pour une analyse ultime et un coaching sur-mesure.</p>
<span class="mode-tag">Ultra personnalise</span>
</div>
</div>
</section>

<section class="why reveal">
<div class="section-label">Pourquoi nous</div>
<h2 class="section-title">Made for producers, by producers</h2>
<p class="section-subtitle">Chaque detail est concu pour t'aider a progresser rapidement</p>
<div class="why-grid">
<div class="why-item">
<div class="why-accent"></div>
<h4>Analyse technique precise</h4>
<p>7 dimensions analysees — frequentiel, dynamique, stereo, rythme, timbre, espace, balance temporelle. Aucun detail ne t'echappe.</p>
</div>
<div class="why-item">
<div class="why-accent"></div>
<h4>Conseils actionnables</h4>
<p>Chaque rapport est personnalise. Notre coach IA te donne des pistes concretes adaptees a ton genre, ton niveau et tes references.</p>
</div>
<div class="why-item">
<div class="why-accent"></div>
<h4>100+ genres references</h4>
<p>De la Techno minimale a l'Amapiano en passant par le Dubstep — chaque style a ses propres standards techniques precis.</p>
</div>
<div class="why-item">
<div class="why-accent"></div>
<h4>Aucune installation</h4>
<p>Tout se passe dans ton navigateur. Upload, analyse, rapport. Simple, rapide, accessible depuis n'importe quel appareil.</p>
</div>
</div>
</section>

<section class="final-cta reveal">
<div class="final-cta-inner">
<h2>Pret a <span style="background:linear-gradient(135deg,var(--v),var(--c));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text">passer au niveau superieur</span> ?</h2>
<p>Decouvre ce que ton mix cache vraiment. Gratuit, instantane, sans inscription.</p>
<a href="/analyze" class="hero-cta">
Analyser mon mix maintenant
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
</a>
</div>
</section>

<footer>
<div class="footer-logo">InsideYourMix</div>
<div class="footer-tagline">Fait avec passion par un producteur, pour les producteurs.</div>
<div class="footer-links">
<a href="/why">Notre histoire</a>
<a href="/how-it-works">Comment ca marche</a>
<a href="/abonnements">Tarifs</a>
<a href="/contact">Contact</a>
<a href="https://instagram.com/insideyourmix" target="_blank">@insideyourmix</a>
</div>
<div class="footer-copy">© 2026 InsideYourMix · All rights reserved</div>
</footer>

<script>
// MENU
function toggleMenu(){
  var m=document.getElementById('dropdownMenu');
  var b=document.getElementById('menuBtn');
  m.classList.toggle('open');
  b.classList.toggle('open');
}
document.addEventListener('click',function(e){
  if(!e.target.closest('.dropdown')){
    document.getElementById('dropdownMenu').classList.remove('open');
    document.getElementById('menuBtn').classList.remove('open');
  }
});
function setLang(l){alert('Langue '+l+' - bientot disponible !');}

// STATS COUNTER
function animateCounter(el){
  var target=parseInt(el.dataset.count);
  var suffix=el.dataset.suffix||'';
  var duration=1800;
  var start=null;
  function step(ts){
    if(!start)start=ts;
    var p=Math.min((ts-start)/duration,1);
    var ease=1-Math.pow(1-p,3);
    el.textContent=Math.round(ease*target)+suffix;
    if(p<1)requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}
var statsObs=new IntersectionObserver(function(entries){
  entries.forEach(function(e){
    if(e.isIntersecting){
      e.target.querySelectorAll('.stat-num').forEach(animateCounter);
      statsObs.unobserve(e.target);
    }
  });
},{threshold:0.4});
document.querySelectorAll('.stats-band').forEach(function(el){statsObs.observe(el);});

// SCROLL REVEAL
var revealObs=new IntersectionObserver(function(entries){
  entries.forEach(function(e){
    if(e.isIntersecting) e.target.classList.add('visible');
  });
},{threshold:0.1,rootMargin:'0px 0px -40px 0px'});
document.querySelectorAll('.reveal').forEach(function(el){revealObs.observe(el);});

// Transitions gérées par portalOverlay global
</script>

<p style="text-align:center;font-size:11px;font-style:italic;color:rgba(136,136,170,0.45);padding:40px 20px 20px;line-height:1.8">J'ai conçu cet outil avec passion dans une démarche purement technique. Le principal reste de s'amuser et de rester créatif. <em>— Loïc</em> · <a href="/privacy" style="color:rgba(136,136,170,0.5);text-decoration:none">Privacy Policy</a></p>
<div id="portalOverlay"><canvas id="portalCanvas"></canvas></div>
<style>#portalOverlay{position:fixed;inset:0;z-index:99999;pointer-events:none;opacity:0}#portalOverlay.active{pointer-events:all}#portalCanvas{position:absolute;inset:0;width:100%;height:100%}</style>
<script>
(function(){
var ov=document.getElementById('portalOverlay'),cv=document.getElementById('portalCanvas'),ctx=cv.getContext('2d');
var W,H,cx,cy,rings=[],beams=[],parts=[],aid=null,t0=0,dest=null,DUR=780;
var C=['#7B2FFF','#00E5FF','#00FF88','#B44FFF','#00FFCC','#FF4488'];
function resize(){W=cv.width=window.innerWidth;H=cv.height=window.innerHeight;}
window.addEventListener('resize',resize);resize();
function init(ox,oy,intense){
  cx=ox||W/2;cy=oy||H/2;rings=[];beams=[];parts=[];
  var nb=intense?12:8,nb2=intense?48:32,nb3=intense?90:60;
  for(var i=0;i<nb;i++)rings.push({d:i*50,col:C[i%C.length],w:1.5+i*.6});
  for(var i=0;i<nb2;i++){var a=i/nb2*Math.PI*2,dist=Math.sqrt(W*W+H*H)*(intense?.75:.6);beams.push({a:a,dist:dist,col:C[i%C.length],w:.5+Math.random()*1.8,d:i*14});}
  for(var i=0;i<nb3;i++){var a=Math.random()*Math.PI*2,s=3+Math.random()*(intense?10:6);parts.push({x:cx,y:cy,vx:Math.cos(a)*s,vy:Math.sin(a)*s,r:1.5+Math.random()*3.5,life:1,col:C[Math.floor(Math.random()*C.length)]});}
}
function frame(ts){
  var t=ts-t0,p=Math.min(t/DUR,1),ease=p<.5?2*p*p:1-Math.pow(-2*p+2,2)/2;
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle='rgba(7,7,15,'+Math.min(ease*.95,.95)+')';ctx.fillRect(0,0,W,H);
  beams.forEach(function(b){
    var bt=Math.max(0,(t-b.d)/DUR);if(!bt)return;
    var alpha=Math.min(bt*3,.88)*(1-ease*.35),startD=b.dist*(1-Math.min(bt*2.2,1));
    var sx=cx+Math.cos(b.a)*b.dist,sy=cy+Math.sin(b.a)*b.dist,ex=cx+Math.cos(b.a)*startD,ey=cy+Math.sin(b.a)*startD;
    var g=ctx.createLinearGradient(sx,sy,ex,ey);
    g.addColorStop(0,b.col+'00');g.addColorStop(.55,b.col+Math.floor(alpha*160).toString(16).padStart(2,'0'));g.addColorStop(1,b.col+Math.floor(alpha*255).toString(16).padStart(2,'0'));
    ctx.beginPath();ctx.moveTo(sx,sy);ctx.lineTo(ex,ey);ctx.strokeStyle=g;ctx.lineWidth=b.w*(1+ease*2.5);ctx.stroke();
  });
  var maxR=Math.sqrt(W*W+H*H);
  rings.forEach(function(r){
    var rt=Math.max(0,(t-r.d)/DUR);if(!rt)return;
    var alpha=Math.min(rt*4,1)*(1-rt*.75);if(alpha<.01)return;
    var radius=maxR*rt*.85;
    ctx.beginPath();ctx.arc(cx,cy,radius,0,Math.PI*2);ctx.strokeStyle=r.col+Math.floor(alpha*230).toString(16).padStart(2,'0');ctx.lineWidth=r.w*(1+ease*2);ctx.stroke();
    if(radius>20){ctx.beginPath();ctx.arc(cx,cy,radius*.86,0,Math.PI*2);ctx.strokeStyle=r.col+Math.floor(alpha*70).toString(16).padStart(2,'0');ctx.lineWidth=r.w*.35;ctx.stroke();}
  });
  parts.forEach(function(p2){p2.x+=p2.vx*(1+ease*4);p2.y+=p2.vy*(1+ease*4);p2.life-=.016;if(p2.life<.05)return;ctx.beginPath();ctx.arc(p2.x,p2.y,p2.r*p2.life,0,Math.PI*2);ctx.fillStyle=p2.col+Math.floor(p2.life*190).toString(16).padStart(2,'0');ctx.fill();});
  if(p>.52&&p<.78){var fp=(p-.52)/.26,fa=fp<.5?fp*2:(1-fp)*2,gr=ctx.createRadialGradient(cx,cy,0,cx,cy,fa*160);gr.addColorStop(0,'rgba(160,80,255,'+fa*.7+')');gr.addColorStop(.35,'rgba(0,229,255,'+fa*.35+')');gr.addColorStop(1,'rgba(0,255,136,0)');ctx.fillStyle=gr;ctx.fillRect(0,0,W,H);}
  if(p>.3){var vp=(p-.3)/.7,vr=vp*80,vg=ctx.createRadialGradient(cx,cy,0,cx,cy,vr);vg.addColorStop(0,'rgba(0,0,0,'+Math.min(vp*.9,.9)+')');vg.addColorStop(.5,'rgba(7,7,15,'+Math.min(vp*.5,.5)+')');vg.addColorStop(1,'rgba(7,7,15,0)');ctx.fillStyle=vg;ctx.beginPath();ctx.arc(cx,cy,vr,0,Math.PI*2);ctx.fill();}
  if(p>.35){var sa=(p-.35)/.65*.14;for(var y=0;y<H;y+=4){ctx.fillStyle='rgba(0,0,0,'+sa+')';ctx.fillRect(0,y,W,1);}}
  if(t<DUR){aid=requestAnimationFrame(frame);}else{ctx.fillStyle='#07070F';ctx.fillRect(0,0,W,H);if(dest&&dest!==null&&dest!=="null"){window.location.href=dest;}else{ov.style.opacity='0';ov.classList.remove('active');aid=null;dest=null;}}
}
window._portalGo=function(href,ox,oy,intense){if(aid)return;dest=href;ov.style.opacity='1';ov.classList.add('active');init(ox,oy,intense);t0=performance.now();aid=requestAnimationFrame(frame);};
document.addEventListener('click',function(e){
  var a=e.target.closest('a[href]');if(!a)return;
  var h=a.getAttribute('href');
  if(!h||h[0]==='#'||h.indexOf('://')>0||h.startsWith('mailto')||h.startsWith('javascript'))return;
  e.preventDefault();var r=a.getBoundingClientRect();if(window._fadeTo){window._fadeTo(h);}else{window.location.href=h;}
});
document.documentElement.style.opacity='0';document.documentElement.style.transition='opacity .38s ease';
window.addEventListener('load',function(){document.documentElement.style.opacity='1';});
setTimeout(function(){document.documentElement.style.opacity='1';},50);
})();
</script>
</body>
</html>"""

# ═══════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ═══════════════════════════════════════════════════════════════════════════

def _render_auth(page, error='', success=''):
    is_register = (page == 'register')
    title      = "Creer un compte" if is_register else "Se connecter"
    action     = "/register" if is_register else "/login"
    btn        = "Creer mon compte" if is_register else "Se connecter"
    swap_text  = "Deja un compte ?" if is_register else "Pas encore de compte ?"
    swap_link  = "/login" if is_register else "/register"
    swap_btn   = "Se connecter" if is_register else "S'inscrire gratuitement"
    extra      = ('<div class="af"><label>Confirmer le mot de passe</label>'
                  '<input type="password" name="confirm" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;" required></div>') if is_register else ''
    err_html   = '<div class="auth-err">' + error + '</div>' if error else ''
    ok_html    = '<div class="auth-ok">' + success + '</div>' if success else ''
    sub        = "Rejoins des milliers de producteurs." if is_register else "Content de te revoir."

    css = (
        '*{margin:0;padding:0;box-sizing:border-box}'
        ':root{--v:#7B2FFF;--c:#00E5FF;--n:#07070F;--n2:#0F0F1A;--w:#F0F0F8;--gr:#8888AA}'
        'body{background:var(--n);color:var(--w);font-family:DM Sans,sans-serif;min-height:100vh;'
        'display:flex;flex-direction:column;align-items:center;justify-content:center;padding:20px}'
        '.bg1{position:fixed;top:-25%;left:-10%;width:75%;height:75%;'
        'background:radial-gradient(ellipse,rgba(123,47,255,0.2) 0%,transparent 60%);'
        'pointer-events:none;z-index:0;filter:blur(48px)}'
        '.bg2{position:fixed;bottom:-20%;right:-8%;width:65%;height:65%;'
        'background:radial-gradient(ellipse,rgba(0,229,255,0.14) 0%,transparent 58%);'
        'pointer-events:none;z-index:0;filter:blur(55px)}'
        '.card{position:relative;z-index:1;background:rgba(15,15,26,0.9);'
        'border:1px solid rgba(255,255,255,0.08);border-radius:20px;padding:40px 36px;'
        'width:100%;max-width:420px;backdrop-filter:blur(20px)}'
        '.logo{font-family:Space Grotesk,sans-serif;font-weight:800;font-size:18px;'
        'background:linear-gradient(90deg,#F0F0F8,#7B2FFF,#00E5FF);'
        '-webkit-background-clip:text;-webkit-text-fill-color:transparent;'
        'text-decoration:none;display:block;text-align:center;margin-bottom:28px}'
        'h1{font-family:Space Grotesk,sans-serif;font-size:22px;font-weight:800;text-align:center;margin-bottom:6px}'
        '.sub{color:var(--gr);font-size:13px;text-align:center;margin-bottom:28px}'
        '.af{margin-bottom:16px}'
        '.af label{font-size:12px;font-weight:600;color:var(--gr);letter-spacing:1px;'
        'text-transform:uppercase;display:block;margin-bottom:6px}'
        '.af input{width:100%;background:rgba(255,255,255,0.04);'
        'border:1px solid rgba(255,255,255,0.1);border-radius:10px;padding:12px 16px;'
        'color:var(--w);font-size:15px;font-family:DM Sans,sans-serif;outline:none;transition:border .2s}'
        '.af input:focus{border-color:rgba(123,47,255,0.5)}'
        '.auth-err{background:rgba(255,60,60,0.1);border:1px solid rgba(255,60,60,0.3);'
        'border-radius:10px;padding:12px 16px;font-size:13px;color:#FF6B6B;margin-bottom:16px}'
        '.auth-ok{background:rgba(0,255,136,0.08);border:1px solid rgba(0,255,136,0.25);'
        'border-radius:10px;padding:12px 16px;font-size:13px;color:#00FF88;margin-bottom:16px}'
        '.btn{width:100%;padding:14px;background:linear-gradient(135deg,#7B2FFF,#5020CC);'
        'border:none;border-radius:12px;color:white;font-family:Space Grotesk,sans-serif;'
        'font-size:15px;font-weight:700;cursor:pointer;margin-top:8px}'
        '.swap{text-align:center;margin-top:20px;font-size:13px;color:var(--gr)}'
        '.swap a{color:var(--c);text-decoration:none;font-weight:600}'
        '.back{display:block;text-align:center;font-size:12px;color:var(--gr);text-decoration:none;margin-top:16px}'
        '@media(max-width:480px){.card{padding:28px 20px}}'
    )

    return (
        '<!DOCTYPE html><html lang="fr"><head>'
        '<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>InsideYourMix — ' + title + '</title>'
        '<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500&family=Space+Grotesk:wght@700;800&display=swap" rel="stylesheet">'
        '<style>' + css + '</style>'
        '</head><body>'
        '<div class="bg1"></div><div class="bg2"></div>'
        '<div class="card">'
        '<a href="/" class="logo">InsideYourMix</a>'
        '<h1>' + title + '</h1>'
        '<p class="sub">' + sub + '</p>'
        + err_html + ok_html +
        '<form method="POST" action="' + action + '">'
        '<div class="af"><label>Email</label>'
        '<input type="email" name="email" placeholder="ton@email.com" required autocomplete="email"></div>'
        '<div class="af"><label>Mot de passe</label>'
        '<input type="password" name="password" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;" required></div>'
        + extra +
        '<button type="submit" class="btn">' + btn + '</button>'
        '</form>'
        '<div class="swap">' + swap_text + ' <a href="' + swap_link + '">' + swap_btn + '</a></div>'
        '<a href="/" class="back">\u2190 Retour a l\'accueil</a>'
        '</div>'
        + TRANSITION_HTML
        + '</body></html>'
    )

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('account'))
    if request.method == 'POST':
        email    = request.form.get('email', '').strip().lower()
        pwd      = request.form.get('password', '')
        confirm  = request.form.get('confirm', '')
        if not email or not pwd:
            return _render_auth('register', error='Email et mot de passe requis.')
        if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
            return _render_auth('register', error='Email invalide.')
        if len(pwd) < 6:
            return _render_auth('register', error='Mot de passe trop court (6 caractères minimum).')
        if pwd != confirm:
            return _render_auth('register', error='Les mots de passe ne correspondent pas.')
        if User.query.filter_by(email=email).first():
            return _render_auth('register', error='Un compte existe déjà avec cet email.')
        user = User(email=email, password_hash=generate_password_hash(pwd))
        db.session.add(user)
        db.session.commit()
        login_user(user, remember=True)
        session.permanent = True
        return redirect(url_for('account'))
    return _render_auth('register')

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('account'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        pwd   = request.form.get('password', '')
        user  = User.query.filter_by(email=email).first()
        if not user or not check_password_hash(user.password_hash, pwd):
            return _render_auth('login', error='Email ou mot de passe incorrect.')
        login_user(user, remember=True)
        session.permanent = True
        next_page = request.args.get('next')
        return redirect(next_page or url_for('account'))
    return _render_auth('login')

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/account')
def account():
    if not current_user.is_authenticated:
        return redirect(url_for('login_page'))
    recent    = Analysis.query.filter_by(user_id=current_user.id).order_by(Analysis.created_at.desc()).limit(10).all()
    success_banner = ''
    if request.args.get('success'):
        success_banner = '<div style="background:rgba(0,255,136,0.1);border:1px solid rgba(0,255,136,0.3);border-radius:12px;padding:16px 24px;text-align:center;color:#00FF88;margin-bottom:24px;font-weight:600">🎉 Paiement confirmé ! Ton plan a été activé.</div>'
    remaining = current_user.remaining()
    limit     = PLAN_LIMITS.get(current_user.plan, 3)
    used      = max(0, limit - remaining)
    pct       = int(used / limit * 100) if limit else 0
    pc_map    = {'free': '#8888AA', 'starter': '#00E5FF', 'pro': '#00FF88', 'studio': '#7B2FFF'}
    pc        = pc_map.get(current_user.plan, '#8888AA')
    rem_color = '#00FF88' if remaining > 5 else ('#FFB400' if remaining > 0 else '#FF3C3C')

    if recent:
        rows = ''.join(
            '<tr><td>' + (a.genre or '?') + '</td><td>' + str(a.score or '-') + '%</td>'
            + '<td style="color:#8888AA;font-size:12px">' + a.created_at.strftime('%d/%m %H:%M') + '</td></tr>'
            for a in recent
        )
    else:
        rows = '<tr><td colspan="3" style="text-align:center;color:#8888AA;padding:20px">Aucune analyse pour l instant</td></tr>'

    upgrade_html = ''
    if current_user.plan in ('free', 'starter'):
        upgrade_html = (
            '<div style="background:linear-gradient(135deg,rgba(123,47,255,0.15),rgba(0,229,255,0.08));'
            'border:1px solid rgba(123,47,255,0.3);border-radius:14px;padding:20px 24px;'
            'display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-top:24px">'
            '<div><div style="font-weight:700;margin-bottom:4px">Passe au niveau superieur</div>'
            '<div style="font-size:13px;color:#8888AA">Plus d analyses, meilleur coaching</div></div>'
            '<a href="/abonnements" style="background:linear-gradient(135deg,#7B2FFF,#5020CC);color:white;'
            'padding:10px 24px;border-radius:10px;font-weight:700;font-size:13px;text-decoration:none">Voir les offres</a></div>'
        )

    css = (
        '*{margin:0;padding:0;box-sizing:border-box}'
        ':root{--v:#7B2FFF;--c:#00E5FF;--n:#07070F;--n2:#0F0F1A;--w:#F0F0F8;--gr:#8888AA}'
        'body{background:var(--n);color:var(--w);font-family:DM Sans,sans-serif;min-height:100vh}'
        '.bg1{position:fixed;top:-25%;left:-10%;width:75%;height:75%;background:radial-gradient(ellipse,rgba(123,47,255,0.18) 0%,transparent 60%);pointer-events:none;z-index:0;filter:blur(48px)}'
        '.bg2{position:fixed;bottom:-20%;right:-8%;width:65%;height:65%;background:radial-gradient(ellipse,rgba(0,229,255,0.12) 0%,transparent 58%);pointer-events:none;z-index:0;filter:blur(55px)}'
        'nav{position:fixed;top:0;left:0;right:0;padding:18px 40px;display:flex;justify-content:space-between;align-items:center;z-index:100;background:rgba(7,7,15,0.7);backdrop-filter:blur(20px);border-bottom:1px solid rgba(255,255,255,0.05)}'
        '.logo{font-family:Space Grotesk,sans-serif;font-weight:800;font-size:18px;background:linear-gradient(90deg,#F0F0F8,#7B2FFF,#00E5FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;text-decoration:none}'
        '.wrap{max-width:860px;margin:0 auto;padding:100px 20px 80px;position:relative;z-index:1}'
        'h1{font-family:Space Grotesk,sans-serif;font-size:28px;font-weight:800;margin-bottom:6px}'
        '.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:28px}'
        '.card{background:var(--n2);border:1px solid rgba(255,255,255,0.06);border-radius:14px;padding:22px}'
        '.card-label{font-size:11px;letter-spacing:2px;text-transform:uppercase;color:var(--gr);margin-bottom:8px}'
        '.card-value{font-family:Space Grotesk,sans-serif;font-size:26px;font-weight:800}'
        '.qbar{height:6px;background:rgba(255,255,255,0.08);border-radius:3px;margin-top:10px;overflow:hidden}'
        '.qfill{height:100%;border-radius:3px;background:linear-gradient(90deg,var(--v),var(--c));width:' + str(pct) + '%}'
        'table{width:100%;border-collapse:collapse;background:var(--n2);border:1px solid rgba(255,255,255,0.06);border-radius:14px;overflow:hidden}'
        'th{font-size:11px;letter-spacing:2px;text-transform:uppercase;color:var(--gr);padding:14px 16px;text-align:left;border-bottom:1px solid rgba(255,255,255,0.06)}'
        'td{padding:12px 16px;font-size:14px;border-bottom:1px solid rgba(255,255,255,0.04)}'
        'tr:last-child td{border:none}'
        '@media(max-width:640px){nav{padding:14px 16px}.wrap{padding:80px 16px 60px}}'
    )

    html = (
        '<!DOCTYPE html><html lang="fr"><head>'
        '<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Mon compte — InsideYourMix</title>'
        '<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Sans:wght@400;500&family=Space+Grotesk:wght@700;800&display=swap" rel="stylesheet">'
        '<style>' + css + '</style>'
        '</head><body>'
        '<div class="bg1"></div><div class="bg2"></div>'
        '<nav>'
        '<a href="/" class="logo">InsideYourMix</a>'
        '<div style="display:flex;gap:16px;align-items:center">'
        '<a href="/analyze" style="color:#8888AA;font-size:13px;text-decoration:none">Analyser</a>'
        '<a href="/account" style="background:linear-gradient(135deg,#7B2FFF,#5020CC);color:white;padding:8px 20px;border-radius:20px;text-decoration:none;font-size:13px;font-weight:600">Mon compte</a>'
        '</div></nav>'
        '<div class="wrap">'
        '<h1>Mon compte</h1>'
        '<p style="color:#8888AA;font-size:14px;margin-bottom:24px">' + current_user.email + '</p>'
        + success_banner +
        '<div class="grid">'
        '<div class="card"><div class="card-label">Plan actuel</div>'
        '<div style="margin-top:6px;font-size:16px;font-weight:700;color:' + pc + '">' + current_user.plan_label() + '</div></div>'
        '<div class="card"><div class="card-label">Analyses restantes</div>'
        '<div class="card-value" style="color:' + rem_color + '">' + str(remaining) + '<span style="font-size:14px;color:#8888AA"> / ' + str(limit) + '</span></div>'
        '<div class="qbar"><div class="qfill"></div></div></div>'
        '<div class="card"><div class="card-label">Total analyses</div>'
        '<div class="card-value">' + str(len(current_user.analyses)) + '</div></div>'
        '<div class="card"><div class="card-label">Membre depuis</div>'
        '<div style="font-size:15px;font-weight:600;margin-top:4px">' + current_user.created_at.strftime('%B %Y') + '</div></div>'
        '</div>'
        '<div style="font-size:11px;letter-spacing:3px;text-transform:uppercase;color:#7B2FFF;margin-bottom:12px">Historique</div>'
        '<table>'
        '<thead><tr><th>Genre</th><th>Score</th><th>Date</th></tr></thead>'
        '<tbody>' + rows + '</tbody>'
        '</table>'
        + upgrade_html
        + '<br><a href="/logout" style="color:#8888AA;font-size:13px;text-decoration:none;margin-top:24px;display:inline-block">Se deconnecter</a>'
        '</div>'
        + TRANSITION_HTML
        + '</body></html>'
    )
    return html


@app.route('/api/me')
def api_me():
    if current_user.is_authenticated:
        return {'logged': True, 'email': current_user.email,
                'plan': current_user.plan, 'remaining': current_user.remaining()}
    return {'logged': False}


# ═══════════════════════════════════════════════════════════════════════════
# STRIPE ROUTES
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/checkout/<plan>')
def checkout(plan):
    if not current_user.is_authenticated:
        return redirect(url_for('register'))
    if not STRIPE_ENABLED or plan not in STRIPE_PRICES or not STRIPE_PRICES.get(plan):
        print(f'Checkout blocked: STRIPE_ENABLED={STRIPE_ENABLED}, plan={plan}, price={STRIPE_PRICES.get(plan)}')
        return redirect(url_for('abonnements'))
    if not stripe.api_key:
        print(f'Checkout blocked: stripe.api_key empty')
        return redirect(url_for('abonnements'))
    try:
        # Si l'utilisateur a déjà un abonnement actif → upgrade via le portail Stripe
        if current_user.stripe_sub_id and current_user.plan in ('starter', 'pro'):
            try:
                # Modifier l'abonnement existant directement
                sub = stripe.Subscription.retrieve(current_user.stripe_sub_id)
                stripe.Subscription.modify(
                    current_user.stripe_sub_id,
                    items=[{'id': sub['items']['data'][0]['id'], 'price': STRIPE_PRICES[plan]}],
                    proration_behavior='always_invoice',
                )
                # Mettre à jour le plan immédiatement
                current_user.plan = plan
                current_user.analyses_this_month = 0
                current_user.quota_reset_at = datetime.utcnow()
                db.session.commit()
                print(f'Upgrade vers {plan} pour {current_user.email}')
                return redirect(url_for('account') + '?success=1')
            except Exception as e:
                print(f'Upgrade error: {e}')
                # Fallback : portail Stripe
                if current_user.stripe_customer_id:
                    portal = stripe.billing_portal.Session.create(
                        customer=current_user.stripe_customer_id,
                        return_url=request.host_url + 'account',
                    )
                    return redirect(portal.url, code=303)

        # Créer ou récupérer le customer Stripe
        if current_user.stripe_customer_id:
            customer_id = current_user.stripe_customer_id
        else:
            customer = stripe.Customer.create(email=current_user.email)
            current_user.stripe_customer_id = customer.id
            db.session.commit()
            customer_id = customer.id

        session_stripe = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=['card'],
            line_items=[{'price': STRIPE_PRICES[plan], 'quantity': 1}],
            mode='subscription',
            success_url=request.host_url + 'payment/success?plan=' + plan + '&session_id={CHECKOUT_SESSION_ID}',
            cancel_url=request.host_url + 'abonnements?canceled=1',
            metadata={'user_id': current_user.id, 'plan': plan},
            locale='fr',
        )
        return redirect(session_stripe.url, code=303)
    except Exception as e:
        print('Stripe checkout error:', e)
        return redirect(url_for('abonnements'))


@app.route('/payment/success')
def payment_success():
    if not current_user.is_authenticated:
        return redirect(url_for('login_page'))
    plan       = request.args.get('plan')
    session_id = request.args.get('session_id')
    if plan and plan in PLAN_LIMITS and STRIPE_ENABLED:
        try:
            # Vérifier la session Stripe pour confirmer le paiement
            session_stripe = stripe.checkout.Session.retrieve(session_id)
            if session_stripe.payment_status == 'paid':
                current_user.plan                = plan
                current_user.stripe_sub_id       = session_stripe.subscription
                current_user.analyses_this_month = 0
                current_user.quota_reset_at      = datetime.utcnow()
                db.session.commit()
                print(f'Plan {plan} activé (via success page) pour {current_user.email}')
        except Exception as e:
            print(f'Payment success error: {e}')
    return redirect(url_for('account') + '?success=1')


@app.route('/webhook/test')
def webhook_test():
    return {'status': 'ok', 'stripe_enabled': STRIPE_ENABLED, 'webhook_secret_set': bool(STRIPE_WEBHOOK_SECRET)}, 200

@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    if not STRIPE_ENABLED:
        return '', 400
    payload    = request.get_data()
    sig_header = request.headers.get('Stripe-Signature', '')
    print(f'Webhook received: payload={len(payload)}bytes, sig={sig_header[:30]}...')
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        print(f'Webhook event: {event["type"]}')
    except Exception as e:
        print(f'Webhook construct_event error: {type(e).__name__}: {e}')
        return '', 400

    ev_type = event['type']
    data    = event['data']['object']

    # Paiement réussi → activer le plan
    if ev_type == 'checkout.session.completed':
        try:
            meta    = data.metadata or {}
            user_id = meta.get('user_id') if hasattr(meta, 'get') else getattr(meta, 'user_id', None)
            plan    = meta.get('plan') if hasattr(meta, 'get') else getattr(meta, 'plan', None)
            if user_id and plan:
                user = User.query.get(int(user_id))
                if user:
                    user.plan                = plan
                    user.stripe_sub_id       = getattr(data, 'subscription', None)
                    user.analyses_this_month = 0
                    user.quota_reset_at      = datetime.utcnow()
                    db.session.commit()
                    print(f'Plan {plan} activé pour {user.email}')
        except Exception as e:
            print(f'checkout.session.completed error: {e}')

    # Abonnement annulé → repasser en free
    elif ev_type in ('customer.subscription.deleted', 'customer.subscription.paused'):
        try:
            sub_id = getattr(data, 'id', None)
            if sub_id:
                user = User.query.filter_by(stripe_sub_id=sub_id).first()
                if user:
                    user.plan          = 'free'
                    user.stripe_sub_id = None
                    db.session.commit()
                    print(f'Plan annulé → free pour {user.email}')
        except Exception as e:
            print(f'subscription.deleted error: {e}')

    # Renouvellement → reset quota
    elif ev_type == 'invoice.paid':
        try:
            sub_id = getattr(data, 'subscription', None)
            if sub_id:
                user = User.query.filter_by(stripe_sub_id=sub_id).first()
                if user:
                    user.analyses_this_month = 0
                    user.quota_reset_at      = datetime.utcnow()
                    db.session.commit()
                    print(f'Quota reset pour {user.email}')
        except Exception as e:
            print(f'invoice.paid error: {e}')

    return '', 200


@app.route('/portal')
def customer_portal():
    if not current_user.is_authenticated or not current_user.stripe_customer_id:
        return redirect(url_for('account'))
    try:
        portal = stripe.billing_portal.Session.create(
            customer=current_user.stripe_customer_id,
            return_url=request.host_url + 'account',
        )
        return redirect(portal.url, code=303)
    except Exception as e:
        print('Portal error:', e)
        return redirect(url_for('account'))



PRIVACY_PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Privacy Policy — InsideYourMix</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Sans:wght@400;500&family=Space+Grotesk:wght@700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--v:#7B2FFF;--c:#00E5FF;--n:#07070F;--n2:#0F0F1A;--w:#F0F0F8;--gr:#8888AA}
body{background:var(--n);color:var(--w);font-family:'DM Sans',sans-serif;min-height:100vh}
.bg1{position:fixed;top:-25%;left:-10%;width:75%;height:75%;background:radial-gradient(ellipse,rgba(123,47,255,0.15) 0%,transparent 60%);pointer-events:none;z-index:0;filter:blur(48px)}
.bg2{position:fixed;bottom:-20%;right:-8%;width:65%;height:65%;background:radial-gradient(ellipse,rgba(0,229,255,0.1) 0%,transparent 58%);pointer-events:none;z-index:0;filter:blur(55px)}
nav{position:fixed;top:0;left:0;right:0;padding:18px 48px;display:flex;justify-content:space-between;align-items:center;z-index:100;background:rgba(7,7,15,0.7);backdrop-filter:blur(20px);border-bottom:1px solid rgba(255,255,255,0.05)}
.logo{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:20px;background:linear-gradient(90deg,#F0F0F8,#7B2FFF,#00E5FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;text-decoration:none}
.nav-cta{background:linear-gradient(135deg,#7B2FFF,#5020CC);color:white;padding:10px 24px;border-radius:24px;text-decoration:none;font-weight:600;font-size:14px}
.wrap{max-width:760px;margin:0 auto;padding:120px 24px 100px;position:relative;z-index:1}
.label{font-size:11px;letter-spacing:3px;text-transform:uppercase;color:var(--v);margin-bottom:12px;font-family:'Syne',sans-serif}
h1{font-family:'Space Grotesk',sans-serif;font-size:clamp(28px,5vw,44px);font-weight:800;margin-bottom:8px;letter-spacing:-0.03em}
.updated{color:var(--gr);font-size:13px;margin-bottom:48px}
h2{font-family:'Syne',sans-serif;font-size:18px;font-weight:700;color:var(--w);margin:36px 0 12px}
p{color:rgba(240,240,248,0.75);font-size:15px;line-height:1.8;margin-bottom:16px}
ul{color:rgba(240,240,248,0.75);font-size:15px;line-height:1.8;padding-left:20px;margin-bottom:16px}
ul li{margin-bottom:6px}
a{color:var(--c);text-decoration:none}
a:hover{text-decoration:underline}
.card{background:var(--n2);border:1px solid rgba(255,255,255,0.06);border-radius:14px;padding:24px;margin-bottom:16px}
@media(max-width:640px){nav{padding:14px 16px}.wrap{padding:90px 16px 80px}}
</style>
</head>
<body>
<div class="bg1"></div><div class="bg2"></div>
<nav>
<a href="/" class="logo">InsideYourMix</a>
<a href="/analyze" class="nav-cta">Analyser →</a>
</nav>
<div class="wrap">
<div class="label">Légal</div>
<h1>Privacy Policy</h1>
<p class="updated">Dernière mise à jour : mai 2026</p>

<div class="card">
<h2>1. Qui sommes-nous ?</h2>
<p>InsideYourMix est un service d'analyse de mix musical par intelligence artificielle, créé et exploité par Loïc (France). Contact : <a href="/contact">via notre page contact</a>.</p>
</div>

<div class="card">
<h2>2. Données collectées</h2>
<p>Nous collectons uniquement les données nécessaires au fonctionnement du service :</p>
<ul>
<li><strong>Compte</strong> : adresse email et mot de passe (hashé, jamais stocké en clair)</li>
<li><strong>Fichiers audio</strong> : tes fichiers sont analysés puis supprimés immédiatement du serveur après traitement</li>
<li><strong>Historique d'analyse</strong> : genre musical, score global, date — pour afficher ton historique</li>
<li><strong>Données de paiement</strong> : gérées exclusivement par Stripe — nous ne stockons jamais tes données bancaires</li>
</ul>
</div>

<div class="card">
<h2>3. Utilisation des données</h2>
<p>Tes données sont utilisées pour :</p>
<ul>
<li>Fournir le service d'analyse de mix</li>
<li>Gérer ton compte et ton quota mensuel</li>
<li>Traiter les paiements (via Stripe)</li>
<li>Améliorer la qualité du service</li>
</ul>
<p>Nous ne vendons jamais tes données. Nous ne les partageons pas avec des tiers sauf Stripe (paiement) et Anthropic (traitement IA des analyses).</p>
</div>

<div class="card">
<h2>4. Fichiers audio et IA</h2>
<p>Tes fichiers audio sont traités de manière confidentielle. Ils sont analysés par notre système puis transmis à l'API Anthropic Claude pour générer le rapport de coaching. <strong>Les fichiers sont supprimés immédiatement après l'analyse</strong> — nous ne les stockons pas.</p>
<p>L'analyse IA est effectuée via l'API Anthropic. Pour en savoir plus sur leur politique de confidentialité : <a href="https://www.anthropic.com/privacy" target="_blank">anthropic.com/privacy</a>.</p>
</div>

<div class="card">
<h2>5. Cookies et session</h2>
<p>Nous utilisons uniquement un cookie de session sécurisé pour maintenir ta connexion. Aucun cookie publicitaire ou de tracking tiers.</p>
</div>

<div class="card">
<h2>6. Tes droits (RGPD)</h2>
<p>Conformément au RGPD, tu as le droit de :</p>
<ul>
<li><strong>Accéder</strong> à tes données depuis ton espace compte</li>
<li><strong>Rectifier</strong> tes informations</li>
<li><strong>Supprimer</strong> ton compte et toutes tes données</li>
<li><strong>Portabilité</strong> de tes données sur demande</li>
</ul>
<p>Pour exercer ces droits, contacte-nous via <a href="/contact">la page contact</a>.</p>
</div>

<div class="card">
<h2>7. Sécurité</h2>
<p>Les mots de passe sont hashés avec bcrypt. Les communications sont chiffrées via HTTPS. La base de données est hébergée sur des serveurs sécurisés (Render, USA).</p>
</div>

<div class="card">
<h2>8. Contact</h2>
<p>Pour toute question relative à ta vie privée : <a href="/contact">page contact</a>.</p>
</div>

<p style="text-align:center;font-size:11px;font-style:italic;color:rgba(136,136,170,0.45);margin-top:48px;line-height:1.8">J'ai conçu cet outil avec passion dans une démarche purement technique.<br>Le principal reste de s'amuser et de rester créatif. <em>— Loïc</em></p>
</div>
""" + TRANSITION_HTML + """
</body></html>"""


@app.route("/privacy")
def privacy():
    return PRIVACY_PAGE

@app.route("/")
def index():
    return HTML_PAGE

@app.route("/analyze")
def analyze():
    return ANALYZE_PAGE

@app.route("/why")
def why():
    return WHY_PAGE

@app.route("/contact")
def contact():
    return CONTACT_PAGE

@app.route("/abonnements")
def abonnements():
    success  = request.args.get('success')
    canceled = request.args.get('canceled')
    banner   = ''
    if success:
        banner = '<div style="background:rgba(0,255,136,0.1);border:1px solid rgba(0,255,136,0.3);border-radius:12px;padding:16px 24px;text-align:center;color:#00FF88;margin-bottom:32px;font-weight:600">🎉 Abonnement activé ! Bienvenue sur le plan superieur.</div>'
    if canceled:
        banner = '<div style="background:rgba(255,180,0,0.08);border:1px solid rgba(255,180,0,0.25);border-radius:12px;padding:16px 24px;text-align:center;color:#FFB400;margin-bottom:32px">Paiement annulé — ton plan actuel est conservé.</div>'

    # Boutons selon statut connexion
    if current_user.is_authenticated:
        if current_user.plan == 'starter':
            btn_starter = '<span style="display:block;text-align:center;padding:14px 24px;border-radius:16px;background:rgba(0,255,136,0.1);color:#00FF88;font-weight:700;border:1px solid rgba(0,255,136,0.3)">✓ Ton plan actuel</span>'
            btn_pro     = '<a href="/checkout/pro" style="display:block;text-align:center;padding:14px 24px;border-radius:16px;font-weight:700;font-size:15px;text-decoration:none;background:linear-gradient(135deg,#7B2FFF,#5020CC);color:white">Passer au Pro →</a>'
        elif current_user.plan == 'pro':
            btn_starter = '<span style="display:block;text-align:center;padding:14px 24px;border-radius:16px;background:rgba(255,255,255,0.04);color:#8888AA;font-weight:600;font-size:14px">Inclus dans ton plan</span>'
            btn_pro     = '<span style="display:block;text-align:center;padding:14px 24px;border-radius:16px;background:rgba(0,255,136,0.1);color:#00FF88;font-weight:700;border:1px solid rgba(0,255,136,0.3)">✓ Ton plan actuel</span>'
        else:
            btn_starter = '<a href="/checkout/starter" style="display:block;text-align:center;padding:14px 24px;border-radius:16px;font-weight:700;font-size:15px;text-decoration:none;background:linear-gradient(135deg,#7B2FFF,#5020CC);color:white">Choisir Starter →</a>'
            btn_pro     = '<a href="/checkout/pro" style="display:block;text-align:center;padding:14px 24px;border-radius:16px;font-weight:700;font-size:15px;text-decoration:none;background:linear-gradient(135deg,#7B2FFF,#5020CC);color:white">Choisir Pro →</a>'
        manage_btn = '<div style="text-align:center;margin-top:32px"><a href="/portal" style="color:#8888AA;font-size:13px;text-decoration:none;border-bottom:1px solid rgba(136,136,170,0.3)">Gérer / annuler mon abonnement</a></div>' if current_user.stripe_customer_id else ''
    else:
        btn_starter = '<a href="/register" style="display:block;text-align:center;padding:14px 24px;border-radius:16px;font-weight:700;font-size:15px;text-decoration:none;background:linear-gradient(135deg,#7B2FFF,#5020CC);color:white">Commencer →</a>'
        btn_pro     = '<a href="/register" style="display:block;text-align:center;padding:14px 24px;border-radius:16px;font-weight:700;font-size:15px;text-decoration:none;background:linear-gradient(135deg,#7B2FFF,#5020CC);color:white">Commencer →</a>'
        manage_btn  = ''

    page = ABONNEMENTS_PAGE
    page = page.replace('%%BANNER%%', banner)
    page = page.replace('%%BTN_STARTER%%', btn_starter)
    page = page.replace('%%BTN_PRO%%', btn_pro)
    page = page.replace('%%MANAGE_BTN%%', manage_btn)
    return page

@app.route("/how-it-works")
def how_it_works():
    return HOW_IT_WORKS_HTML

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)