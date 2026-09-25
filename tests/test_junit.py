from __future__ import annotations

from pathlib import Path

from rein import junit


def _report(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(f"<testsuites><testsuite name='s'>{body}</testsuite></testsuites>", encoding="utf-8")
    return path


def test_failures_and_errors_are_failing_and_a_pass_is_not(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        "<testcase classname='tests.a' name='t1'><failure message='x'/></testcase>"
        "<testcase file='tests/b.py' classname='tests.b' name='t2'><error message='x'/></testcase>"
        "<testcase classname='tests.a' name='t3'/>",
    )
    assert junit.failing(report) == frozenset({"tests.a::t1", "tests/b.py::t2"})


def test_no_readable_report_is_not_an_empty_one(tmp_path: Path) -> None:
    assert junit.failing(tmp_path / "absent.xml") is None
    broken = tmp_path / "broken.xml"
    broken.write_text("<testsuites>", encoding="utf-8")
    assert junit.failing(broken) is None
    assert junit.failing(_report(tmp_path, "")) == frozenset()


def test_a_dotted_classname_resolves_to_the_file_that_exists(tmp_path: Path) -> None:
    (tmp_path / "tests" / "store").mkdir(parents=True)
    (tmp_path / "tests" / "store" / "test_concurrency.py").write_text("", encoding="utf-8")
    node = "tests.store.test_concurrency.TestWal::test_wal"
    assert junit.node_path(node, tmp_path) == "tests/store/test_concurrency.py"
    assert junit.node_path("tests/x.py::t", tmp_path) == "tests/x.py"
    assert junit.node_path("nowhere.at.all::t", tmp_path) == ""
