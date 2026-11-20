#!/usr/bin/env python3
"""YT → MP3: app para converter links do YouTube em MP3.

- Abre numa janela própria (pywebview). Com --browser, abre no navegador.
- ffmpeg vem dentro do app; yt-dlp e deno ficam em
  ~/Library/Application Support/YT-MP3/bin e são baixados/atualizados sozinhos.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:  # certificados para o Shazam (aiohttp) funcionarem em qualquer Mac
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass

import recognize
import spotify
import updater

APP_NAME = "YT MP3"
OUT_DIR = Path(os.environ.get("YTMP3_OUT") or Path.home() / "Downloads" / "Musicas")
VIDEO_DIR = Path(os.environ.get("YTMP3_VIDEO_OUT") or Path.home() / "Downloads" / "Videos")
# "qualidade" escolhida na tela: bitrate do MP3 ou um dos modos de vídeo
AUDIO_QUALITIES = {"0", "2", "5"}
VIDEO_MODES = {
    # maior resolução que existir (4K/8K, geralmente VP9/AV1); mp4 se couber, senão mkv
    "vmax": ["-f", "bv*+ba/b", "-S", "res,fps,hdr:12", "--merge-output-format", "mp4/mkv"],
    # H.264 + AAC até 1080p: abre em qualquer lugar (QuickTime, iPhone, TVs)
    "vmp4": ["-f", "bv*[vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
             "-S", "vcodec:h264,res:1080,acodec:aac", "--merge-output-format", "mp4"],
}
SUPPORT = Path.home() / "Library" / "Application Support" / "YT-MP3"
BIN = SUPPORT / "bin"
YTDLP = BIN / "yt-dlp"
DENO = BIN / "deno"
CONFIG = SUPPORT / "config.json"
YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos"
DENO_URL = "https://github.com/denoland/deno/releases/latest/download/deno-{arch}-apple-darwin.zip"

# Pasta com index.html e bin/ffmpeg (dentro do .app quando empacotado).
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def find_ffmpeg():
    bundled = RES / "bin" / "ffmpeg"
    if bundled.exists():
        return str(bundled)
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return shutil.which("ffmpeg")


FFMPEG = find_ffmpeg()

jobs = {}
finds = {}
setup = {"state": "preparando", "message": "Verificando componentes…", "version": None}
ready = threading.Event()
lock = threading.Lock()
PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%")
DEST_RE = re.compile(r"\[ExtractAudio\] Destination: (.+)$")


# ---------- componentes (yt-dlp + deno) ----------

def set_setup(**kw):
    with lock:
        setup.update(kw)


def curl(url, dest):
    # /usr/bin/curl usa os certificados do macOS; funciona em qualquer Mac.
    tmp = dest.with_suffix(".part")
    subprocess.run(["/usr/bin/curl", "-fsSL", "--retry", "2", "-o", str(tmp), url], check=True)
    tmp.replace(dest)


def ensure_tools():
    try:
        BIN.mkdir(parents=True, exist_ok=True)
        if not YTDLP.exists():
            set_setup(message="Baixando o yt-dlp (só na primeira vez)…")
            curl(YTDLP_URL, YTDLP)
            YTDLP.chmod(0o755)
        else:
            set_setup(message="Procurando atualizações do yt-dlp…")
            subprocess.run([str(YTDLP), "-U"], capture_output=True, timeout=180)
        if not DENO.exists():
            set_setup(message="Baixando o deno (só na primeira vez)…")
            arch = "aarch64" if subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip() == "arm64" else "x86_64"
            zpath = BIN / "deno.zip"
            curl(DENO_URL.format(arch=arch), zpath)
            with zipfile.ZipFile(zpath) as z:
                z.extract("deno", BIN)
            zpath.unlink()
            DENO.chmod(0o755)
        version = subprocess.run([str(YTDLP), "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
        set_setup(state="pronto", message="", version=version)
        ready.set()
    except Exception as e:  # noqa: BLE001
        set_setup(state="erro", message=f"Não consegui preparar os componentes ({e}). Verifique a internet e reabra o app.")


# ---------- downloads ----------

def update(job_id, **kw):
    with lock:
        jobs[job_id].update(kw)


slots = threading.Semaphore(3)  # no máximo 3 downloads ao mesmo tempo


def ytdlp_cmd(url, quality, out_template, thumbnail=True):
    media = VIDEO_MODES[quality] if quality in VIDEO_MODES else \
        ["-x", "--audio-format", "mp3", "--audio-quality", quality]
    cmd = [
        str(YTDLP), url, *media,
        "--embed-metadata",
        "--ffmpeg-location", FFMPEG,
        "--js-runtimes", f"deno:{DENO}",
        "--newline", "--no-colors",
        "-o", out_template,
        "--print", "before_dl:TITLE:%(title)s",
        "--print", "after_move:FILE:%(filepath)s",
    ]
    if thumbnail and quality not in VIDEO_MODES:  # capa só no MP3
        cmd.append("--embed-thumbnail")
    return cmd


def download_with_retry(job_id, cmd, keep_title=False):
    """Roda o yt-dlp, tentando de novo em 403 temporário. Retorna True se deu certo."""
    for attempt in range(3):
        ok, retry = download_once(job_id, cmd, keep_title)
        if ok or not retry or attempt == 2:
            return ok
        update(job_id, status=f"tentando de novo ({attempt + 2}/3)", progress=0, error=None)
    return False


def run_job(job_id, url, quality, playlist):
    ready.wait()
    with slots:
        folder = VIDEO_DIR if quality in VIDEO_MODES else OUT_DIR
        folder.mkdir(parents=True, exist_ok=True)
        cmd = ytdlp_cmd(url, quality, str(folder / "%(title)s.%(ext)s"))
        if not playlist:
            cmd.append("--no-playlist")
        update(job_id, status="baixando")
        if download_with_retry(job_id, cmd):
            update(job_id, status="pronto", progress=100)


def safe_name(s):
    return re.sub(r'[/\\:*?"<>|]+', "-", s).strip(" .")[:150] or "musica"


def run_spotify_track(job_id, track, quality):
    """Música do Spotify: acha no YouTube, baixa e grava nome/artista/capa do Spotify."""
    ready.wait()
    with slots:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        final = OUT_DIR / f"{safe_name(track['artist'].split(',')[0])} - {safe_name(track['title'])}.mp3"
        if final.exists():
            update(job_id, status="pronto", progress=100, file=final.name + " (já existia)")
            return
        update(job_id, status="procurando no YouTube")
        search_page = recognize.links(track["title"], track["artist"].split(",")[0])["youtube"]
        try:
            url, warn = spotify.find_on_youtube(track, str(YTDLP), str(DENO))
        except Exception as e:  # noqa: BLE001
            update(job_id, status="erro", error=str(e), search=search_page)
            return
        if not url:
            update(job_id, status="erro", search=search_page,
                   error="Não achei essa música no YouTube. Procure e cole o link do vídeo aqui em cima.")
            return
        update(job_id, url=url, status="baixando", warn=warn)
        with tempfile.TemporaryDirectory(prefix="ytmp3-sp-") as tmpdir:
            tmp = Path(tmpdir)
            cmd = ytdlp_cmd(url, quality, str(tmp / "audio.%(ext)s"), thumbnail=False) + ["--no-playlist"]
            if not download_with_retry(job_id, cmd, keep_title=True):
                return
            update(job_id, status="gravando capa e nome")
            src = tmp / "audio.mp3"
            cover = None
            if (cover_url := spotify.track_cover(track)):
                cover = tmp / "cover.jpg"
                if subprocess.run(["/usr/bin/curl", "-fsSL", "--max-time", "20", "-o", str(cover), cover_url]).returncode:
                    cover = None
            tags = ["-metadata", f"title={track['title']}", "-metadata", f"artist={track['artist']}"]
            if track.get("album"):
                tags += ["-metadata", f"album={track['album']}"]
            out = tmp / "final.mp3"
            if cover:
                cmd = [FFMPEG, "-loglevel", "error", "-y", "-i", str(src), "-i", str(cover),
                       "-map", "0:a", "-map", "1:v", "-c", "copy", "-id3v2_version", "3",
                       "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)",
                       "-disposition:v", "attached_pic", *tags, str(out)]
            else:
                cmd = [FFMPEG, "-loglevel", "error", "-y", "-i", str(src), "-map", "0:a", "-c", "copy",
                       "-id3v2_version", "3", *tags, str(out)]
            if subprocess.run(cmd).returncode != 0:
                out = src  # se a etiqueta falhar, fica o áudio mesmo assim
            shutil.move(str(out), final)
        update(job_id, status="pronto", progress=100, file=final.name)


def add_spotify(holder_id, url, quality):
    """Lê o link do Spotify e cria um download para cada música."""
    try:
        name, tracks = spotify.expand(url)
    except Exception as e:  # noqa: BLE001
        update(holder_id, status="erro", error=str(e))
        return
    if not tracks:
        update(holder_id, status="erro", error="Nenhuma música encontrada nesse link.")
        return
    with lock:
        del jobs[holder_id]
        for t in tracks:
            jid = uuid.uuid4().hex[:8]
            jobs[jid] = {"id": jid, "url": url, "title": f"{t['artist']} – {t['title']}",
                         "status": "na fila", "progress": 0, "file": None, "error": None,
                         "group": name}
            threading.Thread(target=run_spotify_track, args=(jid, t, quality), daemon=True).start()


def download_once(job_id, cmd, keep_title=False):
    """Roda o yt-dlp uma vez. Retorna (deu_certo, vale_tentar_de_novo)."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        last_lines = []
        for line in proc.stdout:
            line = line.rstrip()
            last_lines = (last_lines + [line])[-5:]
            if line.startswith("TITLE:"):
                if not keep_title:
                    update(job_id, title=line[6:])
                update(job_id, progress=0, status="baixando")
            elif m := PROGRESS_RE.search(line):
                update(job_id, progress=float(m.group(1)))
            elif line.startswith("FILE:"):
                if not keep_title:
                    update(job_id, file=Path(line[5:]).name)
            elif "[Merger]" in line:
                update(job_id, status="juntando vídeo e áudio", progress=100)
            elif "[ExtractAudio]" in line:
                update(job_id, status="convertendo", progress=100)
                if not keep_title and (d := DEST_RE.search(line)):
                    update(job_id, file=Path(d.group(1)).name)
        proc.wait()
        if proc.returncode == 0:
            return True, False
        err = next((l for l in reversed(last_lines) if "ERROR" in l), last_lines[-1] if last_lines else "erro")
        update(job_id, status="erro", error=err)
        return False, "403" in err
    except Exception as e:  # noqa: BLE001
        update(job_id, status="erro", error=str(e))
        return False, False


# ---------- descobrir música ----------

def load_config():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    SUPPORT.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg))


def run_find(fid, url):
    def status(msg):
        with lock:
            finds[fid]["message"] = msg

    status("Na fila…")
    ready.wait()
    try:
        r = recognize.recognize(url, str(YTDLP), str(DENO), FFMPEG,
                                audd_token=load_config().get("audd_token"), status=status)
        with lock:
            finds[fid].update(status="pronto" if r else "nada", result=r,
                              message="" if r else "Não consegui identificar a música.")
    except Exception as e:  # noqa: BLE001
        with lock:
            finds[fid].update(status="erro", message=str(e))


# ---------- servidor local ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = (RES / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/jobs":
            with lock:
                self.send_json({"jobs": list(jobs.values()), "finds": list(finds.values()),
                                "outDir": str(OUT_DIR), "videoDir": str(VIDEO_DIR), "setup": dict(setup), "update": updater.snapshot()})
        elif self.path == "/api/config":
            self.send_json({"hasAudd": bool(load_config().get("audd_token"))})
        else:
            self.send_json({"error": "não encontrado"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/download":
            quality = str(data.get("quality", "0"))
            if quality not in AUDIO_QUALITIES and quality not in VIDEO_MODES:
                quality = "0"
            playlist = bool(data.get("playlist"))
            created = []
            for url in data.get("urls", []):
                url = url.strip()
                if not url.startswith(("http://", "https://", "ytsearch1:")):
                    continue
                job_id = uuid.uuid4().hex[:8]
                sp = spotify.is_spotify(url)
                with lock:
                    jobs[job_id] = {"id": job_id, "url": url, "title": url,
                                    "status": "lendo o Spotify" if sp else "na fila",
                                    "progress": 0, "file": None, "error": None}
                # Spotify é sempre música (MP3)
                sp_quality = quality if quality in AUDIO_QUALITIES else "0"
                target, args = (add_spotify, (job_id, url, sp_quality)) if sp else (run_job, (job_id, url, quality, playlist))
                threading.Thread(target=target, args=args, daemon=True).start()
                created.append(job_id)
            self.send_json({"created": created})
        elif self.path == "/api/find":
            url = str(data.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                self.send_json({"error": "link inválido"}, 400)
                return
            fid = uuid.uuid4().hex[:8]
            with lock:
                finds[fid] = {"id": fid, "url": url, "status": "procurando", "message": "", "result": None}
            threading.Thread(target=run_find, args=(fid, url), daemon=True).start()
            self.send_json({"id": fid})
        elif self.path == "/api/config":
            cfg = load_config()
            token = str(data.get("audd_token", "")).strip()
            if token:
                cfg["audd_token"] = token
            else:
                cfg.pop("audd_token", None)
            save_config(cfg)
            self.send_json({"hasAudd": bool(token)})
        elif self.path == "/api/clear-finds":
            with lock:
                for k in [k for k, f in finds.items() if f["status"] != "procurando"]:
                    del finds[k]
            self.send_json({"ok": True})
        elif self.path == "/api/open-url":
            u = str(data.get("url", ""))
            if u.startswith("https://"):
                subprocess.Popen(["open", u])
            self.send_json({"ok": True})
        elif self.path == "/api/update":
            updater.start_apply()
            self.send_json({"ok": True})
        elif self.path == "/api/open-folder":
            folder = VIDEO_DIR if data.get("video") else OUT_DIR
            folder.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["open", str(folder)])
            self.send_json({"ok": True})
        elif self.path == "/api/clear":
            with lock:
                for k in [k for k, j in jobs.items() if j["status"] in ("pronto", "erro")]:
                    del jobs[k]
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "não encontrado"}, 404)


def free_port(preferred=None):
    if preferred:
        return preferred
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    browser = "--browser" in sys.argv
    port = free_port(8765 if browser else None)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=ensure_tools, daemon=True).start()
    updater.start()
    url = f"http://127.0.0.1:{port}/"

    if browser:
        print(f"{APP_NAME} rodando em {url}  (MP3s em {OUT_DIR})")
        if "--no-open" not in sys.argv:
            import webbrowser
            webbrowser.open(url)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        return

    import webview
    webview.create_window(APP_NAME, url, width=760, height=720, min_size=(520, 480))
    webview.start()


if __name__ == "__main__":
    main()
