"""The Diff Fact Detector is deterministic, so every signal and every coverage gap is a fixed
fact these tests pin down (plan §13, §30.8).

The honesty of the Coverage Manifest is the point under test: a binary blob, an unsupported
language, or an unevaluated dependency change must make coverage `insufficient`, so that "Extra
Behavior: 0" can never be produced from a diff the detector could not fully read.
"""

from __future__ import annotations

import json

from rein import data, diff_facts


def _diff(path: str, added: list[str] | None = None, removed: list[str] | None = None) -> str:
    body = [f"diff --git a/{path} b/{path}", "--- a/" + path, "+++ b/" + path, "@@ -1,3 +1,4 @@"]
    body += [f"-{line}" for line in removed or []]
    body += [f"+{line}" for line in added or []]
    return "\n".join(body) + "\n"


# --- signal detection (plan §13.2) --------------------------------------------


def test_timeout_is_a_failure_policy_signal() -> None:
    facts = diff_facts.analyze(_diff("src/client.py", added=["    resp = get(url, timeout=30)"]))
    assert any(hit.signal == "failure_policy" for hit in facts.signals)


def test_auth_keyword_is_a_high_security_boundary() -> None:
    facts = diff_facts.analyze(_diff("src/api.py", added=["    if not user.has_permission('admin'):"]))
    hit = next(h for h in facts.signals if h.signal == "security_boundary")
    assert hit.risk == "high"
    assert facts.risk_floor == "high"


def test_side_effect_delete_is_high() -> None:
    facts = diff_facts.analyze(_diff("src/store.py", added=["    os.remove(path)  # delete the file"]))
    assert any(h.signal == "side_effect" and h.risk == "high" for h in facts.signals)


def test_deleted_guard_fires_on_removed_lines_only() -> None:
    # An `if` guard that used to be there and is now gone — the quietly-removed-safety case.
    removed = diff_facts.analyze(_diff("src/pay.py", removed=["    if amount <= 0: raise ValueError"]))
    assert any(h.signal == "deleted_guard" and h.risk == "high" for h in removed.signals)
    # The same text merely *added* is not a deleted guard.
    added = diff_facts.analyze(_diff("src/pay.py", added=["    if amount <= 0: raise ValueError"]))
    assert not any(h.signal == "deleted_guard" for h in added.signals)


def test_broad_catch_is_a_swallowed_failure() -> None:
    facts = diff_facts.analyze(_diff("src/job.py", added=["    except Exception:", "        pass"]))
    assert any(h.signal == "swallowed_failure" for h in facts.signals)


def test_dependency_lock_change_is_matched_by_path() -> None:
    facts = diff_facts.analyze(_diff("uv.lock", added=["+requests==2.99.0"]))
    assert any(h.signal == "dependency" for h in facts.signals)


def test_migration_path_is_high() -> None:
    facts = diff_facts.analyze(_diff("migrations/0002_add_column.sql", added=["ALTER TABLE users ADD col int;"]))
    assert any(h.signal == "migration" and h.risk == "high" for h in facts.signals)


def test_a_plain_change_has_no_signals_and_a_low_floor() -> None:
    facts = diff_facts.analyze(_diff("src/util.py", added=["    return x + 1"]))
    assert facts.signals == ()
    assert facts.risk_floor == "low"


# --- coverage manifest (plan §13.3) -------------------------------------------


def test_python_is_analyzed_with_ast() -> None:
    facts = diff_facts.analyze(_diff("src/util.py", added=["    return 1"]))
    assert facts.coverage.languages == {"python": "ast"}
    assert facts.coverage.analyzed_files == 1
    assert facts.coverage.coverage_status == "sufficient"


def test_unsupported_language_makes_coverage_insufficient() -> None:
    facts = diff_facts.analyze(_diff("native/module.zig", added=["const x = 1;"]))
    assert facts.coverage.coverage_status == "insufficient"
    assert facts.coverage.unsupported_files[0]["path"] == "native/module.zig"


def test_plain_text_formats_are_scanned_not_declared_unreadable() -> None:
    """`token_only` is a real method: the signal detector reads every changed line of these.

    Filing a stylesheet or a Dockerfile under "unsupported language" claimed less than was
    actually done, and cost gate ④ its exit — there is no scope split that removes the file.
    """
    for path in ("web/app.css", "Dockerfile", "makefile", "deploy/main.tf"):
        facts = diff_facts.analyze(_diff(path, added=["a = 1"]))
        assert facts.coverage.unsupported_files == (), path
        assert facts.coverage.coverage_status == "sufficient", path


def test_a_format_nothing_here_can_tokenize_is_still_unsupported() -> None:
    facts = diff_facts.analyze(_diff("design/logo.psd", added=["\\x00binaryish"]))
    assert facts.coverage.unsupported_files[0]["path"] == "design/logo.psd"


def test_a_manifest_naming_an_unread_file_can_actually_be_written() -> None:
    """The reason has to be the schema's vocabulary, or `review generate` cannot store its own
    output: a free-text reason failed validation, so no review of a diff with an unread file in
    it was writable at all. The extension survives in `detail`, which is what makes it fixable."""
    from rein import models

    facts = diff_facts.analyze(_diff("design/logo.psd", added=["x"]))
    entry = facts.coverage.to_manifest()
    assert entry["unsupported_files"] == [
        {"path": "design/logo.psd", "reason": "unsupported_language", "detail": "no analyzer for .psd"}
    ]
    document = {
        "machine": {
            "status": "generated",
            "binding": {
                "change_digest": "sha256:" + "a" * 64,
                "plan_digest": "sha256:" + "b" * 64,
                "environment_digest": "sha256:" + "c" * 64,
            },
            "coverage": entry,
            "actual_extraction": [],
            "claims": [],
        },
        "human": {"status": "not_started"},
    }
    assert models.schema_errors(document, "review") == []


def test_binary_file_is_unsupported_and_insufficient() -> None:
    diff = "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
    facts = diff_facts.analyze(diff)
    assert facts.coverage.coverage_status == "insufficient"
    assert facts.coverage.binary_semantics_analyzed is False
    assert facts.coverage.unsupported_files[0]["reason"] == "binary"


def test_generated_file_is_recorded_not_analyzed() -> None:
    facts = diff_facts.analyze(_diff("src/generated/client.py", added=["def call(): ..."]))
    assert facts.coverage.generated_files[0]["path"] == "src/generated/client.py"
    assert facts.coverage.coverage_status == "insufficient"


def test_generated_marker_in_content_is_detected() -> None:
    facts = diff_facts.analyze(_diff("src/client.py", added=["# @generated by openapi", "def call(): ..."]))
    assert facts.coverage.generated_files
    assert facts.coverage.coverage_status == "insufficient"


def test_dependency_change_leaves_semantics_unanalyzed() -> None:
    facts = diff_facts.analyze(_diff("pyproject.toml", added=['requests = "^2.99"']))
    assert facts.coverage.dependency_semantics_analyzed is False
    assert facts.coverage.coverage_status == "insufficient"


def test_a_huge_diff_is_read_whole_and_says_so() -> None:
    """The detector neither splits nor truncates: it reads all of it, or names what it could not.

    A change too big to review in one sitting is refused by `review._refuse_over_budget` before a
    model is launched. There is no size at which the manifest starts describing a fragment.
    """
    added = [f"    x{i} = {i}" for i in range(4000)]
    diff = _diff("src/big.py", added=added)
    facts = diff_facts.analyze(diff)
    assert facts.coverage.analyzed_files == 1
    assert facts.coverage.analyzed_bytes == len(diff.encode("utf-8"))
    assert facts.coverage.coverage_status == "sufficient"


def test_coverage_manifest_only_emits_schema_fields() -> None:
    """`machine.coverage` is `additionalProperties: false`, so an unknown key is a rejected review.

    The permitted set is read from the packaged schema rather than restated here: a hand-copied
    list is one more place to forget, and forgetting it means the detector emits a manifest the
    loader refuses — with the failure surfacing at `review generate`, far from the field that
    caused it.
    """
    schema = json.loads(data.read_text("schema/review.schema.json"))
    allowed = set(schema["$defs"]["machine"]["properties"]["coverage"]["properties"])

    facts = diff_facts.analyze(_diff("src/util.py", added=["    return 1"]))
    manifest = facts.coverage.to_manifest()
    assert set(manifest) <= allowed
    assert str(manifest["diff_digest"]).startswith("sha256:")


def test_coverage_records_the_diff_size_the_budget_is_denominated_in() -> None:
    """`max_diff_bytes` needs a measured actual, or it is a budget that cannot blow."""
    diff = _diff("src/util.py", added=["    return 1"])
    facts = diff_facts.analyze(diff)
    assert facts.coverage.analyzed_bytes == len(diff.encode("utf-8"))
    assert facts.coverage.to_manifest()["analyzed_bytes"] == facts.coverage.analyzed_bytes


def test_parse_handles_multiple_files() -> None:
    diff = _diff("a.py", added=["x = 1"]) + _diff("b.py", added=["y = 2"])
    files = diff_facts.parse_diff(diff)
    assert {f.path for f in files} == {"a.py", "b.py"}
