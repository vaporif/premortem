## Pitch

> ResQuant (resquant.com)
>
> One-liner: Hardware post-quantum cryptography for IoT, automotive, ICT, and defense.
>
> What it does: Three product lines targeting the PQC migration:
>
> - PQC IP Core licenses — hardware accelerator implementations of NIST PQC algorithms (Dilithium, Kyber, SHAKE, AES, XMSS, SPHINCS+), tested vendor-independently across FPGAs.
> - FPGA with PQC — pre-equipped with the full NIST PQC suite for proof-of-concept integration.
> - PQC System-on-a-Chip — in R&D; secure enclave designed and manufactured in EU. Mass production targeted 2027.
>
> Pitch: 'Harvest now, decrypt later' is a real threat. NIST, NSA, NATO, and EU agencies are mandating PQC; ResQuant offers EU-sovereign hardware-level PQC ready for regulated sectors (military, automotive V2X, satellite comms, smart cards).

## Command

```sh
just query ". ResQuant (resquant.com) — One-liner: Hardware post-quantum cryptography for IoT, automotive, ICT, and defense. ..."
```

## Run

- Synthesized: 2 candidates (1 dropped pre-synth, 2 dropped post-synth by similarity floor)
- Cost: $0.6914
- Latency: 225s
- Query Trace: [Laminar](https://laminar.sh/shared/traces/749209d3-6b9f-5c6b-8e8f-c3f9ceb3bf63)
- Top matches: Dark Labs (corpus), Esperanto Technologies (via LLM recall — custom-silicon analog)

PQC hardware is a niche; the ingested corpus has no in-vertical dead startups, so the coverage-gap predicate fires and LLM recall runs. Opus suggests 8 candidates (Zapata AI, Esperanto Technologies, SiFive, BelGaN, Cryptosense, Inside Secure, MagiQ Technologies, Centaur Technology). After `recall_verify` (Tavily search → URL liveness → death-keyword body check → Haiku deathness verdict), 4 are admitted as dead and persisted: Zapata AI, Esperanto Technologies, BelGaN, Centaur Technology. The remaining 4 are correctly rejected — Cryptosense was a healthy SandboxAQ strategic acquisition, Inside Secure's IP continues at Rambus, SiFive is struggling but alive, and MagiQ's homepage no longer anchors the company name. After re-retrieve and re-rerank, two cross-vertical analogs clear the lowered post-recall similarity floor: Dark Labs (French sovereign-defense capital-cycle failure) and Esperanto Technologies (RISC-V AI accelerator that exhausted capital before commercial scale). Consolidated risks lead with four HIGH items: funding runway past the 2027 SoC mass-production target, near-term revenue from IP-core licensing and FPGA kits, non-dilutive grants ahead of slow defense procurement, and securing at least one paying commercial customer before sovereign contracts materialize.

Full report: [`report.md`](report.md).
