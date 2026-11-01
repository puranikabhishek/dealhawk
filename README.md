# DealHawk 🦅

Free deal bot for Telegram. Runs hourly on GitHub Actions (every 30 min Thanksgiving → Dec 2). **$0, no AI tokens.**

## What you get
- **One short digest per run**: price, % off, product name (tap to buy). Max 4 per section.
- **Sections:** 🔥 Ultra Deals (65%+ off, clearance, price mistakes) · 🔌 Electronics · 🧺 Home Appliances ·
  🖼️ Home & Room Decor · 🌿 Patio · 🛋️ Furniture · 🏷️ Coupons & Promo Codes · 🆓 Freebies · 🎁 Giveaways
- **🎯 Watchlist hits** get their own message with photo + Buy button.
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

Commands apply on the next run (within the hour).

## Sources
Slickdeals Frontpage, Popular, Freebies and Sweepstakes; DealNews main, Staff Picks, Electronics, Home & Garden; r/freebies. Edit `feeds` in `config.yaml` to change.

## Settings
`config.yaml`: categories, blocked words/stores, min price, quiet hours, `stock_policy` (`balanced` or `strict` = ✅ only).

## Limits
Big stores (Amazon, Costco, Walmart, Target) block bots, and Slickdeals hides store links, so many deals show ⚠️. DealHawk drops anything flagged expired or confirmed sold out, but always glance at stock before buying.

## Manual run
Actions → DealHawk → Run workflow. Tick "Re-check" to retest deals held as sold-out/unverified.
