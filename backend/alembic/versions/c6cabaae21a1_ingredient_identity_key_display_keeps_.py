"""ingredient identity key, display keeps what was written

Revision ID: c6cabaae21a1
Revises: b9d33848e592
Create Date: 2026-09-17 13:42:25.044832

Decision Q21, amended: `ingredients.name` becomes the display form — what the
household wrote, case and all — and the folded, lowercase identity a write
resolves to moves into its own `canonical_name` column. Every existing row was
written by code that stored the folded form in `name`, so the backfill folds
each row's name once through the same function; importing it (rather than
freezing values, as the chilled-aisle backfill did) is deliberate — the fold is
a function of the row's own name, not a vocabulary that can drift.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.services.ingredient_names import canonical_ingredient_name


# revision identifiers, used by Alembic.
revision: str = 'c6cabaae21a1'
down_revision: Union[str, Sequence[str], None] = 'b9d33848e592'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the identity key, backfill it from the name, then index it."""
    op.add_column("ingredients", sa.Column("canonical_name", sa.String(200), nullable=True))
    bind = op.get_bind()
    for ingredient_id, name in bind.execute(sa.text("SELECT id, name FROM ingredients")):
        folded = canonical_ingredient_name(name) or " ".join(name.lower().split())
        bind.execute(
            sa.text("UPDATE ingredients SET canonical_name = :key WHERE id = :id"),
            {"key": folded, "id": ingredient_id},
        )
    op.create_index(op.f("ix_ingredients_canonical_name"), "ingredients", ["canonical_name"])


def downgrade() -> None:
    """Drop the key. Display names written by newer code keep their case —
    downgrading does not (and cannot) fold them back."""
    op.drop_index(op.f("ix_ingredients_canonical_name"), table_name="ingredients")
    op.drop_column("ingredients", "canonical_name")
