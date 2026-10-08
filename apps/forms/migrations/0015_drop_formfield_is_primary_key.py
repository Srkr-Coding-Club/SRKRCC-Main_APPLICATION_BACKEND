"""
Drop the orphaned ``forms_formfield.is_primary_key`` column.

A deleted migration (``0009_formfield_is_primary_key``, still recorded in
``django_migrations`` but with no file and no corresponding model field) left a
``NOT NULL`` column on ``forms_formfield`` that no current migration or model
defines. Every ``FormField`` insert fails with::

    IntegrityError: null value in column "is_primary_key" ... violates not-null constraint

which blocks all form creation (save draft, publish, manual entry). This migration
drops the column if it is present and is a no-op on databases that never had it.
"""

from django.db import migrations


def drop_legacy_column(apps, schema_editor):
    table_name = "forms_formfield"
    column_name = "is_primary_key"
    connection = schema_editor.connection

    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(cursor, table_name)
        }

    if column_name in columns:
        schema_editor.execute(
            f"ALTER TABLE {schema_editor.quote_name(table_name)} "
            f"DROP COLUMN {schema_editor.quote_name(column_name)}"
        )


class Migration(migrations.Migration):

    dependencies = [
        ("forms", "0014_normalize_legacy_conditional_logic"),
    ]

    operations = [
        migrations.RunPython(drop_legacy_column, migrations.RunPython.noop),
    ]
