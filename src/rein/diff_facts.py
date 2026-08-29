"""The deterministic Diff Fact Detector and Coverage Manifest (plan §13).

Two jobs, and the honesty of the second is the whole point:

**Detect Extra-Behavior candidates.** A fixed set of signals (plan §13.2) — a changed
dependency lock, a new route, a `timeout=`, a deleted `if` guard — is matched against the diff
by regex, never by an LLM's discretion. Each match is a *candidate* the reviewers must ground
or a human must judge; the detector never decides it is benign.

**Say what it could not read.** The Coverage Manifest (plan §13.3) records, for the diff, which
files were analyzed and which were not, and why: a binary blob, an unsupported language, a
generated file. `coverage_status` is `insufficient` whenever something that bears on a
high/critical change went unanalyzed (plan §13.4). This is what stops "Extra Behavior: 0" from
meaning "we looked everywhere and found nothing" when it really means "we could not look" — the
two must never render the same (plan §2.4).

**One manifest, one whole diff.** The detector never cuts the change up: it reads all of it or
says which parts it could not. A change too large to be read in one sitting is refused outright
by `review._refuse_over_budget`, before a model is launched, because the answer to it is
`/revise` and not a narrower window onto the same change.

The detector reports a `risk_floor`: the highest risk implied by the signals it matched. An AI
review cannot lower it (plan §13.5) — a diff that deletes a validation guard is at least
`high`, whatever the plan claims the change is.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from rein import digests, models

# --- diff model ---------------------------------------------------------------


@dataclass(frozen=True)
class Hunk:
    """One `@@` hunk: the added and removed lines, without their +/- markers."""

    added: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def changed(self) -> tuple[str, ...]:
        return self.added + self.removed


@dataclass(frozen=True)
class DiffFile:
    """One file's change: its path, hunks, and how (or whether) it can be analyzed."""

    path: str
    hunks: tuple[Hunk, ...]
    binary: bool = False
    #: A path that git reports renamed-from, so a reviewer can tell a move from a rewrite.
    old_path: str = ""

    @property
    def added_lines(self) -> list[str]:
        return [line for hunk in self.hunks for line in hunk.added]

    @property
    def removed_lines(self) -> list[str]:
        return [line for hunk in self.hunks for line in hunk.removed]

    @property
    def changed_lines(self) -> list[str]:
        return [line for hunk in self.hunks for line in hunk.changed]


# --- unified-diff parsing -----------------------------------------------------

#: One `diff --git` token: git's C-quoted form, or a bare run of non-space. Which one it emits is
#: not the caller's choice — `core.quotePath` defaults to true, so *any* path with a space or a
#: non-ASCII byte arrives quoted. A pattern that only accepted `a/… b/…` therefore did not fail on
#: such a file, it did not see the header at all: the file vanished from `parse_diff` (so from the
#: Coverage Manifest and from every scope check), and its lines were attributed to whichever file
#: came before it — which is how `split_tests` handed a test file's assertions to the blind
#: extractor, the one thing it exists to prevent. For a project whose deliverables are written in
#: the user's language, a quoted path is the common case, not the exotic one.
_TOKEN = r'"(?:[^"\\]|\\.)*"|\S+'
_DIFF_GIT = re.compile(rf"^diff --git (?P<a>{_TOKEN}) (?P<b>{_TOKEN})$")
_RENAME_FROM = re.compile(rf"^rename from (?P<p>{_TOKEN})$")

#: git's C-style escapes. Octal is handled separately: it produces a byte, not a character.
_C_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, '"': 34}


def _unquote(token: str) -> str:
    r"""A `diff --git` token as a path: quotes removed, C escapes decoded, `a/`/`b/` prefix dropped.

    Decoded through *bytes* rather than characters: git escapes each UTF-8 byte of a non-ASCII
    name as its own octal `\NNN`, so `"b/na\303\257ve.py"` is two escapes that together are one
    `ï`. Decoding them one at a time would produce two replacement characters and a path that
    matches nothing.
    """
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        raw = token[1:-1]
        out = bytearray()
        i = 0
        while i < len(raw):
            if raw[i] != "\\" or i + 1 >= len(raw):
                out.extend(raw[i].encode("utf-8"))
                i += 1
            elif raw[i + 1] in _C_ESCAPES:
                out.append(_C_ESCAPES[raw[i + 1]])
                i += 2
            elif len(raw[i + 1 : i + 4]) == 3 and all(c in "01234567" for c in raw[i + 1 : i + 4]):
                out.append(int(raw[i + 1 : i + 4], 8))
                i += 4
            else:  # an escape git does not emit: keep the character rather than dropping it
                out.extend(raw[i + 1].encode("utf-8"))
                i += 2
        token = out.decode("utf-8", errors="replace")
    return token[2:] if token[:2] in ("a/", "b/") else token


def header_path(line: str) -> str | None:
    """The post-image path a `diff --git` line names, or None when the line is not one.

    The one place a header is recognised. Four walks over a diff need the same answer — the parser
    here, and `review`'s three (the test split, the mechanical fold, the byte breakdown) — and the
    three outside this module were reaching for the private pattern and reading `group("b")`
    themselves, which is how they all inherited the same blind spot at once.
    """
    header = _DIFF_GIT.match(line)
    return _unquote(header.group("b")) if header else None


_HUNK = re.compile(r"^@@ ")


def parse_diff(diff_text: str) -> list[DiffFile]:
    """Parse a `git diff` (unified format) into per-file added/removed lines.

    A binary file shows as `Binary files … differ` or a `GIT binary patch`; it carries no
    text hunks and is flagged so the coverage manifest can record it as unanalyzable.
    """
    files: list[DiffFile] = []
    path = ""
    old_path = ""
    binary = False
    hunks: list[Hunk] = []
    added: list[str] = []
    removed: list[str] = []

    def flush_hunk() -> None:
        if added or removed:
            hunks.append(Hunk(added=tuple(added), removed=tuple(removed)))
            added.clear()
            removed.clear()

    def flush_file() -> None:
        flush_hunk()
        if path:
            files.append(DiffFile(path=path, hunks=tuple(hunks), binary=binary, old_path=old_path))

    for line in diff_text.splitlines():
        named = header_path(line)
        if named is not None:
            flush_file()
            path = named
            old_path = ""
            binary = False
            hunks = []
            continue
        renamed = _RENAME_FROM.match(line)
        if renamed:
            old_path = _unquote(renamed.group("p"))
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            binary = True
            continue
        if _HUNK.match(line):
            flush_hunk()
            continue
        # Ignore the ---/+++ file headers; count real content lines only.
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])

    flush_file()
    return files


# --- signal detection (plan §13.2) --------------------------------------------


@dataclass(frozen=True)
class Signal:
    """One Extra-Behavior signal: what it flags, the candidate it implies, its risk floor."""

    name: str
    candidate: str
    risk: str
    #: Matched against added+removed lines. `removed_only` signals fire on deletions alone.
    pattern: re.Pattern[str]
    removed_only: bool = False


def _kw(*words: str) -> re.Pattern[str]:
    # A boundary that also treats `_` as a break, so `has_permission` matches `permission` but
    # `admin` does not match `min`. Plain `\b` fails the first case — `_` is a word character.
    return re.compile(r"(?<![A-Za-z0-9])(" + "|".join(words) + r")(?![A-Za-z0-9])", re.IGNORECASE)


#: The 13 signals of plan §13.2, each with the risk floor a match implies. Ordered as the plan
#: table. Path-based signals (dependency, migration) are matched separately, by filename.
SIGNALS: tuple[Signal, ...] = (
    Signal(
        "public_surface",
        "a new or changed public surface",
        "medium",
        _kw("route", "endpoint", "add_argument", "add_parser", "click.option", "@app", "@router", "getMapping"),
    ),
    Signal(
        "failure_policy",
        "a failure/retry policy",
        "medium",
        _kw("timeout", "retry", "retries", "backoff", "deadline", "max_attempts"),
    ),
    Signal(
        "default_value",
        "a changed default or fallback",
        "medium",
        _kw("default", "fallback", "getenv", "setdefault"),
    ),
    Signal(
        "security_boundary",
        "a security boundary",
        "high",
        _kw("auth", "authorize", "authenticate", "role", "permission", "token", "secret", "credential", "password"),
    ),
    Signal(
        "side_effect",
        "an external side effect",
        "high",
        _kw("delete", "unlink", "rmtree", "remove", "drop_table", "commit", "publish", "send", "post", "destroy"),
    ),
    Signal(
        "swallowed_failure",
        "a swallowed failure",
        "medium",
        re.compile(r"except\s*:|except\s+Exception|contextlib\.suppress|catch\s*\(|rescue\b|\bpass\b\s*(#.*)?$", re.I),
    ),
    Signal(
        "concurrency",
        "a concurrency/atomicity change",
        "high",
        _kw("lock", "mutex", "async", "await", "thread", "transaction", "atomic", "semaphore"),
    ),
    Signal(
        "operation_contract",
        "an operational contract (config/flag)",
        "medium",
        _kw("feature_flag", "feature_gate", "config", "os.environ", "settings"),
    ),
    Signal(
        "observability",
        "an observability change",
        "low",
        _kw("logging", "logger", "log", "metric", "counter", "gauge", "prometheus", "telemetry"),
    ),
    Signal(
        "threshold",
        "a threshold/policy value",
        "medium",
        _kw("threshold", "limit", "max", "min", "quota", "rate_limit", "ceiling"),
    ),
)

#: The 13th signal — a deleted guard — fires on *removed* lines only: a validation that used to
#: be there and is now gone is exactly the change a "what got quietly removed" reviewer misses.
DELETED_GUARD = Signal(
    "deleted_guard",
    "a removed guard or validation",
    "high",
    _kw("if", "assert", "raise", "validate", "check", "require", "verify", "guard", "ensure"),
    removed_only=True,
)

#: Dependency-manifest and migration signals are matched by *path*, not content.
_DEPENDENCY_FILES = re.compile(
    r"(^|/)(requirements[^/]*\.txt|pyproject\.toml|uv\.lock|poetry\.lock|Pipfile(\.lock)?|"
    r"package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.(toml|lock)|go\.(mod|sum)|"
    r"Gemfile(\.lock)?|composer\.(json|lock))$"
)
_MIGRATION_FILES = re.compile(r"(^|/)(migrations?|alembic)/|\.sql$")


@dataclass(frozen=True)
class SignalHit:
    """A signal matched against a file: which signal, where, and the risk it implies."""

    signal: str
    candidate: str
    risk: str
    path: str
    sample: str


def detect_signals(files: list[DiffFile]) -> list[SignalHit]:
    """Every signal match across the diff, in file then signal order. Candidates, not verdicts."""
    hits: list[SignalHit] = []
    for file in sorted(files, key=lambda f: f.path):
        if _DEPENDENCY_FILES.search(file.path) and (file.hunks or file.binary):
            hits.append(SignalHit("dependency", "a dependency/lock change", "medium", file.path, file.path))
        if _MIGRATION_FILES.search(file.path) and (file.hunks or file.binary):
            hits.append(SignalHit("migration", "a schema/migration change", "high", file.path, file.path))
        for signal in SIGNALS:
            lines = file.changed_lines
            hit = _first_match(signal.pattern, lines)
            if hit is not None:
                hits.append(SignalHit(signal.name, signal.candidate, signal.risk, file.path, hit))
        removed_hit = _first_match(DELETED_GUARD.pattern, file.removed_lines)
        if removed_hit is not None:
            guard = DELETED_GUARD
            hits.append(SignalHit(guard.name, guard.candidate, guard.risk, file.path, removed_hit))
    return hits


def _first_match(pattern: re.Pattern[str], lines: list[str]) -> str | None:
    for line in lines:
        if pattern.search(line):
            return line.strip()[:200]
    return None


# --- coverage manifest (plan §13.3) -------------------------------------------

#: Extension → how deeply this release can analyze it. `ast` = structural, `token_only` =
#: lexical, absent = unsupported (recorded honestly, never silently treated as analyzed).
#:
#: `token_only` is a real, declared method — the signal detector reads every changed line of
#: these files. Listing a text format here is therefore not a loosening: it is the difference
#: between "we scanned it lexically and say so" and "we could not read it at all", which is
#: what `unsupported` is reserved for (binaries, and formats nothing here can tokenize).
_LANGUAGE_ANALYSIS: dict[str, tuple[str, str]] = {
    ".py": ("python", "ast"),
    ".pyi": ("python", "ast"),
    ".js": ("javascript", "token_only"),
    ".jsx": ("javascript", "token_only"),
    ".ts": ("typescript", "token_only"),
    ".tsx": ("typescript", "token_only"),
    ".go": ("go", "token_only"),
    ".rs": ("rust", "token_only"),
    ".java": ("java", "token_only"),
    ".kt": ("kotlin", "token_only"),
    ".swift": ("swift", "token_only"),
    ".c": ("c", "token_only"),
    ".h": ("c", "token_only"),
    ".cc": ("cpp", "token_only"),
    ".cpp": ("cpp", "token_only"),
    ".hpp": ("cpp", "token_only"),
    ".cs": ("csharp", "token_only"),
    ".php": ("php", "token_only"),
    ".rb": ("ruby", "token_only"),
    ".sh": ("shell", "token_only"),
    ".bash": ("shell", "token_only"),
    ".ps1": ("powershell", "token_only"),
    ".bat": ("batch", "token_only"),
    ".sql": ("sql", "token_only"),
    ".yaml": ("yaml", "token_only"),
    ".yml": ("yaml", "token_only"),
    ".toml": ("toml", "token_only"),
    ".json": ("json", "token_only"),
    ".ini": ("ini", "token_only"),
    ".cfg": ("ini", "token_only"),
    ".conf": ("ini", "token_only"),
    ".env": ("dotenv", "token_only"),
    ".tf": ("terraform", "token_only"),
    ".proto": ("protobuf", "token_only"),
    ".css": ("css", "token_only"),
    ".scss": ("css", "token_only"),
    ".html": ("html", "token_only"),
    ".htm": ("html", "token_only"),
    ".vue": ("vue", "token_only"),
    ".svelte": ("svelte", "token_only"),
    ".lock": ("lockfile", "token_only"),
    ".txt": ("text", "token_only"),
    ".md": ("markdown", "token_only"),
}

#: Files with no extension that are still plain text. Matched on the base name, case-sensitively
#: except where both spellings are conventional.
_FILENAME_ANALYSIS: dict[str, tuple[str, str]] = {
    "Dockerfile": ("dockerfile", "token_only"),
    "Containerfile": ("dockerfile", "token_only"),
    "makefile": ("make", "token_only"),
    "Makefile": ("make", "token_only"),
    "LICENSE": ("text", "token_only"),
    "CODEOWNERS": ("text", "token_only"),
    ".gitignore": ("text", "token_only"),
    ".gitattributes": ("text", "token_only"),
    ".dockerignore": ("text", "token_only"),
}

#: What an analyzer may claim it did. `ast_plus_llm` is deliberately absent: the Coverage
#: Manifest is the deterministic record of what was read, and a model's opinion does not belong
#: in it whatever an adapter calls itself.
ANALYZER_METHODS = frozenset({"ast", "token_only"})


class AnalyzerUnavailable(RuntimeError):
    """An analyzer could not answer. The file stays unsupported, and this says why."""


class Analyzer(Protocol):
    """An external reader for file kinds this release cannot parse itself.

    This is the second exit the out-of-scope register names for a coverage gap, beside splitting
    the scope — and splitting never helps when the unreadable file *is* the change. An analyzer
    extends `_LANGUAGE_ANALYSIS` from outside: same answer shape, same honesty rules, and the
    digest of whatever produced it is recorded, because "analyzed" with nobody named is not a
    claim anyone can check later.
    """

    @property
    def name(self) -> str: ...

    @property
    def digest(self) -> str: ...

    def analyze(self, path: str, content: str) -> tuple[str, str] | None:
        """`(language, method)`, or None when this analyzer cannot read the file either."""
        ...


#: Paths that are generated, so their diff is a symptom of a source change elsewhere.
_GENERATED_DIRS = re.compile(r"(^|/)(generated|__generated__|_pb2|\.pb\.go$|node_modules/)")
_GENERATED_MARKERS = re.compile(r"@generated|DO NOT EDIT|autogenerated|Code generated by", re.IGNORECASE)

#: Paths that are tests. Not a risk judgement — a reader wants them listed apart from the code
#: they exercise, and an implementer being told "these are your tests" is a different sentence
#: from "these are your changes".
_TEST_PATHS = re.compile(r"(^|/)(tests?|spec|__tests__)/|(^|/)(test_[^/]+|[^/]+_test)\.[A-Za-z0-9]+$|\.spec\.[jt]sx?$")


def classify_path(path: str) -> str:
    """What kind of file this is: `dependency` | `migration` | `generated` | `test` | `source`.

    By path alone, so it can be answered about a name — which is what the per-task dossier has,
    and what the reviewer's scope list is made of. `analyze()` classifies by *content* as well and
    stays the authority for the coverage manifest; this is the cheap question, asked earlier.

    The point of asking it at all is that a lockfile and a hand-written module are not the same
    kind of thing to put in front of a model. Folding 800 mechanical lines into one summary line
    is what stops the meaningful code being buried in them.
    """
    if _DEPENDENCY_FILES.search(path):
        return "dependency"
    if _MIGRATION_FILES.search(path):
        return "migration"
    if _GENERATED_DIRS.search(path):
        return "generated"
    if _TEST_PATHS.search(path):
        return "test"
    return "source"


#: The kinds whose *content* a reader does not need: their diff is bulk, and what it means is
#: already said by the fact that they changed. Deliberately not `migration` — a migration is
#: mechanical to write and consequential to run.
MECHANICAL_KINDS = frozenset({"dependency", "generated"})


@dataclass(frozen=True)
class CoverageManifest:
    """What was and was not analyzed for one diff (plan §13.3), plus the resulting status."""

    diff_digest: str
    analyzed_files: int
    analyzed_hunks: int
    #: Size of the diff this manifest covers, in bytes. Recorded because it is what the
    #: `max_diff_bytes` budget is denominated in: without it that budget has no measurable
    #: actual, and a budget whose actual is a constant is not a budget.
    analyzed_bytes: int
    unsupported_files: tuple[dict[str, str], ...]
    generated_files: tuple[dict[str, str], ...]
    languages: dict[str, str]
    deleted_lines_analyzed: bool
    dependency_semantics_analyzed: bool
    binary_semantics_analyzed: bool
    coverage_status: str = field(default="")
    #: analyzer name → the digest of the executable that answered, for every external analyzer
    #: whose reading is counted above. Nobody named means nobody to check.
    analyzers: dict[str, str] = field(default_factory=dict)

    def to_manifest(self) -> dict[str, object]:
        """The `coverage[]` entry for review.machine — only the schema's fields, no extras."""
        entry: dict[str, object] = {
            "diff_digest": self.diff_digest,
            "analyzed_files": self.analyzed_files,
            "analyzed_hunks": self.analyzed_hunks,
            "analyzed_bytes": self.analyzed_bytes,
            "languages": dict(sorted(self.languages.items())),
            "deleted_lines_analyzed": self.deleted_lines_analyzed,
            "dependency_semantics_analyzed": self.dependency_semantics_analyzed,
            "binary_semantics_analyzed": self.binary_semantics_analyzed,
            "coverage_status": self.coverage_status,
        }
        if self.unsupported_files:
            entry["unsupported_files"] = [dict(u) for u in self.unsupported_files]
        if self.generated_files:
            entry["generated_files"] = [dict(g) for g in self.generated_files]
        if self.analyzers:
            entry["analyzers"] = dict(sorted(self.analyzers.items()))
        return entry


def _extension(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name[name.rindex(".") :] if "." in name else ""


def _analysis_for(path: str) -> tuple[str, str] | None:
    """(language, method) for `path`, or None when nothing here can read it."""
    name = path.rsplit("/", 1)[-1]
    by_name = _FILENAME_ANALYSIS.get(name)
    return by_name if by_name is not None else _LANGUAGE_ANALYSIS.get(_extension(path))


def build_coverage(diff_text: str, files: list[DiffFile], *, analyzers: Sequence[Analyzer] = ()) -> CoverageManifest:
    """Analyze a diff and record honestly what could not be analyzed (plan §13.3).

    `analyzers` are external readers for file kinds this release cannot parse. They are asked
    only about files the built-in table cannot place, they answer in the same `(language,
    method)` shape, and every one whose answer is used is named with its executable digest.
    An analyzer that fails, times out, or is not the binary the manifest pins does not degrade
    the manifest: the file stays unsupported and the reason says which analyzer went missing.
    """
    diff_digest = digests.of_bytes(diff_text.encode("utf-8"))
    analyzed = 0
    analyzed_hunks = 0
    unsupported: list[dict[str, str]] = []
    generated: list[dict[str, str]] = []
    languages: dict[str, str] = {}
    used_analyzers: dict[str, str] = {}
    has_dependency_change = False
    has_binary = False
    deleted_lines_present = False

    for file in files:
        if _DEPENDENCY_FILES.search(file.path):
            has_dependency_change = True
        if file.removed_lines:
            deleted_lines_present = True
        if _is_generated(file):
            generated.append({"path": file.path, "source_locator": ""})
            continue
        if file.binary:
            has_binary = True
            unsupported.append({"path": file.path, "reason": "binary"})
            continue
        language_method = _analysis_for(file.path)
        if language_method is None:
            language_method, detail = _ask_analyzers(analyzers, file, used_analyzers)
        else:
            detail = ""
        if language_method is None:
            entry = {"path": file.path, "reason": "unsupported_language"}
            entry["detail"] = detail or f"no analyzer for {_extension(file.path) or 'a file with no extension'}"
            unsupported.append(entry)
            continue
        language, method = language_method
        languages[language] = _widen_method(languages.get(language), method)
        analyzed += 1
        analyzed_hunks += len(file.hunks)

    manifest = CoverageManifest(
        diff_digest=diff_digest,
        analyzed_files=analyzed,
        analyzed_hunks=analyzed_hunks,
        analyzed_bytes=len(diff_text.encode("utf-8", errors="replace")),
        unsupported_files=tuple(unsupported),
        generated_files=tuple(generated),
        languages=languages,
        # Every removed line is inside a parsed hunk, so deletions are analyzed whenever hunks are.
        deleted_lines_analyzed=True,
        # Detecting a dependency *file* changed is not understanding what the new versions do.
        dependency_semantics_analyzed=not has_dependency_change,
        binary_semantics_analyzed=not has_binary,
        analyzers=used_analyzers,
    )
    # Status is decided against the effective risk by review_policy; a manifest on its own
    # reports the raw facts and a conservative default.
    status = _default_status(manifest, deleted_lines_present)
    return CoverageManifest(**{**manifest.__dict__, "coverage_status": status})


def _default_status(manifest: CoverageManifest, deleted_lines_present: bool) -> str:
    """`sufficient` only when none of the things below went unread — risk-blind by design.

    This records *what was read*, and nothing here weighs how much the unread part matters:
    any unsupported/binary/generated file, or an unevaluated dependency change, makes coverage
    `insufficient` on its own. `review_policy.coverage_gap_risk` is what prices that gap
    against the rest of the change, and `review_policy.coverage_blocks` decides the gate.

    The list is exactly what the checks below name, and no more. The schema used to declare a
    `rename_semantics_analyzed` alongside the dependency and binary flags, written by nobody and
    read by nobody — a third measure this function appeared to take and did not. It is gone rather
    than implemented: `DiffFile.renamed_from` already reaches the reviewer, so a move is
    distinguishable from a rewrite by the participant that can actually judge it.
    """
    if manifest.unsupported_files or manifest.generated_files:
        return "insufficient"
    if not manifest.dependency_semantics_analyzed or not manifest.binary_semantics_analyzed:
        return "insufficient"
    if manifest.analyzed_files == 0 and deleted_lines_present:
        return "insufficient"
    return "sufficient"


def _ask_analyzers(
    analyzers: Sequence[Analyzer], file: DiffFile, used: dict[str, str]
) -> tuple[tuple[str, str] | None, str]:
    """Offer one unreadable file to each analyzer in turn. `(answer, why-not)`.

    Only the changed lines are handed over, because that is what the manifest is a statement
    about, and it keeps the analyzer from needing the working tree. The first usable answer
    wins and its analyzer is recorded; an analyzer that claims a method outside
    `ANALYZER_METHODS` has not answered — `ast_plus_llm` cannot enter the deterministic record
    by being asserted.
    """
    content = "\n".join(file.changed_lines)
    problems: list[str] = []
    for analyzer in analyzers:
        try:
            verdict = analyzer.analyze(file.path, content)
        except AnalyzerUnavailable as exc:
            problems.append(f"{analyzer.name}: {exc}")
            continue
        if verdict is None:
            continue
        language, method = verdict
        if method not in ANALYZER_METHODS:
            problems.append(f"{analyzer.name}: claimed method {method!r}")
            continue
        used[analyzer.name] = analyzer.digest
        return (language, method), ""
    return None, "; ".join(problems)


def _is_generated(file: DiffFile) -> bool:
    if _GENERATED_DIRS.search(file.path):
        return True
    return any(_GENERATED_MARKERS.search(line) for line in file.added_lines)


def _widen_method(existing: str | None, method: str) -> str:
    order = ("token_only", "ast")
    if existing is None:
        return method
    return existing if order.index(existing) >= order.index(method) else method


# --- top-level ----------------------------------------------------------------


@dataclass(frozen=True)
class DiffFacts:
    """The full deterministic read of a diff: its signals, coverage, and risk floor."""

    files: tuple[DiffFile, ...]
    signals: tuple[SignalHit, ...]
    coverage: CoverageManifest

    @property
    def risk_floor(self) -> str:
        """The highest risk any matched signal implies — an AI review cannot go below it."""
        return models.max_risk([hit.risk for hit in self.signals])


def analyze(diff_text: str, *, analyzers: Sequence[Analyzer] = ()) -> DiffFacts:
    """Parse, detect signals, and build the coverage manifest for one diff."""
    files = parse_diff(diff_text)
    return DiffFacts(
        files=tuple(files),
        signals=tuple(detect_signals(files)),
        coverage=build_coverage(diff_text, files, analyzers=analyzers),
    )
