# Architecture

## Overview

A single-window Linux desktop app. The user drags an audio file onto the
window; a worker thread runs `whisper-cli` (whisper.cpp, Vulkan build) as a
subprocess — GPU-accelerated on the user's AMD Radeon RX 6400/6500 XT via
Mesa/RADV — and the transcription streams into the GUI and into a sidecar
`.txt` next to the audio file.

```
┌──────────────────────────── transcribe (process) ──────────────────────────────┐
│                                                                                │
│  ┌───────────────────── GUI thread ─────────────────────┐                      │
│  │  MainWindow (app.py)                                 │                      │
│  │   • accepts file drag-and-drop (Qt mime events)      │                      │
│  │   • progress bar / status label                      │                      │
│  │   • transcript view (read-only text edit)            │                      │
│  │   • Copy to clipboard button                         │                      │
│  └───────────▲──────────────────────────┬───────────────┘                      │
│              │ Qt signals               │ start worker                         │
│  ┌───────────┴─────────────┐  ┌─────────▼───────────────────────────┐          │
│  │ TranscriptionWorker     │  │ non-GUI path:                       │          │
│  │ (worker.py, QThread)    │  │ __main__.py --no-gui                │          │
│  │  emits: progress(float) │  │  calls transcriber directly,        │          │
│  │  emits: segment(text)   │  │  prints transcript to stdout        │          │
│  │  emits: finished(text)  │  └─────────┬──────────────────────────┘          │
│  │  emits: error(str)      │            │                                     │
│  └───────────┬─────────────┘            │                                     │
│              │ spawns                   │                                     │
│  ┌───────────▼──────────────────────────▼───────────┐                          │
│  │ transcriber.py — Transcriber                     │                          │
│  │  • ffmpeg → temp 16 kHz WAV (only if the input   │                          │
│  │    format isn't natively readable: wav/mp3/flac/ │                          │
│  │    ogg are decoded by whisper.cpp itself)        │                          │
│  │  • spawns whisper-cli subprocess                 │                          │
│  │  • streams stdout: one segment per line          │                          │
│  │  • progress = last segment timestamp / duration  │                          │
│  └───────────┬──────────────────────────────────────┘                          │
│              │                                                                 │
│  ┌───────────▼───────────┐     ┌──────────────────────────────────────┐        │
│  │ cache.py              │     │ whisper-cli (whisper-cpp-vulkan)     │        │
│  │  sidecar:             │     │  • model: ggml-large-v3-q5_0.bin     │        │
│  │  <audio>.txt          │     │    pinned via fetchurl in flake.nix  │        │
│  │  read / atomic write  │     │  • GPU: Vulkan → RADV (Navi 24)      │        │
│  └───────────────────────┘     │  • auto CPU fallback if Vulkan fails │        │
│                                └──────────────────────────────────────┘        │
└────────────────────────────────────────────────────────────────────────────────┘
```

## Key decisions

| Decision | Choice | Rationale |
|---|---|---|
| Transcription engine | **whisper.cpp 1.9.5, Vulkan build** (`pkgs.whisper-cpp-vulkan`) | GPU-accelerated on AMD without ROCm: RADV Vulkan driver ships with Mesa. CTranslate2 (faster-whisper) has no ROCm backend and silently falls back to CPU. Prebuilt in nixpkgs — no custom cmake/HIP builds. |
| Hardware | **AMD Navi 24 (RX 6400/6500 XT, 4 GB VRAM) via Vulkan** | whisper.cpp initializes Vulkan automatically; if it fails it falls back to CPU rather than erroring. |
| Model | **`ggml-large-v3-q5_0.bin` (~1.1 GB)**, pinned via `fetchurl` in `flake.nix` | Best Whisper quality for Swedish at Q5_0 quantization (negligible loss vs fp16). 1.1 GB + ~0.5 GB compute buffers fits in 4 GB VRAM. Pinned in the Nix store → zero downloads, reproducible. |
| Integration | **`whisper-cli` subprocess per file** | Simpler than managing a server; natural process isolation (a bad file can't crash the GUI); stdout streams segments live so the GUI shows text as it's produced. No in-model state to manage. |
| Audio decoding | whisper.cpp native (wav/mp3/flac/ogg); **ffmpeg → temp 16 kHz WAV** only for other formats (m4a/aac/opus/webm…) | Avoids an unnecessary conversion step for the common cases; ffmpeg (also in the devshell) covers everything else. |
| Language | **auto-detect** (`-l auto`) | User transcribes mostly Swedish/English; Whisper detects these reliably. |
| GPU safety | **`--no-gpu`** (`-ng`, `TRANSCRIBE_NO_GPU=1`) and **`--no-flash-attn`** (`-nfa`); backend is logged after each run | Navi 24/RADV has hung under Vulkan compute (documented GFXOFF/reset bug). A CPU fallback must be selectable without code changes or a GUI. |
| GPU device selection | **Auto-pick the first discrete GPU** via `vulkaninfo --summary`; override with `--device N` / `TRANSCRIBE_DEVICE=N` | Hybrid laptops enumerate the display iGPU first, and whisper.cpp defaults to `-dev 0`. Loading large-v3 into the 512 MB iGPU wedged the display (black screen, hard reset). The RX 6500 (4 GB) is GPU1 on this machine. |
| Output | GUI text + sidecar `<audio>.txt` | Zero-config persistence; the file lives next to the source audio. |
| Cache | Sidecar file, checked before transcribing | "Already transcribed" = cache hit; transcript loaded instead of re-running the model. Writes are atomic (temp + `os.replace`). |
| GUI toolkit | **PySide6 (Qt)** | Native drag-and-drop, QThread for background work, packaged in nixpkgs. |
| UI state | **QSettings** for window size and the one-time model-load note; no settings dialog | Minimal persistence without building a settings UI. |
| Always-on-top | **Not implemented** — left to the window manager | Native Wayland has no client protocol for keep-above (KWin/Mutter ignore the Qt hint); compositor window rules or extensions are the right layer. |
| Dependencies | Everything from nixpkgs via `flake.nix` | Reproducible on NixOS; no pip/venv layering. |

## Data flow (GUI, one file)

1. **Drop** — Qt delivers a `file://` URL; resolve to a local path.
2. **Cache check** — `cache.load(path)`: if `<path>.txt` exists, show its
   content immediately, status "loaded from cache". Done.
3. **Worker start** — `TranscriptionWorker(path)` is started on a `QThread`.
   UI shows the status ("loading audio…").
4. **Prepare audio** — if the file extension is one whisper.cpp can't decode
   natively, `ffmpeg -i <in> -ar 16000 -ac 1 <tmp>.wav`; the temp file is
   deleted when the run finishes (success or error).
5. **Transcribe** — spawn `whisper-cli -m $TRANSCRIBE_MODEL -f <audio> -l auto`
   (plus `-dev N` for the selected discrete GPU, `-ng` when GPU is disabled,
   `-nfa` when flash attention is off). Each `[… --> …] text` line on stdout
   is parsed into a segment, emitted to the GUI immediately, and progress is
   estimated from the segment's end timestamp vs the audio duration (via
   `ffprobe`).
6. **Finish** — worker emits the full text; UI displays it and calls
   `cache.save(path, text)` (atomic write). The worker also keeps the
   timestamped segments (`segments`); the window stores them in
   `last_segments` for a future `.srt`/`.vtt` export.
7. **Error** — worker emits a readable message (non-zero exit + stderr tail);
   UI shows it in a message box with expandable details and in the status
   label/tooltip.

## Threading rules

- One transcription at a time. Drops that arrive while busy are queued (simple
  FIFO list) rather than rejected or dropped.
- The subprocess is read incrementally (line-buffered) so segments stream;
  `QThread` + signals carry data to the GUI thread, which owns all UI state.
- Cancellation: closing the window cancels the running `whisper-cli`
  (`Transcriber.cancel()`); cancelling an in-progress ffmpeg conversion is
  still future work.

## Future extensions (out of scope for v1)

- `.srt`/`.vtt` export from segment timestamps (kept in
  `TranscriptionWorker.segments` / `MainWindow.last_segments`).
- Model selector (medium/small quantized) for speed-vs-quality choice.
- Long-running `whisper-server` mode (localhost HTTP) to keep weights resident
  in VRAM — relevant only if per-file model-load latency (a couple of seconds
  from page cache) ever becomes noticeable.
- Mistral Voxtral as a pluggable remote backend (behind the same
  `Transcriber` interface).
- Packaging as a Nix derivation / `.desktop` entry so the app can be launched
  from a file manager.
