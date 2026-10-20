"""Regenerate the bundled certification test card (``assets/ibx_test_card.mp4``): our OWN content.

The clip is ffmpeg's built-in ``testsrc`` pattern (colour bars + a running counter), 2 seconds, 160x90,
10 fps, H.264 (yuv420p, no audio): about 10 KB. Nothing third-party is in it.

    uv run python -m app.connectors.peertube.make_test_card            # uses ``ffmpeg`` on PATH, else Docker
    uv run python -m app.connectors.peertube.make_test_card --docker   # force the Docker route

The Docker route runs ffmpeg from the pinned PeerTube image we already use for the ``video`` profile
(``chocobozzz/peertube``), unmodified, as a tool only; ffmpeg is not shipped with this project.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ASSET = Path(__file__).with_name("assets") / "ibx_test_card.mp4"
FFMPEG_IMAGE = "chocobozzz/peertube:v8.3.0-trixie"
ARGS = ["-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=160x90:rate=10",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryslow", "-crf", "35", "-movflags", "+faststart",
        "-metadata", "title=IncuBrix certification test card", "-metadata", "comment=ffmpeg testsrc; own content"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docker", action="store_true", help="run ffmpeg from the pinned PeerTube image")
    args = parser.parse_args(argv)
    ASSET.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("ffmpeg") and not args.docker:
        cmd = ["ffmpeg", *ARGS, str(ASSET)]
    else:
        cmd = ["docker", "run", "--rm", "--network", "none", "-v", f"{ASSET.parent}:/out", "--entrypoint", "ffmpeg",
               FFMPEG_IMAGE, *ARGS, f"/out/{ASSET.name}"]
    subprocess.run(cmd, check=True)
    print(f"wrote {ASSET} ({ASSET.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
