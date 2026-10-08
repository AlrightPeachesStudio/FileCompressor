"""
File Compressor - Alright Peaches Studio

Two ways to use it:
  1. Main window: drop a file/folder, pick a percentage, compress, then DRAG the
     result straight out of the window to wherever you want it saved.
  2. Right-click menu: add "Compress with File Compressor" to chosen file types
     and folders (Settings button). Pick a percentage from the menu and the file
     is compressed in the background, saved next to the original with a
     timestamp appended to its name.

Requirements: PyQt5, ffmpeg.exe next to this script (Ghostscript optional, for PDFs).
"""

import argparse
import atexit
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path

try:
    import winreg
except ImportError:  # Not Windows: right-click menu management is unavailable
    winreg = None

from PyQt5.QtCore import Qt, QObject, QThread, QTimer, QUrl, QMimeData, QPoint, pyqtSignal
from PyQt5.QtGui import QColor, QDesktopServices, QDrag, QFontMetrics, QIcon, QPainter, QPixmap
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QSlider, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

APP_NAME = "File Compressor"
APP_TITLE = "File Compressor - Alright Peaches Studio"
MENU_TITLE = "Compress with File Compressor"
VERB_KEY = "AlrightPeachesFileCompressor"          # registry verb name
IPC_NAME = "AlrightPeaches.FileCompressor.Jobs"    # single-instance job queue
MENU_PERCENTS = list(range(10, 100, 10))           # 10% ... 90%
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Keep APP_VERSION in sync with the release tags on GitHub (for example tag "v1.0.1" -> "1.0.1").
APP_VERSION = "3.1.0.0"
MORE_APPS_URL = "https://store.steampowered.com/search/?developer=Alright%20Peaches%20Studio"
RELEASES_URL = "https://github.com/AlrightPeachesStudio/FileCompressor/releases"
LATEST_RELEASE_URL = RELEASES_URL + "/latest"   # redirects to .../releases/tag/<newest tag>

FORMAT_GROUPS = {
    "Images": [".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"],
    "Audio": [".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus"],
    "Video": [".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg"],
    "PDF & Office": [".pdf", ".docx", ".xlsx", ".pptx"],
}
IMAGE_EXTS = set(FORMAT_GROUPS["Images"])
AUDIO_EXTS = set(FORMAT_GROUPS["Audio"])
VIDEO_EXTS = set(FORMAT_GROUPS["Video"])
OFFICE_EXTS = {".docx", ".xlsx", ".pptx"}

MP3_RATES = [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]  # kbps


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_dirs():
    dirs = [app_dir()]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        dirs.append(bundle)
    return dirs


def find_in_app(names):
    for folder in resource_dirs():
        for name in names:
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def find_ffmpeg():
    found = find_in_app(["ffmpeg.exe", "ffmpeg"])
    if found:
        return found
    return shutil.which("ffmpeg")


def find_ghostscript():
    found = find_in_app(["gswin64c.exe", "gswin32c.exe"])
    if found:
        return found
    for name in ("gswin64c", "gswin32c", "gs"):
        path = shutil.which(name)
        if path:
            return path
    patterns = (
        r"C:\Program Files\gs\gs*\bin\gswin64c.exe",
        r"C:\Program Files (x86)\gs\gs*\bin\gswin32c.exe",
    )
    for pattern in patterns:
        matches = sorted(glob.glob(pattern), reverse=True)
        if matches:
            return matches[0]
    return None


def find_icon():
    return find_in_app(["2.ico"])


def format_size(size_bytes):
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def display_name(path):
    return os.path.basename(os.path.normpath(path)) or path


def unique_path(path, is_dir=False):
    if not os.path.exists(path):
        return path
    base, ext = (path, "") if is_dir else os.path.splitext(path)
    counter = 1
    while os.path.exists(f"{base}_{counter}{ext}"):
        counter += 1
    return f"{base}_{counter}{ext}"


def reveal_in_explorer(path):
    try:
        if sys.platform == "win32":
            subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))
    except OSError:
        pass


def normalize_extension(text):
    ext = text.strip().lower()
    if not ext:
        return None
    if not ext.startswith("."):
        ext = "." + ext
    return ext if re.fullmatch(r"\.[a-z0-9_+\-]{1,15}", ext) else None


def parse_version(text):
    """'v1.2.3-beta' -> (1, 2, 3, 0). Returns None when there is no number in the text."""
    match = re.search(r"\d+(?:\.\d+)*", text or "")
    if not match:
        return None
    parts = [int(part) for part in match.group(0).split(".")][:4]
    return tuple(parts + [0] * (4 - len(parts)))


def version_label(text):
    """'FileCompressor3.2.0.0_Made_by_...' -> '3.2.0.0' (for showing to the user)."""
    match = re.search(r"\d+(?:\.\d+)*", text or "")
    return match.group(0) if match else (text or "")


def is_newer_version(latest, current):
    latest_parts, current_parts = parse_version(latest), parse_version(current)
    return bool(latest_parts and current_parts and latest_parts > current_parts)


def fetch_latest_release_tag(timeout=8):
    """Find the newest published release. Returns its tag, or None if unknown.

    GitHub redirects <releases>/latest to <releases>/tag/<tag>. Reading that redirect
    needs no API key and is not subject to the API's per-IP rate limit.
    """
    request = urllib.request.Request(
        LATEST_RELEASE_URL, method="HEAD", headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
    except Exception:      # offline, blocked, or no such repository: stay silent
        return None
    match = re.search(r"/releases/tag/([^/?#]+)$", final_url)
    return urllib.parse.unquote(match.group(1)) if match else None


def install_excepthook():
    """Under pythonw there is no console, so write unexpected errors to a log file."""
    log_dir = os.path.join(os.environ.get("APPDATA") or tempfile.gettempdir(), "FileCompressor")

    def hook(exc_type, exc, tb):
        try:
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, "error.log"), "a", encoding="utf-8") as handle:
                handle.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}]\n")
                handle.write("".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass

    sys.excepthook = hook


# --------------------------------------------------------------------------- #
# Compression engine (no GUI code in here)
# --------------------------------------------------------------------------- #

class CancelledError(Exception):
    pass


class ToolMissingError(Exception):
    pass


class JobResult:
    def __init__(self, output_path, original_size, new_size, warnings):
        self.output_path = output_path
        self.original_size = original_size
        self.new_size = new_size
        self.warnings = warnings

    @property
    def reduction(self):
        if not self.original_size:
            return 0.0
        return (1.0 - self.new_size / self.original_size) * 100.0


class CompressionEngine:
    """Compresses a file or folder so that it is roughly `percent` % smaller."""

    def __init__(self, percent, log=None, progress=None, cancel_event=None):
        self.percent = max(5, min(95, int(percent)))
        self.keep = 1.0 - self.percent / 100.0
        self.log = log or (lambda message: None)
        self.report = progress or (lambda fraction: None)
        self.cancel_event = cancel_event or threading.Event()
        self.warnings = []
        self._ffmpeg = None
        self._gs = None
        self._base = 0.0   # progress window of the file currently being processed
        self._span = 1.0

    # ----- plumbing --------------------------------------------------------

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise CancelledError()

    def _progress(self, fraction):
        fraction = max(0.0, min(1.0, fraction))
        self.report(self._base + self._span * fraction)

    def _warn(self, message):
        self.warnings.append(message)
        self.log(f"Warning: {message}")

    def ffmpeg(self):
        if not self._ffmpeg:
            self._ffmpeg = find_ffmpeg()
            if not self._ffmpeg:
                raise ToolMissingError(
                    "ffmpeg.exe was not found. Place ffmpeg.exe in the same folder as this program.")
        return self._ffmpeg

    def _ff_cmd(self, with_progress=False):
        cmd = [self.ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error"]
        if with_progress:
            cmd += ["-progress", "pipe:1", "-nostats"]
        return cmd + ["-y"]

    def _run(self, cmd, cwd=None, duration=None, sub_base=0.0, sub_span=1.0):
        """Run a process, follow ffmpeg's -progress output and honour cancellation."""
        self._check_cancel()
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace")
        stop = threading.Event()

        def watchdog():
            while not stop.wait(0.2):
                if self.cancel_event.is_set():
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    return

        threading.Thread(target=watchdog, daemon=True).start()
        tail = deque(maxlen=12)
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                match = re.match(r"out_time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
                if match:
                    if duration:
                        seconds = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
                        self._progress(sub_base + sub_span * min(1.0, seconds / duration))
                    continue
                if re.match(r"^[a-z_0-9]+=", line):
                    continue  # other -progress key=value lines
                tail.append(line)
            code = proc.wait()
        finally:
            stop.set()
        self._check_cancel()
        if code != 0:
            raise RuntimeError(" | ".join(tail) or f"process exited with code {code}")

    def _probe(self, path):
        cmd = [self.ffmpeg(), "-hide_banner", "-nostdin", "-i", path]
        result = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
            errors="replace", creationflags=CREATE_NO_WINDOW)
        text = result.stderr or ""
        info = {"duration": None, "has_audio": False, "has_video": False,
                "has_alpha": False, "width": None, "height": None, "fps": None}
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if match:
            info["duration"] = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
        for line in text.splitlines():
            if "Stream #" not in line:
                continue
            if "Audio:" in line:
                info["has_audio"] = True
            elif "Video:" in line and "attached pic" not in line:
                info["has_video"] = True
                if re.search(r"\b(rgba|bgra|argb|abgr|ya8|ya16le|ya16be|pal8|yuva\w*|gbrap\w*|rgba64\w*|bgra64\w*)\b", line):
                    info["has_alpha"] = True
                size = re.search(r",\s*(\d{2,5})x(\d{2,5})\b", line)
                if size:
                    info["width"], info["height"] = int(size.group(1)), int(size.group(2))
                fps = re.search(r"(\d+(?:\.\d+)?)\s*fps", line)
                if fps:
                    info["fps"] = float(fps.group(1))
        return info

    @staticmethod
    def _scale_filter(scale):
        if scale >= 0.999:
            return None
        return (f"scale='max(2,trunc(iw*{scale:.4f}/2)*2)':'max(2,trunc(ih*{scale:.4f}/2)*2)'")

    # ----- public entry point ---------------------------------------------

    def compress_item(self, path, dest_parent=None):
        """Compress a file or folder.

        The result is placed in `dest_parent` (default: next to the original) and
        is named <original name>_<YYYYMMDD_HHMMSS>. Returns a JobResult.
        """
        path = os.path.abspath(path)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.warnings = []
        if os.path.isfile(path):
            dest_dir = dest_parent or os.path.dirname(path)
            self._base, self._span = 0.0, 1.0
            final, original, new = self._process_file(
                path, dest_dir, f"{Path(path).stem}_{stamp}", allow_generic=True)
            self.report(1.0)
            return JobResult(final, original, new, list(self.warnings))
        if os.path.isdir(path):
            parent = dest_parent or os.path.dirname(os.path.normpath(path))
            name = display_name(path).rstrip(":\\/") or "Folder"
            out_dir = unique_path(os.path.join(parent, f"{name}_{stamp}"), is_dir=True)
            os.makedirs(out_dir)
            try:
                original, new = self._process_folder(path, out_dir)
            except BaseException:
                shutil.rmtree(out_dir, ignore_errors=True)  # never leave a half-finished folder
                raise
            self.report(1.0)
            return JobResult(out_dir, original, new, list(self.warnings))
        raise FileNotFoundError(f"Path not found: {path}")

    # ----- folders and files -----------------------------------------------

    def _process_folder(self, src, out_dir):
        self.log(f"Processing folder: {display_name(src)}")
        entries = []
        total_weight = 0
        for root, _dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            target_dir = out_dir if rel == "." else os.path.join(out_dir, rel)
            os.makedirs(target_dir, exist_ok=True)
            for name in files:
                full = os.path.join(root, name)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                weight = max(size, 1)
                entries.append((full, target_dir, name, weight))
                total_weight += weight
        done = 0
        original_total = new_total = 0
        for full, target_dir, name, weight in entries:
            self._check_cancel()
            self._base = done / total_weight
            self._span = weight / total_weight
            _final, original, new = self._process_file(full, target_dir, Path(name).stem, allow_generic=False)
            original_total += original
            new_total += new
            done += weight
        return original_total, new_total

    @staticmethod
    def _kind(ext):
        if ext in VIDEO_EXTS:
            return "video"
        if ext in AUDIO_EXTS:
            return "audio"
        if ext in IMAGE_EXTS:
            return "image"
        if ext == ".pdf":
            return "pdf"
        if ext in OFFICE_EXTS:
            return "office"
        return None

    def _process_file(self, src, dest_dir, out_stem, allow_generic):
        ext = Path(src).suffix.lower()
        name = os.path.basename(src)
        original = os.path.getsize(src)
        kind = self._kind(ext)
        work = tempfile.mkdtemp(prefix="fc_work_")
        try:
            self._check_cancel()
            self.log(f"Processing: {name} ({format_size(original)})")
            produced = None
            if original > 0:
                try:
                    if kind == "video":
                        produced = self._compress_video(src, work, original)
                    elif kind == "audio":
                        produced = self._compress_audio(src, work, original)
                    elif kind == "image":
                        produced = self._compress_image(src, work, original * self.keep)
                    elif kind == "pdf":
                        produced = self._compress_pdf(src, work, original * self.keep)
                    elif kind == "office":
                        produced = self._compress_office(src, work)
                    elif allow_generic:
                        produced = self._compress_generic(src, work)
                except (CancelledError, ToolMissingError):
                    raise
                except Exception as exc:
                    self._warn(f"{name}: compression failed ({exc}). An unchanged copy was kept.")
                    produced = None
                else:
                    if produced and os.path.getsize(produced) >= original:
                        self._warn(f"{name}: could not be made smaller. An unchanged copy was kept.")
                        produced = None

            os.makedirs(dest_dir, exist_ok=True)
            if produced:
                final = unique_path(os.path.join(dest_dir, out_stem + Path(produced).suffix.lower()))
                shutil.move(produced, final)
            else:
                final = unique_path(os.path.join(dest_dir, out_stem + Path(src).suffix))
                shutil.copy2(src, final)
            new = os.path.getsize(final)
            if produced:
                achieved = (1.0 - new / original) * 100.0
                if achieved < self.percent * 0.6:
                    self._warn(f"{name}: reduced by {achieved:.0f}% (requested {self.percent}%).")
                self.log(f"Completed: {os.path.basename(final)} ({format_size(new)}, {achieved:.1f}% smaller)")
            elif kind is None and not allow_generic:
                self.log(f"Copied: {name}")
            self._progress(1.0)
            return final, original, new
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ----- video -------------------------------------------------------------

    def _compress_video(self, src, work, original):
        info = self._probe(src)
        out = os.path.join(work, "video_out.mp4")
        duration = info["duration"]
        encode = ["-map", "0:v:0", "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]

        if not duration:  # unknown length: fall back to a quality based encode
            crf = str(int(round(20 + self.percent * 0.2)))
            cmd = self._ff_cmd(True) + ["-i", src] + encode + [
                "-crf", crf, "-map", "0:a:0?", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", out]
            self._run(cmd)
            return out

        total_bps = original * self.keep * 8 / duration * 0.96   # 4% container overhead
        audio_bps = min(128000, max(32000, total_bps * 0.12)) if info["has_audio"] else 0
        video_bps = total_bps - audio_bps
        if video_bps < 60000:
            video_bps = 60000
            self._warn(f"{os.path.basename(src)}: the requested reduction is very high; "
                       "quality will be low and the target may not be reached.")

        vf = []
        width, height = info["width"], info["height"]
        if width and height:
            fps = min(60.0, max(1.0, info["fps"] or 30.0))
            bits_per_pixel = video_bps / (width * height * fps)
            if bits_per_pixel < 0.03:   # too few bits for this resolution: shrink the picture a little
                scale = max(0.25, math.sqrt(bits_per_pixel / 0.05))
                vf = ["-vf", self._scale_filter(scale)]

        video_args = encode + vf + ["-b:v", f"{max(1, int(video_bps / 1000))}k",
                                    "-passlogfile", "fc_pass"]
        first = self._ff_cmd(True) + ["-i", src] + video_args + [
            "-pass", "1", "-an", "-f", "null", os.devnull]
        self._run(first, cwd=work, duration=duration, sub_base=0.0, sub_span=0.4)

        audio_args = ["-an"]
        if audio_bps:
            audio_args = ["-map", "0:a:0?", "-c:a", "aac", "-b:a", f"{int(audio_bps / 1000)}k"]
        second = self._ff_cmd(True) + ["-i", src] + video_args + ["-pass", "2"] + audio_args + [
            "-movflags", "+faststart", out]
        self._run(second, cwd=work, duration=duration, sub_base=0.4, sub_span=0.6)
        return out

    # ----- audio -------------------------------------------------------------

    def _compress_audio(self, src, work, original):
        info = self._probe(src)
        if info["duration"]:
            target_kbps = original * self.keep * 8 / info["duration"] / 1000 * 0.97
        else:
            target_kbps = 192 * self.keep
        fitting = [rate for rate in MP3_RATES if rate <= target_kbps]
        if fitting:
            kbps = fitting[-1]
        else:
            kbps = MP3_RATES[0]
            self._warn(f"{os.path.basename(src)}: the requested size is below the MP3 minimum; using 32 kbps.")
        out = os.path.join(work, "audio_out.mp3")
        cmd = self._ff_cmd(True) + ["-i", src, "-vn", "-map", "0:a:0",
                                    "-c:a", "libmp3lame", "-b:a", f"{kbps}k"]
        if kbps < 64:
            cmd += ["-ac", "1"]
        cmd.append(out)
        self._run(cmd, duration=info["duration"])
        return out

    # ----- images ------------------------------------------------------------

    def _attempt_jpeg(self, src, out, quality, scale):
        cmd = self._ff_cmd() + ["-i", src, "-frames:v", "1"]
        vf = self._scale_filter(scale)
        if vf:
            cmd += ["-vf", vf]
        cmd += ["-q:v", str(quality), "-pix_fmt", "yuvj420p", out]
        self._run(cmd)
        return os.path.getsize(out)

    def _attempt_png(self, src, out, scale):
        vf = self._scale_filter(scale)
        chain = (vf + "," if vf else "") + "split[a][b];[a]palettegen[p];[b][p]paletteuse"
        cmd = self._ff_cmd() + ["-i", src, "-frames:v", "1", "-vf", chain, out]
        self._run(cmd)
        return os.path.getsize(out)

    def _compress_image(self, src, work, target_bytes, keep_format=False):
        """Return a compressed copy of an image no larger than target_bytes when possible.

        Photos become JPEG. Images with transparency (or PNGs inside Office files,
        where the file name must not change) stay PNG and are reduced by palette
        quantisation and, if needed, by shrinking the picture.
        """
        info = self._probe(src)
        as_png = info["has_alpha"] or (keep_format and Path(src).suffix.lower() == ".png")
        suffix = ".png" if as_png else ".jpg"
        tmp = os.path.join(work, "img_try" + suffix)
        best = os.path.join(work, "img_best" + suffix)
        target = max(1, int(target_bytes))

        if not as_png:
            size = self._attempt_jpeg(src, tmp, 31, 1.0)
            if size <= target:
                shutil.copyfile(tmp, best)
                low, high = 2, 31          # `high` is known to fit; look for the best quality that fits
                while low < high:
                    self._check_cancel()
                    mid = (low + high) // 2
                    if self._attempt_jpeg(src, tmp, mid, 1.0) <= target:
                        shutil.copyfile(tmp, best)
                        high = mid
                    else:
                        low = mid + 1
                return best
            scale = max(0.1, math.sqrt(target / size) * 0.95)   # even the lowest quality is too big
            best_size = None
            for _ in range(6):
                size = self._attempt_jpeg(src, tmp, 24, scale)
                if best_size is None or size < best_size:
                    shutil.copyfile(tmp, best)
                    best_size = size
                if size <= target or scale <= 0.1:
                    break
                scale = max(0.1, scale * math.sqrt(target / size) * 0.92)
            return best

        scale, best_size = 1.0, None
        for _ in range(6):
            size = self._attempt_png(src, tmp, scale)
            if best_size is None or size < best_size:
                shutil.copyfile(tmp, best)
                best_size = size
            if size <= target or scale <= 0.1:
                break
            scale = max(0.1, scale * math.sqrt(target / max(size, 1)) * 0.95)
        return best

    # ----- PDF, Office, everything else -------------------------------------------

    def _compress_pdf(self, src, work, target_bytes):
        if self._gs is None:
            self._gs = find_ghostscript() or ""
        if not self._gs:
            self._warn(f"{os.path.basename(src)}: Ghostscript was not found, so the PDF could not be compressed.")
            return None
        shutil.copyfile(src, os.path.join(work, "in.pdf"))   # ASCII file names avoid Unicode path problems
        presets = ["/printer", "/ebook", "/screen"]
        start = 0 if self.percent <= 30 else (1 if self.percent <= 60 else 2)
        variants = [(preset, []) for preset in presets[start:]]
        variants.append(("/screen", ["-dColorImageResolution=48", "-dGrayImageResolution=48",
                                     "-dMonoImageResolution=96"]))
        best, best_size = None, None
        for index, (preset, extra) in enumerate(variants):
            self._check_cancel()
            self._progress(index / len(variants))
            out_name = f"pdf_{index}.pdf"
            cmd = [self._gs, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4", f"-dPDFSETTINGS={preset}",
                   "-dNOPAUSE", "-dQUIET", "-dBATCH"] + extra + [f"-sOutputFile={out_name}", "in.pdf"]
            self._run(cmd, cwd=work)
            out = os.path.join(work, out_name)
            size = os.path.getsize(out)
            if best_size is None or size < best_size:
                best, best_size = out, size
            if size <= target_bytes:
                break
        return best

    def _compress_office(self, src, work):
        """Recompress the pictures embedded in a docx/xlsx/pptx and re-zip the package."""
        out = os.path.join(work, "office_out" + Path(src).suffix.lower())
        media = re.compile(r"^(word|xl|ppt)/media/.+\.(jpe?g|png)$", re.IGNORECASE)
        with zipfile.ZipFile(src) as zin, \
                zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
            items = zin.infolist()
            for index, item in enumerate(items):
                self._check_cancel()
                self._progress(index / max(1, len(items)))
                data = zin.read(item.filename)
                if media.match(item.filename) and len(data) > 20000:
                    tmp_in = os.path.join(work, "media_in" + Path(item.filename).suffix.lower())
                    with open(tmp_in, "wb") as handle:
                        handle.write(data)
                    try:
                        new_path = self._compress_image(tmp_in, work, len(data) * self.keep, keep_format=True)
                        with open(new_path, "rb") as handle:
                            new_data = handle.read()
                        if len(new_data) < len(data):
                            data = new_data
                    except (CancelledError, ToolMissingError):
                        raise
                    except Exception:
                        pass   # keep the original picture
                info = zipfile.ZipInfo(item.filename, date_time=item.date_time)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = item.external_attr
                zout.writestr(info, data, compresslevel=9)
        return out

    def _compress_generic(self, src, work):
        out = os.path.join(work, "generic_out.zip")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            archive.write(src, arcname=os.path.basename(src))
        return out


class CompressionWorker(QThread):
    """Runs one compression job on a background thread."""
    message = pyqtSignal(str)
    progress = pyqtSignal(float)

    def __init__(self, path, percent, dest_parent=None, parent=None):
        super().__init__(parent)
        self.path = path
        self.percent = percent
        self.dest_parent = dest_parent
        self.cancel_event = threading.Event()
        self.result = None
        self.error = None
        self.was_cancelled = False

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        engine = CompressionEngine(
            self.percent, log=self.message.emit, progress=self.progress.emit,
            cancel_event=self.cancel_event)
        try:
            self.result = engine.compress_item(self.path, self.dest_parent)
        except CancelledError:
            self.was_cancelled = True
        except ToolMissingError as exc:
            self.error = str(exc)
        except PermissionError as exc:
            self.error = f"Permission denied: {exc.filename or exc}"
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__


# --------------------------------------------------------------------------- #
# Right-click menu registration (Windows registry)
# --------------------------------------------------------------------------- #

class ContextMenuManager:
    """Adds/removes the cascading right-click menu for extensions and folders.

    Entries are written per user (HKEY_CURRENT_USER), so no administrator rights
    are needed. They appear in the classic Windows menu and, on Windows 11, under
    "Show more options" (or directly if the classic menu is switched on).
    """

    SFA_PATH = r"Software\Classes\SystemFileAssociations"
    FOLDER_BASE = rf"Software\Classes\Directory\shell\{VERB_KEY}"
    CLASSIC_CLSID = r"Software\Classes\CLSID\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}"

    available = winreg is not None

    # ----- command line stored in the registry --------------------------------

    @staticmethod
    def build_command(percent):
        if getattr(sys, "frozen", False):
            program = f'"{sys.executable}"'
        else:
            python = sys.executable
            windowless = os.path.join(os.path.dirname(python), "pythonw.exe")
            if os.path.isfile(windowless):
                python = windowless
            program = f'"{python}" "{os.path.abspath(__file__)}"'
        return f'{program} --compress "%1" --percent {percent}'

    @staticmethod
    def icon_value():
        icon = find_icon()
        if icon:
            return icon
        if getattr(sys, "frozen", False):
            return f"{sys.executable},0"
        return None

    # ----- registry plumbing --------------------------------------------------

    @staticmethod
    def _delete_tree(path):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ)
        except OSError:
            return
        with key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                ContextMenuManager._delete_tree(path + "\\" + child)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)

    @staticmethod
    def _delete_if_empty(path):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
                subkeys, values, _ = winreg.QueryInfoKey(key)
            if subkeys == 0 and values == 0:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            pass

    @staticmethod
    def _write_menu(base):
        hive = winreg.HKEY_CURRENT_USER
        with winreg.CreateKeyEx(hive, base, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, "MUIVerb", 0, winreg.REG_SZ, MENU_TITLE)
            winreg.SetValueEx(key, "SubCommands", 0, winreg.REG_SZ, "")
            icon = ContextMenuManager.icon_value()
            if icon:
                winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon)
        for percent in MENU_PERCENTS:
            sub = rf"{base}\shell\p{percent:02d}"
            winreg.SetValue(hive, sub, winreg.REG_SZ, f"Compress by {percent}%")
            winreg.SetValue(hive, sub + r"\command", winreg.REG_SZ,
                            ContextMenuManager.build_command(percent))

    # ----- file extensions ----------------------------------------------------

    @staticmethod
    def installed_extensions():
        found = set()
        if winreg is None:
            return found
        try:
            root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, ContextMenuManager.SFA_PATH)
        except OSError:
            return found
        with root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                if name.startswith("."):
                    try:
                        winreg.CloseKey(winreg.OpenKey(root, rf"{name}\shell\{VERB_KEY}"))
                        found.add(name.lower())
                    except OSError:
                        pass
        return found

    @staticmethod
    def install_extension(ext):
        ContextMenuManager._write_menu(rf"{ContextMenuManager.SFA_PATH}\{ext}\shell\{VERB_KEY}")

    @staticmethod
    def remove_extension(ext):
        base = rf"{ContextMenuManager.SFA_PATH}\{ext}"
        ContextMenuManager._delete_tree(rf"{base}\shell\{VERB_KEY}")
        ContextMenuManager._delete_if_empty(rf"{base}\shell")
        ContextMenuManager._delete_if_empty(base)

    # ----- folders --------------------------------------------------------------

    @staticmethod
    def folder_installed():
        if winreg is None:
            return False
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, ContextMenuManager.FOLDER_BASE))
            return True
        except OSError:
            return False

    @staticmethod
    def install_folder():
        ContextMenuManager._write_menu(ContextMenuManager.FOLDER_BASE)

    @staticmethod
    def remove_folder():
        ContextMenuManager._delete_tree(ContextMenuManager.FOLDER_BASE)

    # ----- Windows 11 -----------------------------------------------------------

    @staticmethod
    def is_windows11():
        return sys.platform == "win32" and sys.getwindowsversion().build >= 22000

    @staticmethod
    def classic_menu_enabled():
        if winreg is None:
            return False
        try:
            winreg.CloseKey(winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, ContextMenuManager.CLASSIC_CLSID + r"\InprocServer32"))
            return True
        except OSError:
            return False

    @staticmethod
    def set_classic_menu(enabled):
        if enabled:
            winreg.SetValue(winreg.HKEY_CURRENT_USER,
                            ContextMenuManager.CLASSIC_CLSID + r"\InprocServer32", winreg.REG_SZ, "")
        else:
            ContextMenuManager._delete_tree(ContextMenuManager.CLASSIC_CLSID)

    @staticmethod
    def restart_explorer():
        flags = CREATE_NO_WINDOW
        subprocess.run(["taskkill", "/f", "/im", "explorer.exe"], creationflags=flags,
                       capture_output=True)
        time.sleep(2.0)
        running = subprocess.run(["tasklist", "/FI", "IMAGENAME eq explorer.exe"],
                                 creationflags=flags, capture_output=True, text=True, errors="replace")
        if "explorer.exe" not in running.stdout.lower():
            subprocess.Popen(["explorer.exe"])

    @staticmethod
    def notify_shell():
        try:
            import ctypes
            ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Right-click mode: single instance, job queue and progress window
# --------------------------------------------------------------------------- #

def send_job_to_running_instance(path, percent):
    """If another instance is already collecting jobs, hand the job over to it."""
    socket = QLocalSocket()
    socket.connectToServer(IPC_NAME)
    if not socket.waitForConnected(400):
        return False
    payload = json.dumps({"path": path, "percent": percent}).encode("utf-8") + b"\n"
    socket.write(payload)
    socket.flush()
    socket.waitForBytesWritten(2000)
    socket.disconnectFromServer()
    if socket.state() != QLocalSocket.UnconnectedState:
        socket.waitForDisconnected(1000)
    return True


class JobServer(QObject):
    job_received = pyqtSignal(str, int)

    def __init__(self):
        super().__init__()
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._on_connection)
        self._buffers = {}

    def listen(self):
        try:
            self.server.setSocketOptions(QLocalServer.UserAccessOption)
        except Exception:
            pass
        if self.server.listen(IPC_NAME):
            return True
        if sys.platform != "win32":      # remove a stale socket file (never needed on Windows)
            QLocalServer.removeServer(IPC_NAME)
            return self.server.listen(IPC_NAME)
        return False

    def close(self):
        self.server.close()

    def _on_connection(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            key = id(socket)
            self._buffers[key] = b""
            socket.readyRead.connect(lambda s=socket, k=key: self._read(s, k, False))
            socket.disconnected.connect(lambda s=socket, k=key: self._read(s, k, True))
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                self._read(socket, key, False)

    def _read(self, socket, key, final):
        if key not in self._buffers:
            return
        self._buffers[key] += bytes(socket.readAll())
        *lines, rest = self._buffers[key].split(b"\n")
        self._buffers[key] = rest
        if final:
            if rest.strip():
                lines.append(rest)
            self._buffers.pop(key, None)
        for line in lines:
            try:
                data = json.loads(line.decode("utf-8"))
                self.job_received.emit(str(data["path"]), int(data["percent"]))
            except (ValueError, KeyError, TypeError):
                continue


class JobRow(QFrame):
    def __init__(self, path, percent):
        super().__init__()
        self.path = path
        self.output_path = None
        self.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)

        top = QHBoxLayout()
        self.name_label = QLabel()
        self.name_label.setStyleSheet("font-weight: bold;")
        self.name_label.setToolTip(path)
        self.name_label.setText(QFontMetrics(self.name_label.font()).elidedText(
            display_name(path), Qt.ElideMiddle, 340))
        top.addWidget(self.name_label, 1)
        badge = QLabel(f"Compress by {percent}%")
        badge.setStyleSheet("color: #666;")
        top.addWidget(badge)
        layout.addLayout(top)

        self.status = QLabel("Waiting...")
        self.status.setStyleSheet("color: #666;")
        layout.addWidget(self.status)

        bottom = QHBoxLayout()
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setFixedHeight(16)
        bottom.addWidget(self.bar, 1)
        self.show_button = QPushButton("Show in folder")
        self.show_button.setVisible(False)
        self.show_button.clicked.connect(lambda: reveal_in_explorer(self.output_path))
        bottom.addWidget(self.show_button)
        layout.addLayout(bottom)

    def _set_status(self, text, color="#666"):
        self.status.setStyleSheet(f"color: {color};")
        self.status.setText(QFontMetrics(self.status.font()).elidedText(text, Qt.ElideMiddle, 540))

    def set_running(self):
        self._set_status("Compressing...", "#1565C0")

    def set_message(self, text):
        self._set_status(text, "#E65100" if text.startswith("Warning") else "#1565C0")

    def set_progress(self, fraction):
        self.bar.setValue(int(fraction * 100))

    def set_success(self, result):
        self.output_path = result.output_path
        self.bar.setValue(100)
        text = (f"Done - {result.reduction:.1f}% smaller ({format_size(result.new_size)}): "
                f"{os.path.basename(result.output_path)}")
        if result.warnings:
            text += f"  [{len(result.warnings)} warning(s)]"
        self._set_status(text, "#E65100" if result.warnings else "#2E7D32")
        tip = [f"{format_size(result.original_size)} → {format_size(result.new_size)}"] + result.warnings
        self.status.setToolTip("\n".join(tip))
        self.show_button.setVisible(True)

    def set_failed(self, message):
        self._set_status(f"Failed: {message}", "#C62828")
        self.status.setToolTip(message)

    def set_cancelled(self):
        self._set_status("Cancelled", "#666")


class JobWindow(QWidget):
    """Small always-on-top window that shows the right-click compression jobs."""

    def __init__(self):
        super().__init__(None, Qt.Window | Qt.WindowStaysOnTopHint)
        self.setWindowTitle(APP_NAME)
        icon = find_icon()
        if icon:
            self.setWindowIcon(QIcon(icon))
        self.setFixedWidth(600)

        self.pending = deque()
        self.worker = None
        self.current_row = None
        self.total = self.completed = self.failed = self.cancelled = 0
        self.auto_close = QTimer(self)
        self.auto_close.setSingleShot(True)
        self.auto_close.setInterval(5000)
        self.auto_close.timeout.connect(self.close)

        outer = QVBoxLayout(self)
        self.header = QLabel("Preparing...")
        self.header.setStyleSheet("font-size: 14px; font-weight: bold;")
        outer.addWidget(self.header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        holder = QWidget()
        self.rows_layout = QVBoxLayout(holder)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(6)
        self.rows_layout.addStretch(1)
        self.scroll.setWidget(holder)
        outer.addWidget(self.scroll)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel All")
        self.cancel_button.clicked.connect(self.cancel_all)
        buttons.addWidget(self.cancel_button)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.close_button)
        outer.addLayout(buttons)

    # ----- queue ------------------------------------------------------------------

    def add_job(self, path, percent):
        row = JobRow(path, percent)
        self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)
        self.pending.append((path, percent, row))
        self.total += 1
        self.auto_close.stop()
        self.cancel_button.setVisible(True)
        if not self.isVisible():
            self.show()
        self._refresh()
        self.raise_()
        self.activateWindow()
        if self.worker is None:
            self._start_next()

    def _start_next(self):
        if not self.pending:
            self._all_done()
            return
        path, percent, row = self.pending.popleft()
        self.current_row = row
        row.set_running()
        worker = CompressionWorker(path, percent)
        worker.message.connect(row.set_message)
        worker.progress.connect(row.set_progress)
        worker.finished.connect(self._on_worker_finished)
        self.worker = worker
        self._refresh()
        worker.start()

    def _on_worker_finished(self):
        worker, row = self.worker, self.current_row
        worker.wait()
        self.worker = self.current_row = None
        if worker.result is not None:
            row.set_success(worker.result)
            self.completed += 1
        elif worker.was_cancelled:
            row.set_cancelled()
            self.cancelled += 1
        else:
            row.set_failed(worker.error or "Unknown error")
            self.failed += 1
        worker.deleteLater()
        self._start_next()

    def cancel_all(self):
        while self.pending:
            _path, _percent, row = self.pending.popleft()
            row.set_cancelled()
            self.cancelled += 1
        if self.worker is not None:
            self.worker.cancel()

    def _all_done(self):
        self.cancel_button.setVisible(False)
        if self.failed:
            self.header.setText(f"Finished with {self.failed} error(s)")
        elif self.cancelled and not self.completed:
            self.header.setText("Cancelled")
        else:
            self.header.setText("All done")
        self._refresh()
        if not self.failed:
            self.auto_close.start()

    # ----- layout -----------------------------------------------------------------

    def _refresh(self):
        if self.worker is not None:
            finished = self.completed + self.failed + self.cancelled
            if self.total > 1:
                self.header.setText(f"Compressing... ({finished + 1} of {self.total})")
            else:
                self.header.setText("Compressing...")
        rows = self.rows_layout.count() - 1
        self.scroll.setFixedHeight(min(max(rows, 1), 4) * 84)
        self.adjustSize()
        area = QApplication.primaryScreen().availableGeometry()
        self.move(area.right() - self.width() - 16, area.bottom() - self.height() - 16)

    def closeEvent(self, event):
        if self.worker is not None or self.pending:
            answer = QMessageBox.question(
                self, "Cancel compression?",
                "Compression is still running. Cancel it and exit?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.cancel_all()
            if self.worker is not None:
                self.worker.wait(15000)
        event.accept()


def create_app():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    icon = find_icon()
    if icon:
        app.setWindowIcon(QIcon(icon))
    return app


def run_context_job(path, percent):
    """Entry point used by the right-click menu: no main window, just a progress window."""
    app = create_app()
    server = JobServer()
    deadline = time.time() + 5.0
    is_server = False
    while time.time() < deadline:
        if send_job_to_running_instance(path, percent):
            return 0                      # another instance will process this file
        if server.listen():
            is_server = True
            break
        time.sleep(0.15)                  # another instance is starting up; try again
    window = JobWindow()
    if is_server:
        server.job_received.connect(window.add_job)
        app.aboutToQuit.connect(server.close)
    window.add_job(path, percent)
    return app.exec_()


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #

class UpdateChecker(QObject):
    """Checks GitHub for a newer release in the background and never blocks or crashes the UI."""
    update_found = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._stopped = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stopped.set()

    def _run(self):
        tag = fetch_latest_release_tag()
        if tag and not self._stopped.is_set() and is_newer_version(tag, APP_VERSION):
            self.update_found.emit(version_label(tag))


class DropArea(QLabel):
    file_dropped = pyqtSignal(str)

    def __init__(self, text="Drop files here or click to browse"):
        super().__init__()
        self.setText(text)
        self.setAlignment(Qt.AlignCenter)
        self.setAcceptDrops(True)
        self.setStyleSheet("""
            QLabel {
                border: 2px dashed #aaa;
                border-radius: 10px;
                padding: 20px;
                background-color: #f9f9f9;
                font-size: 14px;
                color: #666;
            }
            QLabel:hover {
                border-color: #4CAF50;
                background-color: #f0f8f0;
            }
        """)
        self.setMinimumHeight(100)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
            self.setStyleSheet(self.styleSheet().replace("#f9f9f9", "#e8f5e8"))
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.setStyleSheet(self.styleSheet().replace("#e8f5e8", "#f9f9f9"))

    def dropEvent(self, event):
        files = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if files:
            self.choose(files[0])
        self.setStyleSheet(self.styleSheet().replace("#e8f5e8", "#f9f9f9"))

    def choose(self, path):
        self.file_dropped.emit(path)
        self.setText(f"Selected: {display_name(path)}")

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        menu = QMenu(self)
        pick_file = menu.addAction("Select File...")
        pick_folder = menu.addAction("Select Folder...")
        chosen = menu.exec_(event.globalPos())
        if chosen == pick_file:
            path, _ = QFileDialog.getOpenFileName(self, "Select File")
        elif chosen == pick_folder:
            path = QFileDialog.getExistingDirectory(self, "Select Folder")
        else:
            return
        if path:
            self.choose(path)


class DragOutArea(QLabel):
    """Shows the finished result. Drag it onto a folder or the desktop to save it there."""

    IDLE_TEXT = "Compressed files will appear here"

    def __init__(self):
        super().__init__(self.IDLE_TEXT)
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        self.setMinimumHeight(100)
        self.path = None
        self._press_pos = None
        self._apply_style(ready=False)

    def _apply_style(self, ready):
        hover = "QLabel:hover { border-color: #2196F3; background-color: #e8f2fc; }" if ready else ""
        border = "#2196F3" if ready else "#aaa"
        self.setStyleSheet(f"""
            QLabel {{
                border: 2px dashed {border};
                border-radius: 10px;
                padding: 20px;
                background-color: {"#f0f6fd" if ready else "#f9f9f9"};
                font-size: 14px;
                color: {"#1a4f8b" if ready else "#666"};
            }}
            {hover}
        """)
        self.setCursor(Qt.OpenHandCursor if ready else Qt.ArrowCursor)

    def clear_result(self):
        self.path = None
        self.setText(self.IDLE_TEXT)
        self._apply_style(ready=False)

    def set_result(self, result):
        self.path = result.output_path
        kind = "folder" if os.path.isdir(self.path) else "file"
        self.setText(
            f"Ready: {os.path.basename(self.path)}\n"
            f"{format_size(result.original_size)} → {format_size(result.new_size)} "
            f"({result.reduction:.1f}% smaller)\n"
            f"Drag this box onto a folder or the desktop to save the {kind} there")
        self._apply_style(ready=True)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.path:
            self._press_pos = event.pos()

    def mouseReleaseEvent(self, event):
        self._press_pos = None

    def mouseMoveEvent(self, event):
        if (not self.path or self._press_pos is None or not (event.buttons() & Qt.LeftButton)
                or (event.pos() - self._press_pos).manhattanLength() < QApplication.startDragDistance()):
            return
        self._press_pos = None
        if not os.path.exists(self.path):
            self.clear_result()
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(self.path)])
        drag = QDrag(self)
        drag.setMimeData(mime)
        pixmap = self._drag_pixmap()
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(pixmap.width() // 2, pixmap.height() // 2))
        drag.exec_(Qt.CopyAction, Qt.CopyAction)   # copy only: the original stays safe in the temp folder

    def _drag_pixmap(self):
        pixmap = QPixmap(240, 40)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(33, 150, 243, 225))
        painter.drawRoundedRect(pixmap.rect(), 8, 8)
        painter.setPen(Qt.white)
        text = QFontMetrics(painter.font()).elidedText(os.path.basename(self.path), Qt.ElideMiddle, 216)
        painter.drawText(pixmap.rect().adjusted(12, 0, -12, 0), Qt.AlignVCenter | Qt.AlignLeft, text)
        painter.end()
        return pixmap


class SettingsDialog(QDialog):
    """Add or remove the right-click compression menu for file types and folders."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings - Right-click Menu")
        self.resize(540, 640)
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Choose the file types and folders that get a right-click menu "
            f"\"{MENU_TITLE}\" with percentage options.\n"
            "Tick an item to add the menu, untick it to remove the menu, then click Apply.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        layout.addWidget(self.tree, 1)

        row = QHBoxLayout()
        select_all = QPushButton("Select All")
        select_all.clicked.connect(lambda: self._set_all(Qt.Checked))
        select_none = QPushButton("Select None")
        select_none.clicked.connect(lambda: self._set_all(Qt.Unchecked))
        add_custom = QPushButton("Add Custom Extension...")
        add_custom.clicked.connect(self.add_custom_extension)
        for button in (select_all, select_none, add_custom):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)

        self.classic_check = None
        if ContextMenuManager.available and ContextMenuManager.is_windows11():
            box = QGroupBox("Windows 11")
            box_layout = QVBoxLayout(box)
            self.classic_check = QCheckBox("Always show the full (classic) right-click menu")
            self.classic_check.setChecked(ContextMenuManager.classic_menu_enabled())
            note = QLabel(
                "Windows 11's new compact menu cannot list entries added this way. They always "
                "appear under \"Show more options\" (or press Shift+F10). Turn this option on to "
                "show the full menu straight away. It applies to all of Windows, needs Explorer "
                "to restart, and can be turned off here at any time.")
            note.setWordWrap(True)
            note.setStyleSheet("color: #555;")
            box_layout.addWidget(self.classic_check)
            box_layout.addWidget(note)
            layout.addWidget(box)

        footer = QLabel("The menu entries point to this program's current location. If you move the "
                        "program, open Settings and click Apply again.")
        footer.setWordWrap(True)
        footer.setStyleSheet("color: #555;")
        layout.addWidget(footer)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.apply_button = QPushButton("Apply")
        self.apply_button.setDefault(True)
        self.apply_button.clicked.connect(self.apply_changes)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(self.apply_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self.group_items = []
        self.folder_item = None
        self.custom_parent = None
        self._populate()

        if not ContextMenuManager.available:
            self.tree.setEnabled(False)
            for button in (select_all, select_none, add_custom, self.apply_button):
                button.setEnabled(False)
            intro.setText("Right-click menu settings are only available on Windows.")

    # ----- tree ---------------------------------------------------------------------

    def _populate(self):
        installed = ContextMenuManager.installed_extensions()
        known = set()
        for group, extensions in FORMAT_GROUPS.items():
            parent = QTreeWidgetItem(self.tree, [group])
            parent.setFlags(parent.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
            parent.setCheckState(0, Qt.Unchecked)
            for ext in extensions:
                child = QTreeWidgetItem(parent, [ext])
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Checked if ext in installed else Qt.Unchecked)
                known.add(ext)
            parent.setExpanded(True)
            self.group_items.append(parent)

        self.folder_item = QTreeWidgetItem(self.tree, ["Folders (right-click a folder)"])
        self.folder_item.setFlags(self.folder_item.flags() | Qt.ItemIsUserCheckable)
        self.folder_item.setCheckState(
            0, Qt.Checked if ContextMenuManager.folder_installed() else Qt.Unchecked)

        self.custom_parent = QTreeWidgetItem(self.tree, ["Custom extensions"])
        self.custom_parent.setExpanded(True)
        for ext in sorted(installed - known):
            self._add_custom_item(ext, checked=True)

    def _add_custom_item(self, ext, checked):
        child = QTreeWidgetItem(self.custom_parent, [ext])
        child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
        child.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
        return child

    def _all_items(self):
        for parent in self.group_items + [self.custom_parent]:
            for index in range(parent.childCount()):
                yield parent.child(index)

    def _set_all(self, state):
        for item in self._all_items():
            item.setCheckState(0, state)
        self.folder_item.setCheckState(0, state)

    def add_custom_extension(self):
        text, accepted = QInputDialog.getText(
            self, "Add Custom Extension",
            "File extension (for example .txt).\n"
            "Types the program cannot recompress are packed into a .zip file:")
        if not accepted:
            return
        ext = normalize_extension(text)
        if not ext:
            QMessageBox.warning(self, "Invalid Extension",
                                "Please enter an extension such as .txt (letters and digits only).")
            return
        for item in self._all_items():
            if item.text(0) == ext:
                item.setCheckState(0, Qt.Checked)
                self.tree.scrollToItem(item)
                return
        item = self._add_custom_item(ext, checked=True)
        self.tree.scrollToItem(item)

    # ----- applying -----------------------------------------------------------------

    def apply_changes(self):
        wanted = {item.text(0) for item in self._all_items() if item.checkState(0) == Qt.Checked}
        want_folder = self.folder_item.checkState(0) == Qt.Checked
        before = ContextMenuManager.installed_extensions()
        folder_before = ContextMenuManager.folder_installed()
        added = removed = 0
        classic_changed = False
        try:
            for ext in sorted(wanted):               # (re)write every wanted entry so paths stay current
                ContextMenuManager.install_extension(ext)
                added += ext not in before
            for ext in sorted(before - wanted):
                ContextMenuManager.remove_extension(ext)
                removed += 1
            if want_folder:
                ContextMenuManager.install_folder()
                added += not folder_before
            elif folder_before:
                ContextMenuManager.remove_folder()
                removed += 1
            if self.classic_check is not None:
                desired = self.classic_check.isChecked()
                if desired != ContextMenuManager.classic_menu_enabled():
                    ContextMenuManager.set_classic_menu(desired)
                    classic_changed = True
        except OSError as exc:
            QMessageBox.critical(self, "Error", f"Could not update the right-click menu:\n{exc}")
            return
        finally:
            ContextMenuManager.notify_shell()

        for index in reversed(range(self.custom_parent.childCount())):   # unticked custom entries disappear
            if self.custom_parent.child(index).checkState(0) != Qt.Checked:
                self.custom_parent.takeChild(index)

        QMessageBox.information(
            self, "Settings Applied",
            f"Right-click menu updated.\nAdded: {added}\nRemoved: {removed}\n"
            f"Active entries: {len(wanted) + int(want_folder)}")
        if classic_changed:
            answer = QMessageBox.question(
                self, "Restart Explorer?",
                "The Windows 11 menu style changes after Windows Explorer restarts.\n"
                "Restart it now? (Open Explorer windows and the taskbar will refresh briefly.)",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer == QMessageBox.Yes:
                ContextMenuManager.restart_explorer()


class FileCompressor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.input_path = ""
        self.percent = 50
        self.temp_dir = None
        self.worker = None
        self.update_checker = None
        self.flash_step = 0
        self.flash_timer = QTimer(self)
        self.flash_timer.setInterval(450)
        self.flash_timer.timeout.connect(self._flash_tick)
        self.init_ui()
        self.load_icon()
        self.center()
        atexit.register(self.cleanup_temp)
        QTimer.singleShot(1500, self.check_for_updates)   # after the window is up

    def center(self):
        """Center the window on the primary screen."""
        frame = self.frameGeometry()
        frame.moveCenter(QApplication.primaryScreen().availableGeometry().center())
        self.move(frame.topLeft())

    def init_ui(self):
        self.setWindowTitle(APP_TITLE)
        self.setGeometry(100, 100, 600, 680)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        header = QHBoxLayout()
        self.more_apps_label = QLabel(f'<a href="{MORE_APPS_URL}">More Apps from Alright Peaches Studio</a>')
        self.more_apps_label.setTextFormat(Qt.RichText)
        self.more_apps_label.setCursor(Qt.PointingHandCursor)
        self.more_apps_label.linkActivated.connect(self.open_url)
        header.addWidget(self.more_apps_label)
        header.addStretch(1)
        self.new_version_button = QPushButton("New Version")
        self.new_version_button.setToolTip(
            f"Current version: {APP_VERSION}. Open the releases page to look for updates.")
        self.new_version_button.clicked.connect(self.open_releases)
        header.addWidget(self.new_version_button)
        self.settings_button = QPushButton("Settings")
        self.settings_button.setToolTip("Add or remove the right-click compression menu")
        self.settings_button.clicked.connect(self.open_settings)
        header.addWidget(self.settings_button)
        layout.addLayout(header)

        input_group = QGroupBox("Input File/Folder")
        input_layout = QVBoxLayout(input_group)
        self.input_area = DropArea("Drop files or folders here or click to browse")
        self.input_area.file_dropped.connect(self.set_input_path)
        input_layout.addWidget(self.input_area)
        layout.addWidget(input_group)

        settings_group = QGroupBox("Compression Settings")
        settings_layout = QVBoxLayout(settings_group)
        row = QHBoxLayout()
        row.addWidget(QLabel("Compress by:"))
        self.compression_slider = QSlider(Qt.Horizontal)
        self.compression_slider.setRange(2, 18)          # 10% ... 90% in steps of 5
        self.compression_slider.setValue(self.percent // 5)
        self.compression_slider.setTickPosition(QSlider.TicksBelow)
        self.compression_slider.setTickInterval(2)
        self.compression_slider.valueChanged.connect(self.update_compression_level)
        row.addWidget(self.compression_slider)
        self.compression_label = QLabel()
        self.compression_label.setMinimumWidth(40)
        self.compression_label.setStyleSheet("font-weight: bold;")
        row.addWidget(self.compression_label)
        settings_layout.addLayout(row)
        self.compression_hint = QLabel()
        self.compression_hint.setStyleSheet("color: #666;")
        settings_layout.addWidget(self.compression_hint)
        layout.addWidget(settings_group)
        self.update_compression_level(self.compression_slider.value())

        buttons = QHBoxLayout()
        self.process_button = QPushButton("Start Compression")
        self.process_button.setEnabled(False)
        self.process_button.clicked.connect(self.start_compression)
        self.process_button.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 10px;
                font-size: 16px;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
        """)
        buttons.addWidget(self.process_button, 1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.setStyleSheet("padding: 10px; font-size: 16px;")
        self.cancel_button.clicked.connect(self.cancel_compression)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        output_group = QGroupBox("Output - Compressed Files")
        output_layout = QVBoxLayout(output_group)
        self.output_area = DragOutArea()
        output_layout.addWidget(self.output_area)
        self.save_as_button = QPushButton("Save As...")
        self.save_as_button.setToolTip("Alternative to dragging: choose where to save the result")
        self.save_as_button.setEnabled(False)
        self.save_as_button.clicked.connect(self.save_as)
        output_layout.addWidget(self.save_as_button, 0, Qt.AlignRight)
        layout.addWidget(output_group)

        feedback_group = QGroupBox("Feedback")
        feedback_layout = QVBoxLayout(feedback_group)
        self.feedback_area = QTextEdit()
        self.feedback_area.setReadOnly(True)
        self.feedback_area.setMaximumHeight(150)
        feedback_layout.addWidget(self.feedback_area)
        layout.addWidget(feedback_group)

    def load_icon(self):
        icon_path = find_icon()
        if icon_path:
            icon = QIcon(icon_path)
            self.setWindowIcon(icon)
            QApplication.instance().setWindowIcon(icon)

    # ----- actions ---------------------------------------------------------------------

    def open_settings(self):
        SettingsDialog(self).exec_()

    def open_url(self, url):
        """Open a web address in the default browser."""
        if not QDesktopServices.openUrl(QUrl(url)):
            self.add_feedback(f"Could not open your browser. Please visit: {url}")

    def open_releases(self):
        self._stop_flashing()
        self.open_url(RELEASES_URL)

    # ----- update notice ------------------------------------------------------------------

    def check_for_updates(self):
        self.update_checker = UpdateChecker()
        self.update_checker.update_found.connect(self.show_update_available)
        self.update_checker.start()

    def show_update_available(self, version):
        self.new_version_button.setToolTip(
            f"A new version ({version}) is available. You are using {APP_VERSION}. "
            "Click to open the download page.")
        self.add_feedback(f"A new version is available: {version} (you are using {APP_VERSION}).")
        self.flash_step = 0
        self.flash_timer.start()

    def _set_highlight(self, on):
        self.new_version_button.setStyleSheet("""
            QPushButton {
                background-color: #FF9800;
                color: white;
                font-weight: bold;
                border: 1px solid #E65100;
                border-radius: 3px;
                padding: 4px 10px;
            }
        """ if on else "")

    def _flash_tick(self):
        # on, off, on, off, on, off, on: four flashes, then it stays highlighted until clicked
        pattern = [True, False, True, False, True, False, True]
        self._set_highlight(pattern[self.flash_step])
        self.flash_step += 1
        if self.flash_step >= len(pattern):
            self.flash_timer.stop()

    def _stop_flashing(self):
        self.flash_timer.stop()
        self._set_highlight(False)

    def set_input_path(self, path):
        self.input_path = path
        self.process_button.setEnabled(not self.is_busy())
        self.add_feedback(f"Input selected: {path}")
        self.output_area.clear_result()
        self.save_as_button.setEnabled(False)

    def update_compression_level(self, value):
        self.percent = value * 5
        self.compression_label.setText(f"{self.percent}%")
        self.compression_hint.setText(
            f"The result will be about {100 - self.percent}% of the original size.")

    def is_busy(self):
        return self.worker is not None

    def start_compression(self):
        if not self.input_path or not os.path.exists(self.input_path):
            self.add_feedback("Error: the selected file or folder no longer exists.")
            return
        self.cleanup_temp()
        self.temp_dir = tempfile.mkdtemp(prefix="filecompressor_")
        self.output_area.clear_result()
        self.save_as_button.setEnabled(False)
        self.process_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.settings_button.setEnabled(False)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)

        self.worker = CompressionWorker(self.input_path, self.percent, dest_parent=self.temp_dir)
        self.worker.message.connect(self.add_feedback)
        self.worker.progress.connect(lambda fraction: self.progress_bar.setValue(int(fraction * 100)))
        self.worker.finished.connect(self.compression_finished)
        self.worker.start()

    def cancel_compression(self):
        if self.worker is not None:
            self.cancel_button.setEnabled(False)
            self.add_feedback("Cancelling...")
            self.worker.cancel()

    def compression_finished(self):
        worker = self.worker
        worker.wait()
        self.worker = None
        self.cancel_button.setEnabled(False)
        self.settings_button.setEnabled(True)
        self.process_button.setEnabled(True)
        self.progress_bar.setVisible(False)
        if worker.result is not None:
            result = worker.result
            self.add_feedback(
                f"Compression completed: {format_size(result.original_size)} → "
                f"{format_size(result.new_size)} ({result.reduction:.1f}% smaller).")
            self.output_area.set_result(result)
            self.save_as_button.setEnabled(True)
        elif worker.was_cancelled:
            self.add_feedback("Compression cancelled.")
        else:
            self.add_feedback(f"Error: {worker.error}")
        worker.deleteLater()

    def save_as(self):
        path = self.output_area.path
        if not path or not os.path.exists(path):
            return
        if os.path.isfile(path):
            target, _ = QFileDialog.getSaveFileName(self, "Save Compressed File",
                                                    os.path.basename(path), "All Files (*)")
            if target:
                shutil.copy2(path, target)
                self.add_feedback(f"Saved: {target}")
        else:
            folder = QFileDialog.getExistingDirectory(
                self, f"Select where to save '{os.path.basename(path)}'")
            if folder:
                target = unique_path(os.path.join(folder, os.path.basename(path)), is_dir=True)
                shutil.copytree(path, target)
                self.add_feedback(f"Saved: {target}")

    def add_feedback(self, message):
        self.feedback_area.append(message)
        self.feedback_area.verticalScrollBar().setValue(
            self.feedback_area.verticalScrollBar().maximum())

    def cleanup_temp(self):
        if self.temp_dir:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir = None

    def closeEvent(self, event):
        if self.update_checker is not None:
            self.update_checker.stop()
        self.flash_timer.stop()
        if self.worker is not None:
            self.worker.cancel()
            self.worker.wait(15000)
        self.cleanup_temp()
        event.accept()


# --------------------------------------------------------------------------- #

def main():
    install_excepthook()
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--compress")
    parser.add_argument("--percent", default="50")
    args, _unknown = parser.parse_known_args()

    if args.compress:
        try:
            percent = int(args.percent)
        except ValueError:
            percent = 50
        sys.exit(run_context_job(args.compress.strip().rstrip('"'), percent))

    app = create_app()
    window = FileCompressor()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
