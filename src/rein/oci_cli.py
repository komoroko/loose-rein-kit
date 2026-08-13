"""`rein oci build|verify|list` — build the sandbox images locally and pin their digests.

The sandbox a review runs in has to be reproducible from the repository, not fetched from a
registry that could serve different bytes tomorrow (plan §10.2). So the Containerfiles ship in
the package, `build` builds one locally and prints the `sha256:` digest to pin into a config
profile, and `verify` checks that a profile's pinned digest matches a local image — turning
"the sandbox failed to start" into "build the image first".
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from rein import common, executors, models
from rein import repo as repo_mod

logger = logging.getLogger(__name__)


def _profiles_built_from(containerfile: str, repo_arg: str | None) -> list[str]:
    """The executor profiles this Containerfile is the image for, per their `containerfile:` key.

    The mapping is not the identity: `--profile python` builds the image the **quality** profile
    uses, so telling someone to pin it under `executor_profiles.python` sends them to a profile
    that does not exist. Falls back to the Containerfile's own name when there is no readable
    config to ask — a printed hint is not worth failing a successful build over.
    """
    try:
        repo = repo_mod.get(repo_arg)
        config = models.Config.parse(repo.path(".rein/config.yaml").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - any unreadable config just means we cannot narrow the hint
        return [containerfile]
    named = sorted(name for name, profile in config.profiles.items() if profile.containerfile == containerfile)
    return named or [containerfile]


def _custom_dockerfile(profile_name: str, repo_arg: str | None) -> tuple[repo_mod.Repo, Path] | None:
    """`(repo, path)` when `--profile profile_name` names a configured `dockerfile:` profile.

    None for anything else — no config, no such profile, or a profile that uses a packaged
    `containerfile:` instead — so the caller falls back to the packaged-Containerfile lookup
    exactly as before.
    """
    try:
        repo = repo_mod.get(repo_arg)
        config = models.Config.parse(repo.path(".rein/config.yaml").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - no readable config means nothing to route custom-built
        return None
    profile = config.profiles.get(profile_name)
    if profile is None or not profile.dockerfile:
        return None
    return repo, repo.path(profile.dockerfile)


def pin_profiles(text: str, pins: dict[str, str]) -> tuple[str, list[str]]:
    """Rewrite `config.yaml` so each named profile is `kind: oci` with `image:` set. Returns the text.

    Editing YAML as text rather than round-tripping it through a parser is deliberate: config.yaml
    is a document a human maintains, and every comment in it explains a decision — a re-emit would
    drop the lot. So this rewrites the profile's `kind:` line, replaces or inserts its `image:`
    line, and leaves every other byte alone.

    Commented lines are left commented. The shipped profiles carry a `# kind: oci` hint directly
    under the live `kind: host`, and uncommenting it as well produced two `kind` keys in one mapping
    — which strict_yaml rejects, so the write would have broken the file it was pinning.

    The second element is the profiles it could not find, so the caller can say which pins did not
    land rather than reporting a success that silently skipped one.
    """
    lines = text.splitlines()
    out: list[str] = []
    found: set[str] = set()
    active: str | None = None
    active_indent = 0
    wrote_image = False

    def key_of(stripped: str) -> str:
        return stripped.split(":", 1)[0] if ":" in stripped else ""

    for line in lines:
        stripped, indent = line.strip(), len(line) - len(line.lstrip())
        if active is not None and stripped and not stripped.startswith("#") and indent <= active_indent:
            active = None  # the next profile, or a new top-level key, ends this block
        if active is None and key_of(stripped) in pins and stripped.endswith(":"):
            active, active_indent, wrote_image = key_of(stripped), indent, False
            found.add(active)
            out.append(line)
            continue
        if active is not None and not stripped.startswith("#"):
            body = " " * (active_indent + 2)
            if key_of(stripped) == "kind":
                # `image` and `network_profile` go straight after `kind` so they land inside the
                # block whether or not the profile already had them — appending at the end of the
                # block would fall past a trailing comment. The schema requires both once `kind` is
                # `oci`, and `none` is the only network the runner will start a sandbox with.
                out.append(f"{body}kind: oci")
                out.append(f"{body}image: {pins[active]}")
                out.append(f"{body}network_profile: none")
                wrote_image = True
                continue
            if key_of(stripped) in ("image", "network_profile"):
                if key_of(stripped) == "image" and not wrote_image:
                    out.append(f"{body}image: {pins[active]}")
                    wrote_image = True
                continue
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), sorted(set(pins) - found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rein oci", description="build and verify the sandbox images")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="build a packaged Containerfile locally and print its digest")
    target = build.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--profile",
        help=(
            f"one of the packaged Containerfiles ({', '.join(executors.containerfile_names())}), "
            "or an executor_profiles name that sets `dockerfile:`"
        ),
    )
    target.add_argument("--all", action="store_true", help="build every packaged Containerfile")
    build.add_argument(
        "--write-config",
        action="store_true",
        help="pin the digests into .rein/config.yaml and flip those profiles to kind: oci",
    )

    sub.add_parser("verify", help="check that every OCI profile's pinned image is present locally")
    sub.add_parser("list", help="list the packaged Containerfiles")

    for name in ("build", "verify", "list"):
        sub.choices[name].add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")

    args = parser.parse_args(argv)
    common.configure_logging()

    if args.command == "list":
        print("\n".join(executors.containerfile_names()) or "(none)")
        return 0

    if args.command == "build":
        # Checked before the first build rather than discovered inside it: `build_image` would
        # otherwise spend the setup (temp dir, payload extraction) only to report the one thing
        # that was knowable up front, and `--all` would report it three times.
        if executors.container_runtime() is None:
            logger.error(
                "no container runtime on PATH — install docker or podman first. There is nothing here that "
                "can build an image, and until one exists the profiles keep running repository code on the host."
            )
            return 1

        pins: dict[str, str] = {}
        custom = None if args.all else _custom_dockerfile(args.profile, args.repo)
        if custom is not None:
            # `--profile <name>` named a configured `dockerfile:` profile, not a packaged
            # Containerfile — build from the repository instead of `data/oci/<name>/`.
            repo, dockerfile_path = custom
            rel = repo.rel(dockerfile_path) or str(dockerfile_path)
            print(f"[1/1] building '{args.profile}' from {rel} (this can take a few minutes)…", flush=True)
            try:
                digest = executors.build_image_from_dockerfile(dockerfile_path)
            except executors.ExecutorError as exc:
                logger.error(str(exc))
                return 1
            print(f"built {args.profile}\ndigest: {digest}")
            pins[args.profile] = f"localhost/rein-{args.profile}@{digest}"
        else:
            names = list(executors.containerfile_names()) if args.all else [args.profile]
            for index, name in enumerate(names, start=1):
                # Progress goes out before the build, not after. `common.run` captures the
                # engine's output, so a three-image build otherwise prints nothing for several
                # minutes and looks hung at exactly the moment a first-time user is least sure
                # it is working.
                print(f"[{index}/{len(names)}] building rein-{name} (this can take a few minutes)…", flush=True)
                try:
                    digest = executors.build_image(name)
                except executors.ExecutorError as exc:
                    logger.error(str(exc))
                    return 1
                image = f"localhost/rein-{name}@{digest}"
                print(f"built rein-{name}\ndigest: {digest}")
                for profile_name in _profiles_built_from(name, args.repo):
                    pins[profile_name] = image
        if not args.write_config:
            where = ", ".join(f"executor_profiles.{key}" for key in sorted(pins))
            print(f"\nPin these in .rein/config.yaml under {where}:")
            for key, pinned in sorted(pins.items()):
                print(f"  {key}.image: {pinned}")
            print("\nOr re-run with --write-config to have this command do it.")
            return 0
        try:
            repo = repo_mod.get(args.repo)
            path = repo.path(".rein/config.yaml")
            text, missing = pin_profiles(path.read_text(encoding="utf-8"), pins)
        except (repo_mod.RepoNotFoundError, OSError) as exc:
            logger.error(str(exc))
            return 1
        if missing:
            logger.error(f"no such profile(s) in config.yaml: {', '.join(sorted(missing))}")
            return 1
        # Parse before writing: a config this command corrupted would be a worse outcome than one
        # the human had to edit by hand, and `rein guard` rule 2 refuses it after gate 3 anyway.
        try:
            models.Config.parse(text)
        except models.DocumentError as exc:
            logger.error(f"refusing to write a config.yaml that no longer parses: {exc}")
            return 1
        path.write_text(text, encoding="utf-8")
        print(f"\npinned {', '.join(sorted(pins))} in .rein/config.yaml (kind: oci)")
        # Verify here rather than telling the human to run it. A pin that does not resolve is the
        # failure this command is most likely to leave behind, and "run `rein oci verify` next"
        # made confirming it an optional step that a first-time setup skips.
        print("\nverifying the pins:")
        return _verify(args.repo)

    return _verify(args.repo)


def _verify(repo_arg: str | None) -> int:
    """`rein oci verify`: does a local image actually exist for every pinned profile?"""
    from rein import store as store_mod

    try:
        repo = repo_mod.get(repo_arg)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    try:
        config = store_mod.Store(repo).read_config()
    except (models.DocumentError, store_mod.StoreError) as exc:
        logger.error(str(exc))
        return 1
    if config is None:
        logger.error("no .rein/config.yaml")
        return 1

    failures = 0
    for _name, profile in sorted(config.profiles.items()):
        ok, message = executors.verify_pinned(profile)
        print(f"  [{'PASS' if ok else 'FAIL'}] {message}")
        failures += 0 if ok else 1
    if not failures:
        print("\nevery profile's pinned image is present — run `rein doctor` for the rest.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
