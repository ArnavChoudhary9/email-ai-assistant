from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from email_intel.storage.schema import Base


def make_engine(db_path: Path | str) -> Engine:
    if str(db_path) == ":memory:":
        url = "sqlite:///:memory:"
    else:
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{p.as_posix()}"
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
