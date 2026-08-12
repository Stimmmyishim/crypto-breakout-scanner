# Crypto Breakout Scanner — 24/7 cloud version

This project is designed to run continuously as a Render Background Worker and send live Telegram alerts to your phone. Render background workers are intended for continuously running processes. urlRender Background Workers documentationhttps://render.com/docs/background-workers

## What you need

- A GitHub account
- A Render account
- A Telegram account
- A Telegram bot token
- Your Telegram chat ID

Telegram bots can send messages through the official Bot API. urlTelegram Bot APIhttps://core.telegram.org/bots/api

## Deploy

1. Create a new GitHub repository.
2. Upload every file from this folder to the repository.
3. In Render, create a **Blueprint** from that GitHub repository.
4. Render will read `render.yaml` and create the background worker.
5. In the worker's Environment settings, enter:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
6. Deploy.

The worker will continuously poll the market and send Telegram alerts.

## Telegram setup

1. In Telegram, open BotFather.
2. Use `/newbot`.
3. Follow the prompts and copy the bot token.
4. Open your new bot and send it `/start`.
5. In a browser, open:
   `https://api.telegram.org/botYOUR_TOKEN/getUpdates`
6. Find `message.chat.id` and use that number as `TELEGRAM_CHAT_ID`.

**Never publish your bot token in GitHub or send it to anyone.**

## Important cloud-storage note

The scanner uses SQLite for local signal history. Cloud filesystems can be ephemeral, so for long-term historical data/backtesting the next upgrade should move observations to Postgres. Render documents that its default filesystem is ephemeral and offers Postgres, Key Value, or persistent disks for durable storage. urlRender deployment documentationhttps://render.com/docs/deploys

## Accuracy

The scanner is a signal detector, not a guarantee that a token will rise. The best next upgrade is a backtesting system that evaluates every alert against later price movement and then tunes thresholds using out-of-sample data.
