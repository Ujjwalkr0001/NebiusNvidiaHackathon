"""Store GitHub workflow and check run identifiers for worker diagnostics.

Revision ID: 0002_failure_sources
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_failure_sources"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The baseline revision creates tables from current metadata. Inspect first
    # so fresh installs and databases that already applied 0001 both upgrade.
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("repair_jobs")}
    if "workflow_run_id" not in columns:
        op.add_column("repair_jobs", sa.Column("workflow_run_id", sa.Integer(), nullable=True))
    if "check_run_id" not in columns:
        op.add_column("repair_jobs", sa.Column("check_run_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("repair_jobs")}
    if "check_run_id" in columns:
        op.drop_column("repair_jobs", "check_run_id")
    if "workflow_run_id" in columns:
        op.drop_column("repair_jobs", "workflow_run_id")
