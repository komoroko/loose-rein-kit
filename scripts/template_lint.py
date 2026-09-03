"""Self-consistency canaries for the template repo (drift checks across hand-maintained files).

The always-loaded rules (AGENTS.md), the per-phase procedures, the bilingual READMEs, and the
code that parses the machine-read vocabulary are maintained by hand in parallel. The classic
failure is a rename or addition that lands in one file and silently drifts the rest — e.g. a
new make target documented only in README.md, or a task-status value renamed in dag.py but
not in the /tasks procedure. Exact byte comparison is impossible across a translation or
between prose and code, so these checks are *canaries*: they assert the load-bearing
vocabulary and structure survive verbatim in every file that reads them. A tripped canary
usually means "propagate the change everywhere", not "revert the change".

Template-repo only: after `rein init` flips `guard.template_mode` to false, a product owns
its READMEs (and may replace them wholesale), so `main()` skips unless this repo IS the
template. tests/test_template_lint.py runs the same checks against the live repo as part of
the normal `make test`, which is how CI catches a drifting commit.

This lives in `scripts/`, not in the installed `rein` package: it is the template repository's
own maintenance tool, and a product that `uv tool install`s the CLI has no use for a canary
that checks the template's own bilingual READMEs and wrapper parity — shipping it in every
release would be dead weight in every product that isn't this one.

Usage:
  uv run --frozen python scripts/template_lint.py
"""

from __future__ import annotations

import logging
import re
from functools import cache
from itertools import zip_longest
from pathlib import Path

from rein import common, dag, gate_guard, gitignore, install, models, strict_yaml

# Not `logging.getLogger(__name__)`: this script is run directly (`__name__ == "__main__"`,
# makefile's `template-lint` target) as often as it is imported, and `common.configure_logging()`
# only attaches its stderr handler to the `"rein"` logger namespace. A name outside that
# hierarchy would print nothing on either path.
logger = logging.getLogger("rein.template_lint")

AGENTS_MD = "AGENTS.md"
TASKS_CMD = ".rein/prompts/commands/tasks.md"
BUILD_CMD = ".rein/prompts/commands/build.md"
RULES_DIR = ".rein/prompts/rules"
COMMANDS_DIR = ".rein/prompts/commands"
CONFIG_PATH = ".rein/config.yaml"
CLAUDE_MAPPING = "CLAUDE.md"
COPILOT_MAPPING = ".github/instructions/rein.instructions.md"
CODEX_MAPPING = ".codex/rein.md"
GEMINI_MAPPING = ".gemini/rein.md"
#: Every per-host capability mapping. A host added to `_WRAPPER_SETS` and not to this tuple gets
#: its wrappers checked and its capability table checked by nobody.
CAPABILITY_MAPPINGS: tuple[str, ...] = (CLAUDE_MAPPING, COPILOT_MAPPING, CODEX_MAPPING, GEMINI_MAPPING)

# The shared procedure/role bodies and their per-agent thin wrappers. Each body must have a
# wrapper in every dialect, and each wrapper must reference its body — check_wrapper_parity.
_WRAPPER_SETS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        ".rein/prompts/commands",
        (
            (".claude/commands", "{stem}.md"),
            (".github/prompts", "{stem}.prompt.md"),
            (".agents/skills", "{stem}/SKILL.md"),
            (".gemini/commands", "{stem}.toml"),
        ),
    ),
    (
        ".rein/prompts/agents",
        (
            (".claude/agents", "{stem}.md"),
            (".github/agents", "{stem}.agent.md"),
            (".codex/agents", "{stem}.toml"),
            (".gemini/skills", "{stem}/SKILL.md"),
        ),
    ),
)

# Only backticked command mentions count — prose like "make tasks visible" or "rein
# repositories" must not. make survives for the package's own dev targets (check/test/...).
_MAKE_MENTION_RE = re.compile(r"`make (?:-f \S+ )?([a-z][a-z0-9_-]*)")
_REIN_MENTION_RE = re.compile(r"`rein ([a-z][a-z0-9-]*)")
_SCRIPT_MENTION_RE = re.compile(r"src/rein/(\w+\.py)")
# A whole backticked `rein …` command line, for check_documented_invocations. Placeholders
# (`<gate>`, `[--check]`, `T-NNN`) ride along; only the `--flags` inside are checked.
_REIN_INVOCATION_RE = re.compile(r"`rein ([^`\n]+)`")
_FLAG_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)")
# A capability token is the backticked kebab word opening a mapping-table row.
_CAPABILITY_ROW_RE = re.compile(r"^\|\s*`([a-z][a-z-]+)`\s*\|", re.MULTILINE)
# `description:` in YAML frontmatter, `description = "…"` in a Codex subagent's TOML.
_DESCRIPTION_RE = re.compile(r"^description\s*[:=]\s*(.+?)\s*$", re.MULTILINE)
_TOML_ESCAPE_RE = re.compile(r"\\(.)")
# uv.lock's entry for this project itself — `[[package]]` / name / version, in that order.
_LOCK_PROJECT_RE = re.compile(r'^name = "loose-rein-kit"\nversion = "([^"]+)"', re.MULTILINE)

#: `.rein/rein.lock`'s two version stamps: the release that wrote the repo, and the one that wrote
#: the materialized prompts. Read as text — the lock is machine-written YAML and this canary must
#: keep working on a lock that no longer parses as anything else.
_REIN_LOCK_TOOL_RE = re.compile(r"^tool_version:\s*['\"]?([^'\"\s]+)", re.MULTILINE)
_REIN_LOCK_PROMPTS_RE = re.compile(r"^prompts:\n(?:[ \t]+.*\n)*?[ \t]+version:\s*['\"]?([^'\"\s]+)", re.MULTILINE)
# Claude-only mechanism names must never leak into the agent-neutral files — they belong in
# the capability mappings alone. A leak means a neutral body regressed to one agent's dialect.
_CLAUDE_ONLY_TERMS = ("AskUserQuestion", "PushNotification", "ExitPlanMode")
# A rules-module mention is the full materialized path — the form the wiring lines use.
_RULES_REF_RE = re.compile(r"\.rein/prompts/rules/([a-z0-9-]+\.md)")
# Gate-weakening bypasses this release refuses to carry (plan §4.1) — the documentation side of
# `policy_check._BANNED_KEYS`. Their appearance in a neutral operating document means the
# vocabulary regressed to the pre-grounding model — the same drift check_vocabulary guards, but
# for terms that must be *absent*, not merely present.
_BANNED_TERMS: tuple[str, ...] = (
    "approve --force",
    "enforce_hook",
    "build.headless.cmd",
    "schema_version",
    "--refresh-state",
)


def _require(text: str, path: str, terms: list[str], what: str) -> list[str]:
    return [f"{path}: missing {what} `{t}`" for t in terms if t not in text]


def gate_names() -> list[str]:
    """The canonical gate list (models.GATE_ORDER) the prose must echo verbatim."""
    return sorted(models.GATE_ORDER)


def quality_gate_steps(config_text: str) -> list[str]:
    """The DoD step names from config.yaml — defined once there, echoed by AGENTS.md."""
    return [step.name for step in models.Config.parse(config_text).quality_gate if step.name]


def check_vocabulary(files: dict[str, str]) -> list[str]:
    """Assert the machine-read vocabulary appears verbatim in the prose that teaches it.

    dag.py's value sets are what `--validate` enforces on tasks.yaml; state.md's gate keys are
    what gate_guard.py and revise.py act on; config's step names are the single DoD definition.
    If any of these is renamed without updating AGENTS.md / the /tasks procedure, the agent is
    taught vocabulary the code rejects — this is the drift these canaries trip on.
    """
    failures: list[str] = []
    kinds = sorted(dag.KIND_VALUES)
    failures += _require(files[AGENTS_MD], AGENTS_MD, kinds, "task kind (dag.KIND_VALUES)")
    failures += _require(files[TASKS_CMD], TASKS_CMD, kinds, "task kind (dag.KIND_VALUES)")
    failures += _require(files[TASKS_CMD], TASKS_CMD, sorted(dag.STATUS_VALUES), "task status (dag.STATUS_VALUES)")
    failures += _require(files[AGENTS_MD], AGENTS_MD, gate_names(), "gate (models.GATE_ORDER)")
    # The DoD step names are defined once (config.yaml) but narrated in several prose homes —
    # every copy must keep echoing them, or a renamed step teaches stale vocabulary somewhere.
    steps = quality_gate_steps(files[CONFIG_PATH])
    for path in (AGENTS_MD, BUILD_CMD, "README.md", "README.ja.md"):
        failures += _require(files[path], path, steps, "quality-gate step (config.yaml)")
    return failures


def _description(text: str) -> str:
    """The wrapper's one-liner, in whichever dialect it is written.

    A TOML basic string arrives with its quotes and its escapes; both are stripped so that the
    three hosts' values compare as the same sentence — which is the only thing parity is about.
    A description containing a quotation mark (`"options with trade-offs"`) is exactly where a
    byte comparison across dialects would otherwise report drift that is not there.
    """
    m = _DESCRIPTION_RE.search(text)
    if not m:
        return ""
    value = m.group(1)
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = _TOML_ESCAPE_RE.sub(r"\1", value[1:-1])
    return value


def _wrapper_stems(base: Path, pattern: str) -> set[str]:
    """The stems `base` holds, for a pattern naming a file (`{stem}.md`) or a file inside a
    per-stem directory (`{stem}/SKILL.md`) — a Codex skill is a directory, not a file."""
    prefix, _, suffix = pattern.partition("{stem}")
    return {
        rel[len(prefix) : len(rel) - len(suffix)]
        for rel in (p.relative_to(base).as_posix() for p in base.glob(pattern.format(stem="*")))
    }


def check_wrapper_parity(root: Path) -> list[str]:
    """Every shared body has a wrapper in every dialect; every wrapper points at a real body.

    The bodies in .rein/prompts/ are the single procedure source; .claude/*, .github/*,
    and Codex's .agents/skills + .codex/agents are thin wrappers. The drift this trips on: a new
    phase/role added in one place only, a wrapper whose body reference went stale after a rename,
    or the dialects' descriptions diverging (they must stay the same one-liner). A host that is
    not on this list is a host the next new phase silently skips — which is how a host quietly
    degrades into a second-class citizen.
    """
    failures: list[str] = []
    for body_dir, wrappers in _WRAPPER_SETS:
        stems = sorted(p.stem for p in (root / body_dir).glob("*.md"))
        if not stems:
            failures.append(f"{body_dir}: no shared bodies found")
            continue
        descriptions: dict[str, dict[str, str]] = {}
        for wrapper_dir, pattern in wrappers:
            found = _wrapper_stems(root / wrapper_dir, pattern)
            for stem in sorted(set(stems) - found):
                failures.append(f"{wrapper_dir}: missing wrapper {pattern.format(stem=stem)} for {body_dir}/{stem}.md")
            for stem in sorted(found - set(stems)):
                failures.append(f"{wrapper_dir}/{pattern.format(stem=stem)}: no shared body {body_dir}/{stem}.md")
            for stem in sorted(set(stems) & found):
                text = (root / wrapper_dir / pattern.format(stem=stem)).read_text(encoding="utf-8")
                body_ref = f"{body_dir}/{stem}.md"
                if body_ref not in text:
                    failures.append(f"{wrapper_dir}/{pattern.format(stem=stem)}: does not reference {body_ref}")
                descriptions.setdefault(stem, {})[wrapper_dir] = _description(text)
        for stem, per_dir in sorted(descriptions.items()):
            if len(set(per_dir.values())) > 1:
                failures.append(f"wrapper descriptions for `{stem}` differ across {', '.join(sorted(per_dir))}")
    return failures


_ADAPTER_KEY_RE = re.compile(r'^    "([a-z0-9][a-z0-9_-]*)": Adapter\(', re.MULTILINE)

#: Where a human is told which agent CLIs this release can launch. Prose lists of them have gone
#: stale twice — README claimed "a custom command" worked when `launch_refusal` refuses one, and
#: `rein uninstall`'s own error named two of the four integrations — so the list is a canary now
#: rather than something everyone remembers to update.
_ADAPTER_LISTS: tuple[str, ...] = (
    "README.md",
    "README.ja.md",
    ".github/instructions/rein.instructions.md",
    ".codex/rein.md",
    ".gemini/rein.md",
)


def check_adapter_lists(root: Path) -> list[str]:
    """Every place that lists the launchable agent CLIs lists all of them.

    The names come from `ADAPTER_TABLE` itself, so adding an adapter and forgetting a document is
    a failure here rather than a sentence that is quietly wrong until somebody follows it.
    """
    source = (root / "src/rein/adapters.py").read_text(encoding="utf-8")
    names = set(_ADAPTER_KEY_RE.findall(source))
    if not names:
        return ["src/rein/adapters.py: no adapters found — the canary cannot check what it cannot read"]
    failures = []
    for rel in _ADAPTER_LISTS:
        # Inside inline code spans only: an adapter is named as `codex` or as `rein agent codex`,
        # and matching bare prose would let the word "cursor" in a sentence about text cursors
        # pass for a mention of the CLI. Fenced blocks come out first — an odd number of fences
        # between two mentions repairs the backtick pairing into nonsense.
        text = re.sub(r"```.*?```", " ", (root / rel).read_text(encoding="utf-8"), flags=re.DOTALL)
        spans = " ".join(re.findall(r"`([^`\n]+)`", text))
        missing = sorted(name for name in names if not re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", spans))
        if missing:
            failures.append(f"{rel}: never names the launchable adapter(s) {', '.join(missing)}")
    return failures


def check_capability_mapping(mappings: dict[str, str], agents_text: str) -> list[str]:
    """Every capability mapping covers the same token set, and AGENTS.md defines every token.

    The mapping tables (CLAUDE.md, the Copilot instructions file, the Codex one) are
    hand-maintained mirrors; the vocabulary itself lives in AGENTS.md. A capability added to one
    mapping only — or one that AGENTS.md never defines — is the drift.

    Keyed by path rather than fixed to two arguments so that adding a host adds a mapping here
    and nothing else: a mapping this function does not receive is a mapping no canary holds,
    which is how a new host drifts quietly — the very thing `check_wrapper_parity` was widened
    to stop.
    """
    failures: list[str] = []
    per_path = {path: set(_CAPABILITY_ROW_RE.findall(text)) for path, text in mappings.items()}
    union: set[str] = set().union(*per_path.values()) if per_path else set()
    for path, tokens in sorted(per_path.items()):
        elsewhere = sorted(t for t in union - tokens)
        for token in elsewhere:
            others = ", ".join(sorted(p for p, ts in per_path.items() if token in ts))
            failures.append(f"{path}: missing capability `{token}` (mapped in {others})")
    for token in sorted(union):
        if f"`{token}`" not in agents_text:
            failures.append(f"{AGENTS_MD}: capability `{token}` is mapped but never defined here")
    return failures


def neutral_texts(root: Path) -> dict[str, str]:
    """The agent-neutral files the dialect canary scans: AGENTS.md, the shared bodies, and the
    docs scaffolds (docs/notes/ and docs/archive/ are records, not scaffolds — Claude mentions
    there are legitimate)."""
    texts = {AGENTS_MD: (root / AGENTS_MD).read_text(encoding="utf-8")}
    scans: tuple[tuple[Path, tuple[str, ...]], ...] = (
        (root / ".rein" / "prompts", ()),
        (root / "docs", ("notes", "archive")),
        # The packaged scaffold and the per-agent integration bodies are what a *product* repo
        # gets. Leaving them unscanned would let banned vocabulary survive in the files new repos
        # are seeded from, which is the one place it does the most damage.
        (root / "src" / "rein" / "data" / "scaffold", ()),
        (root / "src" / "rein" / "data" / "integrations", ()),
    )
    for base, excluded in scans:
        for path in sorted(base.rglob("*.md")):
            rel = path.relative_to(root)
            if len(rel.parts) > 1 and rel.parts[1] in excluded:
                continue
            texts[rel.as_posix()] = path.read_text(encoding="utf-8")
    return texts


def check_neutral_vocabulary(texts: dict[str, str]) -> list[str]:
    """No Claude-only mechanism name may appear in the agent-neutral files (AGENTS.md, bodies, scaffolds)."""
    failures: list[str] = []
    for path, text in sorted(texts.items()):
        for term in _CLAUDE_ONLY_TERMS:
            if term in text:
                failures.append(f"{path}: Claude-only mechanism `{term}` leaked into a neutral file")
    return failures


def check_banned_absence(texts: dict[str, str]) -> list[str]:
    """No gate-weakening bypass may be named in a neutral operating document.

    `policy_check` refuses these keys in config at CI time; this is the documentation-side
    companion, so a rule body still telling a reader to pass `approve --force` is caught as drift
    instead of silently teaching a workflow the tool refuses to honour.

    There is no exemption. A register of what may not come back has to be able to name it, but no
    such register survives here — every remaining mention in a scanned file is an instruction to a
    reader.
    """
    failures: list[str] = []
    for path, text in sorted(texts.items()):
        for term in _BANNED_TERMS:
            if term in text:
                failures.append(f"{path}: banned bypass `{term}` survives — it weakens a gate (plan §4.1)")
    return failures


def check_rules_wiring(root: Path, texts: dict[str, str]) -> list[str]:
    """Every rules module is read by a command body, and every rules reference resolves.

    The modules in .rein/prompts/rules/ (phase-scoped rules split out of AGENTS.md) are
    loaded only through an explicit "read .rein/prompts/rules/<x>.md" line in a command
    body — nothing auto-loads them. The drift this trips on: a module no command reads (it is
    silently never loaded), or a reference left pointing at a renamed/deleted module. A repo
    with no rules/ directory and no references is healthy (products may trim the modules).
    """
    modules = {p.name for p in (root / RULES_DIR).glob("*.md")}
    failures: list[str] = []
    read_by_commands: set[str] = set()
    for path, text in sorted(texts.items()):
        for name in _RULES_REF_RE.findall(text):
            if path.startswith(COMMANDS_DIR + "/"):
                read_by_commands.add(name)
            if name not in modules:
                failures.append(f"{path}: references {RULES_DIR}/{name} which does not exist")
    for name in sorted(modules - read_by_commands):
        failures.append(f"{RULES_DIR}/{name}: not read by any command body (orphan module)")
    return failures


def check_readme_parity(en: str, ja: str) -> list[str]:
    """Structural canaries between the bilingual READMEs (byte-compare is impossible across a translation).

    A `##` section, a make target, or a script added to one language and not the other is the
    drift; the sets/counts below are language-independent, so they must match exactly.
    """
    failures: list[str] = []
    n_en = len(re.findall(r"^## ", en, re.MULTILINE))
    n_ja = len(re.findall(r"^## ", ja, re.MULTILINE))
    if n_en != n_ja:
        failures.append(f"README.md has {n_en} `##` sections but README.ja.md has {n_ja}")
    for what, pattern in (
        ("make-target", _MAKE_MENTION_RE),
        ("rein-verb", _REIN_MENTION_RE),
        ("script", _SCRIPT_MENTION_RE),
    ):
        only_en = set(pattern.findall(en)) - set(pattern.findall(ja))
        only_ja = set(pattern.findall(ja)) - set(pattern.findall(en))
        for name in sorted(only_en):
            failures.append(f"README.ja.md: missing {what} mention `{name}` (present in README.md)")
        for name in sorted(only_ja):
            failures.append(f"README.md: missing {what} mention `{name}` (present in README.ja.md)")
    return failures


def _declares_flag(source: str, flag: str) -> bool:
    """Does `source` declare `flag` as an option?

    A quoted occurrence is the test, not an `add_argument` call: `gate_guard` reads its argv by
    hand on purpose (a hook that dies in argparse prints no decision, and every host reads no
    decision as allow). A prose mention in a docstring is backticked, not quoted, so it does not
    count as a declaration.
    """
    return re.search(rf"""["']{re.escape(flag)}["']""", source) is not None


def check_documented_invocations(root: Path, texts: dict[str, str]) -> list[str]:
    """Every ``rein …`` command line a document tells an agent to run must actually parse.

    The procedures are executed literally by an agent that cannot see the argparse definitions,
    so a flag that was renamed in code and not in the procedure does not read as a typo — it
    reads as a broken repository the agent then tries to repair. (Two shipped procedures once
    told the agent to run `rein dag --trace --require-design`, a flag that never existed;
    `--test-plan` was the same. Both survived every other canary in this file.)

    Static on purpose: it reads the option strings out of each verb's module rather than
    importing and running it, so a canary can never execute a command it is only checking.
    """
    from rein import cli

    known_verbs = set(cli.VERBS) | {"help"}
    sources: dict[str, str] = {}
    failures: list[str] = []
    for path, text in sorted(texts.items()):
        for match in _REIN_INVOCATION_RE.finditer(text):
            words = match.group(1).split()
            if words[:1] == ["--repo"]:  # the global flag may precede the verb
                words = words[2:]
            verb = next((w for w in words if not w.startswith("-")), "")
            if not verb or "<" in verb:  # `rein <verb> [args]` is a shape, not an invocation
                continue
            if verb not in known_verbs:
                failures.append(f"{path}: `rein {verb}` is not a verb — see cli.VERBS")
                continue
            entry = cli.VERBS.get(verb)
            # `help` is not a module: cli.py answers it (and `--all`) before dispatch.
            module = entry.spec.partition(":")[0] if entry else "cli"
            if module not in sources:
                module_path = root / "src" / "rein" / f"{module}.py"
                sources[module] = module_path.read_text(encoding="utf-8") if module_path.is_file() else ""
            for flag in _FLAG_RE.findall(match.group(1)):
                if flag == "--repo":  # accepted by every verb, via cli.main
                    continue
                if not _declares_flag(sources[module], flag):
                    failures.append(
                        f"{path}: `rein {verb} {flag}` — {module}.py declares no such option, so the "
                        "documented command exits 2"
                    )
    return failures


def check_guard_defaults(config_text: str) -> list[str]:
    """The template config.yaml's guard.paths must mirror gate_guard's built-in defaults.

    The block exists in two hand-maintained places on purpose (the code default applies when the
    key is omitted; the shipped config spells it out for the human editing it) — this canary is
    what keeps the pair from drifting when a path rule is added to only one of them.
    """
    shipped = models.Config.parse(config_text).guard_paths
    if not shipped:
        return [f"{CONFIG_PATH}: guard.paths block is missing (the template config must spell out the defaults)"]
    failures: list[str] = []
    defaults = gate_guard.DEFAULT_GUARD_PATHS
    for key in sorted(set(defaults) - set(shipped)):
        failures.append(f"{CONFIG_PATH}: guard.paths is missing `{key}` (in gate_guard.DEFAULT_GUARD_PATHS)")
    for key in sorted(set(shipped) - set(defaults)):
        failures.append(f"gate_guard.py: DEFAULT_GUARD_PATHS is missing `{key}` (in {CONFIG_PATH} guard.paths)")
    for key in sorted(set(defaults) & set(shipped)):
        if str(shipped[key]) != defaults[key]:
            failures.append(
                f"guard_paths `{key}`: {CONFIG_PATH} says {shipped[key]} but gate_guard.py defaults say {defaults[key]}"
            )
    return failures


GITIGNORE_PATH = ".gitignore"
#: The .gitignore section this canary owns, down to the next `# ----` header. Everything outside
#: it (.venv/, node_modules/, the editor files) is the repository's business, not the tool's.
#: The header and the derivation both live in `rein.gitignore` now — the block products receive
#: and the template's own section are the same text, checked the same way.
IGNORE_SECTION_HEADER = gitignore.SECTION_HEADER
_IGNORE_SECTION_END_RE = re.compile(r"^# ---- ")
runtime_artifacts = gitignore.runtime_artifacts


def _ignore_section(gitignore_text: str) -> list[str] | None:
    """The Loose Rein section's entries (comments and blanks dropped), or None when it is absent.

    Parsed the way **git** parses it, which is the only reading that matters: `#` opens a comment
    only at the start of a line, so a trailing `# …` is part of the pattern. Stripping it here
    instead would make this canary agree with what the author meant rather than with what git
    does — and it did: `.worktrees/  # temporary worktrees` matched nothing, and the check that
    exists to notice that reported the section as correct.
    """
    lines = gitignore_text.splitlines()
    if IGNORE_SECTION_HEADER not in lines:
        return None
    entries: list[str] = []
    for line in lines[lines.index(IGNORE_SECTION_HEADER) + 1 :]:
        if _IGNORE_SECTION_END_RE.match(line):
            break
        if line.lstrip().startswith("#"):
            continue
        entry = line.strip()  # git strips unescaped trailing whitespace and nothing else
        if entry:
            entries.append(entry)
    return entries


def check_runtime_artifacts(gitignore_text: str, config_text: str) -> list[str]:
    """The .gitignore section matches runtime_artifacts() exactly — no gaps, no survivors."""
    entries = _ignore_section(gitignore_text)
    if entries is None:
        return [f"{GITIGNORE_PATH}: the `{IGNORE_SECTION_HEADER}` section is missing"]
    expected = runtime_artifacts(config_text)
    failures = [
        f"{GITIGNORE_PATH}: the Loose Rein section does not ignore `{name}` — the tool writes it"
        for name in sorted(expected - set(entries))
    ]
    failures += [
        f"{GITIGNORE_PATH}: the Loose Rein section ignores `{name}`, which nothing writes any more"
        for name in sorted(set(entries) - expected)
    ]
    return failures


def check_tracked_artifacts(root: Path, config_text: str) -> list[str]:
    """No runtime artifact is tracked by git.

    .gitignore does not apply to a path git already tracks, so a generated file committed once
    stays committed and drifts silently from whatever regenerates it — the ignore rule above it
    reads as protection that is not there. Outside a git checkout there is nothing to assert.
    """
    from rein import repo as repo_mod

    repo = repo_mod.Repo(root)
    if repo.git_common_dir is None:
        return []
    return [
        f"{name}: tracked by git — it is generated, so regenerate it rather than commit it"
        for name in sorted(runtime_artifacts(config_text))
        if repo._git("ls-files", "--", name)
    ]


# The repo files that must stay byte-identical to the package-data payload. The payload
# (src/rein/data/) is what ships in the wheel — init/sync/install write repos from it —
# while the repo-root copies are what this template repo itself runs on (dogfood) and what
# the .claude/.github wrappers @-import. A fix landing in only one home is the drift.
_DATA_PARITY: tuple[tuple[str, str], ...] = (
    (".rein/prompts", "prompts"),
    (".rein/schema", "schema"),
    (".rein/oci", "oci"),
    ("AGENTS.md", "rules/AGENTS.md"),
    (".claude/commands", "integrations/claude/commands"),
    (".claude/agents", "integrations/claude/agents"),
    (".claude/settings.json", "integrations/claude/settings.json"),
    (".github/prompts", "integrations/copilot/prompts"),
    (".github/agents", "integrations/copilot/agents"),
    (".github/hooks", "integrations/copilot/hooks"),
    (".github/instructions", "integrations/copilot/instructions"),
    (".agents/skills", "integrations/codex/skills"),
    (".gemini/commands", "integrations/gemini/commands"),
    (".gemini/skills", "integrations/gemini/skills"),
    (".gemini/rein.md", "integrations/gemini/rein.md"),
    (".codex/agents", "integrations/codex/agents"),
    (".codex/hooks.json", "integrations/codex/hooks.json"),
    (".codex/rein.md", "integrations/codex/rein.md"),
    ("CHANGELOG.md", "CHANGELOG.md"),
)


def check_data_parity(root: Path) -> list[str]:
    """Every materialized file equals its package-data source, pair-complete both ways."""
    from rein import data as data_mod

    failures: list[str] = []
    for repo_rel, data_rel in _DATA_PARITY:
        repo_path = root / repo_rel
        if repo_path.is_file():
            repo_files = {"": repo_path.read_bytes()}
        else:
            repo_files = {
                p.relative_to(repo_path).as_posix(): p.read_bytes() for p in sorted(repo_path.rglob("*")) if p.is_file()
            }
        data_files: dict[str, bytes] = {}
        entry = data_mod.path(data_rel)
        if entry.is_file():
            data_files[""] = entry.read_bytes()
        else:
            strip = len(data_rel) + 1
            for rel, blob in data_mod.iter_files(data_rel):
                data_files[rel[strip:]] = blob
        for name in sorted(set(repo_files) - set(data_files)):
            failures.append(f"src/rein/data/{data_rel}: missing `{name or repo_rel}` (present in {repo_rel})")
        for name in sorted(set(data_files) - set(repo_files)):
            failures.append(f"{repo_rel}: missing `{name or data_rel}` (present in src/rein/data/{data_rel})")
        for name in sorted(set(repo_files) & set(data_files)):
            if repo_files[name] != data_files[name]:
                where = f"{repo_rel}/{name}" if name else repo_rel
                failures.append(f"{where}: differs from src/rein/data/{data_rel}{'/' + name if name else ''}")
    return failures


#: Files that instruct a human to build a sandbox image. `oci build` resolves `--profile` against
#: the packaged Containerfiles, so a name here that is not one of those is a command that exits 1.
#: Both READMEs are on the list because that is where the instructions live now: `docs/` holds the
#: phase deliverables `init` scaffolds, and a page only this repository ships was never reachable
#: from the repositories the shipped config told to go read it.
_OCI_INSTRUCTION_FILES: tuple[str, ...] = (
    ".rein/config.yaml",
    "src/rein/data/scaffold/rein/config.yaml",
    "README.md",
    "README.ja.md",
)
_OCI_INSTRUCTION_GLOBS: tuple[str, ...] = (".rein/oci/*/Containerfile", "src/rein/data/oci/*/Containerfile")
_OCI_PROFILE_RE = re.compile(r"rein oci build --profile ([a-z][a-z0-9_-]*)")


def check_oci_profile_mentions(root: Path) -> list[str]:
    """Every `oci build --profile <x>` in prose names a Containerfile that ships.

    The first canary to validate an *argument* rather than a verb. Every other check here is
    presence-of-known-good or absence-of-known-bad; this one captures a value and asserts
    membership in a code-derived set, the way check_guard_defaults compares a shipped config
    against a code constant.

    It exists because a profile's name and its Containerfile's name are different things. Both
    shipped configs, both copies of the python Containerfile, `doctor`, and `rein next` all
    told people to run `--profile quality` — and `quality` is a profile, not a Containerfile, so
    every one of those was a command that fails the moment it is pasted.
    """
    from rein import executors

    known = set(executors.containerfile_names())
    if not known:  # no packaged Containerfiles: nothing to validate against, so assert nothing
        return []
    paths = [root / rel for rel in _OCI_INSTRUCTION_FILES]
    paths += [p for pattern in _OCI_INSTRUCTION_GLOBS for p in sorted(root.glob(pattern))]
    failures: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        for name in _OCI_PROFILE_RE.findall(path.read_text(encoding="utf-8")):
            if name not in known:
                failures.append(
                    f"{rel}: `rein oci build --profile {name}` names no packaged Containerfile "
                    f"(have: {', '.join(sorted(known))}) — running it exits 1"
                )
    return failures


#: The two config.yaml copies are one document with two project identities — this repository's
#: own, and the one `rein init` scaffolds. Every other line is shared prose and shared
#: defaults, but the pair is not in _DATA_PARITY (they are not byte-identical), so nothing was
#: holding them together: the same wrong `--profile` sat in both, and a fix to one could have
#: left the other behind.
_CONFIG_IDENTITY_PREFIXES = ("name:", "work_branch:")
_SCAFFOLD_CONFIG = "src/rein/data/scaffold/rein/config.yaml"


def check_ssot_validates(root: Path) -> list[str]:
    """The template's own `.rein/*.yaml` still validate against the schema it ships.

    This repository carries a live `.rein/` and the schema those documents are judged by, and a
    schema change lands in one without touching the other. `rein doctor` says so, but nothing in
    `make check` ran it — so a release could ship a template whose very first `rein doctor` fails.
    It has: collapsing `machine.coverage` from a list to one manifest left `.rein/review.yaml`
    holding a `[]` the new schema refuses.

    Only the shape is checked, not the content: these are this repository's own working documents,
    and what they say is its business.
    """
    failures = []
    for name in ("config", "state", "plan", "review"):
        path = root / ".rein" / f"{name}.yaml"
        if not path.is_file():
            continue
        try:
            document = strict_yaml.load_mapping(path.read_text(encoding="utf-8"), what=f"{name}.yaml")
        except strict_yaml.StrictParseError as exc:
            failures.append(f".rein/{name}.yaml does not parse: {exc}")
            continue
        failures += [f".rein/{name}.yaml: {error}" for error in models.schema_errors(document, name)]
    return failures


def check_scaffold_config_parity(root: Path) -> list[str]:
    """The repo's config.yaml and the scaffolded one differ only in the project identity."""
    live, scaffold = root / CONFIG_PATH, root / _SCAFFOLD_CONFIG
    if not (live.is_file() and scaffold.is_file()):
        return []
    left = live.read_text(encoding="utf-8").splitlines()
    right = scaffold.read_text(encoding="utf-8").splitlines()
    failures = [
        f"{CONFIG_PATH}:{n}: differs from {_SCAFFOLD_CONFIG} outside the project identity "
        f"({(a or '(missing)').strip()!r} vs {(b or '(missing)').strip()!r})"
        for n, (a, b) in enumerate(zip_longest(left, right), start=1)
        if a != b and not (a or "").strip().startswith(_CONFIG_IDENTITY_PREFIXES)
    ]
    return failures


#: Schema properties no Python is expected to name, and why. Two kinds only.
#:
#: **Agent-authored** — a reviewer writes the field and jsonschema is what checks it. No Python
#: reads them because nothing downstream needs to: they are carried into the review document and
#: read by the human. Legitimate, and the reason this canary needs a list at all.
#:
#: **Built at runtime** — the key is assembled rather than written out, so a literal search cannot
#: see it. `Config.profile_for` does `f"{role}_profile"`.
#:
#: Anything else is the `machine.extra_behaviors` shape: declared in the schema, consumed by
#: something, and written by nobody. Adding a name here is a claim that has to be true.
DECLARED_BUT_UNREAD: dict[str, str] = {
    "assessment_basis": "agent-authored: how the comparator reached its semantic judgement",
    "assessor_digest": "agent-authored: which assessor produced it",
    "code_anchor_digest": "agent-authored: the integrity axis's own binding",
    "limitations": "agent-authored: what a statement does not cover",
    "observed_conditions": "agent-authored: the conditions an extracted statement holds under",
    "recommended_fix": "agent-authored: what the security reviewer suggests",
    "unknowns": "agent-authored: what the extractor could not determine",
    "implementer_profile": 'built at runtime by Config.profile_for as f"{role}_profile"',
    "reviewer_profile": 'built at runtime by Config.profile_for as f"{role}_profile"',
}


def _declared_properties(root: Path) -> dict[str, str]:
    """Every property name the shipped schemas declare → the first place it is declared."""
    import json

    found: dict[str, str] = {}

    def walk(node: object, schema: str, path: str) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                for key, child in properties.items():
                    found.setdefault(str(key), f"{schema}{path}/{key}")
                    walk(child, schema, f"{path}/{key}")
            for key, child in node.items():
                if key != "properties":
                    walk(child, schema, f"{path}/{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, schema, f"{path}/{index}")

    for path in sorted((root / "src" / "rein" / "data" / "schema").glob("*.schema.json")):
        walk(json.loads(path.read_text(encoding="utf-8")), path.name, "")
    return found


def check_declared_properties_are_read(root: Path) -> list[str]:
    """Every schema property is named by some Python, or listed above with a reason.

    The defect this exists for has happened repeatedly and is always the same shape: a field
    declared in the schema, consumed by decision cards or a budget or a summary, and **written by
    nobody**. `machine.extra_behaviors` reported "extra behaviours: 0" from a list that could not
    hold anything; `rename_semantics_analyzed` sat beside two flags the coverage status actually
    reads; `human.session` described stage progress that is derived instead; `dispositions[].owner`
    and `.due` were affordances `record_disposition` cannot produce.

    A literal name search is coarse on purpose — it cannot tell a writer from a reader — but it
    catches the whole class, because a field nothing *mentions* is certainly one nothing writes.
    """
    sources = "\n".join(path.read_text(encoding="utf-8") for path in sorted((root / "src" / "rein").rglob("*.py")))
    failures = []
    for name, where in sorted(_declared_properties(root).items()):
        if name in DECLARED_BUT_UNREAD or re.search(rf"\b{re.escape(name)}\b", sources):
            continue
        failures.append(
            f"{where}: declared in the schema and named nowhere in src/rein/**.py. Wire it, delete it, "
            f"or record it in DECLARED_BUT_UNREAD with the reason it needs no reader."
        )
    stale = sorted(set(DECLARED_BUT_UNREAD) - set(_declared_properties(root)))
    failures += [
        f"DECLARED_BUT_UNREAD names `{name}`, which no schema declares any more — drop the entry" for name in stale
    ]
    return failures


def check_ignored_payload(root: Path) -> list[str]:
    """No file this template ships is invisible to git.

    check_data_parity reads the filesystem, so a file .gitignore swallows is *parity-clean* and
    still missing from every clone — a fresh checkout ships the hole, and the wheel built here
    does not, which is the worst version of this bug. It is not hypothetical: an unanchored
    `build/` matched `.agents/skills/build/SKILL.md`, so Codex would have shipped seven of the
    eight phase entry points with every other check green. Outside a git checkout, nothing to
    assert.
    """
    from rein import repo as repo_mod

    repo = repo_mod.Repo(root)
    if repo.git_common_dir is None:
        return []
    shipped = {repo_rel for repo_rel, _ in _DATA_PARITY} | {"src/rein/data"}
    candidates = sorted(
        p.relative_to(root).as_posix()
        for base in (root / rel for rel in shipped)
        for p in ([base] if base.is_file() else base.rglob("*"))
        if p.is_file()
    )
    if not candidates:
        return []
    # `check-ignore` consults the index, so a file git already tracks is not reported however the
    # patterns read — tracked is exactly the state that does reach a clone. rc 1 means nothing
    # matched; anything else is git failing, which must not read as a pass.
    rc, out = repo._git_rc("check-ignore", "--", *candidates)
    if rc not in (0, 1):
        return [f"{GITIGNORE_PATH}: `git check-ignore` failed (rc {rc}) — the payload could not be checked"]
    return [
        f"{name}: shipped by the template but ignored by .gitignore — it reaches no clone" for name in out.splitlines()
    ]


def check_version_changelog(version: str, changelog: str) -> list[str]:
    """The pyproject version and CHANGELOG.md's newest `## [x.y.z]` heading must agree.

    Guards the release failure where the identity files go stale *together* (bump one, forget
    the other) — upgrade's `version: A → B` display then lies to every downstream repo.
    """
    if not version:
        return ["the pyproject.toml [project] version is missing or empty"]
    m = install._CHANGELOG_HEADING_RE.search(changelog)
    if not m:
        return ["CHANGELOG.md has no `## [x.y.z]` version heading"]
    if m.group(1) != version:
        return [f"pyproject.toml says version {version} but CHANGELOG.md's newest heading says {m.group(1)}"]
    return []


def check_version_lock(version: str, lock_text: str) -> list[str]:
    """uv.lock's entry for this project agrees with the pyproject version.

    The third identity file, and the one that fails hardest: `uv sync --frozen` refuses a lock
    that disagrees with pyproject, so CI dies at dependency install with nothing said about a
    version. Locally it hides, because `uv run --frozen` does not re-check the lock — so the
    whole quality gate can pass on a machine whose CI run cannot start.
    """
    if not version:
        return []  # check_version_changelog already reports the missing version
    m = _LOCK_PROJECT_RE.search(lock_text)
    if not m:
        return ["uv.lock has no `loose-rein-kit` package entry"]
    if m.group(1) != version:
        return [f"pyproject.toml says version {version} but uv.lock says {m.group(1)} — run `uv lock`"]
    return []


#: Extensions worth scanning for a literal command: prose, code, and CI definitions. A canary
#: that skipped one of these would let the literal survive in exactly the place it did most
#: recently (a module docstring).
_TEXT_SUFFIXES = frozenset({".md", ".py", ".yml", ".yaml", ".json", ".toml", ".sh"})


@cache
def _tracked_texts(root: Path) -> dict[str, str]:
    """Every git-tracked text file, repo-relative path → content.

    Git-tracked rather than a directory walk: `.venv/`, `node_modules/` and the build caches are
    not this repository's prose, and scanning them would make the canary both slow and wrong.
    """
    from rein import repo as repo_mod

    listing = repo_mod.Repo(root)._git("ls-files", "-z")
    if not listing:
        # `_git` returns "" for every failure — git absent, not a checkout, a broken index. An
        # empty listing would make every canary built on it report "no drift" without having
        # looked at a single file, which is the one thing these checks may never do.
        raise OSError(f"could not list git-tracked files under {root} — the canaries cannot look")
    texts: dict[str, str] = {}
    for rel in filter(None, listing.split("\0")):
        if Path(rel).suffix not in _TEXT_SUFFIXES:
            continue
        try:
            texts[rel] = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return texts


#: The command that does NOT upgrade a tag-pinned `uv tool install`. It spent five places in this
#: repository telling people to run it, and `upstream.upgrade_command` now derives the right one
#: from the install's own PEP 610 metadata. Only `upstream.py` may spell it, as the branch-tracking
#: case's answer.
_DEAD_UPGRADE_COMMAND = "uv tool upgrade loose-rein-kit"
_UPGRADE_COMMAND_OWNER = "src/rein/upstream.py"
#: Two files may still name it: the release notes, whose whole point is explaining both install
#: shapes to a reader who has neither installed yet, and this canary — a register of what may not
#: come back has to be able to spell what it forbids.
_UPGRADE_COMMAND_EXEMPT = (".github/workflows/release.yml", "scripts/template_lint.py")


def check_upgrade_command(root: Path) -> list[str]:
    """No file but `upstream.py` may spell an upgrade command.

    A tag-pinned uv tool re-resolves to its pinned rev, so `uv tool upgrade` moves it nowhere —
    and every message that printed it was telling a reader to run a no-op. The command now comes
    from `upstream.upgrade_command`, derived from how the install was actually made; this canary
    is what stops the literal from creeping back into a docstring or a README.
    """
    failures = []
    texts = _tracked_texts(root)
    for path in sorted(texts):
        if path == _UPGRADE_COMMAND_OWNER or path in _UPGRADE_COMMAND_EXEMPT:
            continue
        if _DEAD_UPGRADE_COMMAND in texts[path]:
            failures.append(
                f"{path}: `{_DEAD_UPGRADE_COMMAND}` is a no-op for a tag-pinned install — "
                f"print `upstream.upgrade_command()` instead"
            )
    return failures


def check_distribution_name(root: Path) -> list[str]:
    """`upstream.DIST_NAME` must equal `pyproject.toml [project] name`.

    They differ from the import name, and `detect_source` asked `importlib.metadata` for the
    *import* name for its whole life — so it returned "" on every real install and no test caught
    it, because the tests monkeypatched the one function that touched reality.
    """
    from rein import upstream

    m = re.search(r'^name\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8"), re.MULTILINE)
    if not m:
        return ["pyproject.toml has no [project] name"]
    if upstream.DIST_NAME != m.group(1):
        return [
            f"upstream.DIST_NAME is {upstream.DIST_NAME!r} but pyproject.toml says {m.group(1)!r} — "
            "importlib.metadata resolves the distribution name, not the import name"
        ]
    return []


def check_rein_lock_version(version: str, lock_text: str) -> list[str]:
    """`.rein/rein.lock` records the release that wrote this repository's materialized artifacts.

    The fourth identity file, and the one with no natural symptom. `rein sync` stamps
    `tool_version` and `prompts.version` only as a side effect of *writing* something, so a release
    that changes no payload byte leaves both at the previous version and `sync --check` — which
    compares content, not versions — passes. That is exactly what happened at 0.3.1: the shipped
    template claimed 0.3.0 while the CLI was 0.3.1, and the only thing that noticed was a shell
    snippet in the release workflow, on the day of the release.

    Here instead, so it fails on the pull request that bumps the version rather than at the tag.
    """
    if not version:
        return []  # check_version_changelog already reports the missing version
    failures = []
    for key, pattern in (("tool_version", _REIN_LOCK_TOOL_RE), ("prompts.version", _REIN_LOCK_PROMPTS_RE)):
        m = pattern.search(lock_text)
        if not m:
            failures.append(f".rein/rein.lock has no `{key}` — run `rein sync`")
        elif m.group(1) != version:
            failures.append(
                f"pyproject.toml says version {version} but .rein/rein.lock's {key} says "
                f"{m.group(1)} — run `rein sync` and commit the lock"
            )
    return failures


def main(argv: list[str] | None = None) -> int:
    import argparse

    from rein import repo as repo_mod

    parser = argparse.ArgumentParser(prog="template_lint.py", description="template drift canaries")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()
    try:
        root = repo_mod.get(args.repo).root
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1

    config_text = (root / CONFIG_PATH).read_text(encoding="utf-8")
    try:
        if not models.Config.parse(config_text).template_mode:
            print("skipped (guard.template_mode is false: not the template repo)")
            return 0
    except (models.DocumentError, strict_yaml.StrictParseError) as exc:
        logger.error(f"template-lint: {CONFIG_PATH} is not valid: {exc}")
        return 1

    try:
        files = {
            path: (root / path).read_text(encoding="utf-8")
            for path in (AGENTS_MD, TASKS_CMD, BUILD_CMD, CONFIG_PATH, "README.md", "README.ja.md")
        }
        failures = check_vocabulary(files)
        failures += check_wrapper_parity(root)
        failures += check_adapter_lists(root)
        failures += check_capability_mapping(
            {path: (root / path).read_text(encoding="utf-8") for path in CAPABILITY_MAPPINGS},
            files[AGENTS_MD],
        )
        texts = neutral_texts(root)
        failures += check_neutral_vocabulary(texts)
        failures += check_banned_absence(texts)
        failures += check_rules_wiring(root, texts)
        failures += check_documented_invocations(root, {**texts, **files})
        failures += check_data_parity(root)
        failures += check_guard_defaults(files[CONFIG_PATH])
        gitignore = root / GITIGNORE_PATH
        failures += check_runtime_artifacts(
            gitignore.read_text(encoding="utf-8") if gitignore.is_file() else "", files[CONFIG_PATH]
        )
        failures += check_tracked_artifacts(root, files[CONFIG_PATH])
        failures += check_ignored_payload(root)
        failures += check_declared_properties_are_read(root)
        failures += check_oci_profile_mentions(root)
        failures += check_scaffold_config_parity(root)
        failures += check_ssot_validates(root)
        failures += check_readme_parity(files["README.md"], files["README.ja.md"])
        version = install.read_version(root)
        failures += check_version_changelog(version, (root / "CHANGELOG.md").read_text(encoding="utf-8"))
        lock = root / "uv.lock"
        if lock.is_file():  # a product repository has no uv.lock of its own to keep in step
            failures += check_version_lock(version, lock.read_text(encoding="utf-8"))
        failures += check_rein_lock_version(version, (root / ".rein" / "rein.lock").read_text(encoding="utf-8"))
        failures += check_upgrade_command(root)
        failures += check_distribution_name(root)
    except OSError as exc:
        logger.error(f"template-lint failed: {exc}")
        return 1

    for failure in failures:
        print(f"  drift: {failure}")
    if failures:
        print(f"{len(failures)} drift(s) — propagate the change to every listed file (or revert it).")
        return 1
    print("template-lint: no drift.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
