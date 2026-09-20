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


class Migration(migrations.Migration):

    dependencies = [
        ("forms", "0014_normalize_legacy_conditional_logic"),
    ]

    operations = [
        migrations.RunSQL(
            sql='ALTER TABLE "forms_formfield" DROP COLUMN IF EXISTS "is_primary_key";',
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
