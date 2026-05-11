# Slopmortem report for (unnamed)

Pitch: Touchmarket (touch.market)
One-liner: On-chain one-touch options — speculate on whether a price level gets touched in a window, with capped downside, no liquidations.
What it does: You click a chart to mark a target price, set a time window, size your stake, and lock in a payout. If the price touches the target before expiry, you win; otherwise you lose only the stake. Built on Blueprints (likely a derivatives venue/protocol). Currently testnet-only with paper money on BTC, ETH, BNB, XRP, SOL, SUI. Pricing via integrated market makers' orderbook.
Pitch: Perps and futures are broken because volatility, stop hunts, and liquidations kill correct directional calls. One-touch options give defined max loss, no maintenance margin, no forced close, and you can exit anytime at MM-quoted price. Modeled to outperform leveraged TP/SL by ~21% in winning scenarios under specific assumptions (Brownian motion, 2× multiplier, no slippage — favorable to the comparison).

Generated: 2026-05-11T23:13:33.051969+00:00

## Top risks across all comparables

1. [HIGH] Get explicit legal opinions on whether one-touch options are regulated CFTC/SEC derivatives before mainnet.
   Applies because: Pitch explicitly describes 'on-chain one-touch options' on BTC, ETH, SOL and other assets — binary/one-touch options are a named regulated product under CFTC rules.
   Raised by: Kin (by Kik Interactive), 21120792 (2/3)

2. [HIGH] Geo-fence US retail users or obtain CFTC licensing before launch; 'on-chain' is not a regulatory safe harbor.
   Applies because: Pitch targets retail speculators with one-touch options on major crypto assets; US rules explicitly restrict binary/one-touch options for retail clients, and 'decentralized' status has not shielded prior protocols.
   Raised by: Kin (by Kik Interactive), 21120792 (2/3)

3. [HIGH] Budget for 18-24 months of regulatory defense as a first-class line item, not an afterthought.
   Applies because: Pitch is testnet-only now but plans mainnet on high-profile assets (BTC, ETH, BNB, XRP, SOL, SUI) that attract regulator scrutiny; undercapitalized projects cannot survive enforcement.
   Raised by: Kin (by Kik Interactive), 21120792 (2/3)

4. [HIGH] Secure deep, durable market-maker liquidity commitments before mainnet; options protocols die without tight spreads.
   Applies because: Pitch states pricing is via 'integrated market makers' orderbook' — if MMs withdraw, quoted exit prices and the core 'exit anytime' promise collapse, as seen with Ribbon's liquidity atrophy.
   Raised by: 5b2af439550af405, 21120792 (2/3)

5. [MEDIUM] Validate the ~21% outperformance model under real liquidity conditions before presenting it to users or investors.
   Applies because: Pitch explicitly cites '~21% outperformance' modeled under 'Brownian motion, 2× multiplier, no slippage' — assumptions the pitch itself flags as favorable, meaning real-world results could be materially worse.
   Raised by: 21120792 (1/3)

6. [MEDIUM] Avoid single-venue dependency on Blueprints; have a contingency if that protocol pivots or fails.
   Applies because: Pitch states the product is 'built on Blueprints' with no mention of alternative infrastructure, creating existential dependency on a single underlying venue.
   Raised by: 5b2af439550af405 (1/3)

7. [MEDIUM] Define a concrete, defensible edge over binary/prediction-market competitors before mainnet launch.
   Applies because: Pitch positions against perps/futures but does not address Polymarket, dYdX options, or other on-chain derivatives that offer similar defined-risk structures.
   Raised by: 5b2af439550af405 (1/3)

## Ribbon — currently struggling
*Source: LLM recall (verified against live web)*

DeFi options protocol offering structured yield products (Theta Vaults, covered calls, exotic options) on-chain; merged into Aevo in 2023 after token value collapsed ~82% year-over-year.

Distress observed: 2023-01-01

Similarity:

| Perspective | Score | Rationale |
| --- | --- | --- |
| business_model | 6.0 | Both are on-chain DeFi derivatives protocols built on smart contracts with usage-metered monetization. Ribbon sells options exposure (yield via covered calls) while Touchmarket sells binary one-touch options to speculators — different option types but the same underlying primitive (options) and the same protocol/smart-contract product type. |
| market | 7.0 | Both target the global crypto derivatives market, operating on the same major assets (BTC, ETH, and altcoins). Ribbon served yield-seeking DeFi users; Touchmarket targets directional speculators, but the addressable market of on-chain options users is the same narrow, crypto-native segment. |
| gtm | 5.0 | Both rely on permissionless on-chain access with no geographic gating, targeting a developer/DeFi-native customer base. Ribbon used governance tokens (RBN) and DAO structure for community distribution; Touchmarket is currently testnet-only with no stated token or DAO mechanism — GTM path is less defined but the permissionless DeFi-first approach is shared. |
| stage_scale | 6.0 | Ribbon launched in 2021 and reached meaningful TVL before struggling; Touchmarket is currently testnet-only. Both are early-stage at comparable founding points, though Ribbon was further along operationally at its 2023 distress point than Touchmarket is today. |

Why similar:

Both are on-chain derivatives protocols in the DeFi options sub-sector, built on smart contracts, globally accessible, and usage-metered. Both address the same structural critique of leveraged crypto trading (liquidation risk, forced closes) by offering defined-risk options products. Ribbon launched in 2021 — matching Touchmarket's comparable early-stage position — and its token governance structure and protocol architecture are the canonical DeFi options playbook Touchmarket is also following.

Where diverged:

1. Product type: Ribbon offered yield-generation strategies (covered calls, structured vaults) for depositors seeking passive income; Touchmarket offers speculative one-touch binary options for directional traders — fundamentally different user intent and risk profile. 2. Customer type: Trusted facts list Ribbon's customer as 'developer'; Touchmarket's pitch targets retail directional speculators explicitly. 3. Token/governance layer: Ribbon had a live governance token (RBN) and a DAO that ultimately voted the merger; Touchmarket shows no token or DAO mechanism in its pitch. 4. Venue dependency: Touchmarket is built on 'Blueprints' (a specific derivatives protocol), adding a dependency layer Ribbon did not have. 5. Stage: Touchmarket is testnet-only with paper money; Ribbon had live mainnet TVL before its distress.

Failure causes:

- token value collapse (RBN down ~82% YoY per document)
- absorbed via DAO-approved merger into Aevo (2023), indicating inability to sustain independent operation
- negligible trading volume ($5.57 per 24h per document) signaling severe liquidity atrophy
- loss of market relevance as competing perps and options venues scaled
- governance token dilution risk (83M circulating vs 1B max supply)
- inability to differentiate structured yield products in a crowded DeFi options market

Lessons:

- Do not build your protocol's survival around a governance token whose value is correlated to overall crypto sentiment — Ribbon's RBN lost 82% in a year, hollowing out community incentives.
- Secure deep, durable liquidity from market makers before mainnet launch; Ribbon's near-zero 24h volume shows that liquidity atrophy is fatal for options protocols dependent on tight spreads.
- Avoid single-venue dependency: Touchmarket's reliance on 'Blueprints' mirrors the risk of being absorbed or stranded if that underlying protocol pivots or fails.
- Define a concrete, defensible edge over binary/prediction-market competitors (e.g., Polymarket, dYdX options) before mainnet — Ribbon could not differentiate its structured products at scale.
- Plan for the DAO governance attack surface early: Ribbon's exit was decided by a DAO vote, meaning token holders — not founders — controlled the end-state of the protocol.

Sources:

https://www.ribbon.finance

## Kin (by Kik Interactive)

Cryptocurrency and digital ecosystem launched via ICO to power peer-to-peer commerce inside the Kik messaging app and partner apps.

Failure date: 2020-01-01
Lifespan: 36 months

Similarity:

| Perspective | Score | Rationale |
| --- | --- | --- |
| business_model | 4.0 | Both Kin and Touchmarket are crypto-native products monetized via transaction fees, but Kin's model was token-issuance/ICO-funded with an earn-and-spend token economy, while Touchmarket is a structured derivatives venue charging fees on option trades. The transaction-fee overlap is real but the underlying mechanics and revenue structure differ substantially. |
| market | 5.0 | Both target the global crypto consumer market and operate in the broader crypto-web3 sector. However, Kin targeted social-app users doing micro-commerce, whereas Touchmarket targets crypto traders seeking defined-risk speculation on price levels — overlapping audience but different use cases and risk profiles. |
| gtm | 3.0 | Kin relied on a large ICO raise ($100M) and an existing messaging-app userbase (Kik) for distribution. Touchmarket is testnet-only and appears to rely on organic adoption through a derivatives protocol (Blueprints). GTM strategies are quite different in reach, funding mechanism, and channel. |
| stage_scale | 5.0 | Both were/are early-stage crypto consumer products with no confirmed revenue at comparable milestones. Kin had a testnet-to-mainnet arc and was pre-product-market-fit when regulatory issues hit. Touchmarket is currently testnet-only, placing it at a similar pre-revenue stage. |

Why similar:

Both are global, consumer-facing crypto products (sector: crypto_web3, customer_type: consumer, geography: global) operating in a regulatory grey zone around financial instruments built on blockchain. Both are transaction-fee monetized and launched without formal securities registration or regulatory clearance. Kin was founded in 2017 and failed by 2020 — squarely in the same era of speculative crypto instrument launches that face SEC scrutiny. Touchmarket's one-touch options are structured financial products with defined payouts, placing them even more squarely in the 'securities or derivatives' classification debate that killed Kin.

Where diverged:

1. **Product type**: Kin was a cryptocurrency token used for social commerce/messaging; Touchmarket is a structured derivatives product (one-touch binary options) — options regulation is even more explicit than token regulation. 2. **Distribution channel**: Kin embedded itself into an existing consumer messaging app with millions of users; Touchmarket has no existing user base and is testnet-only. 3. **Funding**: Kin raised $100M via ICO; Touchmarket shows no disclosed raise. 4. **Regulatory exposure vector**: Kin was attacked as an unregistered securities offering; Touchmarket's binary options structure may trigger CFTC (not just SEC) oversight in the US, as binary options are a named regulated instrument. 5. **Ecosystem ambition**: Kin aimed to be a broad monetary system across partner apps; Touchmarket is narrowly focused on price-level speculation for traders.

Failure causes:

- SEC lawsuit alleging unregistered securities offering
- $100M ICO without proper investor disclosures or SEC registration
- Inability to get Kin classified as a currency rather than a security
- Exchange de-listing pressure from SEC
- Regulatory battle depleted resources and forced shutdown of core messaging app
- Settlement requiring $5M civil penalty and cessation of token operations

Lessons:

- Obtain legal clarity on whether your structured derivative product (one-touch binary option) constitutes a regulated instrument under CFTC or SEC rules before any mainnet launch — binary options are an explicitly named regulated product in the US.
- Do not assume 'on-chain' or 'decentralized' status provides a safe harbor from securities or derivatives regulation; regulators have consistently pierced that argument.
- Design your go-to-market to avoid US persons if you cannot secure regulatory approval, and document that exclusion rigorously from day one.
- Build a runway plan that accounts for an 18–24 month regulatory defense; undercapitalized projects cannot survive a protracted enforcement action.
- Avoid launching on a high-profile set of assets (BTC, ETH, SOL, etc.) that draws regulator attention before you have legal counsel on retainer and a compliance framework in place.

Sources:

https://medium.com/@tedlivingston/moving-forward-boldly-with-kin-ec6290a6453

---

Pipeline meta:

- cost_usd_total: 0.4981
- latency_ms_total: 225605
- trace_id: 31888753-2fc0-99a2-d7a3-bb5abbeb959b
- budget_remaining_usd: 1.5019
- budget_exceeded: False
- K_retrieve: 30
- N_synthesize: 5
- min_similarity_score: 4.0
- coverage_gap: True
- recall_used: True
- recall_persisted_count: 1

Models:

- facet: anthropic/claude-haiku-4.5
- rerank: anthropic/claude-sonnet-4.6
- synthesize: anthropic/claude-sonnet-4.6
