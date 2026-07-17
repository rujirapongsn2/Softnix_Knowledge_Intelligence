"""Add normalized trace roots and spans for scalable observability.

Revision ID: 0016_normalized_traces
Revises: 0015_legal_provenance
"""
import sqlalchemy as sa
from alembic import op


revision = "0016_normalized_traces"
down_revision = "0015_legal_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "trace_runs" not in tables:
        op.create_table(
            "trace_runs",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("request_id", sa.String(length=36), nullable=True),
            sa.Column("transport", sa.String(length=30), nullable=False, server_default="api"),
            sa.Column("tool", sa.String(length=100), nullable=True),
            sa.Column("trace_status", sa.String(length=20), nullable=False, server_default="success"),
            sa.Column("source_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("knowledge_base_ids", sa.JSON(), nullable=False),
            sa.Column("retrieval_plan", sa.JSON(), nullable=True),
            sa.Column("request_summary", sa.JSON(), nullable=False),
            sa.Column("response_summary", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_trace_runs_request_id", "trace_runs", ["request_id"])
        op.create_index("ix_trace_runs_transport", "trace_runs", ["transport"])
        op.create_index("ix_trace_runs_tool", "trace_runs", ["tool"])
        op.create_index("ix_trace_runs_trace_status", "trace_runs", ["trace_status"])
        op.create_index("ix_trace_runs_created_at", "trace_runs", ["created_at"])
    if "trace_spans" not in tables:
        op.create_table(
            "trace_spans",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("trace_id", sa.String(length=36), sa.ForeignKey("trace_runs.id"), nullable=False),
            sa.Column("span_id", sa.String(length=100), nullable=False),
            sa.Column("parent_span_id", sa.String(length=100), nullable=True),
            sa.Column("channel", sa.String(length=80), nullable=False),
            sa.Column("system", sa.String(length=160), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("offset_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("reason_code", sa.String(length=80), nullable=True),
            sa.Column("detail", sa.String(length=500), nullable=True),
            sa.Column("input_summary", sa.JSON(), nullable=False),
            sa.Column("output_summary", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_trace_spans_trace_id", "trace_spans", ["trace_id"])
        op.create_index("ix_trace_spans_span_id", "trace_spans", ["span_id"])
        op.create_index("ix_trace_spans_channel", "trace_spans", ["channel"])
        op.create_index("ix_trace_spans_status", "trace_spans", ["status"])
        op.create_index("ix_trace_spans_created_at", "trace_spans", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_trace_spans_created_at", table_name="trace_spans")
    op.drop_index("ix_trace_spans_status", table_name="trace_spans")
    op.drop_index("ix_trace_spans_channel", table_name="trace_spans")
    op.drop_index("ix_trace_spans_span_id", table_name="trace_spans")
    op.drop_index("ix_trace_spans_trace_id", table_name="trace_spans")
    op.drop_table("trace_spans")
    op.drop_index("ix_trace_runs_created_at", table_name="trace_runs")
    op.drop_index("ix_trace_runs_trace_status", table_name="trace_runs")
    op.drop_index("ix_trace_runs_tool", table_name="trace_runs")
    op.drop_index("ix_trace_runs_transport", table_name="trace_runs")
    op.drop_index("ix_trace_runs_request_id", table_name="trace_runs")
    op.drop_table("trace_runs")
