# Backtest results: does the opening-range breakout make money?

Run on 2026-10-25 with `trader-timemachine/backtest.py`. **Short answer: no.**
The rule loses about 0.1R per trade after costs, over 60 days, six months and
two years alike. The best filter only brings it to break-even, and the one
filter that looked profitable failed on fresh data.

## What was tested

- **Rule:** 15-minute opening range; long on the first 5-minute close above
  the range high between 9:45 and 15:30 ET; stop at the range's midpoint
  (`or_mid`); target 1R; flat by 15:55.
- **Fills:** the next bar's open, 5 bps slippage per side (10 round trip),
  no commission (Alpaca). Stop before target when a bar touches both; a gap
  through the stop fills at the open.
- **Limits:** $50 risk per trade, at most $4,000 per order, 3 trades a day,
  no new trade after $150 lost in a day.
- **Stocks:** 75 liquid US names (`DEFAULT_SYMBOLS`): today's big caps, which
  if anything flatters a long rule.
- **Verdict:** the mean R per trade and its 95 % interval. Only a whole
  interval above zero counts as an edge.

## The rule, no filter

| data | sessions | trades with limits | mean R | every signal | mean R (95 % interval) |
| --- | ---: | ---: | ---: | ---: | --- |
| Yahoo, 60 days | 4,440 | 180 | -0.185 | 2,347 | -0.132 (-0.168 to -0.095) |
| Alpaca SIP, 6 months | 9,450 | 378 | -0.073 | 5,154 | -0.123 (-0.148 to -0.098) |
| Alpaca SIP, 2 years | 37,178 | 1,500 | -0.114 | 21,042 | -0.102 (-0.114 to -0.089) |

Two years with the limits: win rate 45.9 %, profit factor 0.80, -$5,275
(about -$2,600 a year at $50 risk), worst drawdown -$5,613. The equity curve
mostly goes one way.

Why: with the stop at the range's midpoint, the risk per share is often about
0.5 % of the price, so 10 bps of round-trip slippage alone costs about 0.2R.
Before costs the rule is near zero; the costs decide.

## Looking for a filter, honestly

The two years were split at 2025-10-27. Filters were chosen on the first half
only (`--split --explore`), and the choice and the pass mark were written
down before the second half was looked at: **pass = the whole 95 % interval
above zero on the second half.**

First half, every signal (10,855), grouped:

| group | trades | mean R | 95 % interval |
| --- | ---: | ---: | --- |
| rvol 2 and up | 699 | +0.057 | -0.009 to +0.122 |
| gap under -1 % | 1,421 | +0.069 | +0.021 to +0.116 |
| SPY up at the signal | 7,360 | -0.078 | -0.100 to -0.057 |
| everything else | | below zero | |

Chosen: `--min-rvol 2` (stocks in play, the condition under which the
literature finds opening-range breakouts working) and, as a weaker second
hypothesis, `--max-gap -1`. Tested separately.

| filter | half | trades | mean R | 95 % interval | result |
| --- | --- | ---: | ---: | --- | --- |
| rvol >= 2 | first | 384 | -0.024 | -0.113 to +0.065 | |
| rvol >= 2 | **second** | 392 | -0.013 | -0.100 to +0.073 | **no edge** (break-even) |
| gap <= -1 % | first | 448 | +0.004 | -0.081 to +0.089 | |
| gap <= -1 % | **second** | 568 | -0.077 | -0.153 to -0.000 | **loses money** |

The rvol filter works in both halves in the same way: it lifts the rule from
about -0.10R to about zero. It removes the bad signals, but leaves no profit
after costs. The gap filter is the textbook case of a pattern found by chance:
significant on the first half, significantly negative on the second.

The second half has now been used. More filters tested on it would mean less.

## Conclusion

- Do not trade this rule with money, filtered or not. The data gives no reason to.
- The safety parts of the pack (risk check, the order gate, the live-price
  check, stale-data detection) work and stay useful; the strategy is what fails.
- A different strategy would need a fresh test on data no one has looked at:
  for example 2019 to 2023 (`--start 2019-01-01 --end 2023-12-31`), or a daily
  universe of the highest relative-volume stocks across the whole market
  instead of 75 big names (not built).

## Reproduce

```bash
python3 trader-timemachine/backtest.py                          # needs ALPACA_API_KEY / ALPACA_SECRET_KEY for 2 years
python3 trader-timemachine/backtest.py --split --explore
python3 trader-timemachine/backtest.py --split --min-rvol 2
python3 trader-timemachine/backtest.py --split --max-gap -1
```

Data changes daily, so later runs differ in the details. Not financial advice.
