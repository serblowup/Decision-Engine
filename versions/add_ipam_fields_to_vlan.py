"""Add IPAM fields to vlan table

Revision ID: xxxx
Revises: 
Create Date: 2026-07-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import CIDR, INET


def upgrade():
    op.add_column('vlan', sa.Column('ip_prefix', CIDR, nullable=True))
    op.add_column('vlan', sa.Column('gateway_ip', INET, nullable=True))
    op.add_column('vlan', sa.Column('dhcp_scope_start', INET, nullable=True))
    op.add_column('vlan', sa.Column('dhcp_scope_end', INET, nullable=True))
    
    # Триггер для проверки перекрытия префиксов
    op.execute("""
    CREATE OR REPLACE FUNCTION check_vlan_prefix_overlap()
    RETURNS TRIGGER AS $$
    BEGIN
      IF EXISTS (
        SELECT 1 FROM vlan
        WHERE id != NEW.id
          AND ip_prefix IS NOT NULL
          AND NEW.ip_prefix IS NOT NULL
          AND (ip_prefix >>= NEW.ip_prefix OR NEW.ip_prefix >>= ip_prefix)
      ) THEN
        RAISE EXCEPTION 'IP prefix % overlaps with existing VLAN prefix', NEW.ip_prefix;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    
    op.execute("""
    CREATE TRIGGER vlan_prefix_overlap_check
    BEFORE INSERT OR UPDATE ON vlan
    FOR EACH ROW EXECUTE FUNCTION check_vlan_prefix_overlap();
    """)


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS vlan_prefix_overlap_check ON vlan")
    op.execute("DROP FUNCTION IF EXISTS check_vlan_prefix_overlap()")
    op.drop_column('vlan', 'dhcp_scope_end')
    op.drop_column('vlan', 'dhcp_scope_start')
    op.drop_column('vlan', 'gateway_ip')
    op.drop_column('vlan', 'ip_prefix')