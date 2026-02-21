# M3U8 Downloader

A Python command-line tool to download video or audio from HLS (M3U8) streams. Supports master playlists, AES-128 encrypted streams, fMP4 and MPEG-TS segments, and mimics browser request headers automatically.

## Prerequisites

### Required

- **Python 3.8+** — [python.org](https://www.python.org/downloads/)
- **ffmpeg / ffprobe** — must be installed and available on your system `PATH`
  - Windows: download from [ffmpeg.org](https://ffmpeg.org/download.html) and add the `bin/` folder to `PATH`
  - Or use `--no-ffmpeg` to skip remuxing and save a raw `.ts` file instead

### Python dependencies

Managed via `requirements.txt` (installed into the virtual environment below):

| Package | Purpose |
|---|---|
| `requests` | HTTP segment downloading |
| `tqdm` | Progress bar |
| `pycryptodome` | AES-128 segment decryption |

## Download / Installation

You can download the compiled standalone desktop GUI for Windows from the **[Releases page](https://github.com/sunaram/m3u8-stream-downloader/releases)**. This executable works out of the box and requires no Python installation.

If you prefer to run from source or build the executable yourself, follow the instructions below.

## Running from Source

```powershell
# 1. Clone or copy this folder, then enter it
cd m3u8-downloader

# 2. Activate the virtual environment
.\venv\Scripts\activate

# If the venv doesn't exist yet, create and populate it first:
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

### Building the Executable (Optional)

If you installed Python and the requirements above, you can compile your own standalone executable using PyInstaller:

```powershell
pip install pyinstaller
python build.py
```

The compiled standalone executable will be placed in the newly created `dist/` folder.

## Usage

```
python m3u8_downloader.py <M3U8_URL> [OPTIONS]
```

### Desktop GUI

A cross-platform PyQt6 GUI wrapper is included for easier use without the command line.

`python m3u8_gui.py`

- **Download Directory**: Defaults to the locally created `downloads` directory if left blank.
- **Auto-naming**: If no output filename is provided, the GUI will automatically save it as `output.mp4` (or `output.ts` if No ffmpeg is checked). If the file already exists, it will auto-append a number (e.g. `output-1.mp4`).

### CLI Options

| Flag | Default | Description |
|---|---|---|
| `-o, --output FILE` | `output.mp4` | Output filename |
| `-r, --resolution RES` | *(highest bandwidth)* | Target resolution height to download, e.g. `720p` or `1080`. Picks the closest available variant from the master playlist. |
| `--referer URL` | *(derived from M3U8 URL)* | Override the `Referer` request header |
| `-H, --header "Name: Value"` | — | Add/override a request header (repeatable) |
| `-w, --workers N` | `4` | Number of parallel download threads |
| `-q, --quality N` | `0` (best) | Variant index from master playlist (ignored if `-r` is set) |
| `-s, --scale HEIGHT` | — | Downscale the merged video to this height (e.g. `720`). The original is preserved as `<name> - original.mp4`. No-op if source is already at or below the target height. GPU-accelerated if NVENC / AMF / QSV is available. |
| `--audio-only` | — | Download audio rendition only |
| `--no-ffmpeg` | — | Skip ffmpeg remux; save raw `.ts` file |
| `--keep-temp` | — | Keep the temporary segment folder after download |

### Examples

**Basic download:**
```powershell
python m3u8_downloader.py "https://example.com/stream/index.m3u8" -o video.mp4
```

**Select stream quality by resolution:**
```powershell
python m3u8_downloader.py "https://example.com/master.m3u8" -r 1080p -o video.mp4
```

**Download 1080p stream, then downscale to 720p:**
```powershell
python m3u8_downloader.py "https://example.com/master.m3u8" -r 1080 -s 720 -o video.mp4
# Produces: video.mp4 (720p)  +  video - original.mp4 (1080p)
```

**Download best quality, downscale to 480p:**
```powershell
python m3u8_downloader.py "https://example.com/master.m3u8" -s 480 -o video.mp4
```

**Custom referer and extra headers:**
```powershell
python m3u8_downloader.py "https://cdn.example.com/stream.m3u8" `
    --referer "https://example.com" `
    -H "Origin: https://example.com" `
    -H "X-Custom-Token: abc123"
```

**More download threads:**
```powershell
python m3u8_downloader.py "https://example.com/stream.m3u8" -w 8 -o video.mp4
```

**Audio only:**
```powershell
python m3u8_downloader.py "https://example.com/master.m3u8" --audio-only -o audio.m4a
```

**Save raw `.ts` without ffmpeg:**
```powershell
python m3u8_downloader.py "https://example.com/stream.m3u8" --no-ffmpeg -o out.ts
```

**Keep temp segments for debugging:**
```powershell
python m3u8_downloader.py "https://example.com/stream.m3u8" --keep-temp -o video.mp4
```

## How it works

1. **Fetches the playlist** — detects master vs. media playlist automatically.
2. **Selects stream** — picks the highest-bandwidth variant by default; use `-r` to target a specific resolution.
3. **Downloads segments** — parallel download with configurable workers and exponential back-off retry.
4. **Decrypts** — AES-128 encrypted segments are decrypted automatically.
5. **Merges** — ffmpeg's concat demuxer merges all segments into the output file (supports MPEG-TS and fMP4, including PNG-wrapped CDN segments).
6. **Downscales** *(optional)* — if `--scale` is set and the source is taller than the target, the merged file is re-encoded at the target height. Output bitrate is capped proportionally to the source so the scaled file is always smaller. GPU acceleration (NVENC / AMF / QSV) is used automatically when available, falling back to `libx264`.

Temp segment files are stored in a `temp/` subfolder of the current directory and cleaned up automatically on completion.

## Notes

- The `Referer` header is automatically set to the scheme + host of the M3U8 URL (e.g. `https://cdn.example.com/`). Override with `--referer` if needed.
- Headers mimic Chrome 122 on Windows 10 by default.
- ffmpeg is checked before downloading starts — if it's missing and `--no-ffmpeg` is not set, the script exits immediately with a clear error.
- `--scale` requires `ffprobe` (bundled with ffmpeg) to probe the source resolution and bitrate.

