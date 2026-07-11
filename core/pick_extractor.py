"""
core/pick_extractor.py
Vision extraction of betting picks from media attached to tracked-account
posts. Sharps increasingly post their card as an image (or a video that
scrolls through picks) with little or no text in the tweet body — text-only
analysis misses those entirely.

Approach: download attached images (photos, plus the preview frame of
videos — full video frame sampling needs ffmpeg, not installed), hand them
to `codex exec -i` (already authenticated on this machine; same CLI
market-digests uses for its Tier 2 LLM) and get back a plain-text
transcription of the picks. The transcript is appended to the post's
`text` field, so the existing matcher / sentiment / sharp-weighting layers
consume media picks with no downstream changes.

Cost control: transcripts are cached per post id in
data/<sport>/state/media_picks_cache.json, a per-run image budget caps
codex calls, and extraction only runs for tracked-tier timeline posts.

Disable with PICK_VISION=off.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests

log = logging.getLogger("pipeline.pick_extractor")

NO_PICKS = "NO_PICKS"
MAX_IMAGES_PER_POST = 2
DEFAULT_IMAGE_BUDGET = 25
CODEX_TIMEOUT_S = 180
CACHE_MAX_ENTRIES = 800
MARKER = "[media picks]"

PROMPT = """\
The attached image(s) come from one tweet by a sports-betting picks account.
Transcribe every betting pick shown into plain text, one pick per line, in
the form: <team or player> <market> <line> <side> <odds if shown> <units/stake if shown>.
Examples:
  Caitlin Clark over 27.5 points -115 1u
  Lynx -4.5 -110
  Yankees ML +120 2u
Only transcribe picks actually visible in the image. Do not add commentary,
headers, or analysis. If the image(s) contain no betting picks, output
exactly NO_PICKS."""


def enabled() -> bool:
    if os.environ.get("PICK_VISION", "").lower() in ("off", "0", "false"):
        return False
    return shutil.which("codex") is not None


def _download(url: str, dest_dir: Path, name: str) -> Path:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    suffix = ".jpg"
    ctype = r.headers.get("Content-Type", "")
    if "png" in ctype:
        suffix = ".png"
    path = dest_dir / f"{name}{suffix}"
    path.write_bytes(r.content)
    return path


def _transcribe(image_paths: list) -> str:
    """One codex exec call for all images of one post. Returns transcript
    text, NO_PICKS, or '' on failure."""
    workdir = tempfile.mkdtemp(prefix="pick-vision-")
    outfile = Path(workdir) / "last-message.txt"
    argv = ["codex", "exec", "--skip-git-repo-check", "-s", "read-only",
            "-C", workdir, "-o", str(outfile)]
    for p in image_paths:
        argv += ["-i", str(p)]
    argv.append("-")
    try:
        proc = subprocess.run(argv, input=PROMPT.encode(),
                              capture_output=True, timeout=CODEX_TIMEOUT_S)
        if proc.returncode != 0:
            log.warning(f"codex exec failed ({proc.returncode}): "
                        f"{proc.stderr.decode(errors='replace')[:300]}")
            return ""
        return outfile.read_text().strip() if outfile.exists() else ""
    except subprocess.TimeoutExpired:
        log.warning("codex exec timed out")
        return ""
    except Exception as e:
        log.warning(f"codex exec error: {e}")
        return ""
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _load_cache(cache_path: Path) -> dict:
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict, cache_path: Path):
    if len(cache) > CACHE_MAX_ENTRIES:
        # dict preserves insertion order; drop the oldest entries
        for key in list(cache)[:len(cache) - CACHE_MAX_ENTRIES]:
            del cache[key]
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=1))


def extract_media_picks(posts: list, state_dir,
                        image_budget: int = DEFAULT_IMAGE_BUDGET) -> int:
    """Transcribe pick images on tracked-timeline posts and append the
    transcript to each post's text (marked, so it's greppable). Mutates
    posts in place; returns the number of posts enriched."""
    if not enabled():
        log.info("pick vision skipped (codex missing or PICK_VISION=off)")
        return 0

    cache_path = Path(state_dir) / "media_picks_cache.json"
    cache = _load_cache(cache_path)
    spent = 0
    enriched = 0
    dirty = False

    for post in posts:
        if post.get("source_type") != "timeline" or not post.get("media"):
            continue
        pid = post.get("id") or post.get("url")
        if not pid:
            continue
        if pid in cache:
            transcript = cache[pid]
        else:
            if spent >= image_budget:
                continue
            urls = []
            for m in post["media"][:MAX_IMAGES_PER_POST]:
                if m.get("image_url"):
                    urls.append(m["image_url"])
            if not urls:
                continue
            tmpdir = Path(tempfile.mkdtemp(prefix="pick-media-"))
            try:
                paths = []
                for i, u in enumerate(urls):
                    try:
                        paths.append(_download(u, tmpdir, f"media{i}"))
                    except Exception as e:
                        log.warning(f"media download failed {u}: {e}")
                if not paths:
                    continue
                spent += len(paths)
                transcript = _transcribe(paths)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
            cache[pid] = transcript
            dirty = True

        if transcript and transcript != NO_PICKS:
            if MARKER not in post.get("text", ""):
                post["text"] = f"{post.get('text', '')}\n{MARKER} {transcript}".strip()
            enriched += 1

    if dirty:
        _save_cache(cache, cache_path)
    log.info(f"pick vision: {enriched} posts enriched, "
             f"{spent} images transcribed this run")
    return enriched
