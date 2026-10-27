"""QThread worker bridging the GUI and the transcriber.

All heavy work (ffprobe/ffmpeg, model load, whisper-cli) happens here; the GUI
only ever talks to this class through Qt signals.
"""

from __future__ import annotations

import traceback
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .transcriber import (
    Segment,
    Transcriber,
    TranscriptionError,
    TranscriptionOptions,
    segments_to_text,
)


class TranscriptionWorker(QThread):
    """Transcribe one file on a background thread.

    Signals:
        progress(float): estimated fraction complete, 0..1
        segment(str):    one finished transcript segment
        finished(str):   the complete transcript (one segment per line)
        error(str):      a user-readable error message
    """

    progress = Signal(float)
    segment = Signal(str)
    # NB: this shadows QThread.finished deliberately (the plan's API); we use
    # the custom signal and clean the thread up from the slot below.
    finished = Signal(str)
    error = Signal(str)

    def __init__(
        self,
        path: str | Path,
        options: TranscriptionOptions | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.path = Path(path)
        self.options = options or TranscriptionOptions()
        self._transcriber: Transcriber | None = None

    def cancel(self) -> None:
        """Kill the running whisper-cli process, if any (thread-safe enough)."""
        transcriber = self._transcriber
        if transcriber is not None:
            transcriber.cancel()

    def run(self) -> None:  # noqa: D401 - QThread entry point
        transcriber = Transcriber(
            use_gpu=self.options.use_gpu,
            flash_attn=self.options.flash_attn,
            threads=self.options.threads,
            device=self.options.device,
        )
        self._transcriber = transcriber
        try:
            duration = transcriber.duration(self.path)
            segments: list[Segment] = []
            for segment in transcriber.iterate(self.path):
                if segment.text:
                    segments.append(segment)
                    self.segment.emit(segment.text)
                if duration and duration > 0:
                    self.progress.emit(min(0.99, segment.end / duration))
            self.finished.emit(segments_to_text(segments))
        except TranscriptionError as exc:
            self.error.emit(str(exc))
        except Exception as exc:  # never leak a raw traceback into the GUI
            traceback.print_exc()
            self.error.emit(f"Unexpected error: {exc}")
        finally:
            self._transcriber = None
