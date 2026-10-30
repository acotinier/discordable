"""Vérifications rapides : python test_discordable.py"""
import os
import subprocess
import tempfile

from discordable import build_cmds, find_tool, newer_release, plan, probe

INFO = {"duration": 60, "width": 1920, "height": 1080, "fps": 60, "v_bps": 20_000_000, "codec": "h264", "has_audio": True}


def test_plan():
    p = plan(INFO, 60, 19, 128, "auto", 0)
    assert abs(p["est_mb"] - 19 * 0.97 * 1.01) < 0.01, p
    assert p["side"] == 720 and p["dims"] == (1280, 720), p  # 1080p60 en 2,3 Mb/s = trop peu, auto descend
    assert plan(INFO, 60, 19, 128, "orig", 0)["side"] is None
    portrait = dict(INFO, width=1080, height=1920)
    assert plan(portrait, 60, 19, 128, 720, 30)["dims"] == (720, 1280)
    # source déjà légère : on ne gonfle pas le débit
    assert plan(dict(INFO, v_bps=500_000), 60, 19, 128, "orig", 0)["v_bps"] == 500_000
    assert plan(INFO, 3600, 19, 128, "auto", 0)["error"]  # 1 h dans 19 Mo : impossible


def test_newer_release():
    rel = {"tag_name": "v1.10.0", "assets": [{"name": "notes.txt"},
           {"name": "Discordable-Setup-1.10.0.exe", "browser_download_url": "https://x/s.exe", "size": 5}]}
    assert newer_release(rel, "1.9.3") == ("1.10.0", "https://x/s.exe", 5)  # comparaison numérique, pas texte
    assert newer_release(rel, "1.10.0") is None
    assert newer_release({"tag_name": "v2.0.0", "assets": []}, "1.0.0") is None
    assert newer_release({"message": "rate limited"}, "1.0.0") is None


def test_encode():
    ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    tmp = tempfile.mkdtemp()
    src, dst = os.path.join(tmp, "source.mp4"), os.path.join(tmp, "source_discord.mp4")
    subprocess.run([ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=12",
                    "-f", "lavfi", "-i", "sine=duration=12", "-c:v", "libx264", "-crf", "5", "-c:a", "aac", src],
                   check=True)
    info = probe(ffprobe, src)
    segments = [(1, 4), (7, 12)]  # deux morceaux mis bout à bout = 8 s
    p = plan(info, 8, 2, 96, 480, 0)
    for cmd in build_cmds(ffmpeg, src, dst, segments, p, 96, "veryfast", os.path.join(tmp, "log")):
        subprocess.run(cmd, check=True, capture_output=True)
    out = probe(ffprobe, dst)
    size_mb = os.path.getsize(dst) / 1e6
    assert size_mb <= 2, size_mb
    assert (out["width"], out["height"]) == (480, 854), out  # portrait : le côté court devient 480
    assert abs(out["duration"] - 8) < 0.2, out
    assert out["has_audio"]
    print(f"encodage OK : {os.path.getsize(src) / 1e6:.1f} Mo -> {size_mb:.2f} Mo")


if __name__ == "__main__":
    test_plan()
    test_newer_release()
    test_encode()
    print("tout est OK")
