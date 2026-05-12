## Pitch

> Touchmarket (touch.market)
>
> One-liner: On-chain one-touch options — speculate on whether a price level gets touched in a window, with capped downside, no liquidations.
>
> What it does: You click a chart to mark a target price, set a time window, size your stake, and lock in a payout. If the price touches the target before expiry, you win; otherwise you lose only the stake. Built on "Blueprints" (likely a derivatives venue/protocol). Currently testnet-only with paper money on BTC, ETH, BNB, XRP, SOL, SUI. Pricing via integrated market makers' orderbook.
>
> Pitch: Perps and futures are broken because volatility, stop hunts, and liquidations kill correct directional calls. One-touch options give defined max loss, no maintenance margin, no forced close, and you can exit anytime at MM-quoted price. Modeled to outperform leveraged TP/SL by ~21% in winning scenarios under specific assumptions (Brownian motion, 2× multiplier, no slippage — favorable to the comparison).

## Command

```sh
just query "Touchmarket (touch.market) — On-chain one-touch options: speculate on whether a price level gets touched in a window, with capped downside, no liquidations. ..."
```

## Run

- Synthesized: 3 candidates (2 dropped post-synth by similarity floor)
- Cost: $0.4981
- Latency: 226s
- Query Trace: [Laminar](https://laminar.sh/shared/traces/31888753-2fc0-99a2-d7a3-bb5abbeb959b)
- Top matches: Ribbon Finance (via LLM recall — currently struggling, merged into Aevo), Kin (by Kik Interactive)

Coverage gap in the ingested corpus — no on-chain options/derivatives post-mortems cleared the `min_similarity_score = 4.0` floor — triggered LLM recall. Ribbon Finance was admitted as the closest structural analog (DeFi options protocol, RBN token collapsed ~82% YoY, absorbed into Aevo in 2023) and persisted to the corpus for future runs. Kin surfaced as the regulatory cautionary tale: binary/one-touch options are an explicitly named regulated instrument under CFTC rules, and "on-chain" has not shielded prior projects from securities/derivatives enforcement. Consolidated risks lead with three HIGH regulatory items and one HIGH market-maker-liquidity item — the structural failure modes shared across both comparables.

Full report: [`report.md`](report.md).
