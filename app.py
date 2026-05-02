import os, uuid, json, re
import stripe
from datetime import datetime, timedelta
from flask import Flask, request, Response, stream_with_context, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import anthropic
from analyse import analyser_audio

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
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
# Price IDs Stripe (à créer sur dashboard.stripe.com)
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

with app.app_context(): db.create_all()

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
    "techno": {"lufs": -9, "sub": 15, "basses": 25, "mids": 30, "hauts_mids": 15, "aigus": 15, "bpm_min": 130, "bpm_max": 150, "stereo": 0.3, "reverb": 0.4, "crest": 8},
    "melodic techno": {"lufs": -10, "sub": 12, "basses": 22, "mids": 35, "hauts_mids": 16, "aigus": 15, "bpm_min": 120, "bpm_max": 135, "stereo": 0.4, "reverb": 0.5, "crest": 9},
    "hard techno": {"lufs": -8, "sub": 18, "basses": 28, "mids": 28, "hauts_mids": 14, "aigus": 12, "bpm_min": 140, "bpm_max": 160, "stereo": 0.25, "reverb": 0.3, "crest": 7},
    "industrial techno": {"lufs": -8, "sub": 16, "basses": 26, "mids": 28, "hauts_mids": 16, "aigus": 14, "bpm_min": 140, "bpm_max": 165, "stereo": 0.3, "reverb": 0.35, "crest": 7},
    "dub techno": {"lufs": -12, "sub": 14, "basses": 24, "mids": 32, "hauts_mids": 14, "aigus": 16, "bpm_min": 125, "bpm_max": 138, "stereo": 0.5, "reverb": 0.65, "crest": 10},
    "minimal techno": {"lufs": -11, "sub": 12, "basses": 22, "mids": 33, "hauts_mids": 17, "aigus": 16, "bpm_min": 128, "bpm_max": 140, "stereo": 0.35, "reverb": 0.45, "crest": 10},
    "house": {"lufs": -10, "sub": 12, "basses": 25, "mids": 35, "hauts_mids": 15, "aigus": 13, "bpm_min": 120, "bpm_max": 130, "stereo": 0.35, "reverb": 0.4, "crest": 9},
    "deep house": {"lufs": -12, "sub": 10, "basses": 22, "mids": 38, "hauts_mids": 16, "aigus": 14, "bpm_min": 118, "bpm_max": 126, "stereo": 0.4, "reverb": 0.5, "crest": 10},
    "tech house": {"lufs": -9, "sub": 13, "basses": 26, "mids": 32, "hauts_mids": 16, "aigus": 13, "bpm_min": 124, "bpm_max": 132, "stereo": 0.3, "reverb": 0.35, "crest": 8},
    "afro house": {"lufs": -10, "sub": 11, "basses": 23, "mids": 36, "hauts_mids": 17, "aigus": 13, "bpm_min": 118, "bpm_max": 126, "stereo": 0.4, "reverb": 0.45, "crest": 9},
    "organic house": {"lufs": -12, "sub": 9, "basses": 20, "mids": 38, "hauts_mids": 18, "aigus": 15, "bpm_min": 116, "bpm_max": 124, "stereo": 0.45, "reverb": 0.55, "crest": 11},
    "progressive house": {"lufs": -10, "sub": 12, "basses": 23, "mids": 35, "hauts_mids": 17, "aigus": 13, "bpm_min": 124, "bpm_max": 132, "stereo": 0.45, "reverb": 0.5, "crest": 9},
    "melodic house": {"lufs": -11, "sub": 11, "basses": 22, "mids": 36, "hauts_mids": 17, "aigus": 14, "bpm_min": 120, "bpm_max": 128, "stereo": 0.45, "reverb": 0.5, "crest": 10},
    "amapiano": {"lufs": -10, "sub": 14, "basses": 26, "mids": 34, "hauts_mids": 14, "aigus": 12, "bpm_min": 100, "bpm_max": 116, "stereo": 0.35, "reverb": 0.4, "crest": 9},
    "drum and bass": {"lufs": -8, "sub": 18, "basses": 28, "mids": 28, "hauts_mids": 14, "aigus": 12, "bpm_min": 160, "bpm_max": 180, "stereo": 0.35, "reverb": 0.3, "crest": 8},
    "dnb": {"lufs": -8, "sub": 18, "basses": 28, "mids": 28, "hauts_mids": 14, "aigus": 12, "bpm_min": 160, "bpm_max": 180, "stereo": 0.35, "reverb": 0.3, "crest": 8},
    "liquid dnb": {"lufs": -10, "sub": 15, "basses": 25, "mids": 32, "hauts_mids": 15, "aigus": 13, "bpm_min": 160, "bpm_max": 175, "stereo": 0.4, "reverb": 0.4, "crest": 9},
    "dubstep": {"lufs": -7, "sub": 20, "basses": 28, "mids": 28, "hauts_mids": 13, "aigus": 11, "bpm_min": 138, "bpm_max": 145, "stereo": 0.35, "reverb": 0.3, "crest": 7},
    "uk garage": {"lufs": -10, "sub": 13, "basses": 25, "mids": 34, "hauts_mids": 15, "aigus": 13, "bpm_min": 128, "bpm_max": 136, "stereo": 0.35, "reverb": 0.35, "crest": 9},
    "hip-hop": {"lufs": -9, "sub": 20, "basses": 28, "mids": 30, "hauts_mids": 12, "aigus": 10, "bpm_min": 70, "bpm_max": 100, "stereo": 0.3, "reverb": 0.35, "crest": 8},
    "trap": {"lufs": -8, "sub": 22, "basses": 25, "mids": 28, "hauts_mids": 13, "aigus": 12, "bpm_min": 130, "bpm_max": 160, "stereo": 0.35, "reverb": 0.3, "crest": 7},
    "drill": {"lufs": -8, "sub": 21, "basses": 26, "mids": 28, "hauts_mids": 13, "aigus": 12, "bpm_min": 130, "bpm_max": 150, "stereo": 0.3, "reverb": 0.3, "crest": 7},
    "boom bap": {"lufs": -11, "sub": 16, "basses": 26, "mids": 32, "hauts_mids": 14, "aigus": 12, "bpm_min": 85, "bpm_max": 100, "stereo": 0.3, "reverb": 0.4, "crest": 9},
    "lo-fi hip-hop": {"lufs": -14, "sub": 12, "basses": 22, "mids": 35, "hauts_mids": 16, "aigus": 15, "bpm_min": 70, "bpm_max": 90, "stereo": 0.35, "reverb": 0.5, "crest": 12},
    "trance": {"lufs": -9, "sub": 12, "basses": 22, "mids": 32, "hauts_mids": 18, "aigus": 16, "bpm_min": 136, "bpm_max": 145, "stereo": 0.5, "reverb": 0.55, "crest": 9},
    "psytrance": {"lufs": -8, "sub": 14, "basses": 24, "mids": 30, "hauts_mids": 18, "aigus": 14, "bpm_min": 140, "bpm_max": 150, "stereo": 0.4, "reverb": 0.45, "crest": 8},
    "hardstyle": {"lufs": -7, "sub": 16, "basses": 26, "mids": 28, "hauts_mids": 16, "aigus": 14, "bpm_min": 148, "bpm_max": 160, "stereo": 0.35, "reverb": 0.35, "crest": 7},
    "ambient": {"lufs": -16, "sub": 5, "basses": 15, "mids": 35, "hauts_mids": 20, "aigus": 25, "bpm_min": 60, "bpm_max": 100, "stereo": 0.6, "reverb": 0.7, "crest": 14},
    "pop": {"lufs": -9, "sub": 10, "basses": 20, "mids": 35, "hauts_mids": 20, "aigus": 15, "bpm_min": 90, "bpm_max": 130, "stereo": 0.4, "reverb": 0.4, "crest": 9},
    "default": {"lufs": -11, "sub": 12, "basses": 24, "mids": 32, "hauts_mids": 16, "aigus": 16, "bpm_min": 100, "bpm_max": 160, "stereo": 0.35, "reverb": 0.4, "crest": 9},
    # ── Aliases Beatport ──
    "techno peak time":              {"lufs": -9,  "sub": 15, "basses": 25, "mids": 30, "hauts_mids": 15, "aigus": 15, "bpm_min": 130, "bpm_max": 150, "stereo": 0.3,  "reverb": 0.4,  "crest": 8},
    "techno raw deep hypnotic":      {"lufs": -11, "sub": 13, "basses": 22, "mids": 32, "hauts_mids": 15, "aigus": 18, "bpm_min": 125, "bpm_max": 142, "stereo": 0.45, "reverb": 0.55, "crest": 10},
    "melodic house techno":          {"lufs": -10, "sub": 11, "basses": 22, "mids": 36, "hauts_mids": 17, "aigus": 14, "bpm_min": 120, "bpm_max": 132, "stereo": 0.45, "reverb": 0.5,  "crest": 9},
    "acid techno":                   {"lufs": -9,  "sub": 14, "basses": 24, "mids": 32, "hauts_mids": 16, "aigus": 14, "bpm_min": 128, "bpm_max": 145, "stereo": 0.3,  "reverb": 0.35, "crest": 8},
    # ── House étendu ──
    "jackin house":                  {"lufs": -10, "sub": 12, "basses": 24, "mids": 34, "hauts_mids": 17, "aigus": 13, "bpm_min": 124, "bpm_max": 130, "stereo": 0.35, "reverb": 0.4,  "crest": 9},
    "funky house":                   {"lufs": -10, "sub": 10, "basses": 22, "mids": 36, "hauts_mids": 18, "aigus": 14, "bpm_min": 120, "bpm_max": 128, "stereo": 0.4,  "reverb": 0.45, "crest": 9},
    "bass house":                    {"lufs": -8,  "sub": 16, "basses": 28, "mids": 30, "hauts_mids": 14, "aigus": 12, "bpm_min": 125, "bpm_max": 132, "stereo": 0.3,  "reverb": 0.3,  "crest": 8},
    "tribal house":                  {"lufs": -10, "sub": 12, "basses": 23, "mids": 35, "hauts_mids": 16, "aigus": 14, "bpm_min": 120, "bpm_max": 128, "stereo": 0.35, "reverb": 0.45, "crest": 9},
    "soulful house":                 {"lufs": -11, "sub": 10, "basses": 22, "mids": 36, "hauts_mids": 18, "aigus": 14, "bpm_min": 118, "bpm_max": 126, "stereo": 0.4,  "reverb": 0.5,  "crest": 10},
    "indie dance":                   {"lufs": -11, "sub": 8,  "basses": 20, "mids": 38, "hauts_mids": 20, "aigus": 14, "bpm_min": 120, "bpm_max": 132, "stereo": 0.45, "reverb": 0.5,  "crest": 10},
    "nu disco":                      {"lufs": -11, "sub": 9,  "basses": 20, "mids": 36, "hauts_mids": 20, "aigus": 15, "bpm_min": 110, "bpm_max": 124, "stereo": 0.45, "reverb": 0.5,  "crest": 10},
    "bass club":                     {"lufs": -8,  "sub": 18, "basses": 26, "mids": 30, "hauts_mids": 14, "aigus": 12, "bpm_min": 125, "bpm_max": 140, "stereo": 0.3,  "reverb": 0.3,  "crest": 8},
    # ── Trance étendu ──
    "trance main floor":             {"lufs": -9,  "sub": 12, "basses": 22, "mids": 32, "hauts_mids": 18, "aigus": 16, "bpm_min": 136, "bpm_max": 145, "stereo": 0.5,  "reverb": 0.55, "crest": 9},
    "trance raw deep hypnotic":      {"lufs": -11, "sub": 11, "basses": 20, "mids": 33, "hauts_mids": 18, "aigus": 18, "bpm_min": 136, "bpm_max": 148, "stereo": 0.55, "reverb": 0.6,  "crest": 10},
    "uplifting trance":              {"lufs": -9,  "sub": 11, "basses": 21, "mids": 32, "hauts_mids": 20, "aigus": 16, "bpm_min": 138, "bpm_max": 146, "stereo": 0.55, "reverb": 0.6,  "crest": 9},
    "progressive trance":            {"lufs": -10, "sub": 11, "basses": 21, "mids": 33, "hauts_mids": 19, "aigus": 16, "bpm_min": 132, "bpm_max": 140, "stereo": 0.5,  "reverb": 0.55, "crest": 9},
    "vocal trance":                  {"lufs": -9,  "sub": 10, "basses": 20, "mids": 34, "hauts_mids": 20, "aigus": 16, "bpm_min": 136, "bpm_max": 144, "stereo": 0.5,  "reverb": 0.6,  "crest": 9},
    "hard trance":                   {"lufs": -8,  "sub": 13, "basses": 23, "mids": 31, "hauts_mids": 18, "aigus": 15, "bpm_min": 145, "bpm_max": 155, "stereo": 0.45, "reverb": 0.45, "crest": 8},
    "tech trance":                   {"lufs": -9,  "sub": 12, "basses": 22, "mids": 32, "hauts_mids": 18, "aigus": 16, "bpm_min": 138, "bpm_max": 146, "stereo": 0.45, "reverb": 0.5,  "crest": 9},
    "full-on psytrance":             {"lufs": -8,  "sub": 15, "basses": 24, "mids": 30, "hauts_mids": 17, "aigus": 14, "bpm_min": 143, "bpm_max": 150, "stereo": 0.4,  "reverb": 0.45, "crest": 8},
    "dark psytrance":                {"lufs": -8,  "sub": 17, "basses": 25, "mids": 28, "hauts_mids": 16, "aigus": 14, "bpm_min": 143, "bpm_max": 152, "stereo": 0.35, "reverb": 0.4,  "crest": 7},
    "goa trance":                    {"lufs": -9,  "sub": 12, "basses": 22, "mids": 30, "hauts_mids": 20, "aigus": 16, "bpm_min": 136, "bpm_max": 150, "stereo": 0.45, "reverb": 0.5,  "crest": 9},
    # ── Bass Music étendu ──
    "jump up dnb":                   {"lufs": -8,  "sub": 20, "basses": 28, "mids": 26, "hauts_mids": 14, "aigus": 12, "bpm_min": 165, "bpm_max": 180, "stereo": 0.35, "reverb": 0.25, "crest": 7},
    "neurofunk":                     {"lufs": -8,  "sub": 18, "basses": 26, "mids": 28, "hauts_mids": 15, "aigus": 13, "bpm_min": 165, "bpm_max": 175, "stereo": 0.35, "reverb": 0.3,  "crest": 8},
    "halftime":                      {"lufs": -9,  "sub": 22, "basses": 26, "mids": 28, "hauts_mids": 13, "aigus": 11, "bpm_min": 65,  "bpm_max": 80,  "stereo": 0.4,  "reverb": 0.4,  "crest": 8},
    "jungle":                        {"lufs": -9,  "sub": 16, "basses": 26, "mids": 30, "hauts_mids": 15, "aigus": 13, "bpm_min": 155, "bpm_max": 175, "stereo": 0.35, "reverb": 0.35, "crest": 8},
    "deep dubstep":                  {"lufs": -10, "sub": 20, "basses": 26, "mids": 28, "hauts_mids": 14, "aigus": 12, "bpm_min": 138, "bpm_max": 142, "stereo": 0.4,  "reverb": 0.45, "crest": 9},
    "140 deep dubstep":              {"lufs": -9,  "sub": 20, "basses": 26, "mids": 28, "hauts_mids": 14, "aigus": 12, "bpm_min": 136, "bpm_max": 145, "stereo": 0.4,  "reverb": 0.4,  "crest": 9},
    "uk bass":                       {"lufs": -9,  "sub": 15, "basses": 25, "mids": 32, "hauts_mids": 15, "aigus": 13, "bpm_min": 128, "bpm_max": 140, "stereo": 0.4,  "reverb": 0.35, "crest": 9},
    "breakbeat":                     {"lufs": -9,  "sub": 14, "basses": 24, "mids": 32, "hauts_mids": 16, "aigus": 14, "bpm_min": 115, "bpm_max": 140, "stereo": 0.4,  "reverb": 0.4,  "crest": 9},
    "breaks":                        {"lufs": -9,  "sub": 14, "basses": 24, "mids": 32, "hauts_mids": 16, "aigus": 14, "bpm_min": 120, "bpm_max": 145, "stereo": 0.4,  "reverb": 0.4,  "crest": 9},
    # ── Electronic étendu ──
    "electronica":                   {"lufs": -13, "sub": 8,  "basses": 18, "mids": 34, "hauts_mids": 22, "aigus": 18, "bpm_min": 70,  "bpm_max": 140, "stereo": 0.55, "reverb": 0.55, "crest": 12},
    "electro":                       {"lufs": -9,  "sub": 14, "basses": 24, "mids": 30, "hauts_mids": 18, "aigus": 14, "bpm_min": 120, "bpm_max": 140, "stereo": 0.4,  "reverb": 0.4,  "crest": 9},
    "mainstage":                     {"lufs": -8,  "sub": 14, "basses": 22, "mids": 30, "hauts_mids": 20, "aigus": 14, "bpm_min": 128, "bpm_max": 138, "stereo": 0.55, "reverb": 0.55, "crest": 8},
    "hard dance":                    {"lufs": -7,  "sub": 15, "basses": 25, "mids": 28, "hauts_mids": 18, "aigus": 14, "bpm_min": 150, "bpm_max": 175, "stereo": 0.35, "reverb": 0.3,  "crest": 7},
    "neo rave":                      {"lufs": -8,  "sub": 15, "basses": 24, "mids": 28, "hauts_mids": 18, "aigus": 15, "bpm_min": 145, "bpm_max": 165, "stereo": 0.4,  "reverb": 0.35, "crest": 8},
    "phonk":                         {"lufs": -8,  "sub": 22, "basses": 26, "mids": 28, "hauts_mids": 12, "aigus": 12, "bpm_min": 130, "bpm_max": 160, "stereo": 0.35, "reverb": 0.35, "crest": 7},
    "future bass":                   {"lufs": -8,  "sub": 16, "basses": 24, "mids": 30, "hauts_mids": 16, "aigus": 14, "bpm_min": 130, "bpm_max": 150, "stereo": 0.5,  "reverb": 0.45, "crest": 8},
    "synthwave":                     {"lufs": -11, "sub": 10, "basses": 22, "mids": 32, "hauts_mids": 20, "aigus": 16, "bpm_min": 90,  "bpm_max": 120, "stereo": 0.45, "reverb": 0.55, "crest": 10},
    "downtempo":                     {"lufs": -13, "sub": 10, "basses": 20, "mids": 34, "hauts_mids": 20, "aigus": 16, "bpm_min": 60,  "bpm_max": 100, "stereo": 0.5,  "reverb": 0.55, "crest": 11},
    "jersey club":                   {"lufs": -8,  "sub": 18, "basses": 25, "mids": 30, "hauts_mids": 14, "aigus": 13, "bpm_min": 130, "bpm_max": 145, "stereo": 0.35, "reverb": 0.3,  "crest": 8},
    "afrobeats":                     {"lufs": -9,  "sub": 12, "basses": 24, "mids": 34, "hauts_mids": 16, "aigus": 14, "bpm_min": 95,  "bpm_max": 115, "stereo": 0.4,  "reverb": 0.45, "crest": 9},
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
    est_club = any(g in genre.lower() for g in GENRES_CLUB)
    if est_club:
        score_freq = min(100, int((freq["sub_basses_pct"] + freq["basses_pct"]) / 45 * 100))
    else:
        score_freq = min(100, int((freq["mids_pct"] + freq["hauts_mids_pct"]) / 50 * 100))
    target = -9 if est_club else -14
    score_dyn = max(0, min(100, int(100 - abs(dyn["lufs_approx"] - target) * 5)))
    score_stereo = min(100, int(ster["largeur_stereo"] / 0.5 * 100))
    score_rythme = min(100, max(0, int((ryt["regularite_beat"] + 1) / 2 * 100)))
    score_espace = min(100, int(esp["densite_mix"] * 100))
    score_global = int((score_freq + score_dyn + score_stereo + score_rythme + score_espace) / 5)
    return {
        "global": score_global,
        "frequentiel": score_freq,
        "dynamique": score_dyn,
        "stereo": score_stereo,
        "rythme": score_rythme,
        "espace": score_espace
    }

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

def build_score_card(dim, label, scores, featured=False):
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
.result{display:none}.result.active{display:block;animation:slideUp .5s cubic-bezier(.22,1,.36,1) forwards;padding-bottom:180px}
.rheader{display:flex;align-items:center;justify-content:space-between;margin-bottom:30px;flex-wrap:wrap;gap:16px}
.rtit{font-family:'Syne',sans-serif;font-size:24px;font-weight:700}
.rgenre{font-size:11px;letter-spacing:3px;text-transform:uppercase;color:var(--c)}
.sgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:30px}
.sc{background:var(--n2);border:1px solid rgba(255,255,255,.06);border-radius:14px;padding:18px;position:relative;cursor:help;transition:transform .2s,box-shadow .2s}
.sc:hover{transform:translateY(-3px);box-shadow:0 8px 30px rgba(123,47,255,.2)}
.sc.feat{background:linear-gradient(135deg,rgba(123,47,255,.15),rgba(0,229,255,.05));border-color:rgba(123,47,255,.3)}
.sc.feat:hover{box-shadow:0 8px 36px rgba(123,47,255,.35)}
.sc::after{content:attr(data-tip);position:absolute;bottom:calc(100% + 8px);left:50%;transform:translateX(-50%);background:rgba(10,10,22,.98);border:1px solid rgba(255,255,255,.12);color:#F0F0F8;font-size:11px;padding:7px 12px;border-radius:8px;white-space:nowrap;opacity:0;pointer-events:none;transition:opacity .18s;font-family:'DM Sans',sans-serif;z-index:100;font-weight:400;box-shadow:0 4px 20px rgba(0,0,0,.4)}
.sc:hover::after{opacity:1}
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
function activateStep(i){
  document.querySelectorAll(".pstep").forEach(function(s,idx){s.classList.remove("active-step");if(idx<i)s.classList.add("done");else s.classList.remove("done");});
  var steps=document.querySelectorAll(".pstep");if(steps[i])steps[i].classList.add("active-step");
}
document.getElementById("mf").addEventListener("submit",async function(e){
  e.preventDefault();var fd=new FormData(this);this.style.display="none";
  var ps=document.getElementById("psteps");ps.classList.add("active");
  document.querySelectorAll(".wb").forEach(function(b){b.classList.remove("fast");b.classList.add("analyzing");});
  startAnalysisCanvas();
  if(window._analysePortal){var btn2=document.querySelector('.upload-btn,[type=submit]');var br2=btn2?btn2.getBoundingClientRect():{left:window.innerWidth/2,top:window.innerHeight/2,width:0,height:0};window._analysePortal(br2.left+br2.width/2,br2.top+br2.height/2);}
  var timers=stepTimings.map(function(t,i){return setTimeout(function(){activateStep(i);},t);});
  try{
    var r=await fetch("/analyser",{method:"POST",body:fd});
    timers.forEach(clearTimeout);activateStep(5);
    await new Promise(function(resolve){setTimeout(resolve,400);});
    ps.classList.remove("active");stopAnalysisCanvas();
    document.querySelectorAll(".wb").forEach(function(b){b.classList.remove("analyzing");});
    var res=document.getElementById("result");res.classList.add("active");
    var reader=r.body.getReader(),decoder=new TextDecoder();
    while(true){var chunk=await reader.read();if(chunk.done)break;res.insertAdjacentHTML("beforeend",decoder.decode(chunk.value));}
  }catch(err){timers.forEach(clearTimeout);ps.classList.remove("active");stopAnalysisCanvas();document.getElementById("mf").style.display="block";alert("Erreur lors de l analyse");}
});

// ── MODE SWITCH ───────────────────────────────────────────────────────────
function switchMode(mode,btn){document.querySelectorAll(".mb").forEach(function(b){b.classList.remove("active");});document.querySelectorAll(".mp").forEach(function(p){p.classList.remove("active");});btn.classList.add("active");document.getElementById("panel-"+mode).classList.add("active");document.getElementById("mi").value=mode;}
function ff(family,btn){document.querySelectorAll(".fb").forEach(function(b){b.classList.remove("active");});btn.classList.add("active");var sel=document.getElementById("gs");sel.querySelectorAll("optgroup").forEach(function(g){g.style.display=(family==="all"||g.dataset.f===family)?"":"none";});}
function toggleMenu(){var m=document.getElementById('dropdownMenu'),btn=document.querySelector('.menu-btn');m.classList.toggle('open');if(btn)btn.classList.toggle('open');}
document.addEventListener('click',function(e){if(!e.target.closest('.dropdown')){document.getElementById('dropdownMenu').classList.remove('open');var btn=document.querySelector('.menu-btn');if(btn)btn.classList.remove('open');}});
function setLang(l){alert('Langue '+l+' - bientot disponible !');}

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

    def generate():
        yield '<div style="display:none">start</div>'
        try:
            donnees = analyser_audio(chemin, genre=genre)
            scores      = calculer_scores(donnees, genre)
            plateformes = calculer_plateformes(donnees)
            clip        = donnees.get("clipping", {"has_clipping": False, "count": 0, "total_pct": 0, "severite": "aucun", "events": [], "seuil_db": -0.3})
            # Durée totale estimée depuis les segments BOT
            segments_bot  = donnees["balance_over_time"].get("segments", [])
            duree_totale  = segments_bot[-1]["t"] + 8 if segments_bot else 180

            os.remove(chemin)
            # Incrémenter quota utilisateur
            if current_user.is_authenticated:
                current_user.analyses_this_month += 1
                analysis_record = Analysis(user_id=current_user.id, genre=genre, score=scores.get('global', 0))
                db.session.add(analysis_record)
                db.session.commit()

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
                + build_score_card("global", "Score Global", scores, True)
                + build_score_card("frequentiel", "Frequentiel", scores)
                + build_score_card("dynamique", "Dynamique", scores)
                + build_score_card("stereo", "Stereo", scores)
                + build_score_card("rythme", "Rythme", scores)
                + build_score_card("espace", "Espace", scores)
                + '</div>'
            )

            plat_html  = build_platform_badges(plateformes)  # garde pour InsideYourMaster
            clip_html  = build_clipping_html(clip, duree_totale)

            yield (
                '<div class="rheader">'
                '<div><div class="rgenre">' + mode.upper() + ' - ' + genre + '</div>'
                '<div class="rtit">Ton rapport de mix</div></div>'
                '<button class="btn-back" onclick="location.reload()">Nouveau mix</button>'
                '</div>'
                + scores_html
                + clip_html +
                '<div class="bots">'
                '<div class="bottit">Balance over Time</div>'
                '<div class="botbars">' + bot_bars + '</div>'
                '<div style="margin-top:10px">' + bot_events + '</div>'
                '</div>'
                '<div class="rbox" id="streamBox">'
            )

            freq = donnees["frequentiel"]
            dyn  = donnees["dynamique"]
            ster = donnees["stereo"]
            ryt  = donnees["rythme"]
            esp  = donnees["espace"]
            bot2 = donnees["balance_over_time"]

            profil = PROFILS_GENRE.get(genre.lower(), PROFILS_GENRE["default"])

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
                f"True Peak: {dyn['true_peak_db']} dBTP (seuil streaming: -1.0 dBTP)",
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
                "--- COMPATIBILITE PLATEFORMES ---",
                f"Spotify/Apple/YouTube: {plateformes['spotify']['verdict']} — {plateformes['spotify']['detail']}",
                f"Beatport/Clubs: {plateformes['beatport']['verdict']} — {plateformes['beatport']['detail']}",
                f"SoundCloud: {plateformes['soundcloud']['verdict']} — {plateformes['soundcloud']['detail']}",
            ]

            if refs_analyse:
                lines.append("")
                lines.append("--- COMPARAISON AVEC TES REFERENCES ---")
                for i, rd in enumerate(refs_analyse):
                    rf = rd["frequentiel"]
                    rd2 = rd["dynamique"]
                    rs = rd["stereo"]
                    lines.append(f"Reference {i+1}: LUFS={rd2['lufs_approx']} | Sub={rf['sub_basses_pct']}% | Basses={rf['basses_pct']}% | Mids={rf['mids_pct']}% | Stereo={rs['largeur_stereo']}")
                lines.append("Compare precisement ces valeurs a celles du mix du producteur pour identifier les ecarts.")

            resume = "\n".join(lines)

            # Contexte BPM et niveau
            contexte_bpm = detecter_contexte_bpm(genre, ryt["bpm"], profil)
            niveau_label, niveau_instruction = detecter_contexte_score(scores["global"], scores)

            refs_genre    = REFS_CULTURELLES.get(genre.lower(), REFS_CULTURELLES["default"])
            ctx_genre     = PROFILS_CONTEXTE.get(genre.lower(), PROFILS_CONTEXTE["default"])

            prompt_lines = [
                f"Tu es un coach bienveillant et encourageant specialise en production musicale {genre}.",
                f"Tu parles a un producteur passionne qui a travaille dur sur ce mix. Ton role est de l'aider a progresser, pas de le decourager.",
                f"",
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
                "4. Pour le TRUE PEAK : si > -1.0 dBTP, le streaming va baisser le volume automatiquement — mentionne-le",
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

            yield '</div>'
            yield '<p style="text-align:center;font-size:11px;font-style:italic;color:rgba(136,136,170,0.45);padding:40px 20px 24px;line-height:1.8">J\'ai conçu cet outil avec passion dans une démarche purement technique. Le principal reste de s\'amuser et de rester créatif. Loïc</p><button class=\"btn-back\" onclick=\"location.reload()\">Analyser un autre mix</button>'

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
Analyser →
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
        '<a href="/" class="back">← Retour a l\'accueil</a>'
        '</div>'
        + TRANSITION_HTML +
        '</body></html>'
    )

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('analyze'))
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
        return redirect(url_for('analyze'))
    return _render_auth('register')

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('analyze'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        pwd   = request.form.get('password', '')
        user  = User.query.filter_by(email=email).first()
        if not user or not check_password_hash(user.password_hash, pwd):
            return _render_auth('login', error='Email ou mot de passe incorrect.')
        login_user(user, remember=True)
        session.permanent = True
        return redirect(url_for('analyze'))
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
        '<p style="color:#8888AA;font-size:14px;margin-bottom:36px">' + current_user.email + '</p>'
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
    if plan not in STRIPE_PRICES or not STRIPE_PRICES[plan]:
        return redirect(url_for('abonnements'))
    if not stripe.api_key:
        return redirect(url_for('abonnements'))
    try:
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
            success_url=request.host_url + 'account?success=1',
            cancel_url=request.host_url + 'abonnements?canceled=1',
            metadata={'user_id': current_user.id, 'plan': plan},
            locale='fr',
        )
        return redirect(session_stripe.url, code=303)
    except Exception as e:
        print('Stripe checkout error:', e)
        return redirect(url_for('abonnements'))


@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload   = request.get_data()
    sig_header = request.headers.get('Stripe-Signature', '')
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        print('Webhook error:', e)
        return '', 400

    ev_type = event['type']
    data    = event['data']['object']

    # Paiement réussi → activer le plan
    if ev_type == 'checkout.session.completed':
        meta    = data.get('metadata', {})
        user_id = meta.get('user_id')
        plan    = meta.get('plan')
        if user_id and plan:
            user = User.query.get(int(user_id))
            if user:
                user.plan             = plan
                user.stripe_sub_id    = data.get('subscription')
                user.analyses_this_month = 0
                user.quota_reset_at   = datetime.utcnow()
                db.session.commit()
                print(f'Plan {plan} activé pour {user.email}')

    # Abonnement annulé / expiré → repasser en free
    elif ev_type in ('customer.subscription.deleted', 'customer.subscription.paused'):
        sub_id = data.get('id')
        user   = User.query.filter_by(stripe_sub_id=sub_id).first()
        if user:
            user.plan          = 'free'
            user.stripe_sub_id = None
            db.session.commit()
            print(f'Plan annulé → free pour {user.email}')

    # Renouvellement réussi → reset quota
    elif ev_type == 'invoice.paid':
        sub_id = data.get('subscription')
        if sub_id:
            user = User.query.filter_by(stripe_sub_id=sub_id).first()
            if user:
                user.analyses_this_month = 0
                user.quota_reset_at      = datetime.utcnow()
                db.session.commit()

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