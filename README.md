# XM2Live

Convert **FastTracker 2 XM** and **Amiga ProTracker MOD** files to **Ableton Live** projects.

Bring your 1990s tracker music into the modern DAW era while preserving the original compositions.

**[Try it online](https://xm2live-web.onrender.com)** - No installation required!

## Features

- **Supported formats:** FastTracker 2 XM (.xm), ProTracker MOD (.mod)
- **Accurate conversion:**
  - Byte-perfect sample extraction (16-bit WAV)
  - Correct BPM calculation (`BPM × 6/speed`)
  - Proper MOD pitch mapping (+24 semitones offset)
  - Loop support (forward and ping-pong)
  - Per-sample volume and panning
- **Smart track organization:**
  - One MIDI track per channel/instrument combination
  - Automatic track grouping by instrument
  - Color-coded tracks
- **Optional effects:**
  - Panning automation (effect 8xx)
  - Sample offset (effect 9xx) via Simpler
  - Volume envelope conversion (experimental)
  - Merge mode for polyphonic playback

## Installation

```bash
pip install xm2live
```

Or install from source:

```bash
git clone https://github.com/samkieffer/xm2live.git
cd xm2live
pip install -e .
```

## Quick Start

### Single File Conversion

```bash
# Basic conversion
xm2live mytrack.xm
xm2live oldschool.mod

# With panning automation (effect 8xx)
xm2live mytrack.xm --pan-automation

# With sample offset support (effect 9xx)
xm2live mytrack.xm --sample-offset

# Merge mode (one track per instrument instead of per channel)
xm2live mytrack.xm --merge-tracks
```

### Batch Conversion

```bash
# Convert all files in a directory (recursive)
xm2live-batch /path/to/modules

# Non-recursive (current directory only)
xm2live-batch /path/to/modules --no-recursive
```

## Output

Converted projects are created in:
```
[source directory]/xm2live_converted_tracks/[name]_Ableton_Project/
├── [name].als          # Ableton Live project
└── Samples/            # Extracted WAV samples (16-bit)
```

## Command Line Options

### xm2live

| Option | Description |
|--------|-------------|
| `--pan-automation` | Enable panning automations (effect 8xx) |
| `--sample-offset` | Enable Sample Offset (effect 9xx) via Simpler |
| `--envelope` | Enable FT2 envelope → ADSR conversion (experimental) |
| `--merge-tracks` | Create one "All notes" track per instrument |

### xm2live-batch

| Option | Description |
|--------|-------------|
| `--template PATH` | Use custom Ableton template |
| `--no-recursive` | Don't search subdirectories |
| `--pan-automation` | Enable panning automations |
| `--sample-offset` | Enable Sample Offset via Simpler |
| `--envelope` | Enable envelope conversion |

## Technical Details

### BPM Calculation

FastTracker 2 uses a dual tempo system:
- **Speed:** Ticks per row (default: 6)
- **BPM:** Base tempo (calibrated for Speed=6)

Real BPM formula: `real_bpm = bpm × (6 / speed)`

Example: Speed=3, BPM=125 → Real BPM = 250

### MOD Pitch Mapping

ProTracker uses different octave conventions than MIDI. The converter applies a +24 semitone offset:
- C-1 (ProTracker) → C-3 (MIDI note 48)
- C-2 (ProTracker) → C-4 (MIDI note 60)
- C-3 (ProTracker) → C-5 (MIDI note 72)

### Sample Offset (Effect 9xx)

When enabled with `--sample-offset`:
- Instruments using effect 9xx are loaded into **Simpler** (instead of Sampler)
- Sample Start automation is created for each note with 9xx
- **Limitation:** Ping-pong loops are converted to forward loops (Simpler limitation)

## Known Limitations

- **Effects:** Most tracker effects (vibrato, arpeggio, etc.) are not converted
- **Volume envelopes:** Approximation only (FT2 multi-point → Ableton ADSR)
- **Dynamic BPM:** Only initial tempo is used
- **Formats:** Only XM and MOD supported (no S3M, IT)

## Requirements

- Python 3.8+
- Ableton Live 10+ (to open converted projects)

## License

MIT License - See [LICENSE](LICENSE) for details.

## Contributing

Contributions are welcome! Please feel free to submit issues and pull requests.

## Acknowledgments

This project was created to preserve and rework tracker music from the 1990s demoscene era.
Special thanks to the FastTracker 2 and ProTracker communities.
