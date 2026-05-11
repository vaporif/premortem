## Pitch

> One-liner: Real-time on-chain threat detection, automated incident response, and compliance monitoring for Web3 — operated as a product by Hacken.
>
> What it does: Two products:
>
> Extractor — runtime monitoring with 60+ crypto-native detectors across four layers (financial/governance/compliance/security). Pre-approved smart actions and a smart-contract firewall that can pause contracts, blacklist addresses, freeze flows, enforce rate limits when triggers fire. SIEM/webhook integrations for Web2↔Web3 unified policy.
> A3 Compliance Dashboard — KYT, sanctions monitoring, audit-ready reports for AML/BSA, MiCA, DORA, FATF, ADGM. Targets exchanges, regulators, and VC funds.
>
> Supports 17+ chains (Ethereum, Polygon, Arbitrum, Optimism, BNB, Avalanche, Base, zkSync, Stellar, ICP, VeChain). Verticals: stablecoin issuers, RWA tokenization, DeFi protocols, exchanges. Full setup included as part of the deal.

## Command

```sh
just query "One-liner: Real-time on-chain threat detection, automated incident response, and compliance monitoring for Web3 — operated as a product by Hacken. ..."
```

## Run

- Synthesized: 2 candidates (3 dropped post-synth by similarity floor)
- Cost: $0.3814
- Latency: 188s
- Query Trace: [Laminar](https://laminar.sh/shared/traces/57dbf282-effd-2f5b-1e1b-c354c5eb4102)
- Top matches: Harpie, Cylance (via LLM recall — coverage gap triggered live-web verification)

Coverage gap in the ingested corpus triggered the LLM-recall path: Opus suggested 8 candidates (Nyota Networks, Dedaub Watchdog, BlockSec Phalcon, Ironblocks, Hypernative, Forta Network, Harpie, Elliptic). After `recall_verify` (name match → URL liveness → death-keyword body check → LLM verdict), only Harpie was admitted as dead with high confidence and persisted into the corpus for future runs.

Full report: [`report.md`](report.md).
