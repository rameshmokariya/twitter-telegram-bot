"""
media.py — Downloads videos, GIFs, and images from tweets.
Uses yt-dlp for best-quality video extraction.
"""

import asyncio
import logging
import os
import tempfile
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False
    log.warning("yt-dlp not installed — video download unavailable")


class MediaResult:
    """Holds downloaded media ready to send to Telegram."""
    __slots__ = ("type", "data", "mime", "caption", "thumb")

    def __init__(self, type_: str, data: bytes, mime: str, caption: str = "", thumb: bytes | None = None):
        self.type    = type_    # "photo" | "video" | "gif" | "card"
        self.data    = data
        self.mime    = mime
        self.caption = caption
        self.thumb   = thumb    # optional JPEG thumbnail for videos


async def download_bytes(url: str, client: httpx.AsyncClient) -> bytes:
    r = await client.get(url, timeout=60, follow_redirects=True)
    r.raise_for_status()
    return r.content


async def download_video_ytdlp(url: str) -> bytes | None:
    """Download best video+audio from a tweet URL using yt-dlp."""
    if not HAS_YTDLP:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        out_tmpl = os.path.join(tmp, "%(id)s.%(ext)s")
        ydl_opts = {
            "outtmpl":        out_tmpl,
            "format":         "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "quiet":          True,
            "no_warnings":    True,
            "merge_output_format": "mp4",
        }
        loop = asyncio.get_event_loop()
        try:
            def _dl():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])

            await loop.run_in_executor(None, _dl)

            files = list(Path(tmp).glob("*.mp4"))
            if not files:
                files = list(Path(tmp).iterdir())
            if files:
                return files[0].read_bytes()
        except Exception as e:
            log.error("yt-dlp failed for %s: %s", url, e)
    return None


async def get_tweet_media(tweet) -> list[MediaResult]:
    """
    Inspect a twscrape Tweet and return MediaResult objects:
      - Videos  → type="video"
      - GIFs    → type="gif"
      - Photos  → type="photo"  (one per photo in multi-image tweets)
    Returns empty list for text-only tweets.
    """
    results: list[MediaResult] = []
    if not tweet.media:
        return results

    caption = tweet.rawContent or tweet.content or ""
    tweet_url = f"https://twitter.com/{tweet.user.username}/status/{tweet.id}"

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0"},
        follow_redirects=True,
        timeout=60,
    ) as client:

        # ── Videos ──────────────────────────────────────────────────────────
        if tweet.media.videos:
            for vid in tweet.media.videos:
                log.info("Downloading video for tweet %s", tweet.id)
                data = await download_video_ytdlp(tweet_url)

                if not data and vid.variants:
                    # Fallback: pick highest-bitrate variant directly
                    best = max(
                        [v for v in vid.variants if v.contentType == "video/mp4"],
                        key=lambda v: v.bitrate or 0,
                        default=None,
                    )
                    if best:
                        try:
                            data = await download_bytes(best.url, client)
                        except Exception as e:
                            log.error("Direct video download failed: %s", e)

                if data:
                    results.append(MediaResult("video", data, "video/mp4", caption))
                    caption = ""   # only attach caption to first media item

        # ── GIFs (Twitter stores these as MP4 videos with gif flag) ─────────
        if tweet.media.animated:
            for gif in tweet.media.animated:
                log.info("Downloading GIF for tweet %s", tweet.id)
                best_url = gif.videoUrl

                if best_url:
                    try:
                        data = await download_bytes(best_url, client)
                        results.append(MediaResult("gif", data, "video/mp4", caption))
                        caption = ""
                    except Exception as e:
                        log.error("GIF download failed: %s", e)

        # ── Photos ───────────────────────────────────────────────────────────
        if tweet.media.photos:
            for i, photo in enumerate(tweet.media.photos):
                log.info("Downloading photo %d for tweet %s", i + 1, tweet.id)
                # Request highest quality
                url = photo.url
                if "?" not in url:
                    url += "?name=large"
                else:
                    url = url.replace("name=small", "name=large")
                try:
                    data = await download_bytes(url, client)
                    mime = "image/jpeg"
                    if url.lower().endswith(".png"):
                        mime = "image/png"
                    elif url.lower().endswith(".webp"):
                        mime = "image/webp"
                    results.append(MediaResult(
                        "photo", data, mime,
                        caption if i == 0 else ""   # caption on first photo only
                    ))
                except Exception as e:
                    log.error("Photo download failed: %s", e)

    return results
