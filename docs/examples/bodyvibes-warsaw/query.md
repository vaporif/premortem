## Pitch

> One-liner: ClassPass for Warsaw — single subscription, 6000+ classes across 80+ studios, 20+ sport types.
>
> What it does: Subscription app for booking studio fitness classes (yoga, pilates, reformer, crossfit, barre, spinning, dance) at boutique studios. One-click booking, check-in via app, "Plus" tier for premium classes. Claims 2000+ active users, savings up to 50% vs. drop-in. B2B tier for corporates, vouchers, partner program for studios.
>
> Pitch: Polish answer to ClassPass / Gympass for boutique fitness in Warsaw. Variety > single gym membership; mobile-first; partner-friendly to small studios that lack their own booking infrastructure.

## Command

```sh
just query "BodyVibes (bodyvibes.pl) — ClassPass for Warsaw: single subscription, 6000+ classes across 80+ studios. ..."
```

## Run

- Synthesized: 2 candidates (3 dropped post-synth by similarity floor)
- Cost: $0.5005
- Latency: 239s
- Query Trace: [Laminar](https://laminar.sh/shared/traces/05db7173-e6c3-5fa2-2e80-4e56e6bb6508)
- Top matches: GuavaPass (via LLM recall — coverage gap), MoviePass

Thin-corpus query: ingested post-mortems contained no boutique-fitness-aggregator deaths above the `min_similarity_score = 4.0` floor, so the coverage gap fired LLM recall. Recall verification admitted GuavaPass (acquired and shut down by ClassPass in 2019) as the structurally identical analog — same multi-studio subscription, mobile booking, B2B tier, partner-studio playbook. MoviePass survived from the corpus on the strength of its unit-economics lessons despite a weaker sector match (sector=4.0).

Full report: [`report.md`](report.md).
