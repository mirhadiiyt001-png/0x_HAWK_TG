# 🚀 Zone SMS Monitor Bot

Premium Telegram bot that monitors SMS from the **0xhawk API**, automatically detects OTPs, and delivers beautifully formatted alerts with premium emoji and styled inline buttons.

## ✨ Features

- **🚨 Auto OTP Detection** — Extracts codes from SMS automatically
- **💎 Premium Emoji** — Loads Telegram sticker packs for rich rendering
- **🎨 Styled Buttons** — Colored inline buttons with premium emoji icons
- **🔒 Owner-Gated Access** — Only approved users can interact
- **📡 Live Number Monitoring** — Detects new SIM ranges automatically
- **🔄 Dual Deduplication** — Timestamp + body matching to prevent duplicates
- **🛡 Fail-Safe Delivery** — Multi-tier fallback ensures messages always arrive

## 📁 Project Structure

```
├── bot.py            # Main bot (all logic in one file)
├── config.py         # Configuration (env vars + constants)
├── .env              # Your credentials (gitignored)
├── .env.example      # Credential template
├── requirements.txt  # Python dependencies
├── Procfile          # Railway worker process
├── railway.toml      # Railway deployment config
└── .gitignore        # Git exclusions
```

## ⚡ Commands

| Command    | Description                           |
|------------|---------------------------------------|
| `/start`   | Welcome & activate access             |
| `/help`    | Command reference & guide             |
| `/stats`   | Live SMS & OTP statistics             |
| `/status`  | System health check                   |
| `/users`   | Manage users (owner only)             |
| `/testmsg` | Preview message formats (owner only)  |

## 🚀 Deploy to Railway

### 1. Push to GitHub
```bash
git init
git add .
git commit -m "Zone SMS Monitor Bot"
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git push -u origin main
```

### 2. Create Railway Project
1. Go to [railway.app](https://railway.app)
2. Click **"New Project"** → **"Deploy from GitHub repo"**
3. Select your repository

### 3. Set Environment Variables
In Railway dashboard → **Variables** tab, add:

| Variable              | Value                                      |
|-----------------------|--------------------------------------------|
| `TELEGRAM_BOT_TOKEN`  | Your bot token from @BotFather             |
| `OWNER_CHAT_ID`       | Your Telegram user ID                      |
| `UPSTREAM_API_URL`     | `https://0xhawk-api.up.railway.app`        |
| `FORWARD_SMS`          | `true`                                     |

### 4. Deploy
Railway will auto-detect the `Procfile` and deploy as a worker process.
The bot starts polling immediately — no webhook setup needed!

## 🖥 Run Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Configure .env (already set up)
# Edit .env if needed

# Run the bot
python bot.py
```

## 🔧 How It Works

1. **Bot starts** → loads emoji packs from Telegram sticker sets
2. **SMS Poller** (every 5s) → fetches from 0xhawk API → detects OTPs → sends formatted alerts
3. **Numbers Poller** (every 15s) → detects new phone ranges → sends notifications
4. **Commands & Callbacks** → user interaction with premium styled buttons
5. **Fail-safe delivery** → 4-tier fallback: styled → no-styles → no-icons → strip tg-emoji
