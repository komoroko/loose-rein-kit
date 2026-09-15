"""The sandbox hardening is unconditional — these tests assert the argv proves it.

Most of this runs without a container runtime: the `docker run` argv is built the same way
whether or not docker is installed, so we can read every hardening flag out of it. The one
test that actually builds an image is behind the `integration` marker.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rein import common, executors, models


def _oci_profile(**overrides: object) -> models.ExecutorProfile:
    raw = {
        "kind": "oci",
        "image": "localhost/rein-python@sha256:" + "a" * 64,
        "network_profile": "none",
        "read_only_root": True,
    }
    raw.update(overrides)
    return models.ExecutorProfile("quality", raw)


def _spec(profile: models.ExecutorProfile, **overrides: object) -> executors.ExecutionSpec:
    kwargs: dict[str, object] = {"command": ("pytest", "-q"), "profile": profile}
    kwargs.update(overrides)
    return executors.ExecutionSpec(**kwargs)  # type: ignore[arg-type]


def test_argv_carries_every_hardening_flag() -> None:
    argv = executors.OciExecutor(runtime="docker")._argv(_spec(_oci_profile()))
    joined = " ".join(argv)
    # The flags the module docstring promises are unconditional — each must be present.
    assert "--network none" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--cap-drop ALL" in joined
    assert "--read-only" in joined
    assert f"--user {os.getuid()}:{os.getgid()}" in joined
    assert "--pids-limit" in joined
    assert "--memory" in joined
    assert "--cpus" in joined
    # An ephemeral HOME so the container cannot read the host's ~/.ssh, ~/.aws, etc.
    assert "HOME=/tmp" in joined
    # The image is the pinned digest reference, and the command comes after it.
    assert argv[-3:] == [_oci_profile().image, "pytest", "-q"]


def test_argv_never_mounts_host_secrets() -> None:
    """No mount is added unless the spec asks for one — the host filesystem is not exposed."""
    argv = executors.OciExecutor(runtime="docker")._argv(_spec(_oci_profile()))
    joined = " ".join(argv)
    for forbidden in ("/var/run/docker.sock", ".ssh", ".aws", "/root", "HOME=/home"):
        assert forbidden not in joined


def test_argv_honors_declared_mounts_readonly_flag() -> None:
    from pathlib import Path

    spec = _spec(_oci_profile(), mounts=((Path("/repo"), "/work", "ro"), (Path("/out"), "/out", "rw")))
    joined = " ".join(executors.OciExecutor(runtime="docker")._argv(spec))
    assert "type=bind,src=/repo,dst=/work,readonly=true" in joined
    assert "type=bind,src=/out,dst=/out,readonly=false" in joined


def test_argv_env_allowlist_only_passes_named_vars() -> None:
    profile = _oci_profile(env_allowlist=["CI"])
    spec = _spec(profile, env={"CI": "1", "SECRET_TOKEN": "leak"})
    joined = " ".join(executors.OciExecutor(runtime="docker")._argv(spec))
    assert "CI=1" in joined
    assert "SECRET_TOKEN" not in joined


def test_read_only_root_can_be_disabled_by_profile() -> None:
    argv = executors.OciExecutor(runtime="docker")._argv(_spec(_oci_profile(read_only_root=False)))
    assert "--read-only" not in argv


def test_oci_executor_refuses_unpinned_image() -> None:
    profile = _oci_profile(image="localhost/rein-python:latest")
    with pytest.raises(executors.ExecutorError, match="digest-pinned"):
        executors.OciExecutor(runtime="docker").run(_spec(profile))


def test_oci_executor_refuses_a_profile_asking_for_egress() -> None:
    """An unenforced knob reads like a boundary. There is no receipt to authorize egress, so
    naming a network is refused rather than quietly honoured."""
    profile = _oci_profile(network_profile="build-egress")
    with pytest.raises(executors.ExecutorError, match="signed receipt"):
        executors.OciExecutor(runtime="docker").run(_spec(profile))


def test_argv_pins_network_none_whatever_the_profile_says() -> None:
    argv = executors.OciExecutor(runtime="docker")._argv(_spec(_oci_profile(network_profile="build-egress")))
    assert "--network none" in " ".join(argv)


def test_oci_executor_refuses_host_profile() -> None:
    host = models.ExecutorProfile("t", {"kind": "host"})
    with pytest.raises(executors.ExecutorError, match="host profile"):
        executors.OciExecutor(runtime="docker").run(_spec(host))


def test_host_executor_refuses_sandboxed_profile() -> None:
    with pytest.raises(executors.ExecutorError, match="OCI profile"):
        executors.HostExecutor().run(_spec(_oci_profile()))


def test_for_profile_dispatches_host_without_a_runtime() -> None:
    host = models.ExecutorProfile("t", {"kind": "host"})
    assert isinstance(executors.for_profile(host), executors.HostExecutor)


def test_host_executor_runs_a_trusted_command() -> None:
    host = models.ExecutorProfile("t", {"kind": "host"})
    result = executors.HostExecutor().run(_spec(host, command=("true",)))
    assert result.exit_code == 0
    assert result.image_digest == "host"


def test_containerfile_names_lists_the_packaged_profiles() -> None:
    """Two images, because two paths reach an executor.

    `python` boxes in repository-derived code; `agent` boxes in the CLI that writes it. What is
    not here is the pair that used to be — `implementer` and `reviewer`, shipped beside `python`
    while nothing launched through either, so the gap read as a configuration somebody had not
    finished rather than a mechanism that did not exist.
    """
    assert set(executors.containerfile_names()) == {"python", "agent"}


def test_verify_pinned_host_profile_is_a_noop() -> None:
    host = models.ExecutorProfile("t", {"kind": "host"})
    ok, message = executors.verify_pinned(host)
    assert ok
    assert "nothing to pin" in message


def test_verify_pinned_reports_unpinned_profile() -> None:
    profile = _oci_profile(image="localhost/rein-python:latest")
    ok, message = executors.verify_pinned(profile)
    assert not ok
    assert "pins no image digest" in message


def test_verify_pinned_reports_missing_local_image() -> None:
    """A pinned digest with no matching local image says 'build it', not a cryptic runtime error."""
    ok, message = executors.verify_pinned(_oci_profile(), runtime="docker")
    assert not ok
    # Either no runtime, or the image is genuinely absent — both are actionable messages.
    assert "oci build" in message or "no local image" in message or "no container runtime" in message


def _fake_inspect(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, tuple[int, str]], calls: list[str] | None = None
) -> None:
    """Stand in for `docker inspect`, answering per reference. Unlisted references are absent."""

    def fake_run(argv: list[str], **kwargs: object) -> tuple[int, str]:
        reference = argv[-1]
        if calls is not None:
            calls.append(reference)
        return answers.get(reference, (1, "Error: No such object"))

    monkeypatch.setattr(common, "run", fake_run)


def test_verify_pinned_accepts_an_image_the_engine_knows_only_by_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Docker's classic image store gives a never-pushed image no repository digest.

    The pinned `repository@digest` then does not resolve even though the image is present under
    exactly that digest as its Id — so `oci verify` must not send anyone off to rebuild it.
    """
    digest = "sha256:" + "a" * 64
    _fake_inspect(monkeypatch, {digest: (0, digest)})
    ok, message = executors.verify_pinned(_oci_profile(), runtime="docker")
    assert ok
    assert digest in message


def test_run_uses_the_reference_the_engine_can_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever `verify` says is present is what `run` runs — the same reference, not the pin."""
    digest = "sha256:" + "a" * 64
    calls: list[str] = []
    _fake_inspect(monkeypatch, {digest: (0, digest)}, calls)
    argv = executors.OciExecutor(runtime="docker")._argv(
        _spec(_oci_profile()), executors.resolve_pinned(_oci_profile(), runtime="docker")[0]
    )
    assert argv[-3:] == [digest, "pytest", "-q"]
    assert "localhost/rein-python@" + digest in calls  # the pinned form was tried first


def test_verify_pinned_reports_a_digest_that_is_not_what_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    installed = "sha256:" + "b" * 64
    _fake_inspect(monkeypatch, {"localhost/rein-python@sha256:" + "a" * 64: (0, installed)})
    ok, message = executors.verify_pinned(_oci_profile(), runtime="docker")
    assert not ok
    assert installed in message and "does not match the pinned" in message


def test_verify_pinned_reads_a_repository_digest_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Podman and docker's containerd store answer with a RepoDigest, not only an Id."""
    digest = "sha256:" + "a" * 64
    reference = "localhost/rein-python@" + digest
    _fake_inspect(monkeypatch, {reference: (0, f"sha256:{'c' * 64} {reference}")})
    ok, _ = executors.verify_pinned(_oci_profile(), runtime="docker")
    assert ok


@pytest.mark.integration
def test_build_image_produces_a_pinned_digest() -> None:
    if executors.container_runtime() is None:
        pytest.skip("no container runtime on PATH")
    digest = executors.build_image("python")
    assert digest.startswith("sha256:")
    profile = _oci_profile(image=f"localhost/rein-python@{digest}")
    ok, message = executors.verify_pinned(profile)
    assert ok, message  # the message names the branch that failed — an `assert ok` alone cannot


# --- a custom, repository-local Containerfile (a `dockerfile:` profile) --------


def test_build_image_from_dockerfile_refuses_up_front_with_no_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executors, "container_runtime", lambda: None)
    dockerfile = tmp_path / "Containerfile"
    dockerfile.write_text("FROM scratch\n")
    with pytest.raises(executors.ExecutorError, match="no container runtime"):
        executors.build_image_from_dockerfile(dockerfile)


def test_build_image_from_dockerfile_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(executors.ExecutorError, match="no Containerfile at"):
        executors.build_image_from_dockerfile(tmp_path / "does-not-exist", runtime="docker")


@pytest.mark.integration
def test_build_image_from_dockerfile_produces_a_pinned_digest(tmp_path: Path) -> None:
    """A custom OCI profile builds from the repository, not `data/oci/<name>/`. Based on the same
    pinned base image the packaged Containerfiles use — already pulled by that integration test,
    so this adds no new network dependency — rather than `FROM scratch`, whose config digest and
    image ID diverge under buildx on some engines, which is a `docker build` quirk orthogonal to
    what this test is checking."""
    if executors.container_runtime() is None:
        pytest.skip("no container runtime on PATH")
    dockerfile = tmp_path / "Containerfile"
    dockerfile.write_text(
        "FROM docker.io/library/python:3.13-slim-bookworm@"
        "sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64\n"
        "LABEL rein-test=1\n"
    )
    digest = executors.build_image_from_dockerfile(dockerfile)
    assert digest.startswith("sha256:")
    profile = _oci_profile(image=f"localhost/rein-{tmp_path.name}@{digest}")
    ok, message = executors.verify_pinned(profile)
    assert ok, message


# --- the limit is the limit ---------------------------------------------------


def test_the_swap_ceiling_is_stated_rather_than_left_to_the_engine() -> None:
    """Docker gives a container twice its `--memory` in memory+swap unless told otherwise, so a
    step declared to have 1 GiB could reach 2 — and a run that survives on swap is not the run the
    measurement was about."""
    profile = models.ExecutorProfile("quality", {"kind": "oci", "image": "x@sha256:" + "a" * 64, "memory_mb": 2048})
    spec = executors.ExecutionSpec(command=("true",), profile=profile, mounts=(), workdir="/work")
    argv = executors.OciExecutor(runtime="docker")._argv(spec)
    assert "--memory" in argv and argv[argv.index("--memory") + 1] == "2048m"
    assert "--memory-swap" in argv and argv[argv.index("--memory-swap") + 1] == "2048m"


# --- the agent sandbox: the other kind, and the opposite network -----------------
#
# `oci` boxes in repository-derived code and is denied egress. `oci-agent` boxes in the CLI that
# writes that code and requires it, because an agent that cannot reach its model API does nothing.
# These assert that the two cannot be confused for each other by a config, because the failure
# mode of confusing them is a quality-gate step with a way out.


def _agent_profile(**overrides: object) -> models.ExecutorProfile:
    raw: dict[str, object] = {
        "kind": "oci-agent",
        "image": "localhost/rein-agent@sha256:" + "b" * 64,
        "network_profile": "egress",
    }
    raw.update(overrides)
    return models.ExecutorProfile("agent", raw)


def test_an_agent_sandbox_is_given_the_bridge_and_every_other_hardening_flag() -> None:
    """The network is the only thing that differs. Everything the quality-gate box drops, this
    drops too — which is what the boundary is actually worth, since the network is wide open."""
    joined = " ".join(executors.OciExecutor(runtime="docker")._argv(_spec(_agent_profile())))
    assert "--network bridge" in joined
    assert "--network none" not in joined
    for flag in (
        "--security-opt no-new-privileges",
        "--cap-drop ALL",
        f"--user {os.getuid()}:{os.getgid()}",
        "HOME=/tmp",
    ):
        assert flag in joined
    for forbidden in ("/var/run/docker.sock", ".ssh", ".aws", "/root"):
        assert forbidden not in joined


def test_the_container_runs_as_the_host_user_not_a_fixed_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    """The uid is not a hardening knob — it is what makes the box reachable.

    `control_plane` binds its socket at 0600 and the worktree is the host user's, so a container
    on any other uid gets EACCES on `rein report`: the leaf works, then cannot say what it did.
    The constant this replaced was `1000:1000`, which is why the suite was green on a developer
    laptop and red on a CI runner at 1001 — so this test says the host, rather than repeating a
    number that happens to match on the machine it runs on.
    """
    monkeypatch.setattr(os, "getuid", lambda: 4242)
    monkeypatch.setattr(os, "getgid", lambda: 4343)
    for profile in (_oci_profile(), _agent_profile()):
        joined = " ".join(executors.OciExecutor(runtime="docker")._argv(_spec(profile)))
        assert "--user 4242:4343" in joined
        assert "1000:1000" not in joined


def test_an_agent_sandbox_without_egress_is_refused() -> None:
    """`none` here would build a box the agent cannot work in, and the failure would arrive as a
    model call that timed out rather than as a config that says the wrong thing."""
    with pytest.raises(executors.ExecutorError, match="does nothing"):
        executors.OciExecutor(runtime="docker").run(_spec(_agent_profile(network_profile="none")))


def test_a_quality_gate_profile_cannot_borrow_the_agent_kind_s_egress() -> None:
    """The reason the two kinds exist rather than one kind with a knob: a typo in a knob would
    hand repository-derived code a way out, and nothing downstream would read differently."""
    with pytest.raises(executors.ExecutorError, match="signed receipt"):
        executors.OciExecutor(runtime="docker").run(_spec(_oci_profile(network_profile="egress")))


def test_stdin_attaches_the_container_s_input_and_nothing_else() -> None:
    """An adapter that takes its prompt on stdin needs `-i`. Never `-t`: a tty tells some CLIs a
    human is watching, and they change what they emit when they think so."""
    joined = " ".join(executors.OciExecutor(runtime="docker")._argv(_spec(_agent_profile(), stdin="prompt")))
    assert "--interactive" in joined
    assert "--tty" not in joined
    assert "-t" not in joined.split()


def test_runner_minted_wiring_passes_whatever_the_allowlist_says() -> None:
    """The allowlist stops the *host's* environment leaking in. The control socket and the
    capability token did not come from the host's environment — they were minted for this launch —
    and a profile that forgot to name them would produce a leaf that cannot report its own outcome
    and no error saying why."""
    spec = _spec(
        _agent_profile(env_allowlist=["ANTHROPIC_API_KEY"]),
        env={"ANTHROPIC_API_KEY": "k", "SECRET_TOKEN": "leak"},
        env_always={"REIN_CONTROL_SOCKET": "/run/rein/control.sock", "REIN_TASK_ID": "T-001"},
    )
    joined = " ".join(executors.OciExecutor(runtime="docker")._argv(spec))
    assert "ANTHROPIC_API_KEY=k" in joined
    assert "REIN_CONTROL_SOCKET=/run/rein/control.sock" in joined
    assert "REIN_TASK_ID=T-001" in joined
    assert "SECRET_TOKEN" not in joined


def test_for_profile_sends_both_contained_kinds_to_the_oci_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executors, "container_runtime", lambda: "docker")
    assert isinstance(executors.for_profile(_oci_profile()), executors.OciExecutor)
    assert isinstance(executors.for_profile(_agent_profile()), executors.OciExecutor)
    assert isinstance(executors.for_profile(models.ExecutorProfile("t", {"kind": "host"})), executors.HostExecutor)
