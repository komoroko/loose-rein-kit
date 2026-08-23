"""`rein init` — seed a repository with Loose Rein state (greenfield and brownfield alike).

The copy-the-template model is gone: this command writes everything a repo needs *from the
package payload*, so the working tree gains only state — `.rein/` (SSOT + materialized
prompts/schema/rules + scaffold snapshot + lock) and `docs/` (deliverable scaffolds) — plus a
marker-guarded pointer block in AGENTS.md. Nothing else is touched: no pyproject rewrite, no
makefile, no agent surfaces (those are opt-in: `rein install <agent>`).

Brownfield is auto-detected (any existing code layout / build manifest at the root): the
seeded config scopes `guard.paths` to the docs deliverables only — pending gates must
not freeze development on existing code — fills the quality-gate test/check commands from the
repo's own tooling when recognizable (overridable with --test-cmd/--check-cmd), and the brief
carries the adopted-note pointing at /onboard. --greenfield/--brownfield override the
detection. Existing files are never overwritten (idempotent re-runs).

Usage:
  uvx --from git+<rein repo> rein init --name myproduct   # first contact
  rein init --name myproduct [--branch build/myproduct] [--source <url>]
  rein init                  # interactive wizard on a TTY (asks only name + brief)

The source URL (for `rein upgrade`) is auto-detected from this install's PEP 610 metadata
when --source is omitted; the work branch defaults to build/<name>, and the headless agent CLI
keeps its scaffold default (`claude -p`, switch later with `rein agent <cli>`).
"""

from __future__ import annotations

import argparse
import datetime
import logging
import re
import shlex
import sys
from collections.abc import Callable
from pathlib import Path

import rein
from rein import common, cycle
from rein import data as data_mod
from rein import install as install_mod
from rein import lock as lock_mod
from rein import repo as repo_mod

logger = logging.getLogger(__name__)

BRIEF_PATH = "docs/00-product-brief.md"

BRIEF_NOTE = (
    "\n> **Adopted into an existing codebase.** Write each cycle's brief as the *change* you want\n"
    "> (delta scope), not the whole product. Run /onboard first so docs/05-current-state.md maps\n"
    "> the existing implementation; /req and /design then start from that baseline and reuse\n"
    "> existing assets.\n"
)

# Root entries whose presence marks an existing codebase (brownfield). Directories the tool
# itself writes (.rein, docs, .claude, .github) deliberately don't count.
_CODE_MARKERS = (
    "src",
    "lib",
    "app",
    "backend",
    "frontend",
    "package.json",
    "pyproject.toml",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
)


# --- pure text surgery (under test) --------------------------------------------


def _cycle_slug(name: str) -> str:
    """A lowercase slug for the first cycle id (the schema pattern is `^[a-z0-9][a-z0-9-]*$`)."""
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return slug or "cycle-1"


def fill_state(text: str, project: str, cycle_id: str, today: str) -> str:
    """Fill state.yaml's placeholders by line surgery, keeping every explanatory comment."""
    text = re.sub(r'^(project: ").*(")', rf"\g<1>{project}\g<2>", text, count=1, flags=re.MULTILINE)
    text = re.sub(r"^(cycle_id: )\S+", rf"\g<1>{cycle_id}", text, count=1, flags=re.MULTILINE)
    return re.sub(r"^(updated_at: ).*$", rf'\g<1>"{today}"', text, count=1, flags=re.MULTILINE)


def fill_plan(text: str, cycle_id: str, branch: str) -> str:
    """Fill plan.yaml's cycle block. Everything else stays empty — the phases fill it."""
    text = re.sub(r"^(  id: )\S+", rf"\g<1>{cycle_id}", text, count=1, flags=re.MULTILINE)
    return re.sub(r"^(  branch: )\S+", rf"\g<1>{branch}", text, count=1, flags=re.MULTILINE)


def fill_config(text: str, project: str, branch: str) -> str:
    """Fill config.yaml's project block."""
    text = re.sub(r"^(  name: )\S+", rf"\g<1>{project}", text, count=1, flags=re.MULTILINE)
    return re.sub(r"^(  work_branch: )\S+", rf"\g<1>{branch}", text, count=1, flags=re.MULTILINE)


def disable_template_mode(text: str) -> str:
    return re.sub(r"^(\s*template_mode:\s*)true\b", r"\g<1>false", text, count=1, flags=re.MULTILINE)


def _argv_yaml(command: str) -> str:
    """A shell-ish command string rendered as a YAML argv list.

    The quality gate takes an argv array, never a shell string. Splitting here (rather than at
    run time) means the human sees exactly what will be executed, and a pipe they
    typed becomes visibly wrong in the config instead of silently mis-executed later.
    """
    parts = shlex.split(command)
    return "[" + ", ".join(f'"{part}"' for part in parts) + "]"


def brownfield_config(text: str, test_cmd: str, check_cmd: str) -> str:
    """Adapt the scaffold config.yaml for an existing repo (pure text surgery, comments survive)."""
    text = disable_template_mode(text)
    # Scope the guard to the docs deliverables only: a pending gate must not freeze normal
    # development on code that already exists. The commented lines show how to re-enable them.
    for key in ("src/", "lib/", "app/", "backend/", "frontend/", "scripts/"):
        text = re.sub(
            rf"^(    - \{{ path: {re.escape(key)}, requires_gate: tasks \}})$",
            r"    # \1   # re-enable (or map your layout) when ready",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    if test_cmd:
        text = _set_step_command(text, "test", test_cmd)
    if check_cmd:
        text = _set_step_command(text, "check", check_cmd)
    return text


def _set_step_command(text: str, step: str, command: str) -> str:
    """Replace the `command:` of the named quality-gate step, anchored on the step's **name**.

    Anchored on the name rather than on the argv it happens to ship with: this used to match the
    literal `command: [make, test]`, so the day that default changed the substitution would have
    silently done nothing and every brownfield repo would have been initialized with a DoD that
    ignored its own detected commands. A missing anchor raises instead — the input is the packaged
    scaffold, so it not being there means the scaffold moved, not that a user did something odd.
    """
    pattern = rf"(^  - name: {re.escape(step)}\n(?:    [^\n]*\n)*?    command: )\[[^\]]*\]$"
    replaced, count = re.subn(pattern, lambda m: m.group(1) + _argv_yaml(command), text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(
            f"the packaged scaffold config has no quality-gate step named {step!r} with a `command:` — "
            "init cannot write the detected command into a config whose shape it no longer recognises"
        )
    return replaced


def fill_brief(text: str, summary: str) -> str:
    """Insert the wizard's 1–3 lines under the brief's first section (pure).

    A no-op when the section already holds non-comment content (never overwrite the human's
    words) or when the heading is absent (a customized scaffold). The scaffold's example
    comment is kept — the summary lands right after it.
    """
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith("## What do you want to build"))
    except StopIteration:
        return text
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    if any(ln.strip() and not ln.lstrip().startswith("<!--") for ln in lines[start + 1 : end]):
        return text
    insert_at = start + 1
    if insert_at < end and lines[insert_at].lstrip().startswith("<!--"):
        insert_at += 1
    new_lines = lines[:insert_at] + [summary.strip()] + lines[insert_at:]
    return "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")


def detect_commands(files: dict[str, str]) -> dict[str, list[str]]:
    """Best-effort test/check command detection from a repo's root manifests (pure).

    `files` maps a root file name to its content ("" for presence-only markers). Returns
    candidate commands most-specific first; the caller takes the first of each.
    """
    import json as json_mod

    test: list[str] = []
    check: list[str] = []
    pkg = files.get("package.json")
    if pkg:
        try:
            scripts = json_mod.loads(pkg).get("scripts") or {}
        except ValueError:
            scripts = {}
        runner = "pnpm" if "pnpm-lock.yaml" in files else "yarn" if "yarn.lock" in files else "npm"
        if "test" in scripts:
            test.append(f"{runner} test" if runner != "npm" else "npm test")
        for name in ("lint", "check"):
            if name in scripts:
                check.append(f"{runner} run {name}")
                break
    pyproject = files.get("pyproject.toml")
    if pyproject:
        if "pytest" in pyproject:
            test.append("uv run pytest" if "uv.lock" in files else "pytest")
        if "ruff" in pyproject:
            check.append("ruff check .")
    if "Cargo.toml" in files:
        test.append("cargo test")
        check.append("cargo clippy -- -D warnings")
    if "go.mod" in files:
        test.append("go test ./...")
        check.append("go vet ./...")
    makefile = files.get("makefile") or files.get("Makefile")
    if makefile:
        targets = set(re.findall(r"^([A-Za-z][\w-]*):", makefile, flags=re.MULTILINE))
        if "test" in targets:
            test.append("make test")
        for name in ("check", "lint"):
            if name in targets:
                check.append(f"make {name}")
                break
    return {"test": test, "check": check}


def is_brownfield(root: Path) -> bool:
    """True when the root already carries a codebase (see _CODE_MARKERS)."""
    return any((root / marker).exists() for marker in _CODE_MARKERS)


def source_from_direct_url(raw: str) -> str:
    """Reconstruct a `git+<url>[@rev]` source from a PEP 610 direct_url.json body (pure).

    A VCS install records `url` + `vcs_info`; a plain file/dir/editable install (`dir_info`) or
    an archive has no VCS coordinates, so there is nothing to record and we return "". Anything
    malformed also yields "" — a missing source is a benign gap, never a failure.
    """
    import json as json_mod

    try:
        data = json_mod.loads(raw)
    except ValueError:
        return ""
    vcs = data.get("vcs_info") if isinstance(data, dict) else None
    url = data.get("url") if isinstance(data, dict) else None
    if not isinstance(vcs, dict) or not isinstance(url, str) or not url:
        return ""
    base = url if url.startswith(("git+", "hg+", "bzr+", "svn+")) else f"{vcs.get('vcs', 'git')}+{url}"
    rev = vcs.get("requested_revision") or vcs.get("commit_id")
    return f"{base}@{rev}" if isinstance(rev, str) and rev else base


def detect_source() -> str:
    """Best-effort recovery of the rein source URL from this install's PEP 610 metadata.

    Returns "" when the metadata is absent (e.g. a source tree run) or the install is not from a
    VCS — the same silent skip the wizard's old free-text source question produced on Enter.
    """
    import importlib.metadata as md

    try:
        raw = md.distribution("rein").read_text("direct_url.json")
    except (md.PackageNotFoundError, OSError):
        return ""
    return source_from_direct_url(raw) if raw else ""


# --- application ------------------------------------------------------------------


def _root_files(root: Path) -> dict[str, str]:
    """Root manifests for detect_commands: name -> content (best-effort reads)."""
    out: dict[str, str] = {}
    for name in ("package.json", "pyproject.toml", "makefile", "Makefile"):
        try:
            out[name] = (root / name).read_text(encoding="utf-8")
        except OSError:
            continue
    for name in ("pnpm-lock.yaml", "yarn.lock", "uv.lock", "Cargo.toml", "go.mod"):
        if (root / name).exists():
            out[name] = ""
    return out


def _seed(root: Path, rel: str, content: bytes) -> bool:
    """Write a seed file unless it already exists. Returns True when written."""
    dest = root / rel
    if dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    return True


def sandbox_step(root: Path, *, offer: bool, ask: Callable[[str], str] | None = None) -> str:
    """Set the sandboxes up, or say exactly what is owed. Returns a line for the summary.

    Sandboxing is a precondition for everything the lifecycle does — `/build` runs repository
    code, and a fresh config ships `kind: host`, which is not policy-compliant — yet it used to
    surface only later, as a `doctor` FAIL. Making it a step of initialization turns it into a
    question with a default.

    `offer` gates the interactive path: on a TTY we ask (default no — it is a multi-minute
    build), otherwise we only print the command. Either way the repository is left usable.
    """
    from rein import executors, models, oci_cli
    from rein import store as store_mod

    try:
        config = store_mod.Store(repo_mod.Repo(root)).read_config()
    except (models.DocumentError, store_mod.StoreError, OSError):
        return "sandbox: could not read .rein/config.yaml — run `rein doctor`"
    if config is None or not config.unsandboxed_code_profiles():
        return "sandbox: every profile that runs repository code is already sandboxed"

    command = config.sandbox_setup_command()
    if executors.container_runtime() is None:
        return (
            "sandbox: docker/podman not found — install one, then run "
            f"`{command}` (until then, repository code would run on this host)"
        )
    if not offer or ask is None:
        return f"sandbox: not built yet — run `{command}`"

    print(f"\nSandbox: {', '.join(config.unsandboxed_code_profiles())} would run repository code on this host.")
    print(f"  Building the packaged images fixes it: `{command}`. It takes a few minutes.")
    if (ask("  build them now? [y/N]") or "n").strip().lower() not in ("y", "yes"):
        return f"sandbox: skipped — run `{command}` when you are ready"
    rc = oci_cli.main(["build", "--all", "--write-config", "--repo", str(root)])
    return "sandbox: built and pinned" if rc == 0 else f"sandbox: build failed (rc={rc}) — re-run `{command}`"


def _switch_branch(root: Path, branch: str) -> str:
    """Create/switch to the work branch (best-effort). Returns a status line for the summary."""
    # symbolic-ref, not rev-parse --abbrev-ref: a fresh `git init` with no commits yet has no
    # HEAD commit to resolve, but its branch ref is real — rev-parse would misreport that repo
    # as absent, exactly the state a greenfield `rein init` most commonly runs in.
    rc, out = common.run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=str(root))
    if rc != 0:
        return f"git: not a repository — run `git init && git switch -c {branch}` yourself"
    if out.strip() == branch:
        return f"git: already on {branch}"
    rc, _ = common.run(["git", "switch", "-c", branch], cwd=str(root))
    if rc == 0:
        return f"git: created and switched to {branch}"
    rc, out = common.run(["git", "switch", branch], cwd=str(root))
    if rc == 0:
        return f"git: switched to existing {branch}"
    return f"git: could not switch to {branch} — {out.strip().splitlines()[-1] if out.strip() else 'unknown error'}"


def run_init(
    root: Path,
    name: str,
    branch: str,
    source: str,
    *,
    test_cmd: str = "",
    check_cmd: str = "",
    mode: str = "auto",
    offer_sandbox: bool = False,
) -> int:
    """Seed the repo (SSOT + docs scaffolds + materialized artifacts + pointer block + lock)."""
    today = datetime.date.today().isoformat()
    root = root.resolve()
    brownfield = is_brownfield(root) if mode == "auto" else (mode == "brownfield")
    flavor = "brownfield (existing codebase detected)" if brownfield else "greenfield"
    print(f"init: {flavor}")

    if brownfield and not (test_cmd and check_cmd):
        detected = detect_commands(_root_files(root))
        test_cmd = test_cmd or (detected["test"][0] if detected["test"] else "")
        check_cmd = check_cmd or (detected["check"][0] if detected["check"] else "")
        for kind, cmd in (("test", test_cmd), ("check", check_cmd)):
            if cmd:
                print(f'  detected      quality-gate {kind} command: "{cmd}"')

    # An unset source is recovered from how this tool was installed (PEP 610), so the wizard
    # need not ask the human to paste a URL they rarely know; a VCS install yields git+<url>,
    # an editable/local install yields "".
    if not source:
        source = detect_source()
        if source:
            print(f"  detected      source: {source}")

    # 1) the four SSOT documents, placeholder-filled (never overwriting an existing file).
    cycle_id = _cycle_slug(name)
    state_text = fill_state(data_mod.read_text("scaffold/rein/state.yaml"), name, cycle_id, today)
    plan_text = fill_plan(data_mod.read_text("scaffold/rein/plan.yaml"), cycle_id, branch)
    config_text = fill_config(data_mod.read_text("scaffold/rein/config.yaml"), name, branch)
    if brownfield:
        config_text = brownfield_config(config_text, test_cmd, check_cmd)
    else:
        config_text = disable_template_mode(config_text)
    seeds: list[tuple[str, bytes]] = [
        (".rein/state.yaml", state_text.encode()),
        (".rein/plan.yaml", plan_text.encode()),
        (".rein/review.yaml", data_mod.read_bytes("scaffold/rein/review.yaml")),
        (".rein/config.yaml", config_text.encode()),
    ]
    # 2) the docs scaffolds (with the brownfield note on the brief).
    for rel, blob in data_mod.iter_files("scaffold/docs"):
        dest_rel = "docs/" + rel[len("scaffold/docs/") :]
        if brownfield and dest_rel == BRIEF_PATH:
            blob = blob + BRIEF_NOTE.encode()
        seeds.append((dest_rel, blob))
    seeded: dict[str, str] = {}
    for rel, blob in seeds:
        wrote = _seed(root, rel, blob)
        print(f"  {'seed' if wrote else 'skip':<13} {rel}{'' if wrote else '  (already exists — left untouched)'}")
        if wrote:
            seeded[rel] = lock_mod.norm_hash(blob)
    (root / "docs" / "notes").mkdir(parents=True, exist_ok=True)

    repo = repo_mod.Repo(root)
    # 3) the materialized artifacts (prompts/schema/rules) + the lock skeleton they update.
    lock_data = lock_mod.read(repo.lock) or lock_mod.new(rein.__version__, source)
    if source:
        lock_data.setdefault("rein", {})["source"] = source
    existing_seeded = lock_data.get("seeded") if isinstance(lock_data.get("seeded"), dict) else {}
    lock_data["seeded"] = {**(existing_seeded or {}), **seeded}
    lock_mod.write(repo.lock, lock_data)
    rc = install_mod.sync(repo)
    if rc != 0:
        return rc
    # 4) the pristine scaffold snapshot cycle-close restores from.
    if cycle.snapshot_scaffold(repo):
        print(f"  snapshot      pristine docs + SSOT → {cycle.SCAFFOLD_DOCS}")
    # 5) the agent-neutral rules pointer (AGENTS.md), appended at most once.
    agents_md = root / "AGENTS.md"
    text = agents_md.read_text(encoding="utf-8") if agents_md.is_file() else "# Repository rules\n"
    if install_mod.CLAUDE_IMPORT_MARKER not in text:
        agents_md.write_text(text + install_mod.agents_pointer_block(), encoding="utf-8")
        print("  merge         AGENTS.md (Loose Rein pointer block appended)")
    print(f"  {_switch_branch(root, branch)}")
    # 6) the sandboxes — offered here rather than left for `doctor` to find later.
    print(f"  {sandbox_step(root, offer=offer_sandbox, ask=_ask if offer_sandbox else None)}")

    next_step = (
        "Next: run /onboard to map the existing code into docs/05-current-state.md, then start with /req."
        if brownfield
        else "Next: write a few lines into docs/00-product-brief.md and start with /req."
    )
    print(
        f'\nInitialized "{name}" (work branch: {branch}; the gate guard is live).\n'
        "Add an agent surface when you want one: `rein install claude` / `rein install copilot`.\n" + next_step
    )
    return 0


# --- interactive wizard (`rein init` / `rein start` on a TTY) ------------


def _ask(prompt: str, default: str = "") -> str:
    shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
    return input(shown).strip() or default


def _ask_brief() -> str:
    print("2/2 What do you want to build? (1-3 lines for docs/00-product-brief.md;")
    lines: list[str] = []
    while len(lines) < 3:
        line = input("  empty line to finish, Enter now to skip: " if not lines else "  ").strip()
        if not line:
            break
        lines.append(line)
    return "\n".join(lines)


def wizard(root: Path | None = None) -> int:
    """Interactive first-run setup: ask everything first, then write (Ctrl+C mid-question loses nothing)."""
    common.configure_logging()
    root = (root or Path.cwd()).resolve()
    print("Loose Rein setup — Enter accepts the [default]; Ctrl+C aborts without writing.")
    try:
        name = _ask("1/2 product name", root.name)
        summary = _ask_brief()
    except (KeyboardInterrupt, EOFError):
        logger.error("\naborted — nothing was written.")
        return 130
    # branch defaults to build/<name>; source is auto-detected in run_init; the headless CLI keeps
    # its scaffold default (claude -p) — all three are overridable later (flags / `rein agent`).
    rc = run_init(root, name, f"build/{name}", "", offer_sandbox=True)
    if rc != 0:
        return rc
    if summary:
        brief = root / BRIEF_PATH
        try:
            brief.write_text(fill_brief(brief.read_text(encoding="utf-8"), summary), encoding="utf-8")
            print(f"  updated: {BRIEF_PATH} (your summary — flesh it out anytime)")
        except OSError as exc:
            logger.error(f"could not write {BRIEF_PATH}: {exc} — add your summary there by hand.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rein init", description="seed this repository with Loose Rein state")
    parser.add_argument("--name", default="", help="the product name (state.md project)")
    parser.add_argument("--branch", default="", help="the work branch (default: build/<name>)")
    parser.add_argument(
        "--source", default="", help="the rein source URL for the lock (default: auto-detected from the install)"
    )
    parser.add_argument("--test-cmd", default="", help="quality-gate test command (brownfield; else auto-detected)")
    parser.add_argument("--check-cmd", default="", help="quality-gate check command (brownfield; else auto-detected)")
    flavor = parser.add_mutually_exclusive_group()
    flavor.add_argument("--greenfield", action="store_true", help="skip the brownfield auto-detection")
    flavor.add_argument("--brownfield", action="store_true", help="force the brownfield adaptations")
    parser.add_argument("--repo", default=None, help="directory to initialize (default: cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    root = Path(args.repo).resolve() if args.repo else Path.cwd()
    name = args.name.strip()
    if not name:
        if sys.stdin.isatty():
            return wizard(root)
        logger.error("usage: rein init --name <product> [--branch build/<product>] (or run on a TTY for the wizard)")
        return 2
    branch = args.branch.strip() or f"build/{name}"
    mode = "greenfield" if args.greenfield else "brownfield" if args.brownfield else "auto"
    return run_init(
        root,
        name,
        branch,
        args.source.strip(),
        test_cmd=args.test_cmd.strip(),
        check_cmd=args.check_cmd.strip(),
        mode=mode,
    )
