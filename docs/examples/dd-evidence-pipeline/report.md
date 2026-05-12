# Slopmortem report for (unnamed)

Pitch: One-liner: Automated, source-traceable risk assessment for DeFi protocol ratings and VC due diligence.
What it does: You define a methodology or schema (risk fields, criteria, thresholds). They run a multi-model, multi-source verification pipeline that produces a structured, cited evidence report. Two product surfaces:

Web3 / DeFi intelligence — research protocols against docs, on-chain data, and public sources. Pitches a 50→200+ protocol coverage jump per analyst, ~2 hours per protocol vs. 8–60h manual, with conflict detection (e.g. 'docs say 5-of-10 multisig, on-chain Safe shows 5-of-9'). Output is API-ready JSON or spreadsheet.
VC / startup DD — upload a pitch deck, get an IC-ready report in 4–6 hours. Cross-checks claims against state filings, court records, regulatory databases. Surfaces conflicts (pitch vs. filing revenue), undisclosed related-party transactions, vendor concentration, regulatory exposure.

Pitch: 'We automate the evidence pipeline, not the judgment.' Every claim links to source, page, timestamp. Three models (Claude, Gemini, GPT) cross-verify; ~100M tokens per verification, ~1000+ sources, 13 pipeline stages.

Generated: 2026-05-12T00:41:07.780850+00:00

> **Curation note:** This is a curated example. Three weak comparables from
> the original run (DappRadar, Harpie, Cover Protocol) were cut, and Top
> Risks items raised only by those entries were dropped. Section headers
> for the two retained candidates were renamed from their legacy canonical
> IDs (`25426044` → Ross Intelligence, `23247304` → Dotscience). See
> [`query.md`](query.md) for the full run context.

## Top risks across all comparables

1. [HIGH] Audit every third-party data source (state filings, court records, regulatory databases, on-chain data) for IP licensing risk before signing enterprise contracts.
   Applies because: Pitch names specific data sources — 'state filings, court records, regulatory databases, on-chain data, public sources' — each is a potential IP or licensing liability analogous to the Thomson Reuters lawsuit that killed Ross Intelligence.
   Raised by: Ross Intelligence (1/2)

2. [HIGH] Establish clear liability and SLAs for missed conflicts or hallucinated citations before institutional clients rely on reports.
   Applies because: Pitch sells conflict detection (e.g., 'docs say 5-of-10 multisig, on-chain Safe shows 5-of-9') and IC-ready VC DD reports as core value — institutional B2B clients will demand contractual remediation if a conflict is missed or a citation is fabricated.
   Raised by: Ross Intelligence (1/2)

3. [HIGH] Validate willingness-to-pay before scaling the ~100M-token-per-verification compute pipeline or costs will sink you.
   Applies because: Pitch explicitly states '~100M tokens per verification' across three models (Claude, Gemini, GPT) and 1000+ sources across 13 pipeline stages — a cost structure that must be covered by customer revenue per run.
   Raised by: Dotscience (1/2)

4. [HIGH] Enter DeFi intelligence and VC DD verticals sequentially, not simultaneously, to avoid split GTM focus killing both.
   Applies because: Pitch explicitly describes two distinct product surfaces launched together — 'Web3/DeFi intelligence' and 'VC/startup DD' — doubling GTM complexity and diluting the core narrative.
   Raised by: Dotscience (1/2)

5. [MEDIUM] Secure a named paying reference customer in each vertical before scaling sales headcount or spend.
   Applies because: Pitch describes two distinct buyer types — 'VC analysts' and 'protocol risk desks' — but names no current paying customers or signed pilots in either vertical.
   Raised by: Ross Intelligence, Dotscience (2/2)

6. [MEDIUM] Price and package around quantified ROI (analyst hours saved, conflicts found) so procurement can approve budget without an internal champion arguing abstractly.
   Applies because: Pitch quantifies '2 hours vs. 8–60h manual' and '50→200+ protocol coverage per analyst' — these are strong ROI hooks but must be translated into dollar savings in sales collateral to pass procurement review.
   Raised by: Dotscience (1/2)

7. [MEDIUM] Build contractual recurring enterprise deals early; ad-hoc and freemium conversion evaporate faster than locked subscriptions.
   Applies because: The DeFi intelligence surface targets protocol research budgets that are directly correlated with crypto market cycles, and the pitch does not describe any locked recurring contract structure.
   Raised by: Dotscience (1/2)

8. [LOW] Design the data pipeline so any single source can be swapped without retraining; avoid load-bearing dependency on one incumbent's proprietary database.
   Applies because: Pitch ingests 'docs, on-chain data, public sources, state filings, court records, regulatory databases' — if any single incumbent restricts access or sues, the pipeline must survive the swap without product disruption.
   Raised by: Ross Intelligence (1/2)

## Ross Intelligence

AI-powered legal research platform that let lawyers ask plain-English questions and receive cited case law in seconds, backed by $13M from YC and top law firms—killed by a Thomson Reuters copyright lawsuit over AI training data.

Failure date: 2020-01-01
Lifespan: unknown

Similarity:

| Perspective | Score | Rationale |
| --- | --- | --- |
| business_model | 7.0 | Both are B2B SaaS platforms sold to enterprise buyers (law firms vs. DeFi analysts/VCs), both promise dramatic analyst productivity gains (hours saved per research task), and both monetize via recurring subscription. The new pitch adds an API/data-feed surface and usage-intensive multi-model pipeline that Ross did not have, but the core value-exchange—pay a subscription to replace manual research hours—is closely aligned. |
| market | 5.0 | Ross served the legal research market exclusively; the new pitch targets DeFi protocol intelligence and VC due diligence. Both markets involve high-stakes, evidence-based professional research where incumbents control large proprietary data moats, but the specific buyer personas, competitive landscapes, and regulatory frameworks are distinct. The structural parallel (professional researchers needing cited, traceable evidence) is real but the addressable markets do not overlap. |
| gtm | 6.0 | Ross went direct to large law firms via named partnerships (BakerHostetler, Latham, Sidley). The new pitch targets analysts and VCs similarly via direct enterprise sales with API-ready outputs. Both rely on demonstrating time-savings and quality improvements to professional users at mid-to-large institutions. The sales-cycle dynamics and proof-of-concept motion are structurally similar. |
| stage_scale | 5.0 | Ross had raised $13M, achieved named enterprise customers, and was growing when it was forced to shut down—well past seed. The new pitch appears to be at an early/pre-product stage. Some similarity exists in that both are pre-scale enterprise SaaS, but Ross was further along operationally at the time of its demise. |

Why similar:

Both Ross Intelligence and the new pitch build an AI-powered, citation-first research automation platform for expert professionals. Both promise a step-function reduction in manual hours per research task (Ross: legal case research; new pitch: protocol/startup DD). Both sell to enterprise buyers as recurring-subscription SaaS, and both anchor their credibility on source traceability—every output links back to a primary source. Ross's founding thesis ('lawyers shouldn't manually dig through databases') maps almost exactly onto the new pitch's framing ('we automate the evidence pipeline, not the judgment'). The enterprise go-to-market via named institutional buyers and the multi-source verification angle (Ross: NLP over legal databases; new pitch: three LLMs over on-chain + public records) are structurally parallel.

Where diverged:

1. Data moat exposure: Ross's fatal flaw was depending on training data controlled by the incumbent (Thomson Reuters/Westlaw); the new pitch draws on open on-chain data, public filings, court records, and state databases—sources that no single incumbent can gate. 2. Sector: Ross was purely legal research; the new pitch spans DeFi protocol intelligence and VC startup DD, two markets without the same IP-lawsuit risk from a dominant data licensor. 3. Multi-model architecture: the new pitch explicitly runs three LLMs (Claude, Gemini, GPT) in a cross-verification pipeline—a design choice that reduces single-model hallucination risk and vendor lock-in, something Ross did not have. 4. Output format: the new pitch produces API-ready JSON and IC-ready structured reports; Ross's output was conversational case-law retrieval.

Failure causes:

- copyright infringement lawsuit by incumbent (Thomson Reuters)
- AI training data controlled by dominant competitor
- insufficient capital to sustain prolonged litigation
- data moat held by legal research monopoly used as competitive weapon
- over-dependence on a single proprietary data source for model training
- inability to license or substitute training data at viable cost

Lessons:

- Audit every data source in your training and retrieval pipeline for IP risk before you have customers—on-chain and public-registry data is safer, but verify licensing terms for each third-party database you ingest.
- Do not let a single incumbent's proprietary data become a load-bearing dependency; design the pipeline so any one source can be swapped out without retraining.
- Raise enough capital (or secure litigation insurance) before signing enterprise contracts—if an incumbent sues, you need 18–24 months of runway to survive the uncertainty.
- Name and sign reference customers early, but ensure your contracts are defensible if your data pipeline is challenged; enterprise logos will not save you if the core IP is disputed.
- The 'evidence pipeline, not the judgment' framing is differentiated—lean into it hard in sales and fundraising to pre-empt commoditization fears and establish why source traceability is your durable moat rather than model performance alone.

Sources:

https://blog.rossintelligence.com/post/announcement

## Dotscience

MLOps platform for ML model reproducibility, provenance tracking, and governance targeting enterprise data science teams.

Failure date: 2020-01-01
Lifespan: unknown

Similarity:

| Perspective | Score | Rationale |
| --- | --- | --- |
| business_model | 6.0 | Both are B2B SaaS platforms sold to enterprise buyers on a subscription basis, automating a multi-step evidence or verification pipeline and delivering structured, auditable outputs. The core monetization mechanic (recurring subscription, API-ready output) maps well, though Dotscience targeted ML engineering teams while the new pitch targets risk/compliance and investment teams. |
| market | 3.0 | Dotscience operated in the MLOps/DevOps-for-ML space targeting enterprise data science teams. The new pitch targets DeFi protocol intelligence and VC due diligence — distinct verticals (crypto_web3 and fintech/VC). The common thread is 'structured, traceable evidence for high-stakes decisions,' but the buyer personas, regulatory contexts, and competitive landscapes diverge substantially. |
| gtm | 5.0 | Both companies pursued enterprise GTM with a technical proof-of-value story (reproducibility/provenance for Dotscience; source-cited, multi-model verification for the new pitch). Dotscience landed a named financial-services reference customer (TrueLayer) early. The new pitch similarly targets financial buyers (DeFi analysts, VC IC). Channel and sales motion appear similar: land on a compelling demo, expand by coverage or protocol count. |
| stage_scale | 6.0 | Dotscience shut down in May 2020 before reaching scale, having demonstrated product-market fit signals and at least one named customer but failing to secure follow-on funding. The new pitch appears to be pre-scale/early-stage, making this a relevant stage comparison: both are early-stage enterprise SaaS with proven technical capability but unproven go-to-market at scale. |

Why similar:

Both companies build an automated, multi-source evidence pipeline that produces structured, traceable outputs for enterprise buyers who need auditability and reproducibility. Dotscience tracked data provenance, model lineage, and compliance trails for ML teams; the new pitch tracks source citations, on-chain data, and regulatory filings for DeFi analysts and VC investors. The core value proposition in both cases is 'you can trust this output because every claim is linked to its origin.' Both target enterprise/institutional buyers, deliver API-ready or structured outputs, and use a subscription monetization model. Both also entered nascent, fast-moving markets where the buyer's workflow was largely manual before the product existed.

Where diverged:

1. Sector: Dotscience was squarely in MLOps/developer tooling; the new pitch operates in crypto_web3 DeFi intelligence and fintech VC due diligence — different buyer personas, sales cycles, and compliance regimes. 2. Data sources: Dotscience versioned internal ML artifacts (datasets, model weights, parameters); the new pitch ingests external on-chain data, court records, state filings, and regulatory databases — a fundamentally different data-access and freshness problem. 3. Conflict detection: The new pitch's explicit 'docs say X, on-chain shows Y' conflict-surfacing feature has no direct analogue in Dotscience's offering. 4. Multi-model cross-verification (Claude/Gemini/GPT ensemble) is architecturally novel relative to Dotscience's single-pipeline approach.

Failure causes:

- inability to scale go-to-market despite product-market fit signals
- failure to secure follow-on venture funding
- nascent/undersized market at time of launch (MLOps adoption still early in 2019-2020)
- crowded competitive landscape as well-funded incumbents entered MLOps
- early-pandemic venture capital contraction (contributing factor, May 2020)
- limited enterprise sales motion to convert pilots to recurring revenue at scale

Lessons:

- Secure a named, paying reference customer in each vertical (DeFi and VC DD) before scaling sales headcount — Dotscience's TrueLayer reference came late and could not compensate for GTM gaps.
- Build the go-to-market motion in parallel with the product; Dotscience built a technically strong platform but could not scale distribution before runway ran out.
- Price and package for measurable ROI: quantify analyst hours saved and conflict-detection value in dollar terms so procurement can justify the subscription budget without a champion having to argue abstractly.
- De-risk venture dependency by pursuing revenue-based milestones early; Dotscience's shutdown was precipitated by inability to raise follow-on, not by product failure — maintain a path to default-alive.
- Enter each vertical sequentially rather than simultaneously; launching DeFi intelligence and VC due diligence at the same time doubles GTM complexity and can dilute the core narrative for investors and buyers alike.

Sources:

https://dotscience.com/blog/2020-05-19-dotscience-is-shutting-down/

---

Pipeline meta (original run, pre-curation):

- cost_usd_total: 0.5855
- latency_ms_total: 258608
- trace_id: 9980c9a6-4578-88b5-3158-f4305514753f
- budget_remaining_usd: 1.4145
- budget_exceeded: False
- K_retrieve: 30
- N_synthesize: 5
- min_similarity_score: 4.0
- coverage_gap: True
- recall_used: True
- recall_persisted_count: 6

Models:

- facet: anthropic/claude-haiku-4.5
- rerank: anthropic/claude-sonnet-4.6
- synthesize: anthropic/claude-sonnet-4.6
