## Pitch

> One-liner: Automated, source-traceable risk assessment for DeFi protocol ratings and VC due diligence.
>
> What it does: You define a methodology or schema (risk fields, criteria, thresholds). They run a multi-model, multi-source verification pipeline that produces a structured, cited evidence report. Two product surfaces:
>
> - **Web3 / DeFi intelligence** — research protocols against docs, on-chain data, and public sources. Pitches a 50→200+ protocol coverage jump per analyst, ~2 hours per protocol vs. 8–60h manual, with conflict detection (e.g. 'docs say 5-of-10 multisig, on-chain Safe shows 5-of-9'). Output is API-ready JSON or spreadsheet.
> - **VC / startup DD** — upload a pitch deck, get an IC-ready report in 4–6 hours. Cross-checks claims against state filings, court records, regulatory databases. Surfaces conflicts (pitch vs. filing revenue), undisclosed related-party transactions, vendor concentration, regulatory exposure.
>
> Pitch: 'We automate the evidence pipeline, not the judgment.' Every claim links to source, page, timestamp. Three models (Claude, Gemini, GPT) cross-verify; ~100M tokens per verification, ~1000+ sources, 13 pipeline stages.

## Command

```sh
just query "One-liner: Automated, source-traceable risk assessment for DeFi protocol ratings and VC due diligence. ..."
```

## Run

- Synthesized: 2 candidates retained (3 cross-vertical recall matches cut as weak — see Curation note)
- Cost: $0.5855
- Latency: 259s
- Query Trace: [Laminar](https://laminar.sh/shared/traces/9980c9a6-4578-88b5-3158-f4305514753f)
- Strong matches retained: **Ross Intelligence** (AI-powered citation-first legal research, killed by Thomson Reuters copyright suit), **Dotscience** (MLOps provenance platform, couldn't scale GTM)

This pitch is a B2B SaaS AI evidence pipeline with two product surfaces and a citation-first positioning ("we automate the evidence pipeline, not the judgment"). The ingested corpus has no in-vertical dead startups, so coverage gap fires and LLM recall runs. Opus suggests 8 candidates (DappRadar, Messari, Nansen, Norse Corp, Cover Protocol, Quantstamp, Harvest.ai, Step Finance); `recall_verify` admits 6 as dead/struggling and correctly rejects 2 as alive (Quantstamp, Harvest.ai). After re-retrieve and re-rerank, 2 candidates clear the post-recall similarity floor and synthesize.
