"""Executors: where repository-derived code is allowed to run, and how it is boxed in.

The rule (plan §10.1): **anything that runs repository code, tests, or build scripts runs in an
OCI sandbox, regardless of risk.** A test file is code an agent wrote, and running it on the
host runs it with the host's credentials, its SSH agent, its cloud tokens, its docker socket.
`host` execution exists only for trusted, pinned tooling that runs nothing the repository
produced.

The sandbox is built from a config `executor_profile` and hardened the same way every time:

  network       denied, full stop (`--network none`). A profile *may* name a network profile,
                and a named one is refused: egress is granted only by an experiment with a
                signed receipt, and no such receipt exists for anything to check. An unenforced
                knob is worse than a missing one — it reads like a boundary.
  filesystem    read-only root (`--read-only`), a size-capped writable tmpfs, the repo or
                worktree mounted read-only (a reviewer) or read-write (an implementer), and
                **nothing else** — no HOME, no ~/.ssh, no ~/.aws, no /var/run/docker.sock.
  privileges    `--security-opt no-new-privileges`, a non-root user, a pids limit, memory and
                cpu caps, so a runaway or a fork bomb cannot take the host with it.
  environment   an allowlist, passed explicitly; the container starts from nothing it did not
                bring.

The image is **digest-pinned**, always. A mutable tag would let the environment a review ran
in change after that review was signed (plan §10.2), so the profile carries
`image: <ref>@sha256:...` and this module refuses to run an un-pinned OCI profile.

Images are built locally from the Containerfiles the package ships (`data/oci/<profile>/`) via
:func:`build_image`, which prints the digest to pin. Nothing here reaches a registry: the
sandbox a review runs in is reproducible from the repository, not fetched.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from rein import common, data, digests, models

# Prefer docker; podman is a drop-in for the flags used here.
_RUNTIMES = ("docker", "podman")


class ExecutorError(RuntimeError):
    """A sandbox could not be prepared or run."""


@dataclass(frozen=True)
class ExecutionSpec:
    """One command to run in a sandbox, with the mounts and limits it is allowed."""

    command: tuple[str, ...]
    profile: models.ExecutorProfile
    mounts: tuple[tuple[Path, str, str], ...] = ()  # (host path, container path, "ro"|"rw")
    env: dict[str, str] = field(default_factory=dict)
    workdir: str = "/work"
    timeout_sec: float | None = None


@dataclass(frozen=True)
class ExecutionResult:
    """What a run produced. `image_digest` records which sandbox it actually ran in."""

    exit_code: int
    output: str
    image_digest: str
    timed_out: bool = False


def container_runtime() -> str | None:
    """The first available container runtime, or None."""
    for runtime in _RUNTIMES:
        if shutil.which(runtime):
            return runtime
    return None


class Executor:
    """Runs an :class:`ExecutionSpec`. Two kinds — `oci` and `host` — chosen by the profile."""

    def run(self, spec: ExecutionSpec) -> ExecutionResult:  # pragma: no cover - dispatch only
        raise NotImplementedError


@dataclass(frozen=True)
class HostExecutor(Executor):
    """Runs on the host, for trusted pinned tooling only.

    Refuses any command that a caller might mistake for "run the repo's code here": the guard
    is not a substitute for reading the call site, but it turns the most likely mistake into an
    error instead of a host-level code execution.
    """

    def run(self, spec: ExecutionSpec) -> ExecutionResult:
        if spec.profile.is_sandboxed:
            raise ExecutorError("HostExecutor was handed an OCI profile — route it through OciExecutor")
        rc, out = common.run(
            list(spec.command),
            # A host run has no mount to place it in, so `workdir` is a real directory here. The
            # default `/work` is the container's, so it is only honoured when a caller sets one.
            cwd=spec.workdir if spec.workdir != "/work" else None,
            timeout=spec.timeout_sec,
            env={**spec.env} if spec.env else None,
        )
        return ExecutionResult(exit_code=rc, output=out, image_digest="host", timed_out=rc == common.RC_TIMEOUT)


@dataclass(frozen=True)
class OciExecutor(Executor):
    """Runs inside a digest-pinned container, hardened per the module docstring."""

    runtime: str

    @classmethod
    def create(cls) -> OciExecutor:
        runtime = container_runtime()
        if runtime is None:
            raise ExecutorError(
                "no container runtime (docker/podman) on PATH, but an OCI profile was requested — "
                "install one, or the code this would sandbox cannot run safely"
            )
        return cls(runtime=runtime)

    def run(self, spec: ExecutionSpec) -> ExecutionResult:
        profile = spec.profile
        if not profile.is_sandboxed:
            raise ExecutorError(f"profile {profile.name!r} is a host profile — route it through HostExecutor")
        digest = profile.image_digest
        if not digests.is_digest(digest):
            raise ExecutorError(
                f"profile {profile.name!r} has no digest-pinned image. A mutable tag would let the "
                "sandbox change after a review was signed — pin it with `rein oci build`."
            )
        network = profile.network_profile or "none"
        if network != "none":
            raise ExecutorError(
                f"profile {profile.name!r} asks for network {network!r}. Egress from a sandbox is granted "
                "only by an experiment with a signed receipt, and no such receipt exists to check — so "
                "there is nothing here that could authorize it. Set `network_profile: none`, or move the "
                "work that needs egress outside the sandbox where a human can see it."
            )

        reference, problem = resolve_pinned(profile, runtime=self.runtime)
        if problem:
            raise ExecutorError(problem)

        argv = self._argv(spec, reference)
        rc, out = common.run(argv, timeout=spec.timeout_sec)
        return ExecutionResult(exit_code=rc, output=out, image_digest=digest, timed_out=rc == common.RC_TIMEOUT)

    def _argv(self, spec: ExecutionSpec, reference: str | None = None) -> list[str]:
        """The full `docker run` argv. Every hardening flag is unconditional, not a knob.

        `reference` is what `resolve_pinned` found this engine can actually run; it names the
        same digest the profile pins. Absent, the pinned reference is used verbatim.
        """
        profile = spec.profile
        argv = [
            self.runtime,
            "run",
            "--rm",
            "--network",
            "none",  # `run` refuses anything else before reaching here
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--user",
            "1000:1000",
            "--workdir",
            spec.workdir,
        ]
        if profile.raw.get("read_only_root", True):
            argv += ["--read-only"]
        tmp_mb = profile.raw.get("writable_tmp_mb", 512)
        argv += ["--tmpfs", f"/tmp:size={int(tmp_mb) if isinstance(tmp_mb, int) else 512}m,mode=1777"]
        argv += ["--pids-limit", str(_int(profile.raw.get("pids_limit"), 256))]
        argv += ["--memory", f"{_int(profile.raw.get('memory_mb'), 1024)}m"]
        argv += ["--cpus", str(_int(profile.raw.get("cpu_count"), 2))]
        # An empty, ephemeral HOME: the container cannot read the host's ~/.ssh, ~/.aws, etc.
        argv += ["--env", "HOME=/tmp"]

        for host_path, container_path, mode in spec.mounts:
            readonly = "true" if mode == "ro" else "false"
            argv += ["--mount", f"type=bind,src={host_path},dst={container_path},readonly={readonly}"]
        for name in profile.env_allowlist:
            if name in spec.env:
                argv += ["--env", f"{name}={spec.env[name]}"]
        argv.append(reference or profile.image)
        argv += list(spec.command)
        return argv


def _int(value: object, default: int) -> int:
    return value if isinstance(value, int) else default


def for_profile(profile: models.ExecutorProfile) -> Executor:
    """The executor a profile calls for. The one dispatch point, so the rule lives in one place."""
    return OciExecutor.create() if profile.is_sandboxed else HostExecutor()


# --- image building ------------------------------------------------------------


def containerfile_names() -> list[str]:
    """The Containerfiles the package ships, by profile name (the `data/oci/<name>/` dirs)."""
    names: set[str] = set()
    for rel, _ in data.iter_files("oci"):
        parts = rel.split("/")
        if len(parts) >= 2 and parts[-1] == "Containerfile":
            names.add(parts[-2])
    return sorted(names)


def build_image(name: str, *, tag: str | None = None, runtime: str | None = None) -> str:
    """Build the packaged Containerfile `name` locally and return its `sha256:` image digest.

    The digest is what a config profile pins. Building is a bootstrap convenience — nothing is
    fetched from a registry — and re-pinning after a rebuild is what keeps the sandbox a review
    ran in reproducible from the repository.
    """
    if name not in containerfile_names():
        raise ExecutorError(f"no packaged Containerfile named {name!r} (have: {', '.join(containerfile_names())})")
    engine = runtime or container_runtime()
    if engine is None:
        raise ExecutorError("no container runtime (docker/podman) on PATH")

    import tempfile

    image_tag = tag or f"localhost/rein-{name}:local"
    with tempfile.TemporaryDirectory() as workdir:
        context = Path(workdir)
        for rel, blob in data.iter_files(f"oci/{name}"):
            target = context / rel[len(f"oci/{name}/") :]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        iid_file = context / "iid"
        rc, out = common.run(
            [
                engine,
                "build",
                "-t",
                image_tag,
                "--iidfile",
                str(iid_file),
                "-f",
                str(context / "Containerfile"),
                str(context),
            ],
            timeout=1800,
        )
        if rc != 0:
            raise ExecutorError(f"building {name} failed (rc={rc}):\n{out[-2000:]}")
        return _image_digest(engine, image_tag, iid_file)


def _image_digest(engine: str, image_tag: str, iid_file: Path) -> str:
    """The image's content digest (`sha256:...`), from the iidfile or an inspect."""
    try:
        raw = iid_file.read_text(encoding="utf-8").strip()
    except OSError:
        raw = ""
    if raw.startswith("sha256:") and digests.is_digest(raw):
        return raw
    rc, out = common.run([engine, "inspect", "--format", "{{.Id}}", image_tag], timeout=60)
    candidate = out.strip()
    if rc == 0 and digests.is_digest(candidate):
        return candidate
    raise ExecutorError(f"could not determine the image digest of {image_tag} (got {candidate!r})")


def _local_digests(engine: str, reference: str) -> set[str]:
    """The `sha256:…` digests under which the engine knows `reference` locally (empty: unknown)."""
    rc, out = common.run(
        [engine, "inspect", "--format", "{{.Id}}{{range .RepoDigests}} {{.}}{{end}}", reference],
        timeout=60,
    )
    if rc != 0:
        return set()
    # An Id is a bare digest; a RepoDigest is `repository@digest`. Keep the digest half of both.
    return {digest for digest in (token.rpartition("@")[2] for token in out.split()) if digests.is_digest(digest)}


def resolve_pinned(profile: models.ExecutorProfile, *, runtime: str | None = None) -> tuple[str, str]:
    """(reference, problem): a reference *this engine* can run for the profile's pinned digest.

    A profile pins `repository@sha256:…`, and that is what the digest means — but it is not
    always a reference the local engine resolves. An image built here and never pushed has no
    repository digest on docker's classic image store, so the pinned form fails there even
    though the image is sitting in the store under exactly that digest as its Id. The digest
    alone is the same content address and always resolves, so fall back to it rather than
    telling someone to rebuild an image they already have. Empty reference: `problem` says why.
    """
    digest = profile.image_digest
    if not digests.is_digest(digest):
        return "", f"profile {profile.name!r} pins no image digest"
    engine = runtime or container_runtime()
    if engine is None:
        return "", "no container runtime on PATH to check the pinned image against"
    by_name = _local_digests(engine, profile.image)
    if digest in by_name:
        return profile.image, ""
    by_digest = _local_digests(engine, digest)
    if digest in by_digest:
        # Same content, a name the engine can resolve. The digest is the identity; the
        # repository half is a label, so a differently-tagged copy of it is still the pin.
        return digest, ""
    found = by_name | by_digest
    if found:
        return "", f"local image digest {', '.join(sorted(found))} does not match the pinned {digest}"
    # `--profile` names a packaged Containerfile, not this profile: `profile.name` here printed
    # `--profile quality`, which `build_image` rejects outright.
    return "", f"no local image {profile.image} — run `rein oci build --profile {profile.build_target}`"


def verify_pinned(profile: models.ExecutorProfile, *, runtime: str | None = None) -> tuple[bool, str]:
    """(ok, message): does a local image with the profile's pinned digest exist?

    Used by `rein oci verify`: a profile can pin a digest that no local image has (a
    stale pin, a machine that never built it), and running would then fail cryptically instead
    of saying "build it".
    """
    if not profile.is_sandboxed:
        return True, f"profile {profile.name!r} is a host profile — nothing to pin"
    reference, problem = resolve_pinned(profile, runtime=runtime)
    if problem:
        return False, problem
    digest = profile.image_digest
    return True, f"profile {profile.name!r} pinned image is present ({digest[:19]}…, runnable as {reference})"
