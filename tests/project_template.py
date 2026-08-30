"""Build an expensive project fixture once per test session, then copy it.

Several suites need a project whose media is real: shots encoded with FFmpeg,
a QA pass run over them, sometimes a full delivery assembled. Building one
costs tens of seconds, and every test in the suite wants the same one, so a
function-scoped fixture pays that cost once per test for no added coverage.

``materialize_project`` builds it once per test session, keyed by name, and
hands each test a private copy: the SQLite file byte for byte, the blob store,
and the media at the root of the workspace. A test may then store assets, swap
a render or advance the project exactly as before - it is mutating its own
copy, and the identities still line up because the rows are the same rows.

The builder must leave everything it wants copied committed to the session it
is given; nothing else is preserved.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from vidgen.db import Base

#: ``(session, blob_root, workspace) -> descriptor``. The descriptor is
#: deep-copied per test, so it must hold values - identifiers, contracts -
#: rather than ORM instances bound to the builder's session.
type ProjectBuilder[T] = Callable[[Session, Path, Path], T]


@dataclass(frozen=True, slots=True)
class _Template[T]:
    root: Path
    value: T


_TEMPLATES: dict[str, _Template[object]] = {}


def _template[T](key: str, build: ProjectBuilder[T]) -> _Template[T]:
    cached = _TEMPLATES.get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    root = Path(tempfile.mkdtemp(prefix=f"vidgen-template-{key}-"))
    atexit.register(shutil.rmtree, root, True)
    engine = create_engine(f"sqlite+pysqlite:///{root / 'template.db'}")
    Base.metadata.create_all(engine)
    try:
        with sessionmaker(bind=engine, expire_on_commit=False)() as session:
            value = build(session, root / "blobs", root / "work")
            session.commit()
    finally:
        # The database file is copied byte for byte, so nothing may still hold
        # a connection to it.
        engine.dispose()
    # The PNG frames the shot encodes were built from are never read again and
    # dwarf everything else on disk.
    for frames in root.rglob("*-frames"):
        if frames.is_dir():
            shutil.rmtree(frames, ignore_errors=True)
    template = _Template(root=root, value=value)
    _TEMPLATES[key] = template
    return template


def materialize_project[T](
    key: str,
    build: ProjectBuilder[T],
    *,
    database_path: Path,
    blob_root: Path,
    workspace: Path,
) -> T:
    """Copy the project named by ``key`` into one test's database and files.

    Call this before opening an engine on ``database_path``: the file is
    replaced wholesale, so an engine already connected to it would be reading
    a database that no longer exists.
    """
    template = _template(key, build)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template.root / "template.db", database_path)
    shutil.copytree(template.root / "blobs", blob_root, dirs_exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    for entry in sorted((template.root / "work").iterdir()):
        if entry.is_file():
            shutil.copyfile(entry, workspace / entry.name)
    return deepcopy(template.value)
