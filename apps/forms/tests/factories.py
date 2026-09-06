"""Small builders for form-validation tests — no external factory lib."""

from __future__ import annotations

from apps.forms.models import Form, FormField, FieldType, FormStatus

_slug_counter = {"n": 0}


def make_form(*, status=FormStatus.PUBLISHED, **kwargs) -> Form:
    _slug_counter["n"] += 1
    defaults = dict(
        title=kwargs.pop("title", f"Test Form {_slug_counter['n']}"),
        slug=kwargs.pop("slug", f"test-form-{_slug_counter['n']}"),
        status=status,
    )
    defaults.update(kwargs)
    return Form.objects.create(**defaults)


def add_field(form: Form, ftype=FieldType.TEXT, *, label=None, required=False,
              options=None, rows=None, order=None, conditional_logic=None,
              validation_rules=None, min_value=None, max_value=None) -> FormField:
    if order is None:
        order = form.fields.count() + 1
    return FormField.objects.create(
        form=form,
        label=label or f"{ftype} {order}",
        type=ftype,
        is_required=required,
        options=options or [],
        rows=rows or [],
        order=order,
        conditional_logic=conditional_logic or {},
        validation_rules=validation_rules or {},
        min_value=min_value,
        max_value=max_value,
    )


def answers(*pairs) -> list[dict]:
    """answers((field, value), (field, value), ...) -> wire payload."""
    return [{"field": f.id if hasattr(f, "id") else f, "value": v} for f, v in pairs]
