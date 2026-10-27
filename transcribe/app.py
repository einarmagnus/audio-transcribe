"""Qt main window: drag-and-drop, live transcript, progress and queueing."""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

from PySide6.QtCore import QMimeData, QSettings, QSize, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import cache
from .transcriber import Segment, TranscriptionOptions
from .worker import TranscriptionWorker

#: File types the GUI accepts drops for (CLI/ffmpeg may handle more).
SUPPORTED_EXTENSIONS = frozenset(
    {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".opus", ".webm"}
)


def paths_from_mime(mime: QMimeData) -> list[Path]:
    """Extract local file paths from a drag-and-drop payload."""
    if not mime.hasUrls():
        return []
    return [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()]


class MainWindow(QMainWindow):
    def __init__(
        self,
        options: TranscriptionOptions | None = None,
        settings: QSettings | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Transcribe")
        self.setMinimumSize(520, 360)
        self.setAcceptDrops(True)

        self._options = options or TranscriptionOptions()
        self._settings = settings or QSettings("transcribe", "Transcribe")
        self._queue: deque[Path] = deque()
        self._busy = False
        self._closing = False
        self._worker: TranscriptionWorker | None = None
        self._current: Path | None = None
        #: Timestamped segments of the last transcript (for future .srt export).
        self.last_segments: list[Segment] = []

        self._build_ui()
        self._restore_window_size()
        self._first_run_note = not bool(
            self._settings.value("ui/model_note_shown", False, type=bool)
        )
        if self._first_run_note:
            self._set_status(
                "First transcription may take longer while the model loads"
            )

    def _restore_window_size(self) -> None:
        size = self._settings.value("window/size")
        if not isinstance(size, QSize) or not size.isValid():
            self.resize(820, 620)
            return
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            size = size.boundedTo(screen.availableGeometry().size())
        self.resize(size.expandedTo(self.minimumSize()))

    # -- UI construction ---------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)

        self.drop_hint = QLabel(
            "Drop an audio file here — "
            "mp3, wav, m4a, ogg, flac, opus, webm",
            central,
        )
        self.drop_hint.setAlignment(Qt.AlignCenter)
        self.drop_hint.setStyleSheet("padding: 6px;")

        self.transcript = QPlainTextEdit(central)
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText("The transcript will appear here.")
        self.transcript.setLineWrapMode(QPlainTextEdit.WidgetWidth)

        bottom = QHBoxLayout()
        self.status = QLabel("Drop an audio file here", central)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.progress = QProgressBar(central)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedWidth(180)
        self.copy_button = QPushButton("Copy", central)
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self._copy_transcript)

        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.progress)
        bottom.addWidget(self.copy_button)

        layout.addWidget(self.drop_hint)
        layout.addWidget(self.transcript, 1)
        layout.addLayout(bottom)
        self.setCentralWidget(central)

    # -- drag and drop -----------------------------------------------------

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt API
        paths = paths_from_mime(event.mimeData())
        if not paths:
            event.ignore()
            return
        self.enqueue(paths)
        event.acceptProposedAction()

    # -- public API --------------------------------------------------------

    def enqueue(self, paths: list[str | Path]) -> None:
        """Queue audio files, starting one immediately when idle."""
        if self._closing:
            return
        accepted = 0
        rejected: list[tuple[Path, str]] = []
        for raw_path in paths:
            path = Path(raw_path).expanduser()
            if not path.exists():
                self._set_status(f"Not found: {path}", error=True)
                rejected.append((path, "not found"))
                continue
            if path.is_dir():
                self._set_status(f"Skipping folder: {path.name}", error=True)
                rejected.append((path, "is a folder"))
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                self._set_status(
                    f"Unsupported file type: {path.suffix or path.name}", error=True
                )
                rejected.append(
                    (path, f"unsupported type ({path.suffix or 'no extension'})")
                )
                continue
            self._queue.append(path)
            accepted += 1

        if rejected and not self._closing:
            details = "\n".join(f"{path}: {reason}" for path, reason in rejected)
            self._show_message(
                QMessageBox.Warning,
                "Files skipped",
                f"{len(rejected)} file(s) could not be added.",
                details,
            )
        if not accepted:
            return
        if self._busy:
            self._set_status(f"Queued {len(self._queue)} file(s)…")
        else:
            self._process_next()

    # -- queue / transcription lifecycle -----------------------------------

    def _process_next(self) -> None:
        if self._busy or self._closing:
            return
        while self._queue:
            path = self._queue.popleft()
            cached = cache.load(path)
            if cached is not None:
                self._show_cached(path, cached)
                continue
            self._begin_transcription(path)
            return

    def _begin_transcription(self, path: Path) -> None:
        self._busy = True
        self._current = path
        self.last_segments = []
        self.transcript.clear()
        self.copy_button.setEnabled(False)
        self.progress.setRange(0, 0)  # indeterminate until the first segment
        queued = f" ({len(self._queue)} queued)" if self._queue else ""
        first_run = self._first_run_note
        if first_run:
            self._first_run_note = False
            self._settings.setValue("ui/model_note_shown", True)
            self._settings.sync()
        note = " — first run: model load may take longer" if first_run else ""
        self._set_status(f"Transcribing {path.name}…{queued}{note}")

        worker = TranscriptionWorker(path, self._options, self)
        worker.progress.connect(self._on_progress)
        worker.segment.connect(self._on_segment)
        worker.finished.connect(self._on_finished)
        worker.error.connect(self._on_error)
        self._worker = worker
        worker.start()

    def _show_cached(self, path: Path, text: str) -> None:
        self._current = path
        self.last_segments = []
        self.transcript.setPlainText(text)
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.copy_button.setEnabled(bool(text))
        self._set_status(f"Loaded from cache: {path.name}")

    def _finish_worker(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        if worker.isRunning() and not worker.wait(5000):
            worker.cancel()
            worker.wait(5000)
        worker.deleteLater()

    # -- worker signal handlers --------------------------------------------

    def _on_progress(self, value: float) -> None:
        if self.progress.maximum() == 0:  # was indeterminate
            self.progress.setRange(0, 100)
        self.progress.setValue(max(0, min(100, round(value * 100))))

    def _on_segment(self, text: str) -> None:
        self.transcript.appendPlainText(text)

    def _on_finished(self, text: str) -> None:
        path = self._current
        worker = self._worker
        self.last_segments = list(worker.segments) if worker is not None else []
        self._busy = False
        self.transcript.setPlainText(text)
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.copy_button.setEnabled(bool(text))
        self._finish_worker()

        if path is not None:
            try:
                sidecar = cache.save(path, text)
            except OSError as exc:
                self._set_status(
                    f"Transcribed {path.name}, but saving failed: {exc}",
                    error=True,
                    tooltip=str(exc),
                )
            else:
                if text.strip():
                    self._set_status(f"Done: {path.name} — saved {sidecar.name}")
                else:
                    self._set_status(
                        f"No speech detected in {path.name} — saved empty transcript"
                    )

        if not self._closing:
            self._process_next()

    def _on_error(self, message: str) -> None:
        first_line = message.strip().splitlines()[0] if message.strip() else "Error"
        print(f"transcribe: {message}", file=sys.stderr)
        self._busy = False
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.copy_button.setEnabled(bool(self.transcript.toPlainText()))
        self._set_status(f"Error: {first_line}", error=True, tooltip=message)
        self._finish_worker()
        if not self._closing:
            self._show_message(
                QMessageBox.Critical,
                "Transcription failed",
                first_line,
                message,
            )
            self._process_next()

    # -- small helpers -----------------------------------------------------

    def _set_status(
        self, text: str, *, error: bool = False, tooltip: str = ""
    ) -> None:
        self.status.setText(text)
        self.status.setToolTip(tooltip)
        self.status.setStyleSheet(
            "color: #d32f2f; font-weight: 600;" if error else ""
        )

    def _copy_transcript(self) -> None:
        text = self.transcript.toPlainText()
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        self._set_status("Copied transcript to clipboard")

    def _show_message(
        self, icon, title: str, text: str, details: str = ""
    ) -> None:
        box = QMessageBox(self)
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setText(text)
        if details and details != text:
            box.setDetailedText(details)
        box.exec()

    # -- shutdown ----------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._busy and self._worker is not None:
            reply = QMessageBox.question(
                self,
                "Transcription in progress",
                "A transcription is still running.\nQuit and cancel it?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            self._closing = True
            self._queue.clear()
            self._worker.cancel()
            if not self._worker.wait(10000):
                self._closing = False
                QMessageBox.information(
                    self,
                    "Still finishing",
                    "Cancellation is taking longer than expected.\n"
                    "Try closing again in a moment.",
                )
                event.ignore()
                return
        self._settings.setValue("window/size", self.size())
        self._settings.sync()
        event.accept()


def run(
    path: str | Path | None = None,
    options: TranscriptionOptions | None = None,
) -> int:
    """Start the GUI, optionally with *path* preloaded."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
        app.setOrganizationName("transcribe")
        app.setApplicationName("Transcribe")
    window = MainWindow(options)
    window.show()
    if path is not None:
        window.enqueue([path])
    return int(app.exec())
