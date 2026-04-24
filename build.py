import os
import subprocess
import sys
from pathlib import Path

def build():
    script_dir = Path(__file__).parent.resolve()
    
    print("Building M3U8 Downloader GUI...")
    
    # We build the GUI script. It already includes the downloader script
    # because we import m3u8_downloader in the --run-downloader branch.
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed", # Don't show the console window
        "--name", "M3U8 Downloader",
        # Include the downloader source so it's guaranteed to be available
        "--add-data", f"{script_dir / 'm3u8_downloader.py'};.",
        str(script_dir / "m3u8_gui.py"),
    ]
    
    subprocess.run(cmd, check=True)
    print("\nBuild complete! Check the 'dist' folder for 'M3U8 Downloader.exe'")

if __name__ == "__main__":
    build()
