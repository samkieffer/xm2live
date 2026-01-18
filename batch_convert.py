#!/usr/bin/env python3
"""
Batch Converter XM/MOD vers Ableton Live
Convertit tous les fichiers .xm et .mod d'un répertoire en projets Ableton Live
"""

import sys
import os
from pathlib import Path
from xm2live import convert_xm_to_ableton
import time


def find_tracker_files(directory, recursive=True):
    """Trouve tous les fichiers .xm et .mod dans un répertoire

    Args:
        directory: Répertoire à scanner
        recursive: Si True, recherche dans les sous-répertoires

    Returns:
        Liste de chemins absolus vers les fichiers trouvés
    """
    files = []
    directory = Path(directory)

    if not directory.exists():
        print(f"❌ Erreur: Directory '{directory}' does not exist")
        return files

    if not directory.is_dir():
        print(f"❌ Erreur: '{directory}' is not a directory")
        return files

    # Patterns de recherche
    patterns = ['*.xm', '*.XM', '*.mod', '*.MOD']

    print(f"🔍 Recherche de fichiers dans: {directory}")
    if recursive:
        print(f"   (recursive search enabled)")

    for pattern in patterns:
        if recursive:
            found = list(directory.rglob(pattern))
        else:
            found = list(directory.glob(pattern))

        files.extend(found)

    # Trier par nom pour un ordre prévisible
    files.sort(key=lambda x: x.name.lower())

    return [str(f) for f in files]


def batch_convert(directory, template_path=None, recursive=True, enable_pan_automation=False, enable_envelope=False, enable_sample_offset=False):
    """Convertit tous les fichiers tracker d'un répertoire

    Args:
        directory: Répertoire contenant les fichiers à convertir
        template_path: Chemin vers le template Ableton (optionnel)
        recursive: Si True, recherche dans les sous-répertoires
        enable_pan_automation: Activer les automations de panning (effet 8xx)
        enable_envelope: Activer la conversion des enveloppes (expérimental)
    """

    print("="*70)
    print("BATCH CONVERSION XM/MOD → ABLETON LIVE")
    print("="*70)

    # Normaliser le chemin du répertoire racine
    root_directory = os.path.abspath(directory)

    # Trouver le template
    if not template_path:
        # Chercher le template dans plusieurs endroits
        script_dir = os.path.dirname(os.path.abspath(__file__))
        template_next_to_script = os.path.join(script_dir, "template_100_tracks.als")
        current_dir_template = "./template_100_tracks.als"

        if os.path.exists(template_next_to_script):
            template_path = template_next_to_script
        elif os.path.exists(current_dir_template):
            template_path = current_dir_template
        else:
            print("❌ ERROR: Template not found!")
            print(f"   Searched in:")
            print(f"   - {template_next_to_script}")
            print(f"   - {current_dir_template}")
            print(f"\n💡 Specify template path with --template")
            return False

    if not os.path.exists(template_path):
        print(f"❌ ERREUR: Template '{template_path}' does not exist!")
        return False

    print(f"\n📋 Template: {os.path.basename(template_path)}\n")

    # Trouver tous les fichiers
    files = find_tracker_files(directory, recursive)

    if not files:
        print(f"\n⚠️  No .xm or .mod files found in '{directory}'")
        return False

    print(f"\n✓ {len(files)} file(s) found\n")

    # Statistiques
    success_count = 0
    error_count = 0
    skipped_count = 0
    errors = []
    skipped_files = []  # Pour collecter les fichiers ignorés

    start_time = time.time()

    # Create centralized conversions directory
    conversions_root = os.path.join(root_directory, "xm2live_converted_tracks")
    os.makedirs(conversions_root, exist_ok=True)

    # Convertir chaque fichier
    for i, file_path in enumerate(files, 1):
        file_name = os.path.basename(file_path)
        file_ext = os.path.splitext(file_path)[1].lower()

        print(f"\n{'='*70}")
        print(f"[{i}/{len(files)}] {file_name}")
        print(f"{'='*70}")

        # Calculer le chemin relatif du fichier par rapport au répertoire racine
        file_abs_path = os.path.abspath(file_path)
        file_dir = os.path.dirname(file_abs_path)

        # Chemin relatif du répertoire par rapport à la racine
        if file_dir == root_directory:
            # Fichier directement dans le répertoire racine
            relative_subdir = ""
        else:
            # Fichier dans un sous-répertoire
            relative_subdir = os.path.relpath(file_dir, root_directory)

        # Construire le chemin de destination
        file_basename = os.path.splitext(file_name)[0]

        if relative_subdir:
            # File in subdirectory: /xm2live_converted_tracks/subdir/file_Ableton_Project
            project_dir = os.path.join(conversions_root, relative_subdir, f"{file_basename}_Ableton_Project")
        else:
            # File at root: /xm2live_converted_tracks/file_Ableton_Project
            project_dir = os.path.join(conversions_root, f"{file_basename}_Ableton_Project")

        als_file = os.path.join(project_dir, f"{file_basename}.als")

        if os.path.exists(als_file):
            print(f"⏭️  File already converted (use --force to reconvert)")
            skipped_count += 1
            skipped_files.append((file_path, als_file))
            continue

        try:
            # Convertir en spécifiant le répertoire de destination
            success = convert_xm_to_ableton(file_path, template_path, output_dir=project_dir,
                                           enable_pan_automation=enable_pan_automation,
                                           enable_envelope=enable_envelope,
                                           enable_sample_offset=enable_sample_offset)

            if success:
                success_count += 1
                print(f"\n✅ [{i}/{len(files)}] Conversion successful!")
            else:
                error_count += 1
                errors.append((file_name, "Conversion échouée"))
                print(f"\n❌ [{i}/{len(files)}] Conversion failed")

        except KeyboardInterrupt:
            print(f"\n\n⚠️  Interruption par l'utilisateur (Ctrl+C)")
            print(f"\n📊 Conversions interrompues:")
            print(f"   ✅ Successful: {success_count}")
            print(f"   ❌ Failed: {error_count}")
            print(f"   ⏭️  Skipped: {skipped_count}")
            print(f"   ⏸️  Restantes: {len(files) - i}")
            return False

        except Exception as e:
            error_count += 1
            error_msg = str(e)
            errors.append((file_name, error_msg))
            print(f"\n❌ [{i}/{len(files)}] ERREUR: {error_msg}")
            print(f"   Passage au fichier suivant...")

    # Temps total
    elapsed_time = time.time() - start_time
    minutes = int(elapsed_time // 60)
    seconds = int(elapsed_time % 60)

    # Résumé final
    print(f"\n\n{'='*70}")
    print("📊 BATCH CONVERSION SUMMARY")
    print(f"{'='*70}")
    print(f"Files processed: {len(files)}")
    print(f"  ✅ Successful conversions: {success_count}")
    if skipped_count > 0:
        print(f"  ⏭️  Files skipped (already converted): {skipped_count}")
    if error_count > 0:
        print(f"  ❌ Failed: {error_count}")
    print(f"\nTemps total: {minutes}m {seconds}s")

    # Afficher les erreurs détaillées
    if errors:
        print(f"\n{'='*70}")
        print("❌ ERROR DETAILS")
        print(f"{'='*70}")
        for file_name, error_msg in errors:
            print(f"\n• {file_name}")
            print(f"  → {error_msg}")

    # Afficher les fichiers ignorés (déjà convertis)
    if skipped_files:
        print(f"\n{'='*70}")
        print("⏭️  SKIPPED FILES (already converted)")
        print(f"{'='*70}")
        for source_path, als_path in skipped_files:
            print(f"\n• {source_path}")
            print(f"  → {als_path}")

    print(f"\n{'='*70}")

    if success_count > 0:
        print(f"✅ {success_count} file(s) converted successfully!")
        print(f"\n💡 Projects are in 'xm2live_converted_tracks' subdirectory")

    return error_count == 0


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Convert all .xm and .mod files from a directory to Ableton Live projects',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Convert all files from current directory
  xm2live-batch .

  # Convert all files from a specific directory
  xm2live-batch "/path/to/modules"

  # Convert without recursive search (current directory only)
  xm2live-batch "/path/to/modules" --no-recursive

  # Specify a custom template
  xm2live-batch "/path/to/modules" --template "/path/to/template.als"
        """
    )

    parser.add_argument(
        'directory',
        help='Directory containing .xm and .mod files to convert'
    )

    parser.add_argument(
        '--template', '-t',
        help='Path to Ableton Live template (.als)',
        default=None
    )

    parser.add_argument(
        '--no-recursive', '-n',
        action='store_true',
        help='Do not search in subdirectories'
    )

    parser.add_argument(
        '--force', '-f',
        action='store_true',
        help='Reconvert even if file already exists (not implemented)'
    )

    parser.add_argument(
        '--pan-automation',
        action='store_true',
        help='Enable panning automations (effect 8xx). Disabled by default.'
    )

    parser.add_argument(
        '--envelope',
        action='store_true',
        help='Enable FT2 volume envelope → ADSR conversion. Disabled by default.'
    )

    parser.add_argument(
        '--sample-offset',
        action='store_true',
        help='Enable effect 9xx (Sample Offset) with Simpler. LIMITATION: ping-pong → forward.'
    )

    args = parser.parse_args()

    # Convertir
    success = batch_convert(
        args.directory,
        template_path=args.template,
        recursive=not args.no_recursive,
        enable_pan_automation=args.pan_automation,
        enable_envelope=args.envelope,
        enable_sample_offset=args.sample_offset
    )

    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
