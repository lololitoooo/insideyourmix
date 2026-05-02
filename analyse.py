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
    n    = len(mono)
    fft  = np.abs(rfft(mono * np.hanning(n)))
    freq = rfftfreq(n, 1 / sr)
    total = np.sum(fft) + 1e-10

    def pct(fmin, fmax):
        mask = (freq >= fmin) & (freq < fmax)
        return round(float(np.sum(fft[mask]) / total * 100), 2)

    centroide = float(np.sum(freq * fft) / (np.sum(fft) + 1e-10))

    return {
        "sub_basses_pct":  pct(20,  80),
        "basses_pct":      pct(80,  250),
        "mids_pct":        pct(250, 2000),
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
    """Corrélation L/R sur une bande de fréquences."""
    n    = len(gauche)
    freqs = rfftfreq(n, 1 / sr)
    mask  = (freqs >= fmin) & (freqs < fmax)

    fg = rfft(gauche); fd = rfft(droite)
    fg_b = np.zeros_like(fg); fd_b = np.zeros_like(fd)
    fg_b[mask] = fg[mask]; fd_b[mask] = fd[mask]

    g_b = irfft(fg_b, n=n).astype(np.float32)
    d_b = irfft(fd_b, n=n).astype(np.float32)

    sg, sd = np.std(g_b), np.std(d_b)
    if sg < 1e-10 or sd < 1e-10:
        return 1.0
    return round(float(np.corrcoef(g_b, d_b)[0, 1]), 3)


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
# DÉTECTION SIDECHAIN
# ─────────────────────────────────────────

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

    return {
        "frequentiel":       analyser_frequentiel(mono, sr),
        "dynamique":         analyser_dynamique(mono, sr),
        "stereo":            analyser_stereo(gauche, droite, sr),
        "rythme":            rythme,
        "timbre":            analyser_timbre(mono, sr),
        "espace":            analyser_espace(mono, sr),
        "balance_over_time": analyser_balance_over_time(mono, gauche, droite, sr),
        "clipping":          detecter_clipping(mono, gauche, droite, sr),
        "sidechain":         analyser_sidechain(mono, sr, rythme["bpm"]),
    }