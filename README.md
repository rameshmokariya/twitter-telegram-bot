# 🐦→📢 Twitter-to-Telegram Bot

Forwards tweets from your Twitter following to a Telegram channel — **completely free**, no Twitter API key needed.

| Component | Tool | Cost |
|-----------|------|------|
| Tweet scraping | [twscrape](https://github.com/vladkens/twscrape) | Free (uses your account) |
| Telegram sending | Telegram Bot API | Free |
| Scheduling & hosting | GitHub Actions | Free (2000 min/month) |
| State tracking | SQLite (cached in GH Actions) | Free |

---

## ⚡ Setup (15 minutes)

### Step 1 — Create a Telegram Bot

1. Open Telegram, search for **@BotFather**
2. Send `/newbot` and follow the prompts
3. Copy the **Bot Token** (looks like `123456789:ABCdef...`)
4. Open your channel → **Admins** → add your bot with **Post Messages** permission
5. Note your channel ID: it's `@your_channel_name` or find the numeric ID with [@userinfobot](https://t.me/userinfobot)

### Step 2 — Fork this repo

Click **Fork** on GitHub so you have your own copy.

### Step 3 — Add GitHub Secrets

Go to your fork → **Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From BotFather |
| `TELEGRAM_CHANNEL_ID` | `@your_channel` or `-100xxxxxxxxx` |
| `TW_USERNAME` | Your Twitter username (no @) |
| `TW_PASSWORD` | Your Twitter password |
| `TW_EMAIL` | *(optional)* Twitter account email |
| `TW_EMAIL_PASSWORD` | *(optional)* Email password |

> **Tip:** Use a secondary Twitter account. twscrape logs in like a browser — the risk is low but a burner account is safer.

### Step 4 — Enable Actions

Go to **Actions** tab in your fork → click **Enable GitHub Actions** if prompted.

The workflow runs every **30 minutes** automatically. You can also trigger it manually via **Run workflow**.

---

## 🏃 Running locally

```bash
# Install deps
pip install -r requirements.txt

# Configure
cp .env.example .env
# edit .env with your values

# Run
export $(cat .env | xargs)
python src/bot.py
```

---

## ⚙️ Configuration

Edit these in `.github/workflows/bot.yml` (or your `.env` for local):

| Variable | Default | Description |
|---|---|---|
| `MAX_FOLLOWING` | `300` | Max accounts to process per run |
| `TWEETS_PER_ACCOUNT` | `5` | Tweets fetched per account |
| `DB_PATH` | `sent_tweets.db` | SQLite database path |

To change the schedule, edit the cron in `.github/workflows/bot.yml`:
```yaml
- cron: "*/30 * * * *"   # every 30 min
- cron: "*/15 * * * *"   # every 15 min
- cron: "0 * * * *"      # every hour
```

---

## 🔧 Customisation ideas

- **Filter retweets** — add `if tweet.retweetedTweet: continue` in bot.py
- **Filter by keyword** — check `tweet.content` for specific words
- **Multiple channels** — call `send_telegram()` with different channel IDs
- **Include replies** — remove the `inReplyToTweetId` check
- **Add media** — use `sendPhoto`/`sendVideo` Telegram endpoints for `tweet.media`

---

## 🚨 Troubleshooting

| Problem | Fix |
|---|---|
| `Login failed` | Check TW_USERNAME / TW_PASSWORD. Twitter may require email too. |
| `Telegram 403` | Bot is not an admin of the channel or wrong CHANNEL_ID |
| `Rate limited` | Increase `TWEETS_PER_ACCOUNT` less, or run less frequently |
| Duplicate tweets | The SQLite cache handles this; don't delete `sent_tweets.db` |

---

## 📁 Project structure

```
twitter-to-telegram/
├── src/
│   └── bot.py                   # Main bot logic
├── .github/
│   └── workflows/
│       └── bot.yml              # GitHub Actions scheduler
├── requirements.txt
├── .env.example
└── README.md
```
