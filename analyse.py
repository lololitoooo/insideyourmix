import numpy as np
import soundfile as sf
import scipy.signal as signal
from scipy.fft import rfft, rfftfreq

def charger_audio(fichier, genre=""):
    y, sr = sf.read(fichier, always_2d=False)
    if y.ndim == 2:
        gauche = y[:, 0]
        droite = y[:, 1]
        mono = y.mean(axis=1)
    else:
        gauche = y
        droite = y
        mono = y

    duree_totale = len(mono) / sr

    # Genres club avec intro DJ longue → on skip le début
    genres_club = [
        "techno", "house", "deep house", "tech house", "melodic techno",
        "melodic house", "progressive house", "trance", "drum and bass",
        "dnb", "dubstep", "hardstyle", "hardcore", "afro house",
        "minimal", "minimal techno", "organic house", "psytrance",
        "amapiano", "uk garage", "hard techno", "industrial techno"
    ]

    genre_lower = genre.lower()
    est_club = any(g in genre_lower for g in genres_club)

    if est_club and duree_totale > 90:
        # Ignorer la première minute pour les genres club
        debut = int(sr * 60)
        raison = "genre club — intro DJ ignorée"
    else:
        # Analyser depuis le début pour tous les autres genres
        debut = 0
        raison = "analyse complète depuis le début"

    fin = len(mono)  # toujours jusqu'à la fin

    mono_crop   = mono[debut:fin].astype(np.float32)
    gauche_crop = gauche[debut:fin].astype(np.float32)
    droite_crop = droite[debut:fin].astype(np.float32)

    print(f"   Durée totale     : {duree_totale:.1f}s")
    print(f"   Zone analysée    : {debut//sr}s → {int(fin/sr)}s ({raison})")
    print(f"   Durée analysée   : {len(mono_crop)/sr:.1f}s")

    return mono_crop, gauche_crop, droite_crop, sr

# ── 01 FRÉQUENTIEL ────────────────────────────────────────────────────────────
def analyser_frequentiel(y, sr):
    fft = np.abs(rfft(y))
    freqs = rfftfreq(len(y), 1/sr)
    total = np.sum(fft) + 1e-10

    sub_idx    = np.where((freqs >= 20)   & (freqs < 80))
    basses_idx = np.where((freqs >= 80)   & (freqs < 250))
    mids_idx   = np.where((freqs >= 250)  & (freqs < 2000))
    hmids_idx  = np.where((freqs >= 2000) & (freqs < 6000))
    aigus_idx  = np.where((freqs >= 6000) & (freqs < 20000))

    pct = lambda idx: round(float(np.sum(fft[idx]) / total * 100), 1)

    centroide = float(np.sum(freqs * fft) / total)
    rolloff_idx = np.where(np.cumsum(fft) >= 0.85 * np.sum(fft))[0]
    rolloff = float(freqs[rolloff_idx[0]]) if len(rolloff_idx) > 0 else 0.0

    return {
        "sub_basses_pct":  pct(sub_idx),
        "basses_pct":      pct(basses_idx),
        "mids_pct":        pct(mids_idx),
        "hauts_mids_pct":  pct(hmids_idx),
        "aigus_pct":       pct(aigus_idx),
        "centroide_hz":    round(centroide),
        "rolloff_hz":      round(rolloff),
    }

# ── 02 DYNAMIQUE & LOUDNESS ───────────────────────────────────────────────────
def analyser_dynamique(y, sr):
    rms = float(np.sqrt(np.mean(y**2)))
    rms_db = round(20 * np.log10(rms + 1e-10), 2)
    peak = float(np.max(np.abs(y)))
    peak_db = round(20 * np.log10(peak + 1e-10), 2)
    crest_factor = round(peak_db - rms_db, 2)
    lufs_approx = round(rms_db - 0.691, 2)

    hop = 1024
    rms_frames = [
        np.sqrt(np.mean(y[i:i+hop]**2))
        for i in range(0, len(y) - hop, hop)
    ]
    rms_frames = np.array(rms_frames)
    rms_frames_db = 20 * np.log10(rms_frames + 1e-10)
    dr = round(float(
        np.percentile(rms_frames_db, 95) -
        np.percentile(rms_frames_db, 10)
    ), 2)

    return {
        "rms_db":           rms_db,
        "peak_db":          peak_db,
        "lufs_approx":      lufs_approx,
        "crest_factor_db":  crest_factor,
        "dynamic_range_db": dr,
    }

# ── 03 CHAMP STÉRÉO ───────────────────────────────────────────────────────────
def analyser_stereo(gauche, droite):
    min_len = min(len(gauche), len(droite))
    g = gauche[:min_len]
    d = droite[:min_len]

    if np.std(g) > 0 and np.std(d) > 0:
        correlation = float(np.corrcoef(g, d)[0, 1])
    else:
        correlation = 1.0

    mid  = (g + d) / 2
    side = (g - d) / 2
    mid_energy  = float(np.mean(mid**2))
    side_energy = float(np.mean(side**2))
    largeur = round(side_energy / (mid_energy + 1e-10), 4)
    balance = round(float(np.mean(np.abs(g)) - np.mean(np.abs(d))), 4)

    return {
        "correlation":    round(correlation, 3),
        "largeur_stereo": largeur,
        "balance_lr":     balance,
        "mid_energy":     round(mid_energy, 4),
        "side_energy":    round(side_energy, 4),
    }

# ── 04 RYTHME & TEMPO ─────────────────────────────────────────────────────────
def analyser_rythme(y, sr):
    hop = 512
    frame_size = 1024
    energies = np.array([
        float(np.sum(y[i:i+frame_size]**2))
        for i in range(0, len(y) - frame_size, hop)
    ])

    fps = sr / hop
    corr = np.correlate(energies, energies, mode='full')
    corr = corr[len(corr)//2:]

    lag_min = int(fps * 60 / 200)
    lag_max = int(fps * 60 / 60)
    segment = corr[lag_min:lag_max]
    peak = np.argmax(segment) + lag_min
    bpm = round((fps * 60) / peak, 1)

    diff = np.diff(energies)
    onset_strength = round(float(np.mean(np.abs(diff))), 4)
    regularite = round(float(
        1 - np.std(diff) / (np.mean(np.abs(diff)) + 1e-10)
    ), 3)

    return {
        "bpm":             bpm,
        "onset_strength":  onset_strength,
        "regularite_beat": regularite,
    }

# ── 05 TIMBRE & TEXTURE ───────────────────────────────────────────────────────
def analyser_timbre(y, sr):
    n_fft = 2048
    hop = 512
    n_mels = 40
    n_mfcc = 13

    frames = np.array([
        y[i:i+n_fft] * np.hanning(n_fft)
        for i in range(0, len(y) - n_fft, hop)
    ])

    spectre = np.abs(rfft(frames, axis=1))

    freqs = rfftfreq(n_fft, 1/sr)
    mel_min = 2595 * np.log10(1 + 20/700)
    mel_max = 2595 * np.log10(1 + sr/2/700)
    mel_points = np.linspace(mel_min, mel_max, n_mels + 2)
    hz_points = 700 * (10**(mel_points/2595) - 1)

    filterbank = np.zeros((n_mels, len(freqs)))
    for m in range(1, n_mels + 1):
        f_m_minus = hz_points[m-1]
        f_m = hz_points[m]
        f_m_plus = hz_points[m+1]
        for k, f in enumerate(freqs):
            if f_m_minus <= f <= f_m:
                filterbank[m-1, k] = (f - f_m_minus) / (f_m - f_m_minus + 1e-10)
            elif f_m <= f <= f_m_plus:
                filterbank[m-1, k] = (f_m_plus - f) / (f_m_plus - f_m + 1e-10)

    mel_spectre = np.dot(spectre, filterbank.T)
    mel_log = np.log(mel_spectre + 1e-10)

    from scipy.fft import dct
    mfccs = dct(mel_log, type=2, axis=1, norm='ortho')[:, :n_mfcc]
    mfccs_mean = np.mean(mfccs, axis=0)

    flatness = float(np.mean(
        np.exp(np.mean(np.log(spectre.mean(axis=0) + 1e-10))) /
        (np.mean(spectre.mean(axis=0)) + 1e-10)
    ))

    return {
        "mfccs": [round(float(v), 3) for v in mfccs_mean],
        "spectral_flatness": round(flatness, 4),
    }

# ── 06 ESPACE & PROFONDEUR ────────────────────────────────────────────────────
def analyser_espace(y, sr):
    hop = 512
    energies = np.array([
        float(np.mean(y[i:i+hop]**2))
        for i in range(0, len(y) - hop, hop)
    ])

    peaks, _ = signal.find_peaks(energies, height=np.mean(energies))
    decay_rates = []
    for p in peaks[:10]:
        end = min(p + 50, len(energies) - 1)
        segment = energies[p:end]
        if len(segment) > 5 and segment[0] > 0:
            decay = float(segment[-1] / (segment[0] + 1e-10))
            decay_rates.append(decay)

    reverb_score = round(
        float(np.mean(decay_rates)) if decay_rates else 0.0, 3
    )

    fft = np.abs(rfft(y))
    freq_occupee = np.sum(fft > np.mean(fft) * 0.1)
    densite = round(float(freq_occupee / len(fft)), 3)

    return {
        "reverb_score": reverb_score,
        "densite_mix":  densite,
    }

# ── 07 BALANCE OVER TIME ──────────────────────────────────────────────────────
def analyser_balance_over_time(y, sr):
    segment_dur = 8  # 8 secondes par segment pour mieux capturer les phases
    segment_len = sr * segment_dur
    n_segments = len(y) // segment_len

    segments_data = []
    for i in range(n_segments):
        seg = y[i*segment_len:(i+1)*segment_len]
        rms = float(np.sqrt(np.mean(seg**2)))
        rms_db = round(20 * np.log10(rms + 1e-10), 2)

        fft = np.abs(rfft(seg))
        freqs = rfftfreq(len(seg), 1/sr)
        total = np.sum(fft) + 1e-10

        basses = float(np.sum(fft[np.where((freqs>=20)&(freqs<250))]) / total * 100)
        mids   = float(np.sum(fft[np.where((freqs>=250)&(freqs<4000))]) / total * 100)
        aigus  = float(np.sum(fft[np.where((freqs>=4000)&(freqs<20000))]) / total * 100)

        segments_data.append({
            "t_start": i * segment_dur,
            "t_end":   (i+1) * segment_dur,
            "rms_db":  rms_db,
            "basses":  round(basses, 1),
            "mids":    round(mids, 1),
            "aigus":   round(aigus, 1),
        })

    # Détecter drops et montées
    energies = [s["rms_db"] for s in segments_data]
    events = []
    for i in range(1, len(energies)):
        diff = energies[i] - energies[i-1]
        if diff > 2.5:
            events.append({
                "type": "DROP/MONTEE",
                "t": segments_data[i]["t_start"],
                "delta_db": round(diff, 1)
            })
        elif diff < -2.5:
            events.append({
                "type": "BREAKDOWN",
                "t": segments_data[i]["t_start"],
                "delta_db": round(diff, 1)
            })

    return {
        "segments": segments_data,
        "events":   events,
    }

# ── MOTEUR PRINCIPAL ──────────────────────────────────────────────────────────
def analyser_audio(fichier, genre=""):
    print(f"\n{'='*50}")
    print(f"  InsideYourMix — Analyse complète")
    print(f"  Fichier : {fichier}")
    print(f"{'='*50}\n")

    mono, gauche, droite, sr = charger_audio(fichier, genre)

    print("\n01 — FRÉQUENTIEL")
    freq = analyser_frequentiel(mono, sr)
    for k, v in freq.items():
        print(f"   {k:<22} : {v}")

    print("\n02 — DYNAMIQUE & LOUDNESS")
    dyn = analyser_dynamique(mono, sr)
    for k, v in dyn.items():
        print(f"   {k:<22} : {v}")

    print("\n03 — CHAMP STÉRÉO")
    stereo = analyser_stereo(gauche, droite)
    for k, v in stereo.items():
        print(f"   {k:<22} : {v}")

    print("\n04 — RYTHME & TEMPO")
    rythme = analyser_rythme(mono, sr)
    for k, v in rythme.items():
        print(f"   {k:<22} : {v}")

    print("\n05 — TIMBRE & TEXTURE")
    timbre = analyser_timbre(mono, sr)
    print(f"   spectral_flatness    : {timbre['spectral_flatness']}")
    print(f"   mfccs (13)           : {timbre['mfccs'][:5]}...")

    print("\n06 — ESPACE & PROFONDEUR")
    espace = analyser_espace(mono, sr)
    for k, v in espace.items():
        print(f"   {k:<22} : {v}")

    print("\n07 — BALANCE OVER TIME")
    bot = analyser_balance_over_time(mono, sr)
    for seg in bot["segments"]:
        print(f"   {seg['t_start']:>3}s-{seg['t_end']:>3}s | "
              f"RMS:{seg['rms_db']:>7} dB | "
              f"B:{seg['basses']:>5}% "
              f"M:{seg['mids']:>5}% "
              f"A:{seg['aigus']:>5}%")
    if bot["events"]:
        print(f"\n   Événements détectés :")
        for e in bot["events"]:
            print(f"   → {e['type']} à {e['t']}s ({e['delta_db']:+} dB)")
    else:
        print("   Aucun événement majeur détecté")

    print(f"\n{'='*50}")
    print("  Analyse terminée")
    print(f"{'='*50}\n")

    return {
        "frequentiel":       freq,
        "dynamique":         dyn,
        "stereo":            stereo,
        "rythme":            rythme,
        "timbre":            timbre,
        "espace":            espace,
        "balance_over_time": bot,
    }

# Test direct
if __name__ == "__main__":
    analyser_audio("test.mp3")