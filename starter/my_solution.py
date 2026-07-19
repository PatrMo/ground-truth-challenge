"""ingest() for the GROUND TRUTH challenge.

Reads the belief graph through a read-only view, decides whether and how to
update it for one evidence item, and returns the decision as a list of
Deltas. Confidence magnitude always comes
from item.provenance; item.body is read only to veto suspicious text and to
locate which claim is being discussed, never to set a value. Rationale for
each choice below, and the alternatives considered, is in DESIGN.md.
"""
from __future__ import annotations
import re
from groundtruth.deltas import Delta, no_op
from groundtruth.ingest import EvidenceItem, IngestResult
from groundtruth.model import GraphView, logit, sigmoid

_COUNT_MAP = {"none": 0, "zero": 0, "one": 1, "a_few": 2, "few": 2,
              "several": 3, "some": 3, "many": 5}
_DIRECTNESS_MULT = {"direct": 1.0, "indirect": 0.5, "observational": 0.7}
_EFFECT_MULT = {"weak": 0.6, "moderate": 0.85, "strong": 1.15}

_METHOD_TO_MECHANISM = {
    "defined_factor_perturbation": "defined_factor",
    "environmental_stress": "env_stress",
    "oocyte_nuclear_transfer": "oocyte_nt",
    "observational": "spontaneous",
    "lineage_tracing": "spontaneous",
}

_REVERT_KEYWORDS = ("revert", "reverted", "returned to", "regressed",
                    "less-committed", "less committed", "less differentiated",
                    "de-differentiat", "dedifferentiat")

_SUPPORT_KEYWORDS = ("consistent with", "confirmed", "confirms", "confirming",
                     "replicated the finding", "reproduces the prior", "supports",
                     "in line with", "as expected", "normal transitions",
                     "no deviation")

_INJECTION_RE = re.compile(
    r'\[[^\]]*\b(set|delete|override|force|ignore)\b[^\]]*\]', re.IGNORECASE)
_IMPERATIVE_RE = re.compile(
    r'\b(set|override|force)\b[^.\n]{0,40}\b(confidence|belief|claim)\b', re.IGNORECASE)

_AXIS_KEYWORDS = {
    "biological_age": ("aging", "aged", "biological age", "senescen", "older", "younger",
                       "passage number", "telomere", "oxidative stress", "chronological age",
                       "epigenetic age", "accumulated damage", "replicative capacity"),
    "cell_function_independent_of_identity": (
        "function improved", "functional capacity", "improved function",
        "without changing identity", "identity unchanged", "same identity",
        "secretory output", "contractile strength", "metabolic output",
        "metabolic activity", "markers unchanged", "identity genes unchanged"),
}

_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "to", "of", "in", "on",
    "by", "and", "or", "not", "no", "any", "all", "at", "with", "from",
    "cannot", "only", "between", "under", "into", "this", "that", "than",
    "does", "do", "did", "be", "as", "it", "its", "for", "then", "same",
})

_CAMEL_RE = re.compile(r'\b[A-Z][A-Za-z]+\b')
_TITLECASE_RUN_RE = re.compile(r'\b(?:[A-Z][a-z]+[ -]?){1,4}')


def _cat(value, mapping, default=0.0):
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(mapping.get(value.strip().lower(), default))
    return default


def _evidence_bits(prov: dict) -> float:
    """Corroboration dominates; directness/strength scale it, not add to it."""
    groups = _cat(prov.get("independent_groups"), _COUNT_MAP)
    repl = _cat(prov.get("replication_count"), _COUNT_MAP)
    base = 0.4 * min(groups, 5) + 0.3 * min(repl, 5)
    direct = _DIRECTNESS_MULT.get(str(prov.get("method_directness", "")).strip().lower(), 0.75)
    strength = _EFFECT_MULT.get(str(prov.get("effect_strength", "")).strip().lower(), 0.85)
    return base * direct * strength


def _is_thin_evidence(prov: dict) -> bool:
    """Either signal alone being thin is enough -- internal replication
    without independent groups isn't corroboration."""
    groups = _cat(prov.get("independent_groups"), _COUNT_MAP)
    repl = _cat(prov.get("replication_count"), _COUNT_MAP)
    return groups <= 1 or repl <= 1


def _is_retracted(prov: dict) -> bool:
    status = str(prov.get("retraction_status", "none") or "none").strip().lower()
    return status not in ("none", "")


def _mentions_direct_edit(body: str, view: GraphView) -> bool:
    """"<claim id> to <number>" regardless of verb -- keyed to a real id
    instead of a guessed verb list."""
    for cid in view.list_claim_ids():
        if re.search(rf'\b{re.escape(cid)}\b[^.\n]{{0,20}}\bto\b[^.\n]{{0,10}}[01](\.\d+)?\b',
                     body, re.IGNORECASE):
            return True
    return False


def _is_injection(body: str, view: GraphView) -> bool:
    return bool(_INJECTION_RE.search(body) or _IMPERATIVE_RE.search(body)
                or _mentions_direct_edit(body, view))


def _extract_states(body: str, view: GraphView):
    found = {}
    candidates = _CAMEL_RE.findall(body) + [
        re.sub(r'[ -]', '', m) for m in _TITLECASE_RUN_RE.findall(body)]
    for tok in candidates:
        if tok in found:
            continue
        cs = view.cell_state(tok)
        if cs is not None:
            found[tok] = cs
    return list(found.values())


def _classify_regime(states, view: GraphView):
    if len(states) != 2:
        return "single_or_none"
    a, b = states
    same_lineage = a.lineage_identity == b.lineage_identity
    dpotency = abs(a.potency_level - b.potency_level)
    if dpotency == 0 and not same_lineage:
        return "lateral_ood"
    if dpotency >= 2 and (view.has_absence(a.name, b.name) or view.has_absence(b.name, a.name)):
        return "in_model_reprogramming"
    if dpotency == 1 and same_lineage:
        return "in_model_adjacent"
    return "unknown"


def _excluded_axis_hit(body: str, domain):
    body_l = body.lower()
    for axis in domain.axes_excluded:
        for kw in _AXIS_KEYWORDS.get(axis, (axis.replace("_", " "),)):
            if kw in body_l:
                return axis
    return None


def _find_claim_by_keyword(view: GraphView, keywords):
    for cid in view.list_claim_ids():
        c = view.get_claim(cid)
        s = c.statement.lower()
        if any(k in s for k in keywords):
            return cid, c
    return None, None


def _find_best_claim_match(body: str, view: GraphView, fallback_keywords=("potency",)):
    # Overlap-scores every claim's own words against the body 
    # falls back to the single-keyword lookup when overlap is too weak to trust.
    body_words = set(re.findall(r'[a-z]+', body.lower())) - _STOPWORDS
    best_cid, best_claim, best_score = None, None, 0
    for cid in view.list_claim_ids():
        c = view.get_claim(cid)
        claim_words = set(re.findall(r'[a-z]+', c.statement.lower())) - _STOPWORDS
        score = len(body_words & claim_words)
        if score > best_score:
            best_cid, best_claim, best_score = cid, c, score
    if best_score >= 2:
        return best_cid, best_claim
    return _find_claim_by_keyword(view, fallback_keywords)


def _resolve_reprogramming_target(view: GraphView, prov: dict):
    cid, claim = _find_claim_by_keyword(view, ("return", "pluripotency", "source state"))
    if claim is None:
        return None, None
    if claim.derived_from:
        mech = _METHOD_TO_MECHANISM.get(str(prov.get("method_class", "")).strip().lower())
        if mech:
            for child_id in claim.derived_from:
                child = view.get_claim(child_id)
                if child is not None and child.scope.get("mechanism_class") == mech:
                    return child_id, child
        # no confident mechanism match: fall back to the umbrella claim itself
    return cid, claim


def ingest(item: EvidenceItem, view: GraphView) -> IngestResult:
    body = item.body
    prov = item.provenance

    if _is_injection(body, view):
        return IngestResult([no_op(item.id)], "embedded instruction ignored; body is untrusted",
                             0.9, False)

    states = _extract_states(body, view)
    retracted = _is_retracted(prov)
    domain = view.domain()
    regime = _classify_regime(states, view)

    body_l = body.lower()
    is_revert = any(kw in body_l for kw in _REVERT_KEYWORDS)
    is_support = any(kw in body_l for kw in _SUPPORT_KEYWORDS)
    target_cid = target_claim = None
    direction = 0
    if regime == "in_model_reprogramming":
        target_cid, target_claim = _resolve_reprogramming_target(view, prov)
        direction = -1
    elif is_revert:
        target_cid, target_claim = _find_best_claim_match(body, view)
        direction = -1
    elif is_support:
        target_cid, target_claim = _find_best_claim_match(body, view)
        direction = 1

    # keyed by target_cid, not entity wording
    if retracted:
        if target_cid is not None:
            key = f"pending_{target_cid}"
            if key in view.pending_ids():
                deltas = [Delta("drop_claim", item.id, {"claim_id": key})]
                if target_claim is not None and target_claim.confidence < 0.97:
                    reaffirmed = sigmoid(logit(target_claim.confidence) + 0.3)
                    deltas.append(Delta("revise_confidence", item.id,
                                         {"claim_id": target_cid, "new_confidence": round(reaffirmed, 4)}))
                return IngestResult(
                    deltas,
                    "challenger retracted/failed to replicate; dropping the pending "
                    "hypothesis and reaffirming the claim it threatened", 0.8, False)
        return IngestResult([no_op(item.id)], "retracted result; no standing claim depends on it",
                             0.6, False)

    if regime == "lateral_ood":
        return IngestResult(
            [Delta("propose_regime", item.id, {"regime": "lateral_somatic_conversion"})],
            "lateral transition between same-potency, different-lineage states; "
            "out-of-model regime, not a contradiction of any existing claim", 0.75, True)

    axis_hit = _excluded_axis_hit(body, domain)
    if axis_hit is not None:
        return IngestResult(
            [Delta("propose_axis", item.id, {"axis": axis_hit})],
            f"describes '{axis_hit}', a property outside the modeled axes", 0.7, True)

    if target_claim is None:
        return IngestResult([no_op(item.id)], "no grounded target claim identified", 0.4, False)

    if direction > 0:
        # already-strong, uncontested: no room to move, so no_op rather than
        # a token bump that would register as an unwanted mutation attempt
        if target_claim.confidence >= 0.9 and target_claim.epistemic_status != "contested":
            return IngestResult(
                [no_op(item.id)],
                "routine confirmation of an already-established claim; no meaningful update",
                0.5, False)
        bits = _evidence_bits(prov)
        step = min(bits, 2.5)
        new_conf = sigmoid(logit(target_claim.confidence) + step)
        return IngestResult(
            [Delta("revise_confidence", item.id,
                   {"claim_id": target_cid, "new_confidence": round(new_conf, 4)})],
            f"confirming evidence; evidence_bits={bits:.2f}, strengthening {target_cid}",
            min(0.5 + bits / 6, 0.95), False)

    if _is_thin_evidence(prov):
        key = f"pending_{target_cid}"
        return IngestResult(
            [Delta("hold_pending", item.id, {"claim_id": key,
                                              "note": f"single-source, unreplicated, vs {target_cid}"})],
            "extraordinary claim, thin corroboration; holding pending, not revising", 0.55, False)

    bits = _evidence_bits(prov)
    step = min(bits, 2.5)
    new_conf = sigmoid(logit(target_claim.confidence) - step)
    deltas = [Delta("revise_confidence", item.id,
                    {"claim_id": target_cid, "new_confidence": round(new_conf, 4)})]
    if regime == "in_model_reprogramming":
        mech = _METHOD_TO_MECHANISM.get(str(prov.get("method_class", "")).strip().lower())
        if mech:
            deltas.append(Delta("set_scope", item.id,
                                 {"claim_id": target_cid, "scope": {"mechanism_class": mech}}))
    return IngestResult(
        deltas,
        f"in-model contradiction; evidence_bits={bits:.2f}, targeting {target_cid}",
        min(0.5 + bits / 6, 0.95), False)
