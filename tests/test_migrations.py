from __future__ import annotations

from pathlib import Path

import pytest

from hoist import migrations
from hoist.errors import DeployError

REAL_VERSIONS = Path(__file__).resolve().parents[3] / "backend" / "alembic" / "versions"


def write(dir: Path, name: str, rev: str, down: str | None, upgrade: str, downgrade: str = "pass") -> None:
    (dir / f"{name}.py").write_text(
        f'revision: str = "{rev}"\ndown_revision = {down!r}\n'
        f"from alembic import op\nimport sqlalchemy as sa\n"
        f"def upgrade():\n{upgrade}\ndef downgrade():\n    {downgrade}\n",
        encoding="utf-8",
    )


@pytest.fixture
def versions(tmp_path: Path) -> Path:
    write(tmp_path, "a_first", "aaa", None, "    op.create_table('t', sa.Column('id', sa.Integer))")
    write(tmp_path, "b_second", "bbb", "aaa", "    op.add_column('t', sa.Column('x', sa.Text(), nullable=True))",
          downgrade="op.drop_column('t', 'x')")
    write(tmp_path, "c_third", "ccc", "bbb", "    op.drop_column('t', 'x')")
    return tmp_path


def test_head_and_pending(versions: Path) -> None:
    revs = migrations.load_revisions(versions)
    assert migrations.head_of(revs) == "ccc"
    assert [r.id for r in migrations.pending(revs, "aaa")] == ["bbb", "ccc"]
    assert migrations.pending(revs, "ccc") == []
    assert len(migrations.pending(revs, None)) == 3


def test_unknown_server_revision_is_refused(versions: Path) -> None:
    revs = migrations.load_revisions(versions)
    with pytest.raises(DeployError, match="does not know"):
        migrations.pending(revs, "zzz")


def test_additive_migration_is_clean_and_downgrade_is_ignored(versions: Path) -> None:
    revs = migrations.load_revisions(versions)
    assert migrations.scan(migrations.pending(revs, "aaa")[:1]) == []  # bbb: its downgrade drops, upgrade adds


def test_drop_column_blocks(versions: Path) -> None:
    revs = migrations.load_revisions(versions)
    findings = migrations.scan(migrations.pending(revs, "bbb"))
    assert [(f.revision, f.blocking) for f in findings] == [("ccc", True)]
    assert "drops a column" in findings[0].message


@pytest.mark.parametrize("body,expect", [
    ("op.alter_column('t', 'a', new_column_name='b')", "renames a column"),
    ("op.alter_column('t', 'a', type_=sa.Text())", "changes a column type"),
    ("op.alter_column('t', 'a', nullable=False)", "NOT NULL"),
    ("op.drop_table('t')", "drops a table"),
    ("op.rename_table('t', 'u')", "renames a table"),
    ("op.execute('DROP TABLE t')", "destructive"),
    ("op.execute('ALTER TABLE t RENAME COLUMN a TO b')", "destructive"),
])
def test_breaking_patterns(tmp_path: Path, body: str, expect: str) -> None:
    write(tmp_path, "a", "aaa", None, f"    {body}")
    findings = migrations.scan(migrations.pending(migrations.load_revisions(tmp_path), None))
    assert findings and findings[0].blocking and expect in findings[0].message


def test_nullable_true_and_safe_execute_are_fine(tmp_path: Path) -> None:
    write(tmp_path, "a", "aaa", None,
          "    op.alter_column('t', 'a', nullable=True)\n    op.execute('UPDATE t SET a = 1')")
    assert migrations.scan(migrations.pending(migrations.load_revisions(tmp_path), None)) == []


def test_plain_index_is_a_note_not_a_block(tmp_path: Path) -> None:
    write(tmp_path, "a", "aaa", None, "    op.create_index('ix', 't', ['a'])")
    findings = migrations.scan(migrations.pending(migrations.load_revisions(tmp_path), None))
    assert len(findings) == 1 and not findings[0].blocking


def test_index_on_a_table_created_in_the_same_migration_is_clean(tmp_path: Path) -> None:
    write(tmp_path, "a", "aaa", None,
          "    op.create_table('t', sa.Column('a', sa.Integer))\n    op.create_index('ix', 't', ['a'])")
    assert migrations.scan(migrations.pending(migrations.load_revisions(tmp_path), None)) == []


def test_concurrent_index_is_clean(tmp_path: Path) -> None:
    write(tmp_path, "a", "aaa", None, "    op.create_index('ix', 't', ['a'], postgresql_concurrently=True)")
    assert migrations.scan(migrations.pending(migrations.load_revisions(tmp_path), None)) == []


def test_multiple_heads_are_refused(tmp_path: Path) -> None:
    write(tmp_path, "a", "aaa", None, "    pass")
    write(tmp_path, "b", "bbb", "aaa", "    pass")
    write(tmp_path, "c", "ccc", "aaa", "    pass")
    with pytest.raises(DeployError, match="exactly one Alembic head"):
        migrations.head_of(migrations.load_revisions(tmp_path))


@pytest.mark.skipif(not REAL_VERSIONS.is_dir(), reason="backend migrations not in this checkout")
def test_parses_the_real_history() -> None:
    revs = migrations.load_revisions(REAL_VERSIONS)
    head = migrations.head_of(revs)
    assert head in revs
    assert migrations.pending(revs, head) == []
