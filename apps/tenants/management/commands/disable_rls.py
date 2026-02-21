"""Disable Row-Level Security on all tables.

Supabase re-enables RLS on tables created by migrations.
Run this after every `migrate` to ensure the application can
read/write without per-row policies blocking access.
"""

from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Disable RLS on all user tables (Supabase re-enables it on new tables)."

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT schemaname, tablename
                FROM pg_tables
                WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
                  AND rowsecurity = true
                """
            )
            tables = cursor.fetchall()

            if not tables:
                self.stdout.write("No tables with RLS enabled.")
                return

            for schema, table in tables:
                fqn = f'"{schema}"."{table}"'
                cursor.execute(f"ALTER TABLE {fqn} DISABLE ROW LEVEL SECURITY;")
                self.stdout.write(f"  Disabled RLS on {fqn}")

            self.stdout.write(
                self.style.SUCCESS(f"Disabled RLS on {len(tables)} table(s).")
            )
