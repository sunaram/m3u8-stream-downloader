#!/usr/bin/env python3
"""
m3u8_downloader.py - Download video/audio from an M3U8 HLS stream.

Features
--------
- Browser-mimicking request headers (Chrome/Windows UA by default)
- Auto-derived Referer from the M3U8 URL (overridable with --referer)
- Custom headers via --header "Name: Value" (repeatable)
- Master-playlist parsing: selects highest-bandwidth variant by default
- Audio-only mode (--audio-only)
- Parallel segment download with configurable workers (--workers)
- AES-128 segment decryption (requires pycryptodome)
- Progress bar via tqdm
- ffmpeg remux to .mp4 / .m4a (falls back to raw .ts if ffmpeg absent)
- Retry logic with exponential back-off on failed segments
- Automatic temp-directory cleanup (--keep-temp to preserve)

Usage
-----
    python m3u8_downloader.py <M3U8_URL> [OPTIONS]

    python m3u8_downloader.py https://example.com/stream/index.m3u8 -o video.mp4
    python m3u8_downloader.py https://example.com/stream/index.m3u8 \\
        -r https://example.com \\
        -H "Origin: https://example.com" \\
        --workers 8
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from pathlib import Path
from typing import Optional

import requests
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Optional AES-128 decryption support
# ---------------------------------------------------------------------------
try:
    from Crypto.Cipher import AES
    PYCRYPTODOME_AVAILABLE = True
except ImportError:
    PYCRYPTODOME_AVAILABLE = False

# ---------------------------------------------------------------------------
# Default browser-mimicking headers (Chrome 122, Windows 10)
# ---------------------------------------------------------------------------
DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
    "Sec-CH-UA": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def derive_referer(url: str) -> str:
    """Return scheme + host of *url* as a Referer value."""
    parsed = urllib.parse.urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def resolve_url(base: str, href: str) -> str:
    """Resolve *href* relative to *base*."""
    return urllib.parse.urljoin(base, href)


def make_session(headers: dict[str, str]) -> requests.Session:
    session = requests.Session()
    session.headers.update(headers)
    return session


def fetch_text(session: requests.Session, url: str, retries: int = 3) -> str:
    """GET *url* and return body as text, with retries."""
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc
            time.sleep(2 ** attempt)
    return ""  # unreachable


def fetch_bytes(session: requests.Session, url: str, retries: int = 5) -> bytes:
    """GET *url* and return raw bytes, with exponential back-off retries."""
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=60)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"Failed to fetch segment {url}: {exc}") from exc
            wait = 2 ** attempt
            print(f"\n  [retry {attempt + 1}/{retries - 1}] {url} - waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
    return b""  # unreachable

# ---------------------------------------------------------------------------
# M3U8 parsing
# ---------------------------------------------------------------------------

def parse_master_playlist(
    text: str,
    base_url: str,
    audio_only: bool,
    quality_index: int,
    resolution: Optional[str] = None,
) -> tuple[str, bool]:
    """
    Parse a master playlist.

    Returns
    -------
    (stream_url, is_audio_only)
    """
    lines = text.splitlines()

    # --- Try to honour audio-only via EXT-X-MEDIA audio renditions ---
    if audio_only:
        audio_uris: list[str] = []
        for line in lines:
            if line.startswith("#EXT-X-MEDIA") and 'TYPE=AUDIO' in line:
                m = re.search(r'URI="([^"]+)"', line)
                if m:
                    audio_uris.append(resolve_url(base_url, m.group(1)))
        if audio_uris:
            chosen = audio_uris[0]
            print(f"[playlist] Selected audio rendition: {chosen}")
            return chosen, True

    # --- Parse EXT-X-STREAM-INF variants ---
    # Each entry: (bandwidth, width_px, height_px, url)
    streams: list[tuple[int, int, int, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXT-X-STREAM-INF"):
            bw = 0
            width = 0
            height = 0
            m_bw = re.search(r'BANDWIDTH=(\d+)', line)
            if m_bw:
                bw = int(m_bw.group(1))
            m_res = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
            if m_res:
                width = int(m_res.group(1))
                height = int(m_res.group(2))
            # Next non-empty line is the stream URI
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                streams.append((bw, width, height, resolve_url(base_url, lines[j])))
            i = j + 1
        else:
            i += 1

    if not streams:
        # Might already be a media playlist - return as-is
        return base_url, False

    # Sort by bandwidth descending (keeps quality_index=0 meaning "best")
    streams.sort(key=lambda x: x[0], reverse=True)

    # Print all available variants for user reference
    print(f"[playlist] {len(streams)} variant(s) available:")
    for rank, (bw, w, h, url) in enumerate(streams):
        res_tag = f"{w}x{h}" if w and h else "audio"
        print(f"  [{rank}] {res_tag:>10}  bw={bw:>9,}  {url}")

    if audio_only:
        # Pick lowest bandwidth as a proxy for audio-only
        chosen_bw, chosen_w, chosen_h, chosen_url = streams[-1]
    elif resolution:
        # Parse requested height: "720p", "720", "1080p", "1080" etc.
        req_h = int(re.sub(r'[^\d]', '', resolution))
        # Pick the variant whose height is closest; tiebreak by highest bandwidth
        best = min(streams, key=lambda x: (abs(x[2] - req_h), -x[0]))
        chosen_bw, chosen_w, chosen_h, chosen_url = best
        if chosen_h != req_h:
            print(f"[playlist] Exact {resolution} not found; "
                  f"closest match: {chosen_w}x{chosen_h} (bw={chosen_bw:,})")
    else:
        idx = max(0, min(quality_index, len(streams) - 1))
        chosen_bw, chosen_w, chosen_h, chosen_url = streams[idx]

    res_tag = f"{chosen_w}x{chosen_h}" if chosen_w and chosen_h else "?"
    print(f"[playlist] Selected: {res_tag}  bw={chosen_bw:,}  {chosen_url}")
    return chosen_url, False


def parse_media_playlist(text: str, base_url: str) -> tuple[list[str], Optional[dict], Optional[str]]:
    """
    Parse a media playlist.

    Returns
    -------
    (segment_urls, encryption_info | None, init_segment_url | None)

    encryption_info = {"method": "AES-128", "uri": "...", "iv": bytes | None}
    init_segment_url is the URL from #EXT-X-MAP, required for fMP4 streams.
    """
    lines = text.splitlines()
    segments: list[str] = []
    encryption: Optional[dict] = None
    init_url: Optional[str] = None

    for line in lines:
        line = line.strip()
        if line.startswith("#EXT-X-MAP"):
            # e.g. #EXT-X-MAP:URI="init.mp4" or #EXT-X-MAP:URI="init.mp4",BYTERANGE="..."
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                init_url = resolve_url(base_url, m.group(1))
        elif line.startswith("#EXT-X-KEY"):
            method_m = re.search(r'METHOD=([^,\s]+)', line)
            uri_m = re.search(r'URI="([^"]+)"', line)
            iv_m = re.search(r'IV=0x([0-9a-fA-F]+)', line)
            if method_m and uri_m:
                iv_bytes: Optional[bytes] = None
                if iv_m:
                    hex_str = iv_m.group(1).zfill(32)
                    iv_bytes = bytes.fromhex(hex_str)
                encryption = {
                    "method": method_m.group(1),
                    "uri": resolve_url(base_url, uri_m.group(1)),
                    "iv": iv_bytes,
                }
        elif line and not line.startswith("#"):
            segments.append(resolve_url(base_url, line))

    return segments, encryption, init_url


def is_master_playlist(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text or "#EXT-X-MEDIA" in text

# ---------------------------------------------------------------------------
# Decryption
# ---------------------------------------------------------------------------

def decrypt_segment(data: bytes, key: bytes, iv: Optional[bytes], segment_index: int) -> bytes:
    """AES-128-CBC decrypt a segment."""
    if not PYCRYPTODOME_AVAILABLE:
        raise RuntimeError(
            "pycryptodome is required for AES-128 decryption.\n"
            "Install with: pip install pycryptodome"
        )
    if iv is None:
        # Default IV is the segment sequence number as a 16-byte big-endian int
        iv = segment_index.to_bytes(16, "big")
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.decrypt(data)

# ---------------------------------------------------------------------------
# Segment downloading
# ---------------------------------------------------------------------------

def download_segment(
    session: requests.Session,
    url: str,
    dest: Path,
    index: int,
    key: Optional[bytes],
    iv: Optional[bytes],
) -> Path:
    """Download one segment, optionally decrypt, write to *dest*."""
    data = fetch_bytes(session, url)
    if key is not None:
        data = decrypt_segment(data, key, iv, index)
    dest.write_bytes(data)
    return dest


def download_all_segments(
    session: requests.Session,
    segments: list[str],
    encryption: Optional[dict],
    tmp_dir: Path,
    workers: int,
    cancel_event: Optional[threading.Event] = None,
) -> list[Path]:
    """Download all segments in parallel and return ordered list of file paths."""

    # Fetch encryption key once if needed
    aes_key: Optional[bytes] = None
    if encryption and encryption["method"] == "AES-128":
        print(f"[crypto] Fetching AES-128 key from {encryption['uri']}")
        aes_key = fetch_bytes(session, encryption["uri"])

    total = len(segments)
    pad = len(str(total))
    futures: dict = {}

    with ThreadPoolExecutor(max_workers=workers) as pool, \
         tqdm(total=total, unit="seg", desc="Downloading", dynamic_ncols=True) as pbar:

        for idx, url in enumerate(segments):
            dest = tmp_dir / f"seg_{idx:0{pad}d}.ts"
            iv = encryption["iv"] if encryption else None
            future = pool.submit(download_segment, session, url, dest, idx, aes_key, iv)
            futures[future] = idx

        # Magic bytes that indicate the server returned an error/non-video response.
        # GIF/JPEG/HTML are almost always auth failures. PNG is trickier -
        # some CDNs legitimately wrap fMP4 segments in a PNG envelope.
        HARD_NON_VIDEO = {
            bytes.fromhex("47494638"): "GIF image",
            bytes.fromhex("ffd8ffe0"): "JPEG image",
            bytes.fromhex("ffd8ffe1"): "JPEG image",
            bytes.fromhex("3c21444f"): "HTML page",   # <!DO (<!DOCTYPE)
            bytes.fromhex("3c68746d"): "HTML page",   # <htm
        }
        PNG_MAGIC = bytes.fromhex("89504e47")

        ordered: list[Optional[Path]] = [None] * total
        bad_content_count = 0
        png_count = 0
        checked_count = 0
        warned_png = False
        CHECK_FIRST_N = 3  # How many segments to validate before deciding

        # Timeout-based loop: wakes up every 0.5 s to check cancel_event even
        # when all workers are blocked on slow network requests.
        pending = set(futures.keys())
        while pending:
            if cancel_event and cancel_event.is_set():
                for f in pending:
                    f.cancel()
                tqdm.write("  [cancelled] Download stopped by user.")
                break

            # Wait up to 0.5 s for any future to finish
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)

            for future in done:
                if cancel_event and cancel_event.is_set():
                    break
                idx = futures[future]
                try:
                    path = future.result()
                    ordered[idx] = path

                    # Validate the first few segments
                    if checked_count < CHECK_FIRST_N and path is not None:
                        checked_count += 1
                        magic = path.read_bytes()[:4]
                        hard_type = HARD_NON_VIDEO.get(magic)
                        if hard_type:
                            bad_content_count += 1
                        elif magic == PNG_MAGIC:
                            png_count += 1

                        if checked_count == CHECK_FIRST_N:
                            if bad_content_count == CHECK_FIRST_N:
                                # Definitely bad (GIF/HTML/JPEG error response) - abort
                                tqdm.write(
                                    f"\n  [error] First {CHECK_FIRST_N} segments are "
                                    f"{hard_type} files (magic={magic.hex()}), not video data.\n"
                                    f"  The server is likely rejecting requests "
                                    f"- check your --referer / headers.\n"
                                    f"  Aborting download."
                                )
                                if cancel_event:
                                    cancel_event.set()
                                for f in pending:
                                    f.cancel()
                                pending.clear()
                                break
                            elif png_count == CHECK_FIRST_N and not warned_png:
                                # PNG: warn but continue - CDN may use PNG-wrapped segments
                                tqdm.write(
                                    "\n  [warning] Segments appear to start with PNG magic bytes. "
                                    "Some CDNs wrap fMP4 in a PNG envelope - continuing download.\n"
                                    "  The merger will attempt to strip PNG headers automatically."
                                )
                                warned_png = True

                except Exception as exc:
                    tqdm.write(f"  [error] Segment {idx} failed: {exc}")
                pbar.update(1)

    # Filter out any None (failed/cancelled) segments and warn
    failed = [i for i, p in enumerate(ordered) if p is None]
    if failed:
        downloaded = total - len(failed)
        print(f"\n[warning] {len(failed)} segment(s) not downloaded "
              f"({downloaded}/{total} completed).", file=sys.stderr)

    return [p for p in ordered if p is not None]

# ---------------------------------------------------------------------------
# PNG-wrapper stripping
# ---------------------------------------------------------------------------

def strip_png_wrapper(data: bytes) -> bytes:
    """
    Some CDNs wrap fMP4/TS segments inside a PNG container to obscure the
    content-type. The real video payload starts immediately after the PNG
    IEND chunk (4-byte chunk length + b'IEND' + 4-byte CRC = 12 bytes).
    This function finds the IEND marker and returns everything after it.
    If no IEND marker is found the original data is returned unchanged.
    """
    IEND = b'IEND'
    pos = data.rfind(IEND)
    if pos == -1:
        return data
    # bytes after IEND chunk: pos + len("IEND")=4 + CRC=4
    payload_start = pos + 8
    if payload_start >= len(data):
        return data
    return data[payload_start:]


def detect_png_wrapped(segment_files: list[Path]) -> bool:
    """Return True if the first available segment starts with PNG magic bytes."""
    PNG_MAGIC = b'\x89PNG'
    for p in segment_files[:3]:
        try:
            if p.read_bytes()[:4] == PNG_MAGIC:
                return True
        except OSError:
            pass
    return False


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------


def is_fmp4_segment(path: Path) -> bool:
    """
    Detect whether a file is a fragmented MP4 (fMP4) segment.
    fMP4 files start with an ISO Base Media File Format box whose type is
    'ftyp', 'moof', 'styp', or 'mdat'. We read the first 12 bytes to check.
    """
    try:
        header = path.read_bytes()[:12]
        if len(header) < 8:
            return False
        box_type = header[4:8]
        return box_type in (b'ftyp', b'moof', b'styp', b'mdat')
    except OSError:
        return False


def merge_segments_binary(
    segment_files: list[Path], output: Path, prefix_bytes: bytes = b""
) -> None:
    """Binary-concatenate optional prefix bytes + segments into a single file."""
    label = "Merging"
    print(f"[merge] Concatenating {len(segment_files)} segments -> {output}")
    with open(output, "wb") as out_f:
        if prefix_bytes:
            out_f.write(prefix_bytes)
        for seg in tqdm(segment_files, desc=label, unit="seg", dynamic_ncols=True):
            out_f.write(seg.read_bytes())


# Keep old name as alias for --no-ffmpeg mode
def merge_segments_ts(segment_files: list[Path], output: Path) -> None:
    """Binary-concatenate segments into a single file (for --no-ffmpeg mode)."""
    merge_segments_binary(segment_files, output)


def write_concat_list(segment_files: list[Path], list_path: Path) -> None:
    """Write an ffmpeg concat demuxer file listing all segments."""
    with open(list_path, "w", encoding="utf-8") as f:
        for seg in segment_files:
            # Use forward slashes and escape single-quotes for ffmpeg
            safe = str(seg.resolve()).replace("'", "'\\''")
            f.write(f"file '{safe}'\n")


def remux_with_ffmpeg(
    segment_files: list[Path], tmp_dir: Path, output: Path, init_data: bytes = b""
) -> bool:
    """
    Merge and remux *segment_files* to *output* using ffmpeg.

    - MPEG-TS segments: uses ffmpeg concat demuxer directly.
    - fMP4 segments: binary-concatenates all segments first into a single
      fragmented MP4, then remuxes that with ffmpeg. The concat demuxer
      cannot handle fMP4 boxes and will error with 'could not find trex'.

    Returns True on success, False on failure.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False

    # Detect segment type from the first segment
    use_fmp4_path = segment_files and is_fmp4_segment(segment_files[0])

    if use_fmp4_path:
        # --- fMP4 path: init_data + binary-cat -> single file -> ffmpeg remux ---
        if init_data:
            print(f"[info] fMP4 segments + init segment ({len(init_data):,} bytes) - binary concat + remux.")
        else:
            print(f"[info] fMP4 segments (no init segment found) - binary concat + remux.")
        raw_mp4 = tmp_dir / "merged_raw.mp4"
        merge_segments_binary(segment_files, raw_mp4, prefix_bytes=init_data)

        cmd = [
            ffmpeg,
            "-y",
            "-i", str(raw_mp4),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output),
        ]
    else:
        # --- MPEG-TS path: concat demuxer ---
        concat_list = tmp_dir / "concat_list.txt"
        write_concat_list(segment_files, concat_list)

        cmd = [
            ffmpeg,
            "-y",
            # Increase probe window so ffmpeg can identify codecs even in
            # segments that were stripped from PNG wrappers or have no PTS.
            "-probesize", "50M",
            "-analyzeduration", "10M",
            # Tolerate corrupt/truncated ADTS frames at the demux level so the
            # aac_adtstoasc BSF never sees a malformed sync word.
            "-err_detect", "ignore_err",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list),
            # +discardcorrupt - drop corrupt packets instead of aborting.
            # +genpts        - regenerate missing PTS from DTS.
            "-fflags", "+discardcorrupt+genpts",
            # Prevent ffmpeg from reordering packets to enforce strict
            # interleaving - suppresses non-monotonic DTS warnings.
            "-max_interleave_delta", "0",
            "-c:v", "copy",
            # Explicitly apply the ADTS->ASC bitstream filter for AAC audio.
            # Running it explicitly (rather than letting ffmpeg apply it
            # automatically) means it runs *after* corrupt packets have been
            # discarded by -fflags +discardcorrupt, avoiding the
            # "Error parsing ADTS frame header" crash.
            "-c:a", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart",
            str(output),
        ]

    print(f"[ffmpeg] Merging {len(segment_files)} segments -> {output} ...")
    result = subprocess.run(cmd, capture_output=True, text=True)

    # Always show warnings/errors from ffmpeg stderr (last 20 lines)
    stderr_lines = result.stderr.strip().splitlines()
    if stderr_lines:
        relevant = "\n".join(stderr_lines[-20:])
        print(f"[ffmpeg] Output:\n{relevant}", file=sys.stderr)

    if result.returncode == 0:
        return True

    # ffmpeg sometimes exits non-zero even when it successfully wrote the file
    # (e.g. a single corrupt AAC packet). Treat as success if the output file
    # exists and is non-empty.
    if output.exists() and output.stat().st_size > 0:
        print(
            f"[ffmpeg] Exited with code {result.returncode} but output file was "
            f"created ({output.stat().st_size:,} bytes) - treating as success.",
            file=sys.stderr,
        )
        return True

    return False

# ---------------------------------------------------------------------------
# Scaling helpers
# ---------------------------------------------------------------------------


def get_video_height(path: Path) -> int:
    """
    Return the height (in pixels) of the video stream in *path* using ffprobe.
    Returns 0 if ffprobe is unavailable or the query fails.
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0
    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=height",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return int(result.stdout.strip().splitlines()[0])
    except (IndexError, ValueError):
        return 0


def get_video_duration(path: Path) -> float:
    """Return video duration in seconds using ffprobe, or 0.0 on failure."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip().splitlines()[0])
    except (IndexError, ValueError):
        return 0.0


def get_video_bitrate(path: Path) -> int:
    """
    Return the video stream's bitrate in bits/s using ffprobe, or 0 on failure.
    Falls back to the container's overall bitrate if the stream bitrate is absent.
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0
    # Try stream-level bitrate first
    for entries in ("stream=bit_rate", "format=bit_rate"):
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", entries,
                "-of", "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
        )
        val = result.stdout.strip().splitlines()
        try:
            bps = int(val[0])
            if bps > 0:
                return bps
        except (IndexError, ValueError):
            pass
    return 0


# GPU encoder priority list: (encoder_name, label, extra_preset_flags)
# Bitrate flags are computed per-call in scale_video and appended separately.
_GPU_ENCODERS = [
    ("h264_nvenc", "NVIDIA NVENC", ["-rc", "vbr", "-preset", "p4"]),
    ("h264_amf",   "AMD AMF",      ["-quality", "balanced", "-rc", "vbr_latency"]),
    ("h264_qsv",   "Intel QSV",    ["-preset", "faster", "-look_ahead", "1"]),
]
_CPU_ENCODER = ("libx264", "CPU (libx264)", ["-preset", "fast"])


def detect_video_encoder(ffmpeg: str) -> tuple[str, str, list[str]]:
    """
    Probe available ffmpeg encoders and return the best one.

    Returns (encoder_name, label, extra_flags).
    Checks M3U8_ENCODER_OVERRIDE env var first (set by the GUI), then
    tries GPU encoders (NVENC -> AMF -> QSV), falls back to libx264.
    """
    # GUI / env-var override: skip auto-detection and use the specified encoder
    override = os.environ.get("M3U8_ENCODER_OVERRIDE", "").strip()
    if override:
        for enc_name, label, flags in _GPU_ENCODERS:
            if enc_name == override:
                print(f"[scale] Encoder override: {label} ({enc_name})")
                return enc_name, label, flags
        if override == "libx264":
            print(f"[scale] Encoder override: CPU (libx264)")
            return _CPU_ENCODER

    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True,
        )
        available = result.stdout + result.stderr
        for enc_name, label, flags in _GPU_ENCODERS:
            if f" {enc_name} " in available:
                # Quick sanity-encode: try encoding a 1-frame black video
                test = subprocess.run(
                    [
                        ffmpeg, "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "color=black:s=64x64:d=0.1",
                        "-frames:v", "1",
                        "-c:v", enc_name,
                        "-f", "null", "-",
                    ],
                    capture_output=True,
                )
                if test.returncode == 0:
                    return enc_name, label, flags
    except Exception:
        pass
    return _CPU_ENCODER


def scale_video(src: Path, dst: Path, target_height: int, source_height: int = 0) -> bool:
    """
    Re-encode *src* to *dst* scaling video to *target_height* pixels tall.

    - Auto-detects GPU encoder (NVENC -> AMF -> QSV -> libx264 CPU fallback).
    - Caps the output bitrate so the scaled file is always smaller than the source.
    - Shows a live tqdm progress bar based on ffmpeg's progress output.
    - Audio is re-encoded to AAC 192 kbps.
    Returns True on success.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False

    enc_name, enc_label, enc_flags = detect_video_encoder(ffmpeg)
    print(f"[scale] Encoder: {enc_label}  ({enc_name})")

    duration = get_video_duration(src)

    # --- Compute a bitrate ceiling so output is always smaller than source ---
    src_bps = get_video_bitrate(src)
    bitrate_flags: list[str] = []
    if src_bps > 0 and source_height > 0 and source_height > target_height:
        # Bitrate scales with pixel area: (h_new/h_old)²
        ratio = (target_height / source_height) ** 2
        target_bps = int(src_bps * ratio)
        target_kbps = max(target_bps // 1000, 200)     # floor at 200 kbps
        maxrate_kbps = int(target_kbps * 1.1)           # allow 10% burst
        bufsize_kbps = target_kbps * 2
        print(
            f"[scale] Source bitrate: {src_bps // 1000:,} kbps  ->  "
            f"target: {target_kbps:,} kbps  (max: {maxrate_kbps:,} kbps)"
        )
        bitrate_flags = [
            "-b:v", f"{target_kbps}k",
            "-maxrate", f"{maxrate_kbps}k",
            "-bufsize", f"{bufsize_kbps}k",
        ]
    else:
        # Fallback: no bitrate info - use conservative quality setting
        if enc_name == "libx264":
            bitrate_flags = ["-crf", "23"]
        else:
            bitrate_flags = ["-b:v", "2M", "-maxrate", "2500k", "-bufsize", "4M"]

    cmd = [
        ffmpeg,
        "-y",
        "-i", str(src),
        "-vf", f"scale=-2:{target_height}",
        "-c:v", enc_name,
        *enc_flags,
        *bitrate_flags,
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        # Write machine-readable progress to stdout so we can parse it
        "-progress", "pipe:1",
        "-loglevel", "error",    # suppress normal chatter; errors still go to stderr
        str(dst),
    ]

    print(f"[scale] Re-encoding {src.name} -> {target_height}p -> {dst.name} ...")
    # Sentinel for GUI: total duration so the progress bar knows its upper bound
    if duration > 0:
        print(f"[scale_total_s={duration:.3f}]", flush=True)

    bar_fmt = "{l_bar}{bar}| {n:.1f}/{total:.1f}s [{elapsed}<{remaining}, {rate_fmt}]"
    total = duration if duration > 0 else None

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,          # line-buffered
    )

    returncode = 0
    stderr_lines: list[str] = []
    _last_emitted_pct = -1

    with tqdm(
        total=total,
        unit="s",
        desc="Scaling",
        dynamic_ncols=True,
        bar_format=bar_fmt if total else None,
    ) as pbar:
        last_time = 0.0
        # ffmpeg -progress writes key=value lines to stdout
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip()
            if line.startswith("out_time_ms="):
                try:
                    ms = int(line.split("=", 1)[1])
                    current = ms / 1_000_000.0   # microseconds -> seconds
                    delta = current - last_time
                    if delta > 0:
                        pbar.update(delta)
                        last_time = current
                        # Emit a machine-readable percentage for the GUI
                        if total and total > 0:
                            pct = min(100, int(last_time / total * 100))
                            if pct != _last_emitted_pct:
                                print(f"[scale_progress={pct}]", flush=True)
                                _last_emitted_pct = pct
                except ValueError:
                    pass
            elif line == "progress=end":
                if total and last_time < total:
                    pbar.update(total - last_time)
                print("[scale_progress=100]", flush=True)

        # Drain stderr for error reporting
        for line in proc.stderr:  # type: ignore[union-attr]
            stderr_lines.append(line.rstrip())

    proc.wait()
    returncode = proc.returncode

    if stderr_lines:
        print(f"[scale] ffmpeg errors:\n" + "\n".join(stderr_lines[-10:]), file=sys.stderr)

    if returncode == 0:
        return True
    if dst.exists() and dst.stat().st_size > 0:
        print(
            f"[scale] ffmpeg exited with code {returncode} but output "
            f"exists ({dst.stat().st_size:,} bytes) - treating as success.",
            file=sys.stderr,
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def build_headers(args: argparse.Namespace) -> dict[str, str]:
    headers = dict(DEFAULT_HEADERS)
    # Auto-derive Referer
    referer = args.referer if args.referer else derive_referer(args.url)
    headers["Referer"] = referer
    # Apply extra headers from --header flags
    for raw in args.header:
        if ":" in raw:
            name, _, value = raw.partition(":")
            headers[name.strip()] = value.strip()
        else:
            print(f"[warning] Skipping malformed header (expected 'Name: Value'): {raw}", file=sys.stderr)
    return headers


def run(args: argparse.Namespace) -> None:
    # Check ffmpeg availability upfront (unless --no-ffmpeg was passed)
    if not args.no_ffmpeg and not shutil.which("ffmpeg"):
        sys.exit(
            "[error] ffmpeg was not found on PATH.\n"
            "  Install ffmpeg and make sure it is available in your PATH, or\n"
            "  pass --no-ffmpeg to save a raw .ts file instead."
        )

    headers = build_headers(args)
    session = make_session(headers)

    print(f"[fetch] {args.url}")
    playlist_text = fetch_text(session, args.url)

    # Resolve master -> media playlist
    stream_url = args.url
    is_audio = False
    if is_master_playlist(playlist_text):
        stream_url, is_audio = parse_master_playlist(
            playlist_text, args.url, args.audio_only, args.quality,
            resolution=args.resolution,
        )
        # Update Referer for the stream domain if it differs
        session.headers["Referer"] = derive_referer(stream_url)
        print(f"[fetch] {stream_url}")
        playlist_text = fetch_text(session, stream_url)
    elif args.audio_only:
        is_audio = True

    segments, encryption, init_url = parse_media_playlist(playlist_text, stream_url)
    if not segments:
        sys.exit("[error] No segments found in the playlist.")

    print(f"[info] Found {len(segments)} segment(s). Encryption: "
          f"{encryption['method'] if encryption else 'none'}. "
          f"Init segment: {'yes' if init_url else 'none'}")
    if init_url:
        print(f"[init] {init_url}")

    if encryption and encryption["method"] != "AES-128":
        sys.exit(f"[error] Unsupported encryption method: {encryption['method']}")

    # Determine output path
    output = Path(args.output)
    if output.suffix.lower() not in (".mp4", ".ts", ".m4a", ".mkv", ".aac"):
        output = output.with_suffix(".m4a" if (args.audio_only or is_audio) else ".mp4")

    # Create temp directory (user-configurable via --temp-dir, else cwd/temp)
    _temp_base = Path(args.temp_dir) if getattr(args, "temp_dir", None) else Path.cwd() / "temp"
    tmp_dir = _temp_base / f"m3u8_dl_{uuid.uuid4().hex[:8]}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    print(f"[tmp] {tmp_dir}")

    cancel_event = threading.Event()

    def _confirm_cancel():
        """Called on the first Ctrl+C - ask the user what to do."""
        print("\n", flush=True)
        try:
            answer = input("[interrupt] Cancel download? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            # Second Ctrl+C during the prompt -> force quit immediately
            print("\n[cancelled] Force-quitting.", file=sys.stderr)
            cancel_event.set()
            return
        if answer in ("y", "yes"):
            print("[cancelled] Stopping after in-flight segments finish...", file=sys.stderr)
            cancel_event.set()
        else:
            print("[resuming] Continuing download.", flush=True)

    try:
        # Download the fMP4 initialization segment (#EXT-X-MAP) if present
        init_data: bytes = b""
        if init_url:
            print("[init] Downloading initialization segment...")
            init_data = fetch_bytes(session, init_url)
            print(f"[init] {len(init_data):,} bytes")

        # ── Run download in a daemon thread ───────────────────────────────────
        # On Windows, any blocking C-level wait (threading.Condition, socket IO)
        # prevents Python from delivering KeyboardInterrupt to the main thread.
        # The only reliable solution: keep the main thread awake with short
        # join(timeout) calls so Python can check for signals every 0.5 s.
        _dl_result: dict = {}

        def _download_worker() -> None:
            try:
                _dl_result["files"] = download_all_segments(
                    session, segments, encryption, tmp_dir, args.workers,
                    cancel_event=cancel_event,
                )
            except Exception as exc:
                _dl_result["error"] = exc

        dl_thread = threading.Thread(target=_download_worker, daemon=True)
        dl_thread.start()

        # Outer loop so that if the user chooses "resume" we go straight
        # back into join(0.5) polling instead of a bare blocking join().
        while dl_thread.is_alive():
            try:
                while dl_thread.is_alive():
                    dl_thread.join(timeout=0.5)  # re-enters Python every 0.5 s
            except KeyboardInterrupt:
                _confirm_cancel()
                if cancel_event.is_set():
                    # os._exit() kills the process immediately - daemon threads
                    # die with it and tqdm stops printing instantly.
                    # Do temp-dir cleanup manually first.
                    if not args.keep_temp:
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                    else:
                        print(f"[tmp] Kept temp dir: {tmp_dir}")
                    print("[cancelled] Download cancelled by user.", file=sys.stderr)
                    os._exit(1)
                # If resumed: outer while re-enters join(0.5) immediately.

        if "error" in _dl_result:
            raise _dl_result["error"]

        segment_files = _dl_result.get("files", [])
        # ─────────────────────────────────────────────────────────────────────

        if not segment_files:
            sys.exit("[error] No segments were successfully downloaded.")

        # Strip PNG wrappers in-place if the CDN used PNG-wrapped segments
        if detect_png_wrapped(segment_files):
            print("[info] PNG-wrapped segments detected - stripping PNG envelopes...")
            stripped = 0
            for seg_path in segment_files:
                raw = seg_path.read_bytes()
                unwrapped = strip_png_wrapper(raw)
                if unwrapped is not raw:
                    seg_path.write_bytes(unwrapped)
                    stripped += 1
            print(f"[info] Stripped PNG wrapper from {stripped}/{len(segment_files)} segments.")

        if args.no_ffmpeg:
            # Binary concatenation -> raw file
            raw_out = tmp_dir / "merged.ts"
            merge_segments_binary(segment_files, raw_out, prefix_bytes=init_data)
            final_out = output.with_suffix(".ts" if not init_data else ".mp4")
            shutil.copy2(raw_out, final_out)
            print(f"\n[done] Saved raw file -> {final_out.resolve()}")
        else:
            # Use ffmpeg - handles both MPEG-TS and fMP4
            success = remux_with_ffmpeg(segment_files, tmp_dir, output, init_data=init_data)
            if not success:
                sys.exit(
                    "[error] ffmpeg failed to merge the segments. "
                    "Try --keep-temp to inspect the segment files."
                )

            # Optional downscale pass
            target_scale: int = getattr(args, "scale", 0) or 0
            if target_scale:
                original_height = get_video_height(output)
                if original_height <= 0:
                    print(
                        "[scale] Could not determine video height - skipping resize.",
                        file=sys.stderr,
                    )
                    print(f"\n[done] -> {output.resolve()}")
                elif original_height <= target_scale:
                    print(
                        f"[scale] Source is already {original_height}p "
                        f"(<= {target_scale}p requested) - no resize needed."
                    )
                    print(f"\n[done] -> {output.resolve()}")
                else:
                    # Rename merged file -> "<stem> - original<suffix>"
                    original_copy = output.with_stem(output.stem + " - original")
                    output.rename(original_copy)
                    print(
                        f"[scale] Original ({original_height}p) saved as: "
                        f"{original_copy.resolve()}"
                    )
                    scaled_ok = scale_video(original_copy, output, target_scale, source_height=original_height)
                    if scaled_ok:
                        print(f"\n[done] -> {output.resolve()} ({target_scale}p)")
                    else:
                        # Restore original filename so the user doesn't lose their file
                        original_copy.rename(output)
                        sys.exit("[error] Scaling failed - original file restored.")
            else:
                print(f"\n[done] -> {output.resolve()}")

    except KeyboardInterrupt:
        _confirm_cancel()
        if cancel_event.is_set():
            sys.exit("[cancelled] Download cancelled by user.")
        sys.exit("[info] Interrupted.")
    finally:
        if args.keep_temp:
            print(f"[tmp] Kept temp dir: {tmp_dir}")
        else:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="m3u8_downloader",
        description="Download video/audio from an M3U8 HLS stream.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("url", help="M3U8 playlist URL")
    parser.add_argument(
        "-o", "--output",
        default="output.mp4",
        metavar="FILE",
        help="Output filename (default: output.mp4)",
    )
    parser.add_argument(
        "--referer",
        default="",
        metavar="URL",
        help="Override the Referer header (default: derived from M3U8 URL)",
    )
    parser.add_argument(
        "-H", "--header",
        action="append",
        default=[],
        metavar="\"Name: Value\"",
        help="Add or override a request header (repeatable)",
    )
    parser.add_argument(
        "-w", "--workers",
        type=int,
        default=4,
        metavar="N",
        help="Parallel download threads (default: 4)",
    )
    parser.add_argument(
        "-q", "--quality",
        type=int,
        default=0,
        metavar="N",
        help="Quality variant index, 0 = best (default: 0). Ignored if --resolution is set.",
    )
    parser.add_argument(
        "-r", "--resolution",
        default="",
        metavar="RES",
        help="Target resolution height, e.g. 720p or 1080p. Picks the closest available variant.",
    )
    parser.add_argument(
        "--audio-only",
        action="store_true",
        help="Download audio rendition only",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep temp segment directory after merge",
    )
    parser.add_argument(
        "-s", "--scale",
        type=int,
        default=0,
        metavar="HEIGHT",
        help=(
            "Downscale the merged video to this height in pixels "
            "(e.g. 720 for 720p). The original is kept with a \"-original\" suffix. "
            "Has no effect if the source is already at or below the target height."
        ),
    )
    parser.add_argument(
        "--temp-dir",
        default="",
        metavar="DIR",
        help="Directory to use as the parent for temporary segment folders (default: <cwd>/temp)",
    )
    parser.add_argument(
        "--no-ffmpeg",
        action="store_true",
        help="Skip ffmpeg remux and output raw .ts file",
    )
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
