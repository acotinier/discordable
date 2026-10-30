"""Discordable : compresse une vidéo pour qu'elle passe sous la limite d'upload de Discord.

Usage :
    pythonw discordable.py [video]      ouvre l'interface
    python discordable.py --install     ajoute "Discordable" au clic droit des vidéos
    python discordable.py --uninstall   retire l'entrée du clic droit
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

VIDEO_EXTS = [".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".flv", ".m4v", ".ts", ".mpg", ".mpeg", ".3gp"]
SIZES = [2160, 1440, 1080, 720, 480, 360]  # côté court, en pixels
PRESETS = {"Rapide": "veryfast", "Équilibré": "medium", "Qualité": "slow"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- logique pure

def find_tool(name):
    """ffmpeg/ffprobe : PATH, puis à côté du script, puis l'install winget (Gyan.FFmpeg)."""
    found = shutil.which(name)
    if found:
        return found
    local = Path(__file__).with_name(name + ".exe")
    if local.exists():
        return str(local)
    pattern = os.path.expandvars(rf"%LOCALAPPDATA%\Microsoft\WinGet\Packages\*FFmpeg*\*\bin\{name}.exe")
    hits = glob.glob(pattern)
    return hits[0] if hits else None


def fraction(text):
    try:
        num, _, den = text.partition("/")
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(ffprobe, path):
    out = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True, encoding="utf-8", creationflags=NO_WINDOW, check=True,
    ).stdout
    data = json.loads(out)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    fmt = data["format"]
    v_bps = int(video.get("bit_rate") or 0)
    if not v_bps and fmt.get("bit_rate"):  # mkv/webm n'ont pas de débit par flux
        v_bps = int(fmt["bit_rate"]) - int((audio or {}).get("bit_rate") or 128_000 * bool(audio))
    return {
        "duration": float(fmt.get("duration") or video.get("duration") or 0),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": fraction(video.get("avg_frame_rate", "")) or fraction(video.get("r_frame_rate", "")) or 30.0,
        "v_bps": max(v_bps, 0),
        "codec": video.get("codec_name", "?"),
        "has_audio": audio is not None,
    }


def plan(info, duration, target_mb, audio_kbps, res, fps_max):
    """Calcule les réglages d'encodage pour tenir dans target_mb.

    res : "auto", "orig" ou un côté court en pixels. fps_max : 0 = garder le fps source.
    """
    audio_bps = audio_kbps * 1000 if info["has_audio"] else 0
    v_bps = target_mb * 8e6 * 0.97 / duration - audio_bps  # 3 % de marge pour le conteneur
    if info["v_bps"]:
        v_bps = min(v_bps, info["v_bps"])  # inutile de dépasser le débit d'origine
    fps = min(info["fps"], fps_max) if fps_max else info["fps"]
    short, long_ = sorted((info["width"], info["height"]))

    def bpp(side):  # bits par pixel et par image : indicateur de qualité
        return v_bps / (side * side * long_ / short * fps)

    if res == "orig":
        side = short
    elif res == "auto":
        candidates = [short] + [s for s in SIZES if s < short]
        side = next((s for s in candidates if bpp(s) >= 0.04), candidates[-1])
    else:
        side = min(int(res), short)

    out_long = round(long_ * side / short / 2) * 2
    p = {
        "v_bps": int(v_bps),
        "side": side if side < short else None,  # None = pas de redimensionnement
        "fps": fps if fps < info["fps"] else None,
        "dims": (out_long, side) if info["width"] >= info["height"] else (side, out_long),
        "est_mb": (v_bps + audio_bps) * duration / 8e6 * 1.01,
        "bpp": bpp(side),
        "error": None,
    }
    if v_bps < 100_000:
        p["error"] = "Débit trop faible : coupe la vidéo, baisse l'audio ou augmente la taille cible."
    return p


def quality_label(bpp):
    for limit, label in ((0.1, "excellente"), (0.06, "bonne"), (0.035, "correcte")):
        if bpp >= limit:
            return label
    return "faible"


def build_cmds(ffmpeg, src, dst, start, duration, p, audio_kbps, preset, logbase):
    """Renvoie les commandes ffmpeg de la passe 1 et de la passe 2 (x264 deux passes)."""
    head = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-progress", "pipe:1", "-nostats",
            "-ss", f"{start:.3f}", "-i", src, "-t", f"{duration:.3f}",
            "-map", "0:v:0", "-c:v", "libx264", "-preset", preset, "-b:v", str(p["v_bps"]),
            "-pix_fmt", "yuv420p", "-passlogfile", logbase]
    filters = []
    if p["side"]:  # le côté court devient p["side"], quelle que soit l'orientation
        s = p["side"]
        filters.append(f"scale='if(gte(iw,ih),-2,{s})':'if(gte(iw,ih),{s},-2)'")
    if p["fps"]:
        filters.append(f"fps={p['fps']:g}")
    if filters:
        head += ["-vf", ",".join(filters)]
    audio = ["-map", "0:a:0?", "-c:a", "aac", "-b:a", f"{audio_kbps}k"] if audio_kbps else ["-an"]
    pass1 = head + ["-pass", "1", "-an", "-f", "null", "-"]
    pass2 = head + ["-pass", "2"] + audio + ["-movflags", "+faststart", dst]
    return pass1, pass2


def fmt_time(sec):
    m, s = divmod(max(sec, 0), 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02}:{s:05.2f}" if h else f"{m}:{s:05.2f}"


# ---------------------------------------------------------------- clic droit Windows

def _menu_keys():
    return [rf"Software\Classes\SystemFileAssociations\{ext}\shell\Discordable" for ext in VIDEO_EXTS]


def install():
    import winreg
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    command = f'"{pythonw}" "{Path(__file__).resolve()}" "%1"'
    icon = Path(__file__).with_name("discordable.ico")
    for key in _menu_keys():
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "Discordable")
            if icon.exists():
                winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, str(icon))
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key + r"\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, command)
    print("Entrée « Discordable » ajoutée au clic droit des vidéos.")


def uninstall():
    import winreg
    for key in _menu_keys():
        for sub in (key + r"\command", key):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            except FileNotFoundError:
                pass
    print("Entrée « Discordable » retirée du clic droit.")


# ---------------------------------------------------------------- interface

def run_gui(path):
    from PySide6.QtCore import QMimeData, QProcess, QSettings, Qt, QUrl
    from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QShortcut
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    from PySide6.QtWidgets import (
        QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
        QLabel, QMessageBox, QProgressBar, QPushButton, QSlider, QStyle, QVBoxLayout, QWidget,
    )

    STYLE = """
    QWidget { background:#313338; color:#dbdee1; font-size:10pt; }
    QGroupBox { border:1px solid #1e1f22; border-radius:8px; margin-top:14px; padding:10px 8px 8px 8px; }
    QGroupBox::title { subcontrol-origin:margin; left:10px; color:#b5bac1; font-weight:bold; }
    QPushButton { background:#4e5058; border:none; border-radius:4px; padding:6px 12px; }
    QPushButton:hover { background:#6d6f78; }
    QPushButton:disabled { background:#3a3c42; color:#80848e; }
    QPushButton#primary { background:#5865f2; color:white; font-weight:bold; padding:9px; }
    QPushButton#primary:hover { background:#4752c4; }
    QComboBox, QDoubleSpinBox { background:#1e1f22; border:none; border-radius:4px; padding:4px 6px; }
    QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width:0; }
    QProgressBar { background:#1e1f22; border:none; border-radius:4px; text-align:center; height:18px; }
    QProgressBar::chunk { background:#5865f2; border-radius:4px; }
    QLabel#muted { color:#949ba4; }
    """
    RED, GREEN, ORANGE = "#f23f43", "#23a55a", "#f0b232"

    class Timeline(QSlider):
        """Slider cliquable qui affiche la zone conservée."""

        def __init__(self):
            super().__init__(Qt.Horizontal)
            self.selection = None  # (début_ms, fin_ms)
            self.setMinimumHeight(26)

        def mousePressEvent(self, e):
            if e.button() == Qt.LeftButton:
                value = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(e.position().x()), self.width())
                self.setValue(value)
                self.sliderMoved.emit(value)
            super().mousePressEvent(e)

        def paintEvent(self, e):
            super().paintEvent(e)
            if self.selection and self.maximum():  # barre bleue sous le rail = partie conservée
                p = QPainter(self)
                a, b = (int(v / self.maximum() * self.width()) for v in self.selection)
                p.fillRect(a, self.height() - 5, max(b - a, 2), 5, QColor("#5865f2"))
                p.end()

    class Window(QWidget):
        def __init__(self, src):
            super().__init__()
            self.src = src
            self.ffmpeg, self.ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
            self.info = probe(self.ffprobe, src)
            self.settings = QSettings("Discordable", "Discordable")
            self.proc = None
            self.dst = str(Path(src).with_name(Path(src).stem + "_discord.mp4"))
            self.showing_result = False
            self.play_selection = False
            self.setWindowTitle(f"Discordable — {Path(src).name}")
            self.resize(1180, 680)
            self.setStyleSheet(STYLE)
            self.setAcceptDrops(False)

            # --- lecteur
            self.video = QVideoWidget()
            self.video.setStyleSheet("background:black")
            self.video.setMinimumSize(560, 315)
            self.player = QMediaPlayer(self)
            self.audio_out = QAudioOutput(self)
            self.player.setAudioOutput(self.audio_out)
            self.player.setVideoOutput(self.video)
            self.player.positionChanged.connect(self.on_position)
            self.player.durationChanged.connect(lambda d: self.timeline.setRange(0, d))
            self.player.playbackStateChanged.connect(
                lambda s: self.btn_play.setIcon(self.style().standardIcon(
                    QStyle.SP_MediaPause if s == QMediaPlayer.PlayingState else QStyle.SP_MediaPlay)))

            self.timeline = Timeline()
            self.timeline.sliderMoved.connect(self.player.setPosition)
            self.lbl_pos = QLabel("0:00.00")
            self.btn_play = QPushButton(self.style().standardIcon(QStyle.SP_MediaPlay), "")
            self.btn_play.setFixedWidth(44)
            self.btn_play.clicked.connect(self.toggle_play)

            self.spin_start, self.spin_end = QDoubleSpinBox(), QDoubleSpinBox()
            for spin in (self.spin_start, self.spin_end):
                spin.setRange(0, self.info["duration"])
                spin.setDecimals(2)
                spin.setSuffix(" s")
            self.spin_end.setValue(self.info["duration"])
            self.btn_in = QPushButton("[ Début ici  (I)")
            self.btn_in.clicked.connect(lambda: self.spin_start.setValue(self.player.position() / 1000))
            self.btn_out = QPushButton("Fin ici ]  (O)")
            self.btn_out.clicked.connect(lambda: self.spin_end.setValue(self.player.position() / 1000))
            self.btn_sel = QPushButton("▶ Lire la sélection")
            self.btn_sel.clicked.connect(self.play_sel)
            self.btn_reset_trim = QPushButton("Tout garder")
            self.btn_reset_trim.clicked.connect(
                lambda: (self.spin_start.setValue(0), self.spin_end.setValue(self.info["duration"])))
            self.lbl_sel = QLabel()
            self.lbl_sel.setObjectName("muted")

            transport = QHBoxLayout()
            transport.addWidget(self.btn_play)
            transport.addWidget(self.timeline, 1)
            transport.addWidget(self.lbl_pos)
            trim_row = QHBoxLayout()
            for w in (self.btn_in, QLabel("Début"), self.spin_start, QLabel("Fin"), self.spin_end, self.btn_out):
                trim_row.addWidget(w)
            trim_row2 = QHBoxLayout()
            trim_row2.addWidget(self.btn_sel)
            trim_row2.addWidget(self.btn_reset_trim)
            trim_row2.addStretch()
            trim_row2.addWidget(self.lbl_sel)
            trim_box = QGroupBox("Découpe")
            tl = QVBoxLayout(trim_box)
            tl.addLayout(trim_row)
            tl.addLayout(trim_row2)

            left = QVBoxLayout()
            left.addWidget(self.video, 1)
            left.addLayout(transport)
            left.addWidget(trim_box)

            # --- infos fichier
            size_mb = os.path.getsize(src) / 1e6
            i = self.info
            info_box = QGroupBox("Fichier")
            fl = QFormLayout(info_box)
            name = QLabel(Path(src).name)
            name.setWordWrap(True)
            folder = QLabel(self.fontMetrics().elidedText(str(Path(src).parent), Qt.ElideMiddle, 240))
            folder.setToolTip(str(Path(src).parent))
            folder.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.lbl_size = QLabel(f"{size_mb:.1f} Mo")
            self.src_mb = size_mb
            details = QLabel(f"{fmt_time(i['duration'])} · {i['width']}×{i['height']} · {i['fps']:.4g} i/s · "
                             f"{i['codec']} · {i['v_bps'] / 1000:.0f} kb/s")
            details.setObjectName("muted")
            details.setWordWrap(True)
            fl.addRow("Nom", name)
            fl.addRow("Dossier", folder)
            fl.addRow("Taille", self.lbl_size)
            fl.addRow("", details)

            # --- paramètres
            self.spin_limit = QDoubleSpinBox()
            self.spin_limit.setRange(1, 1000)
            self.spin_limit.setSuffix(" Mo")
            self.spin_target = QDoubleSpinBox()
            self.spin_target.setRange(0.5, 1000)
            self.spin_target.setSingleStep(0.5)
            self.spin_target.setSuffix(" Mo")
            self.combo_res = QComboBox()
            self.combo_res.addItem("Auto (selon le débit)", "auto")
            self.combo_res.addItem(f"Originale ({min(i['width'], i['height'])}p)", "orig")
            for s in SIZES:
                if s < min(i["width"], i["height"]):
                    self.combo_res.addItem(f"{s}p", s)
            self.combo_fps = QComboBox()
            self.combo_fps.addItem(f"Originale ({i['fps']:.4g})", 0)
            for f in (60, 30, 24):
                if f < i["fps"] - 0.5:
                    self.combo_fps.addItem(f"{f} i/s", f)
            self.combo_audio = QComboBox()
            for kb in (160, 128, 96, 64):
                self.combo_audio.addItem(f"AAC {kb} kb/s", kb)
            self.combo_audio.addItem("Muet", 0)
            self.combo_audio.setEnabled(i["has_audio"])
            self.combo_preset = QComboBox()
            for label, value in PRESETS.items():
                self.combo_preset.addItem(label, value)

            params_box = QGroupBox("Paramètres")
            pl = QFormLayout(params_box)
            pl.addRow("Limite Discord", self.spin_limit)
            pl.addRow("Taille cible", self.spin_target)
            pl.addRow("Résolution", self.combo_res)
            pl.addRow("Images/s", self.combo_fps)
            pl.addRow("Audio", self.combo_audio)
            pl.addRow("Encodage", self.combo_preset)
            btn_defaults = QPushButton("Réglages par défaut")
            btn_defaults.clicked.connect(self.load_defaults)
            pl.addRow("", btn_defaults)

            self.lbl_est = QLabel()
            self.lbl_est.setStyleSheet("font-size:15pt; font-weight:bold")
            self.lbl_est_detail = QLabel()
            self.lbl_est_detail.setObjectName("muted")
            self.lbl_est_detail.setWordWrap(True)
            est_box = QGroupBox("Estimation après traitement")
            el = QVBoxLayout(est_box)
            el.addWidget(self.lbl_est)
            el.addWidget(self.lbl_est_detail)

            # --- sortie et actions
            self.lbl_dst = QLabel()
            self.lbl_dst.setObjectName("muted")
            btn_dst = QPushButton("Changer…")
            btn_dst.clicked.connect(self.choose_dst)
            dst_row = QHBoxLayout()
            dst_row.addWidget(self.lbl_dst, 1)
            dst_row.addWidget(btn_dst)
            self.progress = QProgressBar()
            self.progress.setRange(0, 1000)
            self.progress.setFormat("")
            self.btn_go = QPushButton("Compresser")
            self.btn_go.setObjectName("primary")
            self.btn_go.clicked.connect(self.start)
            self.btn_cancel = QPushButton("Annuler")
            self.btn_cancel.clicked.connect(self.cancel)
            self.btn_cancel.hide()
            self.btn_result = QPushButton("Voir le résultat")
            self.btn_result.clicked.connect(self.toggle_result)
            self.btn_folder = QPushButton("Ouvrir le dossier")
            self.btn_folder.clicked.connect(lambda: subprocess.Popen(f'explorer /select,"{self.dst}"'))
            self.btn_copy = QPushButton("Copier le fichier")
            self.btn_copy.setToolTip("Copie la vidéo : il suffit ensuite de faire Ctrl+V dans Discord")
            self.btn_copy.clicked.connect(self.copy_file)
            done_row = QHBoxLayout()
            for b in (self.btn_result, self.btn_folder, self.btn_copy):
                b.setEnabled(False)
                done_row.addWidget(b)

            right = QVBoxLayout()
            for w in (info_box, params_box, est_box):
                right.addWidget(w)
            right.addStretch()
            right.addLayout(dst_row)
            right.addWidget(self.progress)
            right.addWidget(self.btn_go)
            right.addWidget(self.btn_cancel)
            right.addLayout(done_row)
            right_w = QWidget()
            right_w.setLayout(right)
            right_w.setFixedWidth(400)

            root = QHBoxLayout(self)
            root.addLayout(left, 1)
            root.addWidget(right_w)

            QShortcut(QKeySequence(Qt.Key_Space), self, self.toggle_play)
            QShortcut(QKeySequence(Qt.Key_I), self, self.btn_in.click)
            QShortcut(QKeySequence(Qt.Key_O), self, self.btn_out.click)

            self.load_settings()
            self.spin_limit.valueChanged.connect(lambda v: self.spin_target.setValue(round(v * 0.95, 1)))
            for w in (self.spin_limit, self.spin_target):
                w.valueChanged.connect(self.refresh)
            for w in (self.spin_start, self.spin_end):
                w.valueChanged.connect(self.on_trim)
            for w in (self.combo_res, self.combo_fps, self.combo_audio, self.combo_preset):
                w.currentIndexChanged.connect(self.refresh)
            self.update_dst_label()
            self.on_trim()
            self.player.setSource(QUrl.fromLocalFile(src))
            self.player.pause()  # affiche la première image

        # --- réglages
        def load_settings(self, defaults=False):
            s = (lambda key, default: default) if defaults else (
                lambda key, default: self.settings.value(key, default, type=type(default)))
            self.spin_limit.setValue(s("limit", 20.0))
            self.spin_target.setValue(s("target", 19.0))
            res = s("res", "auto")  # stocké en texte : "auto", "orig" ou "720"
            for combo, value in ((self.combo_res, int(res) if res.isdigit() else res), (self.combo_fps, s("fps", 0)),
                                 (self.combo_audio, s("audio", 128)), (self.combo_preset, s("preset", "medium"))):
                combo.setCurrentIndex(max(combo.findData(value), 0))
            if not self.info["has_audio"]:
                self.combo_audio.setCurrentIndex(self.combo_audio.findData(0))

        def load_defaults(self):
            self.load_settings(defaults=True)
            self.refresh()

        def save_settings(self):
            self.settings.setValue("limit", self.spin_limit.value())
            self.settings.setValue("target", self.spin_target.value())
            self.settings.setValue("res", str(self.combo_res.currentData()))
            self.settings.setValue("fps", self.combo_fps.currentData())
            if self.info["has_audio"]:
                self.settings.setValue("audio", self.combo_audio.currentData())
            self.settings.setValue("preset", self.combo_preset.currentData())

        # --- découpe / lecture
        def trim(self):
            return self.spin_start.value(), self.spin_end.value()

        def on_trim(self):
            start, end = self.trim()
            if not self.showing_result:
                self.timeline.selection = (start * 1000, end * 1000)
                self.timeline.update()
            self.lbl_sel.setText(f"Sélection : {fmt_time(max(end - start, 0))}")
            self.refresh()

        def on_position(self, ms):
            if not self.timeline.isSliderDown():
                self.timeline.setValue(ms)
            self.lbl_pos.setText(fmt_time(ms / 1000))
            if self.play_selection and ms >= self.trim()[1] * 1000:
                self.player.pause()
                self.play_selection = False

        def toggle_play(self):
            self.play_selection = False
            if self.player.playbackState() == QMediaPlayer.PlayingState:
                self.player.pause()
            else:
                self.player.play()

        def play_sel(self):
            self.player.setPosition(int(self.trim()[0] * 1000))
            self.play_selection = True
            self.player.play()

        def toggle_result(self):
            self.player.stop()
            self.showing_result = not self.showing_result
            self.player.setSource(QUrl.fromLocalFile(self.dst if self.showing_result else self.src))
            self.btn_result.setText("Revoir l'original" if self.showing_result else "Voir le résultat")
            self.timeline.selection = None if self.showing_result else tuple(v * 1000 for v in self.trim())
            for w in (self.btn_in, self.btn_out, self.btn_sel, self.spin_start, self.spin_end, self.btn_reset_trim):
                w.setEnabled(not self.showing_result)
            self.player.play()

        # --- estimation
        def current_plan(self):
            start, end = self.trim()
            if end - start < 0.1:
                return None
            audio = self.combo_audio.currentData() if self.info["has_audio"] else 0
            return plan(self.info, end - start, self.spin_target.value(), audio,
                        self.combo_res.currentData(), self.combo_fps.currentData())

        def refresh(self):
            limit = self.spin_limit.value()
            color = RED if self.src_mb > limit else GREEN
            self.lbl_size.setText(f"<b style='color:{color}'>{self.src_mb:.1f} Mo</b>"
                                  + (" — trop lourd pour Discord" if self.src_mb > limit else " — déjà OK"))
            p = self.current_plan()
            if p is None:
                self.lbl_est.setText("—")
                self.lbl_est_detail.setText("La sélection est vide.")
                self.btn_go.setEnabled(False)
                return
            self.btn_go.setEnabled(self.proc is None and not p["error"])
            color = RED if p["error"] or p["est_mb"] > limit else GREEN
            gain = (1 - p["est_mb"] / self.src_mb) * 100
            self.lbl_est.setText(f"<span style='color:{color}'>≈ {p['est_mb']:.1f} Mo</span>"
                                 f"<span style='font-size:10pt; color:#949ba4'>  ({-gain:+.0f} %)</span>")
            q = quality_label(p["bpp"])
            q_color = {"excellente": GREEN, "bonne": GREEN, "correcte": ORANGE}.get(q, RED)
            fps = p["fps"] or self.info["fps"]
            self.lbl_est_detail.setText(
                p["error"] or f"{p['dims'][0]}×{p['dims'][1]} · {fps:.4g} i/s · vidéo {p['v_bps'] / 1000:.0f} kb/s"
                              f"<br>Qualité estimée : <b style='color:{q_color}'>{q}</b>"
                              + ("<br>Astuce : baisse la résolution ou coupe la vidéo." if q == "faible" else ""))

        # --- sortie
        def update_dst_label(self):
            self.lbl_dst.setText(self.fontMetrics().elidedText(f"Sortie : {self.dst}", Qt.ElideMiddle, 240))
            self.lbl_dst.setToolTip(self.dst)

        def choose_dst(self):
            path, _ = QFileDialog.getSaveFileName(self, "Enregistrer sous", self.dst, "Vidéo MP4 (*.mp4)")
            if path:
                self.dst = path
                self.update_dst_label()

        def copy_file(self):
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(self.dst)])
            QApplication.clipboard().setMimeData(mime)
            self.btn_copy.setText("Copié !")

        # --- encodage
        def start(self):
            p = self.current_plan()
            if not p or p["error"]:
                return
            if Path(self.dst).resolve() == Path(self.src).resolve():
                QMessageBox.warning(self, "Discordable", "La sortie ne peut pas écraser la vidéo d'origine.")
                return
            self.save_settings()
            self.player.stop()
            if self.showing_result:
                self.toggle_result()
                self.player.stop()
            self.player.setSource(QUrl())  # libère le fichier de sortie s'il était ouvert
            self.player.setSource(QUrl.fromLocalFile(self.src))
            start, end = self.trim()
            self.job = {
                "start": start, "duration": end - start, "plan": p, "retries": 0,
                "audio": self.combo_audio.currentData() if self.info["has_audio"] else 0,
                "preset": self.combo_preset.currentData(),
                "log": os.path.join(tempfile.gettempdir(), f"discordable_{os.getpid()}"),
            }
            self.queue = list(self.cmds())
            self.n_passes = 2
            self.set_busy(True)
            self.run_next()

        def cmds(self):
            j = self.job
            return build_cmds(self.ffmpeg, self.src, self.dst, j["start"], j["duration"], j["plan"],
                              j["audio"], j["preset"], j["log"])

        def run_next(self):
            cmd = self.queue.pop(0)
            self.pass_index = self.n_passes - len(self.queue) - 1
            self.stderr = b""
            self.proc = QProcess(self)
            self.proc.readyReadStandardOutput.connect(self.on_progress)
            self.proc.readyReadStandardError.connect(
                lambda: setattr(self, "stderr", self.stderr + bytes(self.proc.readAllStandardError())))
            self.proc.finished.connect(self.on_finished)
            self.proc.start(cmd[0], cmd[1:])

        def on_progress(self):
            for line in bytes(self.proc.readAllStandardOutput()).decode(errors="ignore").splitlines():
                key, _, value = line.partition("=")
                if key == "out_time_us" and value.strip().isdigit():
                    frac = min(int(value) / 1e6 / self.job["duration"], 1)
                    total = (self.pass_index + frac) / self.n_passes
                    self.progress.setValue(int(total * 1000))
                    self.progress.setFormat(f"Passe {self.pass_index + 1}/{self.n_passes} — {total * 100:.0f} %")

        def on_finished(self, code, status):
            proc, self.proc = self.proc, None
            if proc.property("cancelled"):
                return
            if code != 0 or status != QProcess.NormalExit:
                self.cleanup(remove_output=True)
                self.set_busy(False)
                err = self.stderr.decode(errors="ignore").strip().splitlines()[-15:]
                QMessageBox.critical(self, "Échec de l'encodage", "\n".join(err) or f"ffmpeg a renvoyé {code}")
                return
            if self.queue:
                self.run_next()
                return
            size_mb = os.path.getsize(self.dst) / 1e6
            target = self.spin_target.value()
            if size_mb > target and self.job["retries"] < 2:
                # x264 a un peu dépassé : on refait seulement la passe 2 avec un débit corrigé
                self.job["retries"] += 1
                self.job["plan"] = dict(self.job["plan"], v_bps=int(self.job["plan"]["v_bps"] * target / size_mb * 0.97))
                self.queue = [self.cmds()[1]]
                self.n_passes = 1
                self.progress.setFormat("Ajustement…")
                self.run_next()
                return
            self.cleanup()
            self.set_busy(False)
            ok = size_mb <= self.spin_limit.value()
            self.progress.setValue(1000)
            self.progress.setFormat(f"Terminé : {size_mb:.2f} Mo" + ("" if ok else " (au-dessus de la limite !)"))
            for b in (self.btn_result, self.btn_folder, self.btn_copy):
                b.setEnabled(True)
            self.btn_copy.setText("Copier le fichier")

        def cancel(self):
            if self.proc:
                self.proc.setProperty("cancelled", True)
                self.proc.kill()
                self.proc.waitForFinished(3000)
                self.proc = None
            self.cleanup(remove_output=True)
            self.set_busy(False)
            self.progress.setValue(0)
            self.progress.setFormat("Annulé")

        def cleanup(self, remove_output=False):
            for f in glob.glob(self.job["log"] + "*"):
                try:
                    os.remove(f)
                except OSError:
                    pass
            if remove_output and os.path.exists(self.dst):
                try:
                    os.remove(self.dst)
                except OSError:
                    pass

        def set_busy(self, busy):
            self.btn_go.setVisible(not busy)
            self.btn_cancel.setVisible(busy)
            for b in (self.btn_result, self.btn_folder, self.btn_copy):
                b.setEnabled(False)
            if busy:
                self.progress.setValue(0)
            self.refresh()

        def closeEvent(self, e):
            if self.proc:
                self.cancel()
            super().closeEvent(e)

    app = QApplication(sys.argv)
    app.setApplicationName("Discordable")
    app.setWindowIcon(QIcon(str(Path(__file__).with_name("discordable.ico"))))
    if not find_tool("ffmpeg") or not find_tool("ffprobe"):
        QMessageBox.critical(None, "Discordable", "ffmpeg est introuvable.\n\n"
                             "Installe-le avec :  winget install Gyan.FFmpeg\n"
                             "ou place ffmpeg.exe et ffprobe.exe à côté de discordable.py.")
        return 1
    if not path:
        exts = " ".join("*" + e for e in VIDEO_EXTS)
        path, _ = QFileDialog.getOpenFileName(None, "Choisir une vidéo", "", f"Vidéos ({exts})")
        if not path:
            return 0
    try:
        win = Window(path)
    except (subprocess.CalledProcessError, StopIteration, KeyError, ValueError):
        QMessageBox.critical(None, "Discordable", f"Impossible de lire cette vidéo :\n{path}")
        return 1
    win.show()
    return app.exec()


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "--install":
        install()
    elif arg == "--uninstall":
        uninstall()
    else:
        sys.exit(run_gui(arg))
