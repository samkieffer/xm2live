#!/usr/bin/env python3
"""
Convertisseur XM/MOD vers Ableton Live
Convertit des fichiers FastTracker 2 Extended Module (.xm) et Amiga ProTracker (.mod)
en projets Ableton Live (.als)
"""

import sys
import os
import struct
import math
try:
    from lxml import etree as ET
except ImportError:
    import xml.etree.ElementTree as ET
import copy
import wave
import numpy as np
from pathlib import Path
from collections import defaultdict
from buildable.live_set import LiveSet

# Importer les fonctions de lecture XM
from xm_parser import (
    read_xm_header,
    read_patterns,
    parse_pattern_data,
    extract_samples_and_patterns as extract_xm,
    organize_tracks_by_channel as organize_xm
)

# Importer les fonctions de lecture MOD
from mod_parser import (
    extract_samples_and_patterns as extract_mod,
    organize_tracks_by_channel as organize_mod
)

# Importer le template minimal embarqué
from minimal_template import get_minimal_template_root

def update_all_ids(element, next_id):
    """
    Met à jour tous les attributs Id dans l'élément et ses enfants.
    Retourne le prochain ID disponible.
    """
    # Mettre à jour l'Id de cet élément s'il en a un
    if 'Id' in element.attrib:
        try:
            int(element.get('Id'))  # Vérifier que c'est un nombre
            element.set('Id', str(next_id))
            next_id += 1
        except (ValueError, TypeError):
            # Si l'Id n'est pas un nombre, on le laisse tel quel
            pass

    # Récursivement mettre à jour tous les enfants
    for child in element:
        next_id = update_all_ids(child, next_id)

    return next_id

def update_track_name(track, new_name):
    """Change le nom d'une piste"""
    name_elem = track.element.find('.//Name/EffectiveName')
    if name_elem is not None:
        name_elem.set('Value', new_name)

def update_track_color(track, color_index):
    """Change la couleur d'une piste et de son clip MIDI"""
    # Couleur de la piste
    track_color = track.element.find('.//Color')
    if track_color is not None:
        track_color.set('Value', str(color_index))

    # Couleur du clip MIDI
    midi_clip = track.element.find('.//MidiClip')
    if midi_clip is not None:
        clip_color = midi_clip.find('.//Color')
        if clip_color is not None:
            clip_color.set('Value', str(color_index))

def update_sampler_sample(track, sample_info, project_dir, template_sample_part, bpm=125, speed=6, enable_envelope=False):
    """Change le sample chargé dans le Sampler/MultiSampler en copiant la structure du template

    Args:
        sample_info: Dict contenant path, relative_note, loop_start, loop_length, loop_type, volume, envelope
        project_dir: Répertoire du projet
        template_sample_part: Template du sample part
        bpm: BPM du module (pour conversion temps enveloppe)
        speed: Speed du module (ticks par row)
        enable_envelope: Activer la conversion enveloppe FT2 → ADSR
    """

    sample_path = sample_info['path']
    relative_note = sample_info.get('relative_note', 0)

    # Trouver le device Sampler/MultiSampler
    devices = track.element.find('.//DeviceChain/Devices')
    if devices is None:
        print(f"    ⚠️  No DeviceChain/Devices found")
        return False

    # Chercher MultiSampler comme enfant direct de Devices (pas descendant)
    sampler = devices.find('./MultiSampler')
    if sampler is None:
        sampler = devices.find('./OriginalSimpler')

    if sampler is None:
        print(f"    ⚠️  No Sampler found")
        return False

    # Trouver ou créer la structure Player/MultiSampleMap/SampleParts
    player = sampler.find('.//Player')
    if player is None:
        player = ET.SubElement(sampler, 'Player')

    multi_sample_map = player.find('.//MultiSampleMap')
    if multi_sample_map is None:
        multi_sample_map = ET.SubElement(player, 'MultiSampleMap')

    sample_parts = multi_sample_map.find('.//SampleParts')
    if sample_parts is None:
        sample_parts = ET.SubElement(multi_sample_map, 'SampleParts')

    # Supprimer tous les SampleParts existants
    for part in list(sample_parts):
        sample_parts.remove(part)

    # Cloner le MultiSamplePart du template
    new_sample_part = ET.fromstring(ET.tostring(template_sample_part))
    sample_parts.append(new_sample_part)

    # Mettre à jour les chemins du sample
    file_ref = new_sample_part.find('.//SampleRef/FileRef')
    if file_ref is not None:
        # Calculer le chemin relatif depuis le fichier .als
        # Le fichier .als est dans output_dir, les samples dans output_dir/Samples/
        sample_filename = os.path.basename(sample_path)
        rel_path_str = f"Samples/{sample_filename}"

        # Mettre à jour RelativePathType (1 = relative to .als file)
        rel_path_type_elem = file_ref.find('.//RelativePathType')
        if rel_path_type_elem is not None:
            rel_path_type_elem.set('Value', '1')

        # Mettre à jour RelativePath
        rel_path_elem = file_ref.find('.//RelativePath')
        if rel_path_elem is not None:
            rel_path_elem.set('Value', rel_path_str)

        # Mettre à jour Path
        path_elem = file_ref.find('.//Path')
        if path_elem is not None:
            path_elem.set('Value', os.path.abspath(sample_path))

        # Mettre à jour OriginalFileSize et OriginalCrc
        if os.path.exists(sample_path):
            file_size = os.path.getsize(sample_path)
            size_elem = file_ref.find('.//OriginalFileSize')
            if size_elem is not None:
                size_elem.set('Value', str(file_size))

            with open(sample_path, 'rb') as f:
                data = f.read()
                crc = sum(data) % 65536
            crc_elem = file_ref.find('.//OriginalCrc')
            if crc_elem is not None:
                crc_elem.set('Value', str(crc))

    # Mettre à jour le nom du sample
    name_elem = new_sample_part.find('.//Name')
    if name_elem is not None:
        sample_name = os.path.splitext(os.path.basename(sample_path))[0]
        name_elem.set('Value', sample_name)

    # Mettre à jour RootKey en fonction du relative_note du XM
    # Dans XM: relative_note = -1 signifie 1 demi-ton plus bas
    # Dans Ableton: RootKey = 60 (C3) par défaut
    # Donc: RootKey = 60 - relative_note
    root_key_elem = new_sample_part.find('.//RootKey')
    if root_key_elem is not None:
        root_key = 60 - relative_note
        root_key_elem.set('Value', str(root_key))
        if relative_note != 0:
            print(f"    → RootKey adjusted: {root_key} (relative_note={relative_note:+d})")

    # Mettre à jour Detune (finetune) juste après RootKey
    # XM finetune: -128 à +127 (centièmes de demi-ton)
    # Ableton Detune: -50 à +50 cents
    finetune = sample_info.get('finetune', 0)
    if finetune != 0:
        detune_elem = new_sample_part.find('.//Detune')
        if detune_elem is not None:
            # Conversion: XM utilise la plage complète -128/+127, Ableton -50/+50
            # Donc diviser par ~2.56 (128/50)
            detune_value = finetune / 2.56
            # Limiter à la plage Ableton
            detune_value = max(-50, min(50, detune_value))
            detune_elem.set('Value', str(detune_value))
            print(f"    → Detune: {detune_value:.1f} cents (finetune={finetune:+d})")

    # Mettre à jour Volume (volume par défaut du sample)
    # XM volume: 0-64 (64 = 100%)
    # Ableton Volume: 0.0-1.0 (1.0 = 0 dB)
    # Réduction à 0.25 (-12 dB) pour éviter la saturation avec velocity + Vol-Vel 35%
    xm_volume = sample_info.get('volume', 64)
    volume_elem = new_sample_part.find('.//Volume')
    if volume_elem is not None:
        # Conversion linéaire avec réduction de -12dB
        ableton_volume = (xm_volume / 64.0) * 0.25
        volume_elem.set('Value', str(ableton_volume))

        # Afficher seulement si différent du maximum
        if xm_volume != 64:
            # Calculer les dB pour l'affichage
            if ableton_volume > 0:
                db_value = 20 * math.log10(ableton_volume)
                print(f"    → Volume: {ableton_volume:.3f} ({db_value:.1f} dB, XM={xm_volume}/64)")
            else:
                print(f"    → Volume: 0.000 (-∞ dB, XM={xm_volume}/64)")

    # Configurer VolumeVelScale (Vol<Vel) à 35% pour que la velocity affecte le volume
    # Cela permet aux variations de volume des notes (velocity) d'influencer le volume final
    # 35% est la valeur par défaut d'Ableton Simpler/Sampler
    vel_scale = sampler.find('.//VolumeAndPan/VolumeVelScale/Manual')
    if vel_scale is not None:
        vel_scale.set('Value', '0.35')

    # Mettre à jour Panorama (panning par défaut du sample)
    # XM panning: 0-255 (0=gauche, 128=centre, 255=droite)
    # Ableton Panorama: -1.0 à +1.0 (0=centre)
    xm_panning = sample_info.get('panning', 128)
    panorama_elem = new_sample_part.find('.//Panorama')
    if panorama_elem is not None:
        # Conversion: (xm_pan - 128) / 128.0
        ableton_pan = (xm_panning - 128) / 128.0
        # Limiter à la plage Ableton
        ableton_pan = max(-1.0, min(1.0, ableton_pan))
        panorama_elem.set('Value', str(ableton_pan))

        # Afficher seulement si différent du centre
        if xm_panning != 128:
            if ableton_pan < 0:
                print(f"    → Panorama: {ableton_pan:.2f} (left, XM={xm_panning}/255)")
            else:
                print(f"    → Panorama: {ableton_pan:.2f} (right, XM={xm_panning}/255)")

    # Lire la longueur du sample WAV pour mettre à jour SampleEnd
    if os.path.exists(sample_path):
        try:
            import wave
            with wave.open(sample_path, 'rb') as wav:
                num_frames = wav.getnframes()

                # Mettre à jour SampleEnd
                sample_end_elem = new_sample_part.find('.//SampleEnd')
                if sample_end_elem is not None:
                    sample_end_elem.set('Value', str(num_frames - 1))

                # Mettre à jour SustainLoop End
                sustain_loop_end = new_sample_part.find('.//SustainLoop/End')
                if sustain_loop_end is not None:
                    sustain_loop_end.set('Value', str(num_frames - 1))

                # Mettre à jour ReleaseLoop End
                release_loop_end = new_sample_part.find('.//ReleaseLoop/End')
                if release_loop_end is not None:
                    release_loop_end.set('Value', str(num_frames - 1))

                # Appliquer les paramètres de boucle du XM
                loop_type = sample_info.get('loop_type', 0)
                if loop_type > 0:  # 1=forward, 2=ping-pong
                    loop_start = sample_info.get('loop_start', 0)
                    loop_length = sample_info.get('loop_length', 0)
                    loop_end = loop_start + loop_length

                    # Activer la boucle dans SustainLoop
                    sustain_loop_on = new_sample_part.find('.//SustainLoop/LoopOn')
                    if sustain_loop_on is not None:
                        sustain_loop_on.set('Value', 'true')

                    # Loop start
                    sustain_loop_start = new_sample_part.find('.//SustainLoop/Start')
                    if sustain_loop_start is not None:
                        sustain_loop_start.set('Value', str(loop_start))

                    # Loop end
                    sustain_loop_end = new_sample_part.find('.//SustainLoop/End')
                    if sustain_loop_end is not None:
                        sustain_loop_end.set('Value', str(loop_end))

                    # Loop mode: 0=Off, 1=Forward, 2=Ping-Pong, 3=Backward
                    # XM: 1=Forward, 2=Ping-Pong → correspond à Ableton
                    sustain_loop_mode = new_sample_part.find('.//SustainLoop/Mode')
                    if sustain_loop_mode is not None:
                        sustain_loop_mode.set('Value', str(loop_type))

                    loop_mode_name = {1: "Forward", 2: "Ping-Pong"}.get(loop_type, "Unknown")
                    print(f"    → Boucle: {loop_start} - {loop_end} ({loop_mode_name})")

        except Exception as e:
            print(f"    ⚠️  Erreur lors de la lecture du WAV: {e}")

    # Paramétrer le nombre de voix à 1 (au lieu de 6 par défaut)
    # NumVoices est numéroté à partir de 0 : 0=1 voix, 5=6 voix
    num_voices = sampler.find('.//NumVoices')
    if num_voices is not None:
        num_voices.set('Value', '0')  # 0 = 1 voix
        print(f"    → Voice: 1 (monophonic)")

    # Configurer l'enveloppe ADSR si activé
    if enable_envelope:
        envelope_info = sample_info.get('envelope')
        if envelope_info and envelope_info.get('enabled'):
            configure_envelope_adsr(sampler, envelope_info, bpm, speed)

    return True


def detect_effect_9xx_per_instrument(patterns, pattern_order, file_format='xm'):
    """Détecte quels instruments utilisent l'effet 9xx (Sample Offset)

    Args:
        patterns: Liste des patterns du module
        pattern_order: Ordre de lecture des patterns
        file_format: 'xm' ou 'mod' (pour savoir quel champ utiliser)

    Returns:
        set: Ensemble des numéros d'instruments qui utilisent l'effet 9xx
    """
    instruments_with_9xx = set()

    # Parcourir les patterns dans l'ordre de lecture
    for pattern_idx in pattern_order:
        if pattern_idx >= len(patterns):
            continue

        pattern = patterns[pattern_idx]

        # Parcourir les events du pattern
        for note_event in pattern.get('data', []):
            instrument = note_event.get('instrument')

            if instrument is None:
                continue

            # Vérifier selon le format
            if file_format == 'xm':
                # XM : effet_type = 0x09
                if note_event.get('effect_type') == 0x09:
                    instruments_with_9xx.add(instrument)
            else:  # MOD
                # MOD : effect = 0x09
                if note_event.get('effect') == 0x09:
                    instruments_with_9xx.add(instrument)

    return instruments_with_9xx


def regenerate_ids(element, id_mapping, next_id):
    """Régénère récursivement tous les IDs d'un élément XML pour éviter les doublons

    Args:
        element: Element lxml à traiter
        id_mapping: Dict pour mapper anciens → nouveaux IDs
        next_id: Prochain ID disponible

    Returns:
        next_id mis à jour
    """
    # Traiter l'attribut Id de l'élément actuel
    if 'Id' in element.attrib:
        old_id = element.attrib['Id']
        if old_id not in id_mapping:
            id_mapping[old_id] = str(next_id)
            next_id += 1
        element.attrib['Id'] = id_mapping[old_id]

    # Traiter les références PointeeId et autres
    if element.tag == 'PointeeId' and 'Value' in element.attrib:
        old_value = element.attrib['Value']
        if old_value in id_mapping:
            element.attrib['Value'] = id_mapping[old_value]

    # Traiter AutomationTarget/Id
    if element.tag == 'AutomationTarget' and 'Id' in element.attrib:
        old_id = element.attrib['Id']
        if old_id not in id_mapping:
            id_mapping[old_id] = str(next_id)
            next_id += 1
        element.attrib['Id'] = id_mapping[old_id]

    # Traiter ModulationTarget/Id
    if element.tag == 'ModulationTarget' and 'Id' in element.attrib:
        old_id = element.attrib['Id']
        if old_id not in id_mapping:
            id_mapping[old_id] = str(next_id)
            next_id += 1
        element.attrib['Id'] = id_mapping[old_id]

    # Traiter récursivement tous les enfants
    for child in element:
        next_id = regenerate_ids(child, id_mapping, next_id)

    return next_id


def get_simpler_template(next_id):
    """Charge le template Simpler depuis simpler_template.xml

    Args:
        next_id: Prochain ID disponible pour la régénération des IDs

    Returns:
        (Element, next_id): Tuple (copie de l'élément OriginalSimpler, next_id mis à jour)
        (None, next_id) si le fichier n'existe pas
    """
    from lxml import etree
    from copy import deepcopy
    import os

    # Chemins possibles pour le template
    script_dir = os.path.dirname(os.path.abspath(__file__))
    template_paths = [
        os.path.join(script_dir, "simpler_template.xml"),
        "./simpler_template.xml"
    ]

    template_path = None
    for path in template_paths:
        if os.path.exists(path):
            template_path = path
            break

    if template_path is None:
        print("❌ Simpler template not found (simpler_template.xml)")
        return None, next_id

    try:
        # Utiliser lxml.etree pour compatibilité avec buildable
        tree = etree.parse(template_path)
        root = tree.getroot()

        # Faire une copie profonde
        simpler_copy = deepcopy(root)

        # IMPORTANT: Régénérer tous les IDs pour éviter les doublons (Pointee ID non uniques)
        id_mapping = {}
        next_id = regenerate_ids(simpler_copy, id_mapping, next_id)

        return simpler_copy, next_id

    except Exception as e:
        print(f"❌ Erreur lors du chargement du template Simpler : {e}")
        return None, next_id


def populate_track_with_simpler(track, sample_info, project_dir, next_id, bpm=125, speed=6, enable_envelope=False):
    """Configure une piste avec OriginalSimpler au lieu de MultiSampler

    Utilisé pour les instruments qui ont l'effet 9xx (Sample Offset)
    car Sampler ne permet pas d'automatiser Sample Start.

    Args:
        track: Objet buildable.live.Track
        sample_info: Dict contenant path, relative_note, loop_start, loop_length, loop_type, volume, panning, envelope
        project_dir: Répertoire du projet
        next_id: Prochain ID disponible (pour régénération IDs Simpler)
        bpm: BPM du module (pour conversion temps enveloppe)
        speed: Speed du module (ticks par row)
        enable_envelope: Activer la conversion enveloppe FT2 → ADSR

    Returns:
        next_id: next_id mis à jour après régénération des IDs

    Note:
        - Simpler ne supporte PAS les loops ping-pong (converti en forward)
        - Multi-sample non supporté (charge seulement le premier sample)
        - SNAP est toujours activé (évite les clics lors des automations Sample Start)
    """
    sample_path = sample_info['path']
    relative_note = sample_info.get('relative_note', 0)

    # Trouver le DeviceChain/Devices
    devices = track.element.find('.//DeviceChain/Devices')
    if devices is None:
        print(f"    ⚠️  No DeviceChain/Devices found")
        return (next_id, None)

    # Supprimer le MultiSampler existant s'il y en a un
    sampler = devices.find('./MultiSampler')
    if sampler is not None:
        devices.remove(sampler)

    # Charger le template Simpler (avec régénération des IDs)
    simpler_template, next_id = get_simpler_template(next_id)
    if simpler_template is None:
        return (next_id, None)

    # Ajouter le Simpler à la piste
    devices.append(simpler_template)
    simpler = simpler_template  # Référence pour la suite

    # Trouver la structure Player/MultiSampleMap/SampleParts/MultiSamplePart
    # (oui, Simpler a la même structure que Sampler pour les samples)
    sample_part = simpler.find('.//Player/MultiSampleMap/SampleParts/MultiSamplePart')
    if sample_part is None:
        print(f"    ⚠️  No MultiSamplePart found in Simpler template")
        return (next_id, None)

    # Mettre à jour les chemins du sample
    file_ref = sample_part.find('.//SampleRef/FileRef')
    if file_ref is not None:
        # Calculer le chemin relatif depuis le fichier .als
        sample_filename = os.path.basename(sample_path)
        rel_path_str = f"Samples/{sample_filename}"

        # Mettre à jour RelativePathType (1 = relative to .als file)
        rel_path_type_elem = file_ref.find('.//RelativePathType')
        if rel_path_type_elem is not None:
            rel_path_type_elem.set('Value', '1')

        # Mettre à jour RelativePath
        rel_path_elem = file_ref.find('.//RelativePath')
        if rel_path_elem is not None:
            rel_path_elem.set('Value', rel_path_str)

        # Mettre à jour Path
        path_elem = file_ref.find('.//Path')
        if path_elem is not None:
            path_elem.set('Value', os.path.abspath(sample_path))

        # Mettre à jour OriginalFileSize et OriginalCrc
        if os.path.exists(sample_path):
            file_size = os.path.getsize(sample_path)
            size_elem = file_ref.find('.//OriginalFileSize')
            if size_elem is not None:
                size_elem.set('Value', str(file_size))

            with open(sample_path, 'rb') as f:
                data = f.read()
                crc = sum(data) % 65536
            crc_elem = file_ref.find('.//OriginalCrc')
            if crc_elem is not None:
                crc_elem.set('Value', str(crc))

    # Mettre à jour le nom du sample
    name_elem = sample_part.find('.//Name')
    if name_elem is not None:
        sample_name = os.path.splitext(os.path.basename(sample_path))[0]
        name_elem.set('Value', sample_name)

    # Mettre à jour RootKey
    root_key_elem = sample_part.find('.//RootKey')
    if root_key_elem is not None:
        root_key = 60 - relative_note
        root_key_elem.set('Value', str(root_key))
        if relative_note != 0:
            print(f"    → RootKey adjusted: {root_key} (relative_note={relative_note:+d})")

    # Mettre à jour Detune (finetune)
    finetune = sample_info.get('finetune', 0)
    if finetune != 0:
        detune_elem = sample_part.find('.//Detune')
        if detune_elem is not None:
            detune_value = finetune / 2.56
            detune_value = max(-50, min(50, detune_value))
            detune_elem.set('Value', str(detune_value))
            print(f"    → Detune: {detune_value:.1f} cents (finetune={finetune:+d})")

    # Mettre à jour Panorama (panning par défaut du sample)
    xm_panning = sample_info.get('panning', 128)
    panorama_elem = sample_part.find('.//Panorama')
    if panorama_elem is not None:
        ableton_pan = (xm_panning - 128) / 128.0
        ableton_pan = max(-1.0, min(1.0, ableton_pan))
        panorama_elem.set('Value', str(ableton_pan))

        if xm_panning != 128:
            if ableton_pan < 0:
                print(f"    → Panorama: {ableton_pan:.2f} (left, XM={xm_panning}/255)")
            else:
                print(f"    → Panorama: {ableton_pan:.2f} (right, XM={xm_panning}/255)")

    # Mettre à jour Volume du sample dans MultiSamplePart
    # (pas le Volume global du Simpler, qui est à -12dB dans le template)
    xm_volume = sample_info.get('volume', 64)
    volume_elem = sample_part.find('.//Volume')
    if volume_elem is not None:
        # Conversion linéaire avec réduction de -12dB
        ableton_volume = (xm_volume / 64.0) * 0.25
        volume_elem.set('Value', str(ableton_volume))

        if xm_volume != 64:
            if ableton_volume > 0:
                db_value = 20 * math.log10(ableton_volume)
                print(f"    → Volume: {ableton_volume:.3f} ({db_value:.1f} dB, XM={xm_volume}/64)")
            else:
                print(f"    → Volume: 0.000 (-∞ dB, XM={xm_volume}/64)")

    # VolumeVelScale est déjà à 35% dans le template (0.349999994)
    # Pas besoin de le modifier

    # Lire la longueur du sample WAV pour configurer les loops
    if os.path.exists(sample_path):
        try:
            import wave
            with wave.open(sample_path, 'rb') as wav:
                num_frames = wav.getnframes()

                # Mettre à jour SampleEnd
                sample_end_elem = sample_part.find('.//SampleEnd')
                if sample_end_elem is not None:
                    sample_end_elem.set('Value', str(num_frames - 1))

                # Mettre à jour SustainLoop End
                sustain_loop_end = sample_part.find('.//SustainLoop/End')
                if sustain_loop_end is not None:
                    sustain_loop_end.set('Value', str(num_frames - 1))

                # Appliquer les paramètres de boucle
                loop_type = sample_info.get('loop_type', 0)

                # Trouver LoopOn dans LoopModulators
                loop_on_elem = simpler.find('.//LoopModulators/LoopOn/Manual')
                snap_elem = simpler.find('.//Snap/Manual')

                # IMPORTANT: SNAP toujours activé (évite les clics lors des automations Sample Start)
                if snap_elem is not None:
                    snap_elem.set('Value', 'true')

                if loop_type > 0:  # 1=forward, 2=ping-pong
                    # LIMITATION : Simpler ne supporte PAS ping-pong, conversion en forward
                    loop_start = sample_info.get('loop_start', 0)
                    loop_length = sample_info.get('loop_length', 0)
                    loop_end = loop_start + loop_length

                    # Activer la boucle dans SustainLoop
                    sustain_loop_on = sample_part.find('.//SustainLoop/LoopOn')
                    if sustain_loop_on is not None:
                        sustain_loop_on.set('Value', 'true')

                    # Loop start
                    sustain_loop_start = sample_part.find('.//SustainLoop/Start')
                    if sustain_loop_start is not None:
                        sustain_loop_start.set('Value', str(loop_start))

                    # Loop end
                    sustain_loop_end = sample_part.find('.//SustainLoop/End')
                    if sustain_loop_end is not None:
                        sustain_loop_end.set('Value', str(loop_end))

                    # Mode toujours Forward (1) pour Simpler
                    sustain_loop_mode = sample_part.find('.//SustainLoop/Mode')
                    if sustain_loop_mode is not None:
                        sustain_loop_mode.set('Value', '1')  # 1 = Forward

                    # Activer LoopOn dans LoopModulators
                    if loop_on_elem is not None:
                        loop_on_elem.set('Value', 'true')

                    if loop_type == 2:
                        print(f"    → Boucle: {loop_start} - {loop_end} (Ping-Pong → Forward, limitation Simpler)")
                    else:
                        print(f"    → Boucle: {loop_start} - {loop_end} (Forward)")

                else:
                    # Pas de loop : désactiver LoopOn
                    if loop_on_elem is not None:
                        loop_on_elem.set('Value', 'false')

        except Exception as e:
            print(f"    ⚠️  Erreur lors de la lecture du WAV: {e}")

    # Paramétrer le nombre de voix à 1 (au lieu de 6 par défaut)
    num_voices = simpler.find('.//NumVoices')
    if num_voices is not None:
        num_voices.set('Value', '0')  # 0 = 1 voix
        print(f"    → Voice: 1 (monophonic)")

    # Configurer l'enveloppe ADSR si activé
    if enable_envelope:
        envelope_info = sample_info.get('envelope')
        if envelope_info and envelope_info.get('enabled'):
            # Note: configure_envelope_adsr() supporte déjà Simpler
            # Car il cherche VolumeAndPan/Envelope qui existe dans les deux devices
            configure_envelope_adsr(simpler, envelope_info, bpm, speed)

    print(f"    ✅ Simpler configured (effect 9xx supported)")

    # Récupérer l'ID de l'AutomationTarget SampleStart pour les automations 9xx
    # On le fait ICI pendant qu'on manipule le Simpler, car buildable ne persiste pas les recherches ultérieures
    automation_target_id = None
    sample_start_target = simpler.find('.//LoopModulators/SampleStart/AutomationTarget')
    if sample_start_target is not None:
        automation_target_id = sample_start_target.get('Id')
        if not automation_target_id:
            # Créer un ID si absent
            automation_target_id = str(next_id)
            sample_start_target.set('Id', automation_target_id)
            next_id += 1

        # Activer IsModulated dans LoopModulators (REQUIS pour automation visible)
        from lxml import etree
        loop_modulators = simpler.find('.//LoopModulators')
        if loop_modulators is not None:
            is_modulated = loop_modulators.find('./IsModulated')
            if is_modulated is None:
                is_modulated = etree.Element('IsModulated')
                is_modulated.set('Value', 'true')
                loop_modulators.insert(0, is_modulated)
            else:
                is_modulated.set('Value', 'true')

    return (next_id, automation_target_id)


def update_midi_clip_notes(track, notes):
    """Remplace les notes dans le premier clip MIDI de la piste"""
    # Trouver le MainSequencer
    main_seq = track.element.find('.//DeviceChain/MainSequencer')
    if main_seq is None:
        print(f"    ⚠️  No MainSequencer found")
        return False

    # Trouver le premier clip MIDI
    midi_clip = main_seq.find('.//MidiClip')
    if midi_clip is None:
        print(f"    ⚠️  No MidiClip found")
        return False

    # Calculer la durée totale
    if not notes:
        return True

    max_time = max(note['time'] for note in notes) + 4.0

    # Mettre à jour CurrentEnd et LoopEnd
    current_end = midi_clip.find('.//CurrentEnd')
    if current_end is not None:
        current_end.set('Value', str(max_time))

    loop_end = midi_clip.find('.//Loop/LoopEnd')
    if loop_end is not None:
        loop_end.set('Value', str(max_time))

    out_marker = midi_clip.find('.//Loop/OutMarker')
    if out_marker is not None:
        out_marker.set('Value', str(max_time))

    # Trouver l'élément Notes/KeyTracks
    key_tracks = midi_clip.find('.//Notes/KeyTracks')
    if key_tracks is None:
        print(f"    ⚠️  No KeyTracks found")
        return False

    # Supprimer tous les KeyTracks existants
    for key_track in list(key_tracks):
        key_tracks.remove(key_track)

    # Grouper les notes par hauteur MIDI
    notes_by_key = defaultdict(list)
    for note in notes:
        midi_key = int(note['note'])
        if 0 <= midi_key <= 127:
            notes_by_key[midi_key].append(note)

    # Créer les nouveaux KeyTracks
    for midi_key, key_notes in sorted(notes_by_key.items()):
        key_track = ET.SubElement(key_tracks, 'KeyTrack')
        key_track.set('Id', str(midi_key))

        notes_elem = ET.SubElement(key_track, 'Notes')

        for note in key_notes:
            note_event = ET.SubElement(notes_elem, 'MidiNoteEvent')
            note_event.set('Time', str(note['time']))
            note_event.set('Duration', str(note['duration']))
            note_event.set('Velocity', str(int(note['velocity'])))
            note_event.set('OffVelocity', '64')
            note_event.set('NoteId', '0')

        midi_key_elem = ET.SubElement(key_track, 'MidiKey')
        midi_key_elem.set('Value', str(midi_key))

    return True

def add_sample_offset_automations_to_file(als_path, automation_data, next_id):
    """Ajoute les automations de Sample Start directement dans le fichier .als après sauvegarde

    Cette fonction est nécessaire car buildable ne persiste pas les modifications XML directes.
    Elle décompresse le fichier .als, ajoute les AutomationEnvelope, puis recompresse.

    Args:
        als_path: Chemin du fichier .als sauvegardé
        automation_data: Liste de dicts avec:
            - track_name: Nom de la piste (pour affichage uniquement)
            - track_index: Index de la piste (0-based, pour identification unique)
            - target_id: ID de l'AutomationTarget SampleStart
            - notes: Liste des notes avec time, duration, sample_offset_xm
            - sample_length: Longueur du sample en samples (pour conversion 9xx correcte)
        next_id: Prochain ID disponible (pour AutomationEnvelope et FloatEvent)

    Returns:
        True si succès, False sinon
    """
    import gzip
    from lxml import etree

    if not automation_data:
        return True  # Rien à faire

    print(f"\n✍️  Post-processing: Adding Sample Start automations...")

    try:
        # 1. Décompresser le fichier .als
        with gzip.open(als_path, 'rb') as f:
            xml_data = f.read()

        # 2. Parser le XML
        root = etree.fromstring(xml_data)

        # 3. Récupérer toutes les MidiTrack dans l'ordre
        all_midi_tracks = root.findall('.//LiveSet/Tracks/MidiTrack')

        # 4. Pour chaque piste avec automation
        for data in automation_data:
            track_name = data['track_name']
            track_index = data['track_index']
            target_id = data['target_id']
            notes = data['notes']
            sample_length = data['sample_length']  # Longueur en samples

            # Utiliser l'index pour accéder directement à la bonne piste
            if track_index >= len(all_midi_tracks):
                print(f"   ⚠️  Index {track_index} out of bounds (only {len(all_midi_tracks)} tracks)")
                continue

            midi_track = all_midi_tracks[track_index]

            # Vérifier que la piste contient bien un OriginalSimpler
            simpler = midi_track.find('.//OriginalSimpler')
            if simpler is None:
                print(f"   ⚠️  Piste #{track_index} '{track_name}' does not contain OriginalSimpler")
                continue

            # Trouver AutomationEnvelopes
            automation_envelopes = midi_track.find('.//AutomationEnvelopes')
            if automation_envelopes is None:
                print(f"   ⚠️  AutomationEnvelopes not found for '{track_name}'")
                continue

            # Supprimer l'ancien Envelopes et en créer un nouveau
            envelopes_container = automation_envelopes.find('.//Envelopes')
            if envelopes_container is not None:
                automation_envelopes.remove(envelopes_container)

            envelopes_container = etree.SubElement(automation_envelopes, 'Envelopes')

            # Créer l'AutomationEnvelope
            envelope = etree.SubElement(envelopes_container, 'AutomationEnvelope')
            envelope.set('Id', str(next_id))
            next_id += 1

            # EnvelopeTarget pointant vers le SampleStart
            envelope_target = etree.SubElement(envelope, 'EnvelopeTarget')
            pointee_id = etree.SubElement(envelope_target, 'PointeeId')
            pointee_id.set('Value', target_id)

            # Automation avec Events
            automation = etree.SubElement(envelope, 'Automation')
            events = etree.SubElement(automation, 'Events')

            # Point initial à temps 0
            event_initial = etree.SubElement(events, 'FloatEvent')
            event_initial.set('Id', str(next_id))
            next_id += 1
            event_initial.set('Time', '0.0')
            event_initial.set('Value', '0.0')

            # Trier les notes par temps
            sorted_notes = sorted(notes, key=lambda n: n['time'])

            # Ajouter les points pour chaque note
            for note in sorted_notes:
                time = note['time']
                duration = note.get('duration', 1.0)
                sample_offset_xm = note.get('sample_offset_xm')

                if sample_offset_xm is not None:
                    # Note avec effet 9xx : créer 2 points (plateau)
                    # Conversion correcte : 9xx utilise un multiplicateur de 256 bytes
                    # Pour 16-bit (2 bytes/sample) : offset_samples = (xx * 256) / 2
                    offset_in_bytes = sample_offset_xm * 256
                    offset_in_samples = offset_in_bytes / 2.0  # 16-bit
                    sample_start = offset_in_samples / sample_length if sample_length > 0 else 0.0
                    sample_start = max(0.0, min(1.0, sample_start))

                    # Point au début de la note
                    event_start = etree.SubElement(events, 'FloatEvent')
                    event_start.set('Id', str(next_id))
                    next_id += 1
                    event_start.set('Time', str(time))
                    event_start.set('Value', str(sample_start))

                    # Point à la fin de la note
                    event_end = etree.SubElement(events, 'FloatEvent')
                    event_end.set('Id', str(next_id))
                    next_id += 1
                    event_end.set('Time', str(time + duration))
                    event_end.set('Value', str(sample_start))
                else:
                    # Note SANS effet 9xx : créer 2 points à 0.0 (plateau)
                    # Point au début de la note
                    event_start = etree.SubElement(events, 'FloatEvent')
                    event_start.set('Id', str(next_id))
                    next_id += 1
                    event_start.set('Time', str(time))
                    event_start.set('Value', '0.0')

                    # Point à la fin de la note (maintien du plateau)
                    event_end = etree.SubElement(events, 'FloatEvent')
                    event_end.set('Id', str(next_id))
                    next_id += 1
                    event_end.set('Time', str(time + duration))
                    event_end.set('Value', '0.0')

            # Ajouter AutomationTransformViewState
            transform_state = etree.SubElement(automation, 'AutomationTransformViewState')
            is_pending = etree.SubElement(transform_state, 'IsTransformPending')
            is_pending.set('Value', 'false')
            time_transforms = etree.SubElement(transform_state, 'TimeAndValueTransforms')

            num_points = len(events.findall('FloatEvent'))
            print(f"   ✓ '{track_name}': {num_points} automation points")

        # 4. Recompresser et sauvegarder
        xml_bytes = etree.tostring(root, xml_declaration=True, encoding='UTF-8')
        with gzip.open(als_path, 'wb') as f:
            f.write(xml_bytes)

        print(f"   ✓ .als file updated with {len(automation_data)} automations")
        return True

    except Exception as e:
        print(f"   ❌ ERREUR lors du post-processing: {e}")
        import traceback
        traceback.print_exc()
        return False


def create_sample_offset_automation(track, notes, sample_length, next_id):
    """Crée une automation de Sample Start basée sur les effets 9xx des notes

    Args:
        track: L'élément Track Ableton
        notes: Liste des notes avec leur sample_offset_xm (0-255) ou None
        sample_length: Longueur du sample en samples (pour conversion 9xx correcte)
        next_id: Prochain ID disponible (pour AutomationTarget)

    Returns:
        next_id mis à jour

    Note:
        - L'effet 9xx (Sample Offset) est converti en Sample Start (0.0-1.0)
        - Conversion correcte : offset_samples = (xx * 256) / 2, puis sample_start = offset_samples / sample_length
        - Utilise le pattern plateau (2 points par note) pour éviter l'interpolation
    """
    # Filtrer les notes qui ont un sample offset défini
    offset_notes = [(note['time'], note.get('sample_offset_xm'))
                    for note in notes
                    if note.get('sample_offset_xm') is not None]

    print(f"    DEBUG automation 9xx: {len(notes)} notes, {len(offset_notes)} avec effet 9xx")

    if not offset_notes:
        # Pas d'effet 9xx dans cette piste
        print(f"    → Pas d'automation Sample Start (aucune note avec effet 9xx)")
        return next_id

    # Trouver le SampleStart AutomationTarget dans LoopModulators
    # LoopModulators est dans OriginalSimpler (pas dans MultiSampler)
    sample_start_target = track.element.find('.//OriginalSimpler/LoopModulators/SampleStart/AutomationTarget')
    if sample_start_target is None:
        print("    ⚠️  SampleStart AutomationTarget not found (Simpler only)")
        return next_id

    sample_start_target_id = sample_start_target.get('Id')
    if not sample_start_target_id:
        # Créer un ID si absent
        sample_start_target_id = str(next_id)
        sample_start_target.set('Id', sample_start_target_id)
        next_id += 1

    # IMPORTANT: Activer IsModulated dans LoopModulators (REQUIS pour que l'automation soit visible)
    from lxml import etree
    loop_modulators = track.element.find('.//OriginalSimpler/LoopModulators')
    if loop_modulators is not None:
        is_modulated = loop_modulators.find('./IsModulated')
        if is_modulated is None:
            # Créer IsModulated si absent (doit être le PREMIER enfant de LoopModulators)
            is_modulated = etree.Element('IsModulated')
            is_modulated.set('Value', 'true')
            loop_modulators.insert(0, is_modulated)  # Insérer en premier
        else:
            is_modulated.set('Value', 'true')

    # Trouver ou créer AutomationEnvelopes
    from lxml import etree
    automation_envelopes = track.element.find('.//AutomationEnvelopes')
    if automation_envelopes is None:
        automation_envelopes = etree.SubElement(track.element, 'AutomationEnvelopes')

    # Trouver Envelopes - S'il existe, le SUPPRIMER et en recréer un nouveau
    envelopes_container = automation_envelopes.find('.//Envelopes')
    if envelopes_container is not None:
        # Supprimer l'ancien conteneur
        automation_envelopes.remove(envelopes_container)

    # Recréer un nouveau conteneur Envelopes
    envelopes_container = etree.SubElement(automation_envelopes, 'Envelopes')

    # Créer l'AutomationEnvelope
    envelope = etree.SubElement(envelopes_container, 'AutomationEnvelope')
    envelope.set('Id', str(next_id))
    next_id += 1

    # EnvelopeTarget pointant vers le SampleStart
    envelope_target = etree.SubElement(envelope, 'EnvelopeTarget')
    pointee_id = etree.SubElement(envelope_target, 'PointeeId')
    pointee_id.set('Value', sample_start_target_id)

    # Automation avec Events
    automation = etree.SubElement(envelope, 'Automation')
    events = etree.SubElement(automation, 'Events')

    # Créer les points d'automation
    # IMPORTANT: Point initial à temps 0 avec Sample Start = 0.0
    event_initial = etree.SubElement(events, 'FloatEvent')
    event_initial.set('Id', str(next_id))
    next_id += 1
    event_initial.set('Time', '0.0')
    event_initial.set('Value', '0.0')

    # Trier les notes par temps
    sorted_notes = sorted(notes, key=lambda n: n['time'])

    # Ajouter les points pour chaque note
    for note in sorted_notes:
        time = note['time']
        duration = note.get('duration', 1.0)
        sample_offset_xm = note.get('sample_offset_xm')

        if sample_offset_xm is not None:
            # Note avec effet 9xx : créer 2 points (plateau)
            # Conversion correcte : 9xx utilise un multiplicateur de 256 bytes
            # Pour 16-bit (2 bytes/sample) : offset_samples = (xx * 256) / 2
            offset_in_bytes = sample_offset_xm * 256
            offset_in_samples = offset_in_bytes / 2.0  # 16-bit
            sample_start = offset_in_samples / sample_length if sample_length > 0 else 0.0
            sample_start = max(0.0, min(1.0, sample_start))

            # Point au début de la note
            event_start = etree.SubElement(events, 'FloatEvent')
            event_start.set('Id', str(next_id))
            next_id += 1
            event_start.set('Time', str(time))
            event_start.set('Value', str(sample_start))

            # Point à la fin de la note (maintien du plateau)
            event_end = etree.SubElement(events, 'FloatEvent')
            event_end.set('Id', str(next_id))
            next_id += 1
            event_end.set('Time', str(time + duration))
            event_end.set('Value', str(sample_start))

        else:
            # Note SANS effet 9xx : retour à 0.0 (début du sample)
            event_return = etree.SubElement(events, 'FloatEvent')
            event_return.set('Id', str(next_id))
            next_id += 1
            event_return.set('Time', str(time))
            event_return.set('Value', '0.0')

    # Ajouter AutomationTransformViewState (requis par Ableton)
    transform_state = etree.SubElement(automation, 'AutomationTransformViewState')
    is_pending = etree.SubElement(transform_state, 'IsTransformPending')
    is_pending.set('Value', 'false')
    time_transforms = etree.SubElement(transform_state, 'TimeAndValueTransforms')

    num_points = len(events.findall('FloatEvent'))
    print(f"    → Automation Sample Start (9xx): {num_points} points")

    return next_id


def create_pan_automation(track, notes, sample_default_pan, next_id):
    """Crée une automation de panning basée sur les effets 8xx des notes

    Args:
        track: L'élément Track Ableton
        notes: Liste des notes avec leur panning_xm (0-255) ou None
        sample_default_pan: Panning par défaut du sample (0-255)
        next_id: Prochain ID disponible (pour AutomationTarget)

    Returns:
        next_id mis à jour
    """
    # Filtrer les notes qui ont un panning défini
    panning_notes = [(note['time'], note.get('panning_xm'))
                     for note in notes
                     if note.get('panning_xm') is not None]

    if not panning_notes:
        # Pas d'effet 8xx dans cette piste
        return next_id

    # Vérifier s'il y a des variations de panning (en incluant le panning par défaut)
    panning_values = [p[1] for p in panning_notes]
    if len(set(panning_values)) == 1 and panning_values[0] == sample_default_pan:
        # Tous les pannings sont identiques au panning par défaut, pas besoin d'automation
        return next_id

    # Trouver le Pan AutomationTarget du Mixer
    mixer_pan_target = track.element.find('.//DeviceChain/Mixer/Pan/AutomationTarget')
    if mixer_pan_target is None:
        print("    ⚠️  Pan AutomationTarget not found")
        return next_id

    pan_target_id = mixer_pan_target.get('Id')
    if not pan_target_id:
        # Créer un ID si absent
        pan_target_id = str(next_id)
        mixer_pan_target.set('Id', pan_target_id)
        next_id += 1

    # Trouver ou créer AutomationEnvelopes
    automation_envelopes = track.element.find('.//AutomationEnvelopes')
    if automation_envelopes is None:
        automation_envelopes = ET.SubElement(track.element, 'AutomationEnvelopes')

    envelopes_container = automation_envelopes.find('.//Envelopes')
    if envelopes_container is None:
        envelopes_container = ET.SubElement(automation_envelopes, 'Envelopes')

    # Créer l'AutomationEnvelope
    envelope = ET.SubElement(envelopes_container, 'AutomationEnvelope')
    envelope.set('Id', str(next_id))
    next_id += 1

    # EnvelopeTarget pointant vers le pan
    envelope_target = ET.SubElement(envelope, 'EnvelopeTarget')
    pointee_id = ET.SubElement(envelope_target, 'PointeeId')
    pointee_id.set('Value', pan_target_id)

    # Automation avec Events
    automation = ET.SubElement(envelope, 'Automation')
    events = ET.SubElement(automation, 'Events')

    # Créer les points d'automation
    # IMPORTANT: Ajouter un point initial à temps 0 avec le panning par défaut du sample
    # Cela reproduit le comportement FastTracker où le panning commence au défaut du sample
    sorted_pan_notes = sorted(panning_notes, key=lambda n: n[0])

    # Ajouter le point initial si la première note ne commence pas à temps 0
    if not sorted_pan_notes or sorted_pan_notes[0][0] > 0:
        pan_ableton_default = (sample_default_pan - 128) / 128.0
        pan_ableton_default = max(-1.0, min(1.0, pan_ableton_default))

        event_initial = ET.SubElement(events, 'FloatEvent')
        event_initial.set('Id', str(next_id))
        next_id += 1
        event_initial.set('Time', '0.0')
        event_initial.set('Value', str(pan_ableton_default))

    # Trier les notes par temps
    sorted_notes = sorted(notes, key=lambda n: n['time'])

    # Calculer le panning par défaut Ableton (pour le retour)
    pan_ableton_default = (sample_default_pan - 128) / 128.0
    pan_ableton_default = max(-1.0, min(1.0, pan_ableton_default))

    # Ajouter les points pour chaque note
    # Comportement FastTracker : retour au panning par défaut si pas d'effet 8xx
    previous_had_panning = False

    for note in sorted_notes:
        time = note['time']
        duration = note.get('duration', 1.0)
        has_panning = note.get('panning_xm') is not None

        if has_panning:
            # Note avec effet 8xx : créer 2 points (plateau)
            panning_xm = note['panning_xm']
            pan_ableton = (panning_xm - 128) / 128.0
            pan_ableton = max(-1.0, min(1.0, pan_ableton))

            # Point au début de la note
            event_start = ET.SubElement(events, 'FloatEvent')
            event_start.set('Id', str(next_id))
            next_id += 1
            event_start.set('Time', str(time))
            event_start.set('Value', str(pan_ableton))

            # Point à la fin de la note
            event_end = ET.SubElement(events, 'FloatEvent')
            event_end.set('Id', str(next_id))
            next_id += 1
            event_end.set('Time', str(time + duration))
            event_end.set('Value', str(pan_ableton))

            previous_had_panning = True

        elif previous_had_panning:
            # Note SANS effet 8xx après une note avec effet : retour au défaut
            event_return = ET.SubElement(events, 'FloatEvent')
            event_return.set('Id', str(next_id))
            next_id += 1
            event_return.set('Time', str(time))
            event_return.set('Value', str(pan_ableton_default))

            previous_had_panning = False

    # Ajouter AutomationTransformViewState (requis par Ableton, leçon apprise !)
    transform_state = ET.SubElement(automation, 'AutomationTransformViewState')
    is_pending = ET.SubElement(transform_state, 'IsTransformPending')
    is_pending.set('Value', 'false')
    time_transforms = ET.SubElement(transform_state, 'TimeAndValueTransforms')

    num_points = len(events.findall('FloatEvent'))
    print(f"    → Automation panning: {num_points} points")

    return next_id


def configure_envelope_adsr(sampler, envelope_info, bpm, speed):
    """Configure l'enveloppe ADSR du Sampler basée sur l'enveloppe volume FT2

    Args:
        sampler: L'élément MultiSampler Ableton
        envelope_info: Infos enveloppe FT2 {'enabled', 'points', 'sustain_point', 'sustain_enabled', 'num_points'}
        bpm: BPM du module (pour conversion temps)
        speed: Speed du module (ticks par row, défaut 6)

    Returns:
        True si enveloppe configurée, False sinon
    """

    # Si enveloppe désactivée ou pas de points, ne rien faire
    if not envelope_info.get('enabled') or not envelope_info.get('points'):
        return False

    points = envelope_info['points']
    num_points = envelope_info['num_points']
    sustain_enabled = envelope_info.get('sustain_enabled', False)
    sustain_point = envelope_info.get('sustain_point', 1)

    # Conversion temps FT2 (ticks) → millisecondes
    # 1 tick = (2500 / bpm) ms à Speed=6
    tick_to_ms = 2500.0 / bpm

    # Trouver l'enveloppe VOLUME (pas Filter !)
    # L'enveloppe de volume est dans VolumeAndPan, pas dans SimplerFilter
    volume_and_pan = sampler.find('.//VolumeAndPan')
    if volume_and_pan is None:
        print("    ⚠️  VolumeAndPan not found in Sampler")
        return False

    envelope = volume_and_pan.find('.//Envelope')
    if envelope is None:
        print("    ⚠️  Volume envelope not found in VolumeAndPan")
        return False

    # Trouver le pic (valeur max)
    max_val = 0
    max_idx = 0
    for i in range(num_points):
        if points[i][1] > max_val:
            max_val = points[i][1]
            max_idx = i

    # === STRATÉGIE SELON LE NOMBRE DE POINTS ===

    if num_points == 2:
        # Cas A : Enveloppe simple à 2 points (66% des cas)
        time_0, val_0 = points[0]
        time_1, val_1 = points[1]

        attack_time = 0.1
        attack_level = 0.0
        attack_slope = 0.0

        decay_time = max(1.0, time_1 * tick_to_ms)
        decay_level = val_0 / 64.0
        decay_slope = 1.0

        sustain_level = val_1 / 64.0

        release_time = 50.0
        release_level = 0.0
        release_slope = 0.0

    elif sustain_enabled and sustain_point < num_points:
        # Cas B : Enveloppe avec sustain FT2 défini
        peak_time, peak_val = points[max_idx]
        sustain_time, sustain_val = points[sustain_point]
        last_time, last_val = points[num_points - 1]

        attack_time = max(0.1, peak_time * tick_to_ms)
        attack_level = 0.0
        attack_slope = 0.0

        decay_time = max(1.0, (sustain_time - peak_time) * tick_to_ms)
        decay_level = peak_val / 64.0
        decay_slope = 0.5

        sustain_level = sustain_val / 64.0

        release_time = max(1.0, (last_time - sustain_time) * tick_to_ms)
        release_level = 0.0
        release_slope = 0.0

    else:
        # Cas C : Enveloppe complexe sans sustain
        peak_time, peak_val = points[max_idx]
        last_time, last_val = points[num_points - 1]

        mid_idx = (max_idx + num_points - 1) // 2
        mid_time, mid_val = points[mid_idx]

        attack_time = max(0.1, peak_time * tick_to_ms)
        attack_level = 0.0
        attack_slope = 0.0

        decay_time = max(1.0, (mid_time - peak_time) * tick_to_ms)
        decay_level = peak_val / 64.0
        decay_slope = 0.5

        sustain_level = mid_val / 64.0

        release_time = max(1.0, (last_time - mid_time) * tick_to_ms)
        release_level = 0.0
        release_slope = 0.0

    # === CONFIGURATION DES PARAMÈTRES ABLETON ===

    def set_envelope_param(param_name, value):
        elem = envelope.find(f'.//{param_name}/Manual')
        if elem is not None:
            elem.set('Value', str(value))

    set_envelope_param('AttackTime', attack_time)
    set_envelope_param('AttackLevel', attack_level)
    set_envelope_param('AttackSlope', attack_slope)

    set_envelope_param('DecayTime', decay_time)
    set_envelope_param('DecayLevel', decay_level)
    set_envelope_param('DecaySlope', decay_slope)

    set_envelope_param('SustainLevel', sustain_level)

    set_envelope_param('ReleaseTime', release_time)
    set_envelope_param('ReleaseLevel', release_level)
    set_envelope_param('ReleaseSlope', release_slope)

    print(f"    → Enveloppe ADSR: {num_points} pts FT2 → A={attack_time:.1f}ms D={decay_time:.1f}ms S={sustain_level:.2f} R={release_time:.1f}ms")

    return True


def merge_and_deduplicate_notes(notes_lists):
    """Fusionne plusieurs listes de notes et déduplique les notes identiques au même moment

    Args:
        notes_lists: Liste de listes de notes à fusionner

    Returns:
        Liste de notes fusionnées et triées par temps, sans doublons
    """
    # Fusionner toutes les notes
    all_notes = []
    for notes in notes_lists:
        all_notes.extend(notes)

    # Trier par temps
    all_notes.sort(key=lambda x: x['time'])

    # Dédupliquer: si 2 notes ont même temps et même note MIDI, garder une seule
    deduplicated = []
    seen = set()

    for note in all_notes:
        # Clé unique: (temps, note MIDI)
        # On arrondit le temps à 4 décimales pour éviter les problèmes de float
        key = (round(note['time'], 4), note['note'])

        if key not in seen:
            seen.add(key)
            deduplicated.append(note)

    return deduplicated


def distribute_notes_to_avoid_overlap(merged_notes):
    """Distribue les notes sur plusieurs pistes pour éviter les chevauchements temporels

    Cette fonction implémente un algorithme de bin-packing pour assigner les notes
    à différentes pistes de manière à ce qu'aucune note ne chevauche une autre
    dans la même piste (éviter la polyphonie par piste).

    Args:
        merged_notes: Liste de notes triées par temps

    Returns:
        Liste de listes de notes (une liste par piste nécessaire)
        Exemple: [[notes_piste_1], [notes_piste_2], ...]
        La première liste correspond à "All notes", les suivantes aux auxiliaires
    """
    if not merged_notes:
        return [[]]

    # Liste de pistes, chaque piste contient des notes
    tracks = [[]]

    for note in merged_notes:
        note_start = note['time']
        note_end = note_start + note['duration']

        # Trouver une piste où cette note ne chevauche aucune note existante
        placed = False
        for track in tracks:
            # Vérifier si la note peut être placée dans cette piste
            has_overlap = False
            for existing_note in track:
                existing_start = existing_note['time']
                existing_end = existing_start + existing_note['duration']

                # Chevauchement si les intervalles se superposent
                # Note: on utilise une petite marge (0.001) pour éviter les problèmes de float
                if not (note_end <= existing_start + 0.001 or note_start >= existing_end - 0.001):
                    has_overlap = True
                    break

            if not has_overlap:
                track.append(note)
                placed = True
                break

        # Si aucune piste ne convient, créer une nouvelle piste auxiliaire
        if not placed:
            tracks.append([note])

    return tracks


def generate_als_with_n_tracks(num_tracks):
    """Génère un fichier .als temporaire avec N pistes à partir du template minimal embarqué

    Args:
        num_tracks: Nombre de MIDI tracks nécessaires

    Returns:
        Chemin du fichier .als créé, ou None en cas d'erreur
    """
    import gzip
    import tempfile
    from lxml import etree
    from copy import deepcopy

    print(f"🏗️  Generating template with {num_tracks} tracks (embedded template)...")

    # Créer un fichier temporaire
    temp_file = tempfile.NamedTemporaryFile(mode='wb', suffix='.als', delete=False)
    output_path = temp_file.name
    temp_file.close()

    try:
        # Charger le template minimal embarqué (1 piste)
        root = get_minimal_template_root()

        # Trouver la MidiTrack unique
        midi_tracks = root.findall('.//MidiTrack')
        if len(midi_tracks) != 1:
            print(f"❌ Error: minimal template must contain exactly 1 track (found: {len(midi_tracks)})")
            os.unlink(output_path)
            return None

        template_track = midi_tracks[0]

        if num_tracks == 1:
            # Pas besoin de dupliquer
            print(f"   ✓ 1 track (no duplication needed)")
            xml_str = etree.tostring(root, encoding='UTF-8', xml_declaration=True)
            with gzip.open(output_path, 'wb', compresslevel=9) as f:
                f.write(xml_str)
            return output_path

        # Trouver le parent des MidiTrack (Tracks)
        tracks_parent = root.find('.//Tracks')
        if tracks_parent is None:
            print("❌ Cannot find <Tracks> element!")
            os.unlink(output_path)
            return None

        # Trouver l'index de la première ReturnTrack (on va insérer AVANT)
        return_track = tracks_parent.find('.//ReturnTrack')
        if return_track is None:
            insert_index = len(list(tracks_parent))
        else:
            insert_index = list(tracks_parent).index(return_track)

        # Collecter tous les IDs existants
        existing_ids = set()
        for elem in root.iter():
            if 'Id' in elem.attrib:
                try:
                    existing_ids.add(int(elem.attrib['Id']))
                except (ValueError, TypeError):
                    pass

        next_id = max(existing_ids) + 1 if existing_ids else 100000

        # Dupliquer la piste (num_tracks - 1) fois
        tracks_to_add = num_tracks - 1
        print(f"   Duplicating template track {tracks_to_add} times...")

        for i in range(tracks_to_add):
            # Créer une copie profonde de la piste template
            new_track = deepcopy(template_track)

            # Mettre à jour le nom
            name_elem = new_track.find('.//Name/EffectiveName')
            if name_elem is not None:
                new_name = f"Track {i + 2}"
                name_elem.set('Value', new_name)

            # Régénérer tous les IDs
            for elem in new_track.iter():
                if 'Id' in elem.attrib:
                    try:
                        int(elem.attrib['Id'])  # Vérifier que c'est un nombre
                        elem.attrib['Id'] = str(next_id)
                        next_id += 1
                    except (ValueError, TypeError):
                        pass

            # Insérer AVANT les ReturnTrack
            # insert() insère AVANT l'index, donc:
            # i=0 → insert_index (décale ReturnTrack vers la droite)
            # i=1 → insert_index+1 (après la première piste ajoutée)
            # etc.
            tracks_parent.insert(insert_index + i, new_track)

            # Progression
            if (i + 1) % 50 == 0 or (i + 1) == tracks_to_add:
                print(f"   {i + 1}/{tracks_to_add} tracks added...")

        # Mettre à jour NextPointeeId
        next_pointee_elem = root.find('.//NextPointeeId')
        if next_pointee_elem is not None:
            next_pointee_elem.set('Value', str(next_id))

        # Vérifier le nombre total de pistes
        total_tracks = len(root.findall('.//MidiTrack'))
        print(f"   ✓ {total_tracks} tracks created")

        if total_tracks != num_tracks:
            print(f"   ⚠️  ATTENTION: {total_tracks} pistes au lieu de {num_tracks}!")

        # Sauvegarder le fichier .als
        print(f"   Compressing and saving...")
        xml_str = etree.tostring(root, encoding='UTF-8', xml_declaration=True)

        with gzip.open(output_path, 'wb', compresslevel=9) as f:
            f.write(xml_str)

        size_kb = os.path.getsize(output_path) / 1024
        print(f"   ✓ File created: {size_kb:.1f} KB")

        return output_path

    except Exception as e:
        print(f"❌ Error during generation: {e}")
        if os.path.exists(output_path):
            os.unlink(output_path)
        return None


def create_template_with_n_tracks(base_template_path, output_path, num_tracks):
    """Crée un template Ableton avec N MIDI tracks à partir d'un template de base

    Args:
        base_template_path: Chemin du template de base (ex: template_100_tracks.als)
        output_path: Chemin du template à créer
        num_tracks: Nombre total de pistes désirées

    Returns:
        True si succès, False sinon
    """
    import gzip
    from lxml import etree
    from copy import deepcopy

    print(f"📦 Creating template with {num_tracks} tracks...")

    # Charger le template de base
    with gzip.open(base_template_path, 'rb') as f:
        tree = etree.parse(f)
        root = tree.getroot()

    # Trouver toutes les MidiTrack existantes
    midi_tracks = root.findall('.//MidiTrack')
    base_num_tracks = len(midi_tracks)

    if base_num_tracks >= num_tracks:
        # Pas besoin de dupliquer, copier simplement le template
        print(f"   Base template already has {base_num_tracks} tracks (>= {num_tracks})")
        with gzip.open(output_path, 'wb', compresslevel=9) as f:
            f.write(etree.tostring(root, encoding='UTF-8', xml_declaration=True))
        return True

    tracks_to_add = num_tracks - base_num_tracks
    print(f"   Template de base: {base_num_tracks} pistes")
    print(f"   Tracks to add: {tracks_to_add}")

    # Trouver le parent des MidiTrack (Tracks)
    tracks_parent = root.find('.//Tracks')
    if tracks_parent is None:
        print("❌ Cannot find <Tracks> element!")
        return False

    # Trouver l'index de la première ReturnTrack (on va insérer AVANT)
    return_track = tracks_parent.find('.//ReturnTrack')
    if return_track is None:
        insert_index = len(list(tracks_parent))
    else:
        insert_index = list(tracks_parent).index(return_track)

    # Collecter tous les IDs existants pour éviter les collisions
    existing_ids = set()
    for elem in root.iter():
        if 'Id' in elem.attrib:
            existing_ids.add(int(elem.attrib['Id']))

    next_id = max(existing_ids) + 1 if existing_ids else 100000

    # Dupliquer les tracks needed (on duplique cycliquement si besoin)
    for i in range(tracks_to_add):
        # Dupliquer la piste i % base_num_tracks
        source_track = midi_tracks[i % base_num_tracks]
        new_track = deepcopy(source_track)

        # Mettre à jour le nom de la piste
        name_elem = new_track.find('.//Name/EffectiveName')
        if name_elem is not None:
            new_name = f"Track {base_num_tracks + i + 1}"
            name_elem.set('Value', new_name)

        # Remplacer tous les IDs par de nouveaux IDs uniques
        for elem in new_track.iter():
            if 'Id' in elem.attrib:
                elem.attrib['Id'] = str(next_id)
                next_id += 1

        # Insérer la nouvelle piste AVANT les ReturnTrack
        tracks_parent.insert(insert_index + i, new_track)

        # Afficher la progression
        if (i + 1) % 50 == 0 or (i + 1) == tracks_to_add:
            print(f"   {i + 1}/{tracks_to_add} tracks added...")

    # Mettre à jour NextPointeeId
    next_pointee_elem = root.find('.//NextPointeeId')
    if next_pointee_elem is not None:
        next_pointee_elem.set('Value', str(next_id))

    # Sauvegarder le nouveau template
    print(f"   💾 Sauvegarde: {output_path}")
    xml_str = etree.tostring(root, encoding='UTF-8', xml_declaration=True)

    with gzip.open(output_path, 'wb', compresslevel=9) as f:
        f.write(xml_str)

    # Vérifier le résultat
    total_tracks = len(root.findall('.//MidiTrack'))
    print(f"   ✓ Template created: {total_tracks} MIDI tracks")

    return True


def get_or_create_template(num_tracks_needed):
    """Trouve ou crée automatiquement un template avec le nombre de tracks needed

    Args:
        num_tracks_needed: Nombre de MIDI tracks requises

    Returns:
        Chemin du template (str) ou None si erreur
    """
    import os

    # Chemins de recherche
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Arrondir au multiple de 50 supérieur (100, 150, 200, 250, etc.)
    rounded_tracks = ((num_tracks_needed + 49) // 50) * 50
    if rounded_tracks < 100:
        rounded_tracks = 100

    # Chercher un template existant avec au moins ce nombre de pistes
    search_candidates = [
        (rounded_tracks, f"./template_{rounded_tracks}_tracks.als"),
        (rounded_tracks, f"{script_dir}/template_{rounded_tracks}_tracks.als"),
        (200, "./template_200_tracks.als"),
        (200, f"{script_dir}/template_200_tracks.als"),
        (100, "./template_100_tracks.als"),
        (100, f"{script_dir}/template_100_tracks.als"),
    ]

    # Chercher un template qui existe et qui a assez de pistes
    for num, path in search_candidates:
        if os.path.exists(path) and num >= num_tracks_needed:
            print(f"📋 Utilisation du template: {os.path.basename(path)} ({num} tracks)")
            return path

    # No template found with assez de pistes, créer automatiquement
    print(f"\n📦 No template found with {num_tracks_needed} pistes")

    # Trouver un template de base pour duplication
    base_template = None
    for _, path in [(100, "./template_100_tracks.als"), (100, f"{script_dir}/template_100_tracks.als")]:
        if os.path.exists(path):
            base_template = path
            break

    if base_template is None:
        print("❌ ERREUR: No base template found (template_100_tracks.als)")
        print("   Please create a base template with at least 100 MIDI tracks.")
        return None

    # Créer le nouveau template
    output_path = f"./template_{rounded_tracks}_tracks.als"
    print(f"   Base: {os.path.basename(base_template)}")
    print(f"   Cible: {os.path.basename(output_path)} ({rounded_tracks} tracks)")

    if not create_template_with_n_tracks(base_template, output_path, rounded_tracks):
        return None

    print(f"   ✓ Template created automatically!")
    return output_path


def convert_xm_to_ableton(xm_path, template_path=None, output_dir=None, enable_pan_automation=False, enable_envelope=False, enable_sample_offset=False, enable_merge_tracks=False):
    """Convertit un fichier XM/MOD en projet Ableton

    Args:
        xm_path: Chemin vers le fichier XM/MOD
        template_path: Chemin vers le template Ableton (optionnel, auto-créé si nécessaire)
        output_dir: Répertoire de sortie (optionnel)
        enable_pan_automation: Activer les automations de panning (effet 8xx)
        enable_envelope: Activer la conversion des enveloppes volume (expérimental)
        enable_sample_offset: Activer l'effet 9xx (Sample Offset) avec Simpler
    """

    if not os.path.exists(xm_path):
        print(f"❌ Erreur: '{xm_path}' n'existe pas")
        return False

    # Détecter le format du fichier
    file_ext = os.path.splitext(xm_path)[1].lower()
    is_mod = (file_ext == '.mod')
    is_xm = (file_ext == '.xm')

    if not (is_mod or is_xm):
        print(f"⚠️  Attention: '{xm_path}' n'a pas l'extension .xm ou .mod")

    format_name = "MOD" if is_mod else "XM"
    print("="*60)
    print(f"CONVERSION {format_name} → ABLETON LIVE (avec buildable)")
    print("="*60)

    # Avertissement si enveloppes activées
    if enable_envelope:
        print("\n⚠️  EXPERIMENTAL MODE: Envelope conversion enabled")
        print("   FT2 multi-point envelopes are approximated to Ableton ADSR.")
        print("   Approximate result, manual adjustments recommended.\n")

    # Préparer les répertoires
    if not output_dir:
        xm_basename = os.path.splitext(os.path.basename(xm_path))[0]
        # Create in "xm2live_converted_tracks" subdirectory
        base_dir = os.path.dirname(xm_path)
        conversions_dir = os.path.join(base_dir, "xm2live_converted_tracks")
        os.makedirs(conversions_dir, exist_ok=True)
        output_dir = os.path.join(conversions_dir, f"{xm_basename}_Ableton_Project")

    os.makedirs(output_dir, exist_ok=True)

    samples_dir = os.path.join(output_dir, 'Samples')
    os.makedirs(samples_dir, exist_ok=True)

    # Extraire samples ET patterns selon le format
    print(f"\n📀 Extraction du fichier {format_name}...")
    if is_mod:
        samples, patterns, xm_info = extract_mod(xm_path, samples_dir)
        organize_func = organize_mod
    else:
        samples, patterns, xm_info = extract_xm(xm_path, samples_dir)
        organize_func = organize_xm

    if not samples:
        print("\n❌ Aucun sample extrait !")
        return False

    # Lire les enveloppes si activé (fonction séparée, plus sûr)
    if enable_envelope and not is_mod:  # Seulement pour XM, pas MOD
        try:
            from envelope_reader import read_xm_envelopes
        except ImportError:
            print("⚠️  AVERTISSEMENT: Le module envelope_reader n'est pas disponible.")
            print("   Envelope conversion is disabled.")
            enable_envelope = False
        else:
            print(f"\n🎛️  Lecture des enveloppes volume...")
            envelopes = read_xm_envelopes(xm_path)
            # Merger les enveloppes avec les samples
            for sample in samples:
                inst_num = sample['instrument']
                if inst_num in envelopes:
                    sample['envelope'] = envelopes[inst_num]
                else:
                    # Enveloppe par défaut
                    sample['envelope'] = {
                        'enabled': False,
                        'sustain_enabled': False,
                        'num_points': 0,
                        'sustain_point': 0,
                        'points': []
                    }
            print(f"    → {len(envelopes)} envelopes loaded")

    print(f"\n{'='*60}")
    print(f"MODULE: {xm_info['name']}")
    print(f"Speed: {xm_info['tempo']} ticks/row")
    print(f"BPM: {xm_info['bpm']}")
    real_bpm = xm_info['bpm'] * (6.0 / xm_info['tempo'])
    print(f"Real BPM: {real_bpm:.2f}")
    print(f"Canaux: {xm_info['channels']}")
    print(f"Samples: {len(samples)}")
    print(f"Patterns: {len(patterns)}")
    print(f"{'='*60}")

    # Détecter les instruments qui utilisent l'effet 9xx (Sample Offset)
    instruments_with_9xx = set()
    if enable_sample_offset:
        file_format = 'mod' if is_mod else 'xm'
        instruments_with_9xx = detect_effect_9xx_per_instrument(
            patterns,
            xm_info['pattern_order'],
            file_format
        )
        if instruments_with_9xx:
            inst_hex_list = [f"{i:02X}" for i in sorted(instruments_with_9xx)]
            print(f"\n🎚️  Effect 9xx detected on {len(instruments_with_9xx)} instrument(s): {', '.join(inst_hex_list)}")
            print(f"   → These instruments will use Simpler (Sample Start automatable)")
        else:
            print(f"\n🎚️  No effect 9xx detected")

    # Organiser les pistes par canal
    tracks_data = organize_func(
        patterns,
        xm_info['pattern_order'],
        samples,
        xm_info['channels']
    )

    # Créer un mapping instrument -> sample
    instrument_to_sample = {s['instrument']: s for s in samples}

    # Compter combien de pistes on va utiliser
    num_tracks_needed = sum(
        len([inst for inst in tracks_data[channel].keys() if inst in instrument_to_sample])
        for channel in tracks_data.keys()
    )

    print(f"\n🎹 {num_tracks_needed} tracks needed")

    # Obtenir ou générer le template avec le bon nombre de pistes
    if template_path is None:
        # Générer dynamiquement un template avec le nombre exact de pistes
        template_path = generate_als_with_n_tracks(num_tracks_needed)
        if template_path is None:
            return False
    else:
        # Template spécifié par l'utilisateur
        if not os.path.exists(template_path):
            print(f"❌ Erreur: template '{template_path}' n'existe pas")
            return False
        print(f"📋 Utilisation du template: {os.path.basename(template_path)}")

    # Charger le template
    print(f"\n📝 Loading template...")
    live_set = LiveSet.from_file(template_path)
    print(f"   Template loaded: {len(live_set.primary_tracks)} tracks")

    # Extraire le MultiSamplePart de la première piste pour l'utiliser comme template
    first_track = live_set.primary_tracks[0]
    template_sample_part = first_track.element.find('.//MultiSampler/Player/MultiSampleMap/SampleParts/MultiSamplePart')

    if template_sample_part is None:
        print(f"\n❌ ERREUR: First track must contain a MultiSampler with a sample!")
        print(f"   Please create a template with a sample in the first track.")
        return False

    print(f"   ✓ Template MultiSamplePart extracted from first track")

    if num_tracks_needed > len(live_set.primary_tracks):
        print(f"\n⚠️  ATTENTION: {num_tracks_needed} tracks needed mais seulement {len(live_set.primary_tracks)} disponibles!")
        print(f"   Extra tracks will be ignored.")

    # Créer un mapping instrument -> couleur
    # Utiliser des couleurs différentes pour chaque instrument (0-69 sont les couleurs d'Ableton)
    unique_instruments = sorted(set(inst for channel_data in tracks_data.values() for inst in channel_data.keys() if inst in instrument_to_sample))
    instrument_colors = {}
    color_palette = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
                     21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40,
                     41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60,
                     61, 62, 63, 64, 65, 66, 67, 68, 69]

    for i, inst in enumerate(unique_instruments):
        instrument_colors[inst] = color_palette[i % len(color_palette)]

    # Organiser les pistes par instrument puis par canal
    # Créer une liste de toutes les pistes à créer
    tracks_to_create = []

    # D'abord grouper par instrument
    for instrument in unique_instruments:
        sample = instrument_to_sample[instrument]
        # Notation hexadécimale pour l'instrument (01, 02, ..., 0A, 0B, etc.)
        inst_hex = f"{instrument:02X}"
        sample_name = sample['name'].strip() if sample['name'].strip() else f"Instrument_{inst_hex}_Sample_1"

        # Collecter toutes les pistes (canaux) qui utilisent cet instrument
        instrument_tracks = []
        for channel in sorted(tracks_data.keys()):
            if instrument in tracks_data[channel]:
                notes = tracks_data[channel][instrument]
                track_name = f"Ch{channel+1} - {sample_name}"

                instrument_tracks.append({
                    'instrument': instrument,
                    'channel': channel,
                    'sample': sample,
                    'notes': notes,
                    'track_name': track_name,
                    'sample_name': sample_name,
                    'color': instrument_colors[instrument]
                })

        # Si --merge-tracks activé, créer uniquement les pistes fusionnées (pas les pistes individuelles)
        if enable_merge_tracks and len(instrument_tracks) > 0:
            # Fusionner toutes les notes de cet instrument
            all_notes_lists = [track['notes'] for track in instrument_tracks]
            merged_notes = merge_and_deduplicate_notes(all_notes_lists)

            # Distribuer les notes sur plusieurs pistes pour éviter les chevauchements
            distributed_tracks = distribute_notes_to_avoid_overlap(merged_notes)

            # Créer la piste "All notes" (première distribution) + pistes auxiliaires si nécessaire
            for i, notes_for_track in enumerate(distributed_tracks):
                if len(notes_for_track) == 0:
                    continue  # Skip empty tracks

                # Nom de la piste: "All notes" pour la première, "All notes (2)", "All notes (3)", etc. pour les auxiliaires
                if i == 0:
                    track_name = "All notes"
                else:
                    track_name = f"All notes ({i+1})"

                merged_track = {
                    'instrument': instrument,
                    'channel': None,  # Pas de canal spécifique
                    'sample': sample,
                    'notes': notes_for_track,
                    'track_name': track_name,
                    'sample_name': sample_name,
                    'color': instrument_colors[instrument]
                }

                tracks_to_create.append(merged_track)

        else:
            # Mode normal: créer les pistes individuelles par canal
            tracks_to_create.extend(instrument_tracks)

    # Recalculer le nombre de tracks needed APRÈS avoir ajouté les pistes ALL
    actual_tracks_needed = len(tracks_to_create)
    if actual_tracks_needed > num_tracks_needed:
        print(f"   → {actual_tracks_needed - num_tracks_needed} ALL tracks added (total: {actual_tracks_needed})")
        num_tracks_needed = actual_tracks_needed

        # Régénérer le template si nécessaire
        # Vérifier si c'est un template temporaire (commence par /tmp/ ou /var/folders/)
        import tempfile
        is_temp_template = template_path and (template_path.startswith('/tmp/') or template_path.startswith(tempfile.gettempdir()))
        if template_path and os.path.exists(template_path) and is_temp_template:
            # C'est un template temporaire généré, on peut le remplacer
            os.unlink(template_path)
            template_path = generate_als_with_n_tracks(num_tracks_needed)
            if template_path is None:
                return False
            # Recharger le template
            live_set = LiveSet.from_file(template_path)
            # Ré-extraire le template sample part
            first_track = live_set.primary_tracks[0]
            template_sample_part = first_track.element.find('.//MultiSampler/Player/MultiSampleMap/SampleParts/MultiSamplePart')

    # Créer les pistes dans l'ordre
    track_index = 0
    pistes_creees = []

    # Compteur global pour tous les IDs (AutomationTarget, AutomationEnvelope, etc.)
    # Commence à 100000 pour éviter les conflits avec le template
    global_next_id = 100000

    # Liste pour collecter les données d'automation Sample Start (post-processing)
    automation_data_list = []

    for track_info in tracks_to_create:
        if track_index >= len(live_set.primary_tracks):
            print(f"\n⚠️  Plus de pistes disponibles dans le template!")
            break

        # Utiliser la piste existante du template
        track = live_set.primary_tracks[track_index]

        # Modifier la piste
        update_track_name(track, track_info['track_name'])
        update_track_color(track, track_info['color'])

        # Toutes les pistes (y compris ALL) ont un instrument
        automation_target_id = None
        instrument = track_info['instrument']
        use_simpler = (enable_sample_offset and instrument in instruments_with_9xx)

        if use_simpler:
            # Utiliser Simpler (pour effet 9xx)
            global_next_id, automation_target_id = populate_track_with_simpler(track, track_info['sample'], output_dir, global_next_id,
                                       bpm=xm_info['bpm'], speed=xm_info['tempo'], enable_envelope=enable_envelope)
        else:
            # Utiliser Sampler (normal)
            update_sampler_sample(track, track_info['sample'], output_dir, template_sample_part,
                                bpm=xm_info['bpm'], speed=xm_info['tempo'], enable_envelope=enable_envelope)

        # Ajouter les notes MIDI
        update_midi_clip_notes(track, track_info['notes'])

        # Collecter les données pour l'automation Sample Start (si activé et Simpler)
        if use_simpler and enable_sample_offset and automation_target_id:
            # Filtrer les notes qui ont un sample offset défini
            offset_notes = [note for note in track_info['notes'] if note.get('sample_offset_xm') is not None]

            if offset_notes:
                # Collecter les données pour post-processing
                automation_data_list.append({
                    'track_name': track_info['track_name'],
                    'track_index': track_index,  # Index de piste pour identification unique
                    'target_id': automation_target_id,
                    'notes': track_info['notes'],
                    'sample_length': track_info['sample']['length']  # Longueur en samples
                })
                print(f"    → Sample Start automation prepared ({len(offset_notes)} notes with effect 9xx)")

        # Créer l'automation de panning basée sur les effets 8xx (si activé)
        if enable_pan_automation:
            sample_default_pan = track_info['sample'].get('panning', 128)
            global_next_id = create_pan_automation(track, track_info['notes'], sample_default_pan, global_next_id)

        # Désactiver le mode record (arm) sur toutes les pistes
        recorder = track.element.find('.//Recorder')
        if recorder is not None:
            is_armed = recorder.find('.//IsArmed')
            if is_armed is not None:
                is_armed.set('Value', 'false')

        # Stocker une référence à l'élément XML pour la création de groupes
        track_info['track_element'] = track.element

        pistes_creees.append(track_info['track_name'])
        print(f"  ✓ {track_info['track_name']} ({len(track_info['notes'])} notes, color={track_info['color']})")
        track_index += 1

    # Créer des groupes pour les instruments avec 2+ pistes AVANT de supprimer les pistes
    print(f"\n📁 Creating track groups...")

    # Identifier les instruments avec plusieurs pistes
    # Ne garder que les tracks qui ont été réellement créées (avec track_element)
    instrument_track_counts = {}
    for track_info in tracks_to_create:
        # Ignorer les tracks qui n'ont pas été créées (manque de pistes dans le template)
        if 'track_element' not in track_info:
            continue
        inst = track_info['instrument']
        if inst not in instrument_track_counts:
            instrument_track_counts[inst] = []
        instrument_track_counts[inst].append(track_info)

    # Trouver le nombre de scènes pour créer les GroupTrackSlots
    num_scenes = 8  # Par défaut 8, mais on peut le lire du LiveSet
    scenes = live_set.element.find('.//Scenes')
    if scenes is not None:
        num_scenes = len(list(scenes))

    # Trouver l'élément Tracks
    tracks_container = live_set.element.find('.//Tracks')

    # Compteur pour l'ID du GroupTrack
    next_group_id = 10000

    # NOTE: On réutilise global_next_id de la boucle précédente
    # NE PAS réinitialiser ici car les Simplers ont déjà utilisé des IDs

    # Parcourir les instruments dans l'ordre (pour insérer les groups aux bons endroits)
    for instrument in unique_instruments:
        if instrument not in instrument_track_counts:
            continue

        tracks_for_inst = instrument_track_counts[instrument]

        # Créer un groupe seulement si 2+ pistes
        if len(tracks_for_inst) >= 2:
            sample_name = tracks_for_inst[0]['sample_name']
            color = tracks_for_inst[0]['color']

            # Créer le GroupTrack
            group_track = ET.Element('GroupTrack')
            group_track.set('Id', str(next_group_id))

            # Ajouter les éléments de base
            ET.SubElement(group_track, 'LomId').set('Value', '0')
            ET.SubElement(group_track, 'LomIdView').set('Value', '0')
            ET.SubElement(group_track, 'IsContentSelectedInDocument').set('Value', 'false')
            ET.SubElement(group_track, 'PreferredContentViewMode').set('Value', '0')

            # TrackDelay
            track_delay = ET.SubElement(group_track, 'TrackDelay')
            ET.SubElement(track_delay, 'Value').set('Value', '0')
            ET.SubElement(track_delay, 'IsValueSampleBased').set('Value', 'false')

            # Name
            name_elem = ET.SubElement(group_track, 'Name')
            ET.SubElement(name_elem, 'EffectiveName').set('Value', sample_name)
            ET.SubElement(name_elem, 'UserName').set('Value', sample_name)
            ET.SubElement(name_elem, 'Annotation').set('Value', '')
            ET.SubElement(name_elem, 'MemorizedFirstClipName').set('Value', '')

            # Color
            ET.SubElement(group_track, 'Color').set('Value', str(color))

            # AutomationEnvelopes
            auto_env = ET.SubElement(group_track, 'AutomationEnvelopes')
            ET.SubElement(auto_env, 'Envelopes')

            # TrackGroupId (pas de parent)
            ET.SubElement(group_track, 'TrackGroupId').set('Value', '-1')
            ET.SubElement(group_track, 'TrackUnfolded').set('Value', 'false')  # Replier le groupe par défaut

            # Autres éléments
            ET.SubElement(group_track, 'DevicesListWrapper').set('LomId', '0')
            ET.SubElement(group_track, 'ClipSlotsListWrapper').set('LomId', '0')
            ET.SubElement(group_track, 'ArrangementClipsListWrapper').set('LomId', '0')
            ET.SubElement(group_track, 'TakeLanesListWrapper').set('LomId', '0')
            ET.SubElement(group_track, 'ViewData').set('Value', '{}')

            # TakeLanes
            take_lanes = ET.SubElement(group_track, 'TakeLanes')
            ET.SubElement(take_lanes, 'TakeLanes')
            ET.SubElement(take_lanes, 'AreTakeLanesFolded').set('Value', 'true')

            ET.SubElement(group_track, 'LinkedTrackGroupId').set('Value', '-1')

            # Slots (GroupTrackSlot)
            slots = ET.SubElement(group_track, 'Slots')
            for i in range(num_scenes):
                slot = ET.SubElement(slots, 'GroupTrackSlot')
                slot.set('Id', str(i))
                ET.SubElement(slot, 'LomId').set('Value', '0')

            ET.SubElement(group_track, 'Freeze').set('Value', 'false')
            ET.SubElement(group_track, 'NeedArrangerRefreeze').set('Value', 'true')

            # DeviceChain complet pour GroupTrack (créé manuellement)
            device_chain = ET.SubElement(group_track, 'DeviceChain')

            # AudioOutputRouting
            audio_out = ET.SubElement(device_chain, 'AudioOutputRouting')
            ET.SubElement(audio_out, 'Target').set('Value', 'AudioOut/Main')
            ET.SubElement(audio_out, 'UpperDisplayString').set('Value', 'Main')
            ET.SubElement(audio_out, 'LowerDisplayString').set('Value', '')

            # Mixer (élément essentiel pour le Solo/Mute/Volume)
            mixer = ET.SubElement(device_chain, 'Mixer')
            ET.SubElement(mixer, 'LomId').set('Value', '0')
            ET.SubElement(mixer, 'LomIdView').set('Value', '0')

            # Speaker (contrôle on/off de la piste)
            speaker = ET.SubElement(mixer, 'Speaker')
            ET.SubElement(speaker, 'LomId').set('Value', '0')
            ET.SubElement(speaker, 'Manual').set('Value', 'true')
            speaker_auto = ET.SubElement(speaker, 'AutomationTarget')
            speaker_auto.set('Id', str(global_next_id))
            global_next_id += 1
            ET.SubElement(speaker_auto, 'LockEnvelope').set('Value', '0')

            # SoloSink (CRITIQUE pour le fonctionnement du Solo)
            ET.SubElement(mixer, 'SoloSink').set('Value', 'false')

            # Volume
            volume = ET.SubElement(mixer, 'Volume')
            ET.SubElement(volume, 'LomId').set('Value', '0')
            ET.SubElement(volume, 'Manual').set('Value', '1')
            volume_auto = ET.SubElement(volume, 'AutomationTarget')
            volume_auto.set('Id', str(global_next_id))
            global_next_id += 1
            ET.SubElement(volume_auto, 'LockEnvelope').set('Value', '0')

            # DeviceChain imbriqué (pour les effets, vide)
            inner_device_chain = ET.SubElement(device_chain, 'DeviceChain')
            ET.SubElement(inner_device_chain, 'Devices')
            ET.SubElement(inner_device_chain, 'SignalModulations')

            # Trouver la position de la première piste de ce groupe dans tracks_container
            first_track_elem = tracks_for_inst[0]['track_element']
            all_tracks = list(tracks_container)
            insert_position = all_tracks.index(first_track_elem)

            # Insérer le GroupTrack avant la première piste de ce groupe
            tracks_container.insert(insert_position, group_track)

            # Mettre à jour le TrackGroupId des pistes de ce groupe en utilisant les références directes
            for track_info in tracks_for_inst:
                track_elem = track_info['track_element']
                track_group_id = track_elem.find('.//TrackGroupId')
                if track_group_id is not None:
                    track_group_id.set('Value', str(next_group_id))

                # IMPORTANT: Router l'audio de la piste vers le GroupTrack parent
                # C'est ce qui permet au Solo du groupe de fonctionner
                audio_output = track_elem.find('.//AudioOutputRouting/Target')
                if audio_output is not None:
                    audio_output.set('Value', 'AudioOut/GroupTrack')

                # Mettre à jour aussi les DisplayStrings
                upper_display = track_elem.find('.//AudioOutputRouting/UpperDisplayString')
                if upper_display is not None:
                    upper_display.set('Value', sample_name)  # Nom du groupe

                lower_display = track_elem.find('.//AudioOutputRouting/LowerDisplayString')
                if lower_display is not None:
                    lower_display.set('Value', '')

            print(f"  ✓ Group created: {sample_name} ({len(tracks_for_inst)} tracks)")

            next_group_id += 1

    # Supprimer les pistes non utilisées APRÈS avoir créé les groupes
    print(f"\n🗑️  Removing unused tracks...")
    # Garder seulement les pistes que nous avons utilisées (et les GroupTracks)
    all_tracks_in_container = list(tracks_container)

    # Créer un set des éléments de piste utilisés
    used_track_elements = {info['track_element'] for info in tracks_to_create if 'track_element' in info}

    # Supprimer les MIDI tracks inutilisées
    # On ne supprime que les MidiTrack qui ne sont pas dans used_track_elements
    tracks_to_remove = []
    for track_elem in all_tracks_in_container:
        if track_elem.tag == 'MidiTrack' and track_elem not in used_track_elements:
            tracks_to_remove.append(track_elem)

    for track in tracks_to_remove:
        tracks_container.remove(track)

    print(f"   {len(tracks_to_remove)} tracks removed")

    # Replier toutes les pistes utilisées pour une meilleure vue d'ensemble
    print(f"\n📁 Folding tracks and groups...")
    folded_count = 0
    for track_info in tracks_to_create:
        if 'track_element' in track_info:
            track_elem = track_info['track_element']
            track_unfolded = track_elem.find('.//TrackUnfolded')
            if track_unfolded is not None:
                track_unfolded.set('Value', 'false')
                folded_count += 1

    print(f"   {folded_count} tracks folded")

    # Calculer le BPM réel selon la formule FastTracker 2
    # BPM réel = BPM × (6 / Speed)
    xm_speed = xm_info['tempo']  # Dans XM, "tempo" = speed (ticks par row)
    xm_bpm = xm_info['bpm']      # BPM XM (calibré pour speed=6)
    real_bpm = xm_bpm * (6.0 / xm_speed)

    # Mettre à jour le tempo partout où il apparaît
    print(f"\n🎵 Calculating real tempo:")
    print(f"   XM Speed: {xm_speed} ticks/row")
    print(f"   XM BPM: {xm_bpm}")
    print(f"   → Real BPM: {real_bpm:.2f} (formula: {xm_bpm} × 6/{xm_speed})")
    tempo_updated_count = 0
    floatevent_updated_count = 0

    # Le BPM n'est PAS dans <Tempo> mais directement dans <Manual> et <FloatEvent>
    # Chercher tous les éléments Manual avec Value="120" (le défaut du template)
    print(f"   Searching for <Manual> elements with Value='120'...")
    for manual in live_set.element.iter('Manual'):
        if manual.get('Value') == '120':
            manual.set('Value', str(real_bpm))
            tempo_updated_count += 1
            print(f"   → Manual: 120 → {real_bpm:.2f} BPM")

    # Aussi mettre à jour les FloatEvent si présents
    print(f"   Searching for <FloatEvent> elements with Value='120'...")
    for floatevent in live_set.element.iter('FloatEvent'):
        if floatevent.get('Value') == '120':
            floatevent.set('Value', str(real_bpm))
            floatevent_updated_count += 1
            print(f"   → FloatEvent: 120 → {real_bpm:.2f} BPM")

    total_updated = tempo_updated_count + floatevent_updated_count
    if total_updated > 0:
        print(f"   ✓ Tempo updated ({tempo_updated_count} Manual, {floatevent_updated_count} FloatEvent)")
    else:
        print(f"   ⚠️  No element found with Value='120' - BPM may already be different from 120?")

    # Réinitialiser la position de la tête de lecture au début (1.1.1)
    print(f"\n⏮️  Resetting playback position...")

    # 1. CurrentTime dans Transport (affichage de la tête de lecture)
    transport = live_set.element.find('.//Transport')
    if transport is not None:
        current_time = transport.find('.//CurrentTime')
        if current_time is not None:
            current_time.set('Value', '0')
            print(f"   ✓ Transport/CurrentTime reset to 0")
        else:
            print(f"   ⚠️  CurrentTime element not found")
    else:
        print(f"   ⚠️  Transport element not found")

    # 2. TimeSelection global (position de démarrage de la lecture)
    # Chercher le TimeSelection au niveau LiveSet (pas dans les clips)
    # Le chemin est Ableton/LiveSet/TimeSelection
    for time_selection in live_set.element.findall('.//TimeSelection'):
        # Vérifier si c'est le TimeSelection global (parent direct de LiveSet)
        parent = time_selection.getparent()
        if parent is not None and parent.tag == 'LiveSet':
            anchor_time = time_selection.find('.//AnchorTime')
            other_time = time_selection.find('.//OtherTime')
            if anchor_time is not None:
                anchor_time.set('Value', '0')
            if other_time is not None:
                other_time.set('Value', '0')
            print(f"   ✓ Global TimeSelection reset to 0")
            break

    print(f"   → Playback will now start at 1.1.1")

    # Mettre à jour NextPointeeId pour refléter les nouveaux IDs créés
    print(f"\n🔢 Updating NextPointeeId counter...")
    next_pointee_elem = live_set.element.find('.//NextPointeeId')
    if next_pointee_elem is not None:
        old_value = next_pointee_elem.get('Value')
        next_pointee_elem.set('Value', str(global_next_id))
        print(f"   ✓ NextPointeeId: {old_value} → {global_next_id}")
    else:
        print(f"   ⚠️  NextPointeeId not found")

    # Sauvegarder (utiliser le nom du fichier, pas le nom interne du module)
    # Cela évite les conflits quand plusieurs fichiers ont le même nom interne
    xm_basename = os.path.splitext(os.path.basename(xm_path))[0]
    als_path = os.path.join(output_dir, f"{xm_basename}.als")
    print(f"\n💾 Saving project...")
    live_set.write_to_file(als_path)

    # Post-processing: Ajouter les automations Sample Start directement dans le fichier .als
    if enable_sample_offset and automation_data_list:
        add_sample_offset_automations_to_file(als_path, automation_data_list, global_next_id)

    print(f"\n{'='*60}")
    print(f"✓ CONVERSION COMPLETE")
    print(f"{'='*60}")
    print(f"\nProject: {als_path}")
    print(f"Samples: {samples_dir}/")
    print(f"\n📊 SUMMARY:")
    print(f"  • {len(pistes_creees)} tracks created")
    print(f"  • {len(samples)} samples extracted")
    print(f"\n💡 Open the .als file in Ableton Live!")

    return True

def main():
    import argparse
    import sys

    # If no argument, show help
    if len(sys.argv) == 1:
        sys.argv.append('--help')

    parser = argparse.ArgumentParser(
        description='''
╔════════════════════════════════════════════════════════════╗
║     XM2LIVE v3.0 - Tracker → Ableton Live Converter        ║
╚════════════════════════════════════════════════════════════╝

Converts tracker files (FastTracker 2 XM, Amiga MOD) to
Ableton Live projects with samples, notes, tempo and effects.
        ''',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
════════════════════════════════════════════════════════════
USAGE EXAMPLES
════════════════════════════════════════════════════════════

Simple conversion (template auto-created):
  xm2live myfile.xm
  xm2live mytrack.mod

With custom template (optional):
  xm2live myfile.xm /path/to/template.als

With advanced options:
  xm2live file.xm --pan-automation
  xm2live file.xm --sample-offset
  xm2live file.xm --pan-automation --sample-offset

Note: Template is now OPTIONAL. If not specified, a template
      with the exact number of tracks needed will be created
      automatically.

════════════════════════════════════════════════════════════
AVAILABLE OPTIONS
════════════════════════════════════════════════════════════

--pan-automation    Enable panning automations (effect 8xx)
                    Creates Track Pan automations in Ableton
                    Default: disabled

--sample-offset     Enable effect 9xx (Sample Offset) via Simpler
                    Creates Sample Start automations
                    LIMITATION: ping-pong loops → forward
                    Default: disabled

--envelope          Enable FT2 envelope → Ableton ADSR conversion
                    Approximate conversion (12 points → 4 ADSR)
                    EXPERIMENTAL - manual adjustments recommended
                    Default: disabled

--merge-tracks      Merge mode: create one "All notes" track per
                    instrument instead of individual channel tracks.
                    Auto-detects overlapping notes and creates
                    auxiliary tracks if needed.
                    Default: disabled

════════════════════════════════════════════════════════════
OUTPUT
════════════════════════════════════════════════════════════

The converted project will be created in:
  [source directory]/xm2live_converted_tracks/[name]_Ableton_Project/

Contents:
  • [name].als         - Ableton Live project
  • Samples/           - Exported WAV samples (16-bit)

════════════════════════════════════════════════════════════
For help: xm2live --help
Full documentation: README.md
        '''
    )

    parser.add_argument('file', help='XM or MOD file to convert')
    parser.add_argument('template', nargs='?', help='Ableton template (optional)')
    parser.add_argument('--pan-automation', action='store_true',
                        help='Enable panning automations (effect 8xx). '
                             'Disabled by default as rarely used in modules.')
    parser.add_argument('--envelope', action='store_true',
                        help='Enable FT2 volume envelope → Ableton ADSR conversion. '
                             'Disabled by default as it is a simplified approximation.')
    parser.add_argument('--sample-offset', action='store_true',
                        help='Enable effect 9xx (Sample Offset) with Simpler. '
                             'Instruments with effect 9xx will use Simpler instead of Sampler. '
                             'LIMITATION: ping-pong → forward conversion.')
    parser.add_argument('--merge-tracks', action='store_true',
                        help='Merge mode: create one "All notes" track per instrument instead of individual channel tracks. '
                             'Auto-detects temporal overlaps and creates auxiliary tracks if needed.')

    args = parser.parse_args()

    xm_path = args.file
    template_path = args.template  # None if not specified, will be auto-created

    # Fix: If template_path starts with '--', it's a misparsed flag, so set to None
    if template_path and template_path.startswith('--'):
        template_path = None


    try:
        success = convert_xm_to_ableton(xm_path, template_path,
                                        enable_pan_automation=args.pan_automation,
                                        enable_envelope=args.envelope,
                                        enable_sample_offset=args.sample_offset,
                                        enable_merge_tracks=args.merge_tracks)
        if success:
            sys.exit(0)
        else:
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n⚠️  Conversion interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ UNEXPECTED ERROR:")
        print(f"   {type(e).__name__}: {e}")
        print(f"\n   If this error persists, please check:")
        print(f"   1. That the XM/MOD file is not corrupted")
        print(f"   2. That the template is valid (if using one)")
        print(f"   3. That you have write permissions in the directory")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
