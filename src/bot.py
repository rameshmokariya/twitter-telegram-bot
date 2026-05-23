"""
bot.py — Main orchestrator.
For each unseen tweet:
  • Text-only        → render HD screenshot card → sendPhoto
  • Photo tweet      → render card (with embedded photo) + send full-res photo(s) as album
  • Video tweet      → sendVideo with tweet text as caption
  • GIF tweet        → sendAnimation with tweet text as caption
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx
from twscrape import API, gather

from renderer import render_tweet_card
from media import get_tweet_media
import telegram as tg

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def patch_twscrape():
    try:
        import inspect
        import os
        import twscrape.utils

        file_path = inspect.getfile(twscrape.utils)
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            old_str = 'if "profile_image_url_https" not in flat:'
            new_str = 'if not flat.get("profile_image_url_https"):'

            if old_str in content:
                log.info("Programmatically patching twscrape library avatar bug inside %s...", file_path)
                content = content.replace(old_str, new_str)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)
                log.info("✓ twscrape package patched successfully!")
    except Exception as e:
        log.warning("Could not programmatically patch twscrape: %s", e)

TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
TW_USERNAME         = os.environ["TW_USERNAME"]
TW_PASSWORD         = os.environ["TW_PASSWORD"]
TW_EMAIL            = os.environ.get("TW_EMAIL", "")
TW_EMAIL_PASSWORD   = os.environ.get("TW_EMAIL_PASSWORD", "")
MAX_FOLLOWING       = int(os.environ.get("MAX_FOLLOWING", "300"))
TWEETS_PER_ACCOUNT  = int(os.environ.get("TWEETS_PER_ACCOUNT", "5"))
CARD_THEME          = os.environ.get("CARD_THEME", "light")   # "light" or "dark"
INCLUDE_REPLIES     = os.environ.get("INCLUDE_REPLIES", "false").lower() == "true"
DB_PATH             = Path(os.environ.get("DB_PATH", "sent_tweets.db"))
TRACKED_ACCOUNTS    = [u.strip() for u in os.environ.get("TRACKED_ACCOUNTS", "darvasboxtrader").split(",") if u.strip()]



def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS sent_tweets (tweet_id TEXT PRIMARY KEY, sent_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS following_cache (user_id TEXT PRIMARY KEY, username TEXT NOT NULL, cached_at TEXT NOT NULL)")
    conn.commit()
    return conn

def is_sent(conn, tid):
    return conn.execute("SELECT 1 FROM sent_tweets WHERE tweet_id=?", (tid,)).fetchone() is not None

def mark_sent(conn, tid):
    conn.execute("INSERT OR IGNORE INTO sent_tweets (tweet_id, sent_at) VALUES (?,?)",
                 (tid, datetime.now(timezone.utc).isoformat()))
    conn.commit()

def get_cached_following(conn):
    rows = conn.execute("SELECT user_id, username, cached_at FROM following_cache LIMIT 1").fetchall()
    if not rows:
        return None
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(rows[0][2])).total_seconds() / 3600
    if age > 24:
        return None
    return [{"user_id": r[0], "username": r[1]}
            for r in conn.execute("SELECT user_id, username FROM following_cache").fetchall()]

def cache_following(conn, users):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM following_cache")
    conn.executemany("INSERT INTO following_cache VALUES (?,?,?)",
                     [(u["user_id"], u["username"], now) for u in users])
    conn.commit()


async def process_tweet(tweet, http: httpx.AsyncClient, api) -> bool:
    tok = TELEGRAM_BOT_TOKEN
    ch  = TELEGRAM_CHANNEL_ID
    caption   = tweet.rawContent or tweet.content or ""
    tweet_url = f"https://twitter.com/{tweet.user.username}/status/{tweet.id}"

    # Fetch parent thread chain recursively only if it is NOT a retweet!
    parent_tweets = []
    if not tweet.retweetedTweet:
        current_reply_to_id = tweet.inReplyToTweetId
        
        # If it's a quote-tweet, treat the quoted tweet as the first parent in the chain!
        if not current_reply_to_id and tweet.quotedTweet:
            parent_tweets.append(tweet.quotedTweet)
            current_reply_to_id = tweet.quotedTweet.inReplyToTweetId

        while current_reply_to_id and len(parent_tweets) < 4:
            log.info("Fetching parent tweet %s in the thread chain...", current_reply_to_id)
            try:
                p_tweet = await api.tweet_details(current_reply_to_id)
                if p_tweet:
                    parent_tweets.insert(0, p_tweet)  # older parents first
                    current_reply_to_id = p_tweet.inReplyToTweetId
                else:
                    break
            except Exception as pe:
                log.warning("Could not fetch parent tweet details: %s", pe)
                break

    media_items = await get_tweet_media(tweet)
    has_video = any(m.type == "video" for m in media_items)
    has_gif   = any(m.type == "gif"   for m in media_items)

    if has_video:
        for m in media_items:
            if m.type == "video":
                cap = f"{caption}\n\n🔗 {tweet_url}" if caption else tweet_url
                ok = await tg.send_video(tok, ch, m.data, cap[:1024], http)
                if not ok:
                    return False
        return True

    if has_gif:
        for m in media_items:
            if m.type == "gif":
                cap = f"{caption}\n\n🔗 {tweet_url}" if caption else tweet_url
                ok = await tg.send_animation(tok, ch, m.data, cap[:1024], http)
                if not ok:
                    return False
        return True

    # Standard tweets (text-only, replies, photo cards): render ONE screenshot card
    card_png = await render_tweet_card(tweet, theme=CARD_THEME, parent_tweets=parent_tweets)
    if card_png:
        return await tg.send_card_photo(tok, ch, card_png, f"🔗 {tweet_url}", http)

    # Plain text fallback
    result = await tg._tg_post(tok, "sendMessage", {
        "chat_id": ch, "text": f"{caption}\n\n🔗 {tweet_url}", "parse_mode": "HTML"
    })
    return result is not None


async def run_bot():
    patch_twscrape()
    
    # 1. Pre-seed accounts.db with cookies and reset locks BEFORE instantiating the API!
    import sqlite3 as _sq
    cookies = os.environ.get('TW_COOKIES', '{}')
    _conn = _sq.connect("accounts.db")
    
    # Ensure the twscrape accounts table is created with correct schema
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            username TEXT PRIMARY KEY,
            password TEXT,
            email TEXT,
            email_password TEXT,
            active INTEGER DEFAULT 1,
            cookies TEXT,
            locks TEXT DEFAULT '{}',
            last_used TEXT
        )
    """)
    
    # Insert or update the account with active=1, cookies, and reset locks!
    _conn.execute(
        "INSERT OR REPLACE INTO accounts (username, password, email, email_password, active, cookies, locks) VALUES (?, ?, ?, ?, 1, ?, '{}')",
        (TW_USERNAME, TW_PASSWORD, TW_EMAIL or None, TW_EMAIL_PASSWORD or None, cookies)
    )
    _conn.commit()
    _conn.close()
    log.info("Injected active cookies and reset locks for @%s — skipping login", TW_USERNAME)

    # 2. NOW instantiate API so it loads the active, cookie-authenticated account correctly!
    api  = API()
    conn = init_db()

    log.info("Loading tracked accounts: %s", TRACKED_ACCOUNTS)
    following = []
    for username in TRACKED_ACCOUNTS:
        row = conn.execute("SELECT user_id FROM following_cache WHERE username=?", (username,)).fetchone()
        if row:
            following.append({"user_id": row[0], "username": username})
        else:
            log.info("Resolving user ID for @%s …", username)
            try:
                user_info = await api.user_by_login(username)
                user_id = str(user_info.id)
                conn.execute(
                    "INSERT OR REPLACE INTO following_cache (user_id, username, cached_at) VALUES (?, ?, ?)",
                    (user_id, username, datetime.now(timezone.utc).isoformat())
                )
                conn.commit()
                following.append({"user_id": user_id, "username": username})
            except Exception as e:
                log.error("Failed to resolve user ID for @%s: %s", username, e)

    async with httpx.AsyncClient() as http:
        sent = 0
        for account in following:
            try:
                tweets = await gather(
                    api.user_tweets(int(account["user_id"]), limit=TWEETS_PER_ACCOUNT))
            except Exception as e:
                log.warning("Fetch failed for @%s: %s", account["username"], e)
                continue

            tweets.sort(key=lambda t: t.date)
            for tweet in tweets:
                # CRITICAL: Only process tweets that are authored or retweeted by the tracked account itself!
                if tweet.user.username.lower() != account["username"].lower():
                    continue

                tid = str(tweet.id)
                if is_sent(conn, tid):
                    continue
                # Only process tweets from the last 24 hours, but bypass date-filter for retweets
                is_retweet = tweet.retweetedTweet is not None
                age_hours = (datetime.now(timezone.utc) - tweet.date.astimezone(timezone.utc)).total_seconds() / 3600
                if not is_retweet and age_hours > 24:
                    continue
                if not INCLUDE_REPLIES and tweet.inReplyToTweetId:
                    mark_sent(conn, tid)
                    continue

                log.info("Processing tweet %s from @%s", tid, account["username"])
                ok = await process_tweet(tweet, http, api)
                mark_sent(conn, tid)
                if ok:
                    sent += 1
                    log.info("✓ Sent %s", tid)
                else:
                    log.warning("✗ Failed %s", tid)
                await asyncio.sleep(0.6)

    log.info("Done — sent %d new tweets.", sent)


if __name__ == "__main__":
    asyncio.run(run_bot())
