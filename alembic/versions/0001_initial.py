"""initial schema

Revision ID: 0001_initial
Revises: None
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pixiv_work",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("pixiv_id", sa.BigInteger(), nullable=False),
        sa.Column("work_type", sa.Enum("ILLUST", "NOVEL", name="worktype"), nullable=False),
        sa.Column("status", sa.Enum("ACTIVE", "UNAVAILABLE", "DELETED", "RESTRICTED", "AUTH_REQUIRED", "ERROR", name="workstatus"), nullable=False),
        sa.Column("status_reason", sa.String(length=1000), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=True),
        sa.Column("author_name", sa.String(length=255), nullable=True),
        sa.Column("remote_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remote_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version_token", sa.String(length=64), nullable=False),
        sa.Column("current_version_no", sa.Integer(), nullable=False),
        sa.Column("cached_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pixiv_id", "work_type", name="uk_pixiv_work_type"),
    )
    op.create_index(op.f("ix_pixiv_work_pixiv_id"), "pixiv_work", ["pixiv_id"], unique=False)

    op.create_table(
        "pixiv_version",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("work_id", sa.BigInteger(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("version_token", sa.String(length=64), nullable=False),
        sa.Column("remote_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("storage_path", sa.String(length=1000), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["work_id"], ["pixiv_work.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_id", "version_no", name="uk_work_version"),
    )
    op.create_index(op.f("ix_pixiv_version_work_id"), "pixiv_version", ["work_id"], unique=False)

    op.create_table(
        "pixiv_asset",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("page_index", sa.Integer(), nullable=False),
        sa.Column("remote_url", sa.Text(), nullable=True),
        sa.Column("local_path", sa.String(length=1000), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["version_id"], ["pixiv_version.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_pixiv_asset_version_id"), "pixiv_asset", ["version_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_pixiv_asset_version_id"), table_name="pixiv_asset")
    op.drop_table("pixiv_asset")
    op.drop_index(op.f("ix_pixiv_version_work_id"), table_name="pixiv_version")
    op.drop_table("pixiv_version")
    op.drop_index(op.f("ix_pixiv_work_pixiv_id"), table_name="pixiv_work")
    op.drop_table("pixiv_work")
