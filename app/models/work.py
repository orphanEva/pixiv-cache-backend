from __future__ import annotations

import enum
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class WorkType(str, enum.Enum):
    ILLUST = "illust"
    NOVEL = "novel"


class WorkStatus(str, enum.Enum):
    ACTIVE = "active"
    UNAVAILABLE = "unavailable"
    DELETED = "deleted"
    RESTRICTED = "restricted"
    AUTH_REQUIRED = "auth_required"
    ERROR = "error"


class PixivWork(Base):
    __tablename__ = "pixiv_work"
    __table_args__ = (UniqueConstraint("pixiv_id", "work_type", name="uk_pixiv_work_type"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pixiv_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    work_type: Mapped[WorkType] = mapped_column(Enum(WorkType), nullable=False)
    status: Mapped[WorkStatus] = mapped_column(Enum(WorkStatus), nullable=False, default=WorkStatus.ACTIVE)
    status_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    author_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    author_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remote_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version_token: Mapped[str] = mapped_column(String(64), nullable=False)
    current_version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    versions: Mapped[list[PixivVersion]] = relationship(back_populates="work", cascade="all, delete-orphan")


class PixivVersion(Base):
    __tablename__ = "pixiv_version"
    __table_args__ = (UniqueConstraint("work_id", "version_no", name="uk_work_version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("pixiv_work.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    version_token: Mapped[str] = mapped_column(String(64), nullable=False)
    remote_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    work: Mapped[PixivWork] = relationship(back_populates="versions")
    assets: Mapped[list[PixivAsset]] = relationship(back_populates="version", cascade="all, delete-orphan")


class PixivAsset(Base):
    __tablename__ = "pixiv_asset"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("pixiv_version.id", ondelete="CASCADE"), index=True)
    page_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remote_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    version: Mapped[PixivVersion] = relationship(back_populates="assets")
