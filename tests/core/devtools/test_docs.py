"""Tests for devtools docs tool (DOC-1..DOC-10)."""

from __future__ import annotations

from pathlib import Path

from algodesk.api.checks import Severity
from algodesk.devtools.docs import (
    extract_summary,
    format_docs_text,
    make_docs_check,
    run_docs_check,
    write_index,
)


def test_extract_summary_finds_heading_and_body() -> None:
    doc = (
        "# Title\n\n"
        "## Summary\n\n"
        "This is the summary of the project.\n"
        "It spans two lines.\n\n"
        "## Details\n\n"
        "More details.\n"
    )
    line, text = extract_summary(doc)
    assert line == 3
    assert text == "This is the summary of the project.\nIt spans two lines."


def test_extract_summary_beyond_line_40_is_rejected() -> None:
    lines = ["line\n"] * 45
    lines.append("## Summary\n\nSummary text\n")
    doc = "".join(lines)
    line, text = extract_summary(doc)
    assert line == 0
    assert text == ""


def test_docs_check_clean_repo_passes(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("## Summary\n\nValid root readme summary.\n")
    (tmp_path / "AGENTS.md").write_text("## Summary\n\nValid agents document summary.\n")
    rep = run_docs_check(tmp_path)
    assert rep.checked == 2
    assert len(rep.errors) == 0
    assert len(rep.warnings) == 0
    assert rep.exit_code == 0
    txt = format_docs_text(rep)
    assert txt.startswith("DOCS: 2 documents checked, 0 errors, 0 warnings")


def test_docs_check_missing_summary_heading(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Only a title without summary\n\nContent.\n")
    rep = run_docs_check(tmp_path)
    assert len(rep.errors) == 1
    assert rep.errors[0].rule == "docs.summary_heading"
    assert rep.exit_code == 1


def test_docs_check_empty_summary(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("## Summary\n\n## Next Header\n")
    rep = run_docs_check(tmp_path)
    assert len(rep.errors) == 1
    assert rep.errors[0].rule == "docs.summary_empty"


def test_docs_check_summary_exceeds_max_words(tmp_path: Path) -> None:
    words = "word " * 160
    (tmp_path / "README.md").write_text(f"## Summary\n\n{words}\n")
    rep = run_docs_check(tmp_path, {"docs.max_summary_words": 150})
    assert len(rep.errors) == 1
    assert rep.errors[0].rule == "docs.summary_length"
    assert "160" in rep.errors[0].message


def test_docs_check_broken_relative_link_reports_file_and_line(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "## Summary\n\nValid summary.\n\nHere is a [broken link](missing_file.md).\n"
    )
    rep = run_docs_check(tmp_path)
    assert len(rep.errors) == 1
    err = rep.errors[0]
    assert err.rule == "docs.broken_link"
    assert err.path == "README.md"
    assert err.line == 5
    assert "missing_file.md" in err.message


def test_docs_check_valid_relative_link_passes(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "## Summary\n\nValid summary.\n\nHere is a [link](doc.md).\n"
    )
    (tmp_path / "doc.md").write_text("content\n")
    rep = run_docs_check(tmp_path)
    assert len(rep.errors) == 0


def test_docs_check_ignores_external_and_anchor_links(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "## Summary\n\nValid summary.\n\n"
        "Links: [web](https://example.com) [mail](mailto:test@test.com) [anchor](#summary)\n"
    )
    rep = run_docs_check(tmp_path)
    assert len(rep.errors) == 0


def test_docs_check_ignores_workbench_directory(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("## Summary\n\nValid summary.\n")
    wb = tmp_path / "_workbench"
    wb.mkdir()
    (wb / "broken.md").write_text("No summary here and [broken](bad.md)\n")
    rep = run_docs_check(tmp_path)
    # _workbench/broken.md must NOT be checked
    assert rep.checked == 1
    assert len(rep.errors) == 0


def test_docs_check_plugin_readme_is_checked(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("## Summary\n\nValid root summary.\n")
    pl = tmp_path / "plugins" / "notifier" / "test"
    pl.mkdir(parents=True)
    (pl / "README.md").write_text("## Summary\n\nValid plugin readme summary.\n")
    rep = run_docs_check(tmp_path)
    assert rep.checked == 2
    assert len(rep.errors) == 0


def test_docs_check_living_docs_limit_exceeded(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    for i in range(5):
        (docs_dir / f"doc{i}.md").write_text(
            f"## Summary\n\nSummary {i}.\n\n<!-- Owner: core, Version: 1.0 -->\n"
        )
    # Write required INDEX and STATUS
    write_index(tmp_path)
    (docs_dir / "STATUS.md").write_text(
        "## Summary\n\nStatus summary.\n\n<!-- Owner: core, Version: 1.0 -->\n"
    )
    write_index(tmp_path)

    # With limit 3, 5 + 2 = 7 docs exceeds 3
    rep = run_docs_check(tmp_path, {"docs.max_living_docs": 3})
    assert any(e.rule == "docs.living_docs_limit" for e in rep.errors)


def test_docs_check_requires_index_and_status_when_docs_dir_exists(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text(
        "## Summary\n\nGuide summary.\n\n<!-- Owner: core, Version: 1.0 -->\n"
    )
    rep = run_docs_check(tmp_path)
    err_rules = [e.rule for e in rep.errors]
    assert "docs.required_file" in err_rules


def test_docs_check_status_word_count_limit(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    write_index(tmp_path)
    long_status = "word " * 450
    (docs_dir / "STATUS.md").write_text(
        f"## Summary\n\nStatus summary.\n\n<!-- Owner: core, Version: 1.0 -->\n\n{long_status}"
    )
    write_index(tmp_path)
    rep = run_docs_check(tmp_path)
    assert any(e.rule == "docs.status_length" for e in rep.errors)


def test_docs_check_index_stale_detected(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    write_index(tmp_path)
    (docs_dir / "STATUS.md").write_text(
        "## Summary\n\nStatus summary.\n\n<!-- Owner: core, Version: 1.0 -->\n"
    )
    # STATUS was added after write_index, so INDEX is stale!
    rep = run_docs_check(tmp_path)
    assert any(e.rule == "docs.index_stale" for e in rep.errors)


def test_write_index_is_deterministic_and_idempotent(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "STATUS.md").write_text(
        "## Summary\n\nStatus of milestones.\n\n<!-- Owner: core, Version: 1.0 -->\n"
    )
    (docs_dir / "arch.md").write_text(
        "## Summary\n\nArchitecture doc.\n\n<!-- Owner: core, Version: 1.0 -->\n"
    )

    p1 = write_index(tmp_path)
    content1 = p1.read_text(encoding="utf-8")

    p2 = write_index(tmp_path)
    content2 = p2.read_text(encoding="utf-8")

    assert content1 == content2
    assert "generated, do not edit" in content1
    assert "arch.md" in content1
    assert "STATUS.md" in content1


def test_docs_check_metadata_warning_for_docs_files(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "doc.md").write_text("## Summary\n\nSummary text.\n")
    rep = run_docs_check(tmp_path)
    warns = [w for w in rep.warnings if w.path == "docs/doc.md"]
    assert len(warns) == 1
    assert warns[0].rule == "docs.metadata_missing"


def test_docs_check_with_valid_metadata_has_no_warnings(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("## Summary\n\nValid root summary.\n")
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "STATUS.md").write_text(
        "## Summary\n\nStatus summary.\n\n<!-- Owner: core | Version: 1.0 -->\n"
    )
    write_index(tmp_path)
    rep = run_docs_check(tmp_path)
    assert len(rep.warnings) == 0
    assert len(rep.errors) == 0


def test_make_docs_check_doctor_pass_and_fail(tmp_path: Path) -> None:
    class FakeRuntime:
        root = tmp_path

    (tmp_path / "README.md").write_text("## Summary\n\nValid summary.\n")
    rt = FakeRuntime()
    chk = make_docs_check(rt)
    assert chk.id == "docs.rules"
    assert chk.severity == Severity.WARNING

    # Valid doc -> ok
    r1 = chk.run()
    assert r1.ok is True

    # Broken doc -> fail
    (tmp_path / "README.md").write_text("No summary here\n")
    r2 = chk.run()
    assert r2.ok is False
    assert "Docs check failed" in r2.message


def test_docs_check_ignores_non_markdown_files(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "STATUS.md").write_text(
        "## Summary\n\nStatus summary.\n\n<!-- Owner: core | Version: 1.0 -->\n"
    )
    write_index(tmp_path)
    (docs_dir / "data.json").write_text('{"key": "val"}\n')
    (docs_dir / "script.py").write_text("print('hello')\n")
    (tmp_path / "README.md").write_text("## Summary\n\nValid root summary.\n")
    rep = run_docs_check(tmp_path)
    assert len(rep.errors) == 0


def test_docs_check_valid_external_links_skipped(tmp_path: Path) -> None:
    content = (
        "## Summary\n\nValid summary with [External](https://example.com) "
        "and [Mail](mailto:user@example.com).\n"
    )
    (tmp_path / "README.md").write_text(content)
    rep = run_docs_check(tmp_path)
    assert len(rep.errors) == 0


def test_docs_check_anchor_in_markdown_file(tmp_path: Path) -> None:
    content = (
        "## Summary\n\nValid root summary with [Anchor](#section-one).\n\n"
        "### Section One\n\nText.\n"
    )
    (tmp_path / "README.md").write_text(content)
    rep = run_docs_check(tmp_path)
    assert len(rep.errors) == 0


def test_docs_check_plugins_readme_validated(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("## Summary\n\nValid root summary.\n")
    plugin_dir = tmp_path / "plugins" / "my_broker"
    plugin_dir.mkdir(parents=True)
    # Plugin README with no summary
    (plugin_dir / "README.md").write_text("# My Broker\n\nNo summary section.\n")
    rep = run_docs_check(tmp_path)
    errors = [e for e in rep.errors if "plugins/my_broker/README.md" in e.path]
    assert len(errors) == 1
    assert errors[0].rule == "docs.summary_heading"


def test_docs_index_overwrites_existing_file(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "doc1.md").write_text("## Summary\n\nDoc 1 summary.\n")
    index_file = write_index(tmp_path)
    first_content = index_file.read_text()
    assert "doc1.md" in first_content

    # Overwrite
    (docs_dir / "doc2.md").write_text("## Summary\n\nDoc 2 summary.\n")
    write_index(tmp_path)
    second_content = index_file.read_text()
    assert "doc2.md" in second_content
