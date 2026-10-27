# Plan

Phased roadmap. Keep each phase small and shippable; check items off as done.

**Stack (revised after evaluating AMD GPU options):** PySide6 GUI +
whisper.cpp (`whisper-cpp-vulkan`) subprocess, model `ggml-large-v3-q5_0`
pinned in the flake. GPU-accelerated on the user's Radeon RX 6400/6500 XT
(Navi 24, 4 GB) via Vulkan/RADV. faster-whisper was rejected: its engine
(CTranslate2) has no ROCm support and silently falls back to CPU on AMD.

## Phase 1 — Scaffold & environment ✅

- [x] `flake.nix` with devshell: Python 3 + PySide6, `whisper-cpp-vulkan`,
      ffmpeg, fontconfig/freetype
- [x] Model `ggml-large-v3-q5_0.bin` pinned via `fetchurl`; exposed to the app
      as `$TRANSCRIBE_MODEL` in the devshell
- [x] `AGENTS.md`, `ARCHITECTURE.md`, `PLAN.md`, `.gitignore`
- [x] Verify `nix develop`: PySide6 QApplication initializes (offscreen),
      faster-whisper smoke test superseded by `whisper-cli --help`

## Phase 2 — Transcription core (no GUI)

- [x] `transcribe/transcriber.py`: `Transcriber` class
      - `transcribe(path)` → full text; `iterate(path)` → yields segments
        `(start, end, text)` parsed from `whisper-cli` stdout
      - ffmpeg fallback conversion to 16 kHz mono WAV for formats whisper.cpp
        can't decode natively (not wav/mp3/flac/ogg); temp file cleanup
      - audio duration via `ffprobe` for progress estimation
      - readable errors from non-zero exit codes
- [x] `transcribe/cache.py`: `load(path)` / `save(path, text)` with atomic
      write (temp file + `os.replace`), sidecar = `<audio path>.txt`
- [x] `transcribe/__main__.py`: `--no-gui <file>` mode — cache check →
      transcribe → print; readable errors, exit codes
- [x] Safety escape hatches: `--no-gpu` (`-ng`, also `TRANSCRIBE_NO_GPU=1`)
      and `--no-flash-attn` (`-nfa`); the CLI logs the chosen flags and the
      backend actually used (`transcribe: backend: …`)
- [x] Verify GPU is actually used: check whisper-cli output mentions the
      Vulkan backend; compare timing vs CPU if it doesn't.
      — **confirmed on the RX 6500 (Vulkan GPU1) after the app started
      auto-selecting the discrete GPU. The original freeze was whisper.cpp's
      default `-dev 0` loading large-v3 onto the 512 MB display iGPU.**
- [x] Manual test: short English and Swedish clips; confirm `.txt` sidecar is
      written and reused on second run
      (EN q5_0 output correct; synthetic espeak Swedish clip auto-detected as
      EN, but `-l sv` decodes it perfectly — real speech should auto-detect
      fine; sidecar written and reused for both)

## Phase 3 — GUI

- [x] `transcribe/app.py`: `MainWindow`
      - drag-and-drop acceptance (`.mp3 .wav .m4a .ogg .flac .opus .webm`)
      - read-only transcript view, status label, progress bar
      - Copy to clipboard button; "transcribing…" disabled state
- [x] `transcribe/worker.py`: `TranscriptionWorker(QThread)` with signals
      `progress(float)`, `segment(str)`, `finished(str)`, `error(str)`
- [x] Cache hit path in the GUI: show cached text instantly, no worker
- [x] Queue drops that arrive while a transcription is running
- [x] Validate headlessly (`QT_QPA_PLATFORM=offscreen`): cache load, fresh
      streaming transcription, FIFO queue behind a running job, error path,
      MIME path extraction, clipboard, screenshot/layout. Real drag-and-drop
      from a file manager still needs a desktop smoke test (no display in the
      sandbox).

## Phase 4 — Polish

- [x] Error dialogs for unreadable/unsupported files (one warning per drop
      batch with a details list; a critical dialog with expandable details for
      per-file transcription failures, plus the status label/tooltip)
- [x] Segment timestamps preserved internally (ready for future .srt export)
      — `TranscriptionWorker.segments` and `MainWindow.last_segments`
- [x] Sensible window title / app name ("Transcribe"); remember last window
      size (`QSettings`, clamped to the available screen)
- [x] CLI: `transcribe <file>` opens the GUI with that file preloaded
      (wired in Phase 3; verified through the GUI option/preload test)
- [x] Model note in status label on first run ("first transcription may take
      longer while the model loads"; stored via `QSettings`, not repeated, and
      not consumed by cache hits)
- [x] Validated headlessly (`QT_QPA_PLATFORM=offscreen`): dialogs, timestamp
      retention, size persistence, first-run note. Real desktop check of the
      dialogs/size is still worth a quick look.

## Phase 5 — Packaging (optional, later)

- [ ] Nix derivation / `apps.<system>.default` so it runs outside the devshell
- [ ] `.desktop` file + file-manager "Open with" integration
- [ ] Consider HIP build of whisper.cpp (rocmPackages) if Vulkan throughput is
      disappointing on Navi 24 — expensive to build, ~20–30% faster at most
- [ ] Model selector (medium/small quantized) if 4 GB VRAM shows pressure or
      speed matters more than quality

## Open questions / risks

- **Fixed root cause of the first GPU freeze (hybrid graphics).** The machine
  has an integrated Radeon (Vulkan GPU0, 512 MB, drives the display) and a
  discrete RX 6500 (GPU1, 4 GB, idle). `whisper-cli` defaults to `-dev 0`, so
  the first run loaded large-v3 onto the display iGPU and wedged it. The app
  now auto-selects the first discrete GPU (`vulkaninfo --summary`); override
  with `--device N` or `TRANSCRIBE_DEVICE=N`. Still to do: validate on a short
  clip with `-dev 1` and `--no-flash-attn` before trusting long runs.
- Residual Navi 24 risk: there is a documented GFXOFF/ring-timeout reset bug
  that can turn a workload timeout into a black screen. If device 1 still
  misbehaves, candidates are `--no-flash-attn`, disabling GFXOFF
  (`amdgpu.ppfeaturemask=0xfff73fff`), a raised compute lockup timeout, or a
  smaller model (`ggml-medium-q5_0`). `scripts/diagnose-gpu.sh` collects the
  evidence (reading `amdgpu_gfxoff` is timeout-bounded now).
- Vulkan on Navi 24 via RADV: expected to work out of the box; whisper.cpp
  falls back to CPU silently, so Phase 2 includes an explicit backend check in
  the output.
- 4 GB VRAM with large-v3-q5_0 (~1.6 GB total incl. buffers): comfortable.
  If a future model or longer contexts cause pressure, `ggml-medium-q5_0`
  (~540 MB) is the fallback.
- whisper-cli model load per file (~1–2 s from page cache): acceptable for
  interactive use; server mode listed as a future extension.
