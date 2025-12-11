"""drop unique constraint on bookings.email

Revision ID: 0001_drop_unique_booking_email
Revises: 
Create Date: 2025-12-02 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = '0001_drop_unique_booking_email'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Try dropping common constraint/index names that may have been created by SQLAlchemy
    # This will not fail if the objects do not exist.
    op.execute("ALTER TABLE IF EXISTS bookings DROP CONSTRAINT IF EXISTS bookings_email_key;")
    op.execute("ALTER TABLE IF EXISTS bookings DROP CONSTRAINT IF EXISTS uq_bookings_email;")
    op.execute("ALTER TABLE IF EXISTS bookings DROP CONSTRAINT IF EXISTS unique_bookings_email;")
    op.execute("DROP INDEX IF EXISTS ix_bookings_email;")
    # Also drop any unnamed unique constraints by checking pg_constraint by column
    op.execute(r"""
    DO $$
    DECLARE
      cname text;
    BEGIN
      SELECT conname INTO cname
      FROM pg_constraint c
      JOIN pg_class t ON c.conrelid = t.oid
      JOIN pg_namespace n ON t.relnamespace = n.oid
      WHERE t.relname = 'bookings' AND c.contype = 'u'
      AND EXISTS (SELECT 1 FROM unnest(c.conkey) a JOIN pg_attribute at ON at.attnum = a AND at.attrelid = t.oid WHERE at.attname = 'email')
      LIMIT 1;
      IF cname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE bookings DROP CONSTRAINT %I', cname);
      END IF;
    END
    $$;
    """)


def downgrade():
    # Downgrade: attempt to recreate a unique index/constraint on bookings.email
    # WARNING: this will fail if duplicate emails exist. Run only if safe.
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_bookings_email_unique ON bookings (email);")
