"""Tests for doctor.py — the read-only diagnosis of everything the guarantees rest on.

Two rules make the output trustworthy, and both are asserted here.

**It never reports "not measured" as "fine".** An insufficient coverage manifest is a FAIL, not
a zero; an approved gate with no receipt is a FAIL, not a green board.

**It never repairs anything.** Several of the things it inspects — an approval, an audit
record — must only ever change by a deliberate human action, and a doctor that fixes what it
finds is a doctor whose findings nobody reads.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from rein import adapters, common, dag_trace, doctor, models, upstream
from rein import lock as lock_mod
from rein import repo as repo_mod
from tests._support import (
    DEMO_CYCLE,
    SANDBOXED_PROFILES,
    chain,
    make_claim,
    make_config,
    make_plan,
    make_review,
    make_state,
    seed_repo,
)


def findings(repo: repo_mod.Repo) -> dict[str, list[doctor.Finding]]:
    """run_checks grouped by area, for readable assertions."""
    grouped: dict[str, list[doctor.Finding]] = {}
    for finding in doctor.run_checks(repo):
        grouped.setdefault(finding.area, []).append(finding)
    return grouped


def levels(items: list[doctor.Finding], substring: str) -> list[str]:
    return [f.level for f in items if substring in f.message]


def healthy(tmp_path: Path, **kwargs: object) -> repo_mod.Repo:
    """A repo that should produce no FAIL: sandboxed profiles, docs the trace can read."""
    kwargs.setdefault("config", make_config(profiles=SANDBOXED_PROFILES))
    kwargs.setdefault("docs", True)
    seed_repo(tmp_path, **kwargs)  # type: ignore[arg-type]
    (tmp_path / "docs" / "10-requirements.md").write_text("### R-1: title\n", encoding="utf-8")
    (tmp_path / "docs" / "20-design.md").write_text("### R-1 → design\ncovered.\n", encoding="utf-8")
    return repo_mod.Repo(tmp_path)


# --- format -------------------------------------------------------------------


def test_missing_ssot_documents_are_named(tmp_path: Path) -> None:
    seed_repo(tmp_path, plan=None, review=None)
    assert any(
        "plan.yaml" in f.message and "review.yaml" in f.message for f in doctor.check_layout(repo_mod.Repo(tmp_path))
    )


def test_an_invalid_document_fails_with_its_validation_errors(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    (tmp_path / ".rein" / "plan.yaml").write_text("cycle: {}\nclaims: []\n", encoding="utf-8")
    results, _ = doctor.check_documents(repo_mod.Repo(tmp_path))
    assert any(f.level == "FAIL" and "plan.yaml" in f.message for f in results)


def test_a_healthy_repo_validates_all_four_documents(tmp_path: Path) -> None:
    repo = healthy(tmp_path)
    _, loaded = doctor.check_documents(repo)
    assert set(loaded) == {"config", "state", "plan", "review"}


def test_the_lock_format_is_reported(tmp_path: Path) -> None:
    repo = healthy(tmp_path)
    assert any("rein-grounded-v1" in f.message for f in doctor.check_lock(repo))


def test_a_lock_without_a_format_key_fails(tmp_path: Path) -> None:
    seed_repo(tmp_path, lock=False)
    (tmp_path / ".rein" / "rein.lock").write_text("version: 1\n", encoding="utf-8")
    results = doctor.check_lock(repo_mod.Repo(tmp_path))
    assert results[0].level == "FAIL"
    assert "is in format None" in results[0].message


def _place_claude_surface(root: Path) -> int:
    """Write the claude integration's real destination files, as a clone of the template would."""
    from rein import install

    written = 0
    for rel, blob in install._dest_map(install.INTEGRATIONS["claude"].files).items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        written += 1
    return written


def test_an_absent_integration_is_not_a_finding(tmp_path: Path) -> None:
    """The surfaces are opt-in, so their absence is a choice, not a defect."""
    seed_repo(tmp_path)
    assert doctor.check_integrations(repo_mod.Repo(tmp_path), models.Config(make_config())) == []


def test_the_templates_own_dogfood_copies_are_reported_as_such(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    _place_claude_surface(tmp_path)
    results = doctor.check_integrations(repo_mod.Repo(tmp_path), models.Config(make_config(template_mode=True)))
    assert [f.level for f in results] == ["INFO"]
    assert "dogfood copies" in results[0].message


def test_copied_surfaces_in_a_product_warn_that_upgrade_will_never_see_them(tmp_path: Path) -> None:
    """Cloning the template and running `rein init` produces exactly this state: working
    .claude files that the lock does not record, so `upgrade` leaves them at the copied release
    forever. The same empty record is correct in the template and a defect here."""
    seed_repo(tmp_path)
    _place_claude_surface(tmp_path)
    results = doctor.check_integrations(repo_mod.Repo(tmp_path), models.Config(make_config(template_mode=False)))
    assert [f.level for f in results] == ["WARN"]
    assert "copied, not installed" in results[0].message
    assert "rein install" in results[0].message


def _materialized_lock(tmp_path: Path, files: dict[str, str]) -> None:
    from rein import lock as lock_mod

    lock_path = tmp_path / ".rein" / "rein.lock"
    data = lock_mod.read(lock_path) or {}
    prompts = data.get("prompts")
    data["prompts"] = {**(prompts if isinstance(prompts, dict) else {}), "files": files}
    lock_mod.write(lock_path, data)


def test_a_lock_hash_the_payload_no_longer_has_is_reported(tmp_path: Path) -> None:
    """The record is what `_plan` reads to tell a pristine file from a locally modified one, so a
    stale entry turns the *next* release's update of that file into a silent "locally modified —
    kept". Three entries were already stale on main when this check was written, and nothing looked
    at them: check_materialized only ever compared disk against the payload."""
    from rein import install

    seed_repo(tmp_path)
    repo = repo_mod.Repo(tmp_path)
    assert install.sync(repo) == 0  # the record only exists once something materialized the payload
    recorded = {k.removeprefix(".rein/"): v for k, v in install.materialized_record(repo).items()}
    assert recorded, "seed_repo is expected to leave a populated materialized record"
    victim = "schema/event.schema.json"
    _materialized_lock(tmp_path, {**recorded, victim: "sha256:" + "0" * 64})

    results = doctor.check_materialized(repo)
    stale = [f for f in results if "stale hash" in f.message]
    assert [f.level for f in stale] == ["WARN"]
    assert victim in stale[0].message and "rein sync" in stale[0].message
    # The disk copy is untouched, so the long-standing disk-vs-payload question still answers PASS.
    assert any(f.level == "PASS" and "match the packaged payload" in f.message for f in results)


def test_a_materialized_file_no_release_ships_is_reported(tmp_path: Path) -> None:
    from rein import install

    seed_repo(tmp_path)
    repo = repo_mod.Repo(tmp_path)
    assert install.sync(repo) == 0  # the record only exists once something materialized the payload
    recorded = {k.removeprefix(".rein/"): v for k, v in install.materialized_record(repo).items()}
    _materialized_lock(tmp_path, {**recorded, "prompts/commands/retired.md": "sha256:" + "0" * 64})

    orphans = [f for f in doctor.check_materialized(repo) if "no longer shipped" in f.message]
    assert [f.level for f in orphans] == ["WARN"]
    assert "prompts/commands/retired.md" in orphans[0].message


def _record_integration(tmp_path: Path, version: str) -> None:
    from rein import lock as lock_mod

    lock_path = tmp_path / ".rein" / "rein.lock"
    data = lock_mod.read(lock_path) or {}
    data["integrations"] = {"claude": {"version": version, "files": {}}}
    lock_mod.write(lock_path, data)


def test_a_recorded_integration_passes(tmp_path: Path) -> None:
    import rein

    seed_repo(tmp_path)
    _place_claude_surface(tmp_path)
    _record_integration(tmp_path, rein.__version__)
    results = doctor.check_integrations(repo_mod.Repo(tmp_path), models.Config(make_config(template_mode=False)))
    assert [f.level for f in results] == ["PASS"]
    assert "recorded in the lock" in results[0].message


def test_a_surface_an_older_release_wrote_is_not_reported_as_healthy(tmp_path: Path) -> None:
    """`stale_integrations` says in its own docstring that sync *and* doctor surface this skew, and
    only sync ever called it. `sync` refreshes the shared artifacts and never the per-agent
    wrappers, so a repository could read new prompts through wrappers an older rein wrote and come
    back all PASS."""
    seed_repo(tmp_path)
    _place_claude_surface(tmp_path)
    _record_integration(tmp_path, "0.1.2")
    results = doctor.check_integrations(repo_mod.Repo(tmp_path), models.Config(make_config(template_mode=False)))
    assert [f.level for f in results] == ["WARN"]
    assert "written by rein 0.1.2" in results[0].message
    assert "rein install claude" in results[0].message


# --- gate rule 2: nothing may pre-authorize a gate-opening verb ----------------


@pytest.mark.parametrize(
    "entry,expected",
    [
        ("Bash(rein approve:*)", ["rein approve"]),
        ("Bash(rein approve build)", ["rein approve"]),
        ("Bash(rein cycle-close:*)", ["rein cycle-close"]),
        # An outward-facing verb is stopped whole, not per flag: prefix matching runs both ways,
        # so the bare verb catches `--push` and `--ready` alike.
        ("Bash(rein pr-stack:*)", ["rein pr-stack"]),
        ("Bash(rein pr-stack --push)", ["rein pr-stack"]),
        # Broader than the verb, so it carries every one of them with it.
        ("Bash(rein:*)", ["rein approve", "rein cycle-close", "rein pr-stack"]),
        # The neighbouring verb that only assembles a body and never reaches the network.
        ("Bash(rein pr-draft:*)", []),
        # Neighbours that must not be mistaken for it.
        ("Bash(rein start:*)", []),
        ("Bash(echo rein approve)", []),
        ("Read", []),
    ],
)
def test_which_permission_entries_reach_a_gate_opening_verb(entry: str, expected: list[str]) -> None:
    assert doctor.preauthorized_verbs(entry) == expected


def test_a_settings_file_with_no_gate_verb_passes(tmp_path: Path) -> None:
    seed_repo(tmp_path, settings='{"permissions": {"allow": ["Bash(rein start:*)"]}}')
    results = doctor.check_preauthorization(repo_mod.Repo(tmp_path))
    assert [f.level for f in results] == ["PASS"]


def test_pre_authorizing_approve_is_a_fail(tmp_path: Path) -> None:
    seed_repo(tmp_path, settings='{"permissions": {"allow": ["Bash(rein approve:*)"]}}')
    results = doctor.check_preauthorization(repo_mod.Repo(tmp_path))
    assert [f.level for f in results] == ["FAIL"]
    assert "gate rule 2" in results[0].message


def test_the_gitignored_local_settings_file_is_checked_too(tmp_path: Path) -> None:
    """The one file no diff, no review and no template-lint run can see — which is precisely
    why an entry that opens gates would be added there rather than to the committed file."""
    seed_repo(tmp_path, settings='{"permissions": {"allow": []}}')
    (tmp_path / ".claude" / "settings.local.json").write_text(
        '{"permissions": {"allow": ["Bash(rein cycle-close:*)"]}}', encoding="utf-8"
    )
    results = doctor.check_preauthorization(repo_mod.Repo(tmp_path))
    assert [f.level for f in results] == ["FAIL"]
    assert "settings.local.json" in results[0].message


def test_no_settings_file_produces_no_claim_either_way(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    assert doctor.check_preauthorization(repo_mod.Repo(tmp_path)) == []


def test_unparseable_settings_warn_rather_than_pass(tmp_path: Path) -> None:
    """Reporting PASS for a file it could not read would be the one failure mode this module's
    docstring forbids: "not measured" rendered as "fine"."""
    seed_repo(tmp_path, settings="{not json")
    results = doctor.check_preauthorization(repo_mod.Repo(tmp_path))
    assert [f.level for f in results] == ["WARN"]


# --- gate receipts --------------------------------------------------------------


def test_no_gate_approved_yet_is_info(tmp_path: Path) -> None:
    state = models.State(make_state(gates=dict.fromkeys(models.GATE_ORDER, "pending")))
    results = doctor.check_receipts(state)
    assert [f.level for f in results] == ["INFO"]


def test_an_approved_gate_with_a_full_receipt_passes(tmp_path: Path) -> None:
    state = models.State(make_state())  # approved through tasks, each with a schema-valid receipt
    results = doctor.check_receipts(state)
    assert all(f.level == "PASS" for f in results)
    assert any("GA-REQUIREMENTS-" in f.message for f in results)


def test_an_approved_gate_with_no_approval_id_fails() -> None:
    """`check_receipts` is a read-only re-check, not a re-run of schema validation — it must
    catch a broken receipt even when nothing upstream of it did."""
    raw = make_state()
    raw["gates"]["requirements"]["receipt"]["approval_id"] = ""
    results = doctor.check_receipts(models.State(raw))
    req = [f for f in results if "requirements" in f.message]
    assert req and req[0].level == "FAIL" and "no approval id" in req[0].message


def test_an_approved_gate_missing_a_bound_digest_fails() -> None:
    """`check_receipts` reads the receipt directly rather than trusting the schema alone — a
    document that was hand-edited around the schema (or read from an older format) must still
    be caught here."""
    raw = make_state()
    del raw["gates"]["requirements"]["receipt"]["attested_chain_root"]
    results = doctor.check_receipts(models.State(raw))
    req = [f for f in results if "requirements" in f.message]
    assert req and req[0].level == "FAIL"
    assert "missing" in req[0].message


# --- freeze drift -----------------------------------------------------------------
#
# `check_receipts` answers "does this receipt bind anything". These answer "does what it bound
# still exist" — a receipt can name every required digest and describe a document that has since
# been edited, and for a long time nothing anywhere said so.


def _repinned(profiles: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
    """The same profiles with every image pinned to a different digest — a rebuild, nothing else."""
    return {
        name: {**body, "image": str(body["image"]).replace("0" * 64, "1" * 64)} if "image" in body else body
        for name, body in profiles.items()
    }


def _frozen(plan_doc: dict[str, object], config_doc: dict[str, object], **overrides: object) -> models.State:
    """A state whose freeze record matches the two documents beside it."""
    raw = make_state(plan_status="frozen")
    raw["plan"] = {
        "status": "frozen",
        "digest": models.Plan(plan_doc).digest(),
        "config_digest": models.Config(config_doc).frozen_digest(),
        "environment_digest": models.Config(config_doc).environment_digest(),
        **overrides,
    }
    for gate in ("requirements", "design", "tasks"):
        receipt = raw["gates"][gate]["receipt"]
        receipt["plan_digest"] = raw["plan"]["digest"]
        receipt["config_digest"] = raw["plan"]["config_digest"]
    return models.State(raw)


def test_a_matching_freeze_passes() -> None:
    plan_doc, config_doc = make_plan(), make_config()
    results = doctor.check_freeze_drift(_frozen(plan_doc, config_doc), models.Plan(plan_doc), models.Config(config_doc))
    assert [f.level for f in results] == ["PASS", "PASS", "PASS"]


def test_a_rebuilt_image_is_reported_and_never_failed() -> None:
    """The pin is deliberately outside `frozen_digest`, so a rebuilt sandbox image no longer costs
    a rollback of a plan nothing changed. What it must not do is pass in silence: gate ④ approves
    over evidence produced here, and the freeze recorded a different environment."""
    plan_doc, config_doc = make_plan(), make_config(profiles=SANDBOXED_PROFILES)
    repinned = make_config(profiles=_repinned(SANDBOXED_PROFILES))
    results = doctor.check_freeze_drift(_frozen(plan_doc, config_doc), models.Plan(plan_doc), models.Config(repinned))
    assert [f.level for f in results] == ["PASS", "PASS", "INFO"]
    assert "the environment has changed" in results[-1].message
    assert not [f for f in results if f.level == "FAIL"]


def test_opening_a_sandbox_still_breaks_the_freeze() -> None:
    """Only the pin moved out. `kind`, `network_profile` and `mount_repo` are decisions, and a
    decision that widens what may happen still needs the human who approved the narrower one."""
    plan_doc, config_doc = make_plan(), make_config(profiles=SANDBOXED_PROFILES)
    opened = {name: {**body, "kind": "host"} for name, body in SANDBOXED_PROFILES.items()}
    results = doctor.check_freeze_drift(
        _frozen(plan_doc, config_doc), models.Plan(plan_doc), models.Config(make_config(profiles=opened))
    )
    assert [f.level for f in results if f.level == "FAIL"]


def test_a_config_edited_after_the_freeze_is_a_fail() -> None:
    """The reported case: pinning a sandbox digest and adding a `guard.paths` entry after gate ③.
    `rein doctor` kept printing `0 FAIL` against a config nobody had approved."""
    plan_doc, config_doc = make_plan(), make_config()
    state = _frozen(plan_doc, config_doc)
    edited = models.Config(make_config(max_parallel=9))
    results = doctor.check_freeze_drift(state, models.Plan(plan_doc), edited)
    fails = [f for f in results if f.level == "FAIL"]
    assert len(fails) == 1
    assert "config.yaml has changed since gate 3 froze it" in fails[0].message


def test_a_plan_edited_after_the_freeze_is_a_fail() -> None:
    plan_doc, config_doc = make_plan(), make_config()
    state = _frozen(plan_doc, config_doc)
    edited = models.Plan(make_plan(claims=[make_claim("C-001"), make_claim("C-002", requirement_ids=["R-2"])]))
    results = doctor.check_freeze_drift(state, edited, models.Config(config_doc))
    assert [f.level for f in results if f.level == "FAIL"]
    assert any("plan.yaml has changed" in f.message for f in results)


def test_a_post_freeze_receipt_naming_another_digest_is_a_fail() -> None:
    """The artifact matches the freeze, but gate ③'s receipt names something else — so the
    approval was taken against a document other than the one now frozen."""
    plan_doc, config_doc = make_plan(), make_config()
    state = _frozen(plan_doc, config_doc)
    raw = dict(state.raw)
    raw["gates"]["tasks"]["receipt"]["config_digest"] = "sha256:" + "0" * 64
    results = doctor.check_freeze_drift(models.State(raw), models.Plan(plan_doc), models.Config(config_doc))
    fails = [f for f in results if f.level == "FAIL"]
    assert len(fails) == 1
    assert "gate 'tasks' receipt" in fails[0].message and "not the one the freeze records" in fails[0].message


def test_the_pre_freeze_gates_are_not_judged_against_the_freeze() -> None:
    """Gates ① and ② were approved while the plan was still a draft, and /design and /tasks then
    moved it — legitimately. Comparing their receipts against the live document (the obvious
    reading of "check every receipt's digests") turns every healthy repository permanently red.
    """
    plan_doc, config_doc = make_plan(), make_config()
    state = _frozen(plan_doc, config_doc)
    raw = dict(state.raw)
    for gate in ("requirements", "design"):
        raw["gates"][gate]["receipt"]["plan_digest"] = "sha256:" + "1" * 64
    results = doctor.check_freeze_drift(models.State(raw), models.Plan(plan_doc), models.Config(config_doc))
    assert not [f for f in results if f.level == "FAIL"]


def test_a_draft_plan_has_no_freeze_to_check() -> None:
    results = doctor.check_freeze_drift(models.State(make_state(plan_status="draft")), None, None)
    assert [f.level for f in results] == ["INFO"]


def test_a_frozen_artifact_that_cannot_be_read_is_a_fail() -> None:
    """Not measured must never render as fine, least of all for the document a freeze covers."""
    plan_doc, config_doc = make_plan(), make_config()
    results = doctor.check_freeze_drift(_frozen(plan_doc, config_doc), None, models.Config(config_doc))
    fails = [f for f in results if f.level == "FAIL"]
    assert len(fails) == 1 and "cannot be read" in fails[0].message


# --- runtime and sandbox ------------------------------------------------------


def test_an_unsandboxed_profile_is_a_fail_with_the_command_to_fix_it(tmp_path: Path) -> None:
    config = models.Config(make_config())  # host profiles
    results = doctor.check_sandbox(config)
    assert results[0].level == "FAIL"
    assert "rein oci build" in results[0].message


def test_a_digest_pinned_profile_passes(tmp_path: Path) -> None:
    config = models.Config(make_config(profiles=SANDBOXED_PROFILES))
    assert not [f for f in doctor.check_sandbox(config) if f.level == "FAIL" and "run repository" in f.message]


def test_an_oci_profile_with_no_pinned_digest_fails() -> None:
    profiles = {"quality": {"kind": "oci", "network_profile": "none"}}
    config = models.Config(make_config(profiles={**SANDBOXED_PROFILES, **profiles}))
    assert any(f.level == "FAIL" and "no digest-pinned image" in f.message for f in doctor.check_sandbox(config))


def test_check_sandbox_warns_when_the_pinned_image_is_not_built_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh checkout has not run `rein oci build` yet — that is expected, not broken."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(common, "run", lambda argv, **kwargs: (1, "Error: No such object"))
    config = models.Config(make_config(profiles=SANDBOXED_PROFILES))
    results = doctor.check_sandbox(config)
    warns = [f for f in results if f.level == "WARN" and "no local image" in f.message]
    assert len(warns) == len(SANDBOXED_PROFILES)
    assert not [f for f in results if f.level == "FAIL"]


def test_check_sandbox_fails_when_the_local_image_does_not_match_the_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A local image exists under a different digest than the pin — the config drifted from what
    gate 3 froze (or was rebuilt without re-pinning), and doctor must not need a human to already
    suspect that before it says so."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    installed = "sha256:" + "b" * 64
    profile_image = SANDBOXED_PROFILES["implementer"]["image"]
    monkeypatch.setattr(
        common,
        "run",
        lambda argv, **kwargs: (0, installed) if argv[-1] == profile_image else (1, "Error: No such object"),
    )
    config = models.Config(make_config(profiles=SANDBOXED_PROFILES))
    results = doctor.check_sandbox(config)
    fails = [f for f in results if f.level == "FAIL" and "profile 'implementer'" in f.message]
    assert len(fails) == 1
    assert "does not match the pinned" in fails[0].message


def test_check_sandbox_passes_when_the_local_image_matches_the_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    digest = SANDBOXED_PROFILES["implementer"]["image"].rpartition("@")[2]
    monkeypatch.setattr(
        common,
        "run",
        lambda argv, **kwargs: (0, digest) if argv[-1] == digest else (1, "Error: No such object"),
    )
    config = models.Config(make_config(profiles=SANDBOXED_PROFILES))
    results = doctor.check_sandbox(config)
    passes = [f for f in results if f.level == "PASS" and "profile 'implementer':" in f.message]
    assert passes and "pinned image is present" in passes[0].message


def test_a_shared_independence_group_fails() -> None:
    config = make_config()
    config["agents"]["comparator"]["model"] = "opus"  # type: ignore[index]
    results = doctor.check_independence(models.Config(config))
    assert results[0].level == "FAIL"
    assert "share the independence group" in results[0].message


def test_two_models_of_one_provider_pass_but_warn() -> None:
    """A mechanical pass that is weaker than two providers, and the honest thing is to say so
    rather than let a green check imply more independence than exists."""
    results = doctor.check_independence(models.Config(make_config()))
    assert results[0].level == "WARN"
    assert "same provider" in results[0].message


def test_two_providers_pass_cleanly() -> None:
    config = make_config()
    # The provider is the adapter, not a label beside it — two providers means two CLIs.
    config["agents"]["comparator"] = {"adapter": "codex", "model": "gpt"}  # type: ignore[index]
    assert doctor.check_independence(models.Config(config))[0].level == "PASS"


def _no_adapter_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "claude" else f"/usr/bin/{name}")


def test_a_missing_adapter_only_warns_before_the_build_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    """`rein build` is the only implementation path, but nothing has needed the CLI yet."""
    _no_adapter_on_path(monkeypatch)
    results = doctor.check_adapters(models.Config(make_config()), models.State({"current_phase": "tasks"}))
    assert [f.level for f in results] == ["WARN"]
    assert "rein agent <cli>" in results[0].message


def test_a_missing_adapter_fails_once_the_build_phase_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_adapter_on_path(monkeypatch)
    results = doctor.check_adapters(models.Config(make_config()), models.State({"current_phase": "build"}))
    assert [f.level for f in results] == ["FAIL"]


def test_a_missing_adapter_names_the_install_command_and_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`rein` never installs a third-party agent CLI — what lands on an operator's PATH, and what
    it is then allowed to reach, is theirs to choose. Saying *Install it* and stopping there left
    them to go and find out how; the adapter carries the command, and doctor prints it."""
    _no_adapter_on_path(monkeypatch)
    results = doctor.check_adapters(models.Config(make_config()), models.State({"current_phase": "build"}))
    assert adapters.ADAPTER_TABLE["claude"].install_hint in results[0].message
    assert all(a.install_hint for a in adapters.ADAPTER_TABLE.values()), "every adapter can say how"


def test_an_adapter_this_release_cannot_launch_fails_whatever_is_on_path() -> None:
    config = make_config()
    config["agents"]["implementer"]["adapter"] = "cursor"  # type: ignore[index]
    results = doctor.check_adapters(models.Config(config), models.State({"current_phase": "tasks"}))
    assert results[0].level == "FAIL"
    assert "'cursor'" in results[0].message


def test_a_model_the_adapter_cannot_be_told_to_run_fails() -> None:
    """The launchers refuse it; doctor is where that is meant to be found, not `rein build`."""
    config = make_config()
    config["agents"]["comparator"] = {"adapter": "codex", "model": "o1"}  # type: ignore[index]
    results = doctor.check_adapters(models.Config(config), models.State({"current_phase": "tasks"}))
    assert [f.level for f in results if f.level == "FAIL"], results
    assert any("cannot tell 'codex' which model to run" in f.message for f in results)


def test_the_runtime_fallback_warns_that_it_is_weaker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    repo = healthy(tmp_path)
    assert any(f.level == "WARN" and "isolation is weaker" in f.message for f in doctor.check_runtime(repo))


def test_a_leftover_journal_is_reported(tmp_path: Path) -> None:
    from rein import store as store_mod

    repo = healthy(tmp_path)
    store = store_mod.Store(repo)
    store_mod.ensure_private_dir(store.runtime)
    store._write_journal({"tx_id": "abc", "phase": "prepared"})
    assert any("transaction was interrupted" in f.message for f in doctor.check_runtime(repo))


# --- gates and the traceability thread -----------------------------------------


def test_a_broken_gate_ladder_fails(tmp_path: Path) -> None:
    state = models.State(make_state(gates={"requirements": "pending"}))
    results = doctor.check_gate_chain(state)
    assert results[0].level == "FAIL"
    assert "survived a roll back" in results[0].message


def test_a_healthy_ladder_passes() -> None:
    assert doctor.check_gate_chain(models.State(make_state()))[0].level == "PASS"


def test_a_whole_thread_passes(tmp_path: Path) -> None:
    repo = healthy(tmp_path)
    grouped = findings(repo)
    assert any(f.level == "PASS" for f in grouped["trace"])
    assert not [f for f in grouped["plan"] if f.level == "FAIL"]


def test_an_empty_plan_is_not_reported_as_a_broken_thread(tmp_path: Path) -> None:
    """Two things this must not do: report the thread whole (the false green it replaced), and
    warn about work that has not started. A template plan has no thread to follow yet."""
    # docs=True mirrors a real fresh repo: `rein init` scaffolds them, so the only thing missing
    # is the content of the plan itself.
    repo = repo_mod.Repo(seed_repo(tmp_path, plan=make_plan(claims=[], tasks=[]), docs=True))
    plan = models.Plan(make_plan(claims=[], tasks=[]))

    results = doctor.check_plan(repo, plan, None)

    trace = [f for f in results if f.area == "trace"]
    assert [f.level for f in trace] == ["INFO"]
    assert "no thread to follow" in trace[0].message


def test_a_filled_plan_with_no_requirement_id_still_warns(tmp_path: Path) -> None:
    """The warning is about a thread that should exist and does not — not about an empty plan."""
    document = make_plan(claims=[make_claim(requirement_ids=[])])
    repo = repo_mod.Repo(seed_repo(tmp_path, plan=document))

    results = doctor.check_plan(repo, models.Plan(document), None)

    assert any(f.level == "WARN" and "unknown, not whole" in f.message for f in results)


def test_a_claim_citing_an_undeclared_requirement_fails(tmp_path: Path) -> None:
    seed_repo(tmp_path, docs=True)
    (tmp_path / "docs" / "10-requirements.md").write_text("### R-1: title\n", encoding="utf-8")
    plan = models.Plan(make_plan(claims=[make_claim("C-001", requirement_ids=["R-9"])], tasks=[]))
    results = doctor.check_plan(repo_mod.Repo(tmp_path), plan, None)
    assert any(f.level == "FAIL" and f.area == "trace" and "does not declare" in f.message for f in results)


def test_dag_trace_and_check_plan_agree_on_declared_requirements() -> None:
    """A regression that only one of the two read `declared_requirements` would show up as the
    board and the CLI disagreeing about the same repository."""
    text = "### R-1: <title>\n### R-2: real\n"
    assert dag_trace.declared_requirements(text) == ["R-2"]


# --- CI ---------------------------------------------------------------------------

_WIRED_POLICY_WORKFLOW = """on:
  pull_request:
jobs:
  policy:
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
        with:
          fetch-depth: 0
      - run: >-
          uv run rein policy-check
          --base-sha "${{ github.event.pull_request.base.sha }}"
          --head-sha "${{ github.event.pull_request.head.sha }}"
"""


def _ci(tmp_path: Path, body: str) -> list[doctor.Finding]:
    seed_repo(tmp_path)
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "ci.yml").write_text(body, encoding="utf-8")
    return doctor.check_ci(repo_mod.Repo(tmp_path))


def test_ci_that_runs_the_base_side_policy_check_passes(tmp_path: Path) -> None:
    assert [f.level for f in _ci(tmp_path, _WIRED_POLICY_WORKFLOW)] == ["PASS"]


def test_a_policy_check_fed_a_base_the_head_can_choose_is_not_a_check(tmp_path: Path) -> None:
    """Substring-matching the command name called this wired. What makes it a check is the base
    coming from the event context — a branch name is a base the head controls."""
    weakened = _WIRED_POLICY_WORKFLOW.replace("github.event.pull_request.base.sha", "github.head_ref")
    findings = _ci(tmp_path, weakened)
    assert any(f.level == "WARN" and "base SHA from the event context" in f.message for f in findings)


def test_an_unpinned_third_party_action_is_reported(tmp_path: Path) -> None:
    findings = _ci(tmp_path, _WIRED_POLICY_WORKFLOW.replace("@11d5960a326750d5838078e36cf38b85af677262", "@v4"))
    assert any("not pinned to a commit SHA" in f.message for f in findings)


def test_pull_request_target_is_reported(tmp_path: Path) -> None:
    findings = _ci(tmp_path, _WIRED_POLICY_WORKFLOW.replace("pull_request:", "pull_request_target:"))
    assert any("pull_request_target" in f.message for f in findings)


def test_ci_without_the_base_side_check_warns_rather_than_fails(tmp_path: Path) -> None:
    """A product may run it from CI that leaves no trace here — reporting that as broken would be a false alarm."""
    seed_repo(tmp_path)
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("jobs:\n  test:\n    run: pytest\n", encoding="utf-8")
    assert [f.level for f in doctor.check_ci(repo_mod.Repo(tmp_path))] == ["WARN"]


def test_no_workflows_directory_is_information_not_a_finding(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    assert [f.level for f in doctor.check_ci(repo_mod.Repo(tmp_path))] == ["INFO"]


def test_a_profile_naming_a_network_warns_because_the_executor_will_refuse_it() -> None:
    config = models.Config(
        make_config(
            profiles={
                "quality": {
                    "kind": "oci",
                    "image": "localhost/img@sha256:" + "a" * 64,
                    "network_profile": "build-egress",
                }
            }
        )
    )
    assert levels(doctor.check_sandbox(config), "names network") == ["WARN"]


# --- the audit chain and the review -------------------------------------------


def test_an_intact_chain_reports_its_root(tmp_path: Path) -> None:
    repo = healthy(tmp_path, events=chain("cycle_initialized"))
    results = doctor.check_chain(repo)
    assert results[0].level == "PASS"
    assert "root sha256:" in results[0].message


def test_a_damaged_chain_fails_and_says_restore_not_rewrite(tmp_path: Path) -> None:
    repo = healthy(tmp_path, events=chain("cycle_initialized", "task_completed"))
    repo.events.write_text(repo.events.read_text(encoding="utf-8").replace("demo-cycle", "x", 1), encoding="utf-8")
    results = doctor.check_chain(repo)
    assert results[0].level == "FAIL"
    assert "never rewrite it to agree" in results[0].message


def test_an_ungenerated_review_is_info_not_a_pass() -> None:
    results = doctor.check_review(models.Review(make_review(generated=False)))
    assert results[0].level == "INFO"


def test_an_insufficient_coverage_manifest_fails() -> None:
    review = models.Review(make_review(generated=True, coverage_status="insufficient"))
    assert any(f.level == "FAIL" and "undeterminable, not zero" in f.message for f in doctor.check_review(review))


def test_a_blocking_security_finding_fails() -> None:
    finding = {
        "id": "SEC-001",
        "severity": "critical",
        "category": "sandbox_escape",
        "attack_scenario": "the sandbox reaches the docker socket",
        "blocking": True,
    }
    review = models.Review(make_review(generated=True, security_findings=[finding]))
    assert any(f.level == "FAIL" and "1 blocking security" in f.message for f in doctor.check_review(review))


# --- the CLI ------------------------------------------------------------------


def test_a_healthy_repo_has_no_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Healthy is a claim about the repo, so the host's PATH is pinned instead of inherited —
    otherwise git or an agent CLI missing from the runner decides the result."""
    repo = healthy(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    fails = [f for f in doctor.run_checks(repo) if f.level == "FAIL"]
    assert fails == [], "\n".join(f"{f.area}: {f.message}" for f in fails)


def test_the_cli_exits_1_when_something_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_repo(tmp_path)  # host profiles, no requirements doc — several FAILs
    monkeypatch.chdir(tmp_path)
    assert doctor.main([]) == 1


def test_doctor_never_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A doctor that repairs what it finds is a doctor whose findings nobody reads."""
    from rein import store as store_mod

    repo = healthy(tmp_path)
    before = {name: store_mod.Store(repo).document_digest(name) for name in ("plan", "state", "review")}
    monkeypatch.chdir(tmp_path)
    doctor.main([])
    after = {name: store_mod.Store(repo).document_digest(name) for name in ("plan", "state", "review")}
    assert before == after


# --- the gate guard's hook hosts ----------------------------------------------


def _levels(findings: list[doctor.Finding], text: str) -> list[str]:
    return [f.level for f in findings if text in f.message]


def test_a_repo_with_no_hook_host_is_warned(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    findings = doctor.check_hook(repo_mod.Repo(tmp_path))
    assert [f.level for f in findings] == ["WARN"]
    assert "edit-time enforcement is absent" in findings[0].message


def test_the_codex_hook_file_alone_registers_the_guard(tmp_path: Path) -> None:
    """Codex is a hook host in its own right — not an AGENTS.md reader that gets the commit-stage
    check and nothing else."""
    seed_repo(tmp_path)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/hooks.json").write_text('{"hooks": {"PreToolUse": []}}', encoding="utf-8")
    assert _levels(doctor.check_hook(repo_mod.Repo(tmp_path)), "gate guard registered") == []

    (tmp_path / ".codex/hooks.json").write_text(
        '{"hooks": {"PreToolUse": [{"matcher": "apply_patch", "hooks": '
        '[{"type": "command", "command": "rein guard"}]}]}}',
        encoding="utf-8",
    )
    findings = doctor.check_hook(repo_mod.Repo(tmp_path))
    assert _levels(findings, "gate guard registered (codex)") == ["PASS"]
    # …and the two hosts with no registration are named, rather than one "other" of two.
    assert _levels(findings, "Claude Code / VS Code Copilot") == ["INFO"]


def test_the_codex_registration_does_not_claim_to_be_active(tmp_path: Path) -> None:
    """Codex reads project-scoped config only once the project is trusted, and nothing on disk
    can tell whether it has been. The PASS must not be read as "this guard is firing"."""
    seed_repo(tmp_path)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/config.toml").write_text(
        '[[hooks.PreToolUse]]\nmatcher = "apply_patch"\n[[hooks.PreToolUse.hooks]]\n'
        'type = "command"\ncommand = "rein guard"\n',
        encoding="utf-8",
    )
    findings = doctor.check_hook(repo_mod.Repo(tmp_path))
    assert _levels(findings, "only once the project is trusted") == ["INFO"]


def test_a_matcher_that_misses_a_write_tool_is_reported(tmp_path: Path) -> None:
    """ "Is the guard registered?" and "does the registration cover the tools that write?" are two
    questions, and only the first was ever asked. The shipped matcher read `Write|Edit|MultiEdit` —
    `MultiEdit` retired upstream, `NotebookEdit` never added — so a `.ipynb` edit never reached the
    guard and nothing said so."""
    from rein import gate_guard

    seed_repo(
        tmp_path,
        settings='{"hooks": {"PreToolUse": [{"matcher": "Write|Edit", '
        '"hooks": [{"type": "command", "command": "rein guard"}]}]}}',
    )
    findings = doctor.check_hook(repo_mod.Repo(tmp_path))
    assert _levels(findings, "does not name") == ["WARN"]
    assert "NotebookEdit" in [f for f in findings if "does not name" in f.message][0].message

    seed_repo(
        tmp_path,
        settings=json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "|".join(gate_guard.CLAUDE_WRITE_TOOLS),
                            "hooks": [{"type": "command", "command": "rein guard"}],
                        }
                    ]
                }
            }
        ),
    )
    assert _levels(doctor.check_hook(repo_mod.Repo(tmp_path)), "covers every write tool") == ["PASS"]


def test_the_shipped_matcher_covers_every_write_tool() -> None:
    """The payload's settings.json is what `rein install claude` writes, so a hole there ships to
    every product — checking only the repo's own copy would miss exactly that."""
    from rein import gate_guard, install

    groups = install._settings_template(install.INTEGRATIONS["claude"].settings_source)["hooks"]["PreToolUse"]
    covered = {tool for group in groups for tool in str(group.get("matcher", "")).split("|")}
    assert covered >= set(gate_guard.CLAUDE_WRITE_TOOLS), f"missing: {set(gate_guard.CLAUDE_WRITE_TOOLS) - covered}"


def test_a_host_with_no_codex_registration_gets_no_trust_note(tmp_path: Path) -> None:
    seed_repo(tmp_path, settings='{"hooks": {"PreToolUse": [{"hooks": [{"command": "rein guard"}]}]}}')
    findings = doctor.check_hook(repo_mod.Repo(tmp_path))
    assert _levels(findings, "gate guard registered (claude)") == ["PASS"]
    assert _levels(findings, "only once the project is trusted") == []


# --- the sandbox prerequisite -----------------------------------------------------


def test_a_missing_runtime_is_reported_before_any_image_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh repository has every profile on `kind: host`. Gating the runtime check on "OCI
    profiles are configured" meant it was told to build images and never told what with — the
    prerequisite surfaced as a failed build several minutes later instead."""
    monkeypatch.setattr("rein.doctor.shutil.which", lambda _name: None)
    results = doctor.check_sandbox(models.Config(make_config()))
    runtime = [f for f in results if "docker/podman" in f.message]
    assert len(runtime) == 1 and runtime[0].level == "FAIL"
    assert "install one first" in runtime[0].message


def test_the_sandbox_fail_names_a_command_that_finishes_the_job() -> None:
    results = doctor.check_sandbox(models.Config(make_config()))
    fail = next(f for f in results if f.level == "FAIL" and "run repository-derived code" in f.message)
    assert "rein oci build --all --write-config" in fail.message


# --- what the last build run came to ------------------------------------------


def aborted_chain(
    *, reported: str = "", fault: str = "environment_transient", then: tuple[str, ...] = ()
) -> list[models.Event]:
    """A log whose build run stopped on a machine fault, optionally with progress afterwards."""
    from rein import event_chain

    built: list[models.Event] = []
    previous: models.Event | None = None
    detail = {"fault": fault, "where": "T-018: implementer", "rc": 1, "reported": reported}
    for name in ("cycle_initialized", "run_aborted", *then):
        made = event_chain.make(name, DEMO_CYCLE, detail=detail if name == "run_aborted" else None)
        previous = event_chain.link(previous, made)
        built.append(previous)
    return built


def test_a_run_the_machine_stopped_is_surfaced_because_nothing_else_shows_it(tmp_path: Path) -> None:
    """This is the state that looks like nothing happened: no task blocked, no escalation open,
    the board unchanged. Someone returning to it has nothing to read but the absence of progress.
    """
    repo = healthy(tmp_path, events=aborted_chain(reported="resets 3:30am (Asia/Tokyo)"))
    results = doctor.check_last_run(repo)
    assert [f.level for f in results] == ["INFO"]
    assert "3:30am" in results[0].message
    assert "re-run `rein build`" in results[0].message


def test_a_permanent_stop_does_not_invite_a_re_run(tmp_path: Path) -> None:
    repo = healthy(tmp_path, events=aborted_chain(fault="environment_permanent"))
    results = doctor.check_last_run(repo)
    assert [f.level for f in results] == ["WARN"]
    assert "Repair what it names first" in results[0].message


def test_a_run_that_got_going_again_says_nothing(tmp_path: Path) -> None:
    repo = healthy(tmp_path, events=aborted_chain(then=("task_started", "task_completed")))
    assert doctor.check_last_run(repo) == []


def test_a_repository_that_never_stopped_says_nothing(tmp_path: Path) -> None:
    assert doctor.check_last_run(healthy(tmp_path, events=chain("cycle_initialized"))) == []


def aborted_chain_stale(hours_ago: float, *, reported: str = "") -> list[models.Event]:
    """Like `aborted_chain`, but the `run_aborted` event's timestamp is backdated — simulating
    a capacity-limit stop nobody has re-run `rein build` since."""
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone

    from rein import event_chain

    old_ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    detail = {"fault": "environment_transient", "where": "T-018: implementer", "rc": 1, "reported": reported}
    cycle_event = event_chain.link(None, event_chain.make("cycle_initialized", DEMO_CYCLE))
    abort = replace(event_chain.make("run_aborted", DEMO_CYCLE, detail=detail), ts=old_ts)
    return [cycle_event, event_chain.link(cycle_event, abort)]


def test_a_retryable_stop_nobody_has_re_run_in_hours_is_escalated(tmp_path: Path) -> None:
    """A capacity limit that "resets in a few hours" and gets no re-run for the better part of a
    day is no longer "wait for it" — it is "nobody is watching this"."""
    repo = healthy(tmp_path, events=aborted_chain_stale(4, reported="resets 3pm (Asia/Tokyo)"))
    results = doctor.check_last_run(repo)
    assert [f.level for f in results] == ["WARN"]
    assert "--supervise" in results[0].message


def test_a_retryable_stop_still_within_the_window_stays_informational(tmp_path: Path) -> None:
    repo = healthy(tmp_path, events=aborted_chain_stale(0.1))
    results = doctor.check_last_run(repo)
    assert [f.level for f in results] == ["INFO"]


# --- what the *plan* needs, versus what the config declares ----------------------


def test_an_undeclared_independence_pair_is_a_warning_until_a_critical_claim_needs_it() -> None:
    """Independence is required for a *critical* review, and for one pair of roles only.

    Reporting an unset pair as a FAIL regardless of the plan is what made the whole thing read as
    "the implementer and the reviewer must be different models" — on a template whose plan has no
    claims in it at all, and where those two roles are not the pair in question.
    """
    config = make_config()
    for role in ("actual_extractor", "comparator"):
        config["agents"][role].pop("model", None)  # type: ignore[index]

    results = doctor.check_independence(models.Config(config), models.Plan(make_plan()))
    assert [f.level for f in results] == ["WARN"]
    assert "No claim in this plan is `critical`" in results[0].message


def test_an_undeclared_independence_pair_fails_once_a_critical_claim_exists() -> None:
    config = make_config()
    for role in ("actual_extractor", "comparator"):
        config["agents"][role].pop("model", None)  # type: ignore[index]
    plan = make_plan(claims=[{"id": "C-001", "statement": "it holds", "risk": "critical"}])

    results = doctor.check_independence(models.Config(config), models.Plan(plan))
    assert [f.level for f in results] == ["FAIL"]


def test_a_shared_group_stays_a_failure_whatever_the_plan_says() -> None:
    """A plan change cannot make one group into two: that is a configuration, not a judgement."""
    config = make_config()
    config["agents"]["comparator"]["model"] = "opus"  # type: ignore[index]
    results = doctor.check_independence(models.Config(config), models.Plan(make_plan()))
    assert results[0].level == "FAIL"


# --- the adapter capability record -----------------------------------------------


def test_a_self_sandboxing_adapter_inside_a_container_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """The double isolation that surfaced as "the task changed nothing".

    `codex exec` sandboxes itself; inside a container that inner sandbox needs kernel features
    the outer one dropped, so it fails exactly where the agent writes. The symptom points nowhere
    near the cause, which is the whole reason this has to be said out loud.
    """
    monkeypatch.setattr(doctor, "running_containerized", lambda: True)
    config = make_config()
    config["agents"]["implementer"]["adapter"] = "codex"  # type: ignore[index]

    results = doctor.check_nested_sandbox(models.Config(config))
    assert [f.level for f in results] == ["WARN"]
    assert "establishes its own sandbox" in results[0].message


def test_no_nested_sandbox_finding_on_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "running_containerized", lambda: False)
    config = make_config()
    config["agents"]["implementer"]["adapter"] = "codex"  # type: ignore[index]
    assert doctor.check_nested_sandbox(models.Config(config)) == []


def test_an_adapter_that_cannot_resume_says_what_that_costs() -> None:
    """Not a defect — it is what the CLI offers — but it was invisible, and it is the largest
    avoidable cost in a long build."""
    config = make_config()
    config["agents"]["implementer"]["adapter"] = "codex"  # type: ignore[index]
    results = doctor.check_retry_continuity(models.Config(config))
    assert [f.level for f in results] == ["INFO"]
    assert "fresh launch" in results[0].message

    assert doctor.check_retry_continuity(models.Config(make_config()))[0].level == "PASS"


# --- the DoD steps that establish nothing --------------------------------------


def _gate(*steps: dict[str, object]) -> models.Config:
    return models.Config(make_config(quality_gate=list(steps)))


def _cmd(name: str, command: list[str], *, required: bool = True) -> dict[str, object]:
    return {
        "name": name,
        "kind": "command",
        "command": command,
        "executor_profile": "quality",
        "retries": 1,
        "required": required,
    }


def test_a_placeholder_command_is_reported() -> None:
    """`build_loop` has claimed since it was written that doctor can see this. It could not."""
    results = doctor.check_quality_gate(_gate(_cmd("smoke", ["true"], required=False)))

    assert [f.level for f in results] == ["WARN"]
    assert "exits zero and establishes nothing" in results[0].message


def test_a_placeholder_is_matched_on_its_argv_not_its_step_name() -> None:
    """`brief.py` keys on `name == "smoke"`; a rename silences it. This must not inherit that."""
    results = doctor.check_quality_gate(_gate(_cmd("launch", ["/bin/true"])))

    assert [f.level for f in results] == ["WARN"]
    assert "step 'launch'" in results[0].message


def test_a_step_that_is_not_required_is_reported_as_skippable() -> None:
    results = doctor.check_quality_gate(_gate(_cmd("smoke", ["./run", "--once"], required=False)))

    assert [f.level for f in results] == ["WARN"]
    assert "not `required`" in results[0].message


def test_a_gate_that_establishes_something_passes() -> None:
    results = doctor.check_quality_gate(_gate(_cmd("test", ["python", "-m", "pytest"])))

    assert [f.level for f in results] == ["PASS"]


def test_an_agent_step_is_not_judged_by_its_command() -> None:
    review = {"name": "review", "kind": "agent", "agent_role": "code_reviewer", "retries": 1, "required": True}
    results = doctor.check_quality_gate(_gate(review))

    assert [f.level for f in results] == ["PASS"]


# --- sandbox: a FAIL that means "fix before continuing" ------------------------


def test_unsandboxed_profiles_are_a_warning_before_the_build_phase(tmp_path: Path) -> None:
    """`FAIL` is defined as fix-before-continuing, and a repo at `brief` has nothing to continue to."""
    config = models.Config(make_config())
    state = models.State(make_state(phase="requirements"))

    results = doctor.check_sandbox(config, state)

    assert [f.level for f in results if "run repository-derived code" in f.message] == ["WARN"]
    assert "Not yet blocking" in results[0].message


def test_unsandboxed_profiles_are_a_failure_once_the_build_starts(tmp_path: Path) -> None:
    config = models.Config(make_config())
    state = models.State(make_state(phase="build"))

    results = doctor.check_sandbox(config, state)

    assert [f.level for f in results if "run repository-derived code" in f.message] == ["FAIL"]


def test_with_no_state_at_all_the_strict_reading_wins(tmp_path: Path) -> None:
    """Not knowing the phase is not a reason to relax a boundary."""
    results = doctor.check_sandbox(models.Config(make_config()), None)

    assert [f.level for f in results if "run repository-derived code" in f.message] == ["FAIL"]


def _git_repo(tmp_path: Path, gitignore_text: str | None) -> repo_mod.Repo:
    seed_repo(tmp_path, git=True, docs=True, config=make_config())
    if gitignore_text is not None:
        (tmp_path / ".gitignore").write_text(gitignore_text, encoding="utf-8")
    return repo_mod.Repo(tmp_path)


def _healthy_gitignore(tmp_path: Path) -> str:
    from rein import gitignore

    return gitignore.merge("", (tmp_path / ".rein" / "config.yaml").read_text(encoding="utf-8"))[0]


def test_gitignore_check_is_skipped_outside_a_git_checkout(tmp_path: Path) -> None:
    seed_repo(tmp_path, config=make_config())
    results = doctor.check_gitignore(repo_mod.Repo(tmp_path))
    assert [f.level for f in results] == ["INFO"]


def test_a_healthy_gitignore_passes(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, None)
    (tmp_path / ".gitignore").write_text(_healthy_gitignore(tmp_path), encoding="utf-8")
    results = doctor.check_gitignore(repo)
    assert [f.level for f in results] == ["PASS"]


def test_a_missing_runtime_artifact_block_warns_and_names_sync(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, ".venv/\n")
    results = doctor.check_gitignore(repo)
    assert [f.level for f in results] == ["WARN"]
    assert "rein sync" in results[0].message


def test_a_gitignored_ssot_path_is_a_failure(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, None)
    (tmp_path / ".gitignore").write_text(_healthy_gitignore(tmp_path) + "\n.rein/\n", encoding="utf-8")
    results = doctor.check_gitignore(repo)
    fail = [f for f in results if f.level == "FAIL"]
    assert fail, "gitignoring the SSOT is a broken invariant"
    assert ".rein/state.yaml" in fail[0].message
    assert "remove them from .gitignore" in fail[0].message


def test_gitignoring_docs_is_also_a_failure(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, None)
    (tmp_path / ".gitignore").write_text(_healthy_gitignore(tmp_path) + "\ndocs/\n", encoding="utf-8")
    results = doctor.check_gitignore(repo)
    assert [f.level for f in results if f.level == "FAIL"]
    assert "docs/" in next(f.message for f in results if f.level == "FAIL")


def test_a_tracked_runtime_artifact_warns_that_the_ignore_rule_is_inert(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, None)
    (tmp_path / ".gitignore").write_text(_healthy_gitignore(tmp_path), encoding="utf-8")
    work = tmp_path / ".rein" / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "T-001.json").write_text("{}", encoding="utf-8")
    repo._git("add", "-f", ".rein/work/T-001.json")
    results = doctor.check_gitignore(repo)
    warned = [f for f in results if f.level == "WARN" and "tracked by git" in f.message]
    assert warned
    assert ".rein/work/" in warned[0].message


def test_a_schema_invalid_document_is_reported_with_the_command_that_repairs_it(tmp_path: Path) -> None:
    """`build.md` promises exactly that, and these four were reported with none — an upgrade that
    renamed `config.yaml` keys left a repo with FAILs, no command, and a repair (`rein revise --to
    tasks`) that is not guessable, because the keys are frozen at gate ③."""
    (tmp_path / ".rein").mkdir()
    (tmp_path / ".rein" / "config.yaml").write_text("not_a_known_key: 1\n", encoding="utf-8")
    findings, _ = doctor.check_documents(repo_mod.Repo(tmp_path))
    failed = [f for f in findings if f.level == "FAIL" and f.message.startswith("config.yaml")]
    assert failed, "a config the schema refuses is a FAIL"
    assert "rein revise --to tasks" in failed[0].message


def test_a_document_a_newer_rein_wrote_is_not_reported_as_damaged(tmp_path: Path) -> None:
    """The same FAIL, a different cause, and the difference is what the human is told to do.

    A repository written by a newer release carries keys this tool's schema has never heard of, so
    every document check fails — and the standing repair sends them to `rein revise --to tasks`,
    rewinding an approved gate to fix a document that is not broken. The skew is knowable (the lock
    records who wrote it), so the repair line says to upgrade instead.
    """
    (tmp_path / ".rein").mkdir()
    (tmp_path / ".rein" / "config.yaml").write_text("not_a_known_key: 1\n", encoding="utf-8")
    lock_mod.write(tmp_path / ".rein" / "rein.lock", lock_mod.new("99.0.0", ""))
    findings, _ = doctor.check_documents(repo_mod.Repo(tmp_path))
    failed = [f for f in findings if f.level == "FAIL" and f.message.startswith("config.yaml")]
    assert failed, "the tool still cannot read it, so it is still a FAIL"
    assert "99.0.0" in failed[0].message
    assert "rein revise --to tasks" not in failed[0].message, "never a gate rollback for a stale tool"


def test_the_stack_extension_is_reported_when_gh_is_there(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--merge` is the only thing that needs it, so its absence is INFO — but it must be said.

    Merging a stack by hand is where the damage is: merge a subset and GitHub rebases everything
    above the cut off the commits the record names.
    """
    monkeypatch.setattr("rein.doctor.shutil.which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr("rein.doctor.common.run", lambda *a, **k: (0, "gh stack\tgithub/gh-stack\tv0.1.0"))
    assert [f.level for f in doctor.check_stack_extension()] == ["PASS"]

    monkeypatch.setattr("rein.doctor.common.run", lambda *a, **k: (0, ""))
    missing = doctor.check_stack_extension()
    assert [f.level for f in missing] == ["INFO"]
    assert "gh extension install github/gh-stack" in missing[0].message


def test_no_second_line_about_an_extension_when_gh_itself_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rein.doctor.shutil.which", lambda _name: None)
    assert doctor.check_stack_extension() == []


# --- the release check -----------------------------------------------------------


def _upstream(repo: repo_mod.Repo) -> doctor.Finding:
    found = doctor.check_upstream(repo)
    assert len(found) == 1
    return found[0]


def test_a_newer_release_is_a_warn_naming_the_command_that_actually_upgrades(
    make_repo_obj: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`uv tool upgrade` leaves a tag-pinned install where it was, so the WARN must not print it."""
    repo = make_repo_obj()  # type: ignore[operator]
    monkeypatch.delenv(upstream.NO_CHECK_ENV, raising=False)
    monkeypatch.setattr(upstream, "detect_source", lambda: "git+https://github.com/o/r@v0.0.1")
    monkeypatch.setattr(upstream, "latest_release", lambda *a, **k: "v999.0.0")
    finding = _upstream(repo)
    assert finding.level == "WARN"
    assert "uv tool install --force 'git+https://github.com/o/r@v999.0.0'" in finding.message
    assert "uv tool upgrade" not in finding.message
    # What it saw is left in the cache, which is the only thing `rein start` ever reads.
    cached = upstream.read_cache()
    assert cached is not None and cached["tag"] == "v999.0.0"


def test_being_current_is_a_pass(make_repo_obj: object, monkeypatch: pytest.MonkeyPatch) -> None:
    import rein

    repo = make_repo_obj()  # type: ignore[operator]
    monkeypatch.delenv(upstream.NO_CHECK_ENV, raising=False)
    monkeypatch.setattr(upstream, "detect_source", lambda: "git+https://github.com/o/r@v0.0.1")
    monkeypatch.setattr(upstream, "latest_release", lambda *a, **k: f"v{rein.__version__}")
    assert _upstream(repo).level == "PASS"


def test_an_unanswerable_release_check_is_info_never_pass(
    make_repo_obj: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No gh, no network, no VCS origin: doctor never reports an unasked question as healthy."""
    repo = make_repo_obj()  # type: ignore[operator]
    monkeypatch.setattr(upstream, "latest_release", lambda *a, **k: None)
    monkeypatch.delenv(upstream.NO_CHECK_ENV, raising=False)
    finding = _upstream(repo)
    assert finding.level == "INFO"
    assert "could not check" in finding.message
    assert upstream.read_cache() is None  # nothing seen, nothing recorded


def test_the_opt_out_is_reported_as_the_reason(make_repo_obj: object, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo_obj()  # type: ignore[operator]
    monkeypatch.setenv(upstream.NO_CHECK_ENV, "1")
    monkeypatch.setattr(
        common, "run", lambda *a, **k: pytest.fail(f"{upstream.NO_CHECK_ENV} must keep doctor off the network")
    )
    finding = _upstream(repo)
    assert finding.level == "INFO" and upstream.NO_CHECK_ENV in finding.message
