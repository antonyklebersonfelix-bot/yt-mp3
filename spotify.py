"""Links do Spotify: lê nome/artista/duração/capa e acha a mesma música no YouTube.

O áudio NÃO vem do Spotify (é protegido); só os dados da música vêm de lá.
"""
import json
import re
import subprocess
import time

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
LINK_RE = re.compile(r"open\.spotify\.com/(?:intl-[\w-]+/)?(?:embed/)?(track|album|playlist)/([A-Za-z0-9]{22})")
BAD_WORDS = ["live", "ao vivo", "cover", "karaoke", "instrumental", "remix", "sped up", "slowed",
             "reverb", "8d", "nightcore", "acoustic", "acústico", "lyrics video", "reaction", "tutorial"]


def is_spotify(url):
    return bool(LINK_RE.search(url))


def _embed(kind, sid):
    out = subprocess.run(["/usr/bin/curl", "-fsSL", "--max-time", "20", "-A", UA,
                          f"https://open.spotify.com/embed/{kind}/{sid}"], capture_output=True, text=True)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', out.stdout, re.S)
    if out.returncode != 0 or not m:
        raise RuntimeError("Não consegui ler esse link do Spotify (ele é público?).")
    return json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]


def _cover(entity):
    imgs = (entity.get("visualIdentity") or {}).get("image") or (entity.get("coverArt") or {}).get("sources") or []
    imgs = sorted(imgs, key=lambda i: i.get("maxWidth") or i.get("width") or 0, reverse=True)
    return imgs[0]["url"] if imgs else None


def _clean(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def expand(url):
    """Retorna (nome da coleção, lista de faixas). Faixa = {id, title, artist, duration}."""
    kind, sid = LINK_RE.search(url).groups()
    e = _embed(kind, sid)
    if kind == "track":
        artist = ", ".join(a["name"] for a in e.get("artists", []))
        return None, [{"id": sid, "title": _clean(e.get("title") or e.get("name")), "artist": _clean(artist),
                       "duration": (e.get("duration") or 0) / 1000, "cover": _cover(e)}]
    tracks = []
    for t in e.get("trackList", []):
        tid = (t.get("uri") or "").rsplit(":", 1)[-1]
        if t.get("entityType", "track") != "track":
            continue
        tracks.append({"id": tid, "title": _clean(t.get("title")), "artist": _clean(t.get("subtitle")),
                       "duration": (t.get("duration") or 0) / 1000, "cover": None})
    album_cover = _cover(e) if kind == "album" else None
    for t in tracks:
        t["cover"] = album_cover
        if kind == "album":
            t["album"] = _clean(e.get("name"))
    return _clean(e.get("name")), tracks


def track_cover(track):
    """Capa da faixa (playlists não trazem a capa de cada música)."""
    if track.get("cover"):
        return track["cover"]
    try:
        return _cover(_embed("track", track["id"]))
    except Exception:  # noqa: BLE001
        return None


def _score(c, track):
    title = c["title"].lower()
    want = f'{track["title"]} {track["artist"]}'.lower()
    s = 0.0
    if track["duration"] and c["duration"]:
        diff = abs(c["duration"] - track["duration"])
        s -= diff if diff > 3 else 0
        if diff > max(20, track["duration"] * 0.15):
            s -= 100
    if c["channel"].endswith(" - Topic"):
        s += 25  # áudio oficial distribuído pela gravadora
    if "official audio" in title or "áudio oficial" in title or "audio oficial" in title:
        s += 10
    for w in BAD_WORDS:
        if w in title and w not in want:
            s -= 40
    first_artist = track["artist"].split(",")[0].strip().lower()
    if first_artist and (first_artist in title or first_artist in c["channel"].lower()):
        s += 10
    if track["title"].lower().split(" (")[0] in title:
        s += 10
    return s


class SearchFailed(RuntimeError):
    pass


def simplify_title(title):
    """Tira 'Remastered 2011', '(feat. X)', '- Radio Edit' etc. para a busca."""
    t = re.sub(r"\s*[\(\[](?:feat|ft|with|com)\.?\s[^\)\]]*[\)\]]", "", title, flags=re.I)
    t = re.sub(r"\s+-\s+(?:\d{4}\s+)?(?:remaster(?:ed)?|radio edit|single version|mono|stereo)\b.*$", "", t, flags=re.I)
    return t.strip() or title


def _search(query, ytdlp, deno, n=8):
    """Busca no YouTube, tentando de novo se o YouTube recusar/demorar."""
    last = ""
    for attempt in range(3):
        try:
            out = subprocess.run(
                [ytdlp, f"ytsearch{n}:{query}", "--flat-playlist", "--no-colors", "--js-runtimes", f"deno:{deno}",
                 "--print", "%(id)s\t%(duration)s\t%(channel)s\t%(title)s"],
                capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            last = "o YouTube demorou demais para responder"
        else:
            if out.stdout.strip():
                return out.stdout
            if out.returncode == 0:
                return ""  # busca funcionou, só não teve resultado
            last = next((l for l in reversed(out.stderr.splitlines()) if "ERROR" in l), "erro na busca")
        time.sleep(3 * (attempt + 1))
    raise SearchFailed(f"A busca no YouTube falhou ({last}). Tente de novo daqui a pouco.")


def find_on_youtube(track, ytdlp, deno):
    """Retorna (url, aviso). Lança SearchFailed se o YouTube não respondeu."""
    first_artist = track["artist"].split(",")[0].strip()
    title = simplify_title(track["title"])
    cands = {}
    for query in (f"{first_artist} - {title}", f"{title} {first_artist} audio"):
        for c in _parse(_search(query, ytdlp, deno)):
            cands.setdefault(c["id"], c)
        best = max(cands.values(), key=lambda c: _score(c, track), default=None)
        if best and _score(best, track) > 0:
            break
    if not best:
        return None, None
    warn = None
    if _score(best, track) <= -100:
        # nada com duração parecida: baixa o mais próximo, mas avisa
        best = min(cands.values(), key=lambda c: abs(c["duration"] - track["duration"]) if c["duration"] else 1e9)
        if not best["duration"] or abs(best["duration"] - track["duration"]) > track["duration"] * 0.5:
            return None, None
        warn = f"versão com duração diferente ({_mmss(best['duration'])} no YouTube, {_mmss(track['duration'])} no Spotify)"
    return f"https://www.youtube.com/watch?v={best['id']}", warn


def _mmss(s):
    s = int(s or 0)
    return f"{s // 60}:{s % 60:02d}"


def _parse(stdout):
    cands = []
    for line in stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) < 4:
            continue
        vid, dur, channel, title = parts
        try:
            dur = float(dur)
        except ValueError:
            dur = 0
        cands.append({"id": vid, "duration": dur, "channel": channel if channel != "NA" else "", "title": title})
    return cands
