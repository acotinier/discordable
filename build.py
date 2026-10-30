"""Construit l'installeur Windows : python build.py  ->  build/Discordable-Setup-<version>.exe

Nécessite PyInstaller (pip install pyinstaller) et Inno Setup (winget install JRSoftware.InnoSetup).
FFmpeg (build « shared » de gyan.dev) est téléchargé une seule fois dans build/ffmpeg-dl.
"""
import glob
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from discordable import __version__

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
DIST = BUILD / "dist" / "Discordable"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-full-shared.7z"
FFMPEG_FILES = ["ffmpeg.exe", "ffprobe.exe", "avcodec-*.dll", "avdevice-*.dll", "avfilter-*.dll",
                "avformat-*.dll", "avutil-*.dll", "swresample-*.dll", "swscale-*.dll"]


def ffmpeg_bin():
    dl = BUILD / "ffmpeg-dl"
    hits = glob.glob(str(dl / "*" / "bin"))
    if not hits:
        dl.mkdir(parents=True, exist_ok=True)
        archive = dl / "ffmpeg-shared.7z"
        print("Téléchargement de FFmpeg…")
        urllib.request.urlretrieve(FFMPEG_URL, archive)
        subprocess.run([r"C:\Windows\System32\tar.exe", "-xf", str(archive)], cwd=dl, check=True)
        hits = glob.glob(str(dl / "*" / "bin"))
    return Path(hits[0])


def iscc():
    for path in (shutil.which("iscc"),
                 os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"),
                 r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"):
        if path and Path(path).exists():
            return path
    sys.exit("Inno Setup introuvable : winget install JRSoftware.InnoSetup")


def main():
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--name", "Discordable",
                    "--icon", str(ROOT / "discordable.ico"), "--exclude-module", "tkinter",
                    "--distpath", str(BUILD / "dist"), "--workpath", str(BUILD / "work"), "--specpath", str(BUILD),
                    str(ROOT / "discordable.py")], check=True)
    shutil.copy(ROOT / "discordable.ico", DIST)
    src = ffmpeg_bin()
    (DIST / "ffmpeg").mkdir(exist_ok=True)
    for pattern in FFMPEG_FILES:
        for f in src.glob(pattern):
            shutil.copy(f, DIST / "ffmpeg")
    shutil.copy(src.parent / "LICENSE", DIST / "ffmpeg" / "LICENSE.txt")  # GPL : la licence voyage avec les binaires
    subprocess.run([iscc(), f"/DAppVersion={__version__}", str(ROOT / "installer.iss")], check=True)
    print(f"\nInstalleur : {BUILD / f'Discordable-Setup-{__version__}.exe'}")


if __name__ == "__main__":
    main()
