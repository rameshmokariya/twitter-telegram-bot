"""
telegram.py — Sends tweet cards and raw media to a Telegram channel.
"""

import logging
import io
from typing import Literal

import httpx

log = logging.getLogger(__name__)

TG_BASE = "https://api.telegram.org/bot{token}/{method}"


async def _tg_post(
    token: str,
    method: str,
    data: dict,
    files: dict | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict | None:
    url = TG_BASE.format(token=token, method=method)
    _close = False
    if client is None:
        client = httpx.AsyncClient()
        _close = True
    try:
        if files:
            resp = await client.post(url, data=data, files=files, timeout=120)
        else:
            resp = await client.post(url, json=data, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        log.error("Telegram %s failed [%s]: %s", method, e.response.status_code, e.response.text[:300])
        return None
    except Exception as e:
        log.error("Telegram %s error: %s", method, e)
        return None
    finally:
        if _close:
            await client.aclose()


async def send_card_photo(
    token: str,
    channel: str,
    png_bytes: bytes,
    caption: str = "",
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Send a tweet card screenshot as a photo."""
    result = await _tg_post(
        token, "sendPhoto",
        data={"chat_id": channel, "caption": caption, "parse_mode": "HTML"},
        files={"photo": ("card.png", png_bytes, "image/png")},
        client=client,
    )
    return result is not None and result.get("ok")


async def send_video(
    token: str,
    channel: str,
    video_bytes: bytes,
    caption: str = "",
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Send a video file."""
    data = {
        "chat_id":    channel,
        "caption":    caption[:1024],
        "parse_mode": "HTML",
        "supports_streaming": "true",
    }
    result = await _tg_post(
        token, "sendVideo",
        data=data,
        files={"video": ("video.mp4", video_bytes, "video/mp4")},
        client=client,
    )
    return result is not None and result.get("ok")


async def send_animation(
    token: str,
    channel: str,
    gif_bytes: bytes,
    caption: str = "",
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Send a GIF/animated MP4 using sendAnimation."""
    data = {
        "chat_id":    channel,
        "caption":    caption[:1024],
        "parse_mode": "HTML",
    }
    result = await _tg_post(
        token, "sendAnimation",
        data=data,
        files={"animation": ("anim.mp4", gif_bytes, "video/mp4")},
        client=client,
    )
    return result is not None and result.get("ok")


async def send_photo_file(
    token: str,
    channel: str,
    photo_bytes: bytes,
    mime: str = "image/jpeg",
    caption: str = "",
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Send a raw photo file."""
    ext = "jpg" if "jpeg" in mime else mime.split("/")[-1]
    result = await _tg_post(
        token, "sendPhoto",
        data={"chat_id": channel, "caption": caption[:1024], "parse_mode": "HTML"},
        files={"photo": (f"photo.{ext}", photo_bytes, mime)},
        client=client,
    )
    return result is not None and result.get("ok")


async def send_media_group(
    token: str,
    channel: str,
    photos: list[tuple[bytes, str]],   # list of (bytes, mime)
    caption: str = "",
    client: httpx.AsyncClient | None = None,
) -> bool:
    """
    Send up to 10 photos as a media group (album).
    caption is attached to the first photo.
    """
    if not photos:
        return False
    if len(photos) == 1:
        return await send_photo_file(token, channel, photos[0][0], photos[0][1], caption, client)

    media_json = []
    files = {}
    for i, (data, mime) in enumerate(photos[:10]):
        key = f"photo{i}"
        ext = "jpg" if "jpeg" in mime else mime.split("/")[-1]
        files[key] = (f"photo{i}.{ext}", data, mime)
        entry = {"type": "photo", "media": f"attach://{key}"}
        if i == 0 and caption:
            entry["caption"]    = caption[:1024]
            entry["parse_mode"] = "HTML"
        media_json.append(entry)

    import json
    result = await _tg_post(
        token, "sendMediaGroup",
        data={"chat_id": channel, "media": json.dumps(media_json)},
        files=files,
        client=client,
    )
    return result is not None and result.get("ok")
