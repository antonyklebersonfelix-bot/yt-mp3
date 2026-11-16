"""Descobrir qual música um vídeo usa.

Ordem: (1) dados do próprio post (Instagram / YouTube Music),
(2) Shazam (ShazamIO, não oficial), (3) AudD, se houver chave.
"""
import asyncio
import json
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

SEG_LEN = 12


def links(title, artist):
    q = quote(f"{artist} {title}".strip())
    return {
        "spotify": f"https://open.spotify.com/search/{q}",
        "apple": f"https://music.apple.com/search?term={q}",
        "youtube": f"https://www.youtube.com/results?search_query={q}",
    }


def result(source, title, artist, album=None, cover=None, extra_links=None):
    r = {"source": source, "title": title, "artist": artist, "album": album, "cover": cover,
         "links": links(title, artist)}
    r["links"].update(extra_links or {})
    return r


# ---------- 1. metadados do post ----------

IG_PATTERNS = [
    # Reels com áudio da biblioteca do Instagram
    re.compile(r'"clips_music_attribution_info"\s*:\s*(\{[^{}]*\})'),
    re.compile(r'"music_asset_info"\s*:\s*(\{[^{}]*\})'),
]


def from_pages(tmp: Path):
    for dump in sorted(tmp.glob("*.dump")):
        text = dump.read_text(errors="ignore")
        for pat in IG_PATTERNS:
            for m in pat.finditer(text):
                try:
                    d = json.loads(m.group(1))
                except ValueError:
                    continue
                if d.get("uses_original_audio") or d.get("is_original_audio") is True:
                    continue
                title = d.get("song_name") or d.get("title")
                artist = d.get("artist_name") or d.get("display_artist")
                if title and artist and "original audio" not in title.lower() and "áudio original" not in title.lower():
                    cover = d.get("cover_artwork_uri") or d.get("cover_artwork_thumbnail_uri")
                    return result("Instagram", title, artist, cover=cover)
    return None


def from_info(info):
    title, artist = info.get("track"), info.get("artist") or info.get("creator")
    if info.get("track") and artist:
        return result("Dados do vídeo", title, artist, album=info.get("album"))
    return None


# ---------- 2. Shazam ----------

def cut(ffmpeg, src, dst, start, length):
    subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-ss", str(start), "-t", str(length),
                    "-i", str(src), "-ac", "1", "-ar", "16000", str(dst)], check=True)


def offsets(duration):
    if not duration or duration <= SEG_LEN + 2:
        return [0]
    cands = [0, duration / 2 - SEG_LEN / 2, 10, 25, 40, 60, 90]
    out = []
    for c in cands:
        c = max(0, int(c))
        if c + 4 < duration and all(abs(c - o) >= 8 for o in out):
            out.append(c)
    return out[:5]


def shazam(path):
    from shazamio import Shazam

    async def go():
        return await Shazam().recognize(str(path))

    data = asyncio.run(go())
    t = data.get("track")
    if not t:
        return None
    album = None
    for s in t.get("sections", []):
        for m in s.get("metadata", []) or []:
            if m.get("title") == "Album":
                album = m.get("text")
    return result("Shazam", t.get("title"), t.get("subtitle"), album=album,
                  cover=(t.get("images") or {}).get("coverart"),
                  extra_links={"shazam": t.get("url")} if t.get("url") else None)


# ---------- 3. AudD ----------

def audd(path, token):
    out = subprocess.run(["/usr/bin/curl", "-sS", "--max-time", "60", "https://api.audd.io/",
                          "-F", f"api_token={token}", "-F", "return=apple_music,spotify",
                          "-F", f"file=@{path}"], capture_output=True, text=True)
    data = json.loads(out.stdout or "{}")
    if data.get("status") == "error":
        raise RuntimeError(f"AudD: {(data.get('error') or {}).get('error_message', 'erro')}")
    r = data.get("result")
    if not r:
        return None
    extra = {}
    if (r.get("spotify") or {}).get("external_urls", {}).get("spotify"):
        extra["spotify"] = r["spotify"]["external_urls"]["spotify"]
    if (r.get("apple_music") or {}).get("url"):
        extra["apple"] = r["apple_music"]["url"]
    cover = (((r.get("spotify") or {}).get("album") or {}).get("images") or [{}])[0].get("url")
    return result("AudD", r.get("title"), r.get("artist"), album=r.get("album"), cover=cover, extra_links=extra)


# ---------- orquestração ----------

def recognize(url, ytdlp, deno, ffmpeg, audd_token=None, status=lambda msg: None):
    with tempfile.TemporaryDirectory(prefix="ytmp3-") as tmpdir:
        tmp = Path(tmpdir)
        status("Baixando o áudio do vídeo…")
        for attempt in range(3):  # o YouTube às vezes devolve 403 temporário
            proc = subprocess.run(
                [ytdlp, url, "--no-playlist", "-f", "bestaudio/best", "--write-pages", "--write-info-json",
                 "--js-runtimes", f"deno:{deno}", "--ffmpeg-location", ffmpeg, "--no-colors",
                 "-o", str(tmp / "audio.%(ext)s")],
                cwd=tmp, capture_output=True, text=True)
            if proc.returncode == 0 or "403" not in proc.stdout + proc.stderr:
                break
            status(f"O site recusou o download, tentando de novo ({attempt + 2}/3)…")
        if proc.returncode != 0:
            err = next((l for l in reversed(proc.stdout.splitlines() + proc.stderr.splitlines()) if "ERROR" in l), "erro ao baixar")
            raise RuntimeError(err)
        info = {}
        if (ij := next(tmp.glob("*.info.json"), None)):
            info = json.loads(ij.read_text())

        status("Procurando a música nos dados do post…")
        found = from_pages(tmp) or from_info(info)
        if found:
            return found

        audio = next(p for p in tmp.glob("audio.*") if not p.name.endswith(".json"))
        tried = []
        for i, off in enumerate(offsets(info.get("duration")), 1):
            status(f"Perguntando ao Shazam (tentativa {i})…")
            seg = tmp / f"seg{i}.wav"
            cut(ffmpeg, audio, seg, off, SEG_LEN)
            try:
                if (r := shazam(seg)):
                    return r
            except Exception as e:  # noqa: BLE001
                tried.append(f"Shazam indisponível ({e.__class__.__name__})")
                break

        if audd_token:
            status("Perguntando ao AudD…")
            seg = tmp / "audd.mp3"
            start = max(0, int((info.get("duration") or 0) / 2) - 10)
            subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-ss", str(start), "-t", "20", "-i", str(audio),
                            "-ac", "1", "-b:a", "128k", str(seg)], check=True)
            if (r := audd(seg, audd_token)):
                return r
        return None
