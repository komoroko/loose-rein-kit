"""The human review as a decision procedure, not a screen: sequence, challenge, expertise, budget.

review_api.py serves the pane and ui.py serves the bytes; this module owns the *rules* that make a
human a decision-maker rather than the ratifier of a tidy explanation (plan §14). Everything here is
pure over ``review.yaml`` — a machine half the generator produced and a human half the reviewer is
building — so each rule is a fixed fact a test can pin without a browser:

- **Sequence** (§14.1, §21.2). A high/critical Decision Card withholds its evidence until the
  reviewer has recorded what they think should happen. Seeing the answer before you have thought
  about the case yourself is exactly the priming the order exists to prevent, so the withholding
  is mechanical, not advisory.
- **Challenge** (§14.2, §14.3). A mismatch does not close on one acknowledgement: it opens a
  counterfactual the reviewer must resolve with a corrected mental model. The score is never
  evidence of anyone's correctness — the point is cognitive forcing.
- **Expertise** (§14.9, E2E-05). High/critical work in a domain the reviewer declared `partial` or
  `unfamiliar` cannot be closed by a general reviewer's risk acceptance; it needs an expert, an
  experiment, a scope reduction, a safe-default revision, or the behaviour removed.
- **Budget** (§14.10, E2E-30). Past a budget the answer is to *split the scope*, never to lengthen
  the screen — so exceeding one blocks completion with `scope_split_required` instead of scrolling.

The two halves are digested separately (models.Review). A human answering a challenge must not make
the machine review stale (E2E-09); a machine review regenerated under a reviewer's feet must refuse
that reviewer's next write (`assert_machine_current` → 409, E2E-08). The ``apply_*`` functions return
a new human mapping for store.Transaction to persist — this module never touches the disk itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rein import models

# The standard review budget (plan §14.10). A machine review may carry its own measured
# `review_budget`, but these are the limits the loop enforces when the config does not override
# them — the numbers past which a screen must become a scope split.
DEFAULT_BUDGET: dict[str, int] = {
    "max_critical_decisions": 5,
    "max_human_statements": 30,
    "max_unresolved_low_medium_unknowns": 5,
    "max_diff_bytes_per_partition": 524288,
}

#: Dispositions that discharge an expertise gap (plan §14.9). "Accept the risk" is deliberately not
#: among them — a general reviewer's acceptance is exactly what an unfamiliar domain may not rest on.
REMEDIATING_ACTIONS = frozenset(
    {"request_expert", "run_experiment", "reduce_scope", "revise_requirement", "revise_design", "revise_implementation"}
)

#: Where a domain counts as high-stakes: a decision card at this risk or above pulls its
#: `requires_domains` into the set that needs an expertise declaration.
_EXPERT_RISK_FLOOR = "high"

#: Challenge-first is scoped by risk, not applied to everything (see :func:`challenges`). A
#: low-risk change asks nothing; a high-risk one asks at most `MAX_CHALLENGES`, hardest first.
CHALLENGE_RISK_FLOOR = "high"
MAX_CHALLENGES = 3


class StaleReview(Exception):
    """A human write raced a machine-review regeneration — answer against the review you read.

    Carries no HTTP status of its own; the UI layer maps it to 409 (plan §17.5). The name of the
    failure is the whole point: the reviewer's answers were about a machine review that no longer
    exists, so merging them silently would attribute thought to an artefact nobody read.
    """


# -- reads over the machine half ----------------------------------------------


def _risk(mapping: Mapping[str, Any]) -> str:
    """A mapping's `risk` field as a ladder value, `low` when absent or malformed."""
    value = mapping.get("risk")
    return value if value in models.RISK_VALUES else "low"


def _machine_list(review: models.Review, key: str) -> tuple[Mapping[str, Any], ...]:
    value = review.machine.get(key)
    return tuple(v for v in value if isinstance(v, Mapping)) if isinstance(value, list) else ()


def _human_list(human: Mapping[str, Any], key: str) -> tuple[Mapping[str, Any], ...]:
    value = human.get(key)
    return tuple(v for v in value if isinstance(v, Mapping)) if isinstance(value, list) else ()


def challenges(review: models.Review) -> tuple[Mapping[str, Any], ...]:
    """The cards a reviewer must take a position on **before** seeing the evidence.

    A challenge is not a separate artefact any more, and no reviewer stage produces one: it is a
    high/critical Decision Card asked in two beats. The reviewer records what they think should
    happen, and only then does the card open its `evidence` — the Expected the plan states, the
    Actual the blind extractor read, and the gap or finding that raised it. Deriving the challenge
    from the card rather than asking a model for one is what makes this reachable at all; the
    generator-produced version was specified in three documents and written by nothing, so the
    forcing function it describes never once fired.

    Challenge-first is a cognitive-forcing function, and a forcing function applied to everything
    stops forcing anything: a reviewer asked the same number of questions about a typo fix as about
    a change to an authorisation boundary learns to clear the list. So the two-beat set is the high
    and critical cards only, capped — a low-risk change legitimately has none, and
    `challenges_complete` is then true from the start rather than after a ritual.

    Scoping in code rather than in a prompt is deliberate. A generator asked for "at most three,
    high risk only" that produced eight low-risk ones would silently reinstate the formality the
    cap exists to prevent, and the reviewer would have no way to tell.
    """
    cards = [c for c in _machine_list(review, "decision_cards") if models.risk_at_least(_risk(c), CHALLENGE_RISK_FLOOR)]
    cards.sort(key=lambda c: (-models.RISK_ORDER.index(_risk(c)), str(c.get("id", ""))))
    return tuple(cards[:MAX_CHALLENGES])


def _reveal(challenge: Mapping[str, Any]) -> Mapping[str, Any]:
    """A card's evidence — withheld until the reviewer has recorded their own read of it."""
    reveal = challenge.get("evidence")
    return reveal if isinstance(reveal, Mapping) else {}


# -- challenge sequence (plan §14.2, §14.3) -----------------------------------


def answered_challenge_ids(human: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(str(a.get("challenge_id")) for a in _human_list(human, "challenge_answers"))


def unanswered_challenges(review: models.Review, human: Mapping[str, Any]) -> list[str]:
    """Challenge ids the reviewer has not yet answered, in document order."""
    answered = answered_challenge_ids(human)
    return [str(c.get("id")) for c in challenges(review) if str(c.get("id")) not in answered]


def next_challenge(review: models.Review, human: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The next card awaiting a first read, **with its evidence stripped**.

    The evidence is not a secret — `review show` prints the whole document — but the API must not
    hand it back on the same screen that asks the question, or the forcing function is defeated for
    anyone using the UI rather than reading the YAML (plan §14.2).
    """
    answered = answered_challenge_ids(human)
    for challenge in challenges(review):
        if str(challenge.get("id")) not in answered:
            return {k: v for k, v in challenge.items() if k != "evidence"}
    return None


def reveal_for(review: models.Review, challenge_id: str) -> Mapping[str, Any] | None:
    """The reveal for an *answered* challenge; None if the challenge is unknown or unanswered."""
    for challenge in challenges(review):
        if str(challenge.get("id")) == challenge_id:
            return _reveal(challenge)
    return None


def mismatched_challenges(review: models.Review, human: Mapping[str, Any]) -> list[str]:
    """Answered challenges whose choice differs from the revealed expected choice."""
    expected = {str(c.get("id")): str(_reveal(c).get("expected_choice", "")) for c in challenges(review)}
    out = []
    for answer in _human_list(human, "challenge_answers"):
        cid = str(answer.get("challenge_id"))
        if cid in expected and expected[cid] and str(answer.get("choice")) != expected[cid]:
            out.append(cid)
    return out


def open_counterfactuals(review: models.Review, human: Mapping[str, Any]) -> list[str]:
    """Mismatched challenges the reviewer has not yet closed with a corrected model (plan §14.3)."""
    resolved = {
        str(cf.get("challenge_id"))
        for cf in _human_list(human, "counterfactual_answers")
        if str(cf.get("corrected_model", "")).strip()
    }
    return [cid for cid in mismatched_challenges(review, human) if cid not in resolved]


def challenges_complete(review: models.Review, human: Mapping[str, Any]) -> bool:
    """Every high/critical card read once unprimed, and every mismatch closed."""
    return not unanswered_challenges(review, human) and not open_counterfactuals(review, human)


# -- sequence enforcement (plan §14.1, §21.2) ---------------------------------


def stage_settled(review: models.Review, human: Mapping[str, Any], stage: str) -> bool | None:
    """Whether `stage` holds a recorded human judgement — True, False, or None for "not trackable".

    A tick on a review screen has to mean something the repository can show afterwards — ticking a
    deliverable the moment it is *selected* records nothing except that a mouse moved.

    So completion is derived from the artefacts a stage actually produces: an answered challenge set,
    an answered decision set, a frozen review. The reading stages produce nothing, and None says so
    rather than claiming either that they were done or that they were skipped — "we did not measure"
    has to be a state you can name (plan §2.4).
    """
    if stage == "decision":
        return challenges_complete(review, human) and not unanswered_decisions(review, human)
    if stage == "freeze":
        return str(human.get("status", "not_started")) == "frozen"
    return None


# -- expertise routing (plan §14.9, E2E-05) -----------------------------------


def _expert_domains(review: models.Review) -> set[str]:
    """Domains a high/critical decision card requires — where a general reviewer is not enough."""
    domains: set[str] = set()
    for card in _machine_list(review, "decision_cards"):
        if models.risk_at_least(_risk(card), _EXPERT_RISK_FLOOR):
            required = card.get("requires_domains")
            if isinstance(required, list):
                domains.update(str(d) for d in required)
    return domains


def _declared_levels(human: Mapping[str, Any]) -> dict[str, str]:
    return {str(e.get("domain")): str(e.get("level")) for e in _human_list(human, "expertise")}


def _cards_requiring(review: models.Review, domain: str) -> set[str]:
    ids = set()
    for card in _machine_list(review, "decision_cards"):
        required = card.get("requires_domains")
        if isinstance(required, list) and domain in [str(d) for d in required]:
            ids.add(str(card.get("id")))
    return ids


def _domain_remediated(review: models.Review, human: Mapping[str, Any], domain: str) -> bool:
    """Whether a partial/unfamiliar domain has an expert, an experiment, or a scope reduction.

    Satisfied by a requested expert for the domain, or a remediating disposition on one of the
    decision cards that pulled the domain in. Behaviour removal maps to `reduce_scope`/`revise_*`.
    """
    for req in _human_list(human, "requested_experts"):
        if str(req.get("domain")) == domain:
            return True
    card_ids = _cards_requiring(review, domain)
    for disp in _human_list(human, "dispositions"):
        if str(disp.get("action")) in REMEDIATING_ACTIONS and str(disp.get("subject_id")) in card_ids:
            return True
    return False


def expertise_gaps(review: models.Review, human: Mapping[str, Any]) -> list[dict[str, str]]:
    """Domains where high/critical work outruns the reviewer's declared, unremediated expertise.

    An undeclared domain a high/critical card requires is a gap in itself: silence is not a
    familiarity claim, and the loop must not treat "did not say" as "familiar" (plan §14.9).
    """
    levels = _declared_levels(human)
    gaps = []
    for domain in sorted(_expert_domains(review)):
        level = levels.get(domain, "undeclared")
        if level == "familiar":
            continue
        if _domain_remediated(review, human, domain):
            continue
        gaps.append({"domain": domain, "level": level})
    return gaps


# -- review budget (plan §14.10, E2E-30) --------------------------------------


def _critical_claim_ids(review: models.Review) -> set[str]:
    ids: set[str] = set()
    for challenge in challenges(review):
        if _risk(challenge) == "critical":
            claim_ids = challenge.get("claim_ids")
            if isinstance(claim_ids, list):
                ids.update(str(c) for c in claim_ids)
    return ids


def _critical_modules(review: models.Review) -> int:
    critical = _critical_claim_ids(review)
    count = 0
    for module in _machine_list(review, "module_deltas"):
        invariants = module.get("changed_invariants")
        if isinstance(invariants, list) and any(str(c) in critical for c in invariants):
            count += 1
    return count


def _unresolved_low_medium_unknowns(review: models.Review, human: Mapping[str, Any]) -> int:
    """Low/medium non-blocking gaps with no human disposition — the ones a budget bounds."""
    disposed = {str(d.get("subject_id")) for d in _human_list(human, "dispositions")}
    count = 0
    for gap in _machine_list(review, "gaps"):
        risk = _risk(gap)
        if risk in ("low", "medium") and gap.get("blocking") is not True and str(gap.get("id")) not in disposed:
            count += 1
    return count


def _largest_partition_bytes(review: models.Review) -> int:
    """The biggest single diff partition this review covers, in bytes.

    The budget is per *partition*, not per change, so the measure is the max rather than the sum: a
    change split into readable pieces is within budget however many pieces there are, and one
    unreadable piece is over it however small the rest were. A coverage entry carrying no
    `analyzed_bytes` reads as 0, so a review written before the measure keeps the verdict it had.
    """
    return max((int(entry.get("analyzed_bytes", 0) or 0) for entry in review.coverage), default=0)


def budget_actuals(review: models.Review, human: Mapping[str, Any]) -> dict[str, int]:
    """The measured value for each budget line, derived from the review content (plan §14.10).

    `max_diff_bytes_per_partition` is measured from the coverage manifest, not assumed enforced
    upstream: `diff_facts` raises its `partitioned` flag past a *line* count and never measures
    bytes, so a constant actual here would make the one byte-denominated budget impossible to
    exceed by a change of any size — and "a blown budget splits the scope" (config.yaml,
    gate-workflow.md, review.schema.json) something that could never happen.
    """
    return {
        "max_critical_decisions": sum(1 for c in _machine_list(review, "decision_cards") if _risk(c) == "critical"),
        "max_critical_modules": _critical_modules(review),
        "max_human_statements": len(_machine_list(review, "statements")),
        "max_scenarios": len(_machine_list(review, "scenarios")),
        "max_unresolved_low_medium_unknowns": _unresolved_low_medium_unknowns(review, human),
        "max_diff_bytes_per_partition": _largest_partition_bytes(review),
    }


def recorded_limits(review: models.Review) -> dict[str, int]:
    """The ceilings this review was *generated* against (`machine.review_budget`).

    `review.py` merges `review_policy.budgets` from config.yaml into the snapshot it writes at
    generation time, and that snapshot is what the gate receipt binds. Reading it back here is what
    makes a configured budget reach the screen: recomputing against `DEFAULT_BUDGET` alone would
    block a project that deliberately raised a limit at the default anyway — and the review a human
    signed would disagree with the review they were shown.
    """
    limits: dict[str, int] = {}
    for row in _machine_list(review, "review_budget"):
        name, limit = str(row.get("name", "")), row.get("limit")
        if name in models.BUDGET_NAME_VALUES and isinstance(limit, int) and not isinstance(limit, bool):
            limits[name] = limit
    return limits


def budget_report(
    review: models.Review, human: Mapping[str, Any], limits: Mapping[str, int] | None = None
) -> list[dict[str, Any]]:
    """One row per budget: name, limit, measured actual, and whether it is exceeded."""
    ceilings = {**DEFAULT_BUDGET, **recorded_limits(review), **(limits or {})}
    actuals = budget_actuals(review, human)
    return [
        {"name": name, "limit": ceilings[name], "actual": actuals[name], "exceeded": actuals[name] > ceilings[name]}
        for name in models.BUDGET_NAMES
    ]


def scope_split_required(
    review: models.Review, human: Mapping[str, Any], limits: Mapping[str, int] | None = None
) -> list[str]:
    """The budgets that are blown — non-empty means the scope must be split, not the screen grown."""
    return [row["name"] for row in budget_report(review, human, limits) if row["exceeded"]]


# -- completion readiness (plan §21.5) ----------------------------------------


def completion_blockers(
    review: models.Review | None, human: Mapping[str, Any] | None = None, *, limits: Mapping[str, int] | None = None
) -> list[str]:
    """Every reason the human review cannot be frozen — the approve button's disabled reasons.

    Exhaustive rather than short-circuiting, like approve.readiness: handing the reviewer one
    blocker, then the next after they fix it, is the review friction plan §2.6 budgets against.
    A frozen human review is the precondition approve.readiness re-checks at the gate, so this is
    the same wall seen from the review side.
    """
    if review is None or not review.is_generated:
        return ["no machine review has been generated — run `rein review generate`"]
    human = human if human is not None else dict(review.human)
    blockers: list[str] = []

    if not review.coverage_sufficient:
        blockers.append("coverage is insufficient — extra-behaviour counts are undeterminable, not zero")

    unanswered = unanswered_challenges(review, human)
    if unanswered:
        blockers.append(f"unanswered challenges: {', '.join(unanswered)}")
    open_cf = open_counterfactuals(review, human)
    if open_cf:
        blockers.append(f"unresolved challenge mismatches (need a corrected model): {', '.join(open_cf)}")

    # The substantive judgement. Without it the challenge — a comprehension check — would be the
    # only mandatory answer, so a review could be frozen having answered a quiz about the change
    # while deciding nothing about it.
    undecided = unanswered_decisions(review, human)
    if undecided:
        blockers.append(f"unanswered high/critical decision cards: {', '.join(undecided)}")

    gaps = expertise_gaps(review, human)
    if gaps:
        blockers.append(
            "high/critical domains need an expert, an experiment, or a smaller scope: "
            + ", ".join(f"{g['domain']} ({g['level']})" for g in gaps)
        )

    blown = scope_split_required(review, human, limits)
    if blown:
        blockers.append(
            f"review budget exceeded — split the scope, do not grow the screen: {', '.join(blown)}. "
            "Reduce what this cycle claims through `/revise` and review the remainder in its own "
            "gate ④ round, or raise the limit in `review_policy.budgets` as a deliberate, recorded "
            "decision about how much one person can hold at once"
        )

    blocking_gaps = [str(g.get("id")) for g in _machine_list(review, "gaps") if g.get("blocking") is True]
    if blocking_gaps:
        blockers.append(f"blocking gaps: {', '.join(blocking_gaps)}")
    blocking_extras = [str(e.get("id")) for e in review.extra_behaviors if e.get("blocking") is True]
    if blocking_extras:
        blockers.append(f"blocking extra behaviours: {', '.join(blocking_extras)}")
    if review.blocking_security_findings:
        blockers.append(
            "blocking security findings: " + ", ".join(str(f.get("id")) for f in review.blocking_security_findings)
        )
    return blockers


def can_freeze(review: models.Review | None, human: Mapping[str, Any] | None = None) -> bool:
    return not completion_blockers(review, human)


# -- staleness / optimistic concurrency (plan §17.5, E2E-08/09) ---------------


def assert_machine_current(review: models.Review, expected_machine_digest: str) -> None:
    """Refuse a human write whose `machine_digest` no longer matches — 409, not a silent merge.

    The reviewer's answers are *about* a specific machine review. If it was regenerated between the
    screen loading and the write landing, the answers describe an artefact that is gone; merging
    them would attribute the reviewer's judgement to a review they never saw (plan §17.5, E2E-08).
    A pure machine-digest comparison is also why a human-only update never trips this on itself
    (E2E-09): answering a challenge changes `human`, never `machine`.

    An absent digest is refused rather than waved through: a caller that names no machine review
    has not established that it saw *this* one, and treating "no claim" as "the right claim" is
    the same fail-open the check exists to prevent.
    """
    current = review.machine_digest()
    if not expected_machine_digest:
        raise StaleReview(
            "no machine_digest was presented — an answer must name the machine review it is about, "
            "so that a review regenerated underneath it can be detected"
        )
    if current != expected_machine_digest:
        raise StaleReview(
            "the machine review changed since this screen was loaded — reload and answer against the "
            f"current review (expected {expected_machine_digest[:19]}…, now {current[:19]}…)"
        )


# -- human-half mutations (returned for store.Transaction to persist) ---------


def _human_base(human: Mapping[str, Any]) -> dict[str, Any]:
    """A mutable copy of the human half with its list fields materialised."""
    out: dict[str, Any] = dict(human)
    for key in (
        "expertise",
        "challenge_answers",
        "counterfactual_answers",
        "decisions",
        "dispositions",
        "requested_experts",
    ):
        value = out.get(key)
        out[key] = list(value) if isinstance(value, list) else []
    return out


def record_challenge_answer(
    review: models.Review,
    human: Mapping[str, Any],
    challenge_id: str,
    choice: str,
    *,
    confidence: str,
    rationale: str = "",
) -> dict[str, Any]:
    """Add a challenge answer. `answered_before_reveal` is stamped true because the API only ever
    reaches this before the reveal is served (plan §14.2); the schema pins it to that constant."""
    if challenge_id not in {str(c.get("id")) for c in challenges(review)}:
        raise ValueError(f"unknown challenge {challenge_id!r}")
    if challenge_id in answered_challenge_ids(human):
        raise ValueError(f"challenge {challenge_id!r} is already answered")
    if confidence not in models.CONFIDENCE_VALUES:
        raise ValueError(f"confidence must be one of {', '.join(sorted(models.CONFIDENCE_VALUES))}")
    out = _human_base(human)
    answer: dict[str, Any] = {
        "challenge_id": challenge_id,
        "choice": choice,
        "confidence": confidence,
        "answered_before_reveal": True,
    }
    if rationale.strip():
        answer["rationale"] = rationale
    out["challenge_answers"].append(answer)
    out["status"] = "in_progress"
    return out


def record_decision(
    review: models.Review,
    human: Mapping[str, Any],
    card_id: str,
    choice: str,
    *,
    confidence: str,
    reason: str = "",
) -> dict[str, Any]:
    """Answer one Decision Card — the judgement gate ④ actually asks the human for.

    Re-answering is allowed and replaces the previous answer, unlike a challenge: a challenge
    records what the reviewer thought *before* seeing the reveal, so a second attempt would falsify
    it, whereas a decision is a conclusion the reviewer is entitled to change while the review is
    still open. Both are refused once the review is frozen, by the caller's transaction.
    """
    card = next((c for c in _machine_list(review, "decision_cards") if str(c.get("id")) == card_id), None)
    if card is None:
        raise ValueError(f"unknown decision card {card_id!r}")
    options = {str(o.get("id")) for o in card.get("options", ()) or ()}
    if choice not in options:
        raise ValueError(f"choice {choice!r} is not an option of {card_id} ({', '.join(sorted(options))})")
    if confidence not in models.CONFIDENCE_VALUES:
        raise ValueError(f"confidence must be one of {', '.join(sorted(models.CONFIDENCE_VALUES))}")
    out = _human_base(human)
    entry: dict[str, Any] = {"card_id": card_id, "choice": choice, "confidence": confidence}
    if reason.strip():
        entry["reason"] = reason
    out["decisions"] = [d for d in out["decisions"] if str(d.get("card_id")) != card_id]
    out["decisions"].append(entry)
    out["status"] = "in_progress"
    return out


def answered_card_ids(human: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(str(d.get("card_id")) for d in _human_list(human, "decisions"))


def unanswered_decisions(review: models.Review, human: Mapping[str, Any], *, floor: str = "high") -> list[str]:
    """Decision cards at or above `floor` with no recorded answer — the judgement that must not lapse.

    Only high and critical cards block by default. A low-risk card is still worth answering, but
    making every one mandatory is how a forcing function turns into a formality: the reviewer learns
    to clear the list rather than to weigh it (plan §2.6).
    """
    answered = answered_card_ids(human)
    return [
        str(c.get("id"))
        for c in _machine_list(review, "decision_cards")
        if models.risk_at_least(_risk(c), floor) and str(c.get("id")) not in answered
    ]


def record_counterfactual(
    human: Mapping[str, Any], challenge_id: str, corrected_model: str, *, answer: str = ""
) -> dict[str, Any]:
    """Close a mismatch with the reviewer's corrected mental model (plan §14.3)."""
    if not corrected_model.strip():
        raise ValueError("a counterfactual needs a non-empty corrected_model")
    out = _human_base(human)
    entry: dict[str, Any] = {"challenge_id": challenge_id, "corrected_model": corrected_model}
    if answer.strip():
        entry["answer"] = answer
    out["counterfactual_answers"].append(entry)
    out["status"] = "in_progress"
    return out


def record_expertise(human: Mapping[str, Any], domain: str, level: str) -> dict[str, Any]:
    """Declare (or redeclare) familiarity with a domain."""
    if level not in models.EXPERTISE_LEVEL_VALUES:
        raise ValueError(f"level must be one of {', '.join(sorted(models.EXPERTISE_LEVEL_VALUES))}")
    out = _human_base(human)
    out["expertise"] = [e for e in out["expertise"] if str(e.get("domain")) != domain]
    out["expertise"].append({"domain": domain, "level": level})
    out["status"] = "in_progress"
    return out


def record_disposition(human: Mapping[str, Any], subject_id: str, action: str, *, note: str = "") -> dict[str, Any]:
    """Record what the reviewer decided to do about a mismatch or a gap (plan §14.3)."""
    if action not in models.DISPOSITION_VALUES:
        raise ValueError(f"action must be one of {', '.join(sorted(models.DISPOSITION_VALUES))}")
    out = _human_base(human)
    entry: dict[str, Any] = {"subject_id": subject_id, "action": action}
    if note.strip():
        entry["note"] = note
    out["dispositions"].append(entry)
    out["status"] = "in_progress"
    return out


def request_expert(
    human: Mapping[str, Any], domain: str, subject_ids: Sequence[str], *, reason: str = ""
) -> dict[str, Any]:
    """Route a domain to an expert — one of the ways an expertise gap is discharged (plan §14.9)."""
    if not subject_ids:
        raise ValueError("an expert request needs at least one subject id")
    out = _human_base(human)
    entry: dict[str, Any] = {"domain": domain, "subject_ids": list(subject_ids)}
    if reason.strip():
        entry["reason"] = reason
    out["requested_experts"].append(entry)
    out["status"] = "in_progress"
    return out


def freeze(
    review: models.Review, human: Mapping[str, Any], *, limits: Mapping[str, int] | None = None
) -> dict[str, Any]:
    """Move the human half to `frozen`, refusing if any completion blocker remains.

    Freezing is the precondition the gate approval is built on — so it must fail
    closed on the same wall approve.readiness re-checks, never on a subset of it.
    """
    blockers = completion_blockers(review, human, limits=limits)
    if blockers:
        raise ValueError("cannot freeze the human review:\n  - " + "\n  - ".join(blockers))
    out = _human_base(human)
    out["status"] = "frozen"
    return out
