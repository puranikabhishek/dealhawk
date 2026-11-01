# DealHawk 🦅

Free deal bot for Telegram. Runs hourly on GitHub Actions (every 30 min Thanksgiving → Dec 2). **$0, no AI tokens.**

## What you get
- **One digest per run**, grouped by category. Tap a product name to buy.
- **🔥 Hot deals** (≥60% off, 100+ votes, or a watchlist hit) get their own message with photo + Buy button.
- **Flags:** ✅ store confirmed in stock · ⚠️ store blocks bots, check stock · 📉 lowest price DealHawk has seen.
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
Slickdeals Frontpage + Popular (community-voted) and DealNews (editor-checked). Edit `feeds` in `config.yaml` to change.

## Settings
`config.yaml`: categories, blocked words/stores, min price, quiet hours, `stock_policy` (`balanced` or `strict` = ✅ only).

## Limits
Big stores (Amazon, Costco, Walmart, Target) block bots, and Slickdeals hides store links, so many deals show ⚠️. DealHawk drops anything flagged expired or confirmed sold out, but always glance at stock before buying.

## Manual run
Actions → DealHawk → Run workflow. Tick "Re-check" to retest deals held as sold-out/unverified.
