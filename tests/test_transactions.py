from __future__ import annotations

import os

from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from mneme.db.transactions import transaction  # noqa: E402


def _make_session_and_table() -> tuple[Session, Table, Engine]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata = MetaData()
    table = Table(
        "items",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("value", Integer, nullable=False),
    )
    metadata.create_all(engine)
    return Session(engine), table, engine


def test_transaction_commits_after_autobegin_read() -> None:
    db, table, engine = _make_session_and_table()
    try:
        db.execute(text("SELECT 1"))
        assert db.get_transaction() is not None

        with transaction(db):
            db.execute(table.insert().values(id=1, value=10))
        assert not db.in_transaction(), db.get_transaction().origin
    finally:
        db.close()

    with Session(engine) as verify_db:
        value = verify_db.execute(select(table.c.value)).scalar_one()

    assert value == 10


def test_transaction_rolls_back_after_autobegin_read_on_error() -> None:
    db, table, engine = _make_session_and_table()
    try:
        db.execute(text("SELECT 1"))

        try:
            with transaction(db):
                db.execute(table.insert().values(id=1, value=10))
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass
    finally:
        db.close()

    with Session(engine) as verify_db:
        count = verify_db.execute(select(table.c.id)).scalars().all()

    assert count == []


def test_transaction_preserves_explicit_outer_transaction_ownership() -> None:
    db, table, engine = _make_session_and_table()
    try:
        try:
            with db.begin():
                with transaction(db):
                    db.execute(table.insert().values(id=1, value=10))
                raise RuntimeError("outer owner rolls back")
        except RuntimeError:
            pass
    finally:
        db.close()

    with Session(engine) as verify_db:
        count = verify_db.execute(select(table.c.id)).scalars().all()

    assert count == []
