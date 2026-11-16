#!/bin/sh
# Publica uma versão nova do YT MP3 no GitHub.
#   ./release.sh 1.2.0 "O que mudou nesta versão"
# Os apps dos sócios avisam da versão nova e se atualizam com um clique.
set -e
cd "$(dirname "$0")"

VER="$1"; NOTES="${2:-Melhorias e correções.}"
[ -n "$VER" ] || { echo "uso: ./release.sh 1.2.0 \"notas\""; exit 1; }
REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)

# versão + repositório embutidos no app
cat > version.py <<EOF
# Atualizado pelo release.sh — não edite à mão.
VERSION = "$VER"
REPO = "$REPO"  # dono/repositório no GitHub
EOF

# ambiente de build
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q pyinstaller pywebview imageio-ffmpeg shazamio certifi pillow
mkdir -p build_assets/bin
cp "$(.venv/bin/python -c 'import imageio_ffmpeg as i; print(i.get_ffmpeg_exe())')" build_assets/bin/ffmpeg

.venv/bin/pyinstaller --noconfirm --clean --windowed --name "YT MP3" \
  --icon build_assets/icon.icns --osx-bundle-identifier local.ytmp3.app \
  --add-data "index.html:." --add-binary "build_assets/bin/ffmpeg:bin" \
  --collect-all shazamio --collect-all shazamio_core --collect-data certifi \
  --exclude-module imageio_ffmpeg --exclude-module PIL app.py >/dev/null 2>&1

mkdir -p compartilhar
rm -f compartilhar/YT-MP3.zip "compartilhar/YT MP3.zip"
ditto -c -k --keepParent "dist/YT MP3.app" compartilhar/YT-MP3.zip
python3 -c 'import json,sys; json.dump({"version": sys.argv[1], "notes": sys.argv[2]}, open("compartilhar/latest.json", "w"), ensure_ascii=False)' "$VER" "$NOTES"

git add -A && git commit -qm "Versão $VER" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" && git push -q
gh release create "v$VER" compartilhar/YT-MP3.zip compartilhar/latest.json --title "YT MP3 $VER" --notes "$NOTES"
echo "Publicado: https://github.com/$REPO/releases/tag/v$VER"
