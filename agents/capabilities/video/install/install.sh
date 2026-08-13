#!/usr/bin/env bash
# Host-install for the video capability: fetch the pinned static ffmpeg build,
# verify its hash, and populate <package_root>/bin with ffmpeg + ffprobe.
#
# Idempotent: if bin/ffmpeg exists and reports the pinned version, exits 0
# without network access. Extraction uses python3's tarfile (handles .tar.xz
# without an xz binary). Set FFMPEG_TARBALL_CACHE to a pre-downloaded tarball
# to skip the fetch (used by tests; the hash is verified either way).
set -euo pipefail

PKG_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIN="$PKG_ROOT/install/ffmpeg.pin"
BIN="$PKG_ROOT/bin"

url=$(grep '^url=' "$PIN" | cut -d= -f2-)
sha=$(grep '^sha256=' "$PIN" | cut -d= -f2-)
version=$(grep '^version=' "$PIN" | cut -d= -f2-)

if [ -x "$BIN/ffmpeg" ] && "$BIN/ffmpeg" -version 2>/dev/null | head -1 | grep -q "$version"; then
  echo "ffmpeg $version already installed at $BIN/ffmpeg"
  exit 0
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
tarball="$work/ffmpeg.tar.xz"

if [ -n "${FFMPEG_TARBALL_CACHE:-}" ] && [ -f "$FFMPEG_TARBALL_CACHE" ]; then
  cp "$FFMPEG_TARBALL_CACHE" "$tarball"
else
  curl -fsSL "$url" -o "$tarball"
fi

echo "$sha  $tarball" | sha256sum -c - >/dev/null

mkdir -p "$BIN"
python3 - "$tarball" "$BIN" <<'PYEOF'
import sys, tarfile
tarball, bindir = sys.argv[1], sys.argv[2]
with tarfile.open(tarball) as t:
    for m in t.getmembers():
        base = m.name.split("/")[-1]
        if base in ("ffmpeg", "ffprobe") and m.isfile():
            m.name = base
            t.extract(m, bindir)
PYEOF
chmod +x "$BIN/ffmpeg" "$BIN/ffprobe"
"$BIN/ffmpeg" -version | head -1
echo "installed to $BIN"
