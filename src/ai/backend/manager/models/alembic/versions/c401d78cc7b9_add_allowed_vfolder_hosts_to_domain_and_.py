"""add_allowed_vfolder_hosts_to_domain_and_group

Revision ID: c401d78cc7b9
Revises: 3cf19d906e71
Create Date: 2019-06-26 11:34:55.426107

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "c401d78cc7b9"
down_revision = "3cf19d906e71"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column(
        "domains", sa.Column("allowed_vfolder_hosts", postgresql.ARRAY(sa.String()), nullable=True)
    )
    op.add_column(
        "groups", sa.Column("allowed_vfolder_hosts", postgresql.ARRAY(sa.String()), nullable=True)
    )
    # ### end Alembic commands ###

    print("\nSet domain and group's allowed_vfolder_hosts with empty array.")
    connection = op.get_bind()
    query = "UPDATE domains SET allowed_vfolder_hosts = '{}';"
    connection.execute(query)
    query = "UPDATE groups SET allowed_vfolder_hosts = '{}';"
    connection.execute(query)

    op.alter_column("domains", column_name="allowed_vfolder_hosts", nullable=False)
    op.alter_column("groups", column_name="allowed_vfolder_hosts", nullable=False)


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("groups", "allowed_vfolder_hosts")
    op.drop_column("domains", "allowed_vfolder_hosts")
    # ### end Alembic commands ###
