"""Behavioral tests for the bundled ``skills`` views."""

import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from acpc import skills, vocab
from acpc.cli import main


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "state"))


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


def invoke(cli: CliRunner, *args: str):
    return cli.invoke(main, list(args), catch_exceptions=False)


def _resource_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "bundled"
    root.mkdir()
    monkeypatch.setattr(skills, "_bundled_resources", lambda: iter(sorted(root.iterdir())))
    return root


def _write_skill(root: Path, name: str, content: str) -> Path:
    directory = root / name
    directory.mkdir()
    (directory / "SKILL.md").write_text(content, encoding="utf-8", newline="")
    return directory


def test_frontmatter_scalars_block_forms_and_tolerated_missing_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _resource_root(tmp_path, monkeypatch)
    _write_skill(
        root,
        "plain",
        "---\nname: frontmatter-name\ndescription: plain value\n---\nplain body\n",
    )
    _write_skill(
        root,
        "quoted",
        '---\nname: quoted\ndescription: "quoted value"\n---\nquoted body\n',
    )
    _write_skill(
        root,
        "folded-strip",
        "---\ndescription: >-\n  first line\n  second line\n---\nbody\n",
    )
    _write_skill(
        root,
        "folded-keep",
        "---\ndescription: >\n  first line\n  second line\n---\nbody\n",
    )
    _write_skill(
        root,
        "literal",
        "---\ndescription: |\n  first line\n  second line\n---\nbody\n",
    )
    _write_skill(root, "missing", "---\nname: missing\n---\nbody\n")
    _write_skill(root, "none", "# no frontmatter\nbody\n")
    _write_skill(root, "unterminated", "---\ndescription: >-\n  never closed\n")

    found = {skill.name: skill for skill in skills.list_skills()}

    assert found["plain"].description == "plain value"
    assert found["quoted"].description == "quoted value"
    assert found["folded-strip"].description == "first line second line"
    assert found["folded-keep"].description == "first line second line\n"
    assert found["literal"].description == "first line\nsecond line\n"
    assert found["missing"].description is None
    assert found["none"].description is None
    assert found["unterminated"].description is None


def test_directory_name_wins_over_frontmatter_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _resource_root(tmp_path, monkeypatch)
    directory = _write_skill(
        root,
        "filesystem-name",
        "---\nname: frontmatter-name\ndescription: present\n---\nbody\n",
    )

    skill = skills.get_skill("filesystem-name")

    assert skill.name == "filesystem-name"
    assert skill.path == directory
    with pytest.raises(skills.SkillNotFoundError):
        skills.get_skill("frontmatter-name")


def test_list_has_header_alignment_sorting_and_bounded_rendered_description(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _resource_root(tmp_path, monkeypatch)
    _write_skill(root, "zeta", "---\ndescription: last\n---\nz\n")
    _write_skill(
        root,
        "alpha-name-longer-than-header",
        "---\ndescription: >-\n  A long description with   repeated whitespace\n"
        "  and enough words to exceed the eighty character roster budget safely.\n"
        "---\na\n",
    )

    result = invoke(cli, "skills")

    assert result.exit_code == vocab.EXIT_OK
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["name", "description"]
    assert [line.split()[0] for line in lines[1:]] == [
        "alpha-name-longer-than-header",
        "zeta",
    ]
    header = lines[0]
    alpha = lines[1]
    assert alpha.index("A long") == header.index("description")
    assert alpha.endswith("...")
    assert len(alpha.split("  ", 1)[1]) <= 80
    assert "repeated whitespace and" in alpha


def test_detail_preserves_body_and_places_directory_metadata_on_stderr(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _resource_root(tmp_path, monkeypatch)
    body = "# Exact body\n\nUnicode: café\nwithout final newline"
    directory = _write_skill(root, "exact", f"---\ndescription: x\n---\n{body}")

    result = invoke(cli, "skills", "exact")

    assert result.exit_code == vocab.EXIT_OK
    assert result.stdout == body
    assert f"-- skill exact | dir {directory}" in result.stderr
    assert str(directory) not in result.stdout


def test_json_views_include_paths_body_and_null_description(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _resource_root(tmp_path, monkeypatch)
    directory = _write_skill(root, "json-skill", "---\nname: ignored\n---\nbody\n")

    listed = invoke(cli, "skills", "--json")
    detail = invoke(cli, "skills", "json-skill", "--json")

    assert listed.exit_code == vocab.EXIT_OK
    assert detail.exit_code == vocab.EXIT_OK
    list_payload = json.loads(listed.stdout)
    detail_payload = json.loads(detail.stdout)
    assert list_payload == {
        "skills": [
            {
                "name": "json-skill",
                "description": None,
                "path": str(directory),
            }
        ]
    }
    assert detail_payload == {
        "name": "json-skill",
        "description": None,
        "path": str(directory),
        "body": "body\n",
    }
    assert f"-- skill json-skill | dir {directory}" in detail.stderr

    before_the_name = invoke(cli, "skills", "--json", "json-skill")
    assert before_the_name.exit_code == vocab.EXIT_OK
    assert json.loads(before_the_name.stdout) == detail_payload


def test_unknown_skill_is_usage_error_pointing_at_listing_command(cli: CliRunner) -> None:
    result = invoke(cli, "skills", "does-not-exist")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "acpc skills" in result.stderr


def test_directory_without_skill_file_is_skipped(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _resource_root(tmp_path, monkeypatch)
    (root / "half-installed").mkdir()
    _write_skill(root, "complete", "---\ndescription: ready\n---\nbody\n")

    result = invoke(cli, "skills")

    assert result.exit_code == vocab.EXIT_OK
    assert "complete" in result.stdout
    assert "half-installed" not in result.stdout


def test_skill_help_pages_document_json(cli: CliRunner) -> None:
    group = invoke(cli, "skills", "--help")
    detail = invoke(cli, "skills", "provider-bringup", "--help")

    assert group.exit_code == vocab.EXIT_OK
    assert detail.exit_code == vocab.EXIT_OK
    assert "--json" in group.stdout
    assert "--json" in detail.stdout


def test_wheel_contains_bundled_skill_files(tmp_path: Path) -> None:
    project = Path(__file__).parents[1]
    cache_dir = tmp_path / "uv-cache"
    env = {**os.environ, "UV_CACHE_DIR": str(cache_dir)}
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "acpc/data/skills/provider-bringup/SKILL.md" in names
    assert "acpc/data/skills/provider-bringup/references/openrouter.md" in names
