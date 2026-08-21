from collections.abc import Generator

from sqlalchemy.orm import Session

from app.db.models import get_session_factory, init_db


def get_db() -> Generator[Session, None, None]:
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


__all__ = ["get_db", "init_db", "get_session_factory"]
