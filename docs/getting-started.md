# Getting started

From a fresh clone to your first scan, then a full paper trade. Every step
works without a broker; the broker comes last, and only on a paper account.

Times below are New York time (ET). The NYSE trades 9:30 to 16:00 ET, which
is 15:30 to 22:00 in Germany, or 14:30 to 21:00 in the weeks when only one
side of the Atlantic has switched to or from summer time (late March, and
late October to early November).

## 1. Requirements

- Python 3.8 or newer (`python3 --version`). Nothing to pip install.
- `pdftotext` (poppler) only for the Pelosi tracker's PDFs: `brew install poppler`.
- Optional: Claude Code, to use the skills by talking to Claude (step 7).

Check that everything runs on your machine:

```bash
cd day-trader-skill
python3 -m unittest discover -s tests
```

All tests run offline, on saved real data. They never read your own
`trader/` folder or your keys.

## 2. Your settings: `trader/config.json`

Everything the tools write lands in `trader/` in the project. That folder is
in `.gitignore`: it holds your portfolio, your trades and your keys, so it
never goes into git.

```bash
mkdir -p trader
cp trader-budget/config.example.json trader/config.json
```

Open `trader/config.json` and set at least:

| key | what to put there |
| --- | --- |
| `sec_user_agent` | your name and email, `"Jane Doe jane@example.com"`. The SEC requires it; without it the 8-K filings and the analyst are skipped |
| `budget` | the account size the position sizing works with |
| `max_loss_per_trade_usd`, `max_daily_loss_usd` | what one trade and one day may cost you at most |
| `paper` | leave it `true` |

The rest has sensible defaults; each skill's `SKILL.md` explains its keys.

## 3. Optional: Finnhub for more news

Yahoo's headlines and the SEC run without a key. Finnhub adds headlines per
ticker from many outlets.

1. Make a free account at finnhub.io and copy the API key.
2. Put it in your shell profile (`~/.zshrc` on a Mac), with an editor so the
   key never lands in your shell history:

   ```bash
   export FINNHUB_API_KEY="your-key"
   ```

3. `source ~/.zshrc`, or open a new terminal.

Claude Desktop started from the Dock does not read `~/.zshrc`. If you use the
skills there, put `"finnhub_api_key": "your-key"` in `trader/config.json`
instead. The environment variable wins when both are set.

Your own RSS feeds go in `news_feeds` in the config. The example has
MarketWatch and Nasdaq. A feed that keeps timing out costs up to 30 seconds
per check: take it out.

## 4. First run, no market needed

These work any time, also on a weekend.

```bash
python3 trader-scanner/scan.py universe                   # once a week: ~5,900 US stocks -> trader/universe.txt
python3 trader-scanner/scan.py daily                      # setups on the last closes, 2-3 minutes
python3 trader-news/news.py --tickers TSLA NVDA --hours 72
```

Quick test without the whole universe:

```bash
python3 trader-scanner/scan.py daily --symbols TSLA NVDA AAPL AMD
```

A replay of a past trade, to see what the Time Machine says:

```bash
python3 trader-timemachine/timemachine.py run --symbol TSLA --date 2026-10-19 \
    --entry 364.18 --stop 358.32 --target 370.04 --entry-time "10:15 AM"
```

## 5. Does it make money? The backtest

Before any trading day, test the rule itself.

### More data: Alpaca keys (free)

Yahoo keeps only 60 days of 5-minute bars: one market mood. With a free
Alpaca paper account the backtest uses years of consolidated bars.

1. Sign up at alpaca.markets, open the **paper** dashboard and create API keys.
2. Add them to `~/.zshrc` with an editor (the same names the Alpaca MCP
   server uses in step 9):

   ```bash
   export ALPACA_API_KEY="your-paper-key"
   export ALPACA_SECRET_KEY="your-paper-secret"
   ```

3. `source ~/.zshrc`. The backtest now says "Alpaca SIP 5-minute bars".

### Run it

```bash
python3 trader-timemachine/backtest.py            # 75 liquid US stocks, 2 years (or 60 days), your limits
python3 trader-timemachine/backtest.py --grid     # how fragile: other stops and targets
```

It enters at the next bar's open plus slippage, checks the stop before the
target, and applies your limits from the config. The verdict uses the mean
result per trade (in R, 1R = the planned risk) and its 95 % interval:

- **an edge in this sample**: the whole interval is above zero
- **loses money in this sample**: the whole interval is below zero
- **no evidence of an edge**: zero is inside; it could be luck either way

### Looking for a filter, honestly

```bash
python3 trader-timemachine/backtest.py --split --explore              # first half of the days only
python3 trader-timemachine/backtest.py --split --min-rvol 1.5 --market  # judged on the second half
```

`--explore` groups the signals by relative volume, gap and market direction,
but only on the first half of the days. Choose at most one or two filters
there, for a reason. Then `--split` with those filters gives the verdict on
the second half, which you have not looked at. Only that verdict counts. Each
further try on the same second half makes it a little less trustworthy.

Even a good result is history. The next step after it is paper trading
(step 9), never real money.

## 6. A trading day, step by step

The order matters: each step reads what the one before wrote in `trader/`.
Nothing here places an order. Steps 6.9 and 6.10 need the broker (step 9).

| when (ET) | step | command |
| --- | --- | --- |
| before 9:30 | 6.1 the day's candidates | `scan.py daily` (writes `trader/watchlist.json`) |
| before 9:30 | 6.2 news on them | `news.py` (reads the watchlist) |
| from 9:45 | 6.3 opening-range breakouts | `scan.py orb --at 10:15`, or without `--at` for now |
| from 9:45 | 6.4 the entry, bar by bar | `entry.py watch TSLA --until 15:30` (writes `trader/entry.json` on a trigger) |
| on a trigger | 6.5 stop and target first | `exit.py plan --stop or-mid --target 1R --write` (`trader/plan.json`) |
| on a trigger | 6.6 the honest odds | `timemachine.py run --write` |
| on a trigger | 6.7 the size | `size.py --write` (`trader/orders.json`) |
| on a trigger | 6.8 your limits | `check.py` (PASS or BLOCK per order) |
| on a PASS | 6.9 the ticket | `ticket.py show`, then your yes (in Claude) |
| while open | 6.10 alerts | `alerts.py add --from-plan`, then `alerts.py check` |
| by 15:55 | 6.11 flat | `ticket.py flatten --symbol TSLA --qty 8`, then 6.8 and 6.9 again |

All scripts live in their skill folder (`trader-scanner/scan.py` and so on).
What the statuses mean:

- **WAITING / TRIGGERED / EXPIRED**: the entry timer's answer for the setup.
- **STALE DATA**: Yahoo's last trade is more than 5 minutes old during the
  session. No signal on old data: check the price at your broker.
- **BLOCK**: trader-risk stopped the order and says which limit.
- **Not recorded**: the yes was not explicit, or the broker's live price is
  more than 1 % from the plan (or at the stop). Plan again.

## 7. Claude does the steps for you

With the skills installed (step 8) you talk instead of typing commands:
"scan for breakouts", "any news on TSLA?", "watch TSLA for an entry", "plan
the exit", "what are the odds?", "size it", "check it", "place it". Claude
runs the same scripts and shows you the results.

## 8. Install the skills in Claude Code

As a plugin, from your local clone (works for a private repo too):

```bash
cd day-trader-skill
claude
```

```
/plugin marketplace add ./
/plugin install day-trader-skill@day-trader-skill
```

The skills then show up as `/day-trader-skill:trader-scanner` and so on, and
the order gate (`hooks/hooks.json`) is on by itself.

Or copy the folders to use plain `/trader-scanner`:

```bash
cp -r trader-* ~/.claude/skills/
```

Then add the `hooks` block from
`trader-broker/claude_code_settings.example.json` to `.claude/settings.json`,
or the gate is off.

## 9. The broker, on paper

Only after you have watched steps 4 to 7 work for a few days.

1. Make an Alpaca account and create **paper** API keys (separate from live keys).
2. Follow "Setup A" in `trader-broker/SKILL.md`: the MCP server pinned to
   `alpaca-mcp-server@2.4.0`, keys in the config file, never in the chat.
3. Set `place_stock_order` and every tool that changes orders to
   **Needs approval**, never Always allow.
4. Ask Claude for the account info and check in the Alpaca dashboard that it
   is the paper account.

Before every buy the gate checks: trader-risk passed this exact order today,
you said yes to it in the last 15 minutes, the broker's live price is within
1 % of the plan, and this yes has not been used yet. Then Claude's own Allow
once / Deny card asks you again.

## 10. When something goes wrong

| you see | it means | do |
| --- | --- | --- |
| `no trader/universe.txt yet` | the scanner has no stock list | `scan.py universe` |
| `SEC filings skipped` | no `sec_user_agent` | set it in `trader/config.json` |
| `note: feed X: ... broke off the answer` | a feed hung up or timed out | ignore once; remove it if it keeps happening |
| `Finnhub: Invalid API key` | wrong or missing key | check `FINNHUB_API_KEY` |
| `HTTP 429` | Yahoo is rate-limiting you | wait a minute; scan fewer symbols |
| `STALE DATA` | Yahoo lags or the stock barely trades | no trade on that signal |
| `the NYSE is closed today` | weekend or holiday | nothing to do |
| `Not recorded: ... live price` | the price moved away from the plan | plan again from 6.5 |
| `trader-broker gate: order blocked` | a check above failed | read the reason; start again at `ticket.py show` |
| backtest: `too few trades` | not enough signals to judge | test more stocks or the default list |

Not financial advice. Most people who day trade lose money. Paper first.
