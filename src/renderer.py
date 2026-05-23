"""
renderer.py — Generates HD tweet card screenshots using Playwright.
Falls back to a Pillow-based card if Playwright is unavailable (shared hosting).
"""

import asyncio
import base64
import io
import re
import textwrap
from pathlib import Path

import httpx

# ── Optional imports ──────────────────────────────────────────────────────────
try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


# ─────────────────────────────────────────────────────────────────────────────
# HTML tweet card template (renders beautifully at 2× scale)
# ─────────────────────────────────────────────────────────────────────────────
def _build_html(tweet_data: dict) -> str:
    """
    tweet_data keys:
        display_name, username, verified, avatar_b64, avatar_mime,
        text, date_str, likes, retweets, replies,
        photo_b64 (optional), photo_mime (optional),
        theme ("light" | "dark")
    """
    theme = tweet_data.get("theme", "light")

    if theme == "dark":
        bg_color      = "#15202B"
        card_bg       = "#192734"
        text_color    = "#FFFFFF"
        sub_color      = "#8899A6"
        border_color  = "#38444D"
        stat_color    = "#8899A6"
    else:
        bg_color      = "#F0F2F5"
        card_bg       = "#FFFFFF"
        text_color    = "#0F1419"
        sub_color      = "#536471"
        border_color  = "#EFF3F4"
        stat_color    = "#536471"

    verified_badge = ""
    if tweet_data.get("verified"):
        verified_badge = """
        <svg viewBox="0 0 24 24" class="verified" aria-label="Verified account">
          <g><path d="M22.25 12c0-1.43-.88-2.67-2.19-3.34.46-1.39.2-2.9-.81-3.91s-2.52-1.27-3.91-.81c-.66-1.31-1.9-2.19-3.34-2.19s-2.67.88-3.33 2.19c-1.4-.46-2.91-.2-3.92.81s-1.26 2.52-.8 3.91c-1.31.67-2.2 1.91-2.2 3.34s.89 2.67 2.2 3.34c-.46 1.39-.21 2.9.8 3.91s2.52 1.26 3.91.81c.67 1.31 1.9 2.19 3.34 2.19s2.68-.88 3.34-2.19c1.39.45 2.9.2 3.91-.81s1.27-2.52.81-3.91c1.31-.67 2.19-1.91 2.19-3.34zm-11.71 4.2L6.8 12.46l1.41-1.42 2.26 2.26 4.8-5.23 1.47 1.36-6.2 6.77z"/></g>
        </svg>"""

    avatar_src = ""
    if tweet_data.get("avatar_b64"):
        mime = tweet_data.get("avatar_mime", "image/jpeg")
        avatar_src = f"data:{mime};base64,{tweet_data['avatar_b64']}"
    else:
        # Grey placeholder circle via SVG data URL
        avatar_src = "data:image/svg+xml;base64," + base64.b64encode(
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">'
            b'<circle cx="24" cy="24" r="24" fill="#ccc"/></svg>'
        ).decode()

    photo_html = ""
    if tweet_data.get("photo_b64"):
        mime = tweet_data.get("photo_mime", "image/jpeg")
        photo_html = f"""
        <div class="tweet-image-wrap">
          <img class="tweet-image" src="data:{mime};base64,{tweet_data['photo_b64']}" />
        </div>"""

    # Linkify URLs, hashtags, mentions in tweet text
    raw_text = tweet_data.get("text", "")
    escaped = (raw_text
               .replace("&", "&amp;")
               .replace("<", "&lt;")
               .replace(">", "&gt;"))

    # Highlight hashtags and mentions
    def highlight(m):
        t = m.group(0)
        if t.startswith("#"):
            return f'<span class="hashtag">{t}</span>'
        if t.startswith("@"):
            return f'<span class="mention">{t}</span>'
        if t.startswith("http"):
            short = t[:30] + "…" if len(t) > 33 else t
            return f'<span class="link">{short}</span>'
        return t

    text_html = re.sub(r"(#\w+|@\w+|https?://\S+)", highlight, escaped)
    text_html = text_html.replace("\n", "<br>")

    def fmt_num(n):
        if not n:
            return "0"
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n/1_000:.1f}K"
        return str(n)

    # Resolve Retweet Banner (if any)
    retweet_banner = ""
    if tweet_data.get("retweeted_by"):
        retweet_banner = f"""
        <div class="retweet-banner">
          <svg viewBox="0 0 24 24" class="retweet-icon"><path d="M4.5 3.88l4.432 4.14-1.364 1.46L5.5 7.55V16c0 1.1.896 2 2 2H13v2H7.5c-2.209 0-4-1.79-4-4V7.55L1.432 9.48.068 8.02 4.5 3.88zM16.5 6H11V4h5.5c2.209 0 4 1.79 4 4v8.45l2.068-1.93 1.364 1.46-4.432 4.14-4.432-4.14 1.364-1.46 2.068 1.93V8c0-1.1-.896-2-2-2z"/></svg>
          <span>{tweet_data['retweeted_by']} Retweeted</span>
        </div>"""

    # Resolve Thread HTML elements (if any parent exists)
    parents = tweet_data.get("parents", [])
    if parents:
        body_content = retweet_banner
        for idx, parent in enumerate(parents):
            if parent.get("avatar_b64"):
                mime = parent.get("avatar_mime", "image/jpeg")
                parent_avatar_src = f"data:{mime};base64,{parent['avatar_b64']}"
            else:
                parent_avatar_src = "data:image/svg+xml;base64," + base64.b64encode(
                    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">'
                    b'<circle cx="24" cy="24" r="24" fill="#ccc"/></svg>'
                ).decode()

            parent_photo_html = ""
            if parent.get("photo_b64"):
                mime = parent.get("photo_mime", "image/jpeg")
                parent_photo_html = f"""
                <div class="tweet-image-wrap" style="margin-top: 8px; margin-bottom: 8px;">
                  <img class="tweet-image" src="data:{mime};base64,{parent['photo_b64']}" />
                </div>"""

            parent_raw_text = parent.get("text", "")
            parent_escaped = (parent_raw_text
                              .replace("&", "&amp;")
                              .replace("<", "&lt;")
                              .replace(">", "&gt;"))
            parent_text_html = re.sub(r"(#\w+|@\w+|https?://\S+)", highlight, parent_escaped)
            parent_text_html = parent_text_html.replace("\n", "<br>")

            parent_verified_badge = ""
            if parent.get("verified"):
                parent_verified_badge = """
                <svg viewBox="0 0 24 24" class="verified" aria-label="Verified account">
                  <g><path d="M22.25 12c0-1.43-.88-2.67-2.19-3.34.46-1.39.2-2.9-.81-3.91s-2.52-1.27-3.91-.81c-.66-1.31-1.9-2.19-3.34-2.19s-2.67.88-3.33 2.19c-1.4-.46-2.91-.2-3.92.81s-1.26 2.52-.8 3.91c-1.31.67-2.2 1.91-2.2 3.34s.89 2.67 2.2 3.34c-.46 1.39-.21 2.9.8 3.91s2.52 1.26 3.91.81c.67 1.31 1.9 2.19 3.34 2.19s2.68-.88 3.34-2.19c1.39.45 2.9.2 3.91-.81s1.27-2.52.81-3.91c1.31-.67 2.19-1.91 2.19-3.34zm-11.71 4.2L6.8 12.46l1.41-1.42 2.26 2.26 4.8-5.23 1.47 1.36-6.2 6.77z"/></g>
                </svg>"""

            body_content += f"""
            <div class="thread-row">
              <div class="left-col">
                <img class="avatar" src="{parent_avatar_src}" />
                <div class="connecting-line"></div>
              </div>
              <div class="right-col">
                <div class="user-row-inline">
                  <span class="display-name">{parent['display_name']}{parent_verified_badge}</span>
                  <span class="username">@{parent['username']}</span>
                </div>
                <div class="parent-text">{parent_text_html}</div>
                {parent_photo_html}
                <div class="date parent-date" style="margin-top: 4px; margin-bottom: 12px; font-size: 13px; color: {sub_color};">{parent['date_str']}</div>
              </div>
            </div>
            """

        # Append target tweet to the bottom of the chain
        body_content += f"""
        <div class="thread-row" style="margin-bottom: 0;">
          <div class="left-col">
            <img class="avatar" src="{avatar_src}" />
          </div>
          <div class="right-col">
            <div class="user-row-inline">
              <span class="display-name">{tweet_data['display_name']}{verified_badge}</span>
              <span class="username">@{tweet_data['username']}</span>
            </div>
            <div class="tweet-text" style="font-size: 16px; margin-bottom: 12px;">{text_html}</div>
            {photo_html}
            <div class="date" style="margin-top: 4px; margin-bottom: 12px;">{tweet_data['date_str']}</div>
            <hr class="divider" style="margin: 8px 0;">
            <div class="stats">
              <div class="stat">
                <svg viewBox="0 0 24 24"><path d="M1.751 10c0-4.42 3.584-8 8.005-8h4.366c4.49 0 8.129 3.64 8.129 8.13 0 2.96-1.607 5.68-4.196 7.11l-8.054 4.46v-3.69h-.067c-4.49.1-8.183-3.51-8.183-8.01z"/></svg>
                {fmt_num(tweet_data.get('replies'))}
              </div>
              <div class="stat">
                <svg viewBox="0 0 24 24"><path d="M4.5 3.88l4.432 4.14-1.364 1.46L5.5 7.55V16c0 1.1.896 2 2 2H13v2H7.5c-2.209 0-4-1.79-4-4V7.55L1.432 9.48.068 8.02 4.5 3.88zM16.5 6H11V4h5.5c2.209 0 4 1.79 4 4v8.45l2.068-1.93 1.364 1.46-4.432 4.14-4.432-4.14 1.364-1.46 2.068 1.93V8c0-1.1-.896-2-2-2z"/></svg>
                {fmt_num(tweet_data.get('retweets'))}
              </div>
              <div class="stat">
                <svg viewBox="0 0 24 24"><path d="M16.697 5.5c-1.222-.06-2.679.51-3.89 2.16l-.805 1.09-.806-1.09C9.984 6.01 8.526 5.44 7.304 5.5c-1.243.07-2.349.78-2.91 1.91-.552 1.12-.633 2.78.479 4.82 1.074 1.97 3.257 4.27 7.129 6.61 3.87-2.34 6.052-4.64 7.126-6.61 1.111-2.04 1.03-3.7.477-4.82-.561-1.13-1.666-1.84-2.908-1.91zm4.187 7.69c-1.351 2.48-4.001 5.12-8.379 7.67l-.503.3-.504-.3c-4.379-2.55-7.029-5.19-8.382-7.67-1.36-2.5-1.41-4.86-.514-6.67.887-1.79 2.647-2.91 4.601-3.01 1.651-.09 3.368.56 4.798 2.01 1.429-1.45 3.146-2.1 4.796-2.01 1.954.1 3.714 1.22 4.601 3.01.896 1.81.846 4.17-.514 6.67z"/></svg>
                {fmt_num(tweet_data.get('likes'))}
              </div>
            </div>
          </div>
        </div>
        """
    else:
        body_content = f"""
        {retweet_banner}
        <div class="header">
          <div class="user-row">
            <img class="avatar" src="{avatar_src}" />
            <div class="user-info">
              <div class="display-name">
                {tweet_data['display_name']}
                {verified_badge}
              </div>
              <div class="username">@{tweet_data['username']}</div>
            </div>
          </div>
          <!-- X / Twitter logo -->
          <svg class="x-logo" viewBox="0 0 24 24"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-4.714-6.231-5.401 6.231H2.746l7.73-8.835L1.254 2.25H8.08l4.253 5.622L18.244 2.25zm-1.161 17.52h1.833L7.084 4.126H5.117L17.083 19.77z"/></svg>
        </div>

        <div class="tweet-text">{text_html}</div>

        {photo_html}

        <div class="date">{tweet_data['date_str']}</div>
        <hr class="divider">

        <div class="stats">
          <!-- Replies -->
          <div class="stat">
            <svg viewBox="0 0 24 24"><path d="M1.751 10c0-4.42 3.584-8 8.005-8h4.366c4.49 0 8.129 3.64 8.129 8.13 0 2.96-1.607 5.68-4.196 7.11l-8.054 4.46v-3.69h-.067c-4.49.1-8.183-3.51-8.183-8.01z"/></svg>
            {fmt_num(tweet_data.get('replies'))}
          </div>
          <!-- Retweets -->
          <div class="stat">
            <svg viewBox="0 0 24 24"><path d="M4.5 3.88l4.432 4.14-1.364 1.46L5.5 7.55V16c0 1.1.896 2 2 2H13v2H7.5c-2.209 0-4-1.79-4-4V7.55L1.432 9.48.068 8.02 4.5 3.88zM16.5 6H11V4h5.5c2.209 0 4 1.79 4 4v8.45l2.068-1.93 1.364 1.46-4.432 4.14-4.432-4.14 1.364-1.46 2.068 1.93V8c0-1.1-.896-2-2-2z"/></svg>
            {fmt_num(tweet_data.get('retweets'))}
          </div>
          <!-- Likes -->
          <div class="stat">
            <svg viewBox="0 0 24 24"><path d="M16.697 5.5c-1.222-.06-2.679.51-3.89 2.16l-.805 1.09-.806-1.09C9.984 6.01 8.526 5.44 7.304 5.5c-1.243.07-2.349.78-2.91 1.91-.552 1.12-.633 2.78.479 4.82 1.074 1.97 3.257 4.27 7.129 6.61 3.87-2.34 6.052-4.64 7.126-6.61 1.111-2.04 1.03-3.7.477-4.82-.561-1.13-1.666-1.84-2.908-1.91zm4.187 7.69c-1.351 2.48-4.001 5.12-8.379 7.67l-.503.3-.504-.3c-4.379-2.55-7.029-5.19-8.382-7.67-1.36-2.5-1.41-4.86-.514-6.67.887-1.79 2.647-2.91 4.601-3.01 1.651-.09 3.368.56 4.798 2.01 1.429-1.45 3.146-2.1 4.796-2.01 1.954.1 3.714 1.22 4.601 3.01.896 1.81.846 4.17-.514 6.67z"/></svg>
            {fmt_num(tweet_data.get('likes'))}
          </div>
        </div>
        """

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: {bg_color};
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 40px;
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }}
  .card {{
    background: {card_bg};
    border: 1px solid {border_color};
    border-radius: 16px;
    padding: 24px;
    width: 560px;
    box-shadow: 0 2px 12px rgba(0,0,0,.08);
  }}
  .retweet-banner {{
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    font-weight: 700;
    color: {sub_color};
    margin-bottom: 12px;
    margin-left: 36px;
  }}
  .retweet-icon {{
    width: 16px;
    height: 16px;
    fill: #00BA7C;
  }}
  .thread-row {{
    display: flex;
    gap: 12px;
    margin-bottom: 12px;
  }}
  .left-col {{
    display: flex;
    flex-direction: column;
    align-items: center;
    width: 48px;
    flex-shrink: 0;
  }}
  .connecting-line {{
    width: 2px;
    background: {border_color};
    flex-grow: 1;
    margin-top: 4px;
    margin-bottom: 4px;
  }}
  .right-col {{
    flex-grow: 1;
    min-width: 0;
  }}
  .user-row-inline {{
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 4px;
    flex-wrap: wrap;
  }}
  .parent-text {{
    font-size: 15px;
    line-height: 1.55;
    color: {text_color};
    margin-bottom: 8px;
    word-break: break-word;
  }}
  .header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
  }}
  .user-row {{
    display: flex;
    align-items: center;
    gap: 12px;
  }}
  .avatar {{
    width: 48px;
    height: 48px;
    border-radius: 50%;
    object-fit: cover;
    flex-shrink: 0;
  }}
  .user-info {{ display: flex; flex-direction: column; }}
  .display-name {{
    font-size: 15px;
    font-weight: 700;
    color: {text_color};
    display: flex;
    align-items: center;
    gap: 4px;
  }}
  .verified {{
    width: 18px; height: 18px;
    fill: #1D9BF0;
    flex-shrink: 0;
  }}
  .username {{
    font-size: 14px;
    color: {sub_color};
  }}
  .x-logo {{
    width: 28px;
    height: 28px;
    fill: {text_color};
    opacity: .85;
  }}
  .tweet-text {{
    font-size: 17px;
    line-height: 1.55;
    color: {text_color};
    margin-bottom: 16px;
    word-break: break-word;
  }}
  .hashtag  {{ color: #1D9BF0; }}
  .mention  {{ color: #1D9BF0; }}
  .link     {{ color: #1D9BF0; text-decoration: underline; }}
  .tweet-image-wrap {{
    border-radius: 12px;
    overflow: hidden;
    margin-bottom: 16px;
    border: 1px solid {border_color};
  }}
  .tweet-image {{
    width: 100%;
    display: block;
    max-height: 380px;
    object-fit: cover;
  }}
  .divider {{
    border: none;
    border-top: 1px solid {border_color};
    margin: 14px 0;
  }}
  .stats {{
    display: flex;
    gap: 24px;
  }}
  .stat {{
    display: flex;
    align-items: center;
    gap: 6px;
    color: {stat_color};
    font-size: 14px;
  }}
  .stat svg {{
    width: 18px; height: 18px;
    fill: {stat_color};
  }}
  .date {{
    font-size: 14px;
    color: {sub_color};
    margin-top: 14px;
  }}
</style>
</head>
<body>
<div class="card">
  {body_content}
</div>
</body>
</html>"""


async def _fetch_image_b64(url: str, client: httpx.AsyncClient) -> tuple[str, str]:
    """Download an image and return (base64_str, mime_type)."""
    try:
        headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "referer": "https://x.com/"
        }
        r = await client.get(url, headers=headers, timeout=15, follow_redirects=True)
        if r.status_code != 200:
            print(f"[renderer] Image fetch failed for {url} with status {r.status_code}: {r.text[:200]}")
        r.raise_for_status()
        mime = r.headers.get("content-type", "image/jpeg").split(";")[0]
        return base64.b64encode(r.content).decode(), mime
    except Exception as e:
        print(f"[renderer] Image fetch exception for {url}: {e}")
        return "", "image/jpeg"


async def screenshot_tweet_playwright(tweet_data: dict) -> bytes | None:
    """Render tweet card HTML with Playwright and return PNG bytes."""
    html = _build_html(tweet_data)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox"])
            page = await browser.new_page(device_scale_factor=2)   # 2× = HD
            await page.set_content(html, wait_until="networkidle")
            # Auto-fit screenshot to the card element
            card = await page.query_selector(".card")
            png = await card.screenshot(type="png")
            await browser.close()
            return png
    except Exception as e:
        print(f"[renderer] Playwright error: {e}")
        return None


def screenshot_tweet_pillow(tweet_data: dict) -> bytes | None:
    """Lightweight fallback using Pillow (no browser needed)."""
    if not HAS_PILLOW:
        return None
    try:
        W, PAD = 640, 32
        theme = tweet_data.get("theme", "light")
        bg    = (255, 255, 255) if theme == "light" else (25, 39, 52)
        fg    = (15,  20,  25)  if theme == "light" else (255, 255, 255)
        sub   = (83, 100, 113)  if theme == "light" else (136, 153, 166)

        # Wrap text
        lines = textwrap.wrap(tweet_data.get("text", ""), width=52)
        line_h = 28
        content_h = 80 + len(lines) * line_h + 60   # avatar + text + stats
        H = content_h + PAD * 2

        img  = Image.new("RGB", (W, H), bg)
        draw = ImageDraw.Draw(img)

        try:
            font_bold  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
            font_reg   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
            font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        except Exception:
            font_bold = font_reg = font_small = ImageFont.load_default()

        # Avatar circle placeholder
        draw.ellipse([PAD, PAD, PAD + 52, PAD + 52], fill=(200, 200, 200))

        # Name & username
        x_text = PAD + 64
        draw.text((x_text, PAD + 4),  tweet_data.get("display_name", ""), fill=fg,  font=font_bold)
        draw.text((x_text, PAD + 26), f"@{tweet_data.get('username','')}", fill=sub, font=font_small)

        # Tweet text
        y = PAD + 70
        for line in lines:
            draw.text((PAD, y), line, fill=fg, font=font_reg)
            y += line_h

        # Stats
        y += 12
        stats = (f"♡ {tweet_data.get('likes', 0)}   "
                 f"↻ {tweet_data.get('retweets', 0)}   "
                 f"↩ {tweet_data.get('replies', 0)}   "
                 f"· {tweet_data.get('date_str', '')}")
        draw.text((PAD, y), stats, fill=sub, font=font_small)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        print(f"[renderer] Pillow error: {e}")
        return None


async def render_tweet_card(tweet, theme: str = "light", parent_tweets=None) -> bytes | None:
    """
    Build tweet_data dict from a twscrape Tweet object,
    fetch avatar + first photo, then render to PNG bytes.
    """
    # Detect retweet and resolve to original tweet internally
    retweeted_by = None
    if tweet.retweetedTweet:
        retweeted_by = tweet.user.displayname or tweet.user.username
        tweet = tweet.retweetedTweet

    async with httpx.AsyncClient() as client:
        # Fetch avatar with normal resolution fallback
        avatar_b64, avatar_mime = "", "image/jpeg"
        if hasattr(tweet.user, "profileImageUrl") and tweet.user.profileImageUrl:
            url = tweet.user.profileImageUrl.replace("_normal", "_400x400")
            avatar_b64, avatar_mime = await _fetch_image_b64(url, client)
            if not avatar_b64:
                avatar_b64, avatar_mime = await _fetch_image_b64(tweet.user.profileImageUrl, client)

        # First photo (if any)
        photo_b64, photo_mime = "", "image/jpeg"
        if tweet.media and tweet.media.photos:
            photo_url = tweet.media.photos[0].url
            photo_b64, photo_mime = await _fetch_image_b64(photo_url, client)

        # Fetch parent details (if parent_tweets were passed)
        parents_data = []
        if parent_tweets:
            for p_tweet in parent_tweets:
                p_avatar_b64, p_avatar_mime = "", "image/jpeg"
                if hasattr(p_tweet.user, "profileImageUrl") and p_tweet.user.profileImageUrl:
                    p_url = p_tweet.user.profileImageUrl.replace("_normal", "_400x400")
                    p_avatar_b64, p_avatar_mime = await _fetch_image_b64(p_url, client)
                    if not p_avatar_b64:
                        p_avatar_b64, p_avatar_mime = await _fetch_image_b64(p_tweet.user.profileImageUrl, client)

                p_photo_b64, p_photo_mime = "", "image/jpeg"
                if p_tweet.media and p_tweet.media.photos:
                    p_photo_url = p_tweet.media.photos[0].url
                    p_photo_b64, p_photo_mime = await _fetch_image_b64(p_photo_url, client)

                from datetime import timezone, timedelta
                ist_tz = timezone(timedelta(hours=5, minutes=30))
                p_ist_date = p_tweet.date.astimezone(ist_tz)
                p_date_str = p_ist_date.strftime("%I:%M %p · %b %d, %Y (IST)")

                parents_data.append({
                    "display_name": p_tweet.user.displayname or p_tweet.user.username,
                    "username":     p_tweet.user.username,
                    "verified":     getattr(p_tweet.user, "verified", False) or getattr(p_tweet.user, "blue", False),
                    "avatar_b64":   p_avatar_b64,
                    "avatar_mime":  p_avatar_mime,
                    "text":         p_tweet.rawContent or p_tweet.content or "",
                    "date_str":     p_date_str,
                    "photo_b64":    p_photo_b64,
                    "photo_mime":   p_photo_mime,
                })

    # Convert main date to Indian Standard Time (IST: UTC + 5:30)
    from datetime import timezone, timedelta
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    ist_date = tweet.date.astimezone(ist_tz)
    date_str = ist_date.strftime("%I:%M %p · %b %d, %Y (IST)")

    tweet_data = {
        "display_name": tweet.user.displayname or tweet.user.username,
        "username":     tweet.user.username,
        "verified":     getattr(tweet.user, "verified", False) or getattr(tweet.user, "blue", False),
        "avatar_b64":   avatar_b64,
        "avatar_mime":  avatar_mime,
        "text":         tweet.rawContent or tweet.content or "",
        "date_str":     date_str,
        "likes":        tweet.likeCount or 0,
        "retweets":     tweet.retweetCount or 0,
        "replies":      tweet.replyCount or 0,
        "photo_b64":    photo_b64,
        "photo_mime":   photo_mime,
        "theme":        theme,
        "retweeted_by": retweeted_by,
        "parents":      parents_data,
    }

    if HAS_PLAYWRIGHT:
        return await screenshot_tweet_playwright(tweet_data)
    return screenshot_tweet_pillow(tweet_data)
