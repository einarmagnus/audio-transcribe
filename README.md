# transcribe

Local, GPU-accelerated audio transcription for Linux. Drop an audio file onto
the window; whisper.cpp transcribes it with a pinned `ggml-large-v3-q5_0`
model and writes `<audio>.txt` next to the source file.

## Features

- Swedish and English speech; Whisper auto-detects the language
- Vulkan/RADV GPU acceleration on AMD (auto-selects the discrete GPU), with a
  CPU fallback (`--no-gpu`)
- Streaming transcript, progress bar, copy button, drag-and-drop, FIFO queue
- Cache: an existing `<audio>.txt` is shown instead of re-transcribing
- No runtime downloads — the model is pinned in the Nix store
- Desktop entry + icon; "Open with" for common audio formats

## Requirements

- Linux with Nix flakes enabled
- AMD Vulkan (Mesa/RADV) for GPU acceleration; CPU works without it
- Roughly 1.2 GB of disk for the model plus the Python/Qt closure

## Development

```sh
nix develop

python -m transcribe                       # GUI
python -m transcribe file.mp3              # GUI with a file preloaded
python -m transcribe --no-gui file.mp3     # transcript to stdout
python -m transcribe --no-gui --no-gpu file.mp3   # force CPU
```

Useful flags: `--device N` (Vulkan device index), `--threads N` (CPU),
`--no-flash-attn` (Vulkan debug escape hatch). The GUI auto-selects the first
discrete GPU; on hybrid laptops this avoids loading the model onto the small
display iGPU.

## Install system-wide (NixOS flake)

Add this repository as a flake input:

```nix
inputs.transcribe.url = "path:/home/einar/projects/transcribe";
inputs.transcribe.inputs.nixpkgs.follows = "nixpkgs";
```

The `follows` line reuses your system's nixpkgs instead of pulling a second
copy into the closure.

Then either enable the module:

```nix
imports = [ inputs.transcribe.nixosModules.default ];
programs.transcribe.enable = true;
```

or add the package manually:

```nix
environment.systemPackages = [
  inputs.transcribe.packages.${pkgs.stdenv.hostPlatform.system}.default
];
```

Rebuild the system, then launch **Transcribe** from the application menu or
choose it via “Open with” for an audio file. `nix run .` also works directly
from this repository without installing.

## Output and cache

Transcripts are written atomically to `<audio>.<ext>.txt` next to the source
file and reused on the next run. Delete the sidecar to force a fresh
transcription.

## Optional: default audio handler

Installing the package makes it available under “Open with”. To make it the
default for some formats:

```nix
xdg.mime.defaultApplications = {
  "audio/mpeg" = "transcribe.desktop";
  "audio/x-wav" = "transcribe.desktop";
  "audio/mp4" = "transcribe.desktop";
  "audio/ogg" = "transcribe.desktop";
  "audio/flac" = "transcribe.desktop";
};
```
