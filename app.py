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
def generer_rapport_ia(donnees, genre, scores):
    freq = donnees["frequentiel"]
    dyn  = donnees["dynamique"]
    ster = donnees["stereo"]
    ryt  = donnees["rythme"]
    esp  = donnees["espace"]
    bot  = donnees["balance_over_time"]
    lines = [
        "Genre: " + genre,
        "Scores: Global=" + str(scores["global"]) + "% Freq=" + str(scores["frequentiel"]) + "% Dyn=" + str(scores["dynamique"]) + "% Stereo=" + str(scores["stereo"]) + "%",
        "BPM=" + str(ryt["bpm"]) + " LUFS=" + str(dyn["lufs_approx"]) + " RMS=" + str(dyn["rms_db"]),
        "Sub=" + str(freq["sub_basses_pct"]) + "% Basses=" + str(freq["basses_pct"]) + "% Mids=" + str(freq["mids_pct"]) + "% Aigus=" + str(freq["aigus_pct"]) + "%",
        "Stereo largeur=" + str(ster["largeur_stereo"]) + " correlation=" + str(ster["correlation"]),
        "Reverb=" + str(esp["reverb_score"]) + " Densite=" + str(esp["densite_mix"]),
        "Events BOT: " + json.dumps(bot["events"])
    ]
    resume = "\n".join(lines)
    prompt_lines = [
        "Tu es un coach en production musicale bienveillant specialise en " + genre + ".",
        "Tu es la pour aider le producteur a progresser, pas pour le juger.",
        "Voici l'analyse de son mix:",
        resume,
        "",
        "Genere un rapport de coaching encourageant en francais avec ce ton:",
        "- Positif et encourageant, mets en avant le potentiel",
        "- Concret et actionnable",
        "- Transforme chaque point faible en opportunite d'amelioration",
        "",
        "Structure obligatoire:",
        "## Resume",
        "## Ce qui fonctionne bien",
        "## Tes pistes d'amelioration",
        "## Tes 3 priorites cette semaine",
        "## Pret pour le streaming ?"
    ]
    prompt = "\n".join(prompt_lines)
    msg = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text

def get_color(score):
    if score >= 75:
        return "#00FF88"
    if score >= 50:
        return "#00E5FF"
    return "#7B2FFF"

def render_rapport(text):
    import re
    html = ""
    for line in text.split("\n"):
        if line.startswith("## "):
            html += "<h2>" + line[3:] + "</h2>"
        elif line.startswith("### "):
            html += "<h3>" + line[4:] + "</h3>"
        elif line.startswith("- ") or line.startswith("* "):
            clean = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', line[2:])
            html += "<li>" + clean + "</li>"
        elif line.strip():
            clean = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', line)
            html += "<p>" + clean + "</p>"
    return html
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
    parts.append('<div class="scval" style="' + val_style + '">' + str(v) + '%</div>')
    parts.append('<div class="sbbg"><div class="sbf" style="width:' + str(v) + '%;' + bar_bg + '"></div></div>')
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
.logo{font-family:'Syne',sans-serif;font-size:22px;font-weight:800;background:linear-gradient(135deg,var(--v),var(--c));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.badge{font-size:11px;background:rgba(123,47,255,0.15);border:1px solid rgba(123,47,255,0.3);color:var(--c);padding:4px 12px;border-radius:100px;letter-spacing:2px}
.hero{text-align:center;padding:60px 20px 40px}
.hero h1{font-family:'Syne',sans-serif;font-size:clamp(36px,6vw,64px);font-weight:800;letter-spacing:-2px;margin-bottom:16px}
.hero h1 span{background:linear-gradient(135deg,var(--v),var(--c));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.hero p{color:var(--gr);font-size:17px;max-width:500px;margin:0 auto}
.waveform{display:flex;align-items:center;justify-content:center;gap:3px;margin:30px auto;height:40px}
.wb{width:3px;background:linear-gradient(to top,var(--v),var(--c));border-radius:10px;animation:wave 1.5s ease-in-out infinite;opacity:.6}
@keyframes wave{0%,100%{transform:scaleY(.3)}50%{transform:scaleY(1)}}
.main{max-width:860px;margin:0 auto;padding:0 20px 80px}
.modes{display:flex;gap:8px;margin-bottom:24px;background:var(--n2);padding:6px;border-radius:14px;border:1px solid rgba(255,255,255,0.06)}
.mb{flex:1;padding:12px;background:transparent;border:none;color:var(--gr);font-family:'Syne',sans-serif;font-size:13px;font-weight:600;border-radius:10px;cursor:pointer}
.mb.active{background:linear-gradient(135deg,rgba(123,47,255,.3),rgba(0,229,255,.1));color:var(--w);border:1px solid rgba(123,47,255,.4)}
.upload-zone{border:2px dashed rgba(123,47,255,.4);border-radius:20px;padding:50px 30px;text-align:center;background:rgba(123,47,255,.04);cursor:pointer;margin-bottom:20px;position:relative}
.upload-zone:hover{border-color:var(--v);background:rgba(123,47,255,.08)}
.upload-zone input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.upload-icon{font-size:40px;margin-bottom:12px}
.upload-zone h3{font-family:'Syne',sans-serif;font-size:18px;margin-bottom:8px}
.upload-zone p{color:var(--gr);font-size:14px}
.formats{display:flex;gap:8px;justify-content:center;margin-top:16px}
.fmt{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);padding:4px 10px;border-radius:6px;font-size:12px;color:var(--gr)}
.fsel{color:var(--g);font-weight:500;margin-top:10px;font-size:14px}
.mp{display:none}.mp.active{display:block}
.slabel{font-family:'Syne',sans-serif;font-size:11px;letter-spacing:3px;text-transform:uppercase;color:var(--v);margin-bottom:10px}
.families{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.fb{padding:6px 14px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:100px;color:var(--gr);font-size:12px;cursor:pointer}
.fb.active,.fb:hover{background:rgba(123,47,255,.15);border-color:rgba(123,47,255,.4);color:var(--w)}
.gsel{width:100%;padding:14px 16px;background:var(--n2);border:1px solid rgba(255,255,255,.08);border-radius:12px;color:var(--w);font-size:15px;cursor:pointer}
.ref-zone{background:var(--n2);border:1px solid rgba(255,255,255,.06);border-radius:16px;padding:20px;margin-bottom:20px}
.ref-slot{display:flex;align-items:center;gap:12px;padding:14px;background:rgba(255,255,255,.03);border:1px dashed rgba(255,255,255,.1);border-radius:10px;margin-bottom:10px;cursor:pointer;position:relative}
.ref-slot:hover{border-color:rgba(123,47,255,.4);background:rgba(123,47,255,.05)}
.ref-slot input{position:absolute;inset:0;opacity:0;cursor:pointer}
.rnum{width:28px;height:28px;border-radius:50%;background:rgba(123,47,255,.2);border:1px solid rgba(123,47,255,.4);display:flex;align-items:center;justify-content:center;font-size:12px;color:var(--v);flex-shrink:0}
.rinfo{flex:1}.rtitle{font-size:13px;color:var(--w)}.rsub{font-size:11px;color:var(--gr);margin-top:2px}
.btn-go{margin-top:40px;width:100%;padding:18px;background:linear-gradient(135deg,var(--v),#5020CC);border:none;border-radius:14px;color:white;font-family:'Syne',sans-serif;font-size:16px;font-weight:700;cursor:pointer;letter-spacing:1px}
.btn-go:hover{transform:translateY(-2px);box-shadow:0 10px 40px rgba(123,47,255,.4)}
.loading{display:none;text-align:center;padding:60px 20px}.loading.active{display:block}
.lwave{display:flex;align-items:center;justify-content:center;gap:4px;margin-bottom:20px}
.lb{width:4px;height:30px;background:linear-gradient(to top,var(--v),var(--c));border-radius:10px;animation:wave .8s ease-in-out infinite}
.loading h3{font-family:'Syne',sans-serif;font-size:18px;margin-bottom:8px}
.loading p{color:var(--gr);font-size:14px}
.result{display:none}.result.active{display:block}
.rheader{display:flex;align-items:center;justify-content:space-between;margin-bottom:30px;flex-wrap:wrap;gap:16px}
.rtit{font-family:'Syne',sans-serif;font-size:24px;font-weight:700}
.rgenre{font-size:11px;letter-spacing:3px;text-transform:uppercase;color:var(--c)}
.sgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:30px}
.sc{background:var(--n2);border:1px solid rgba(255,255,255,.06);border-radius:14px;padding:18px}
.sc.feat{background:linear-gradient(135deg,rgba(123,47,255,.15),rgba(0,229,255,.05));border-color:rgba(123,47,255,.3)}
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
.btn-back{display:inline-flex;align-items:center;gap:8px;padding:14px 28px;background:rgba(123,47,255,.15);border:1px solid rgba(123,47,255,.3);border-radius:12px;color:var(--w);font-family:'Syne',sans-serif;font-size:14px;cursor:pointer;margin-top:8px;text-decoration:none}
.btn-back:hover{background:rgba(123,47,255,.25);transform:translateY(-1px)}
"""

JS_SCRIPT = """
const hw=document.getElementById("hw");
for(let i=0;i<36;i++){
  const b=document.createElement("div");
  b.className="wb";
  b.style.height=(Math.random()*28+8)+"px";
  b.style.animationDelay=(Math.random()*1.5).toFixed(2)+"s";
  hw.appendChild(b);
}
document.getElementById("fi").addEventListener("change",function(){
  if(this.files.length)document.getElementById("fs").textContent="Fichier: "+this.files[0].name;
});
[["ref1","r1n"],["ref2","r2n"],["ref3","r3n"],["href1","h1n"],["href2","h2n"]].forEach(function(p){
  const inp=document.querySelector("input[name='"+p[0]+"']");
  if(inp)inp.addEventListener("change",function(){
    if(this.files.length)document.getElementById(p[1]).textContent="OK: "+this.files[0].name;
  });
});
function switchMode(mode,btn){
  document.querySelectorAll(".mb").forEach(function(b){b.classList.remove("active")});
  document.querySelectorAll(".mp").forEach(function(p){p.classList.remove("active")});
  btn.classList.add("active");
  document.getElementById("panel-"+mode).classList.add("active");
  document.getElementById("mi").value=mode;
}
function ff(family,btn){
  document.querySelectorAll(".fb").forEach(function(b){b.classList.remove("active")});
  btn.classList.add("active");
  const sel=document.getElementById("gs");
  sel.querySelectorAll("optgroup").forEach(function(g){
    g.style.display=(family==="all"||g.dataset.f===family)?"":"none";
  });
}
document.getElementById("mf").addEventListener("submit",async function(e){
  e.preventDefault();
  const fd=new FormData(this);
  document.getElementById("mf").style.display="none";
  document.getElementById("loading").classList.add("active");
  try{
    const r=await fetch("/analyser",{method:"POST",body:fd});
document.getElementById("loading").classList.remove("active");
const res=document.getElementById("result");
res.classList.add("active");
const reader=r.body.getReader();
const decoder=new TextDecoder();
while(true){
  const {done,value}=await reader.read();
  if(done)break;
  res.innerHTML+=decoder.decode(value);
}
  }catch(err){
    document.getElementById("loading").classList.remove("active");
    document.getElementById("mf").style.display="block";
    alert("Erreur");
  }
});
"""

HTML_BODY = """
<nav><div class="logo">InsideYourMix</div><div style="display:flex;gap:24px;align-items:center"><a href="/how-it-works" style="color:#8888AA;text-decoration:none;font-size:13px;letter-spacing:1px">How it works</a><div class="badge">AI Mix Analysis</div></div></nav>
<div class="hero"><h1>Inside<span>Your</span>Mix</h1>
<p>Upload ton mix. Choisis ton style ou tes références. On analyse et on te guide vers le son que tu vises.</p>
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
<div class="loading" id="loading">
<div class="lwave">
<div class="lb" style="animation-delay:0s"></div>
<div class="lb" style="animation-delay:.1s"></div>
<div class="lb" style="animation-delay:.2s"></div>
<div class="lb" style="animation-delay:.3s"></div>
<div class="lb" style="animation-delay:.4s"></div>
<div class="lb" style="animation-delay:.3s"></div>
<div class="lb" style="animation-delay:.2s"></div>
<div class="lb" style="animation-delay:.1s"></div>
</div>
<h3>Analyse en cours...</h3>
<p>L'IA examine ton mix en profondeur</p>
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
        yield '<div style="display:none">start</div>'  # Premier byte immédiat
        try:
            donnees = analyser_audio(chemin, genre=genre)
            scores  = calculer_scores(donnees, genre)

            os.remove(chemin)
            for r in refs:
                if os.path.exists(r): os.remove(r)

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
            lines = [
                "Genre: " + genre,
                "Scores: Global=" + str(scores["global"]) + "% Freq=" + str(scores["frequentiel"]) + "% Dyn=" + str(scores["dynamique"]) + "% Stereo=" + str(scores["stereo"]) + "%",
                "BPM=" + str(ryt["bpm"]) + " LUFS=" + str(dyn["lufs_approx"]) + " RMS=" + str(dyn["rms_db"]),
                "Sub=" + str(freq["sub_basses_pct"]) + "% Basses=" + str(freq["basses_pct"]) + "% Mids=" + str(freq["mids_pct"]) + "% Aigus=" + str(freq["aigus_pct"]) + "%",
                "Stereo largeur=" + str(ster["largeur_stereo"]) + " correlation=" + str(ster["correlation"]),
                "Reverb=" + str(esp["reverb_score"]) + " Densite=" + str(esp["densite_mix"]),
                "Events BOT: " + json.dumps(bot2["events"])
            ]
            resume = "\n".join(lines)
            prompt_lines = [
                "Tu es un coach en production musicale bienveillant specialise en " + genre + ".",
                "Tu es la pour aider le producteur a progresser, pas pour le juger.",
                "Voici l'analyse de son mix:",
                resume,
                "",
                "Genere un rapport de coaching encourageant en francais avec ce ton:",
                "- Positif et encourageant, mets en avant le potentiel",
                "- Concret et actionnable",
                "- Transforme chaque point faible en opportunite d'amelioration",
                "",
                "Structure obligatoire:",
                "## Resume",
                "## Ce qui fonctionne bien",
                "## Tes pistes d'amelioration",
                "## Tes 3 priorites cette semaine",
                "## Pret pour le streaming ?"
            ]
            prompt = "\n".join(prompt_lines)

            import re
            buffer = ""
            with client.messages.stream(
                model="claude-sonnet-4-5",
                max_tokens=1500,
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
<title>InsideYourMix — Comment ça marche</title>
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
.dim-header{display:flex;align-items:center;gap:12px;margin-bottom:12px}
.dim-icon{width:36px;height:36px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0;font-family:'Syne',sans-serif;font-weight:700}
.dim-num{font-size:10px;letter-spacing:2px;color:var(--v);font-weight:600;font-family:'Syne',sans-serif}
.dim-name{font-size:14px;font-weight:600;color:var(--w);font-family:'Syne',sans-serif;margin-top:2px}
.dim-metrics{display:flex;flex-wrap:wrap;gap:5px;margin-top:10px}
.metric-tag{font-size:10px;padding:3px 9px;border-radius:100px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);color:var(--gr)}
.detail-box{background:var(--n2);border:1px solid rgba(123,47,255,0.3);border-radius:16px;padding:28px;margin-bottom:50px;display:none}
.detail-box.active{display:block}
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
    <div class="step"><div class="step-num">03</div><div class="step-label">AI Coach</div><div class="step-sub">Claude analyse</div></div>
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
    <div class="report-step"><div class="rs-dot" style="background:rgba(255,140,0,0.15);color:#FF8C00">4</div><div><div class="rs-title">Tes 3 priorites cette semaine</div><div class="rs-desc">Actions concretes et immediates, du plus impactant au moins impactant</div></div></div>
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
    dTitle.textContent = d.num+' — '+d.name;
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
<script>
function toggleMenu(){document.getElementById('dropdownMenu').classList.toggle('open')}
document.addEventListener('click',function(e){if(!e.target.closest('.dropdown'))document.getElementById('dropdownMenu').classList.remove('open')})
function setLang(l){alert('Langue '+l+' — bientôt disponible !')}
</script>
</body>
</html>"""
WHY_PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
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
.block{margin-bottom:80px}
.block-label{font-size:12px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:#7B2FFF;margin-bottom:24px}
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
<nav class="nav">
<a href="/" class="logo">InsideYourMix</a>
<a href="/analyze" class="nav-cta">Try it for free</a>
</nav>
<div class="content">
<div class="badge">💡 Notre histoire</div>
<h1>Pourquoi<br><span>InsideYourMix</span> ?</h1>

<div class="stats">
<div class="stat"><div class="stat-number">50M+</div><p>Producteurs dans le monde</p></div>
<div class="stat"><div class="stat-number">300€+</div><p>Le coût d'une session avec un ingénieur son</p></div>
<div class="stat"><div class="stat-number">7</div><p>Dimensions analysées dans chaque mix</p></div>
<div class="stat"><div class="stat-number">100+</div><p>Genres musicaux référencés</p></div>
</div>

<div class="block">
<div class="block-label">❌ Le problème</div>
<h2>Tu mixes dans le vide.</h2>
<p>50 millions de producteurs dans le monde travaillent leurs sons seuls, sans feedback technique précis. Un ingénieur son professionnel coûte plusieurs centaines d'euros la session — inaccessible pour la majorité. Et à force d'écouter ton propre mix en boucle, tu perds toute perspective. Tu ne sais plus ce qui sonne vraiment et ce qui doit être corrigé.</p>
</div>

<div class="arrow">↓</div>

<div class="solution-block">
<div class="block-label">✅ La solution</div>
<h2>Un coach technique disponible 24h/24, au prix d'un café.</h2>
<p>InsideYourMix analyse ton mix sur 7 dimensions techniques précises — fréquentiel, dynamique, stéréo, rythme, timbre, espace, balance temporelle. Notre IA compare ton travail aux standards de ton genre et te livre un rapport coaching personnalisé, concret et actionnable. En quelques minutes, tu sais exactement sur quoi travailler pour atteindre les standards de l'industrie.</p>
</div>

<div class="block">
<div class="block-label">🎯 Pour qui ?</div>
<h2>Pour tous les producteurs qui veulent progresser vite.</h2>
<p>Que tu sois débutant qui cherche à comprendre les bases du mixage, ou producteur expérimenté qui veut valider ses sessions — InsideYourMix te donne un regard extérieur objectif et technique à chaque fois que tu en as besoin.</p>
</div>

<div class="cta-section">
<h3>Prêt à découvrir ce que cache ton mix ?</h3>
<a href="/analyze" class="hero-cta">Analyser mon mix gratuitement</a>
</div>
</div>
</body>
</html>"""

CONTACT_PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
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
<nav class="nav">
<a href="/" class="logo">InsideYourMix</a>
<a href="/analyze" class="nav-cta">Try it for free</a>
</nav>
<div class="content">
<div class="badge">📩 Nous contacter</div>
<h1>On est là pour <span>t'aider</span>.</h1>
<p class="intro">Une question sur l'outil ? Un bug à signaler ? Une idée à partager ? On répond à tout.</p>

<a href="mailto:insideyourmix.contact@gmail.com" class="contact-card">
<div class="contact-icon">✉️</div>
<div class="contact-info">
<h3>Email</h3>
<p>insideyourmix.contact@gmail.com</p>
</div>
</a>

<a href="https://instagram.com/insideyourmix" target="_blank" class="contact-card">
<div class="contact-icon">📸</div>
<div class="contact-info">
<h3>Instagram</h3>
<p>@insideyourmix · Actualités, tips, coulisses</p>
</div>
</a>
</div>
</body>
</html>"""

ABONNEMENTS_PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
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
.badge{display:inline-block;padding:8px 16px;background:rgba(123,47,255,0.15);border:1px solid rgba(123,47,255,0.3);border-radius:24px;font-size:12px;font-weight:600;color:#00E5FF;margin-bottom:32px;letter-spacing:0.05em;text-transform:uppercase}
h1{font-family:'Syne',sans-serif;font-size:clamp(40px,6vw,72px);font-weight:800;margin-bottom:24px;letter-spacing:-0.03em;text-align:center}
h1 span{background:linear-gradient(90deg,#7B2FFF,#00E5FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.intro{opacity:0.75;font-size:18px;line-height:1.7;margin-bottom:80px;text-align:center;max-width:600px;margin-left:auto;margin-right:auto}
.plans{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:24px;margin-bottom:64px}
.plan{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:24px;padding:40px 32px;transition:all 0.3s;position:relative}
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
<nav class="nav">
<a href="/" class="logo">InsideYourMix</a>
<a href="/analyze" class="nav-cta">Try it for free</a>
</nav>
<div class="content">
<div style="text-align:center">
<div class="badge">💳 Tarifs</div>
</div>
<h1>Simple, <span>transparent</span>.</h1>
<p class="intro">Commence gratuitement. Upgrade quand tu es prêt.</p>

<div class="plans">
<div class="plan">
<div class="plan-name">Gratuit</div>
<div class="plan-price">0€</div>
<div class="plan-period">pour toujours</div>
<ul class="plan-features">
<li>3 analyses offertes</li>
<li>Les 3 modes disponibles</li>
<li>Rapport complet</li>
<li>0,50€ par analyse supplémentaire</li>
</ul>
<span class="plan-cta">Commencer</span>
</div>

<div class="plan featured">
<div class="plan-badge">⚡ Populaire</div>
<div class="plan-name">Starter</div>
<div class="plan-price">3€</div>
<div class="plan-period">par mois</div>
<ul class="plan-features">
<li>20 analyses par mois</li>
<li>Les 3 modes disponibles</li>
<li>Rapport complet</li>
<li>Historique de tes analyses</li>
</ul>
<span class="plan-cta coming">Bientôt disponible</span>
</div>

<div class="plan">
<div class="plan-name">Pro</div>
<div class="plan-price">7,90€</div>
<div class="plan-period">par mois</div>
<ul class="plan-features">
<li>100 analyses par mois</li>
<li>Les 3 modes disponibles</li>
<li>Rapport complet</li>
<li>Historique + export PDF</li>
</ul>
<span class="plan-cta coming">Bientôt disponible</span>
</div>

<div class="plan">
<div class="plan-name">Studio</div>
<div class="plan-price">29€</div>
<div class="plan-period">par mois</div>
<ul class="plan-features">
<li>Analyses illimitées</li>
<li>Multi-utilisateurs</li>
<li>Idéal labels et écoles</li>
<li>Support prioritaire</li>
</ul>
<span class="plan-cta coming">Bientôt disponible</span>
</div>
</div>

<div class="coming-soon">
🔐 Système de comptes et paiement en ligne — <strong>Bientôt disponible</strong>
</div>
</div>
</body>
</html>"""
HTML_PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>InsideYourMix - Comprends ton mix</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#07070F;color:#F0F0F8;font-family:'DM Sans',sans-serif;overflow-x:hidden}
.bg-gradient{position:fixed;top:0;left:0;width:100%;height:100%;background:radial-gradient(ellipse at top,rgba(123,47,255,0.15) 0%,transparent 50%),radial-gradient(ellipse at bottom right,rgba(0,229,255,0.1) 0%,transparent 50%);z-index:0;pointer-events:none}
.nav{position:fixed;top:0;left:0;right:0;padding:24px 48px;display:flex;justify-content:space-between;align-items:center;z-index:100;background:rgba(7,7,15,0.7);backdrop-filter:blur(20px);border-bottom:1px solid rgba(255,255,255,0.05)}
.logo{font-family:'Syne',sans-serif;font-weight:800;font-size:22px;background:linear-gradient(90deg,#F0F0F8,#7B2FFF,#00E5FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;letter-spacing:-0.02em}
.nav-right{display:flex;gap:32px;align-items:center}
.nav-link{color:#F0F0F8;text-decoration:none;font-size:14px;font-weight:500;opacity:0.8;transition:opacity 0.2s}
.nav-link:hover{opacity:1}
.nav-cta{background:linear-gradient(90deg,#7B2FFF,#00E5FF);color:white;padding:10px 24px;border-radius:24px;text-decoration:none;font-weight:600;font-size:14px;transition:transform 0.2s}
.nav-cta:hover{transform:translateY(-1px)}
.hero{min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:120px 24px 80px;position:relative;z-index:1}
.badge{display:inline-block;padding:8px 16px;background:rgba(123,47,255,0.15);border:1px solid rgba(123,47,255,0.3);border-radius:24px;font-size:12px;font-weight:600;color:#00E5FF;margin-bottom:32px;letter-spacing:0.05em;text-transform:uppercase}
.hero h1{font-family:'Syne',sans-serif;font-size:clamp(48px,8vw,96px);font-weight:800;line-height:1.05;margin-bottom:32px;letter-spacing:-0.03em;max-width:1100px}
.hero h1 .accent{background:linear-gradient(90deg,#7B2FFF,#00E5FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hero p{font-size:clamp(16px,1.5vw,20px);max-width:700px;margin-bottom:48px;opacity:0.75;line-height:1.6;font-weight:400}
.hero-cta{display:inline-flex;align-items:center;gap:12px;background:linear-gradient(90deg,#7B2FFF,#00E5FF);color:white;padding:20px 48px;border-radius:32px;text-decoration:none;font-weight:700;font-size:18px;box-shadow:0 10px 40px rgba(123,47,255,0.4);transition:all 0.3s}
.hero-cta:hover{transform:translateY(-2px);box-shadow:0 20px 60px rgba(123,47,255,0.6)}
.hero-cta svg{width:20px;height:20px}
.hero-note{margin-top:20px;font-size:13px;opacity:0.5}
.modes{padding:120px 48px;position:relative;z-index:1;max-width:1400px;margin:0 auto}
.section-title{font-family:'Syne',sans-serif;font-size:clamp(32px,5vw,56px);font-weight:700;text-align:center;margin-bottom:24px;letter-spacing:-0.02em}
.section-subtitle{text-align:center;opacity:0.6;font-size:18px;margin-bottom:80px;max-width:600px;margin-left:auto;margin-right:auto}
.modes-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:24px}
.mode-card{background:linear-gradient(180deg,rgba(255,255,255,0.04),rgba(255,255,255,0.01));border:1px solid rgba(255,255,255,0.08);border-radius:24px;padding:40px 32px;transition:all 0.3s;position:relative;overflow:hidden}
.mode-card:hover{transform:translateY(-4px);border-color:rgba(123,47,255,0.4);background:linear-gradient(180deg,rgba(123,47,255,0.08),rgba(0,229,255,0.03))}
.mode-icon{width:56px;height:56px;border-radius:16px;background:linear-gradient(135deg,#7B2FFF,#00E5FF);display:flex;align-items:center;justify-content:center;font-size:28px;margin-bottom:24px}
.mode-card h3{font-family:'Syne',sans-serif;font-size:24px;font-weight:700;margin-bottom:12px}
.mode-card p{opacity:0.7;line-height:1.6;font-size:15px}
.why{padding:120px 48px;position:relative;z-index:1;max-width:1200px;margin:0 auto}
.why-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:32px;margin-top:64px}
.why-item{padding:32px 24px}
.why-number{font-family:'Syne',sans-serif;font-size:48px;font-weight:800;background:linear-gradient(90deg,#7B2FFF,#00E5FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:16px}
.why-item h4{font-family:'Syne',sans-serif;font-size:20px;margin-bottom:12px;font-weight:600}
.why-item p{opacity:0.65;line-height:1.6;font-size:15px}
.final-cta{padding:120px 48px;text-align:center;position:relative;z-index:1}
.final-cta h2{font-family:'Syne',sans-serif;font-size:clamp(36px,6vw,64px);font-weight:700;margin-bottom:24px;letter-spacing:-0.02em;line-height:1.1}
.final-cta p{opacity:0.7;font-size:18px;margin-bottom:40px;max-width:500px;margin-left:auto;margin-right:auto}
footer{padding:48px;text-align:center;opacity:0.4;font-size:14px;border-top:1px solid rgba(255,255,255,0.05);position:relative;z-index:1}
@media(max-width:768px){.nav{padding:16px 24px}.nav-right{gap:16px}.modes,.why,.final-cta{padding:80px 24px}}
.dropdown{position:relative}
.menu-btn{background:none;border:1px solid rgba(255,255,255,0.2);border-radius:8px;padding:8px 12px;cursor:pointer;display:flex;flex-direction:column;gap:5px}
.menu-btn span{display:block;width:22px;height:2px;background:#F0F0F8;border-radius:2px}
.dropdown-menu{position:absolute;top:52px;right:0;background:rgba(15,15,25,0.97);backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,0.1);border-radius:16px;padding:12px;min-width:240px;display:none;flex-direction:column;gap:4px;z-index:1000;box-shadow:0 20px 60px rgba(0,0,0,0.5)}
.dropdown-menu.open{display:flex}
.dropdown-item{color:#F0F0F8;text-decoration:none;padding:12px 16px;border-radius:10px;font-size:15px;font-weight:500;transition:background 0.2s}
.dropdown-item:hover{background:rgba(123,47,255,0.2)}
.dropdown-divider{height:1px;background:rgba(255,255,255,0.08);margin:8px 0}
.lang-selector{display:flex;gap:8px;padding:8px 16px;justify-content:center}
.lang-flag{font-size:22px;cursor:pointer;opacity:0.7;transition:all 0.2s;border-radius:4px;padding:4px}
.lang-flag:hover{opacity:1;transform:scale(1.2)}
</style>
</head>
<body>
<video autoplay muted loop playsinline id="bgVideo" style="position:fixed;top:0;left:0;width:100%;height:100%;object-fit:cover;z-index:0;opacity:0.18;pointer-events:none">
<source src="https://videos.pexels.com/video-files/7087635/7087635-uhd_1440_2732_25fps.mp4" type="video/mp4">
</video>
<div class="bg-gradient"></div>
<nav class="nav">
<a href="/" class="logo">InsideYourMix</a>
<div class="nav-right">
<div class="dropdown">
<button class="menu-btn" onclick="toggleMenu()">
<span></span><span></span><span></span>
</button>
<div class="dropdown-menu" id="dropdownMenu">
<a href="/how-it-works" class="dropdown-item">How it works</a>
<a href="/why" class="dropdown-item">Why InsideYourMix</a>
<a href="/abonnements" class="dropdown-item">Abonnements</a>
<a href="/contact" class="dropdown-item">Contact</a>
<div class="dropdown-divider"></div>
<div class="lang-selector">
<span onclick="setLang('fr')" class="lang-flag" title="Français">🇫🇷</span>
<span onclick="setLang('en')" class="lang-flag" title="English">🇬🇧</span>
<span onclick="setLang('es')" class="lang-flag" title="Español">🇪🇸</span>
<span onclick="setLang('de')" class="lang-flag" title="Deutsch">🇩🇪</span>
<span onclick="setLang('pt')" class="lang-flag" title="Português">🇵🇹</span>
</div>
</div>
</div>
<a href="/analyze" class="nav-cta">Try it for free</a>
</div>
</nav>
<section class="hero">
<div class="badge"> AI Mix Analysis · Première mondiale</div>
<h1>Analyse ton <span class="accent">MIX</span>.<br>Perfectionne ton <span class="accent">SON</span>.</h1>
<p>Upload ton mix, choisis ton style. Reçois un rapport technique ultra-précis qui te dit exactement sur quoi travailler pour atteindre les standards de l'industrie.</p>
<a href="/analyze" class="hero-cta">
Try it for free
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
</a>
<div class="hero-note">Gratuit · Aucune inscription requise</div>
</section>
<section class="modes">
<h2 class="section-title">3 modes d'analyse</h2>
<p class="section-subtitle">Choisis l'approche qui correspond à ton workflow</p>
<div class="modes-grid">
<div class="mode-card">
<div class="mode-icon">1</div>
<h3>Mode Genre</h3>
<p>Compare ton mix aux standards techniques de ton style musical. Plus de 100 genres analysés — Techno, House, Hip-Hop, Drum & Bass, et bien plus.</p>
</div>
<div class="mode-card">
<div class="mode-icon">2</div>
<h3>Mode Référence</h3>
<p>Upload tes morceaux préférés et reçois une analyse comparative détaillée. Notre coach te montre exactement ce qui sépare ton mix de tes références.</p>
</div>
<div class="mode-card">
<div class="mode-icon">3</div>
<h3>Mode Hybride</h3>
<p>Le meilleur des deux mondes. Combine standards de genre et morceaux de référence pour une analyse ultime et un coaching sur-mesure.</p>
</div>
</div>
</section>
<section class="why">
<h2 class="section-title">Pourquoi InsideYourMix ?</h2>
<p class="section-subtitle">Made for producers, by producers</p>
<div class="why-grid">
<div class="why-item">
<div class="why-number">01</div>
<h4>Analyse technique précise</h4>
<p>7 dimensions analysées — fréquentiel, dynamique, stéréo, rythme, timbre, espace, balance temporelle. Aucun détail ne t'échappe.</p>
</div>
<div class="why-item">
<div class="why-number">02</div>
<h4>Conseils actionnables</h4>
<p>Chaque rapport est personnalisé. Notre coach IA te donne des pistes concrètes adaptées à ton genre et à ton niveau.</p>
</div>
<div class="why-item">
<div class="why-number">03</div>
<h4>100+ genres référencés</h4>
<p>De la Techno minimale à l'Amapiano en passant par le Dubstep — chaque style a ses propres standards techniques.</p>
</div>
<div class="why-item">
<div class="why-number">04</div>
<h4>Aucune installation</h4>
<p>Tout se passe dans ton navigateur. Upload, analyse, rapport. Simple, rapide, accessible partout.</p>
</div>
</div>
</section>
<section class="final-cta">
<h2>Prêt à <span style="background:linear-gradient(90deg,#7B2FFF,#00E5FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text">passer au niveau supérieur</span> ?</h2>
<p>Découvre ce que ton mix cache vraiment. Gratuit, instantané, sans inscription.</p>
<a href="/analyze" class="hero-cta">
Analyser mon mix
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
</a>
</section>
<footer>© 2026 InsideYourMix · Made for producers, by producers</footer>
<script>
function toggleMenu(){document.getElementById('dropdownMenu').classList.toggle('open')}
document.addEventListener('click',function(e){if(!e.target.closest('.dropdown'))document.getElementById('dropdownMenu').classList.remove('open')})
function setLang(l){alert('Langue '+l+' — bientôt disponible !')}
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