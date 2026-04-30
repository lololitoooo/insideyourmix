import os
import uuid
import json
from flask import Flask, request, Response, stream_with_context
from dotenv import load_dotenv
import anthropic
from analyse import analyser_audio

load_dotenv()
app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

GENRES_CLUB = ["techno","melodic techno","hard techno","industrial techno",
    "dub techno","dark techno","house","deep house","tech house","afro house",
    "organic house","progressive house","melodic house","minimal","minimal techno",
    "trance","psytrance","drum and bass","dnb","dubstep","hardstyle",
    "hardcore","amapiano","uk garage","grime"]

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
nav{display:flex;align-items:center;justify-content:space-between;padding:20px 40px;border-bottom:1px solid rgba(255,255,255,0.05)}
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
.result{display:none}.result.active{display:block;animation:slideUp .5s cubic-bezier(.22,1,.36,1) forwards}
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
.btn-back{display:inline-flex;align-items:center;gap:8px;padding:14px 28px;background:rgba(123,47,255,.15);border:1px solid rgba(123,47,255,.3);border-radius:12px;color:var(--w);font-family:'Syne',sans-serif;font-size:14px;cursor:pointer;margin-top:8px;text-decoration:none;transition:all .2s}
.btn-back:hover{background:rgba(123,47,255,.25);transform:translateY(-1px)}
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
// --- WAVEFORM ---
const hw=document.getElementById("hw");
for(let i=0;i<36;i++){
  const b=document.createElement("div");
  b.className="wb";
  b.style.height=(Math.random()*28+8)+"px";
  b.style.animationDelay=(Math.random()*1.5).toFixed(2)+"s";
  hw.appendChild(b);
}

// --- MUTATION OBSERVER : anime les scores des qu'ils apparaissent ---
function animateScore(el){
  if(el._animated) return;
  el._animated=true;
  var target=parseInt(el.getAttribute("data-score"));
  var start=null, duration=1200;
  function step(ts){
    if(!start) start=ts;
    var p=Math.min((ts-start)/duration,1);
    var ease=1-Math.pow(1-p,3);
    el.textContent=Math.round(ease*target)+"%";
    if(p<1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}
function animateBars(root){
  (root.querySelectorAll?root.querySelectorAll(".sbf[data-width]"):[]).forEach(function(b){
    if(b._animated) return;
    b._animated=true;
    setTimeout(function(){b.style.width=b.getAttribute("data-width")+"%"},60);
  });
  if(root.classList&&root.classList.contains("sbf")&&root.dataset.width&&!root._animated){
    root._animated=true;
    setTimeout(function(){root.style.width=root.getAttribute("data-width")+"%"},60);
  }
}
function scanForScores(root){
  if(!root.querySelectorAll) return;
  root.querySelectorAll(".scval[data-score]").forEach(animateScore);
  animateBars(root);
  // confetti si score global > 80
  root.querySelectorAll(".sc.feat .scval[data-score]").forEach(function(el){
    if(parseInt(el.getAttribute("data-score"))>=80) launchConfetti();
  });
}
var resDiv=document.getElementById("result");
var observer=new MutationObserver(function(mutations){
  mutations.forEach(function(m){
    m.addedNodes.forEach(function(n){
      if(n.nodeType===1){ scanForScores(n); }
    });
  });
});
observer.observe(resDiv,{childList:true,subtree:true});

// --- CONFETTI ---
function launchConfetti(){
  if(window._confettiFired) return;
  window._confettiFired=true;
  var colors=["#7B2FFF","#00E5FF","#00FF88","#FF8C00","#FF4488"];
  for(var i=0;i<80;i++){
    (function(i){
      setTimeout(function(){
        var el=document.createElement("div");
        el.className="confetti-piece";
        el.style.left=Math.random()*100+"vw";
        el.style.background=colors[Math.floor(Math.random()*colors.length)];
        el.style.animationDuration=(2+Math.random()*2).toFixed(2)+"s";
        el.style.animationDelay=(Math.random()*0.8).toFixed(2)+"s";
        el.style.width=(6+Math.random()*8)+"px";
        el.style.height=(6+Math.random()*8)+"px";
        document.body.appendChild(el);
        setTimeout(function(){el.remove()},4000);
      },i*30);
    })(i);
  }
}

// --- FILE INPUT ---
document.getElementById("fi").addEventListener("change",function(){
  var uz=this.closest(".upload-zone")||document.querySelector(".upload-zone");
  if(this.files.length){
    document.getElementById("fs").textContent="Fichier: "+this.files[0].name;
    if(uz) uz.classList.add("has-file");
    document.querySelectorAll(".wb").forEach(function(b){b.classList.add("fast")});
  }
});
[["ref1","r1n"],["ref2","r2n"],["ref3","r3n"],["href1","h1n"],["href2","h2n"]].forEach(function(p){
  var inp=document.querySelector("input[name='"+p[0]+"']");
  if(inp) inp.addEventListener("change",function(){
    if(this.files.length) document.getElementById(p[1]).textContent="OK: "+this.files[0].name;
  });
});

// --- DRAG & DROP ---
var uz=document.querySelector(".upload-zone");
if(uz){
  ["dragenter","dragover"].forEach(function(ev){
    uz.addEventListener(ev,function(e){e.preventDefault();uz.classList.add("dragover");});
  });
  ["dragleave","drop"].forEach(function(ev){
    uz.addEventListener(ev,function(e){
      e.preventDefault();
      uz.classList.remove("dragover");
      if(ev==="drop"&&e.dataTransfer.files.length){
        var fi=document.getElementById("fi");
        var dt=new DataTransfer();
        dt.items.add(e.dataTransfer.files[0]);
        fi.files=dt.files;
        fi.dispatchEvent(new Event("change"));
      }
    });
  });
}

// --- PROGRESS STEPS ---
var STEPS=[
  {id:"ps1",label:"Upload en cours",icon:"📤"},
  {id:"ps2",label:"Lecture du fichier audio",icon:"🎵"},
  {id:"ps3",label:"Analyse frequentielle",icon:"📊"},
  {id:"ps4",label:"Analyse dynamique et stereo",icon:"⚡"},
  {id:"ps5",label:"Analyse rythme et espace",icon:"🎚"},
  {id:"ps6",label:"Coach IA en train d ecrire",icon:"🤖"},
  {id:"ps7",label:"Rapport pret !",icon:"✅"}
];
var stepTimings=[0,1200,3500,7000,12000,20000];

function activateStep(i){
  document.querySelectorAll(".pstep").forEach(function(s,idx){
    s.classList.remove("active-step");
    if(idx<i) s.classList.add("done");
    else s.classList.remove("done");
  });
  var steps=document.querySelectorAll(".pstep");
  if(steps[i]) steps[i].classList.add("active-step");
}

// --- FORM SUBMIT ---
document.getElementById("mf").addEventListener("submit",async function(e){
  e.preventDefault();
  var fd=new FormData(this);
  this.style.display="none";
  var ps=document.getElementById("psteps");
  ps.classList.add("active");
  document.querySelectorAll(".wb").forEach(function(b){b.classList.remove("fast");b.classList.add("analyzing");});
  var timers=stepTimings.map(function(t,i){return setTimeout(function(){activateStep(i);},t);});
  try{
    var r=await fetch("/analyser",{method:"POST",body:fd});
    timers.forEach(clearTimeout);
    activateStep(5);
    await new Promise(function(resolve){setTimeout(resolve,400);});
    ps.classList.remove("active");
    document.querySelectorAll(".wb").forEach(function(b){b.classList.remove("analyzing");});
    var res=document.getElementById("result");
    res.classList.add("active");
    var reader=r.body.getReader();
    var decoder=new TextDecoder();
    while(true){
      var chunk=await reader.read();
      if(chunk.done) break;
      res.insertAdjacentHTML("beforeend",decoder.decode(chunk.value));
    }
  }catch(err){
    timers.forEach(clearTimeout);
    ps.classList.remove("active");
    document.getElementById("mf").style.display="block";
    alert("Erreur lors de l analyse");
  }
});

// --- MODE SWITCH ---
function switchMode(mode,btn){
  document.querySelectorAll(".mb").forEach(function(b){b.classList.remove("active");});
  document.querySelectorAll(".mp").forEach(function(p){p.classList.remove("active");});
  btn.classList.add("active");
  document.getElementById("panel-"+mode).classList.add("active");
  document.getElementById("mi").value=mode;
}
function ff(family,btn){
  document.querySelectorAll(".fb").forEach(function(b){b.classList.remove("active");});
  btn.classList.add("active");
  var sel=document.getElementById("gs");
  sel.querySelectorAll("optgroup").forEach(function(g){
    g.style.display=(family==="all"||g.dataset.f===family)?"":"none";
  });
}

// --- MENU ---
function toggleMenu(){
  var m=document.getElementById("dropdownMenu");
  var btn=document.querySelector(".menu-btn");
  m.classList.toggle("open");
  if(btn) btn.classList.toggle("open");
}
document.addEventListener("click",function(e){
  if(!e.target.closest(".dropdown")){
    document.getElementById("dropdownMenu").classList.remove("open");
    var btn=document.querySelector(".menu-btn");
    if(btn) btn.classList.remove("open");
  }
});
function setLang(l){alert("Langue "+l+" - bientot disponible !");}
"""

HTML_BODY = """
<nav><a href="/" class="logo">InsideYourMix</a><div style="display:flex;gap:24px;align-items:center"><div class="dropdown"><button class="menu-btn" onclick="toggleMenu()"><span></span><span></span><span></span></button><div class="dropdown-menu" id="dropdownMenu"><a href="/how-it-works" class="dropdown-item">How it works</a><a href="/why" class="dropdown-item">Why InsideYourMix</a><a href="/abonnements" class="dropdown-item">Abonnements</a><a href="/contact" class="dropdown-item">Contact</a><div class="dropdown-divider"></div><div class="lang-selector"><span onclick="setLang('fr')" class="lang-flag">🇫🇷</span><span onclick="setLang('en')" class="lang-flag">🇬🇧</span><span onclick="setLang('es')" class="lang-flag">🇪🇸</span><span onclick="setLang('de')" class="lang-flag">🇩🇪</span><span onclick="setLang('pt')" class="lang-flag">🇵🇹</span></div></div></div><div class="badge">AI Mix Analysis</div></div></nav>
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
<button type="button" class="mb active" onclick="switchMode('genre',this)">Par Genre</button>
<button type="button" class="mb" onclick="switchMode('reference',this)">Par Reference</button>
<button type="button" class="mb" onclick="switchMode('hybride',this)">Mode Hybride</button>
</div>
<div class="mp active" id="panel-genre">
<div class="slabel">Famille musicale</div>
<div class="families">
<button type="button" class="fb active" onclick="ff('all',this)">Tous</button>
<button type="button" class="fb" onclick="ff('techno',this)">Techno</button>
<button type="button" class="fb" onclick="ff('house',this)">House</button>
<button type="button" class="fb" onclick="ff('bass',this)">Bass Music</button>
<button type="button" class="fb" onclick="ff('hiphop',this)">Hip-Hop</button>
<button type="button" class="fb" onclick="ff('elec',this)">Electronique</button>
<button type="button" class="fb" onclick="ff('other',this)">Autres</button>
</div>
<select name="genre" id="gs" class="gsel">
<optgroup label="TECHNO" data-f="techno">
<option>Techno</option><option>Melodic Techno</option><option>Hard Techno</option>
<option>Industrial Techno</option><option>Dub Techno</option><option>Minimal Techno</option>
</optgroup>
<optgroup label="HOUSE" data-f="house">
<option>Deep House</option><option>Tech House</option><option>Afro House</option>
<option>Progressive House</option><option>Organic House</option><option>Melodic House</option>
<option>Amapiano</option>
</optgroup>
<optgroup label="BASS MUSIC" data-f="bass">
<option>Drum and Bass</option><option>Liquid DnB</option><option>Neurofunk</option>
<option>Dubstep</option><option>UK Garage</option><option>Grime</option>
</optgroup>
<optgroup label="HIP-HOP" data-f="hiphop">
<option>Hip-Hop</option><option>Trap</option><option>Drill</option>
<option>Boom Bap</option><option>Phonk</option><option>Lo-fi Hip-Hop</option>
<option>RnB</option><option>Afrobeats</option>
</optgroup>
<optgroup label="ELECTRONIQUE" data-f="elec">
<option>Trance</option><option>Psytrance</option><option>Hardstyle</option>
<option>Future Bass</option><option>Electro</option><option>Synthwave</option>
<option>Ambient</option><option>Downtempo</option>
</optgroup>
<optgroup label="AUTRES" data-f="other">
<option>Pop</option><option>Rock</option><option>Jazz</option>
<option>Soul</option><option>Funk</option><option>Reggae</option>
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
<div class="pstep" id="ps1"><div class="pstep-dot">📤</div><div class="pstep-label">Upload en cours</div></div>
<div class="pstep" id="ps2"><div class="pstep-dot">🎵</div><div class="pstep-label">Lecture du fichier audio</div></div>
<div class="pstep" id="ps3"><div class="pstep-dot">📊</div><div class="pstep-label">Analyse frequentielle</div></div>
<div class="pstep" id="ps4"><div class="pstep-dot">⚡</div><div class="pstep-label">Analyse dynamique et stereo</div></div>
<div class="pstep" id="ps5"><div class="pstep-dot">🎚</div><div class="pstep-label">Analyse rythme et espace</div></div>
<div class="pstep" id="ps6"><div class="pstep-dot">🤖</div><div class="pstep-label">Coach IA en train d ecrire</div></div>
<div class="pstep" id="ps7"><div class="pstep-dot">✅</div><div class="pstep-label">Rapport pret !</div></div>
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
    '<style>' + CSS_STYLES + '</style></head><body>'
    + HTML_BODY +
    '<script>' + JS_SCRIPT + '</script>'
    '</body></html>'
)

@app.route("/analyser", methods=["POST"])
def analyser():
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
            scores  = calculer_scores(donnees, genre)

            os.remove(chemin)

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

            yield (
                '<div class="rheader">'
                '<div><div class="rgenre">' + mode.upper() + ' - ' + genre + '</div>'
                '<div class="rtit">Ton rapport de mix</div></div>'
                '<button class="btn-back" onclick="location.reload()">Nouveau mix</button>'
                '</div>'
                + scores_html +
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
                diag(round(dyn['lufs_approx'], 1), profil['lufs'], "LUFS", genre),
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
                "--- CHAMP STEREO ---",
                diag(round(ster['largeur_stereo'], 3), profil['stereo'], "Largeur stereo", genre),
                f"Correlation L/R: {ster['correlation']} (>0.7=compatible mono, <0.5=risque probleme mono)",
                f"Balance: {ster['balance_lr']} (0=parfaitement centre)",
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

            prompt_lines = [
                f"Tu es un coach bienveillant et encourageant specialise en production musicale {genre}.",
                f"Tu parles a un producteur passionne qui a travaille dur sur ce mix. Ton role est de l'aider a progresser, pas de le decourager.",
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
                "1. Cite les valeurs exactes mesurees (ex: ton LUFS de -11.2 est 2.2 dB au-dessus de la reference)",
                "2. Donne des corrections chiffrees et precises (ex: un boost de +4dB autour de 80Hz va apporter...)",
                "3. Si des references ont ete fournies, compare les valeurs precisement",
                f"4. Utilise les termes et references culturelles specifiques a la scene {genre}",
                "5. Pour le BPM, utilise le contexte fourni ci-dessus - ne dis pas juste 'hors norme'",
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

            yield '</div><button class="btn-back" onclick="location.reload()">Analyser un autre mix</button>'

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
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--v:#7B2FFF;--c:#00E5FF;--g:#00FF88;--n:#07070F;--n2:#0F0F1A;--w:#F0F0F8;--gr:#8888AA}
body{background:var(--n);color:var(--w);font-family:'DM Sans',sans-serif;min-height:100vh}
nav{display:flex;align-items:center;justify-content:space-between;padding:20px 40px;border-bottom:1px solid rgba(255,255,255,0.05)}
.logo{font-family:'Syne',sans-serif;font-size:22px;font-weight:800;background:linear-gradient(135deg,var(--v),var(--c));-webkit-background-clip:text;-webkit-text-fill-color:transparent;text-decoration:none}
.nav-right{display:flex;gap:24px;align-items:center}
.nav-link{color:var(--gr);font-size:13px;letter-spacing:1px;text-decoration:none}
.nav-link.active{color:var(--w)}
.badge{font-size:11px;background:rgba(123,47,255,0.15);border:1px solid rgba(123,47,255,0.3);color:var(--c);padding:4px 12px;border-radius:100px;letter-spacing:2px}
.hero{text-align:center;padding:60px 20px 40px;max-width:700px;margin:0 auto}
.hero-label{font-size:11px;letter-spacing:3px;text-transform:uppercase;color:var(--v);margin-bottom:12px;font-family:'Syne',sans-serif}
.hero h1{font-family:'Syne',sans-serif;font-size:clamp(32px,5vw,48px);font-weight:800;letter-spacing:-1px;margin-bottom:16px;background:linear-gradient(135deg,var(--v),var(--c));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.hero p{color:var(--gr);font-size:16px;line-height:1.6}
.main{max-width:960px;margin:0 auto;padding:0 20px 80px}
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
.dim-card.active{border-color:var(--v);background:rgba(123,47,255,0.08)}
.detail-box{background:var(--n2);border:1px solid rgba(123,47,255,0.3);border-radius:16px;padding:28px;margin-bottom:50px;display:none}
.detail-title{font-size:18px;font-weight:700;font-family:'Syne',sans-serif;margin-bottom:6px}
.detail-desc{color:var(--gr);font-size:14px;line-height:1.7;margin-bottom:20px}
.detail-metrics-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
.detail-metric{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:14px}
.dm-label{font-size:10px;letter-spacing:1px;text-transform:uppercase;color:var(--gr);margin-bottom:6px}
.dm-val{font-size:20px;font-weight:700;font-family:'Syne',sans-serif}
.dm-desc{font-size:11px;color:var(--gr);margin-top:3px}
.report-flow{display:flex;flex-direction:column;gap:10px;margin-bottom:50px}
.report-step{display:flex;align-items:flex-start;gap:14px;padding:18px;background:var(--n2);border:1px solid rgba(255,255,255,0.06);border-radius:12px}
.rs-dot{width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0;font-family:'Syne',sans-serif}
.rs-title{font-size:14px;font-weight:600;color:var(--w);font-family:'Syne',sans-serif;margin-bottom:4px}
.rs-desc{font-size:13px;color:var(--gr);line-height:1.6}
.cta{text-align:center;padding:40px 20px}
.cta-btn{display:inline-block;padding:18px 40px;background:linear-gradient(135deg,var(--v),#5020CC);border:none;border-radius:14px;color:white;font-family:'Syne',sans-serif;font-size:16px;font-weight:700;cursor:pointer;letter-spacing:1px;text-decoration:none}
.cta-btn:hover{transform:translateY(-2px);box-shadow:0 10px 40px rgba(123,47,255,0.4)}
</style>
</head>
<body>
<nav>
  <a href="/" class="logo">InsideYourMix</a>
  <div class="nav-right">
    <a href="/" class="nav-link">Analyser</a>
    <a href="/how-it-works" class="nav-link active">How it works</a>
    <div class="badge">AI Mix Analysis</div>
  </div>
</nav>
<div class="hero">
  <div class="hero-label">Comment ca marche</div>
  <h1>L'IA decortique ton mix</h1>
  <p>Ton morceau analyse en profondeur sur 7 dimensions techniques. Notre coach IA traduit les donnees en conseils concrets et actionnables.</p>
</div>
<div class="main">
  <div class="section-label">Le parcours en 4 etapes</div>
  <div class="flow">
    <div class="step"><div class="step-num">01</div><div class="step-label">Upload</div><div class="step-sub">MP3 - WAV - FLAC</div></div>
    <div class="arrow">-></div>
    <div class="step"><div class="step-num">02</div><div class="step-label">Analyse</div><div class="step-sub">7 dimensions</div></div>
    <div class="arrow">-></div>
    <div class="step"><div class="step-num">03</div><div class="step-label">Coach personnalise</div><div class="step-sub">Claude analyse</div></div>
    <div class="arrow">-></div>
    <div class="step" style="border-color:rgba(123,47,255,0.4);background:rgba(123,47,255,0.08)"><div class="step-num">04</div><div class="step-label">Rapport</div><div class="step-sub">Actions concretes</div></div>
  </div>
  <div class="section-label">Les 7 dimensions analysees - clique pour explorer</div>
  <div class="dims-grid" id="dimGrid"></div>
  <div class="detail-box" id="detailBox">
    <div class="detail-title" id="dTitle"></div>
    <div class="detail-desc" id="dDesc"></div>
    <div class="detail-metrics-grid" id="dMetrics"></div>
  </div>
  <div class="section-label">Ce que genere ton rapport</div>
  <div class="report-flow">
    <div class="report-step"><div class="rs-dot" style="background:rgba(123,47,255,0.2);color:#7B2FFF">1</div><div><div class="rs-title">Resume global</div><div class="rs-desc">2-3 phrases positives qui reconnaissent le travail et le potentiel du mix</div></div></div>
    <div class="report-step"><div class="rs-dot" style="background:rgba(0,229,255,0.15);color:#00E5FF">2</div><div><div class="rs-title">Ce qui fonctionne bien</div><div class="rs-desc">Points forts concrets avec les valeurs techniques mesurees</div></div></div>
    <div class="report-step"><div class="rs-dot" style="background:rgba(0,255,136,0.15);color:#00FF88">3</div><div><div class="rs-title">Coach personnalise</div><div class="rs-desc">Opportunites de progression formulees positivement avec valeurs cibles precises</div></div></div>
    <div class="report-step"><div class="rs-dot" style="background:rgba(255,140,0,0.15);color:#FF8C00">4</div><div><div class="rs-title">Tes 3 priorites</div><div class="rs-desc">Actions concretes et immediates, du plus impactant au moins impactant</div></div></div>
    <div class="report-step"><div class="rs-dot" style="background:rgba(123,47,255,0.2);color:#7B2FFF">5</div><div><div class="rs-title">Pret pour le streaming ?</div><div class="rs-desc">Verdict Spotify et Beatport avec ajustements precis en dB</div></div></div>
  </div>
  <div class="cta"><a href="/analyze" class="cta-btn">Analyser mon mix</a></div>
<script>
const dims = [
  {num:"01",name:"Analyse Frequentielle",color:"#7B2FFF",bg:"rgba(123,47,255,0.15)",icon:"F",
   metrics:["Sub-basses","Basses","Mids","Hauts-mids","Aigus","Centroide"],
   desc:"Decompose ton mix en 5 bandes frequentielles (20Hz-20kHz) en pourcentage. Revele l'equilibre spectral et compare aux standards de ton genre.",
   examples:[{label:"Sub-basses",val:"17%",desc:"20-80Hz - grave profond"},{label:"Basses",val:"32%",desc:"80-250Hz - corps du mix"},{label:"Mids",val:"29%",desc:"250Hz-2kHz - presence"},{label:"Hauts-mids",val:"14%",desc:"2-6kHz - brillance"},{label:"Aigus",val:"8%",desc:"6-20kHz - air et detail"},{label:"Centroide",val:"3354 Hz",desc:"Brillance globale"}]},
  {num:"02",name:"Dynamique & Loudness",color:"#00E5FF",bg:"rgba(0,229,255,0.12)",icon:"D",
   metrics:["RMS","Peak","LUFS","Crest Factor","Dynamic Range"],
   desc:"Mesure l'energie, le volume percu et la compression. Compare ton LUFS aux standards Spotify (-14) et Beatport (-9).",
   examples:[{label:"RMS",val:"-7.72 dB",desc:"Niveau moyen"},{label:"Peak",val:"-0.28 dB",desc:"Crete maximum"},{label:"LUFS",val:"-8.41",desc:"Volume percu"},{label:"Crest Factor",val:"7.44 dB",desc:"Punch et dynamique"},{label:"Dynamic Range",val:"7.6 dB",desc:"Respiration"}]},
  {num:"03",name:"Champ Stereo",color:"#00FF88",bg:"rgba(0,255,136,0.12)",icon:"S",
   metrics:["Correlation","Largeur","Balance","Mid/Side"],
   desc:"Analyse l'image stereo : largeur, correlation L/R et balance. Detecte les problemes de compatibilite mono.",
   examples:[{label:"Correlation",val:"0.954",desc:"1=parfait mono compat"},{label:"Largeur",val:"0.024",desc:"Ouverture image"},{label:"Balance",val:"0.000",desc:"Centrage L/R"},{label:"Mid energy",val:"0.169",desc:"Canal central"},{label:"Side energy",val:"0.004",desc:"Canaux lateraux"}]},
  {num:"04",name:"Rythme & Tempo",color:"#FF8C00",bg:"rgba(255,140,0,0.12)",icon:"R",
   metrics:["BPM","Onset strength","Regularite"],
   desc:"Detecte automatiquement le tempo par autocorrelation, mesure la puissance rythmique et la regularite du beat.",
   examples:[{label:"BPM",val:"129.2",desc:"Tempo detecte"},{label:"Onset strength",val:"37.27",desc:"Puissance attaques"},{label:"Regularite",val:"-0.34",desc:"Stabilite du groove"}]},
  {num:"05",name:"Timbre & Texture",color:"#FF4488",bg:"rgba(255,68,136,0.12)",icon:"T",
   metrics:["13 MFCCs","Spectral flatness"],
   desc:"13 coefficients MFCC capturent la couleur sonore et la texture. La flatness mesure le rapport bruit/tonal.",
   examples:[{label:"MFCC 1",val:"11.1",desc:"Energie globale"},{label:"MFCC 2",val:"8.0",desc:"Spectre grave/aigu"},{label:"MFCC 3-13",val:"...",desc:"Nuances timbre"},{label:"Flatness",val:"0.10",desc:"0=tonal / 1=bruit"}]},
  {num:"06",name:"Espace & Profondeur",color:"#7B2FFF",bg:"rgba(123,47,255,0.15)",icon:"E",
   metrics:["Reverb score","Densite mix"],
   desc:"Estime la quantite de reverberation et la densite spectrale. Revele la profondeur percue et la plenitude.",
   examples:[{label:"Reverb",val:"0.641",desc:"Presence reverb (0-1)"},{label:"Densite",val:"0.829",desc:"Plenitude spectrale"}]},
  {num:"07",name:"Balance over Time",color:"#00E5FF",bg:"rgba(0,229,255,0.12)",icon:"B",
   metrics:["Segments 8s","RMS","Drops","Breakdowns"],
   desc:"Decoupe le morceau en segments de 8s et analyse l'evolution. Detecte automatiquement les drops et breakdowns.",
   examples:[{label:"Segment 0-8s",val:"-7.29 dB",desc:"B:49% M:38% A:12%"},{label:"Drop",val:"+5.4 dB",desc:"A 120s - montee energie"},{label:"Breakdown",val:"-4.6 dB",desc:"A 112s - chute energie"}]}
];
const grid = document.getElementById('dimGrid');
const detailBox = document.getElementById('detailBox');
const dTitle = document.getElementById('dTitle');
const dDesc = document.getElementById('dDesc');
const dMetrics = document.getElementById('dMetrics');
dims.forEach(d => {
  const card = document.createElement('div');
  card.className = 'dim-card';
  card.style.cssText = 'background:'+d.bg+';border:1px solid '+d.color+'40;border-radius:16px;padding:20px;cursor:pointer;transition:all 0.3s';
  card.innerHTML = '<div style="font-size:11px;color:'+d.color+';font-weight:700;letter-spacing:.1em;margin-bottom:8px">'+d.num+'</div><div style="font-size:15px;font-weight:600;color:#F0F0F8">'+d.name+'</div>';
  card.addEventListener('click', function(){
    dTitle.textContent = d.num+' - '+d.name;
    dTitle.style.color = d.color;
    dDesc.textContent = d.desc;
    dMetrics.innerHTML = d.examples.map(e =>
      '<div style="background:rgba(255,255,255,0.04);border-radius:10px;padding:12px 16px;border:1px solid rgba(255,255,255,0.06)">'
      +'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">'
      +'<span style="font-size:13px;color:#F0F0F8;font-weight:500">'+e.label+'</span>'
      +'<span style="font-size:14px;font-weight:700;color:'+d.color+'">'+e.val+'</span></div>'
      +'<div style="font-size:11px;color:#8888AA">'+e.desc+'</div></div>'
    ).join('');
    detailBox.style.display = 'block';
    detailBox.scrollIntoView({behavior:'smooth',block:'nearest'});
  });
  grid.appendChild(card);
});
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
.nav{position:fixed;top:0;left:0;right:0;padding:24px 48px;display:flex;justify-content:space-between;align-items:center;z-index:100;background:rgba(7,7,15,0.7);backdrop-filter:blur(20px);border-bottom:1px solid rgba(255,255,255,0.05)}
.logo{font-family:'Syne',sans-serif;font-weight:800;font-size:22px;background:linear-gradient(90deg,#F0F0F8,#7B2FFF,#00E5FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;text-decoration:none}
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
</style>
</head>
<body>
<div class="bg-gradient"></div>
<nav class="nav"><a href="/" class="logo">InsideYourMix</a><a href="/analyze" class="nav-cta">Try it for free</a></nav>
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
</div></body></html>"""

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
.nav{position:fixed;top:0;left:0;right:0;padding:24px 48px;display:flex;justify-content:space-between;align-items:center;z-index:100;background:rgba(7,7,15,0.7);backdrop-filter:blur(20px);border-bottom:1px solid rgba(255,255,255,0.05)}
.logo{font-family:'Syne',sans-serif;font-weight:800;font-size:22px;background:linear-gradient(90deg,#F0F0F8,#7B2FFF,#00E5FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;text-decoration:none}
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
</style>
</head>
<body>
<div class="bg-gradient"></div>
<nav class="nav"><a href="/" class="logo">InsideYourMix</a><a href="/analyze" class="nav-cta">Try it for free</a></nav>
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
</div></body></html>"""

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
.nav{position:fixed;top:0;left:0;right:0;padding:24px 48px;display:flex;justify-content:space-between;align-items:center;z-index:100;background:rgba(7,7,15,0.7);backdrop-filter:blur(20px);border-bottom:1px solid rgba(255,255,255,0.05)}
.logo{font-family:'Syne',sans-serif;font-weight:800;font-size:22px;background:linear-gradient(90deg,#F0F0F8,#7B2FFF,#00E5FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;text-decoration:none}
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
</style>
</head>
<body>
<div class="bg-gradient"></div>
<nav class="nav"><a href="/" class="logo">InsideYourMix</a><a href="/analyze" class="nav-cta">Try it for free</a></nav>
<div class="content">
<h1>Simple, <span>transparent</span>.</h1>
<p class="intro">Commence gratuitement. Upgrade quand tu es pret.</p>
<div class="plans">
<div class="plan">
<div class="plan-name">Gratuit</div><div class="plan-price">0€</div><div class="plan-period">pour toujours</div>
<ul class="plan-features"><li>3 analyses offertes</li><li>Les 3 modes disponibles</li><li>Rapport complet</li><li>0,50€ par analyse supplementaire</li></ul>
<span class="plan-cta">Commencer</span>
</div>
<div class="plan featured">
<div class="plan-badge">Populaire</div>
<div class="plan-name">Starter</div><div class="plan-price">3€</div><div class="plan-period">par mois</div>
<ul class="plan-features"><li>20 analyses par mois</li><li>Les 3 modes disponibles</li><li>Rapport complet</li><li>Historique de tes analyses</li></ul>
<span class="plan-cta coming">Bientot disponible</span>
</div>
<div class="plan">
<div class="plan-name">Pro</div><div class="plan-price">7,90€</div><div class="plan-period">par mois</div>
<ul class="plan-features"><li>100 analyses par mois</li><li>Les 3 modes disponibles</li><li>Rapport complet</li><li>Historique + export PDF</li></ul>
<span class="plan-cta coming">Bientot disponible</span>
</div>
<div class="plan">
<div class="plan-name">Studio</div><div class="plan-price">29€</div><div class="plan-period">par mois</div>
<ul class="plan-features"><li>Analyses illimitees</li><li>Multi-utilisateurs</li><li>Ideal labels et ecoles</li><li>Support prioritaire</li></ul>
<span class="plan-cta coming">Bientot disponible</span>
</div>
</div>
<div class="coming-soon">Systeme de comptes et paiement en ligne — <strong>Bientot disponible</strong></div>
</div></body></html>"""

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
.bg-glow-a{position:fixed;top:-20%;left:-10%;width:70%;height:70%;background:radial-gradient(ellipse,rgba(123,47,255,0.18) 0%,transparent 65%);z-index:0;pointer-events:none;animation:floatA 14s ease-in-out infinite}
.bg-glow-b{position:fixed;bottom:-20%;right:-10%;width:60%;height:60%;background:radial-gradient(ellipse,rgba(0,229,255,0.1) 0%,transparent 65%);z-index:0;pointer-events:none;animation:floatB 18s ease-in-out infinite}
.bg-glow-c{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);width:40%;height:40%;background:radial-gradient(ellipse,rgba(255,107,53,0.04) 0%,transparent 70%);z-index:0;pointer-events:none;animation:floatA 22s ease-in-out infinite reverse}
@keyframes floatA{0%,100%{transform:translate(0,0) scale(1)}33%{transform:translate(4%,-6%) scale(1.1)}66%{transform:translate(-3%,4%) scale(0.95)}}
@keyframes floatB{0%,100%{transform:translate(0,0) scale(1)}33%{transform:translate(-5%,4%) scale(1.05)}66%{transform:translate(3%,-5%) scale(1.1)}}
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
.stats-band{position:relative;z-index:1;padding:0 48px 80px;max-width:1200px;margin:0 auto}
.stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:2px;background:rgba(255,255,255,.04);border-radius:20px;overflow:hidden;border:1px solid rgba(255,255,255,.06)}
.stat-card{padding:32px 24px;text-align:center;background:var(--n);position:relative;transition:background .3s}
.stat-card:hover{background:rgba(123,47,255,.06)}
.stat-card::after{content:"";position:absolute;right:0;top:20%;height:60%;width:1px;background:rgba(255,255,255,.05)}
.stat-card:last-child::after{display:none}
.stat-num{font-family:'Space Grotesk',sans-serif;font-size:42px;font-weight:800;display:block;margin-bottom:8px;background:linear-gradient(135deg,var(--w),rgba(240,240,248,.7));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat-card:nth-child(1) .stat-num{background:linear-gradient(135deg,var(--v),var(--c));-webkit-background-clip:text;background-clip:text}
.stat-card:nth-child(2) .stat-num{background:linear-gradient(135deg,var(--o),var(--p));-webkit-background-clip:text;background-clip:text}
.stat-card:nth-child(3) .stat-num{background:linear-gradient(135deg,var(--c),var(--g));-webkit-background-clip:text;background-clip:text}
.stat-card:nth-child(4) .stat-num{background:linear-gradient(135deg,var(--g),var(--c));-webkit-background-clip:text;background-clip:text}
.stat-label{font-size:13px;opacity:.55;line-height:1.4}

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
.mode-icon-wrap{width:60px;height:60px;border-radius:18px;display:flex;align-items:center;justify-content:center;font-size:28px;margin-bottom:24px}
.mode-card-1 .mode-icon-wrap{background:rgba(123,47,255,.2);box-shadow:0 4px 20px rgba(123,47,255,.25)}
.mode-card-2 .mode-icon-wrap{background:rgba(0,229,255,.15);box-shadow:0 4px 20px rgba(0,229,255,.2)}
.mode-card-3 .mode-icon-wrap{background:rgba(255,107,53,.15);box-shadow:0 4px 20px rgba(255,107,53,.2)}
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
<div class="lang-selector">
<span onclick="setLang('fr')" class="lang-flag">🇫🇷</span>
<span onclick="setLang('en')" class="lang-flag">🇬🇧</span>
<span onclick="setLang('es')" class="lang-flag">🇪🇸</span>
<span onclick="setLang('de')" class="lang-flag">🇩🇪</span>
<span onclick="setLang('pt')" class="lang-flag">🇵🇹</span>
</div>
</div>
</div>
<a href="/analyze" class="nav-cta">Try it free →</a>
</div>
</nav>

<section class="hero">
<div class="badge"><span class="badge-dot"></span>AI Mix Analysis · Premiere mondiale</div>
<h1>Analyse ton <span class="accent">MIX</span>.<br>Perfectionne ton <span class="accent-warm">SON</span>.</h1>
<p>Upload ton mix, choisis ton style. Recois un rapport technique ultra-precis qui te dit exactement sur quoi travailler pour atteindre les standards de l'industrie.</p>
<a href="/analyze" class="hero-cta">
Try it for free
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
</a>
<div class="hero-note">Gratuit · Aucune inscription requise · 3 analyses offertes</div>
</section>

<div class="stats-band reveal">
<div class="stats-grid">
<div class="stat-card"><span class="stat-num" data-count="50" data-suffix="M+">0</span><span class="stat-label">Producteurs dans le monde</span></div>
<div class="stat-card"><span class="stat-num" data-count="300" data-suffix="€+">0</span><span class="stat-label">Cout d'un ingenieur son</span></div>
<div class="stat-card"><span class="stat-num" data-count="7" data-suffix="">0</span><span class="stat-label">Dimensions analysees</span></div>
<div class="stat-card"><span class="stat-num" data-count="100" data-suffix="+">0</span><span class="stat-label">Genres references</span></div>
</div>
</div>

<section class="modes reveal">
<div class="section-label">Comment ca marche</div>
<h2 class="section-title">3 modes d'analyse</h2>
<p class="section-subtitle">Choisis l'approche qui correspond a ton workflow et ton objectif</p>
<div class="modes-grid">
<div class="mode-card mode-card-1">
<div class="mode-icon-wrap">🎛️</div>
<h3>Mode Genre</h3>
<p>Compare ton mix aux standards techniques de ton style musical. Plus de 100 genres analyses — Techno, House, Hip-Hop, Drum & Bass, et bien plus.</p>
<span class="mode-tag">100+ genres</span>
</div>
<div class="mode-card mode-card-2 reveal-delay-1">
<div class="mode-icon-wrap">🎵</div>
<h3>Mode Reference</h3>
<p>Upload tes morceaux preferes et recois une analyse comparative detaillee. Notre coach te montre exactement ce qui separe ton mix de tes references.</p>
<span class="mode-tag">Jusqu a 3 refs</span>
</div>
<div class="mode-card mode-card-3 reveal-delay-2">
<div class="mode-icon-wrap">⚡</div>
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
<h2>Pret a <span style="background:linear-gradient(135deg,var(--o),var(--p));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text">passer au niveau superieur</span> ?</h2>
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
</script>
</body>
</html>"""
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
    return ABONNEMENTS_PAGE

@app.route("/how-it-works")
def how_it_works():
    return HOW_IT_WORKS_HTML

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)