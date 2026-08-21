from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Property(Base):
    __tablename__ = "properties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String(500))
    address_norm: Mapped[str | None] = mapped_column(String(500), index=True)
    district: Mapped[str | None] = mapped_column(String(120), index=True)
    city: Mapped[str | None] = mapped_column(String(120), default="Київ")
    region: Mapped[str | None] = mapped_column(String(120), default="Київська область")
    property_type: Mapped[str | None] = mapped_column(String(64), index=True)
    deal_type: Mapped[str | None] = mapped_column(String(16), index=True)
    area_sqm: Mapped[float | None] = mapped_column(Float)
    floor: Mapped[int | None] = mapped_column(Integer)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    listings: Mapped[list[Listing]] = relationship(back_populates="property")
    events: Mapped[list[PropertyEvent]] = relationship(back_populates="property")
    deal_hypotheses: Mapped[list[DealHypothesis]] = relationship(back_populates="property")


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_listing_source_ext"),
        Index("ix_listings_status_source", "status", "source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int | None] = mapped_column(ForeignKey("properties.id"), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    deal_type: Mapped[str] = mapped_column(String(16), index=True)
    property_type: Mapped[str | None] = mapped_column(String(64))
    price: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(8))
    price_per_sqm: Mapped[float | None] = mapped_column(Float)
    area_sqm: Mapped[float | None] = mapped_column(Float)
    floor: Mapped[int | None] = mapped_column(Integer)
    rooms: Mapped[int | None] = mapped_column(Integer)
    address_raw: Mapped[str | None] = mapped_column(String(500))
    district: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(120))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    phone: Mapped[str | None] = mapped_column(String(64))
    agency: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    source_status_raw: Mapped[str | None] = mapped_column(String(120))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    vanished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    price_drop_count: Mapped[int] = mapped_column(Integer, default=0)
    raw_extra: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    property: Mapped[Property | None] = relationship(back_populates="listings")
    snapshots: Mapped[list[ListingSnapshot]] = relationship(back_populates="listing")


class ListingSnapshot(Base):
    __tablename__ = "listing_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
    crawled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    price: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(8))
    status: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str | None] = mapped_column(String(500))
    area_sqm: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict | None] = mapped_column(JSON)

    listing: Mapped[Listing] = relationship(back_populates="snapshots")


class PropertyEvent(Base):
    __tablename__ = "property_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int | None] = mapped_column(ForeignKey("properties.id"), index=True)
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    payload: Mapped[dict | None] = mapped_column(JSON)

    property: Mapped[Property | None] = relationship(back_populates="events")


class DealHypothesis(Base):
    __tablename__ = "deal_hypotheses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), index=True)
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id"), index=True)
    score: Mapped[int] = mapped_column(Integer, index=True)
    bucket: Mapped[str] = mapped_column(String(32), index=True)
    features: Mapped[dict | None] = mapped_column(JSON)
    human_label: Mapped[str | None] = mapped_column(String(32))  # deal / withdrawn / unknown
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    property: Mapped[Property] = relationship(back_populates="deal_hypotheses")


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="running")
    pages_fetched: Mapped[int] = mapped_column(Integer, default=0)
    listings_seen: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


_engine = None
_SessionLocal = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        connect_args = {}
        if settings.database_url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
        _engine = create_engine(
            settings.database_url,
            future=True,
            echo=False,
            connect_args=connect_args,
        )
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    return _engine


def get_session_factory():
    get_engine()
    return _SessionLocal


def init_db() -> None:
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
