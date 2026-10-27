"""whisper.cpp wrapper.

Runs ``whisper-cli`` as a subprocess, streams its timestamped segments and
reports progress-friendly metadata.  whisper.cpp decodes wav/mp3/flac/ogg
itself; anything else is converted to 16 kHz mono WAV with ffmpeg first.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

#: Formats whisper.cpp can decode without a conversion step.
NATIVE_EXTENSIONS = frozenset({".wav", ".mp3", ".flac", ".ogg"})

# [00:00:00.000 --> 00:00:02.960]   Some text
_SEGMENT_RE = re.compile(
    r"^\[(\d+):(\d{2}):(\d{2})[.,](\d{1,3})"
    r"\s*-->\s*"
    r"(\d+):(\d{2}):(\d{2})[.,](\d{1,3})\]"
    r"\s*(.*)$"
)

_DEVICE_RE = re.compile(r"whisper_backend_init_gpu: device \d+: ([^(]+?)\s*\(")


class TranscriptionError(RuntimeError):
    """A transcription failure with a message suitable for the user."""


@dataclass(frozen=True)
class Segment:
    """One timestamped chunk of transcript, in seconds."""

    start: float
    end: float
    text: str


def segments_to_text(segments: list[Segment]) -> str:
    """Join segments into the canonical transcript text (one line each)."""
    return "".join(segment.text + "\n" for segment in segments if segment.text)


@dataclass(frozen=True)
class TranscriptionOptions:
    """Settings shared by the CLI and the GUI worker."""

    use_gpu: bool = True
    flash_attn: bool = True
    threads: int | None = None
    device: int | None = None


def _timestamp(hours: str, minutes: str, seconds: str, fraction: str) -> float:
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(fraction) / (10 ** len(fraction))
    )


def parse_segment(line: str) -> Segment | None:
    """Parse a ``[start --> end] text`` line, or return ``None``."""
    match = _SEGMENT_RE.match(line.strip())
    if match is None:
        return None
    groups = match.groups()
    return Segment(
        start=_timestamp(*groups[0:4]),
        end=_timestamp(*groups[4:8]),
        text=groups[8].strip(),
    )


def audio_duration(path: str | os.PathLike[str]) -> float | None:
    """Return the audio duration in seconds via ffprobe, or ``None``."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def _tail(lines: deque[str], count: int) -> str:
    """Pick the most useful stderr lines for an error message."""
    interesting = [
        line
        for line in lines
        if re.search(r"error|failed|not found|invalid|unsupported", line, re.I)
    ]
    chosen = interesting[-count:] if interesting else [
        line for line in list(lines)[-count:] if line.strip()
    ]
    return "\n".join(chosen)


def detect_discrete_gpu(timeout: float = 20.0) -> int | None:
    """Return the Vulkan index of the first discrete GPU, if one exists.

    Hybrid laptops enumerate the integrated GPU first, and it is usually the
    one driving the display with very little dedicated VRAM.  whisper.cpp
    defaults to device 0, so loading a large model there can wedge the display
    GPU.  Prefer a discrete card when vulkaninfo can identify one.
    """
    vulkaninfo = shutil.which("vulkaninfo")
    if vulkaninfo is None:
        return None
    try:
        result = subprocess.run(
            [vulkaninfo, "--summary"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    current: int | None = None
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*GPU(\d+):\s*$", line)
        if match is not None:
            current = int(match.group(1))
            continue
        if current is not None and "PHYSICAL_DEVICE_TYPE_DISCRETE_GPU" in line:
            return current
    return None


class Transcriber:
    """Spawns ``whisper-cli`` and turns its output into segments.

    One instance may be reused; each call to :meth:`iterate` runs a fresh
    subprocess.  Only one run may be active at a time.
    """

    def __init__(
        self,
        model: str | os.PathLike[str] | None = None,
        whisper_cli: str | None = None,
        *,
        language: str = "auto",
        use_gpu: bool = True,
        flash_attn: bool = True,
        threads: int | None = None,
        device: int | None = None,
        stderr_tail: int = 40,
    ) -> None:
        self.model = str(model or os.environ.get("TRANSCRIBE_MODEL") or "")
        self.whisper_cli = whisper_cli or shutil.which("whisper-cli") or ""
        self.language = language
        self.use_gpu = use_gpu
        self.flash_attn = flash_attn
        self.threads = threads
        self.device = device
        self.device_source = "explicit" if device is not None else None
        self.vulkan_devices: list[str] = []
        self.stderr_tail = stderr_tail
        #: Backend reported by whisper.cpp for the last run ("CPU", "Vulkan0", …)
        self.device_info: str | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    # -- public API --------------------------------------------------------

    def check_environment(self) -> None:
        """Raise :class:`TranscriptionError` if the toolchain is unavailable."""
        if not self.whisper_cli or (
            os.path.isfile(self.whisper_cli) is False
            and shutil.which(self.whisper_cli) is None
        ):
            raise TranscriptionError(
                "whisper-cli not found — run the app inside `nix develop`"
            )
        if not self.model or not os.path.isfile(self.model):
            raise TranscriptionError(
                "Whisper model not found — set $TRANSCRIBE_MODEL "
                "(run the app inside `nix develop`)"
            )

    def duration(self, path: str | os.PathLike[str]) -> float | None:
        """Duration of *path* in seconds, when ffprobe can tell us."""
        return audio_duration(path)

    def choose_device(self) -> int | None:
        """Pick a Vulkan device index for whisper-cli (discrete GPU preferred)."""
        if not self.use_gpu:
            return None
        if self.device is None:
            detected = detect_discrete_gpu()
            if detected is not None:
                self.device = detected
                self.device_source = "auto-detected discrete GPU"
            else:
                self.device_source = "whisper-cli default"
        return self.device

    def iterate(self, path: str | os.PathLike[str]) -> Iterator[Segment]:
        """Yield transcript segments as whisper-cli produces them.

        Raises :class:`TranscriptionError` for missing files, conversion
        failures and non-zero whisper-cli exit codes.
        """
        audio_path = Path(path)
        if not audio_path.exists():
            raise TranscriptionError(f"File not found: {audio_path}")
        if not audio_path.is_file():
            raise TranscriptionError(f"Not a file: {audio_path}")
        self.check_environment()
        self.choose_device()
        with self._prepare_audio(audio_path) as prepared:
            yield from self._stream(prepared)

    def transcribe(self, path: str | os.PathLike[str]) -> str:
        """Transcribe *path* and return the complete text."""
        return segments_to_text(list(self.iterate(path)))

    def cancel(self) -> None:
        """Kill a running whisper-cli process, if any."""
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.kill()

    # -- internals ---------------------------------------------------------

    @contextmanager
    def _prepare_audio(self, path: Path) -> Iterator[Path]:
        """Yield a path whisper-cli can read, converting with ffmpeg if needed."""
        if path.suffix.lower() in NATIVE_EXTENSIONS:
            yield path
            return

        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise TranscriptionError(
                f"{path.suffix or 'This file type'} needs ffmpeg for decoding, "
                "but ffmpeg was not found — run the app inside `nix develop`"
            )

        fd, tmp_name = tempfile.mkstemp(prefix="transcribe-", suffix=".wav")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            result = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(path),
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-c:a",
                    "pcm_s16le",
                    str(tmp_path),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                detail = (result.stderr or "").strip().splitlines()
                tail = "\n".join(detail[-4:]) if detail else "unknown error"
                raise TranscriptionError(
                    f"Could not decode {path.name}:\n{tail}"
                )
            yield tmp_path
        finally:
            tmp_path.unlink(missing_ok=True)

    def _command(self, audio: Path) -> list[str]:
        command = [
            self.whisper_cli,
            "-m",
            self.model,
            "-f",
            str(audio),
            "-l",
            self.language,
        ]
        if not self.use_gpu:
            command.append("-ng")
        elif self.device is not None:
            command.extend(["-dev", str(self.device)])
        if not self.flash_attn:
            command.append("-nfa")
        if self.threads:
            command.extend(["-t", str(self.threads)])
        return command

    def _stream(self, audio: Path) -> Iterator[Segment]:
        self.device_info = None
        self.vulkan_devices = []
        command = self._command(audio)
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise TranscriptionError(f"Could not start whisper-cli: {exc}") from exc

        stderr_lines: deque[str] = deque(maxlen=self.stderr_tail)
        with self._lock:
            self._proc = proc

        def drain_stderr() -> None:
            assert proc.stderr is not None
            for raw_line in proc.stderr:
                line = raw_line.rstrip("\n")
                stderr_lines.append(line)
                if "ggml_vulkan: Found" in line or re.search(
                    r"ggml_vulkan: \d+ = ", line
                ):
                    self.vulkan_devices.append(line)
                if "whisper_backend_init_gpu: no GPU found" in line:
                    self.device_info = "CPU"
                else:
                    match = _DEVICE_RE.search(line)
                    if match is not None:
                        self.device_info = match.group(1).strip()

        reader = threading.Thread(
            target=drain_stderr, name="whisper-stderr", daemon=True
        )
        reader.start()

        completed = False
        returncode: int | None = None
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                segment = parse_segment(line)
                if segment is not None:
                    yield segment
            completed = True
        finally:
            if proc.poll() is None:
                if completed:
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                else:
                    # Consumer stopped early (cancellation) — don't wait for
                    # a long transcription to finish.
                    proc.kill()
                    proc.wait()
            returncode = proc.returncode
            reader.join(timeout=5)
            with self._lock:
                self._proc = None

        if completed and returncode != 0:
            detail = _tail(stderr_lines, 6)
            message = f"whisper-cli failed (exit {returncode})"
            if detail:
                message += f":\n{detail}"
            raise TranscriptionError(message)
