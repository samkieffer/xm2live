#!/usr/bin/env python3
"""
Module de parsing de fichiers Amiga ProTracker Module (.mod)
Extrait les samples, patterns et notes MIDI
"""

import sys
import os
import struct
import wave
import numpy as np
from pathlib import Path
from collections import defaultdict


# Table de conversion Period → Note MIDI
# Basée sur les periods standards ProTracker (PAL)
# Note: +24 semitones pour corriger l'octave (C-1 ProTracker = C-3 MIDI)
PERIOD_TABLE = {
    # Octave 1 (C-1 à B-1 ProTracker) → C-3 à B-3 MIDI
    856: 48, 808: 49, 762: 50, 720: 51, 678: 52, 640: 53,
    604: 54, 570: 55, 538: 56, 508: 57, 480: 58, 453: 59,
    # Octave 2 (C-2 à B-2 ProTracker) → C-4 à B-4 MIDI
    428: 60, 404: 61, 381: 62, 360: 63, 339: 64, 320: 65,
    302: 66, 285: 67, 269: 68, 254: 69, 240: 70, 226: 71,
    # Octave 3 (C-3 à B-3 ProTracker) → C-5 à B-5 MIDI
    214: 72, 202: 73, 190: 74, 180: 75, 170: 76, 160: 77,
    151: 78, 143: 79, 135: 80, 127: 81, 120: 82, 113: 83,
    # Octave 4 (extension) → C-6 à B-6 MIDI
    107: 84, 101: 85, 95: 86, 90: 87, 85: 88, 80: 89,
    76: 90, 71: 91, 67: 92, 64: 93, 60: 94, 57: 95,
}


def period_to_midi(period):
    """Convertit une valeur de period ProTracker en note MIDI

    La formule exacte est basée sur la fréquence:
    freq = 7093789.2 / (period * 2) (PAL)
    MIDI = 12 * log2(freq / 440) + 69

    Mais on utilise une table de lookup pour les valeurs standards
    """
    if period == 0:
        return None

    # Chercher la valeur la plus proche dans la table
    closest_period = min(PERIOD_TABLE.keys(), key=lambda x: abs(x - period))

    # Si la différence est trop grande, calculer via la formule
    if abs(closest_period - period) > 10:
        # Formule: freq = 7093789.2 / (period * 2)
        freq = 7093789.2 / (period * 2)
        # MIDI = 12 * log2(freq / 440) + 69 + 24 (correction octave)
        import math
        midi_note = 12 * math.log2(freq / 440.0) + 69 + 24
        return int(round(midi_note))

    return PERIOD_TABLE[closest_period]


def read_mod_header(filepath):
    """Lit le header d'un fichier MOD et détermine le format"""
    with open(filepath, 'rb') as f:
        # Lire le titre (20 bytes)
        title = f.read(20).decode('ascii', errors='ignore').strip('\x00')

        # Chercher la signature à l'offset 1080 pour déterminer le format
        f.seek(1080)
        signature = f.read(4).decode('ascii', errors='ignore')

        # Déterminer le nombre de samples
        if signature in ['M.K.', 'M!K!', 'FLT4', 'FLT8', '4CHN', '6CHN', '8CHN']:
            num_samples = 31
        else:
            num_samples = 15

        # Déterminer le nombre de canaux
        if signature == 'FLT8' or signature == '8CHN':
            num_channels = 8
        elif signature == '6CHN':
            num_channels = 6
        else:
            num_channels = 4  # Standard MOD

        return {
            'title': title,
            'num_samples': num_samples,
            'num_channels': num_channels,
            'signature': signature
        }


def read_mod_samples_info(f, num_samples):
    """Lit les informations des samples (headers seulement, pas les données)"""
    samples_info = []

    for i in range(num_samples):
        # Lire les 30 bytes de sample info
        name = f.read(22).decode('ascii', errors='ignore').strip('\x00')
        length = struct.unpack('>H', f.read(2))[0] * 2  # En words -> bytes
        finetune = struct.unpack('B', f.read(1))[0]
        # Finetune est signé sur 4 bits
        if finetune > 7:
            finetune = finetune - 16
        volume = struct.unpack('B', f.read(1))[0]
        repeat_start = struct.unpack('>H', f.read(2))[0] * 2  # En words -> bytes
        repeat_length = struct.unpack('>H', f.read(2))[0] * 2  # En words -> bytes

        samples_info.append({
            'name': name,
            'length': length,
            'finetune': finetune,
            'volume': volume,
            'repeat_start': repeat_start,
            'repeat_length': repeat_length,
        })

    return samples_info


def read_mod_patterns(f, num_patterns, num_channels):
    """Lit les patterns du MOD

    Chaque pattern = 64 rows
    Chaque row = num_channels notes
    Chaque note = 4 bytes
    """
    patterns = []

    for pattern_num in range(num_patterns):
        pattern_data = []

        for row in range(64):  # Toujours 64 rows dans MOD
            for channel in range(num_channels):
                # Lire 4 bytes pour cette note
                data = f.read(4)
                if len(data) < 4:
                    continue

                # Décoder les 4 bytes
                # Byte 0: sssspppp  s=sample high nibble, p=period high nibble
                # Byte 1: pppppppp  p=period low byte
                # Byte 2: ssssehhh  s=sample low nibble, e=effect, h=effect param high
                # Byte 3: hhhhhhhh  h=effect param low byte

                sample_high = (data[0] & 0xF0) >> 4
                period_high = (data[0] & 0x0F) << 8
                period_low = data[1]
                period = period_high | period_low

                sample_low = (data[2] & 0xF0) >> 4
                sample = (sample_high << 4) | sample_low

                effect = data[2] & 0x0F
                effect_param = data[3]

                # Extraire le sample offset si effet 9xx est présent
                sample_offset_xm = None
                if effect == 0x09:  # Effet 9xx = Sample Offset
                    sample_offset_xm = effect_param if effect_param is not None else 0

                # Ajouter les notes (si présentes)
                if period > 0 and sample > 0:
                    midi_note = period_to_midi(period)
                    if midi_note:
                        pattern_data.append({
                            'row': row,
                            'channel': channel,
                            'note': midi_note,
                            'period': period,
                            'sample': sample,
                            'effect': effect,
                            'effect_param': effect_param,
                            'volume': 64,  # Volume par défaut
                            'sample_offset_xm': sample_offset_xm
                        })
                # Aussi capturer les effets F (Set Speed/Tempo) même sans note
                elif effect == 0xF and effect_param > 0:
                    pattern_data.append({
                        'row': row,
                        'channel': channel,
                        'note': None,  # Pas de note
                        'period': 0,
                        'sample': 0,
                        'effect': effect,
                        'effect_param': effect_param,
                        'volume': 64,
                        'sample_offset_xm': sample_offset_xm
                    })
                # Capturer effet C00 (Set Volume 00 = stop note)
                elif effect == 0xC and effect_param == 0x00:
                    pattern_data.append({
                        'row': row,
                        'channel': channel,
                        'note': None,
                        'period': 0,
                        'sample': 0,
                        'effect': effect,
                        'effect_param': effect_param,
                        'volume': 0,
                        'sample_offset_xm': None,
                        'is_volume_stop': True
                    })

        patterns.append({
            'number': pattern_num,
            'rows': 64,
            'data': pattern_data
        })

    return patterns


def save_sample_as_wav(sample_data, output_path, sample_rate=8363):
    """Sauvegarde les données d'un sample en WAV (8-bit signé → 16-bit)"""
    if len(sample_data) == 0:
        return False

    # Convertir de 8-bit signé à 16-bit signé
    # Les samples MOD sont 8-bit signé (-128 à 127)
    # D'abord convertir bytes en uint8, puis réinterpréter comme int8
    sample_8bit = np.frombuffer(sample_data, dtype=np.uint8).astype(np.int8)
    sample_16bit = sample_8bit.astype(np.int16) * 256

    with wave.open(output_path, 'w') as wav:
        wav.setnchannels(1)  # Mono
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(sample_rate)
        wav.writeframes(sample_16bit.tobytes())

    return True


def extract_samples_and_patterns(mod_path, samples_dir):
    """Extrait les samples ET les patterns d'un fichier MOD

    Retourne: (samples, patterns, mod_info)
    Compatible avec la signature de xm_parser.py
    """
    samples = []
    patterns = []
    mod_info = None

    with open(mod_path, 'rb') as f:
        # Lire le titre (20 bytes)
        title = f.read(20).decode('ascii', errors='ignore').strip('\x00')

        # Lire les infos des samples (31 samples, même si format 15)
        samples_info = read_mod_samples_info(f, 31)

        # Lire song length et pattern order
        song_length = struct.unpack('B', f.read(1))[0]
        restart_pos = struct.unpack('B', f.read(1))[0]  # Ignoré généralement
        pattern_order = list(f.read(128))[:song_length]

        # Déterminer le nombre de patterns uniques
        num_patterns = max(pattern_order) + 1 if pattern_order else 0

        # Lire la signature pour déterminer le format
        signature = f.read(4).decode('ascii', errors='ignore')

        # Déterminer le nombre de samples et canaux
        if signature in ['M.K.', 'M!K!', 'FLT4', 'FLT8', '4CHN', '6CHN', '8CHN']:
            num_samples = 31
        else:
            num_samples = 15
            # Pas de signature, reculer de 4 bytes
            f.seek(-4, 1)

        # Déterminer le nombre de canaux
        if signature == 'FLT8' or signature == '8CHN':
            num_channels = 8
        elif signature == '6CHN':
            num_channels = 6
        else:
            num_channels = 4

        print(f"\nInfos: {num_samples} samples, {num_patterns} patterns, {num_channels} canaux")
        print(f"Signature: '{signature}' (format {'31-sample' if num_samples == 31 else '15-sample'})")

        # Lire les patterns
        print("\nLecture des patterns...")
        patterns = read_mod_patterns(f, num_patterns, num_channels)

        for i, pattern in enumerate(patterns):
            print(f"  Pattern {i+1}/{num_patterns}: 64 rows, {len(pattern['data'])} notes")

        # Lire les données des samples
        print("\nLecture des samples...")
        for i in range(num_samples):
            sample_info = samples_info[i]

            if sample_info['length'] == 0:
                continue

            print(f"\nSample {i+1}: '{sample_info['name']}', {sample_info['length']} bytes")

            # Lire les données du sample (8-bit signé)
            sample_data = f.read(sample_info['length'])

            if len(sample_data) > 0:
                # Sauvegarder en WAV
                sample_name = sample_info['name'] if sample_info['name'] else f'Sample_{i+1:02d}'
                # Nettoyer le nom pour le filesystem
                safe_name = "".join(c for c in sample_name if c.isalnum() or c in (' ', '-', '_')).strip()
                if not safe_name:
                    safe_name = f'Sample_{i+1:02d}'

                wav_path = os.path.join(samples_dir, f'{safe_name}.wav')

                # Éviter les collisions de noms
                counter = 1
                while os.path.exists(wav_path):
                    wav_path = os.path.join(samples_dir, f'{safe_name}_{counter}.wav')
                    counter += 1

                if save_sample_as_wav(sample_data, wav_path):
                    print(f"    ✓ {safe_name}.wav")

                    # Déterminer le type de loop
                    loop_type = 0  # Pas de loop par défaut
                    loop_start = 0
                    loop_length = 0

                    if sample_info['repeat_length'] > 2:  # Loop actif si > 1 word
                        loop_type = 1  # Forward loop (MOD n'a pas de ping-pong)
                        loop_start = sample_info['repeat_start']
                        loop_length = sample_info['repeat_length']

                    samples.append({
                        'instrument': i + 1,
                        'sample': 1,  # MOD n'a qu'un sample par instrument
                        'name': safe_name,
                        'path': wav_path,
                        'relative_note': 0,  # MOD utilise finetune, pas relative_note
                        'finetune': sample_info['finetune'],
                        'loop_start': loop_start,
                        'loop_length': loop_length,
                        'loop_type': loop_type,
                        'volume': sample_info['volume'],
                        'length': sample_info['length']  # Longueur en samples (pour effet 9xx)
                    })

        # Extraire Speed et BPM depuis les effets F dans les patterns
        # MOD utilise Speed=6, BPM=125 par défaut (modifiable via effet F)
        default_speed = 6
        default_bpm = 125

        # Chercher dans les patterns dans l'ordre de lecture (pattern_order)
        for pattern_idx in pattern_order[:10]:  # Chercher dans les 10 premiers patterns joués
            if pattern_idx < len(patterns):
                pattern = patterns[pattern_idx]
                for note_data in pattern['data']:
                    if note_data.get('effect') == 0xF:  # Effet F
                        param = note_data.get('effect_param', 0)
                        if param > 0:
                            if param <= 0x1F:  # 0x01-0x1F = Set Speed
                                default_speed = param
                                print(f"   → Speed found in pattern {pattern_idx}: {param}")
                            else:  # 0x20-0xFF = Set Tempo (BPM)
                                default_bpm = param
                                print(f"   → BPM found in pattern {pattern_idx}: {param}")
                            break  # Prendre le premier effet F trouvé
                if default_speed != 6 or default_bpm != 125:
                    break  # Arrêter si on a trouvé des valeurs

        mod_info = {
            'name': title,
            'channels': num_channels,
            'tempo': default_speed,
            'bpm': default_bpm,
            'pattern_order': pattern_order,
            'song_length': song_length
        }

    return samples, patterns, mod_info


def organize_tracks_by_channel(patterns, pattern_order, samples, num_channels):
    """Organise les notes par canal et par instrument

    Compatible avec la fonction de xm_parser.py
    Calcule les durées basées sur les intervalles entre notes
    """
    tracks = defaultdict(lambda: defaultdict(list))

    # D'abord collecter toutes les notes par canal
    notes_by_channel = defaultdict(list)
    current_time = 0.0

    for pattern_idx in pattern_order:
        if pattern_idx >= len(patterns):
            continue

        pattern = patterns[pattern_idx]

        for note_event in pattern['data']:
            row = note_event['row']
            channel = note_event['channel']

            # Calculer le temps absolu (en beats)
            absolute_time = current_time + (row / 4.0)

            # Si c'est un événement volume stop (effet C00)
            if note_event.get('is_volume_stop'):
                notes_by_channel[channel].append({
                    'time': absolute_time,
                    'is_volume_stop': True
                })
                continue  # Passer au suivant

            # Code pour les vraies notes
            sample_num = note_event['sample']
            midi_note = note_event['note']
            velocity = int((note_event['volume'] / 64.0) * 127)

            # Extraire le sample offset si effet 9xx est présent
            sample_offset_xm = note_event.get('sample_offset_xm')  # Déjà extrait par le parser

            notes_by_channel[channel].append({
                'time': absolute_time,
                'note': midi_note,
                'velocity': velocity,
                'instrument': sample_num,
                'duration': 0,  # Sera calculé
                'sample_offset_xm': sample_offset_xm  # Sample offset de l'effet 9xx (0-255) ou None
            })

        # Avancer le temps (64 rows = 16 beats à 4 rows/beat)
        current_time += pattern['rows'] / 4.0

    # Calculer les durées basées sur les intervalles
    for channel, channel_notes in notes_by_channel.items():
        # Trier par temps
        channel_notes.sort(key=lambda n: n['time'])

        for i, note in enumerate(channel_notes):
            # Ignorer les événements volume stop (utilisés juste pour calcul)
            if note.get('is_volume_stop'):
                continue

            # Chercher le prochain événement (note OU volume stop)
            next_event_time = None
            for j in range(i + 1, len(channel_notes)):
                next_event = channel_notes[j]

                # Volume stop coupe la note immédiatement
                if next_event.get('is_volume_stop'):
                    next_event_time = next_event['time']
                    break

                # Nouvelle note coupe aussi
                if not next_event.get('is_volume_stop'):
                    next_event_time = next_event['time']
                    break

            # Calculer la durée
            if next_event_time is not None:
                interval = next_event_time - note['time']
                # Limiter à 4 beats max pour la lisibilité
                note['duration'] = min(interval, 4.0)
            else:
                # Dernière note : durée par défaut de 4 beats
                note['duration'] = 4.0

            # Ajouter à la structure finale
            tracks[channel][note['instrument']].append({
                'time': note['time'],
                'note': note['note'],
                'velocity': note['velocity'],
                'duration': note['duration'],
                'sample_offset_xm': note.get('sample_offset_xm')  # Sample offset de l'effet 9xx (0-255) ou None
            })

    return tracks


# Fonction principale pour test
if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python3 mod_parser.py <fichier.mod>")
        sys.exit(1)

    mod_path = sys.argv[1]
    samples_dir = './mod_samples_test'
    os.makedirs(samples_dir, exist_ok=True)

    print(f"Parsing de {mod_path}...")
    samples, patterns, mod_info = extract_samples_and_patterns(mod_path, samples_dir)

    print(f"\n{'='*60}")
    print(f"MODULE: {mod_info['name']}")
    print(f"Samples extraits: {len(samples)}")
    print(f"Patterns: {len(patterns)}")
    print(f"Canaux: {mod_info['channels']}")
    print(f"{'='*60}")
