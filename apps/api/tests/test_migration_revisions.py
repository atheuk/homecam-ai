"""Guards on the Alembic revision identifiers themselves."""
import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
# alembic_version.version_num is VARCHAR(32); a longer id passes every local
# test and then fails in production at the very last statement of the
# migration, after the DDL has already run.
MAX_REVISION_LENGTH = 32

_REVISION = re.compile(r'^\s*revision\s*=\s*"([^"]+)"', re.MULTILINE)
_DOWN_REVISION = re.compile(r'down_revision\s*=\s*(?:"([^"]+)"|None)')


def _revisions() -> dict[str, tuple[str, str | None]]:
    found: dict[str, tuple[str, str | None]] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        match = _REVISION.search(text)
        assert match, f"{path.name} declares no revision id"
        down = _DOWN_REVISION.search(text)
        assert down, f"{path.name} declares no down_revision"
        found[match.group(1)] = (path.name, down.group(1))
    return found


def test_every_revision_id_fits_the_version_column():
    too_long = {
        name: revision
        for revision, (name, _) in _revisions().items()
        if len(revision) > MAX_REVISION_LENGTH
    }
    assert not too_long, f"revision ids longer than {MAX_REVISION_LENGTH} chars: {too_long}"


def test_the_revision_chain_has_no_dangling_parent():
    """Renaming a revision must drag its children along.

    A child left pointing at the old id only fails when alembic actually
    walks the chain, which is to say during a deployment.
    """
    revisions = _revisions()
    dangling = {
        name: down
        for _, (name, down) in revisions.items()
        if down is not None and down not in revisions
    }
    assert not dangling, f"down_revision targets that do not exist: {dangling}"
