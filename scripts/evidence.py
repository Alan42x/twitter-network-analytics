"""Content identities used to reject stale validation evidence."""
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_hashes(paths):
    return {name: sha256(ROOT / name) for name in paths}


def project_sources():
    paths = [path.relative_to(ROOT).as_posix()
             for folder in ("analysis", "backend", "scripts", "tests")
             for path in (ROOT / folder).rglob("*.py")]
    return sorted(paths + ["infra/cloudformation.yaml", "artifacts/cloudformation.generated.json"])


def sources_match(record, expected=None):
    if not isinstance(record, dict):
        return False
    sources = record.get("source_sha256", {})
    if not isinstance(sources, dict) or not sources or (expected is not None and set(sources) != set(expected)):
        return False
    for name, value in sources.items():
        if (not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts
                or not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)):
            return False
        try:
            if not (ROOT / name).is_file() or sha256(ROOT / name) != value:
                return False
        except OSError:
            return False
    return True
