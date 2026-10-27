# AGENTS.md

Guidance for AI agents (and humans) working in this repository.

## Project

**transcribe** — a simple Linux GUI app: drag an audio file into a window, get a
transcription via a local Whisper model (whisper.cpp, Vulkan build —
GPU-accelerated on the user's AMD Radeon). Results are shown in the GUI and
written to a sidecar `.txt` next to the audio file.

- Language: Python 3 (PySide6 for GUI), whisper.cpp `whisper-cli` subprocess
  for transcription
- Platform: Linux only
- Primary languages to support: **Swedish and English** (language auto-detect on)
- Hardware target: **AMD GPU via Vulkan** (Mesa/RADV; Navi 24, 4 GB VRAM).
  whisper.cpp falls back to CPU automatically if Vulkan fails. Model:
  `ggml-large-v3-q5_0`, pinned in `flake.nix` and exposed as
  `$TRANSCRIBE_MODEL`.

## Environment

All development happens inside the Nix devshell:

```sh
nix develop          # enter the shell
python -m transcribe                 # launch the GUI
python -m transcribe file.mp3        # GUI + preloaded file
python -m transcribe --no-gui file.mp3   # print transcription to stdout (for testing)
python -m transcribe --no-gui --no-gpu file.mp3   # CPU-only (skip Vulkan)
```

GPU selection is automatic (first discrete Vulkan GPU; the display iGPU is
avoided on hybrid laptops). Override with `--device N` or
`TRANSCRIBE_DEVICE=N`. `--no-flash-attn` is a debugging escape hatch.

- The Whisper model comes from the Nix store (`$TRANSCRIBE_MODEL`) — there are
  **no runtime downloads**.
- whisper.cpp decodes wav/mp3/flac/ogg natively; for other formats the
  transcriber converts to 16 kHz mono WAV via ffmpeg first (temp file, cleaned
  up afterwards).
- Do not add other package managers (pip, uv, poetry). Everything comes from
  `flake.nix`. If a new Python dependency is needed, add it to
  `pkgs.python3.withPackages` in `flake.nix` **after verifying it exists in
  nixpkgs** (`nix eval --raw nixpkgs#python3Packages.<name>.version`).

## Layout

```
flake.nix            devshell (Python, PySide6, whisper-cpp-vulkan, ffmpeg,
                     pinned ggml-large-v3-q5_0 model)
transcribe/
  __init__.py
  __main__.py        entry point, argument parsing
  app.py             Qt widgets: main window, drag-and-drop, result view
  worker.py          QThread worker bridging GUI <-> transcription
  transcriber.py     whisper.cpp subprocess wrapper (segment streaming,
                     ffmpeg fallback, errors, audio duration)
  cache.py           sidecar .txt cache read/write
ARCHITECTURE.md      design decisions and data flow
PLAN.md              roadmap with phases
```

## Rules

1. **Never block the GUI thread.** Transcription runs in a worker thread that
   spawns and streams a `whisper-cli` subprocess; communicate with the UI only
   via Qt signals.
2. **Cache-first.** Before transcribing, check for an existing sidecar
   `<audiofile>.txt` and load it instead of re-transcribing. When writing
   results, write atomically (temp file + `os.replace`) so a crash never
   corrupts a transcript.
3. **One transcription at a time.** Queue additional drops; never run two
   `whisper-cli` processes concurrently (VRAM + fairness). Do not reload the
   model per file beyond what a fresh subprocess entails.
4. **Subprocess hygiene.** Always clean up temp WAV files (try/finally), and
   capture stderr for error reporting; a non-zero exit must surface as a
   readable message, never a raw traceback.
5. Keep v1 minimal. No settings dialogs, no playlists, no cloud APIs. Features
   beyond the spec in `PLAN.md` must be added to the plan first.
6. Keep the non-GUI path working: `--no-gui <file>` must always produce a
   transcript on stdout — it is how agents verify transcription without a
   display. If GPU/Vulkan is suspect, use `--no-gui --no-gpu` (CPU only).
7. Error handling: unreadable/unsupported audio files produce a user-readable
   message in the GUI (and exit code != 0 in `--no-gui` mode), never a raw
   traceback.
8. Swedish comments/docstrings are fine only if the whole file is consistent;
   otherwise use English.
9. **Commit as you go.** Use git for every change: one commit per validated
   phase or feature, with a descriptive message. Check `git status` before
   reporting a change as done; scratch/diagnostic files stay untracked.

## Definition of done (per change)

- `python -m transcribe --no-gui <file>` works for a fresh file and a
  previously transcribed file (cache hit).
- GUI starts, accepts a dragged file, shows progress, shows the transcript.
- `nix flake check` (if added) or at minimum `nix develop -c true` evaluates
  without errors.
- `git status` is clean and the change is committed.
