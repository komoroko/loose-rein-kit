"""How a gate-④ reviewer stage is actually launched, and what the launching costs.

Everything above this file — the pipeline in `review`, the three stage validators — is about what
a review *is*. This is about getting a request to an agent CLI and an answer back: which CLI
answers for which role, the empty working directory it runs in, the one reading of the change two
stages branch rather than each pay for, and the ledger of what all of that cost.

Two boundaries hold it together:

- **A launch's result is a value.** `review_policy.Answer` carries the text *and* what the launch
  cost, so nothing has to reach around the call to find out. The cost used to travel in a mutable
  dict threaded down from the CLI and, separately, in a `ContextVar` so the stage cache could
  record which model answered — two channels for one value, one of them ambient.
- **The role is given, never inferred.** `StagedReviewers.for_role` is asked for the role the
  pipeline is running; the one callable that used to serve all three stages recovered the role by
  inspecting the request (`"expected_model" in request`), which made the request shapes part of
  the dispatch contract by accident.

The ledger lives here because this is what pays — including for the launches no single stage owns
(a shared reading's priming turn) and the ones that failed before returning anything. The pipeline
reads it; it never writes to it.
"""

from __future__ import annotations

import json
import tempfile
import threading
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from rein import actual_extraction, adapters, common, digests, faults, models, review_policy
from rein import repo as repo_mod
from rein import store as store_mod
from rein import usage as usage_mod


class TransportError(Exception):
    """The launching itself is impossible or incoherent — not something a reviewer said."""


#: How much of a failed adapter's output travels with the error. Its closing words are where a
#: CLI says what stopped it, and an error message is read on a terminal, not scrolled.


#: What the priming turn is told to answer. Content-free on purpose: whatever the model says in
#: that turn is in *both* branches' context afterwards, so it must carry no reading of the change.
_PRIME_ACK = "READY"

#: The stage-request key the reading lives under, and what replaces it in a branch.
_READING_KEY = "diff"


def shareable_reading(config: models.Config | None, roles: Sequence[str]) -> adapters.Adapter | None:
    """The adapter `roles` can share one reading through, or None when they cannot.

    Two conditions:

    - **The same launch.** Not "the same adapter name" — the argv these roles are actually launched
      with, model included. Two roles on different models do not share: a cache written by one
      model is not another's, and the reading would be paid for twice anyway.
    - **A CLI that can branch a session.** *Continuing* one — the second stage reading the first
      stage's answer — is exactly the correlated blindness this exists to avoid, so a CLI that can
      resume but not fork is not good enough (`Adapter.forkable`).

    The independence group is not consulted separately, because it no longer says anything the argv
    does not: it is derived from `<adapter>/<model>`, and both are in the argv. Two roles that share
    a launch share a group by construction.
    """
    records = [adapters.adapter_for_role(config, role) for role in roles]
    if len({adapters.launch_argv(config, role) for role in roles}) != 1 or not records[0].forkable:
        return None
    return records[0]


class SharedReading:
    """One reading of the change, primed once and **branched** for each stage that needs it.

    The extractor and the security reviewer are handed the same diff — up to `max_diff_bytes`, so
    up to half a megabyte — and were launched separately, each paying to read it in full. Measured
    on an 82 KB payload: two independent launches cost $0.2153, a priming turn plus two branches
    cost $0.1298. Repeated on a 58 KB payload with real request shapes: $0.196 against $0.126.

    **Serialising them into one session is not the answer**, which is why this branches rather than
    continues: the second stage would read the first stage's conclusions and inherit its frame, and
    catching what the extraction's frame missed is the whole value of the security review. A fork
    shares everything read before the fork and none of what any branch then concludes.

    Three facts this rests on, all measured rather than assumed — the alternatives look identical
    from the outside and are not:

    - Sending the same prefix to two *separate* one-shot launches does **not** hit the cache. The
      session is the only mechanism that works — two branches read 51,969 tokens from cache where
      two separate launches with an identical prefix read 16,737 and paid to write the rest again.
    - Two branches resumed **in parallel** both hit the cache, so the pipeline keeps the
      concurrency it has.
    - A branch sent a request the size of a real one (a 4 KB contract beside the pointer) hits it
      too, immediately after the priming turn, with no settling time.

    **The saving is typical, not guaranteed.** One run out of four measured had the first branch
    miss and pay to write the prefix again; the second branch then read what *it* had written, and
    the round still came out at or below two independent launches. The floor is what happens if the
    cache never serves at all — three full reads instead of two — so this is a bet on a mechanism
    that was observed working, not a guarantee. It is priced accordingly: nothing downstream
    depends on the hit, only the bill does.

    The priming turn is lazy — the first stage to ask creates it, the other waits on the lock —
    because nothing here knows in advance how many stages will actually launch. When one stage is
    served from `review_cache` and the other is not, this pays a priming turn for a single branch:
    about 7% more than launching it alone, against 40% less whenever both run, which is the common
    case. A channel for telling the transport what the cache is about to do would cost more than
    the case is worth.
    """

    def __init__(
        self,
        repo: repo_mod.Repo,
        record: adapters.Adapter,
        *,
        argv: Sequence[str] | None = None,
        timeout: float,
        ledger: usage_mod.Ledger,
    ) -> None:
        self._repo = repo
        self._record = record
        #: The priming turn must run the same launch the branches do — a cache written by one
        #: model is not another model's, so a prime on the CLI default under branches pinned to a
        #: model pays for the reading and serves nobody. Measured: the branch that followed such a
        #: prime wrote the whole prefix again.
        self._argv = tuple(argv) if argv is not None else record.launch_argv()
        self._timeout = timeout
        self._ledger = ledger
        self._lock = threading.Lock()
        self._session = ""
        self._digest = ""

    def branch_flags(self, request: Mapping[str, Any]) -> tuple[str, ...]:
        """The flags that put this request on its own branch of the shared reading.

        Empty for a request that carries no reading — the comparator's, which is handed the Actual
        rather than the code and has nothing to share.
        """
        if _READING_KEY not in request:
            return ()
        session = self._prime(request)
        return (*self._record.resume_flags, session, *self._record.fork_flags)

    def without_the_reading(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """`request` with the reading replaced by a pointer at the turn that carries it.

        Only this one key moves. `deterministic_facts.context` and `.files` are shared too, but the
        diff is ~98% of the payload, and a request that exists in two shapes is a request that will
        drift — `build_request` keeps producing exactly one.
        """
        if _READING_KEY not in request:
            return request
        self._prime(request)
        return {
            **request,
            _READING_KEY: {"in_previous_message": True, "digest": self._digest},
        }

    def _prime(self, request: Mapping[str, Any]) -> str:
        """Create the shared session, once, and return its id.

        The reading is checked against the primed one rather than assumed to match: two stages
        branching one session must be branching it about the same bytes, and a mismatch is a bug in
        the pipeline above rather than something to paper over by sending the diff again.
        """
        digest = digests.of_bytes(str(request.get(_READING_KEY, "")).encode("utf-8"))
        with self._lock:
            if self._session:
                if digest != self._digest:
                    raise TransportError(
                        "two stages were about to branch one reading of different changes "
                        f"({self._digest} vs {digest}) — the pipeline moved underneath the review"
                    )
                return self._session
            session = str(uuid.uuid4())
            # The instruction and the reading, and deliberately nothing else. The two shas used
            # to sit here, between them: duplicated out of a request that keeps them anyway
            # (`without_the_reading` moves only the reading), and volatile — a 40-character field
            # that changes on every commit, in front of the largest thing in the payload, in the
            # one turn whose entire purpose is to be a cache prefix the branches hit.
            payload = {
                "instruction": (
                    "Read the change below. Do not analyse it yet and do not describe it: a "
                    f"question about it follows in the next message. Reply with exactly {_PRIME_ACK}."
                ),
                _READING_KEY: request.get(_READING_KEY, ""),
            }
            # The same guard the extractor's own request gets. This is a new path into the
            # extractor's context, and a new path without the guard is how priming comes back.
            actual_extraction.assert_blind(payload)
            self._launch(session, payload)
            self._session, self._digest = session, digest
            return session

    def _launch(self, session: str, payload: Mapping[str, Any]) -> None:
        """Send the priming turn. Its failure is the review's failure, not a reason to fall back.

        There is deliberately no "prime failed, launch the stages separately instead" path: that
        would hide a broken adapter behind a bill twice the size, and `--supervise` already waits
        out the failures that time alone fixes.
        """
        argv = list(self._argv) + [*self._record.session_flags, session]
        with tempfile.TemporaryDirectory(prefix="rein-reading-", ignore_cleanup_errors=True) as elsewhere:
            rc, out = common.run(
                argv, cwd=elsewhere, timeout=self._timeout or None, input_text=json.dumps(payload, ensure_ascii=False)
            )
        if rc != 0:
            self._ledger.add(_SHARED_READING_ROLE, usage_mod.Usage.unavailable())
            said = faults.said(out)
            raise review_policy.AdapterFailure(
                f"the shared reading could not be primed — the adapter exited {rc}"
                + (f", saying:\n{said}" if said else " and said nothing"),
                rc=rc,
                output=out,
            )
        try:
            said, spent = self._record.read_output(out)
        except usage_mod.AdapterEnvelopeError as exc:
            self._ledger.add(_SHARED_READING_ROLE, usage_mod.Usage.unavailable())
            raise review_policy.AdapterFailure(
                f"the shared reading could not be primed — the adapter reported a failed run: {exc}",
                rc=0,
                output=out,
            ) from None
        self._ledger.add(_SHARED_READING_ROLE, spent)
        # The acknowledgement is the whole contract of this turn, and it was being thrown away.
        # Whatever the model says here is in *both* branches' context afterwards, so a model that
        # answered by analysing the diff instead of acking would hand the extractor and the
        # security reviewer one shared reading of it — the correlated blindness this class forks
        # rather than continues in order to avoid, arriving through the door it opened.
        # `actual_extraction.assert_blind` cannot see it: it guards the payload, and this is the
        # answer.
        if said.strip() != _PRIME_ACK:
            raise TransportError(
                f"the shared reading was primed and the adapter did not acknowledge it: expected "
                f"exactly {_PRIME_ACK!r}, got {said.strip()[:200]!r}. Whatever it said instead is "
                "now in both stages' context, so this reading cannot be branched"
            )


#: The two stages handed the same reading of the change. The comparator is deliberately absent: it
#: is given the Actual and the Expected, never the code, so it has no reading to share — and §12.4
#: forbids it sharing one with the extractor in any case.
_READING_ROLES: tuple[str, ...] = ("actual_extractor", "security_reviewer")

#: The priming turn belongs to no stage — it is what both of them read — so it is counted under a
#: name of its own. Folding it into either role would make that role's cost a fiction.
_SHARED_READING_ROLE = "shared_reading"

#: Every role the gate-④ pipeline launches, each with its own adapter (§12.4) — the roles of the
#: one stage→role map, so a stage the pipeline asks for always has a launcher here.
_STAGE_ROLES: tuple[str, ...] = tuple(review_policy.STAGE_ROLE.values())


def shares_reading(config: models.Config | None) -> bool:
    """Will the extractor and the security reviewer branch one reading of the change?

    The question the execution plan asks before anything launches. `LaunchRefused` is left to the
    caller: for a run whose reviewers were injected it means "cannot say", not "no".
    """
    return shareable_reading(config, _READING_ROLES) is not None


def _adapter_reviewer(
    repo: repo_mod.Repo,
    role: str,
    *,
    config: models.Config | None = None,
    ledger: usage_mod.Ledger,
    reading: SharedReading | None = None,
) -> review_policy.Reviewer:
    """A production reviewer that hands the request as JSON to the adapter configured for `role`.

    Kept small on purpose: the request goes to the adapter on stdin and the adapter answers with
    the single JSON document the stage validators parse. Every stage revalidates the output, so
    this is a transport, not a trust boundary.

    The request goes on stdin — a whole diff as an argv element hits E2BIG on a large change.

    The launch takes `execution.agent_timeout_sec`, the same knob the build loop launches under,
    rather than the fifteen minutes that used to be written here. Fifteen minutes was not a
    judgement about reviewing; it was a number, and it was not a comfortable one — field runs of
    this pipeline came in at 6m27s and 6m40s, so a change a little larger than that cycle's would
    have crossed it, thrown the whole launch away, and been re-run from cold. The knob defaults to
    no limit for the reason its own docstring gives, and Ctrl-C is what stops a launch that really
    is stuck.

    **It runs in an empty directory, not in the repository.** This transport passed no `cwd`, so
    every stage inherited rein's — the repository root. An agent CLI reads its working directory:
    the root is where `AGENTS.md` explains the Expected Model and `.rein/plan.yaml` *is* the
    Expected Model, both a `git show` away from the one stage whose whole value is that it has
    never seen them. `actual_extraction.assert_blind` guards the payload and could never have
    caught this, because the priming did not travel in the payload.

    Cutting the directory only works because the request now carries what the answer needs: the
    stage contract (`<stage>.contract`) instead of whatever instructions the CLI picked up from a
    project, and `deterministic_facts.files` instead of the `git rev-parse` a reviewer used to
    have to run to anchor anything. What the launch can still read is the user's own global CLI
    configuration, which is theirs and not this repository's to remove.
    """

    if config is None:
        config = store_mod.Store(repo).read_config()
    record = adapters.adapter_for_role(config, role)
    role_argv = adapters.launch_argv(config, role)
    timeout = float(config.agent_timeout_sec) if config is not None else 0.0

    def call(request: Mapping[str, Any]) -> review_policy.Answer:
        argv = list(role_argv) + list(reading.branch_flags(request) if reading else ())
        payload = json.dumps(reading.without_the_reading(request) if reading else request, ensure_ascii=False)
        # `ignore_cleanup_errors` because the answer is already in hand by then: an agent CLI that
        # left something undeletable behind must not turn a finished review into a traceback.
        with tempfile.TemporaryDirectory(prefix="rein-review-", ignore_cleanup_errors=True) as elsewhere:
            rc, out = common.run(argv, cwd=elsewhere, timeout=timeout or None, input_text=payload)
        if rc != 0:
            ledger.add(role, usage_mod.Usage.unavailable())
            # What the adapter said, not merely that it stopped. `common.run` merges stderr into
            # `out`, so the reason was in hand and thrown away: a field report of three identical
            # `exited 1` failures was diagnosable only by wrapping the CLI in a logging shim, and
            # the message behind them — "Prompt is too long" — named its own cause exactly.
            said = faults.said(out)
            raise review_policy.AdapterFailure(
                f"the {role} adapter exited {rc}" + (f", saying:\n{said}" if said else " and said nothing"),
                rc=rc,
                output=out,
            )
        try:
            answer, spent = record.read_output(out)
        except usage_mod.AdapterEnvelopeError as exc:
            # A CLI can report a failed run on a process that exited 0. Without this the failure
            # travels on to the stage validator and is reported as a malformed reviewer answer —
            # blaming the reviewer for something the launch said about itself.
            ledger.add(role, usage_mod.Usage.unavailable())
            raise review_policy.AdapterFailure(
                f"the {role} adapter reported a failed run: {exc}", rc=0, output=out
            ) from None
        ledger.add(role, spent)
        return review_policy.Answer(answer, spent)

    return call


class StagedReviewers:
    """The reviewer for each gate-④ role, and what launching them has cost so far.

    Each stage gets its own launch of its own configured adapter: one callable serving every stage
    would mean the same session answers as the Actual Extractor and as the Comparator, the
    independence violation §12.4 exists to prevent. The pipeline names the role it wants; this
    used to be one callable that recovered the role by inspecting the request shape, which made
    those shapes part of the dispatch contract without anything saying so.
    """

    def __init__(self, repo: repo_mod.Repo, *, config: models.Config | None = None) -> None:
        if config is None:
            config = store_mod.Store(repo).read_config()
        self._ledger = usage_mod.Ledger()
        # The extractor and the security reviewer read the same diff. When they can share one
        # reading without sharing a conclusion, they do; otherwise `shareable_reading` says no and
        # each is launched exactly as before.
        shared = shareable_reading(config, _READING_ROLES)
        reading = (
            SharedReading(
                repo,
                shared,
                argv=adapters.launch_argv(config, _READING_ROLES[0]),
                timeout=float(config.agent_timeout_sec) if config is not None else 0.0,
                ledger=self._ledger,
            )
            if shared is not None
            else None
        )
        self._by_role = {
            role: _adapter_reviewer(
                repo,
                role,
                config=config,
                ledger=self._ledger,
                reading=reading if role in _READING_ROLES else None,
            )
            for role in _STAGE_ROLES
        }

    def for_role(self, role: str) -> review_policy.Reviewer:
        return self._by_role[role]

    def spend(self) -> dict[str, usage_mod.Usage]:
        return self._ledger.totals()

    def spend_summary(self) -> str:
        """What this generation cost, by stage, worst first. Empty when nothing was launched.

        Token counts rather than bytes on stdin. The old measure could not see the system prompt,
        the CLI's own project instructions, or the cache — a probe of a one-word prompt came back
        with 10 input tokens and 20,956 cached ones — so it answered "what did rein send", never
        "what did this cost". A stage whose adapter reports nothing is named as unmeasured
        (`usage.summarize`), because a zero would read as free.

        What stays outside this number: the host's global CLI configuration is part of every
        launch's input and nothing here can itemize it. Its *size* is visible in the cache and
        input counts, which is as far as an honest measurement goes.
        """
        return usage_mod.summarize(self._ledger.totals(), what="review")
