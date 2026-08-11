"""`rein agent <cli>` — point the AI roles at an adapter, without hand-editing config.

Two of the five roles must **not** be the same thing: the actual extractor and the comparator
need distinct independence groups for a critical change, because an extractor and a comparator
sharing a model share its blind spots (plan §12.4).

So this command sets the adapter for one role, or for all of them, and then says out loud
whether the independence requirement is currently satisfiable. Saying so is the point — a
setup that silently violates it would fail much later, at gate ④, as an unexplained block.

  rein agent --show
  rein agent claude                          # every role
  rein agent claude --role implementer
  rein agent claude --role comparator --group claude/sonnet

The rewrite is surgical: only the touched keys change, and every comment in config.yaml
survives, because a YAML round-trip would silently delete the comments that explain the file.
"""

from __future__ import annotations

import argparse
import logging
import re

from rein import common, models, strict_yaml
from rein import repo as repo_mod

logger = logging.getLogger(__name__)

#: The roles a human may point at an adapter, in config order.
ROLES: tuple[str, ...] = (
    "implementer",
    "code_reviewer",
    "actual_extractor",
    "comparator",
    "security_reviewer",
)

#: The two roles a critical change requires to differ (plan §12.4).
INDEPENDENT_PAIR = ("actual_extractor", "comparator")


class AgentCliError(Exception):
    """A refused switch, with a message that already names the next step."""


def _section(text: str, name: str) -> tuple[int, int] | None:
    """(start, end) of a top-level block's body, or None when the block is absent.

    Scoping the surgery to one section is not a nicety. `executor_profiles` and `agents` both
    hold two-space-indented keys called `implementer` and `reviewer`, so a search anchored on
    the role name alone silently rewrote the wrong section.
    """
    header = re.search(rf"^{re.escape(name)}:\s*(?:#.*)?$", text, re.MULTILINE)
    if header is None:
        return None
    start = header.end()
    following = re.search(r"^\S", text[start:], re.MULTILINE)
    return start, (start + following.start()) if following else len(text)


def _set_role_key(text: str, role: str, key: str, value: str) -> tuple[str, bool]:
    """Set `agents.<role>.<key>` by line surgery. Returns (new text, **role found**).

    The flag reports whether the role block exists, not whether the text moved: setting an
    adapter to the value it already has is a successful no-op, and reporting it as "role not
    declared" would send the human off to fix a file that is already correct.
    """
    bounds = _section(text, "agents")
    if bounds is None:
        return text, False
    section_start, section_end = bounds
    section = text[section_start:section_end]

    role_re = re.compile(rf"^(  {re.escape(role)}:\s*(?:#.*)?)$", re.MULTILINE)
    match = role_re.search(section)
    if match is None:
        return text, False
    start = match.end()
    # The role's block runs until the next line indented two spaces or less that is not blank.
    rest = section[start:]
    block_end = len(rest)
    for candidate in re.finditer(r"^(?! {4})(?=\S| {2}\S)", rest, re.MULTILINE):
        if candidate.start() > 0:
            block_end = candidate.start()
            break
    block = rest[:block_end]
    key_re = re.compile(rf"^(    {re.escape(key)}:\s*)(\S+)(.*)$", re.MULTILINE)
    if key_re.search(block):
        new_block = key_re.sub(rf"\g<1>{value}\3", block, count=1)
    else:
        new_block = block.rstrip("\n") + f"\n    {key}: {value}\n"
    new_section = section[:start] + new_block + rest[block_end:]
    return text[:section_start] + new_section + text[section_end:], True


def apply_switch(text: str, adapter: str, roles: tuple[str, ...], group: str = "", *, strict: bool = True) -> str:
    """The config text with `adapter` (and optionally `independence_group`) set for `roles`.

    `strict=False` skips roles this config does not declare, which is what a bulk set needs: a
    config may legitimately omit a role it does not use, and refusing the whole operation over
    one absent block would just push the human into editing YAML by hand.
    """
    for role in roles:
        text, found = _set_role_key(text, role, "adapter", adapter)
        if not found:
            if not strict:
                continue
            raise AgentCliError(
                f"agents.{role} is not declared in .rein/config.yaml — add the role block first "
                "(the scaffold declares all five)"
            )
        if group:
            text, _ = _set_role_key(text, role, "independence_group", group)
    return text


def independence_status(config: models.Config) -> tuple[str, list[str]]:
    """(level, messages) for the actual-extractor / comparator pair. `PASS` means distinct.

    The level is decided **here**, where the branches are, and returned rather than inferred:
    recovering it from the message text would let a reworded sentence in this module silently
    downgrade a FAIL to a WARN with no test anywhere going red.

    `FAIL` blocks a critical change: an extractor and a comparator on one model share its blind
    spots, so the comparison is not a check. `WARN` is the honest middle — two models of one
    provider are mechanically distinct but share a training lineage, which is weaker than two
    providers and must not read as a green light.
    """
    left, right = (config.independence_group(role) for role in INDEPENDENT_PAIR)
    if not left or not right:
        missing = [r for r in INDEPENDENT_PAIR if not config.independence_group(r)]
        return "FAIL", [
            f"no independence_group set for: {', '.join(missing)} — a critical change cannot satisfy "
            "the independence requirement without them"
        ]
    if left == right:
        return "FAIL", [
            f"{INDEPENDENT_PAIR[0]} and {INDEPENDENT_PAIR[1]} share the independence group '{left}'. "
            "A critical change will be blocked: an extractor and a comparator on one model share its "
            "blind spots. Give them distinct groups — one model answering both halves is not a second opinion."
        ]
    if left.split("/")[0] == right.split("/")[0]:
        # A single-provider environment is the common case, so this warning is permanent for most
        # repositories. One that only names a weakness, with nothing to do about it, is a warning
        # people learn to scroll past — the remedy belongs in the same sentence.
        return "WARN", [
            f"{INDEPENDENT_PAIR[0]} ('{left}') and {INDEPENDENT_PAIR[1]} ('{right}') are distinct models of "
            "the same provider. This passes the mechanical check but is weaker than two providers — "
            "they still share a training lineage and a failure mode. A critical change cannot rest on "
            "this pair alone: satisfy it with a deterministic check, or "
            "reduce the scope that needs it."
        ]
    return "PASS", []


def independence_report(config: models.Config) -> list[str]:
    """Just the messages — for the CLI, which prints them and does not rank them."""
    return independence_status(config)[1]


def render_show(config: models.Config) -> str:
    lines = ["| role | adapter | independence group |", "|------|---------|--------------------|"]
    for role in ROLES:
        lines.append(f"| {role} | {config.adapter(role) or '-'} | {config.independence_group(role) or '-'} |")
    warnings = independence_report(config)
    if warnings:
        lines += ["", "### Independence"] + [f"- {w}" for w in warnings]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="point the AI roles at an adapter")
    parser.add_argument("adapter", nargs="?", default="", help="the adapter name to set")
    parser.add_argument("--role", default="", help=f"one role (default: all). One of: {', '.join(ROLES)}")
    parser.add_argument("--group", default="", help="independence_group to set alongside (provider/model)")
    parser.add_argument("--show", action="store_true", help="print the current roles and stop")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1

    try:
        text = repo.config.read_text(encoding="utf-8")
        config = models.Config.parse(text)
    except (OSError, models.DocumentError, strict_yaml.StrictParseError) as exc:
        logger.error(f"cannot read .rein/config.yaml: {exc}")
        return 1

    if args.show or not args.adapter:
        print(render_show(config))
        return 0

    if args.role and args.role not in ROLES:
        logger.error(f"unknown role {args.role!r} (one of {', '.join(ROLES)})")
        return 2
    if args.group and not args.role:
        logger.error("--group applies to one role — pass --role too, or the whole pair would share a group")
        return 2

    roles = (args.role,) if args.role else ROLES
    try:
        updated = apply_switch(text, args.adapter, roles, args.group, strict=bool(args.role))
        new_config = models.Config.parse(updated)
    except (AgentCliError, models.DocumentError, strict_yaml.StrictParseError) as exc:
        logger.error(str(exc))
        return 1

    repo.config.write_text(updated, encoding="utf-8")
    print(f"adapter '{args.adapter}' set for: {', '.join(roles)}")
    for warning in independence_report(new_config):
        logger.warning(warning)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
