"""
SQLAlchemy 2.0 models — Phase A persistence layer.

Replaces flat CSV/TXT/JSON files (app/core/data/*, app/core/sessions/*) with a single
SQLite database. See memory/project_structure.md and the architecture plan for the
full rationale: single operator, single machine, no Postgres/Celery needed.

Field mapping from legacy files (no data loss) is documented per-table below and
implemented in migrate_legacy.py.
"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class Account(Base):
    """Migrated 1:1 from accounts.csv / test_accounts.csv."""
    __tablename__ = "account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    legacy_csv_id: Mapped[str | None] = mapped_column(String(16))
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), index=True)  # new|registered|verified|active|applied|blocked|failed|login_failed
    account_type: Mapped[str] = mapped_column(String(16))       # test|real
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    gender: Mapped[str | None] = mapped_column(String(8))
    birthdate: Mapped[str | None] = mapped_column(String(16))    # kept as "YYYY/MM/DD" string, no parsing
    nationality: Mapped[str | None] = mapped_column(String(8))
    traveldoc: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(128))
    email_pass: Mapped[str | None] = mapped_column(String(128))
    proxy_last_used: Mapped[str | None] = mapped_column(String(256))
    proxy_idx: Mapped[int | None] = mapped_column(Integer)
    registered_at: Mapped[str | None] = mapped_column(String(32))
    last_login: Mapped[str | None] = mapped_column(String(32))
    appointment_ref: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ProxyPool(Base):
    """Named, geo-tagged group of proxies, e.g. 'webshare-do', 'soax-fr', 'isp-static'."""
    __tablename__ = "proxy_pool"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(16))   # webshare|soax|isp
    geo: Mapped[str | None] = mapped_column(String(8))
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_now)

    proxies: Mapped[list["Proxy"]] = relationship(back_populates="pool")


class Proxy(Base):
    """Individual connection string."""
    __tablename__ = "proxy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proxy_pool_id: Mapped[int] = mapped_column(ForeignKey("proxy_pool.id"), index=True)
    connection_string: Mapped[str] = mapped_column(String(512), unique=True)
    exit_ip_cached: Mapped[str | None] = mapped_column(String(64))
    last_used_at: Mapped[_dt.datetime | None] = mapped_column(DateTime)
    used_by_account_id: Mapped[int | None] = mapped_column(ForeignKey("account.id"))
    burned_until: Mapped[str | None] = mapped_column(String(16))  # ISO date string, daily burn
    is_rotating: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_now)

    pool: Mapped[ProxyPool] = relationship(back_populates="proxies")


class BookingEvent(Base):
    """First-class booking campaign."""
    __tablename__ = "booking_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str | None] = mapped_column(String(128))
    start_time: Mapped[_dt.datetime] = mapped_column(DateTime)
    mode: Mapped[str] = mapped_column(String(8))  # test|real
    target_posto_id: Mapped[str] = mapped_column(String(16))
    target_nationality: Mapped[str] = mapped_column(String(8))
    target_residence: Mapped[str] = mapped_column(String(8))
    max_lifetime: Mapped[float] = mapped_column(Float, default=43200.0)
    max_slot_retries: Mapped[int] = mapped_column(Integer, default=3)
    login_concurrency: Mapped[int] = mapped_column(Integer, default=50)
    apply_concurrency: Mapped[int] = mapped_column(Integer, default=30)
    scouts_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="scheduled")  # scheduled|running|completed|cancelled|failed
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_now)
    started_at: Mapped[_dt.datetime | None] = mapped_column(DateTime)
    ended_at: Mapped[_dt.datetime | None] = mapped_column(DateTime)


class BookingEventProxyPool(Base):
    """M2M: which pools (plural, multi-geo) feed a booking event, with dispatch weight."""
    __tablename__ = "booking_event_proxy_pool"

    booking_event_id: Mapped[int] = mapped_column(ForeignKey("booking_event.id"), primary_key=True)
    proxy_pool_id: Mapped[int] = mapped_column(ForeignKey("proxy_pool.id"), primary_key=True)
    weight: Mapped[int] = mapped_column(Integer, default=1)


class WorkerRun(Base):
    """One worker's execution record (account x booking_event)."""
    __tablename__ = "worker_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    booking_event_id: Mapped[int] = mapped_column(ForeignKey("booking_event.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), index=True)
    role: Mapped[str] = mapped_column(String(8))     # real|scout
    state: Mapped[str] = mapped_column(String(32), default="idle")
    proxy_id: Mapped[int | None] = mapped_column(ForeignKey("proxy.id"))
    started_at: Mapped[_dt.datetime | None] = mapped_column(DateTime)
    ended_at: Mapped[_dt.datetime | None] = mapped_column(DateTime)
    error_log: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class AppointmentResult(Base):
    """Confirmed booking outcome."""
    __tablename__ = "appointment_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    worker_run_id: Mapped[int] = mapped_column(ForeignKey("worker_run.id"), index=True)
    date: Mapped[str] = mapped_column(String(16))
    period_id: Mapped[str] = mapped_column(String(16))
    pdf_path: Mapped[str | None] = mapped_column(String(512))
    appointment_ref: Mapped[str | None] = mapped_column(String(64))
    confirmed_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_now)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_now)


class SessionCheckpoint(Base):
    """Replaces app/core/sessions/{user}.json. One live checkpoint per account."""
    __tablename__ = "session_checkpoint"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), unique=True)
    proxy: Mapped[str | None] = mapped_column(String(512))
    cookies_json: Mapped[str] = mapped_column(Text)
    checkpoint: Mapped[str] = mapped_column(String(32))  # login|schedule_jsp
    posto_id: Mapped[str | None] = mapped_column(String(16))
    posto_pdf: Mapped[str | None] = mapped_column(String(16))
    nationality: Mapped[str | None] = mapped_column(String(8))
    residence: Mapped[str | None] = mapped_column(String(8))
    saved_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_now)


class CaptchaUsageLog(Base):
    """Solver telemetry."""
    __tablename__ = "captcha_usage_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    worker_run_id: Mapped[int | None] = mapped_column(ForeignKey("worker_run.id"))
    service: Mapped[str] = mapped_column(String(32))   # capsolver|anticaptcha|twocaptcha|capmonster
    action: Mapped[str] = mapped_column(String(32))
    token_score: Mapped[int | None] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_now)


class EventLog(Base):
    """Audit trail / dashboard feed."""
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    worker_run_id: Mapped[int | None] = mapped_column(ForeignKey("worker_run.id"), index=True)
    booking_event_id: Mapped[int | None] = mapped_column(ForeignKey("booking_event.id"), index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    state: Mapped[str | None] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    detail_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_now, index=True)


class ScheduleEntry(Base):
    """Scheduler's due-list."""
    __tablename__ = "schedule_entry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    booking_event_id: Mapped[int] = mapped_column(ForeignKey("booking_event.id"), unique=True)
    fire_at: Mapped[_dt.datetime] = mapped_column(DateTime, index=True)
    fired: Mapped[bool] = mapped_column(Boolean, default=False)
    fired_at: Mapped[_dt.datetime | None] = mapped_column(DateTime)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_now)


class TelegramChat(Base):
    """Operator's chat for control/notify."""
    __tablename__ = "telegram_chat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, unique=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    username: Mapped[str | None] = mapped_column(String(64))
    is_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_on_success: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_on_failure: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_now)


__all__ = [
    "Base",
    "Account",
    "ProxyPool",
    "Proxy",
    "BookingEvent",
    "BookingEventProxyPool",
    "WorkerRun",
    "AppointmentResult",
    "SessionCheckpoint",
    "CaptchaUsageLog",
    "EventLog",
    "ScheduleEntry",
    "TelegramChat",
]
