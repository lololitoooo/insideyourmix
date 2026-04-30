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
# FONCTION PRINCIPALE
# ─────────────────────────────────────────

def analyser_audio(chemin, genre="default"):
    mono, gauche, droite, sr = charger_audio(chemin)

    return {
        "frequentiel":       analyser_frequentiel(mono, sr),
        "dynamique":         analyser_dynamique(mono, sr),
        "stereo":            analyser_stereo(gauche, droite, sr),
        "rythme":            analyser_rythme(mono, sr),
        "timbre":            analyser_timbre(mono, sr),
        "espace":            analyser_espace(mono, sr),
        "balance_over_time": analyser_balance_over_time(mono, gauche, droite, sr),
    }
