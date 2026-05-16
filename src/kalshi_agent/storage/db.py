"""Database session management. SQLite in WAL mode for concurrent read/write."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from kalshi_agent.storage.models import Base, SchemaVersion

SCHEMA_VERSION = 1


def _enable_sqlite_pragmas(dbapi_conn, _) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    event.listen(engine, "connect", _enable_sqlite_pragmas)
    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        current = s.query(SchemaVersion).order_by(SchemaVersion.version.desc()).first()
        if current is None:
            s.add(SchemaVersion(version=SCHEMA_VERSION))
            s.commit()


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def healthcheck(engine: Engine) -> bool:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return True
