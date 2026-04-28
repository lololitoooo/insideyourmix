import os
import json
from dotenv import load_dotenv
import anthropic
from analyse import analyser_audio

# Charger la clé API
load_dotenv()
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

def generer_rapport(fichier_audio, genre="Techno"):
    
    # 1. Analyser le fichier audio
    print("Analyse en cours...")
    donnees = analyser_audio(fichier_audio)
    
    # 2. Préparer le résumé pour Claude
    freq = donnees["frequentiel"]
    dyn  = donnees["dynamique"]
    ster = donnees["stereo"]
    ryt  = donnees["rythme"]
    esp  = donnees["espace"]
    bot  = donnees["balance_over_time"]
    
    resume = f"""
Genre cible : {genre}

ANALYSE FRÉQUENTIELLE :
- Sub-basses : {freq['sub_basses_pct']}%
- Basses : {freq['basses_pct']}%
- Mids : {freq['mids_pct']}%
- Hauts-mids : {freq['hauts_mids_pct']}%
- Aigus : {freq['aigus_pct']}%
- Centroïde spectral : {freq['centroide_hz']} Hz

DYNAMIQUE & LOUDNESS :
- RMS : {dyn['rms_db']} dB
- Peak : {dyn['peak_db']} dB
- LUFS approximé : {dyn['lufs_approx']} dB
- Crest Factor : {dyn['crest_factor_db']} dB
- Dynamic Range : {dyn['dynamic_range_db']} dB

CHAMP STÉRÉO :
- Corrélation : {ster['correlation']}
- Largeur stéréo : {ster['largeur_stereo']}
- Balance L/R : {ster['balance_lr']}

RYTHME :
- BPM : {ryt['bpm']}
- Onset strength : {ryt['onset_strength']}
- Régularité beat : {ryt['regularite_beat']}

ESPACE :
- Reverb score : {esp['reverb_score']}
- Densité mix : {esp['densite_mix']}

BALANCE OVER TIME :
{json.dumps(bot['events'], indent=2)}
"""

    # 3. Envoyer à Claude pour générer le rapport
    print("\nGénération du rapport IA...\n")
    
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": f"""Tu es un ingénieur du son expert spécialisé en musique électronique.
                
Voici les données d'analyse d'un mix audio :

{resume}

Génère un rapport de mix professionnel en français avec :
1. Un résumé global en 2-3 phrases
2. Les points forts du mix
3. Les points à améliorer (sois précis et technique)
4. Les 3 priorités d'action concrètes pour améliorer ce mix dans le style {genre}
5. Est-ce que ce mix est prêt pour le streaming ? (Spotify, Beatport)

Sois direct, précis et actionnable. Parle comme un vrai ingénieur du son."""
            }
        ]
    )
    
    rapport = message.content[0].text
    print("=" * 50)
    print("RAPPORT INSIDEYOURMIX")
    print("=" * 50)
    print(rapport)
    print("=" * 50)
    
    return rapport

# Test
generer_rapport("test.mp3", genre="Techno")