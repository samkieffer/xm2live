#!/usr/bin/env python3
"""
Module de parsing de fichiers FastTracker 2 Extended Module (.xm)
Extrait les samples, patterns et notes MIDI
"""

import sys
import os
import struct
import wave
import numpy as np
from pathlib import Path
from collections import defaultdict


def read_xm_header(filepath):
    """Lit les infos de base du fichier XM"""
    with open(filepath, 'rb') as f:
        id_text = f.read(17).decode('ascii', errors='ignore')
        if not id_text.startswith("Extended Module"):
            return None

        module_name = f.read(20).decode('ascii', errors='ignore').strip('\x00')
        f.read(1)  # 0x1a
        tracker_name = f.read(20).decode('ascii', errors='ignore').strip('\x00')

        f.read(2)  # version
        f.read(4)  # header size

        song_length = int.from_bytes(f.read(2), 'little')
        restart_pos = int.from_bytes(f.read(2), 'little')
        num_channels = int.from_bytes(f.read(2), 'little')
        num_patterns = int.from_bytes(f.read(2), 'little')
        num_instruments = int.from_bytes(f.read(2), 'little')
        flags = int.from_bytes(f.read(2), 'little')
        default_tempo = int.from_bytes(f.read(2), 'little')
        default_bpm = int.from_bytes(f.read(2), 'little')

        pattern_order = list(f.read(256))[:song_length]

        return {
            'name': module_name,
            'tracker': tracker_name,
            'song_length': song_length,
            'channels': num_channels,
            'patterns': num_patterns,
            'instruments': num_instruments,
            'tempo': default_tempo,
            'bpm': default_bpm,
            'pattern_order': pattern_order
        }


def parse_pattern_data(data, num_rows, num_channels):
    """Parse les données d'un pattern XM"""
    notes = []
    pos = 0

    for row in range(num_rows):
        for channel in range(num_channels):
            if pos >= len(data):
                break

            # Lire le premier byte
            note_byte = data[pos]
            pos += 1

            note = None
            instrument = None
            volume = None
            effect_type = None
            effect_param = None

            if note_byte & 0x80:  # Packed note
                if note_byte & 0x01:  # Note présente
                    if pos < len(data):
                        note = data[pos]
                        pos += 1

                if note_byte & 0x02:  # Instrument présent
                    if pos < len(data):
                        instrument = data[pos]
                        pos += 1

                if note_byte & 0x04:  # Volume présent
                    if pos < len(data):
                        volume = data[pos]
                        pos += 1

                if note_byte & 0x08:  # Effect type présent
                    if pos < len(data):
                        effect_type = data[pos]
                        pos += 1

                if note_byte & 0x10:  # Effect param présent
                    if pos < len(data):
                        effect_param = data[pos]
                        pos += 1
            else:  # Unpacked note (5 bytes)
                note = note_byte
                if pos + 3 < len(data):
                    instrument = data[pos]
                    volume = data[pos + 1]
                    effect_type = data[pos + 2]
                    effect_param = data[pos + 3]
                    pos += 4

            # Décoder la colonne volume XM (0x10-0x50 = volume 0-64)
            decoded_volume = None
            if volume is not None:
                if 0x10 <= volume <= 0x50:
                    decoded_volume = volume - 0x10  # 0x10 -> 0, 0x50 -> 64
                # Ignorer les autres valeurs (0x00-0x0F, 0x60+ sont des commandes spéciales)

            # Si on a une note valide et un instrument
            if note and note > 0 and note < 97 and instrument and instrument > 0:
                # Extraire le sample offset si effet 9xx est présent
                sample_offset_xm = None
                if effect_type == 0x09:  # Effet 9xx = Sample Offset
                    sample_offset_xm = effect_param if effect_param is not None else 0

                notes.append({
                    'row': row,
                    'channel': channel,
                    'note': note,
                    'instrument': instrument,
                    'volume': 64 if decoded_volume is None else decoded_volume,
                    'effect_type': effect_type,
                    'effect_param': effect_param,
                    'sample_offset_xm': sample_offset_xm
                })

            # Capturer volume 00 même sans note (événement "note stop")
            elif decoded_volume is not None and decoded_volume == 0:
                notes.append({
                    'row': row,
                    'channel': channel,
                    'note': None,
                    'instrument': None,
                    'volume': 0,
                    'effect_type': None,
                    'effect_param': None,
                    'sample_offset_xm': None,
                    'is_volume_stop': True
                })

    return notes


def read_patterns(f, num_patterns, num_channels):
    """Lit tous les patterns du XM"""
    patterns = []

    for i in range(num_patterns):
        try:
            pattern_header_len = struct.unpack('<I', f.read(4))[0]
            packing_type = struct.unpack('<B', f.read(1))[0]
            num_rows = struct.unpack('<H', f.read(2))[0]
            packed_size = struct.unpack('<H', f.read(2))[0]

            # Skip reste du header
            remaining_header = pattern_header_len - 9
            if remaining_header > 0:
                f.read(remaining_header)

            # Lire les données du pattern
            pattern_data = []
            if packed_size > 0:
                data = f.read(packed_size)
                pattern_data = parse_pattern_data(data, num_rows, num_channels)

            patterns.append({
                'number': i,
                'rows': num_rows,
                'data': pattern_data
            })

            print(f"  Pattern {i+1}/{num_patterns}: {num_rows} rows, {len(pattern_data)} notes")

        except Exception as e:
            print(f"  Erreur pattern {i+1}: {e}")
            patterns.append({'number': i, 'rows': 64, 'data': []})

    return patterns


def extract_samples_and_patterns(xm_path, samples_dir):
    """Extrait les samples ET les patterns du XM"""
    samples = []
    patterns = []
    xm_info = None

    with open(xm_path, 'rb') as f:
        # Lire le header principal
        id_text = f.read(17)
        module_name = f.read(20).decode('ascii', errors='ignore').strip('\x00')
        f.read(1)  # 0x1a
        tracker_name = f.read(20)
        version = struct.unpack('<H', f.read(2))[0]

        header_size = struct.unpack('<I', f.read(4))[0]
        song_length = struct.unpack('<H', f.read(2))[0]
        restart_pos = struct.unpack('<H', f.read(2))[0]
        num_channels = struct.unpack('<H', f.read(2))[0]
        num_patterns = struct.unpack('<H', f.read(2))[0]
        num_instruments = struct.unpack('<H', f.read(2))[0]
        flags = struct.unpack('<H', f.read(2))[0]
        default_tempo = struct.unpack('<H', f.read(2))[0]
        default_bpm = struct.unpack('<H', f.read(2))[0]
        pattern_order = list(f.read(256))[:song_length]

        xm_info = {
            'name': module_name,
            'channels': num_channels,
            'tempo': default_tempo,  # Speed (ticks par row)
            'bpm': default_bpm,
            'pattern_order': pattern_order,
            'song_length': song_length
        }

        print(f"\nInfos: {num_instruments} instruments, {num_patterns} patterns, {num_channels} canaux")

        # Lire les patterns
        print("\nLecture des patterns...")
        patterns = read_patterns(f, num_patterns, num_channels)

        # Lire les instruments et samples
        print("\nLecture des instruments...")
        for inst_num in range(num_instruments):
            try:
                inst_header_size = struct.unpack('<I', f.read(4))[0]

                if inst_header_size < 29:
                    continue

                inst_name = f.read(22).decode('ascii', errors='ignore').strip('\x00')
                inst_type = struct.unpack('<B', f.read(1))[0]
                num_samples = struct.unpack('<H', f.read(2))[0]

                print(f"\nInstrument {inst_num+1}: '{inst_name}', {num_samples} samples")

                if num_samples > 0 and num_samples < 256:
                    sample_header_size = struct.unpack('<I', f.read(4))[0]

                    remaining = inst_header_size - 33
                    if remaining > 0:
                        f.read(remaining)

                    # Lire les headers de samples
                    sample_headers = []
                    for j in range(num_samples):
                        sample_len = struct.unpack('<I', f.read(4))[0]
                        loop_start = struct.unpack('<I', f.read(4))[0]
                        loop_len = struct.unpack('<I', f.read(4))[0]
                        volume = struct.unpack('<B', f.read(1))[0]
                        finetune = struct.unpack('<b', f.read(1))[0]
                        sample_type = struct.unpack('<B', f.read(1))[0]
                        panning = struct.unpack('<B', f.read(1))[0]
                        relative_note = struct.unpack('<b', f.read(1))[0]
                        reserved = struct.unpack('<B', f.read(1))[0]
                        sample_name = f.read(22).decode('ascii', errors='ignore').strip('\x00')

                        sample_headers.append({
                            'length': sample_len,
                            'loop_start': loop_start,
                            'loop_length': loop_len,
                            'volume': volume,
                            'type': sample_type,
                            'name': sample_name or inst_name or f"Sample_{inst_num+1}_{j+1}",
                            'relative_note': relative_note,
                            'finetune': finetune,  # -128 à +127 (centièmes de demi-ton)
                            'panning': panning  # 0-255 (0=gauche, 128=centre, 255=droite)
                        })

                    # Lire les données des samples
                    for j, header in enumerate(sample_headers):
                        if header['length'] > 0 and header['length'] < 10000000:
                            sample_data = f.read(header['length'])

                            is_16bit = header['type'] & 0x10

                            if is_16bit:
                                data = np.frombuffer(sample_data, dtype=np.int16)
                            else:
                                data = np.frombuffer(sample_data, dtype=np.int8).astype(np.int16) * 256

                            # Delta decode (avec gestion naturelle du wrapping)
                            if len(data) > 0:
                                decoded = np.zeros(len(data), dtype=np.int16)
                                current = 0  # Utiliser int natif pour éviter overflow warning
                                for k in range(len(data)):
                                    current = (current + int(data[k])) & 0xFFFF  # Wrapping 16-bit
                                    # Convertir en int16 signé
                                    if current >= 32768:
                                        decoded[k] = current - 65536
                                    else:
                                        decoded[k] = current

                                # Save as WAV
                                safe_name = "".join(c for c in header['name'] if c.isalnum() or c in (' ', '_', '-', ',')).strip()
                                if not safe_name:
                                    # Utiliser la notation hexadécimale pour les instruments
                                    inst_hex = f"{inst_num+1:02X}"
                                    safe_name = f"Instrument_{inst_hex}_Sample_{j+1}"

                                wav_path = os.path.join(samples_dir, f"{safe_name}.wav")

                                counter = 1
                                while os.path.exists(wav_path):
                                    wav_path = os.path.join(samples_dir, f"{safe_name}_{counter}.wav")
                                    counter += 1

                                with wave.open(wav_path, 'w') as wav:
                                    wav.setnchannels(1)
                                    wav.setsampwidth(2)
                                    wav.setframerate(8363)
                                    wav.writeframes(decoded.tobytes())

                                samples.append({
                                    'instrument': inst_num + 1,
                                    'sample': j + 1,
                                    'name': header['name'],
                                    'path': wav_path,
                                    'relative_note': header['relative_note'],
                                    'finetune': header['finetune'],  # -128 à +127 (centièmes de demi-ton)
                                    'loop_start': header['loop_start'],
                                    'loop_length': header['loop_length'],
                                    'loop_type': header['type'] & 0x03,  # Bits 0-1: 0=off, 1=forward, 2=ping-pong
                                    'volume': header['volume'],
                                    'panning': header['panning'],  # 0-255 (0=gauche, 128=centre, 255=droite)
                                    'length': len(decoded)  # Longueur en samples (pour effet 9xx)
                                })

                                print(f"    ✓ {safe_name}")
                else:
                    remaining = inst_header_size - 29
                    if remaining > 0:
                        f.read(remaining)

            except Exception as e:
                print(f"  Erreur instrument {inst_num+1}: {e}")
                continue

    return samples, patterns, xm_info


def organize_tracks_by_channel(patterns, pattern_order, samples, num_channels):
    """Organise les notes par canal et par instrument"""
    # Structure: channel -> instrument -> notes
    tracks = defaultdict(lambda: defaultdict(list))

    # D'abord, collecter toutes les notes par canal (pour calculer les durées)
    notes_by_channel = defaultdict(list)

    # Parcourir les patterns dans l'ordre de la song
    current_time = 0.0

    for pattern_idx in pattern_order:
        if pattern_idx >= len(patterns):
            continue

        pattern = patterns[pattern_idx]

        for note_event in pattern['data']:
            channel = note_event['channel']
            row = note_event['row']

            # Calculer le temps en beats (4 rows = 1 beat en général)
            time_in_pattern = row / 4.0
            absolute_time = current_time + time_in_pattern

            # Si c'est un événement volume stop (volume 00)
            if note_event.get('is_volume_stop'):
                notes_by_channel[channel].append({
                    'time': absolute_time,
                    'is_volume_stop': True
                })
                continue  # Passer au suivant

            # Code pour les vraies notes
            instrument = note_event['instrument']

            # Convertir la note XM (1-96) en MIDI (0-127)
            # Note 1 = C-0, Note 49 = C-4 (middle C = MIDI 60)
            midi_note = note_event['note'] - 1 + 12  # Ajustement

            # Velocity depuis volume (0-64 -> 0-127)
            velocity = min(127, note_event['volume'] * 2)

            # Extraire le panning si effet 8xx est présent
            panning_xm = None
            if note_event.get('effect_type') == 0x08:  # Effet 8xx = Set Panning
                param = note_event.get('effect_param')
                panning_xm = param if param is not None else 128  # 0-255, 128=centre par défaut

            # Extraire le sample offset si effet 9xx est présent
            sample_offset_xm = note_event.get('sample_offset_xm')  # Déjà extrait par le parser

            # Stocker temporairement (durée sera calculée après)
            notes_by_channel[channel].append({
                'time': absolute_time,
                'note': midi_note,
                'velocity': velocity,
                'instrument': instrument,
                'duration': 0,  # Sera calculé
                'panning_xm': panning_xm,  # Panning de l'effet 8xx (0-255) ou None
                'sample_offset_xm': sample_offset_xm  # Sample offset de l'effet 9xx (0-255) ou None
            })

        # Avancer le temps (nombre de rows dans le pattern / 4)
        current_time += pattern['rows'] / 4.0

    # Calculer les durées intelligemment : intervalle jusqu'à la prochaine note
    # Dans FastTracker, une piste = une voix, donc une nouvelle note interrompt la précédente
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
                'panning_xm': channel_notes[i].get('panning_xm'),  # Panning de l'effet 8xx (0-255) ou None
                'sample_offset_xm': note.get('sample_offset_xm')  # Sample offset de l'effet 9xx (0-255) ou None
            })

    return tracks
