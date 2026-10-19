"""Identity issuance: bind each identity to its controller proof; proofs are append-only.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No identity can exist before this revision (issuance did not exist), so the new
    # column can be NOT NULL straight away.
    op.add_column("identities", sa.Column("controller_proof_id", sa.Text(), nullable=False))
    op.create_foreign_key(
        op.f("fk_identities_controller_proof_id_controller_proofs"),
        "identities",
        "controller_proofs",
        ["controller_proof_id"],
        ["id"],
    )
    op.create_unique_constraint(
        op.f("uq_identities_controller_proof_id"), "identities", ["controller_proof_id"]
    )

    op.add_column("controller_proofs", sa.Column("failure_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_controller_proofs_verified_at_set"),
        "controller_proofs",
        "status <> 'verified' OR verified_at IS NOT NULL",
    )
    op.execute(
        "CREATE TRIGGER controller_proofs_no_update_delete BEFORE UPDATE OR DELETE "
        "ON controller_proofs FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
    )
    op.execute(
        "CREATE TRIGGER controller_proofs_no_truncate BEFORE TRUNCATE "
        "ON controller_proofs FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS controller_proofs_no_truncate ON controller_proofs")
    op.execute("DROP TRIGGER IF EXISTS controller_proofs_no_update_delete ON controller_proofs")
    op.drop_constraint(
        op.f("ck_controller_proofs_verified_at_set"), "controller_proofs", type_="check"
    )
    op.drop_column("controller_proofs", "failure_reason")
    op.drop_constraint(op.f("uq_identities_controller_proof_id"), "identities", type_="unique")
    op.drop_constraint(
        op.f("fk_identities_controller_proof_id_controller_proofs"),
        "identities",
        type_="foreignkey",
    )
    op.drop_column("identities", "controller_proof_id")
