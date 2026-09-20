"""
Cosmetic data migration: rewrite the frontend's unfinished
``{"if": "parent", "equals": "..."}`` conditional-logic placeholder to ``{}`` and
canonicalize any other legacy ``conditional_logic`` / ``validation_rules`` blobs
so they match the shapes the validation engine emits on save.

The engine already tolerates every legacy shape at runtime, so this migration is
purely to stop ``data_health`` from flagging the placeholders and to keep stored
config uniform. Fully reversible in the sense that it never loses a *usable* rule
(the placeholder was never functional).
"""

from django.db import migrations


def forwards(apps, schema_editor):
    FormField = apps.get_model("forms", "FormField")
    # Import lazily — the validation package is not a migration dependency.
    from apps.forms.validation.schema import (
        normalize_conditional_logic, normalize_validation_rules,
    )

    # Historical models use a plain manager, so .objects returns every row
    # (the ActiveFieldManager soft-delete filter is not applied in migrations).
    for field in FormField.objects.all():
        changed = False

        cl = field.conditional_logic or {}
        if cl:
            norm = normalize_conditional_logic(cl)
            # Drop the placeholder entirely; otherwise store the canonical shape.
            new_cl = {} if not norm else norm
            if new_cl != cl:
                field.conditional_logic = new_cl
                changed = True

        vr = field.validation_rules or {}
        if vr:
            norm_vr = normalize_validation_rules(field.type, vr)
            if norm_vr != vr:
                field.validation_rules = norm_vr
                changed = True

        if changed:
            field.save(update_fields=["conditional_logic", "validation_rules"])


def backwards(apps, schema_editor):
    # Non-reversible in practice (the placeholder carried no information), but a
    # no-op keeps `migrate` able to unapply the migration cleanly.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("forms", "0013_form_club_id_enabled_form_club_id_field_mapping_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
