"""ORM for the manually managed pixiv_archive schema.

Keep these columns in sync with sql/pixiv_archive.sql. No runtime create_all.
"""
from __future__ import annotations
import enum
from datetime import datetime
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey, Index, Integer, JSON,
    String, Text, UniqueConstraint, text
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT, INTEGER as MYSQL_INTEGER, BIGINT as MYSQL_BIGINT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class WorkType(str, enum.Enum):
    ILLUST = "illust"
    MANGA = "manga"
    UGOIRA = "ugoira"
    NOVEL = "novel"


class WorkStatus(str, enum.Enum):
    ACTIVE = "active"
    DELETED = "deleted"
    RESTRICTED = "private"
    UNAVAILABLE = "unavailable"
    AUTH_REQUIRED = "auth_required"
    ERROR = "error"


class SyncSourceType(str, enum.Enum):
    AUTHOR = "author"
    BOOKMARKS = "bookmarks"


class SyncRestrict(str, enum.Enum):
    PUBLIC = "public"
    PRIVATE = "private"


def db_enum(enum_class):
    return Enum(enum_class, values_callable=lambda cls: [v.value for v in cls], native_enum=True)


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class Series(Base):
    __tablename__ = "series"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    type: Mapped[str] = mapped_column(Enum("novel", "manga"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)
    author_id: Mapped[str] = mapped_column(String(32), nullable=False)
    author_name: Mapped[str] = mapped_column(String(128), nullable=False)
    published_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = (Index("idx_series_author", "author_id"), Index("idx_series_type", "type"))


class Tag(Base):
    __tablename__ = "tags"
    id: Mapped[int] = mapped_column(Integer().with_variant(MYSQL_INTEGER(unsigned=True), 'mysql'), primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    translated_name: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class PixivWork(Base):
    __tablename__ = "works"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    work_type: Mapped[WorkType] = mapped_column("type", db_enum(WorkType), nullable=False)
    status: Mapped[WorkStatus] = mapped_column(db_enum(WorkStatus), nullable=False, default=WorkStatus.ACTIVE)
    status_reason: Mapped[str | None] = mapped_column(String(1000))
    x_restrict: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_ai: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    series_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("series.id", ondelete="SET NULL"))
    series_order: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    caption: Mapped[str | None] = mapped_column(Text)
    author_id: Mapped[str] = mapped_column(String(32), nullable=False, default="0")
    author_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    remote_created_at: Mapped[datetime | None] = mapped_column("remote_create_at", DateTime)
    remote_updated_at: Mapped[datetime | None] = mapped_column("remote_update_at", DateTime)
    novel_content: Mapped[str | None] = mapped_column(Text().with_variant(MEDIUMTEXT, 'mysql'))
    metadata_json: Mapped[dict | None] = mapped_column("raw_meta", JSON)
    version_token: Mapped[str] = mapped_column(String(64), nullable=False)
    current_version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    cached_at: Mapped[datetime] = mapped_column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    versions: Mapped[list["PixivVersion"]] = relationship(back_populates="work", cascade="all, delete-orphan")
    current_files: Mapped[list["CurrentFile"]] = relationship(back_populates="work", cascade="all, delete-orphan")
    __table_args__ = (
        Index("idx_type_status", "type", "status"),
        Index("idx_author_id", "author_id"),
        Index("idx_remote_update_at", "remote_update_at"),
        Index("idx_series_lookup", "series_id", "series_order"),
    )

    @property
    def pixiv_id(self) -> int:
        return int(self.id)


class PixivVersion(Base):
    __tablename__ = "work_history"
    id: Mapped[int] = mapped_column("history_id", BigInteger().with_variant(MYSQL_BIGINT(unsigned=True), "mysql"), primary_key=True, autoincrement=True)
    work_id: Mapped[str] = mapped_column(String(32), ForeignKey("works.id", ondelete="CASCADE"), nullable=False)
    version_no: Mapped[int] = mapped_column("version_num", Integer, nullable=False)
    version_token: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)
    tags_snapshot: Mapped[list | None] = mapped_column(JSON)
    remote_updated_at: Mapped[datetime | None] = mapped_column("remote_update_at", DateTime)
    novel_content: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    metadata_json: Mapped[dict] = mapped_column("raw_meta", JSON, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_at: Mapped[datetime] = mapped_column("archived_at", DateTime, nullable=False)
    work: Mapped["PixivWork"] = relationship(back_populates="versions")
    assets: Mapped[list["PixivAsset"]] = relationship(back_populates="version", cascade="all, delete-orphan")
    __table_args__ = (UniqueConstraint("work_id", "version_num", name="uk_work_version"),)


class PixivAsset(Base):
    __tablename__ = "work_history_files"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column("history_id", BigInteger().with_variant(MYSQL_BIGINT(unsigned=True), "mysql"), ForeignKey("work_history.history_id", ondelete="CASCADE"), nullable=False)
    page_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    file_type: Mapped[str] = mapped_column(String(24), nullable=False, default="image")
    local_path: Mapped[str] = mapped_column("backup_path", String(1000), nullable=False)
    sha256: Mapped[str | None] = mapped_column("file_hash", String(64))
    size_bytes: Mapped[int] = mapped_column("file_size", BigInteger, nullable=False, default=0)
    remote_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    version: Mapped["PixivVersion"] = relationship(back_populates="assets")


class CurrentFile(Base):
    __tablename__ = "work_files"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    work_id: Mapped[str] = mapped_column(String(32), ForeignKey("works.id", ondelete="CASCADE"), nullable=False)
    page_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    file_type: Mapped[str] = mapped_column(String(24), nullable=False)
    local_path: Mapped[str] = mapped_column("file_path", String(1000), nullable=False)
    size_bytes: Mapped[int] = mapped_column("file_size", BigInteger, nullable=False)
    sha256: Mapped[str | None] = mapped_column("file_hash", String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    work: Mapped["PixivWork"] = relationship(back_populates="current_files")


class WorkTagRelation(Base):
    __tablename__ = "work_tag_relation"
    work_id: Mapped[str] = mapped_column(String(32), ForeignKey("works.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(Integer().with_variant(MYSQL_INTEGER(unsigned=True), "mysql"), ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))



class SyncSource(Base):
    __tablename__ = "sync_sources"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(MYSQL_BIGINT(unsigned=True), "mysql"),
        primary_key=True,
        autoincrement=True,
    )
    source_type: Mapped[SyncSourceType] = mapped_column(
        "type", db_enum(SyncSourceType), nullable=False
    )
    remote_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    restrict_mode: Mapped[SyncRestrict] = mapped_column(
        db_enum(SyncRestrict), nullable=False, default=SyncRestrict.PUBLIC
    )
    include_illust: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    include_novel: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=21600)
    frontier_json: Mapped[dict | None] = mapped_column(JSON)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP")
    )
    __table_args__ = (
        UniqueConstraint(
            "type", "remote_user_id", "restrict_mode",
            name="uk_sync_source_identity",
        ),
        Index("idx_sync_due", "enabled", "next_run_at"),
    )
