from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import TypeVar

from sqlalchemy.orm import Session, SessionTransactionOrigin

from mneme.db.base import SessionLocal


T = TypeVar("T")


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        with db.begin():
            yield db
    finally:
        db.close()


@contextmanager
def transaction(db: Session) -> Generator[Session, None, None]:
    current_transaction = db.get_transaction()

    if current_transaction is None:
        with db.begin():
            yield db
        return

    # Plain reads auto-begin a transaction in SQLAlchemy. In that state there is
    # no explicit outer owner, so this helper must finish the write boundary.
    if current_transaction.origin == SessionTransactionOrigin.AUTOBEGIN:
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        return

    yield db


# TODO(Phase 2): Expose as a public convenience wrapper once callers are
# migrated from direct session_scope / transaction usage.
def run_in_transaction(work: Callable[[Session], T]) -> T:
    with session_scope() as db:
        return work(db)
