import numpy as np
from numpy.fft import rfft, rfftfreq, irfft
from scipy.signal import resample_poly, correlate
import soundfile as sf

# ─────────────────────────────────────────
# CHARGEMENT AUDIO
# ─────────────────────────────────────────

def charger_audio(chemin, max_duree=180):
    data, sr = sf.read(chemin, always_2d=True)

    if data.shape[1] >= 2:
        gauche = data[:, 0].astype(np.float32)
        droite = data[:, 1].astype(np.float32)
    else:
        gauche = data[:, 0].astype(np.float32)
        droite = gauche.copy()

    mono = (gauche + droite) / 2

    fin = min(len(mono), int(sr * max_duree))
    mono   = mono[:fin]
    gauche = gauche[:fin]
    droite = droite[:fin]

    # Downsample à 22050 Hz pour économiser CPU et RAM
    if sr > 22050:
        ratio_den = int(sr / 22050)
        mono   = resample_poly(mono,   1, ratio_den).astype(np.float32)
        gauche = resample_poly(gauche, 1, ratio_den).astype(np.float32)
        droite = resample_poly(droite, 1, ratio_den).astype(np.float32)
        sr = 22050

    return mono, gauche, droite, int(sr)


# ─────────────────────────────────────────
# 1. ANALYSE FRÉQUENTIELLE
# ─────────────────────────────────────────

def analyser_frequentiel(mono, sr):
    """
    Analyse la balance spectrale par STFT (court-terme) avec énergie (amplitude²).
    
    Pourquoi STFT et pas FFT globale :
    - La fenêtre Hanning sur le signal entier (3 min) atténue le début et la fin
    - On découpe en frames de 4096 échantillons (~185ms), on moyennne les spectres
    
    Pourquoi énergie et pas amplitude :
    - La bande mids a ~30× plus de bins FFT que le sub
    - Sommer les amplitudes biaise massivement vers les mids
    - L'énergie (amplitude²) donne le vrai contenu énergétique par bande
    """
    FRAME   = 4096
    HOP     = 2048
    window  = np.hanning(FRAME)

    # Accumuler l'énergie par bin FFT sur toutes les frames
    power   = np.zeros(FRAME // 2 + 1, dtype=np.float64)
    n_frames = 0
    for i in range(0, len(mono) - FRAME, HOP):
        frame   = mono[i:i + FRAME] * window
        power  += np.abs(rfft(frame)) ** 2
        n_frames += 1

    if n_frames == 0:
        # Fallback si signal trop court
        frame  = mono[:FRAME] if len(mono) >= FRAME else np.pad(mono, (0, FRAME - len(mono)))
        power  = np.abs(rfft(frame * np.hanning(FRAME))) ** 2

    power_avg = power / max(n_frames, 1)
    freq      = rfftfreq(FRAME, 1 / sr)
    total     = np.sum(power_avg) + 1e-10

    def pct(fmin, fmax):
        mask = (freq >= fmin) & (freq < fmax)
        return round(float(np.sum(power_avg[mask]) / total * 100), 2)

    # Centroïde spectral (pondéré par énergie pour cohérence)
    centroide = float(np.sum(freq * power_avg) / (np.sum(power_avg) + 1e-10))

    return {
        "sub_basses_pct":  pct(20,   80),
        "basses_pct":      pct(80,   250),
        "mids_pct":        pct(250,  2000),
        "hauts_mids_pct":  pct(2000, 6000),
        "aigus_pct":       pct(6000, sr // 2),
        "centroide_hz":    round(centroide, 1),
    }


# ─────────────────────────────────────────
# 2. DYNAMIQUE & LOUDNESS — True LUFS BS.1770
# ─────────────────────────────────────────

def analyser_dynamique(mono, sr):
    # ── RMS & Peak ──
    rms_lin  = float(np.sqrt(np.mean(mono ** 2)))
    rms_db   = round(20 * np.log10(rms_lin + 1e-10), 2)
    peak_lin = float(np.max(np.abs(mono)))
    peak_db  = round(20 * np.log10(peak_lin + 1e-10), 2)

    # ── True LUFS intégré (BS.1770-4 simplifié) ──
    # Filtre de pre-emphasis (K-weighting stage 1 : shelf haute fréquence)
    # Approximation légère sans scipy.signal.lfilter pour économiser la mémoire
    try:
        import pyloudnorm as pyln
        meter = pyln.Meter(sr)
        lufs_integrated = round(float(meter.integrated_loudness(mono.astype(np.float64))), 2)
        # True Peak inter-sample
        true_peak_db = round(float(20 * np.log10(max(pyln.true_peak(mono.astype(np.float64), sr), 1e-10))), 2)
    except Exception:
        # Fallback : approximation LUFS depuis RMS + correction -1 dB
        lufs_integrated = round(rms_db - 0.8, 2)
        true_peak_db    = peak_db

    # ── Short-Term LUFS (fenêtre 3s) ──
    block = int(sr * 3)
    st_vals = []
    for i in range(0, len(mono) - block, block // 2):
        seg = mono[i:i + block]
        r   = float(np.sqrt(np.mean(seg ** 2)))
        if r > 1e-10:
            st_vals.append(20 * np.log10(r) - 0.8)
    lufs_short_term = round(float(np.mean(st_vals)), 2) if st_vals else lufs_integrated

    # ── Crest Factor & Dynamic Range ──
    crest_db = round(peak_db - rms_db, 2)
    block2   = int(sr * 0.5)
    blk_rms  = [
        20 * np.log10(np.sqrt(np.mean(mono[i:i+block2]**2)) + 1e-10)
        for i in range(0, len(mono) - block2, block2)
    ]
    dyn_range = round(float(np.percentile(blk_rms, 95) - np.percentile(blk_rms, 5)), 2) if blk_rms else 0.0

    return {
        # Compatibilité avec l'ancien code
        "lufs_approx":       lufs_integrated,
        # Nouvelles métriques précises
        "lufs_integrated":   lufs_integrated,
        "lufs_short_term":   lufs_short_term,
        "true_peak_db":      true_peak_db,
        "rms_db":            rms_db,
        "peak_db":           peak_db,
        "crest_factor_db":   crest_db,
        "dynamic_range_db":  dyn_range,
    }


# ─────────────────────────────────────────
# 3. CHAMP STÉRÉO — Corrélation par bande
# ─────────────────────────────────────────

def _band_correlation(gauche, droite, sr, fmin, fmax):
    """Corrélation L/R sur une bande via STFT pour éviter la fuite spectrale."""
    FRAME = 4096
    HOP   = 2048
    win   = np.hanning(FRAME)
    corrs = []
    n     = min(len(gauche), len(droite))
    for i in range(0, n - FRAME, HOP):
        gf    = rfft(gauche[i:i+FRAME] * win)
        df    = rfft(droite[i:i+FRAME] * win)
        freqs = rfftfreq(FRAME, 1 / sr)
        mask  = (freqs >= fmin) & (freqs < fmax)
        gf_b  = np.zeros_like(gf); df_b = np.zeros_like(df)
        gf_b[mask] = gf[mask]; df_b[mask] = df[mask]
        g_b = irfft(gf_b, n=FRAME).astype(np.float32)
        d_b = irfft(df_b, n=FRAME).astype(np.float32)
        sg, sd = np.std(g_b), np.std(d_b)
        if sg > 1e-10 and sd > 1e-10:
            corrs.append(float(np.corrcoef(g_b, d_b)[0, 1]))
    return round(float(np.mean(corrs)), 3) if corrs else 1.0


def analyser_stereo(gauche, droite, sr):
    # Corrélation globale
    sg, sd = np.std(gauche), np.std(droite)
    if sg > 1e-10 and sd > 1e-10:
        correlation = round(float(np.corrcoef(gauche, droite)[0, 1]), 3)
    else:
        correlation = 1.0

    # Mid / Side
    mid  = (gauche + droite) / 2
    side = (gauche - droite) / 2
    mid_energy  = round(float(np.sqrt(np.mean(mid  ** 2))), 4)
    side_energy = round(float(np.sqrt(np.mean(side ** 2))), 4)
    largeur     = round(float(side_energy / (mid_energy + 1e-10)), 4)
    balance_lr  = round(float(np.mean(gauche ** 2) - np.mean(droite ** 2)), 5)

    # Corrélation par bande de fréquences
    corr_sub   = _band_correlation(gauche, droite, sr, 20,   80)     # Sub-basses
    corr_bass  = _band_correlation(gauche, droite, sr, 80,   250)    # Basses
    corr_mids  = _band_correlation(gauche, droite, sr, 250,  2000)   # Mids
    corr_highs = _band_correlation(gauche, droite, sr, 2000, sr // 2) # Aigus

    # Interprétation automatique
    def interp_corr(c):
        if c > 0.85: return "MONO"
        if c > 0.5:  return "ETROIT"
        if c > 0.0:  return "NORMAL"
        return "TROP_LARGE"

    return {
        "correlation":     correlation,
        "largeur_stereo":  largeur,
        "balance_lr":      balance_lr,
        "mid_energy":      mid_energy,
        "side_energy":     side_energy,
        # Nouvelles métriques par bande
        "corr_sub":        corr_sub,
        "corr_bass":       corr_bass,
        "corr_mids":       corr_mids,
        "corr_highs":      corr_highs,
        "stereo_sub":      interp_corr(corr_sub),
        "stereo_bass":     interp_corr(corr_bass),
        "stereo_mids":     interp_corr(corr_mids),
        "stereo_highs":    interp_corr(corr_highs),
    }


# ─────────────────────────────────────────
# 4. RYTHME & TEMPO
# ─────────────────────────────────────────

def analyser_rythme(mono, sr):
    # Onset strength (enveloppe de l'énergie par blocs)
    hop    = int(sr * 0.02)
    frames = [mono[i:i+hop] for i in range(0, len(mono) - hop, hop)]
    energy = np.array([np.sqrt(np.mean(f**2)) for f in frames])

    onset = np.diff(energy, prepend=energy[0])
    onset = np.maximum(onset, 0)
    onset_strength = round(float(np.mean(onset) * 1000), 2)

    # BPM par autocorrélation
    bpm = 120.0
    try:
        fps  = sr / hop
        lmin = int(fps * 60 / 200)
        lmax = int(fps * 60 / 60)
        if lmin < lmax and lmax < len(onset):
            auto = correlate(onset, onset, mode="full")
            auto = auto[len(auto)//2:]
            auto = auto[lmin:lmax]
            lag  = np.argmax(auto) + lmin
            bpm  = round(float(fps * 60 / lag), 1)
    except Exception:
        pass

    # Régularité du beat
    regularite = round(float(np.corrcoef(energy[:-1], energy[1:])[0, 1]), 3)

    return {
        "bpm":              bpm,
        "onset_strength":   onset_strength,
        "regularite_beat":  regularite,
    }


# ─────────────────────────────────────────
# 5. TIMBRE & TEXTURE (version légère)
# ─────────────────────────────────────────

def analyser_timbre(mono, sr):
    fft      = np.abs(rfft(mono))
    flatness = float(np.exp(np.mean(np.log(fft + 1e-10))) / (np.mean(fft) + 1e-10))

    return {
        "mfccs":              [0.0] * 13,
        "spectral_flatness":  round(flatness, 4),
    }


# ─────────────────────────────────────────
# 6. ESPACE & PROFONDEUR
# ─────────────────────────────────────────

def analyser_espace(mono, sr):
    # Score de réverbération : variance de l'enveloppe
    hop    = int(sr * 0.05)
    frames = [mono[i:i+hop] for i in range(0, len(mono) - hop, hop)]
    env    = np.array([np.sqrt(np.mean(f**2)) for f in frames])
    reverb = round(float(1 - np.std(env) / (np.mean(env) + 1e-10)), 4)
    reverb = max(0.0, min(1.0, reverb))

    # Densité spectrale
    fft      = np.abs(rfft(mono))
    densite  = round(float(np.sum(fft > np.mean(fft)) / len(fft)), 4)

    return {
        "reverb_score": reverb,
        "densite_mix":  densite,
    }


# ─────────────────────────────────────────
# 7. BALANCE OVER TIME
# ─────────────────────────────────────────

def analyser_balance_over_time(mono, gauche, droite, sr, segment_s=8):
    seg_len   = int(sr * segment_s)
    segments  = []
    events    = []
    prev_rms  = None

    for i in range(0, len(mono) - seg_len, seg_len):
        seg   = mono[i:i+seg_len]
        rms   = float(np.sqrt(np.mean(seg**2)))
        rms_db = round(20 * np.log10(rms + 1e-10), 2)
        t     = round(i / sr, 1)

        # Balance fréquentielle du segment
        fft  = np.abs(rfft(seg))
        freq = rfftfreq(seg_len, 1 / sr)
        tot  = np.sum(fft) + 1e-10
        b_pct = round(float(np.sum(fft[(freq >= 20)  & (freq < 250)]) / tot * 100), 1)
        m_pct = round(float(np.sum(fft[(freq >= 250) & (freq < 2000)]) / tot * 100), 1)
        a_pct = round(float(np.sum(fft[(freq >= 2000)]) / tot * 100), 1)

        segments.append({
            "t": t, "rms_db": rms_db,
            "basses_pct": b_pct, "mids_pct": m_pct, "aigus_pct": a_pct,
        })

        if prev_rms is not None:
            delta = round(rms_db - prev_rms, 2)
            if delta > 4.0:
                events.append({"t": t, "type": "DROP", "delta_db": delta})
            elif delta < -4.0:
                events.append({"t": t, "type": "BREAKDOWN", "delta_db": delta})
        prev_rms = rms_db

    return {"segments": segments, "events": events}



# ─────────────────────────────────────────
# 8. DÉTECTION DE CLIPPING AVEC TIMESTAMPS
# ─────────────────────────────────────────

def detecter_clipping(mono, gauche, droite, sr, seuil_db=-0.3):
    """
    Détecte les moments de saturation/clipping dans le mix.
    seuil_db : seuil en dBFS au-dessus duquel on considère qu'il y a clipping.
    Standard industrie : -0.3 dBFS (True Peak -0.3 dBTP).
    """
    seuil_lin = 10 ** (seuil_db / 20.0)

    # Masque de clipping sur tous les canaux
    clips = (np.abs(mono) >= seuil_lin) | \
            (np.abs(gauche) >= seuil_lin) | \
            (np.abs(droite) >= seuil_lin)

    # Grouper les samples consécutifs en événements
    events = []
    in_clip  = False
    start_i  = 0

    for i in range(len(clips)):
        if clips[i] and not in_clip:
            in_clip = True
            start_i = i
        elif not clips[i] and in_clip:
            in_clip = False
            seg    = mono[start_i:i]
            peak   = float(np.max(np.abs(seg)))
            peak_db = round(20 * np.log10(peak + 1e-10), 2)
            dur_ms  = round((i - start_i) / sr * 1000, 1)
            t_sec   = round(start_i / sr, 2)
            # Formatter le timestamp mm:ss
            minutes = int(t_sec // 60)
            seconds = int(t_sec % 60)
            ts      = f"{minutes}:{seconds:02d}"
            events.append({
                "t":          t_sec,
                "ts":         ts,
                "duration_ms": dur_ms,
                "peak_db":    peak_db,
                "severity":   "fort" if peak_db > -0.1 else "leger",
            })

    # Dédoublonner les événements très proches (< 50ms d'écart)
    filtered = []
    for e in events:
        if not filtered or (e["t"] - filtered[-1]["t"]) > 0.05:
            filtered.append(e)

    # Stats globales
    total_clips  = int(np.sum(clips))
    total_pct    = round(float(total_clips) / max(len(clips), 1) * 100, 3)
    count        = len(filtered)

    # Niveau de sévérité global
    if count == 0:
        severite = "aucun"
    elif count <= 3 and total_pct < 0.01:
        severite = "leger"
    elif count <= 10 and total_pct < 0.1:
        severite = "modere"
    else:
        severite = "severe"

    return {
        "events":      filtered[:25],   # Max 25 timestamps
        "count":       count,
        "total_pct":   total_pct,
        "severite":    severite,
        "has_clipping": count > 0,
        "seuil_db":    seuil_db,
    }


# ─────────────────────────────────────────
# DÉTECTION AUTOMATIQUE DU NIVEAU PRODUCTEUR
# ─────────────────────────────────────────

def detecter_niveau_producteur(donnees):
    """
    Détecte automatiquement le niveau du producteur depuis les données d'analyse.
    Retourne un dict avec : niveau, score_technique, points_forts, points_faibles, instruction_coaching
    """
    dyn  = donnees["dynamique"]
    freq = donnees["frequentiel"]
    ster = donnees["stereo"]
    esp  = donnees["espace"]
    clip = donnees.get("clipping", {})
    ryt  = donnees["rythme"]

    points = 0
    max_points = 0
    forces = []
    faiblesses = []

    # ── LUFS / Loudness ───────────────────────────────────────────────────
    lufs = dyn.get("lufs_integrated", dyn.get("lufs_approx", -20))
    max_points += 3
    if -14 <= lufs <= -6:
        points += 3
        forces.append(f"Loudness maîtrisée ({lufs} LUFS — dans la plage professionnelle)")
    elif -18 <= lufs <= -5:
        points += 1
        faiblesses.append(f"Loudness perfectible ({lufs} LUFS — légèrement hors plage optimale)")
    else:
        faiblesses.append(f"Loudness problématique ({lufs} LUFS — trop éloigné des standards)")

    # ── True Peak ─────────────────────────────────────────────────────────
    tp = dyn.get("true_peak_db", dyn.get("peak_db", 0))
    max_points += 2
    if tp <= -0.3:
        points += 2
        forces.append(f"True Peak sous contrôle ({tp} dBTP)")
    elif tp <= -0.1:
        points += 1
        faiblesses.append(f"True Peak limite ({tp} dBTP — risque de saturation sur certains systèmes)")
    else:
        faiblesses.append(f"True Peak trop élevé ({tp} dBTP — saturation probable)")

    # ── Clipping ──────────────────────────────────────────────────────────
    max_points += 2
    severite_clip = clip.get("severite", "aucun")
    if severite_clip == "aucun":
        points += 2
        forces.append("Aucun clipping détecté — mix propre")
    elif severite_clip == "leger":
        points += 1
        faiblesses.append(f"Clipping léger détecté ({clip.get('count', 0)} événement(s))")
    else:
        faiblesses.append(f"Clipping {severite_clip} ({clip.get('count', 0)} événements) — problème critique")

    # ── Crest Factor (dynamique) ───────────────────────────────────────────
    crest = dyn.get("crest_factor_db", 0)
    max_points += 2
    if 6 <= crest <= 18:
        points += 2
        forces.append(f"Dynamique équilibrée (crest factor {crest} dB)")
    elif 4 <= crest <= 22:
        points += 1
        faiblesses.append(f"Dynamique perfectible (crest factor {crest} dB)")
    else:
        faiblesses.append(f"Dynamique problématique (crest factor {crest} dB — {'trop compressé' if crest < 4 else 'trop dynamique'})")

    # ── Balance fréquentielle ─────────────────────────────────────────────
    sub  = freq["sub_basses_pct"]
    bass = freq["basses_pct"]
    mids = freq["mids_pct"]
    hauts= freq["hauts_mids_pct"]
    aigus= freq["aigus_pct"]
    max_points += 3

    # Vérifier l'équilibre (pas de bande qui dépasse 45% ou est en dessous de 3%)
    bandes = {"sub": sub, "basses": bass, "mids": mids, "hauts-mids": hauts, "aigus": aigus}
    desequilibres = [(k, v) for k, v in bandes.items() if v > 45 or v < 3]
    if not desequilibres:
        points += 3
        forces.append("Balance spectrale équilibrée sur toutes les bandes")
    elif len(desequilibres) == 1:
        points += 1
        k, v = desequilibres[0]
        faiblesses.append(f"Léger déséquilibre sur les {k} ({v}%)")
    else:
        faiblesses.append(f"Balance fréquentielle déséquilibrée ({', '.join([f'{k}={v}%' for k,v in desequilibres])})")

    # ── Stéréo ────────────────────────────────────────────────────────────
    corr_sub  = ster.get("corr_sub", 1.0)
    corr_bass = ster.get("corr_bass", 1.0)
    max_points += 3

    stereo_score = 0
    # Sub doit être MONO (corr > 0.85)
    if corr_sub >= 0.85:
        stereo_score += 1
        forces.append(f"Sub-basses en mono ({corr_sub}) — standard professionnel respecté")
    else:
        faiblesses.append(f"Sub-basses trop larges en stéréo (corr={corr_sub}) — risque de problèmes en club")
    # Basses doivent être ETROIT (corr > 0.65)
    if corr_bass >= 0.65:
        stereo_score += 1
    else:
        faiblesses.append(f"Basses trop larges en stéréo (corr={corr_bass})")
    # Corrélation globale > 0.3 (pas de problème de phase)
    corr_global = ster.get("correlation", 1.0)
    if corr_global >= 0.3:
        stereo_score += 1
    else:
        faiblesses.append(f"Problème de phase potentiel (corrélation globale {corr_global})")
    points += stereo_score

    # ── BPM cohérent ─────────────────────────────────────────────────────
    max_points += 1
    bpm = ryt["bpm"]
    if 60 < bpm < 200:
        points += 1

    # ── Calcul du niveau ─────────────────────────────────────────────────
    ratio = points / max_points if max_points > 0 else 0

    if ratio >= 0.80:
        niveau = "avance"
        label  = "Avancé"
        instruction = ("Tu parles à un producteur avancé qui maîtrise les bases. "
                       "Va directement dans les détails techniques fins. "
                       "Parle de micro-réglages, de nuances subtiles, de positionnement dans la scène. "
                       "Pas besoin d'expliquer ce qu'est un sidechain ou un EQ — tu peux aller beaucoup plus loin.")
    elif ratio >= 0.55:
        niveau = "intermediaire"
        label  = "Intermédiaire"
        instruction = ("Tu parles à un producteur intermédiaire qui a les bases mais progresse encore. "
                       "Explique le pourquoi de chaque conseil avec des valeurs précises. "
                       "Encourage sans condescendance, sois concret et actionnable. "
                       "Mentionne des outils et techniques spécifiques adaptés à son niveau.")
    else:
        niveau = "debutant"
        label  = "Débutant/Apprenti"
        instruction = ("Tu parles à un producteur débutant ou apprenti qui apprend encore. "
                       "Sois très encourageant et positif avant tout — il a besoin de confiance. "
                       "Explique les concepts clairement sans jargon excessif. "
                       "Concentre-toi sur les 2-3 choses les plus impactantes, pas sur tout à la fois. "
                       "Termine toujours sur une note positive et motivante.")

    return {
        "niveau":        niveau,
        "label":         label,
        "score_technique": round(ratio * 100),
        "points_forts":  forces[:4],    # Max 4 pour ne pas surcharger
        "points_faibles": faiblesses[:4],
        "instruction":   instruction,
    }






# ─────────────────────────────────────────
# PUNCH DU KICK (80-120Hz sur les temps forts)
# ─────────────────────────────────────────

def analyser_punch_kick(mono, sr, bpm):
    """
    Mesure le punch réel du kick en isolant l'énergie transitoire
    dans la bande 80-120Hz sur les temps forts détectés.
    Distingue un kick qui frappe vraiment d'un kick qui "existe" seulement.
    """
    from numpy.fft import rfft, rfftfreq, irfft

    if bpm <= 0:
        return {"punch_db": 0, "punch_score": 0, "verdict": "indéterminé",
                "attaque_ms": 0, "sustain_ratio": 0, "nb_kicks": 0}

    # ── Isoler la bande kick (80-120Hz) ──────────────────────────────────
    n       = len(mono)
    freqs   = rfftfreq(n, 1.0 / sr)
    fft_s   = rfft(mono)
    mask    = (freqs >= 80) & (freqs <= 120)
    fft_f   = np.zeros_like(fft_s)
    fft_f[mask] = fft_s[mask]
    kick_band = irfft(fft_f, n=n).astype(np.float32)

    # ── Enveloppe RMS haute résolution (5ms) ─────────────────────────────
    hop        = max(1, int(sr * 0.005))
    n_frames   = (len(kick_band) - hop) // hop
    env        = np.array([np.sqrt(np.mean(kick_band[i*hop:(i+1)*hop]**2))
                           for i in range(n_frames)])

    if np.max(env) < 1e-8:
        return {"punch_db": 0, "punch_score": 0, "verdict": "bande kick silencieuse",
                "attaque_ms": 0, "sustain_ratio": 0, "nb_kicks": 0}

    # ── Détecter les peaks de kick (temps forts) ─────────────────────────
    # Interval attendu entre deux kicks (en frames)
    beat_frames = int(sr * 60.0 / bpm / hop)
    min_gap     = int(beat_frames * 0.7)  # tolérance rythmique 30%

    # Chercher les maxima locaux au-dessus du seuil
    threshold   = np.percentile(env, 75)  # seuil = 75e percentile
    peaks       = []
    last_peak   = -min_gap

    for i in range(1, len(env) - 1):
        if (env[i] > env[i-1] and env[i] >= env[i+1]
                and env[i] > threshold
                and i - last_peak >= min_gap):
            peaks.append(i)
            last_peak = i

    if len(peaks) < 2:
        return {"punch_db": 0, "punch_score": 20, "verdict": "kick peu défini",
                "attaque_ms": 0, "sustain_ratio": 0, "nb_kicks": len(peaks)}

    # ── Mesurer punch, attaque et sustain sur chaque kick ────────────────
    punches     = []
    attaques_ms = []
    sustains    = []
    window_post = min(int(beat_frames * 0.8), 100)  # fenêtre d'analyse post-kick

    for pk in peaks:
        # Niveau de fond AVANT le kick
        pre_start  = max(0, pk - int(beat_frames * 0.3))
        fond_level = np.mean(env[pre_start:pk]) if pk > pre_start else 1e-8
        if fond_level < 1e-10:
            fond_level = 1e-10

        # Niveau de pic
        pic_level  = env[pk]

        # Punch = différence pic/fond en dB
        if pic_level > fond_level:
            punch = 20 * np.log10(pic_level / fond_level)
            punches.append(punch)

        # Vitesse d'attaque : temps pour passer de 10% à 90% du pic
        # (chercher en arrière depuis le pic)
        level_10 = fond_level + (pic_level - fond_level) * 0.10
        level_90 = fond_level + (pic_level - fond_level) * 0.90
        t_10 = pk
        for j in range(pk, max(0, pk - 30), -1):
            if env[j] < level_10:
                t_10 = j
                break
        t_90 = pk
        for j in range(pk, max(0, pk - 30), -1):
            if env[j] < level_90:
                t_90 = j
                break
        attaque_frames = max(1, pk - t_90)
        attaques_ms.append(attaque_frames * 5)  # 5ms par frame

        # Ratio sustain : énergie moyenne 50-150ms après le kick vs pic
        post_start = pk + int(0.050 / 0.005)  # 50ms après
        post_end   = min(len(env), pk + int(0.150 / 0.005))  # 150ms après
        if post_end > post_start:
            sustain_level = np.mean(env[post_start:post_end])
            sustains.append(sustain_level / (pic_level + 1e-10))

    if not punches:
        return {"punch_db": 0, "punch_score": 20, "verdict": "punch non mesurable",
                "attaque_ms": 0, "sustain_ratio": 0, "nb_kicks": len(peaks)}

    avg_punch   = float(np.mean(punches))
    avg_attaque = float(np.mean(attaques_ms)) if attaques_ms else 0
    avg_sustain = float(np.mean(sustains)) if sustains else 0

    # ── Score de punch (0-100) ────────────────────────────────────────────
    # Référence pro : 8-15dB de punch, attaque < 20ms, sustain 0.2-0.5
    score = 0
    # Punch en dB (max à 14dB)
    score += min(40, int(avg_punch / 14 * 40))
    # Attaque (rapide = meilleur, < 15ms = max)
    if avg_attaque > 0:
        score += max(0, int(30 - avg_attaque * 1.5))
    # Sustain équilibré (0.25-0.45 = idéal)
    if 0.20 <= avg_sustain <= 0.50:
        score += 30
    elif 0.10 <= avg_sustain < 0.20 or 0.50 < avg_sustain <= 0.65:
        score += 15
    score = min(100, max(0, score))

    # Verdict
    if avg_punch >= 12 and avg_attaque < 20:
        verdict = "excellent — kick très défini et impactant"
    elif avg_punch >= 8:
        verdict = "bon — kick présent avec du punch"
    elif avg_punch >= 5:
        verdict = "moyen — kick audible mais manque d'impact"
    elif avg_punch >= 3:
        verdict = "faible — kick peu défini, manque de punch"
    else:
        verdict = "très faible — kick noyé dans le mix"

    return {
        "punch_db":     round(avg_punch, 1),
        "punch_score":  score,
        "verdict":      verdict,
        "attaque_ms":   round(avg_attaque, 1),
        "sustain_ratio": round(avg_sustain, 3),
        "nb_kicks":     len(peaks),
    }


# ─────────────────────────────────────────
# CLASSIFICATION DES SECTIONS (intro/build/drop/breakdown/outro)
# ─────────────────────────────────────────

def classifier_sections(mono, gauche, droite, sr, bpm):
    """
    Détecte et classifie les sections musicales du morceau :
    intro, build, drop, breakdown, outro.
    Basé sur l'énergie RMS, la densité spectrale et la dynamique locale.
    """
    if bpm <= 0:
        return {"sections": [], "nb_sections": 0, "resume": "BPM non détecté"}

    # ── Paramètres ────────────────────────────────────────────────────────
    # Segments de 4 temps (1 mesure) avec overlap 50%
    beats_per_seg = 4
    seg_dur       = beats_per_seg * 60.0 / bpm
    seg_len       = int(seg_dur * sr)
    hop_len       = seg_len // 2
    n_segs        = max(1, (len(mono) - seg_len) // hop_len)

    if n_segs < 4:
        return {"sections": [], "nb_sections": 0, "resume": "Morceau trop court pour classifier"}

    from numpy.fft import rfft, rfftfreq, irfft

    def get_band(signal, fmin, fmax):
        n     = len(signal)
        f     = rfftfreq(n, 1.0 / sr)
        fft_s = rfft(signal)
        mask  = (f >= fmin) & (f <= fmax)
        fft_f = np.zeros_like(fft_s)
        fft_f[mask] = fft_s[mask]
        return irfft(fft_f, n=n).astype(np.float32)

    # ── Calculer les features par segment ────────────────────────────────
    features = []
    for i in range(n_segs):
        start = i * hop_len
        end   = start + seg_len
        seg   = mono[start:min(end, len(mono))]
        if len(seg) < seg_len // 2:
            continue

        # RMS global
        rms_global = float(np.sqrt(np.mean(seg ** 2)))

        # Énergie sub-basses (kick/basse — indicateur de drop)
        sub = get_band(seg, 20, 150)
        rms_sub = float(np.sqrt(np.mean(sub ** 2)))

        # Énergie hauts-mids (présence, énergie perçue)
        highs = get_band(seg, 2000, 8000)
        rms_highs = float(np.sqrt(np.mean(highs ** 2)))

        # Densité spectrale (combien de fréquences sont actives)
        fft_seg  = np.abs(rfft(seg))
        densite  = float(np.sum(fft_seg > np.mean(fft_seg)) / len(fft_seg))

        # Variabilité temporelle (breakdowns = moins variable)
        hop_var  = max(1, seg_len // 20)
        env_var  = np.array([np.sqrt(np.mean(seg[j:j+hop_var]**2))
                             for j in range(0, len(seg)-hop_var, hop_var)])
        variabilite = float(np.std(env_var) / (np.mean(env_var) + 1e-10))

        # Stéréo width (breakdowns souvent plus larges)
        if len(gauche) > end and len(droite) > end:
            g_seg = gauche[start:end]
            d_seg = droite[start:end]
            width = 1.0 - abs(float(np.corrcoef(g_seg, d_seg)[0, 1]))
        else:
            width = 0.5

        features.append({
            "idx":        i,
            "t_start":    start / sr,
            "t_end":      min(end, len(mono)) / sr,
            "rms":        rms_global,
            "rms_sub":    rms_sub,
            "rms_highs":  rms_highs,
            "densite":    densite,
            "variabilite": variabilite,
            "width":      width,
        })

    if not features:
        return {"sections": [], "nb_sections": 0, "resume": "Impossible de calculer les features"}

    # ── Normaliser les features pour la classification ────────────────────
    rms_vals  = np.array([f["rms"] for f in features])
    sub_vals  = np.array([f["rms_sub"] for f in features])
    dens_vals = np.array([f["densite"] for f in features])
    rms_max   = np.max(rms_vals) + 1e-10
    sub_max   = np.max(sub_vals) + 1e-10
    dens_max  = np.max(dens_vals) + 1e-10

    # ── Score d'énergie composite par segment ─────────────────────────────
    energie_scores = []
    for f in features:
        score = (f["rms"] / rms_max * 0.40
                 + f["rms_sub"] / sub_max * 0.35
                 + f["densite"] / dens_max * 0.25)
        energie_scores.append(score)

    energie_scores = np.array(energie_scores)

    # ── Classifier chaque segment ─────────────────────────────────────────
    n     = len(features)
    seuil_drop  = np.percentile(energie_scores, 70)
    seuil_break = np.percentile(energie_scores, 30)

    labels_raw = []
    for i, (f, e) in enumerate(zip(features, energie_scores)):
        # Position relative dans le morceau
        pos = i / max(n - 1, 1)

        if e >= seuil_drop:
            # Haute énergie → drop ou peak
            label = "drop"
        elif e <= seuil_break:
            # Basse énergie → breakdown ou intro/outro
            if pos < 0.15:
                label = "intro"
            elif pos > 0.80:
                label = "outro"
            else:
                label = "breakdown"
        else:
            # Énergie intermédiaire → build (si énergie montante) ou transition
            if i > 0 and energie_scores[i] > energie_scores[i-1]:
                label = "build"
            else:
                label = "transition"

        labels_raw.append(label)

    # ── Lisser les labels (éviter les alternances rapides) ────────────────
    labels = labels_raw.copy()
    for i in range(1, len(labels) - 1):
        if labels[i] != labels[i-1] and labels[i] != labels[i+1]:
            labels[i] = labels[i-1]  # corriger les isolés

    # ── Fusionner les segments consécutifs de même type ───────────────────
    sections = []
    current_label = labels[0]
    current_start = features[0]["t_start"]
    current_e_max = energie_scores[0]

    for i in range(1, len(labels)):
        if labels[i] == current_label:
            current_e_max = max(current_e_max, energie_scores[i])
        else:
            sections.append({
                "type":    current_label,
                "t_start": round(current_start, 1),
                "t_end":   round(features[i]["t_start"], 1),
                "duree":   round(features[i]["t_start"] - current_start, 1),
                "energie": round(float(current_e_max), 3),
            })
            current_label = labels[i]
            current_start = features[i]["t_start"]
            current_e_max = energie_scores[i]

    # Dernière section
    sections.append({
        "type":    current_label,
        "t_start": round(current_start, 1),
        "t_end":   round(features[-1]["t_end"], 1),
        "duree":   round(features[-1]["t_end"] - current_start, 1),
        "energie": round(float(current_e_max), 3),
    })

    # ── Comparer les sections drop vs breakdown ────────────────────────────
    drops     = [s for s in sections if s["type"] == "drop"]
    breakdowns= [s for s in sections if s["type"] == "breakdown"]
    intros    = [s for s in sections if s["type"] == "intro"]
    builds    = [s for s in sections if s["type"] == "build"]

    observations = []
    if drops and breakdowns:
        e_drop  = np.mean([s["energie"] for s in drops])
        e_break = np.mean([s["energie"] for s in breakdowns])
        ratio   = e_drop / (e_break + 1e-10)
        if ratio > 2.5:
            observations.append(f"Contraste drop/breakdown fort ({round(ratio,1)}x) — structure dynamique efficace")
        elif ratio > 1.5:
            observations.append(f"Contraste drop/breakdown modéré ({round(ratio,1)}x) — peut être accentué")
        else:
            observations.append(f"Peu de contraste drop/breakdown ({round(ratio,1)}x) — le drop ne ressort pas assez")

    if drops:
        avg_drop_dur = np.mean([s["duree"] for s in drops])
        if avg_drop_dur < 16:
            observations.append(f"Drops courts ({round(avg_drop_dur,0)}s) — peuvent sembler trop brefs")
        elif avg_drop_dur > 64:
            observations.append(f"Drops très longs ({round(avg_drop_dur,0)}s) — énergie bien maintenue")

    if builds:
        avg_build_dur = np.mean([s["duree"] for s in builds])
        observations.append(f"Build(s) moyen(s) de {round(avg_build_dur,0)}s")

    resume = f"{len(sections)} section(s) : " + ", ".join(
        f"{s['type']} ({s['t_start']}s-{s['t_end']}s)" for s in sections
    )

    return {
        "sections":      sections,
        "nb_sections":   len(sections),
        "nb_drops":      len(drops),
        "nb_breakdowns": len(breakdowns),
        "nb_builds":     len(builds),
        "observations":  observations,
        "resume":        resume,
    }


# ─────────────────────────────────────────
# DÉTECTION TONALITÉ
# ─────────────────────────────────────────


def analyser_tonalite(mono, sr):
    """
    Détecte la tonalité et la clé musicale du mix via Chromagram.
    Analyse la bande 200Hz-4kHz pour éviter l'influence du kick sur la détection.
    Utilise les profils Krumhansl-Schmuckler pour identifier la gamme.
    Retourne : clé, mode (majeur/mineur), notation Camelot, confiance, fondamentale Hz.
    """
    # ── Profils Krumhansl-Schmuckler ──────────────────────────────────────
    # Profil majeur et mineur (pondération de chaque degré de la gamme)
    PROFIL_MAJEUR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                               2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    PROFIL_MINEUR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                               2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

    NOTES     = ['Do', 'Do#', 'Re', 'Re#', 'Mi', 'Fa',
                 'Fa#', 'Sol', 'Sol#', 'La', 'La#', 'Si']
    NOTES_EN  = ['C', 'C#', 'D', 'D#', 'E', 'F',
                 'F#', 'G', 'G#', 'A', 'A#', 'B']

    # Roue Camelot : (note_index, mode) → code Camelot
    CAMELOT = {
        (0,  'majeur'): '8B',  (0,  'mineur'): '5A',
        (1,  'majeur'): '3B',  (1,  'mineur'): '12A',
        (2,  'majeur'): '10B', (2,  'mineur'): '7A',
        (3,  'majeur'): '5B',  (3,  'mineur'): '2A',
        (4,  'majeur'): '12B', (4,  'mineur'): '9A',
        (5,  'majeur'): '7B',  (5,  'mineur'): '4A',
        (6,  'majeur'): '2B',  (6,  'mineur'): '11A',
        (7,  'majeur'): '9B',  (7,  'mineur'): '6A',
        (8,  'majeur'): '4B',  (8,  'mineur'): '1A',
        (9,  'majeur'): '11B', (9,  'mineur'): '8A',
        (10, 'majeur'): '6B',  (10, 'mineur'): '3A',
        (11, 'majeur'): '1B',  (11, 'mineur'): '10A',
    }

    # Fondamentales en Hz pour chaque note (octave 2-3, plage basse)
    FONDAMENTALES = {
        'Do': 65.4, 'Do#': 69.3, 'Re': 73.4, 'Re#': 77.8,
        'Mi': 82.4, 'Fa': 87.3, 'Fa#': 92.5, 'Sol': 98.0,
        'Sol#': 103.8, 'La': 110.0, 'La#': 116.5, 'Si': 123.5
    }

    # ── Filtrage bande harmonique (200Hz - 4kHz) ──────────────────────────
    n      = len(mono)
    freqs  = rfftfreq(n, 1.0 / sr)
    fft_s  = rfft(mono)
    mask   = (freqs >= 200) & (freqs <= 4000)
    fft_f  = np.zeros_like(fft_s)
    fft_f[mask] = fft_s[mask]
    signal_harm = irfft(fft_f, n=n).astype(np.float32)

    if np.max(np.abs(signal_harm)) < 1e-8:
        return {
            "cle": "Indéterminée", "mode": "inconnu", "camelot": "?",
            "confiance": 0.0, "fondamentale_hz": 0,
            "top3": [], "detail": "Signal harmonique trop faible"
        }

    # ── Calcul du Chromagram ──────────────────────────────────────────────
    # Fréquences de référence pour chaque note (A4 = 440Hz)
    A4   = 440.0
    chroma = np.zeros(12)

    # Segmenter le signal en frames de 100ms avec overlap 50%
    frame_len  = int(sr * 0.10)
    hop_len    = int(sr * 0.05)
    n_frames   = max(1, (len(signal_harm) - frame_len) // hop_len)

    for i_frame in range(n_frames):
        frame  = signal_harm[i_frame * hop_len: i_frame * hop_len + frame_len]
        if len(frame) < frame_len:
            break
        # Fenêtre de Hanning pour réduire les artefacts
        frame  = frame * np.hanning(len(frame))
        fft_f  = np.abs(rfft(frame))
        freqs_f = rfftfreq(len(frame), 1.0 / sr)

        # Accumuler l'énergie par classe de hauteur (chroma)
        for bin_i, freq in enumerate(freqs_f):
            if freq < 100 or freq > 5000:
                continue
            # Convertir la fréquence en numéro de note MIDI puis en chroma
            midi = 69 + 12 * np.log2(freq / A4 + 1e-10)
            chroma_bin = int(round(midi)) % 12
            chroma[chroma_bin] += fft_f[bin_i] ** 2  # énergie au carré

    if np.sum(chroma) < 1e-10:
        return {
            "cle": "Indéterminée", "mode": "inconnu", "camelot": "?",
            "confiance": 0.0, "fondamentale_hz": 0,
            "top3": [], "detail": "Chromagram vide"
        }

    # Normaliser
    chroma = chroma / (np.max(chroma) + 1e-10)

    # ── Corrélation avec les 24 profils ──────────────────────────────────
    resultats = []
    for root in range(12):
        for mode_name, profil in [('majeur', PROFIL_MAJEUR), ('mineur', PROFIL_MINEUR)]:
            profil_rot = np.roll(profil, root)
            profil_rot = profil_rot / (np.max(profil_rot) + 1e-10)
            # Corrélation de Pearson
            corr = float(np.corrcoef(chroma, profil_rot)[0, 1])
            resultats.append((corr, root, mode_name))

    resultats.sort(key=lambda x: x[0], reverse=True)
    best_corr, best_root, best_mode = resultats[0]

    # ── Score de confiance ────────────────────────────────────────────────
    # Basé sur l'écart entre le 1er et le 2ème meilleur résultat
    second_corr = resultats[1][0]
    gap         = best_corr - second_corr
    confiance   = round(min(1.0, max(0.0, gap * 5.0 + best_corr * 0.3)), 3)

    note_fr  = NOTES[best_root]
    note_en  = NOTES_EN[best_root]
    camelot  = CAMELOT.get((best_root, best_mode), '?')
    fondamentale = FONDAMENTALES.get(note_fr, 0)

    # Top 3 alternatives
    top3 = []
    for corr, root, mode in resultats[1:4]:
        top3.append(f"{NOTES[root]} {mode} ({CAMELOT.get((root, mode), '?')}) — corr={round(corr, 3)}")

    # Analyse cohérence kick (fondamentale basse vs tonalité)
    # La fondamentale standard du kick est souvent autour de 60-120Hz
    # On peut estimer la note du kick depuis l'analyse fréquentielle basse
    kick_fondamentale_note = ""
    kick_freq_est = fondamentale  # par défaut on suppose cohérence

    detail = (f"Corrélation Krumhansl: {round(best_corr, 3)} | "
              f"Écart vs 2ème: {round(gap, 3)} | "
              f"Chroma dominant: {NOTES[np.argmax(chroma)]}")

    return {
        "cle":             f"{note_fr} {best_mode}",
        "cle_en":          f"{note_en} {best_mode}",
        "note":            note_fr,
        "note_en":         note_en,
        "mode":            best_mode,
        "camelot":         camelot,
        "confiance":       confiance,
        "fondamentale_hz": fondamentale,
        "correlation":     round(best_corr, 3),
        "top3":            top3,
        "detail":          detail,
        "chroma":          chroma.tolist(),
    }


def analyser_sidechain(mono, sr, bpm):
    """
    Détecte la présence et les caractéristiques d'une compression sidechain.
    Retourne un dict avec métriques + certitude explicite.
    On ne rapporte que si 4 conditions simultanées sont vraies.
    """
    from numpy.fft import rfft, rfftfreq, irfft

    HOP_MS  = 10
    hop     = max(1, int(sr * HOP_MS / 1000))

    def bandpass(signal, fmin, fmax):
        n     = len(signal)
        freqs = rfftfreq(n, 1 / sr)
        fft_s = rfft(signal)
        mask  = (freqs >= fmin) & (freqs <= fmax)
        fft_f = np.zeros_like(fft_s)
        fft_f[mask] = fft_s[mask]
        return irfft(fft_f, n=n).astype(np.float32)

    bass_band = bandpass(mono, 80, 300)
    kick_band = bandpass(mono, 60, 120)

    n_frames = (len(bass_band) - hop) // hop
    if n_frames < 16:
        return {"detected": False, "certitude": "impossible", "raison": "Signal trop court",
                "profondeur_db": 0, "regularite": 0, "release_ms": 0, "profondeur_interp": "indéterminé"}

    env_bass = np.array([np.sqrt(np.mean(bass_band[i*hop:(i+1)*hop]**2)) for i in range(n_frames)])
    env_kick = np.array([np.sqrt(np.mean(kick_band[i*hop:(i+1)*hop]**2)) for i in range(n_frames)])

    if np.max(env_bass) < 1e-8 or np.max(env_kick) < 1e-8:
        return {"detected": False, "certitude": "impossible", "raison": "Signal trop silencieux",
                "profondeur_db": 0, "regularite": 0, "release_ms": 0, "profondeur_interp": "indéterminé"}

    env_bass_n = env_bass / (np.max(env_bass) + 1e-10)
    env_kick_n = env_kick / (np.max(env_kick) + 1e-10)

    # Détection des onsets de kick
    diff_kick  = np.diff(env_kick_n, prepend=env_kick_n[0])
    onset_mask = diff_kick > (np.std(diff_kick) * 1.5)
    onset_times = np.where(onset_mask)[0]

    min_gap_frames = int(sr * 30 / bpm / hop * 0.4) if bpm > 0 else int(sr * 0.15 / hop)
    filtered_onsets = []
    last = -min_gap_frames
    for o in onset_times:
        if o - last >= min_gap_frames:
            filtered_onsets.append(o)
            last = o

    if len(filtered_onsets) < 4:
        return {"detected": False, "certitude": "faible",
                "raison": f"Pas assez d'onsets kick ({len(filtered_onsets)})",
                "profondeur_db": 0, "regularite": 0, "release_ms": 0,
                "profondeur_interp": "indéterminé", "nb_kicks": len(filtered_onsets), "nb_creux": 0}

    pre_frames  = max(1, int(0.010 * sr / hop))
    post_frames = min(n_frames // 4, int(0.300 * sr / hop))
    creux_db, releases_ms = [], []

    for onset in filtered_onsets:
        start_ref = max(0, onset - pre_frames)
        ref_level = np.mean(env_bass_n[start_ref:onset + 1])
        if ref_level < 1e-6:
            continue
        end_post = min(n_frames, onset + post_frames)
        window   = env_bass_n[onset:end_post]
        if len(window) < 3:
            continue
        min_level = np.min(window)
        min_idx   = np.argmin(window)
        if min_level < 1e-8:
            continue
        creux = 20 * np.log10(min_level / (ref_level + 1e-10))
        recovery_threshold = ref_level * 0.70
        release_frames = post_frames
        for j in range(min_idx, len(window)):
            if window[j] >= recovery_threshold:
                release_frames = j - min_idx
                break
        if creux < -1.0:
            creux_db.append(creux)
            releases_ms.append(release_frames * HOP_MS)

    if len(creux_db) < 4:
        return {"detected": False, "certitude": "faible",
                "raison": f"Creux insuffisants ({len(creux_db)}/{len(filtered_onsets)} kicks)",
                "profondeur_db": round(float(np.mean(creux_db)), 1) if creux_db else 0,
                "regularite": 0, "release_ms": 0, "profondeur_interp": "indéterminé",
                "nb_kicks": len(filtered_onsets), "nb_creux": len(creux_db)}

    avg_creux   = float(np.mean(creux_db))
    std_creux   = float(np.std(creux_db))
    avg_release = float(np.mean(releases_ms))

    # Régularité rythmique
    gaps        = np.diff(filtered_onsets) if len(filtered_onsets) >= 4 else [1]
    cv_gap      = np.std(gaps) / (np.mean(gaps) + 1e-10)
    regularite  = float(max(0.0, min(1.0, 1.0 - cv_gap)))

    # Score de certitude global
    ratio_creux  = len(creux_db) / max(len(filtered_onsets), 1)
    consistance  = 1.0 - min(1.0, std_creux / (abs(avg_creux) + 1e-10))
    score_cert   = (
        min(1.0, abs(avg_creux) / 8.0) * 0.35
        + regularite                    * 0.30
        + ratio_creux                   * 0.20
        + consistance                   * 0.15
    )

    # 4 conditions simultanées obligatoires
    cond_profondeur = abs(avg_creux)  >= 2.5
    cond_regularite = regularite      >= 0.45
    cond_ratio      = ratio_creux     >= 0.55
    cond_certitude  = score_cert      >= 0.35
    detected        = cond_profondeur and cond_regularite and cond_ratio and cond_certitude

    if not detected:
        certitude = "non detecte"
    elif score_cert >= 0.70:
        certitude = "forte"
    elif score_cert >= 0.50:
        certitude = "moderee"
    else:
        certitude = "faible"

    if abs(avg_creux) < 2.5:      interp = "absent ou très subtil"
    elif abs(avg_creux) < 5.0:    interp = "léger (subtil, musical)"
    elif abs(avg_creux) < 9.0:    interp = "modéré (bien présent)"
    elif abs(avg_creux) < 14.0:   interp = "prononcé (pumping marqué)"
    else:                          interp = "très agressif (pumping extrême)"

    raisons = []
    if not detected:
        if not cond_profondeur: raisons.append(f"creux trop faibles ({abs(avg_creux):.1f}dB < 2.5dB)")
        if not cond_regularite: raisons.append(f"rythme irrégulier (rég. {regularite:.2f})")
        if not cond_ratio:      raisons.append(f"peu présent ({ratio_creux*100:.0f}% kicks)")

    return {
        "detected":          detected,
        "certitude":         certitude,
        "profondeur_db":     round(avg_creux, 1),
        "profondeur_interp": interp,
        "regularite":        round(regularite, 3),
        "release_ms":        round(avg_release, 0),
        "ratio_kicks":       round(ratio_creux, 2),
        "score_certitude":   round(score_cert, 2),
        "nb_kicks":          len(filtered_onsets),
        "nb_creux":          len(creux_db),
        "raison":            " | ".join(raisons) if raisons else "",
    }


# ─────────────────────────────────────────
# FONCTION PRINCIPALE
# ─────────────────────────────────────────

def analyser_audio(chemin, genre="default"):
    mono, gauche, droite, sr = charger_audio(chemin)
    rythme = analyser_rythme(mono, sr)
    bpm    = rythme["bpm"]

    return {
        "frequentiel":       analyser_frequentiel(mono, sr),
        "dynamique":         analyser_dynamique(mono, sr),
        "stereo":            analyser_stereo(gauche, droite, sr),
        "rythme":            rythme,
        "timbre":            analyser_timbre(mono, sr),
        "espace":            analyser_espace(mono, sr),
        "balance_over_time": analyser_balance_over_time(mono, gauche, droite, sr),
        "clipping":          detecter_clipping(mono, gauche, droite, sr),
        "sidechain":         analyser_sidechain(mono, sr, bpm),
        "tonalite":          analyser_tonalite(mono, sr),
        "punch_kick":        analyser_punch_kick(mono, sr, bpm),
        "sections":          classifier_sections(mono, gauche, droite, sr, bpm),
    }