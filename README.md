# DealHawk 🦅

Free deal sniper. Scans vetted deal feeds every 2 hours (every 30 min Black Friday → Cyber Monday), filters to what you care about, checks the link is live, and pings your Telegram with a **Buy** button.

**Cost: $0.** GitHub Actions (free) + Telegram bot (free) + public RSS feeds (free). No API keys to pay for.

## How it decides
1. **Sources:** Slickdeals Frontpage (community-vetted), r/buildapcsales, r/frugalmalefashion.
2. **Filters** (all in `config.yaml`): category keywords, trusted stores only, ≥30% off, Slickdeals thumbs ≥15, junk words (refurb, used, open box…).
3. **Watchlist:** specific items + max price — always alerts on a hit.
4. **Link check:** follows the link to the store; drops expired / sold out / 404s.
5. **Price memory:** saves every price it sees to `data/prices.json` and tells you "📉 lowest seen" over time.
6. **Quiet hours:** no pings 11pm–7am Central.

## Setup (~10 min)

### 1. Make the Telegram bot (phone, 2 min)
1. In Telegram, open **@BotFather** → send `/newbot` → name it `DealHawk` → pick a username like `abhi_dealhawk_bot`.
2. Copy the **token** it gives you (looks like `123456:ABC-xyz…`).
3. Open your new bot and send it any message ("hi").
4. Get your chat id: open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser → find `"chat":{"id": 12345678` → that number is your chat id.

### 2. Put the code on GitHub
1. Create a new repo at github.com/new — name `dealhawk`. **Public** = unlimited free minutes; private = 2,000 free min/month (DealHawk uses ~400–600).
2. Upload all files from this folder (keep the `.github/workflows/` path).

### 3. Add your secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**
- `TELEGRAM_BOT_TOKEN` = token from step 1
- `TELEGRAM_CHAT_ID` = chat id from step 1

### 4. Turn it on
Repo → **Actions** tab → enable workflows → **DealHawk → Run workflow**. Deals land in Telegram within a minute. After that it runs on its own.

## Tuning
Edit `config.yaml` in GitHub's web editor (works from your phone):
- Too many pings → raise `min_discount_pct` / `min_thumbs`, lower `max_alerts_per_run`.
- Too few → lower them, add keywords.
- Want a specific item → add it under `watchlist` with a `max_price`.

## Local testing
```bash
pip install -r requirements.txt
python dealhawk.py --dry-run -v          # prints alerts, sends nothing
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python dealhawk.py --test
```

## Honest limits
- Amazon/Walmart/Costco block bots, so those links show "⚠️ check price" instead of ✅ — the deal is still community-vetted.
- "All-year low" = Slickdeals community vetting + DealHawk's own price memory (gets smarter the longer it runs). True multi-year history needs paid Keepa — skipped to stay $0. Free manual check for Amazon: paste the link into camelcamelcamel.com.
- GitHub's cron can run a few minutes late at busy times.
