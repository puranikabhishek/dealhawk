# DealHawk 🦅

Free deal bot for Telegram. Runs every 30 minutes on GitHub Actions, triggered by a free cron-job.org timer (token expires 2027-11-02). **$0, no AI tokens.**

## What you get
- **One short digest per run**: price, % off, product name (tap to buy). Max 4 per section.
- **Sections:** 🔥 Ultra Deals (65%+ off, clearance, price mistakes) · 🔌 Electronics · 🧺 Home Appliances ·
  🎯 Wish List · 🪴 Room Revamp (budget office makeover: frames, LED strips, puck lights, planter, cable tidy; own $8 price floor) · 🖼️ Home & Room Decor · 🌿 Patio · 🛋️ Furniture · 🏷️ Coupons & Promo Codes · 🆓 Freebies · 🎁 Giveaways
- **🚨 Price mistakes** get their own instant message with a Buy button, even overnight (silently). Only ones posted in the last 12h.
- **🚨 STEALS** — a wish-list/watchlist item at a crazy price (at or under its `steal:` price in the watchlist, or 50%+ off).
  Sent **first** in the run, its own loud card with a big **BUY NOW** button, **pinned** to the top of the chat, rings even in
  quiet hours, no waiting for votes. The pin comes off and the card flips to ❌ EXPIRED when the deal dies.
  Optional: set a `TELEGRAM_STEAL_CHAT_ID` secret (a private channel with the bot as admin) to get steals in their own chat
  with a custom ringtone.
- **Radar mode** (`--radar`, or the "radar" box on Run workflow): a fast steal-only pass over the radar + Slickdeals front-page
  feeds — no digest, no sweep. Point a second cron-job.org timer at it to check for steals between full runs.
- **🎯 Wish List + watchlist deals** each get their own **photo card**: price, list price, % off, store, how long ago it
  was posted (🟢 under 1h · 🟡 under 4h · 🟠 older), and whether it's verified live. Buttons: **🛒 Buy** (straight to the store),
  **🔎 Search** (store search for the product, a link that can't expire).
- **Cards update themselves**: every run re-checks cards sent in the last 36h. When a deal dies the card turns into
  **❌ EXPIRED** and loses its Buy button, so you never tap a dead link.
- **Sources:** Slickdeals, DealNews, TechBargains, 9to5Toys, Hip2Save, Reddit r/deals. Same deal from two
  sites shows once. Newer boards are capped at 24h old; 9to5Toys/Hip2Save posts are dropped when marked expired.
- **Buy = straight to the store.** DealHawk digs the real store link out of each post (unwrapping affiliate/short links).
  No direct store link → the deal isn't sent. Never a blog post or forum thread.
- **Real expiry checks**: Slickdeals and DealNews pages are read on every run; anything marked expired is dropped.
  DealNews links are followed to the store page for a stock check where the store allows it.
- **Wish-list radar**: Slickdeals searches for your wish-list items catch new posts within minutes. A fresh wish-list
  post needs only 2 votes instead of 5.
- Sold-out/expired deals are dropped and re-checked after 12h.

## Control it from Telegram
| Command | Does |
|---|---|
| `/add grill, dyson` | Want these (🎯 Your picks) |
| `/remove grill` | Stop wanting |
| `/block cushion` | Never show deals with this word |
| `/unblock cushion` | Undo |
| `/watch dyson v15 400` | Alert if under $400 |
| `/unwatch dyson` | Stop watching |
| `/off tools` · `/on clothing` | Toggle a category |
| `/list` | Show everything |

Commands apply on the next run (within 30 min).

## Sources
Slickdeals Frontpage, Popular, Freebies and Sweepstakes; DealNews main, Staff Picks, Electronics, Home & Garden; r/freebies. Edit `feeds` in `config.yaml` to change.

## Settings
`config.yaml`: categories, blocked words/stores, min price, quiet hours, `stock_policy` (`balanced` or `strict` = ✅ only),
`wishlist_alerts` (cards per run, vote bar, tracking window), `digest_silent` (`true` = only cards and price mistakes buzz).

## Limits
Big stores (Amazon, Costco, Walmart, Target) often block bots, so "✅ Still live" usually means the deal site says it's active, not that the store was checked. Checks happen once per run, so a deal can still die between runs.

## Manual run
Actions → DealHawk → Run workflow. Tick "Re-check" to retest deals held as sold-out/unverified.
Type `yes` in "demo" to get a sample wish-list card. Tick "Dry run" to log what would be sent without sending.
