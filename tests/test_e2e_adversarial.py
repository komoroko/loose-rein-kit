"""The adversarial scenario index (plan §31): one test per numbered attack the design must survive.

These are deliberately end-of-the-funnel checks — the detailed unit tests for each mechanism live in
that mechanism's own file; here each scenario is asserted once, at the boundary an attacker would push
on, so the list reads as "every attack in §31 has a test that fails if the defence regresses". The
scenarios needing real git/docker/ssh carry the `integration` marker; the rest are pure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from rein import (
    diff_facts,
    event_chain,
    human_review,
    models,
    policy_check,
    review_policy,
    strict_yaml,
)
from rein import repo as repo_mod
from tests._support import chain, make_config, make_state, seed_repo


def _review(machine: dict[str, Any], human: dict[str, Any] | None = None) -> models.Review:
    base = {
        "status": "generated",
        "binding": {
            "change_digest": "sha256:" + "a" * 64,
            "plan_digest": "sha256:" + "b" * 64,
            "toolchain_digest": "sha256:" + "c" * 64,
        },
        "coverage": [
            {
                "diff_digest": "sha256:" + "d" * 64,
                "analyzed_files": 1,
                "analyzed_bytes": 1024,
                "truncated": False,
                "coverage_status": "sufficient",
            }
        ],
        "actual_extraction": [],
        "claims": [],
    }
    base.update(machine)
    return models.Review({"machine": base, "human": human or {"status": "not_started"}})


# E2E-02: an AI silently adds `timeout=30` — the detector flags it, it is not a free default.
def test_e2e_02_ai_invented_default_is_a_signal() -> None:
    diff = "diff --git a/c.py b/c.py\n--- a/c.py\n+++ b/c.py\n@@ -1 +1 @@\n+    resp = get(url, timeout=30)\n"
    facts = diff_facts.analyze(diff)
    assert any(h.signal == "failure_policy" for h in facts.signals)


# E2E-05: critical work in an unfamiliar domain cannot close on a general reviewer's acceptance.
def test_e2e_05_unfamiliar_critical_domain_blocks() -> None:
    machine = {
        "decision_cards": [
            {
                "id": "DC-001",
                "question": "retry?",
                "risk": "critical",
                "options": [{"id": "A", "statement_id": "STMT-001"}, {"id": "B", "statement_id": "STMT-002"}],
                "requires_domains": ["idempotency"],
            }
        ]
    }
    human = {"status": "in_progress", "expertise": [{"domain": "idempotency", "level": "unfamiliar"}]}
    review = _review(machine, human)
    assert human_review.expertise_gaps(review, dict(review.human)) == [{"domain": "idempotency", "level": "unfamiliar"}]


_HIGH_CARD = {
    "id": "DC-001",
    "risk": "high",
    "subject_id": "C-001",
    "kind": "claim",
    "question": "C-001 is 'diverged'. What happens to it?",
    "options": [{"id": "A", "statement_id": "STMT-001"}, {"id": "B", "statement_id": "STMT-002"}],
    "evidence": {"expected": {"statement": "x"}, "expected_choice": "B"},
}


# E2E-08: a machine review regenerated under the reviewer refuses their next write.
def test_e2e_08_stale_machine_digest_is_refused() -> None:
    review = _review({})
    with pytest.raises(human_review.StaleReview):
        human_review.assert_machine_current(review, "sha256:" + "0" * 64)


# E2E-09: a human-only answer never makes the machine review stale.
def test_e2e_09_human_answer_does_not_stale_the_machine() -> None:
    review = _review({"decision_cards": [_HIGH_CARD]})
    before = review.machine_digest()
    human = human_review.record_challenge_answer(review, dict(review.human), "DC-001", "B", confidence="low")
    after = models.Review({"machine": dict(review.machine), "human": human})
    assert after.machine_digest() == before and after.human_digest() != review.human_digest()


# E2E-19: a high-risk card withholds its evidence until the reviewer records their own read.
def test_e2e_19_priming_defense() -> None:
    review = _review({"decision_cards": [_HIGH_CARD]})
    asked = human_review.next_challenge(review, dict(review.human))
    assert asked is not None and "evidence" not in asked
    assert human_review.reveal_for(review, "DC-001") == _HIGH_CARD["evidence"]

    answered = human_review.record_challenge_answer(review, dict(review.human), "DC-001", "B", confidence="high")
    assert human_review.next_challenge(review, answered) is None


# E2E-23: the strict loader refuses duplicate keys, merge keys, aliases, and deep nesting.
@pytest.mark.parametrize(
    "text",
    [
        "a: 1\na: 2\n",  # duplicate key
        "base: &b {x: 1}\nchild:\n  <<: *b\n",  # merge key + alias
        "a: &x [1]\nb: *x\n",  # alias
        "a:\n" + "".join(" " * i + "b:\n" for i in range(1, 80)),  # deep nesting
    ],
)
def test_e2e_23_yaml_parser_attacks_are_refused(text: str) -> None:
    with pytest.raises(strict_yaml.StrictParseError):
        strict_yaml.load_mapping(text, what="attack")


# E2E-24: a diff the detector cannot fully read makes coverage insufficient, never "0 extra".
def test_e2e_24_unsupported_diff_is_insufficient() -> None:
    facts = diff_facts.analyze("diff --git a/x.zig b/x.zig\n--- a/x.zig\n+++ b/x.zig\n@@ -1 +1 @@\n+const x = 1;\n")
    assert facts.coverage.coverage_status == "insufficient"


# E2E-26: two reviewers in the same independence group cannot certify a critical change.
def test_e2e_26_same_model_independence_is_rejected() -> None:
    independence = {"actual_extractor": {"group": "claude/opus"}, "comparator": {"group": "claude/opus"}}
    ok, message = review_policy.independence_ok(independence, "critical")
    assert not ok and "not independent" in message


# E2E-29: a wholesale re-hash of the log does not reproduce the chain root a receipt pinned.
def test_e2e_29_chain_rewrite_changes_the_root() -> None:
    original = chain("task_completed", "task_completed", "gate_approved")
    pinned_root = event_chain.chain_root(original)
    # An attacker drops the middle event and re-hashes into a consistent-looking chain.
    rewritten = chain("task_completed", "gate_approved")
    assert event_chain.chain_root(rewritten) != pinned_root


# E2E-30: a blown review budget requires a scope split, not a longer screen.
def test_e2e_30_review_budget_blocks() -> None:
    cards = [
        {
            "id": f"DC-{i:03d}",
            "question": "q",
            "risk": "critical",
            "options": [{"id": "A", "statement_id": "STMT-001"}, {"id": "B", "statement_id": "STMT-002"}],
        }
        for i in range(1, 7)
    ]
    review = _review({"decision_cards": cards})
    assert human_review.scope_split_required(review, dict(review.human)) == ["max_critical_decisions"]


# --- scenarios exercising the real tree (git) --------------------------------


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> repo_mod.Repo:
    seed_repo(tmp_path, state=make_state(project="p"), config=make_config())
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    return repo_mod.Repo(tmp_path)


# E2E-21: the base-side policy check catches a head that weakens the gate.
@pytest.mark.integration
def test_e2e_21_policy_ci_tamper_is_caught(git_repo: repo_mod.Repo) -> None:
    (git_repo.root / ".rein" / "config.yaml").write_text(
        "project:\n  name: p\ngates:\n  enforce_hook: false\n", encoding="utf-8"
    )
    _git(git_repo.root, "add", "-A")
    _git(git_repo.root, "commit", "-qm", "weaken")
    head = git_repo._git_rc("rev-parse", "HEAD")[1].strip()
    assert any("enforce_hook" in p for p in policy_check.check(git_repo, "0" * 40, head))


# E2E-22: a truncated audit chain is detected by the policy check.
@pytest.mark.integration
def test_e2e_22_event_chain_attack_is_detected(git_repo: repo_mod.Repo) -> None:
    event_chain.append_lines(git_repo.events, chain("task_completed", "task_completed"))
    lines = git_repo.events.read_text(encoding="utf-8").splitlines()
    git_repo.events.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")  # drop the first, dangle the link
    # Committed, because the check reads the head commit: on a pull request the checkout is the
    # merge commit, so verifying the working tree verifies a log nobody proposed.
    _git(git_repo.root, "add", "-A")
    _git(git_repo.root, "commit", "-qm", "truncate the chain")
    head = git_repo._git_rc("rev-parse", "HEAD")[1].strip()
    assert any("audit chain" in p for p in policy_check.check(git_repo, "0" * 40, head))
