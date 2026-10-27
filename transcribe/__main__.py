"""Command-line entry point: GUI by default, ``--no-gui`` for stdout mode."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import cache, __version__
from .transcriber import (
    Transcriber,
    TranscriptionError,
    TranscriptionOptions,
    segments_to_text,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcribe",
        description="Transcribe an audio file with whisper.cpp (Swedish/English).",
    )
    parser.add_argument(
        "file",
        nargs="?",
        metavar="AUDIO",
        help="audio file to transcribe (the GUI can also accept drag-and-drop)",
    )
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="transcribe on stdout instead of opening the GUI",
    )
    parser.add_argument(
        "--no-gpu",
        action="store_true",
        help="force CPU inference (whisper-cli -ng); use if Vulkan hangs the GPU",
    )
    parser.add_argument(
        "--no-flash-attn",
        action="store_true",
        help="disable flash attention (whisper-cli -nfa), a known GPU-crash source",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        metavar="N",
        help="CPU threads for whisper-cli (default: all cores in --no-gpu mode)",
    )
    parser.add_argument(
        "--device",
        type=int,
        default=None,
        metavar="N",
        help="Vulkan device index (default: auto-pick a discrete GPU)",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def _options_from_args(args: argparse.Namespace) -> TranscriptionOptions:
    use_gpu = not args.no_gpu and os.environ.get("TRANSCRIBE_NO_GPU") != "1"
    flash_attn = (
        not args.no_flash_attn
        and os.environ.get("TRANSCRIBE_NO_FLASH_ATTN") != "1"
    )
    threads = args.threads
    if threads is None and not use_gpu:
        threads = os.cpu_count()
    device = args.device
    if device is None:
        raw_device = os.environ.get("TRANSCRIBE_DEVICE", "").strip()
        if raw_device:
            try:
                device = int(raw_device)
            except ValueError:
                print(
                    "transcribe: warning: ignoring invalid "
                    f"TRANSCRIBE_DEVICE={raw_device!r}",
                    file=sys.stderr,
                )
    return TranscriptionOptions(
        use_gpu=use_gpu,
        flash_attn=flash_attn,
        threads=threads,
        device=device,
    )


def _run_cli(path: Path, options: TranscriptionOptions) -> int:
    """Cache check → transcribe → stream text to stdout. Returns an exit code."""
    if not path.exists():
        print(f"transcribe: file not found: {path}", file=sys.stderr)
        return 2
    if not path.is_file():
        print(f"transcribe: not a file: {path}", file=sys.stderr)
        return 2

    cached = cache.load(path)
    if cached is not None:
        print(
            f"transcribe: loaded cached transcript {cache.sidecar_path(path)}",
            file=sys.stderr,
        )
        sys.stdout.write(cached)
        if cached and not cached.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
        return 0

    transcriber = Transcriber(
        use_gpu=options.use_gpu,
        flash_attn=options.flash_attn,
        threads=options.threads,
        device=options.device,
    )
    transcriber.choose_device()
    if transcriber.use_gpu and transcriber.device is not None:
        device_label = f"{transcriber.device} ({transcriber.device_source})"
    elif transcriber.use_gpu:
        device_label = transcriber.device_source or "default"
    else:
        device_label = "-"
    print(
        "transcribe: starting whisper-cli "
        f"(gpu={'off' if not transcriber.use_gpu else 'on'}, "
        f"device={device_label}, "
        f"flash_attn={'on' if transcriber.flash_attn else 'off'}, "
        f"threads={transcriber.threads or 'default'})",
        file=sys.stderr,
        flush=True,
    )
    segments = []
    try:
        for segment in transcriber.iterate(path):
            if segment.text:
                print(segment.text, flush=True)
                segments.append(segment)
    except TranscriptionError as exc:
        print(f"transcribe: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("transcribe: interrupted", file=sys.stderr)
        return 130

    if transcriber.device_info:
        print(f"transcribe: backend: {transcriber.device_info}", file=sys.stderr)
    for line in transcriber.vulkan_devices:
        print(f"transcribe: {line}", file=sys.stderr)

    text = segments_to_text(segments)
    try:
        sidecar = cache.save(path, text)
        print(f"transcribe: saved transcript {sidecar}", file=sys.stderr)
    except OSError as exc:
        # Transcription succeeded; an unwritable sidecar is only a warning.
        print(f"transcribe: warning: could not save transcript: {exc}", file=sys.stderr)
    return 0


def _run_gui(path: Path | None, options: TranscriptionOptions) -> int:
    try:
        from .app import run
    except ImportError as exc:
        print(
            f"transcribe: GUI unavailable ({exc}); use --no-gui",
            file=sys.stderr,
        )
        return 2
    return run(path, options)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = Path(args.file).expanduser() if args.file else None
    options = _options_from_args(args)

    if args.no_gui:
        if path is None:
            print("transcribe: --no-gui requires an audio file", file=sys.stderr)
            return 2
        return _run_cli(path, options)

    if path is not None and not path.exists():
        print(f"transcribe: file not found: {path}", file=sys.stderr)
        return 2
    return _run_gui(path, options)


if __name__ == "__main__":
    raise SystemExit(main())
