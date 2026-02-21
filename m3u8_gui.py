#!/usr/bin/env python3
"""
m3u8_gui.py - PyQt6 GUI for m3u8_downloader.py
"""

from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import (
    QSettings, QThread, QTimer, Qt, pyqtSignal,
)
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
    QFrame,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
DOWNLOADER = SCRIPT_DIR / "m3u8_downloader.py"
APP_NAME   = "m3u8downloader"
APP_ORG    = "m3u8-downloader"

GPU_ENCODERS = [
    ("auto",        "Auto-detect (recommended)"),
    ("h264_nvenc",  "NVIDIA NVENC"),
    ("h264_amf",    "AMD AMF"),
    ("h264_qsv",    "Intel QSV"),
    ("libx264",     "CPU (libx264)"),
]

STYLESHEET = """
QMainWindow, QWidget {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 13px;
}
QGroupBox {
    border: 1px solid #45475a;
    border-radius: 6px;
    margin-top: 10px;
    padding-top: 8px;
    font-weight: bold;
    color: #89b4fa;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}
QLineEdit, QSpinBox, QComboBox {
    background-color: #313244;
    border: 1px solid #45475a;
    border-radius: 4px;
    padding: 5px 8px;
    color: #cdd6f4;
    selection-background-color: #89b4fa;
    min-height: 28px;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border: 1px solid #89b4fa;
}
QComboBox::drop-down { border: none; }
QComboBox::down-arrow { image: none; width: 12px; }
QComboBox QAbstractItemView {
    background-color: #313244;
    border: 1px solid #45475a;
    color: #cdd6f4;
    selection-background-color: #89b4fa;
}
QPushButton {
    background-color: #45475a;
    border: 1px solid #585b70;
    border-radius: 5px;
    padding: 6px 14px;
    color: #cdd6f4;
}
QPushButton:hover  { background-color: #585b70; }
QPushButton:pressed { background-color: #6c7086; }
QPushButton#btn_start {
    background-color: #89b4fa;
    color: #1e1e2e;
    font-weight: bold;
    padding: 8px 28px;
    font-size: 14px;
}
QPushButton#btn_start:hover  { background-color: #b4befe; }
QPushButton#btn_start:disabled { background-color: #45475a; color: #6c7086; }
QPushButton#btn_cancel {
    background-color: #f38ba8;
    color: #1e1e2e;
    font-weight: bold;
    padding: 8px 20px;
    font-size: 14px;
}
QPushButton#btn_cancel:hover  { background-color: #eba0ac; }
QPushButton#btn_cancel:disabled { background-color: #45475a; color: #6c7086; }
QProgressBar {
    background-color: #313244;
    border: 1px solid #45475a;
    border-radius: 5px;
    text-align: center;
    color: #cdd6f4;
    height: 18px;
}
QProgressBar::chunk {
    background-color: #89b4fa;
    border-radius: 4px;
}
QCheckBox { spacing: 8px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #45475a;
    border-radius: 3px;
    background-color: #313244;
}
QCheckBox::indicator:checked {
    background-color: #89b4fa;
    border-color: #89b4fa;
}
QPlainTextEdit {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 5px;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 12px;
    color: #a6e3a1;
}
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical {
    background: #1e1e2e;
    width: 8px;
    border-radius: 4px;
}
QScrollBar::handle:vertical {
    background: #45475a;
    min-height: 24px;
    border-radius: 4px;
}
QScrollBar::handle:vertical:hover { background: #585b70; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
/* Collapsible section toggle button */
QPushButton#section_toggle {
    background-color: #2a2a3e;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 8px 14px;
    color: #89b4fa;
    font-weight: bold;
    text-align: left;
    font-size: 13px;
}
QPushButton#section_toggle:hover { background-color: #313244; border-color: #89b4fa; }
"""


# ---------------------------------------------------------------------------
# Collapsible section widget
# ---------------------------------------------------------------------------

class CollapsibleSection(QWidget):
    """A header button that toggles visibility of a content area below it."""

    def __init__(self, title: str, expanded: bool = True,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title    = title
        self._expanded = expanded

        outer = QVBoxLayout(self)
        outer.setSpacing(4)
        outer.setContentsMargins(0, 0, 0, 0)

        # Toggle header button
        self._btn = QPushButton(self._label())
        self._btn.setObjectName("section_toggle")
        self._btn.setCheckable(True)
        self._btn.setChecked(expanded)
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.setFixedHeight(36)
        self._btn.clicked.connect(self._on_toggle)
        outer.addWidget(self._btn)

        # Content body
        self._body = QFrame()
        self._body.setFrameShape(QFrame.Shape.StyledPanel)
        self._body.setStyleSheet(
            "QFrame { border: 1px solid #45475a; border-radius: 6px; }"
        )
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setSpacing(0)
        self._body_lay.setContentsMargins(0, 4, 0, 4)
        self._body.setVisible(expanded)
        outer.addWidget(self._body)

    def _label(self) -> str:
        return ("  ▼   " if self._expanded else "  ▶   ") + self._title

    def _on_toggle(self, checked: bool) -> None:
        self._expanded = checked
        self._body.setVisible(checked)
        self._btn.setText(self._label())

    def body_layout(self) -> QVBoxLayout:
        return self._body_lay


# ---------------------------------------------------------------------------
# GPU detection worker
# ---------------------------------------------------------------------------

class GpuDetectWorker(QThread):
    """Detect available GPU encoders without blocking the UI."""
    results = pyqtSignal(list)   # list of (value, label) tuples

    def __init__(self, ffmpeg_path: str):
        super().__init__()
        self.ffmpeg_path = ffmpeg_path

    def run(self) -> None:
        ffmpeg = self.ffmpeg_path or shutil.which("ffmpeg")
        if not ffmpeg:
            self.results.emit([])
            return

        try:
            enc_result = subprocess.run(
                [ffmpeg, "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=10,
            )
            available_text = enc_result.stdout + enc_result.stderr
        except Exception:
            self.results.emit([])
            return

        found: list[tuple[str, str]] = []
        for value, label in GPU_ENCODERS[1:-1]:   # skip auto and libx264
            if f" {value} " not in available_text:
                continue
            # Quick sanity test-encode
            test = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=black:s=64x64:d=0.1",
                    "-frames:v", "1", "-c:v", value, "-f", "null", "-",
                ],
                capture_output=True, timeout=15,
            )
            if test.returncode == 0:
                found.append((value, label))

        self.results.emit(found)


# ---------------------------------------------------------------------------
# Download worker
# ---------------------------------------------------------------------------

class DownloadWorker(QThread):
    """Spawn m3u8_downloader.py and stream progress back via signals."""

    progress_download = pyqtSignal(int)   # 0-100
    progress_scale    = pyqtSignal(int)   # 0-100  (-1 = scale started, no total yet)
    log_line          = pyqtSignal(str)
    finished          = pyqtSignal(bool, str)   # success, message

    def __init__(self, cmd: list[str], env: dict[str, str]):
        super().__init__()
        self.cmd = cmd
        self.env = env
        self._proc: subprocess.Popen | None = None

    def run(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.env,
                cwd=str(SCRIPT_DIR),
            )
        except Exception as exc:
            self.finished.emit(False, str(exc))
            return

        line_q: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()

        def _read(stream, tag: str) -> None:
            buf = ""
            for ch in iter(lambda: stream.read(1), ""):
                if ch in ("\n", "\r"):
                    stripped = buf.strip()
                    if stripped:
                        line_q.put((tag, stripped))
                    buf = ""
                else:
                    buf += ch
            if buf.strip():
                line_q.put((tag, buf.strip()))
            line_q.put((tag, None))   # sentinel: stream ended

        t_out = threading.Thread(target=_read, args=(self._proc.stdout, "out"), daemon=True)
        t_err = threading.Thread(target=_read, args=(self._proc.stderr, "err"), daemon=True)
        t_out.start()
        t_err.start()

        streams_done = 0
        _scale_total: float = 0.0
        _dl_re    = re.compile(r"Downloading:\s+(\d+)%")
        _scale_re = re.compile(r"Scaling:\s+(\d+)%")

        while streams_done < 2 or not line_q.empty():
            try:
                tag, line = line_q.get(timeout=0.1)
            except queue.Empty:
                if self._proc.poll() is not None:
                    break
                continue

            if line is None:
                streams_done += 1
                continue

            # --- parse sentinels (stdout) ---
            if tag == "out":
                m = re.match(r"\[scale_total_s=([\d.]+)\]", line)
                if m:
                    _scale_total = float(m.group(1))
                    self.progress_scale.emit(-1)   # signal: scale started
                    continue

                m = re.match(r"\[scale_progress=(\d+)\]", line)
                if m:
                    self.progress_scale.emit(int(m.group(1)))
                    continue

            # --- parse tqdm download progress (stderr) ---
            if tag == "err":
                m = _dl_re.search(line)
                if m:
                    self.progress_download.emit(int(m.group(1)))
                    clean = re.sub(r"\x1b\[[0-9;]*m", "", line)
                    self.log_line.emit(clean)
                    continue

                m = _scale_re.search(line)
                if m and _scale_total == 0:
                    self.progress_scale.emit(int(m.group(1)))
                    continue

            # --- everything else -> log ---
            clean = re.sub(r"\x1b\[[0-9;]*m", "", line)
            self.log_line.emit(clean)

        self._proc.wait()
        rc = self._proc.returncode

        if rc == 0:
            self.finished.emit(True, "Download complete.")
        else:
            self.finished.emit(rc in (0,), f"Process exited with code {rc}.")

    def cancel(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("M3U8 Downloader")
        self.setMinimumSize(700, 500)
        self._settings = QSettings(APP_ORG, APP_NAME)
        self._worker: DownloadWorker | None = None
        self._gpu_worker: GpuDetectWorker | None = None
        self._scale_active = False

        self._build_ui()
        self._load_settings()

        QTimer.singleShot(300, self._detect_gpu)

    # ------------------------------------------------------------------ UI --

    def _build_ui(self) -> None:
        # ── Outer scroll area: the entire UI is scrollable ───────────────
        outer_scroll = QScrollArea()
        outer_scroll.setWidgetResizable(True)
        outer_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.setCentralWidget(outer_scroll)

        root = QWidget()
        outer_scroll.setWidget(root)
        vlay = QVBoxLayout(root)
        vlay.setSpacing(10)
        vlay.setContentsMargins(12, 12, 12, 12)

        # ── Shared form helpers ───────────────────────────────────────────
        def _sep() -> QFrame:
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            line.setFixedHeight(1)
            line.setStyleSheet("background-color: #313244;")
            return line

        def _form_row(parent_vlay: QVBoxLayout, lbl_text: str,
                      *widgets, lbl_width: int = 110) -> None:
            """QWidget-wrapped row with label + widgets, followed by a separator."""
            container = QWidget()
            container.setMinimumHeight(48)
            hlay = QHBoxLayout(container)
            hlay.setContentsMargins(12, 4, 12, 4)
            lbl = QLabel(lbl_text)
            lbl.setFixedWidth(lbl_width)
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            hlay.addWidget(lbl)
            hlay.addSpacing(8)
            for w in widgets:
                if isinstance(w, QWidget):
                    hlay.addWidget(w)
                else:
                    hlay.addLayout(w)
            hlay.addStretch()
            parent_vlay.addWidget(container)
            parent_vlay.addWidget(_sep())

        def _path_row(parent_vlay: QVBoxLayout, lbl_text: str,
                      placeholder: str) -> QLineEdit:
            """Directory-picker row: label + line edit + Browse button."""
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            btn = QPushButton("Browse...")
            btn.setFixedWidth(80)

            def _browse() -> None:
                p = QFileDialog.getExistingDirectory(
                    None, f"Select {lbl_text}",
                    edit.text() or str(Path.home()),
                )
                if p:
                    edit.setText(p)

            btn.clicked.connect(_browse)
            _form_row(parent_vlay, lbl_text, edit, btn)
            return edit

        # ── Stream URL ───────────────────────────────────────────────────
        url_group = QGroupBox("Stream URL")
        url_hlay = QHBoxLayout(url_group)
        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText("https://example.com/stream/index.m3u8")
        btn_paste = QPushButton("Paste")
        btn_paste.setFixedWidth(64)
        btn_paste.clicked.connect(
            lambda: self._url_edit.setText(QApplication.clipboard().text().strip())
        )
        url_hlay.addWidget(self._url_edit)
        url_hlay.addWidget(btn_paste)
        vlay.addWidget(url_group)

        # ── Output File ──────────────────────────────────────────────────
        out_group = QGroupBox("Output File")
        out_hlay = QHBoxLayout(out_group)
        self._output_edit = QLineEdit()
        self._output_edit.setPlaceholderText("output.mp4")
        btn_out = QPushButton("Browse...")
        btn_out.setFixedWidth(80)
        btn_out.clicked.connect(self._browse_output)
        out_hlay.addWidget(self._output_edit)
        out_hlay.addWidget(btn_out)
        vlay.addWidget(out_group)

        # ── Download Options (collapsible) ───────────────────────────────
        dl_sec = CollapsibleSection("Download Options")
        dl_lay = dl_sec.body_layout()

        self._res_edit = QLineEdit()
        self._res_edit.setPlaceholderText("e.g. 1080p  (blank = best)")
        _form_row(dl_lay, "Resolution:", self._res_edit)

        self._scale_spin = QSpinBox()
        self._scale_spin.setRange(0, 4320)
        self._scale_spin.setSpecialValueText("No scaling")
        self._scale_spin.setSuffix("p")
        self._scale_spin.setFixedWidth(120)
        _sh = QLabel("0 = disabled")
        _sh.setStyleSheet("color: #6c7086; font-size: 11px;")
        _form_row(dl_lay, "Scale to:", self._scale_spin, _sh)

        self._workers_spin = QSpinBox()
        self._workers_spin.setRange(1, 32)
        self._workers_spin.setValue(4)
        self._workers_spin.setFixedWidth(70)
        _wh = QLabel("parallel threads")
        _wh.setStyleSheet("color: #6c7086; font-size: 11px;")
        _form_row(dl_lay, "Workers:", self._workers_spin, _wh)

        self._referer_edit = QLineEdit()
        self._referer_edit.setPlaceholderText("Auto-derived from URL")
        _form_row(dl_lay, "Referer:", self._referer_edit)

        # Checkboxes - indented to align with inputs
        chk_container = QWidget()
        chk_container.setMinimumHeight(44)
        chk_hlay = QHBoxLayout(chk_container)
        chk_hlay.setContentsMargins(12 + 110 + 8, 4, 12, 4)
        self._audio_chk     = QCheckBox("Audio only")
        self._no_ffmpeg_chk = QCheckBox("No ffmpeg  (save raw .ts)")
        chk_hlay.addWidget(self._audio_chk)
        chk_hlay.addSpacing(20)
        chk_hlay.addWidget(self._no_ffmpeg_chk)
        chk_hlay.addStretch()
        dl_lay.addWidget(chk_container)

        vlay.addWidget(dl_sec)

        # ── Paths & Encoder (collapsible) ────────────────────────────────
        pe_sec = CollapsibleSection("Paths && Encoder")
        pe_lay = pe_sec.body_layout()

        self._ffmpeg_edit   = _path_row(pe_lay, "FFmpeg dir:",   "Leave blank to use PATH")
        self._dl_dir_edit   = _path_row(pe_lay, "Download dir:", "Leave blank for 'downloads' locally")
        self._temp_dir_edit = _path_row(pe_lay, "Temp dir:",     "Segment temp folder")

        self._gpu_combo = QComboBox()
        for val, lbl_text in GPU_ENCODERS:
            self._gpu_combo.addItem(lbl_text, val)
        self._gpu_combo.setMinimumWidth(210)
        self._btn_detect = QPushButton("Detect")
        self._btn_detect.setFixedWidth(70)
        self._btn_detect.clicked.connect(self._detect_gpu)
        self._btn_detect.setToolTip("Re-scan for available hardware encoders")
        _form_row(pe_lay, "GPU encoder:", self._gpu_combo, self._btn_detect)

        vlay.addWidget(pe_sec)

        # ── Progress ─────────────────────────────────────────────────────
        prog_group = QGroupBox("Progress")
        prog_vlay = QVBoxLayout(prog_group)
        prog_vlay.setSpacing(6)

        dl_hlay = QHBoxLayout()
        dl_hlay.addWidget(QLabel("Download:"))
        self._dl_bar = QProgressBar()
        self._dl_bar.setRange(0, 100)
        self._dl_bar.setValue(0)
        self._dl_bar.setFormat("%p%")
        dl_hlay.addWidget(self._dl_bar)
        prog_vlay.addLayout(dl_hlay)

        sc_hlay = QHBoxLayout()
        sc_hlay.addWidget(QLabel("  Scaling:"))
        self._sc_bar = QProgressBar()
        self._sc_bar.setRange(0, 100)
        self._sc_bar.setValue(0)
        self._sc_bar.setFormat("%p%  (idle)")
        sc_hlay.addWidget(self._sc_bar)
        prog_vlay.addLayout(sc_hlay)

        vlay.addWidget(prog_group)

        # ── Action buttons ────────────────────────────────────────────────
        btn_hlay = QHBoxLayout()
        btn_hlay.addStretch()
        self._btn_start = QPushButton("▶  Start Download")
        self._btn_start.setObjectName("btn_start")
        self._btn_start.clicked.connect(self._start_download)
        self._btn_cancel = QPushButton("✕  Cancel")
        self._btn_cancel.setObjectName("btn_cancel")
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.clicked.connect(self._cancel_download)
        btn_hlay.addWidget(self._btn_start)
        btn_hlay.addWidget(self._btn_cancel)
        btn_hlay.addStretch()
        vlay.addLayout(btn_hlay)

        # ── Log ──────────────────────────────────────────────────────────
        log_group = QGroupBox("Log")
        log_vlay = QVBoxLayout(log_group)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(2000)
        self._log.setMinimumHeight(140)
        log_vlay.addWidget(self._log)
        vlay.addWidget(log_group)

        vlay.addStretch()

    # ---------------------------------------------------------------- browse --

    def _browse_output(self) -> None:
        dl_dir = self._dl_dir_edit.text().strip()
        if not dl_dir:
            dl_dir = str(SCRIPT_DIR / "downloads")
        
        ext = ".ts" if self._no_ffmpeg_chk.isChecked() else ".mp4"
        default_name = str(Path(dl_dir) / f"output{ext}")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save output as...", default_name,
            "MP4 Files (*.mp4);;MKV Files (*.mkv);;TS Files (*.ts);;All Files (*)",
        )
        if path:
            self._output_edit.setText(path)

    # ----------------------------------------------------------- GPU detect --

    def _detect_gpu(self) -> None:
        ffmpeg_dir = self._ffmpeg_edit.text().strip()
        ffmpeg_path = ""
        if ffmpeg_dir and Path(ffmpeg_dir).is_dir():
            ffmpeg_path = str(Path(ffmpeg_dir) / "ffmpeg.exe")
        else:
            ffmpeg_path = shutil.which("ffmpeg") or ""

        self._btn_detect.setEnabled(False)
        self._btn_detect.setText("...")
        self._log.appendPlainText("[gui] Detecting GPU encoders...")

        self._gpu_worker = GpuDetectWorker(ffmpeg_path)
        self._gpu_worker.results.connect(self._on_gpu_results)
        self._gpu_worker.start()

    def _on_gpu_results(self, found: list[tuple[str, str]]) -> None:
        self._btn_detect.setEnabled(True)
        self._btn_detect.setText("Detect")

        current_val = self._gpu_combo.currentData()
        self._gpu_combo.clear()
        self._gpu_combo.addItem("Auto-detect (recommended)", "auto")
        for val, label in found:
            self._gpu_combo.addItem(f"✓ {label}", val)
        self._gpu_combo.addItem("CPU (libx264)", "libx264")

        idx = self._gpu_combo.findData(current_val)
        self._gpu_combo.setCurrentIndex(idx if idx >= 0 else 0)

        if found:
            names = ", ".join(lbl for _, lbl in found)
            self._log.appendPlainText(f"[gui] GPU encoders found: {names}")
        else:
            self._log.appendPlainText("[gui] No hardware GPU encoders found - will use CPU (libx264).")

    # --------------------------------------------------------- settings I/O --

    def _load_settings(self) -> None:
        s = self._settings
        self._ffmpeg_edit.setText(s.value("ffmpeg_dir", ""))
        self._dl_dir_edit.setText(s.value("download_dir", ""))
        self._temp_dir_edit.setText(s.value("temp_dir", ""))
        self._workers_spin.setValue(int(s.value("workers", 4)))
        self._scale_spin.setValue(int(s.value("scale", 0)))
        gpu_val = s.value("gpu_encoder", "auto")
        idx = self._gpu_combo.findData(gpu_val)
        if idx >= 0:
            self._gpu_combo.setCurrentIndex(idx)
        geom = s.value("geometry")
        if geom:
            self.restoreGeometry(geom)

    def _save_settings(self) -> None:
        s = self._settings
        s.setValue("ffmpeg_dir",    self._ffmpeg_edit.text().strip())
        s.setValue("download_dir",  self._dl_dir_edit.text().strip())
        s.setValue("temp_dir",      self._temp_dir_edit.text().strip())
        s.setValue("workers",       self._workers_spin.value())
        s.setValue("scale",         self._scale_spin.value())
        s.setValue("gpu_encoder",   self._gpu_combo.currentData())
        s.setValue("geometry",      self.saveGeometry())

    def closeEvent(self, event) -> None:
        self._save_settings()
        super().closeEvent(event)

    # ----------------------------------------------------------- download ----

    def _build_cmd(self) -> list[str] | None:
        url = self._url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "Missing URL", "Please enter an M3U8 stream URL.")
            return None

        output = self._output_edit.text().strip()
        if not output:
            dl_dir = self._dl_dir_edit.text().strip()
            if not dl_dir:
                dl_dir = str(SCRIPT_DIR / "downloads")
                
            ext = ".ts" if self._no_ffmpeg_chk.isChecked() else ".mp4"
            base_name = "output"
            out_path = Path(dl_dir) / f"{base_name}{ext}"
            
            counter = 1
            while out_path.exists():
                out_path = Path(dl_dir) / f"{base_name}-{counter}{ext}"
                counter += 1
                
            output = str(out_path)
            self._output_edit.setText(output)

        cmd = [
            sys.executable, "-u", str(DOWNLOADER),
            url,
            "-o", output,
            "-w", str(self._workers_spin.value()),
        ]
        if self._res_edit.text().strip():
            cmd += ["-r", self._res_edit.text().strip()]
        if self._scale_spin.value() > 0:
            cmd += ["-s", str(self._scale_spin.value())]
        if self._referer_edit.text().strip():
            cmd += ["--referer", self._referer_edit.text().strip()]
        if self._audio_chk.isChecked():
            cmd.append("--audio-only")
        if self._no_ffmpeg_chk.isChecked():
            cmd.append("--no-ffmpeg")
        if self._temp_dir_edit.text().strip():
            cmd += ["--temp-dir", self._temp_dir_edit.text().strip()]

        return cmd

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        ffmpeg_dir = self._ffmpeg_edit.text().strip()
        if ffmpeg_dir and Path(ffmpeg_dir).is_dir():
            env["PATH"] = ffmpeg_dir + os.pathsep + env.get("PATH", "")
        gpu_val = self._gpu_combo.currentData()
        if gpu_val and gpu_val != "auto":
            env["M3U8_ENCODER_OVERRIDE"] = gpu_val
        else:
            env.pop("M3U8_ENCODER_OVERRIDE", None)
        return env

    def _start_download(self) -> None:
        cmd = self._build_cmd()
        if cmd is None:
            return

        self._dl_bar.setValue(0)
        self._dl_bar.setFormat("%p%")
        self._sc_bar.setValue(0)
        self._sc_bar.setFormat("%p%  (idle)")
        self._log.clear()
        self._log.appendPlainText("[gui] Starting: " + " ".join(cmd[2:]))
        self._scale_active = False

        self._btn_start.setEnabled(False)
        self._btn_cancel.setEnabled(True)

        env = self._build_env()
        self._worker = DownloadWorker(cmd, env)
        self._worker.log_line.connect(self._on_log_line)
        self._worker.progress_download.connect(self._on_progress_download)
        self._worker.progress_scale.connect(self._on_progress_scale)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _cancel_download(self) -> None:
        if self._worker:
            self._worker.cancel()
        self._log.appendPlainText("[gui] Cancellation requested...")
        self._btn_cancel.setEnabled(False)

    # ----------------------------------------------------------- slots ------

    def _on_log_line(self, line: str) -> None:
        self._log.appendPlainText(line)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_progress_download(self, pct: int) -> None:
        self._dl_bar.setValue(pct)
        self._dl_bar.setFormat(f"Downloading - {pct}%")

    def _on_progress_scale(self, pct: int) -> None:
        if pct == -1:
            self._sc_bar.setRange(0, 0)   # indeterminate pulse
            self._sc_bar.setFormat("Scaling...")
            self._scale_active = True
        else:
            if self._sc_bar.maximum() == 0:
                self._sc_bar.setRange(0, 100)
            self._sc_bar.setValue(pct)
            self._sc_bar.setFormat(f"Scaling - {pct}%")

    def _on_finished(self, success: bool, message: str) -> None:
        self._btn_start.setEnabled(True)
        self._btn_cancel.setEnabled(False)

        if success:
            self._dl_bar.setValue(100)
            self._dl_bar.setFormat("Done ✓  100%")
            if self._scale_active:
                self._sc_bar.setRange(0, 100)
                self._sc_bar.setValue(100)
                self._sc_bar.setFormat("Done ✓  100%")
            self._log.appendPlainText(f"\n[gui] ✔ {message}")
        else:
            self._log.appendPlainText(f"\n[gui] ✘ {message}")

        self._worker = None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORG)
    app.setStyleSheet(STYLESHEET)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
