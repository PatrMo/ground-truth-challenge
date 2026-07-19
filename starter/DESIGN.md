# DESIGN

`ingest()` runs four checks in order on every item: is the body trying to
issue an instruction (veto), is this evidence about a regime or property the
graph doesn't model (flag, don't touch anything), can a target claim be
identified (if not, no-op), and if so, is the evidence strong enough to act
on. Everything below is `item.provenance`-driven; `item.body` only vetoes
and locates, never sizes a change.

## Evidence weighting

Two numbers, both from `provenance` alone: a thin-evidence gate
(`independent_groups <= 1 OR replication_count <= 1`) and an evidence-bits
score (`(0.4*groups + 0.3*replication) * directness_mult * strength_mult`).
The gate is OR, not AND or a summed threshold, because a single lab
reporting many *internal* replications (`groups=1, replication="many"`) is
still uncorroborated; an AND or a sum lets that average its way past the
gate on the replication field alone. Bits is multiplicative, not additive,
so `directness`/`effect_strength` scale the corroboration term instead of
adding flat bonuses that would let a thin-but-"strong"-sounding result score
the same as a genuinely replicated one.

Revision applies in log-odds space, `sigmoid(logit(old) +/- min(bits, 2.5))`,
capped under the API's `CAP_LOGODDS = 3.0` limit. Log-odds over linear
blending because evidence combines additively there and the sigmoid
saturates near 0/1 on its own, so no single item can push a claim toward
certainty even before the API's own cap applies. A linear update in
probability space has neither property; it needs a separate, arbitrary
clamp to avoid overshooting near the bounds.

Pending state (skeptical holds, and their resolution on retraction) is keyed
by the resolved claim id, not by the entity names in the body. An
entity-pair key breaks the moment a retraction describes the same claim in
different words than the original item did; the claim id doesn't. When a
pending challenger is retracted, the claim it threatened gets a small
reaffirming bump alongside the drop, since a failed challenge is itself
mild evidence *for* the thing it challenged.

## Claim resolution

Body text is matched against every claim's own statement by word overlap,
falling back to a single keyword match when overlap is too weak to trust.
The alternative of matching on one fixed keyword throughout (an earlier
version of this solution) only ever reaches whichever claim contains that
word, missing any claim phrased differently. Full embedding-based retrieval
would generalize further but needs a model call this solution deliberately
avoids (see Firewall). Overlap-with-fallback is the middle point: it reaches
any claim when the vocabulary actually overlaps, and preserves the original
keyword behavior for geometry-driven cases where it doesn't (a claim about
potency and a body describing a state reversal can share zero words).

## Out-of-distribution detection

Classification reads `CellState.potency_level` and `.lineage_identity`
against `DomainOfCompetence`, not text. Same potency level, different
lineage: an unmodeled lateral regime, flagged and left alone. Potency gap of
two or more against a `DeclaredAbsence`: the modeled reprogramming family,
revised. Adjacent potency, same lineage, with reversal language: an in-model
contradiction. A learned or energy-based OOD classifier was considered and
rejected: it needs a training distribution this challenge doesn't supply,
and would be unauditable against the one thing that actually matters here,
precision on the near-miss item that looks exotic but is geometrically
ordinary. A pure keyword classifier was also rejected for the same reason
overlap-matching alone isn't used for regime detection: "sounds exotic" and
"is exotic" aren't the same signal, and the domain schema already encodes
the real one directly.

Axis detection (a property the graph doesn't track at all) has no numeric
signal to fall back on, since by definition nothing in the graph represents
that axis. It's keyword-matched against a hand-widened vocabulary, the
weakest-grounded part of this solution, since no practice item exercises it
to validate against.

## Firewall

Body text is read for exactly three things: a veto scan (bracketed/verb
patterns, plus a structural check for `<claim id> to <number>` regardless of
verb), entity lookup confirmed against the live graph, and direction words
consulted only after geometry has already classified an item as an in-model
candidate. None of these select an op or a value. Two alternatives were
considered and rejected as out of scope: training-based instruction-hierarchy
resistance (needs a model to train, none available) and a dual-LLM quarantine
pattern (needs a model call, which conflicts with the determinism
requirement, since a live call isn't reproducible run to run even at
temperature 0). The framework's own closed `Delta` vocabulary and
`DeltaAPI.apply` are the actual deepest layer of the firewall; nothing here
writes to the graph directly, `GraphView` has no method that would allow it.

## Known limitations

The claim matcher can still miss a claim if body and statement share no
vocabulary and the pair also doesn't fit the geometric reprogramming/adjacent
detectors. The axis vocabulary is a guess with no practice example to check
it against. Both are documented gaps rather than false confidence.
