# Security

What this pack connects to, what it can and cannot do, and where the limits
of its safety checks are. Read this before you connect a brokerage account.

## Network: every host the tools talk to

All tools are standard-library Python. They make plain HTTPS GET requests
and never upload anything except the request itself (URL and headers). None
of them talks to a server run by the author of this pack.

| host | used by | what is fetched |
| --- | --- | --- |
| query1/query2.finance.yahoo.com | scanner, entry, exit, timemachine, alerts, news | charts and quotes (unofficial endpoint) |
| feeds.finance.yahoo.com | news | headline RSS per ticker |
| finnhub.io | news (only with `FINNHUB_API_KEY`) | company news per ticker; the key is sent in a header |
| data.alpaca.markets | backtest (only with `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`) | historical 5-minute bars; the keys are sent in headers, read-only market data, no trading |
| the hosts in `news_feeds` | news (only if you list them) | the RSS/Atom feeds you chose, http(s) only, at most 20 |
| stooq.com | alerts | fallback daily quotes |
| www.nasdaqtrader.com | scanner | listed-symbol directories |
| www.sec.gov, data.sec.gov | news, analyst | 8-K feeds, company facts, ticker map |
| disclosures-clerk.house.gov | pelosi | House disclosure index (ZIP) and PTR PDFs |

What these hosts learn: your IP address, the tickers you look at and when.
Finnhub also learns your account (the key). X/Twitter is deliberately not a
source: paid per post, and the usual home of pump-and-dump posts.
The SEC requires a User-Agent with your name and an email address
(`sec_user_agent`); that goes to the SEC with every SEC request.

The broker is not called by any tool here. Claude calls it through a
separate MCP server (Alpaca, runs on your machine) or connector (Interactive
Brokers). Their security is theirs.

## Orders: what stands between Claude and the market

| check | where it runs | can the model skip it? |
| --- | --- | --- |
| trader-risk PASS for this exact order | `check.py`, Claude runs it | yes, it is an instruction |
| your explicit yes, recorded by `ticket.py approve` | Claude runs it | yes, it is an instruction |
| `gate.py` PreToolUse hook (Claude Code) | Claude Code, outside the model | **no** |
| Allow once / Deny card (tool set to Needs approval) | Claude app, outside the model | **no** |
| paper account (`ALPACA_PAPER_TRADE=true`) | the Alpaca server | **no** |

The first two are rules in SKILL.md. A model can misread them, and text from
outside (a headline, a filing) can try to talk it out of them. The last
three hold regardless of what the conversation says. So:

- In Claude Code, keep the gate on (plugin installs get it from
  `hooks/hooks.json`; copied skills need the block from
  `trader-broker/claude_code_settings.example.json`).
- Everywhere, set `place_stock_order` and every tool that changes orders or
  positions to **Needs approval**, never Always allow. In Claude Desktop the
  card is the only check outside the model, because Desktop has no hooks.
- Stay on paper keys until you have watched the whole flow work.

What the gate checks before every `place_stock_order`: an unused yes from
today, recorded at most `gate_max_age_min` minutes ago (default 15), for
exactly these arguments, a trader-risk PASS for that order today, and for a
buy a live price from the broker within `max_price_drift_pct` (default 1 %)
of the planned entry. The live price is passed in by Claude from the
broker's read-only quote tool: it catches stale or wrong chart data, not a
model that makes a number up. The gate marks the yes as used, so one yes is
one call. It always blocks `close_all_positions`, `cancel_all_orders`, option
and crypto orders and `update_account_config` unless you list one in
`gate_allow`. It blocks when it cannot read its files.

Stale data: trader-entry gives no signal during the session when Yahoo's last
trade is older than `max_data_age_min` (default 5 minutes).

What the gate cannot see: what the order tool does after the call, other
brokers' tools under other names (the matcher is `mcp__.*alpaca.*`), and
anything done outside Claude Code.

## Third-party text

Headlines, SEC filings and disclosure PDFs are written by others and are
shown to Claude. The tools strip markup, control characters and invisible
Unicode (zero-width, bidi overrides) and cap the length, and the skills tell
Claude to treat that text as data, never as instructions. That lowers the
risk; it does not remove it. Do not run trader-news or trader-pelosi between
your yes and the order.

## Keys and personal data

- Broker keys go in the Claude Desktop config or environment variables,
  never in the chat. If you pasted one, delete the message and rotate it.
- Pin the broker server (`alpaca-mcp-server@2.4.0` in the example) so a new
  release does not run with your keys before you have looked at it.
- `trader/` in your project holds your portfolio, fills, decisions and your
  SEC user agent. Add `trader/` to your project's `.gitignore` so it never
  ends up in a public repository.

## Reporting a problem

Open an issue on the repository. For anything that could let an order
through without the checks above, say so in the title and leave out a
working exploit; it will be handled first.
