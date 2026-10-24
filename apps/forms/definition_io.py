"""
Copying a form's definition: duplicate, save as a template, export to a file and import it back.

Every route here ends in the same place: build a new DRAFT form from a list of questions, then rewrite the
question ids that other questions refer to (conditions, comparisons, formulas, {{answer}} pipes, page routes and
the Club ID mapping) so they point at the NEW questions. A reference to a question that did not come along is
dropped rather than left pointing at something unrelated.
"""

from __future__ import annotations

import re
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import serializers

from .models import Form
from .validation import limits
from .validation.schema import ROUTING_ACTIONS, normalize_conditional_logic
from .versions import FIELD_KEYS, SETTING_KEYS, record_version, snapshot_form

EXPORT_FORMAT = 'srkrcc-form'
EXPORT_VERSION = 1
MAX_IMPORT_BYTES = 1_000_000

_FORMULA_REF = re.compile(r'\{(\d+)\}')
_PIPE_REF = re.compile(r'\{\{\s*(\d+)\s*\}\}')

# Dates belong to the form it was written for.
_NOT_COPIED = ('open_at', 'close_at', 'allow_edits_until')


def unique_slug(base: str) -> str:
    stem = (slugify(base) or 'form')[:170]
    slug, n = stem, 1
    while Form.objects.filter(slug=slug).exists():
        n += 1
        slug = f'{stem}-{n}'
    return slug


def _as_int(value: Any):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _remap_condition(raw: Any, id_map: dict[int, int]) -> dict[str, Any]:
    normalized = normalize_conditional_logic(raw)
    if not normalized:
        return {}

    def walk(node: dict[str, Any]):
        if 'rules' in node:
            kept = [k for k in (walk(r) for r in node['rules']) if k]
            return {**node, 'rules': kept} if kept else None
        watched = id_map.get(node.get('field')) if node.get('field') is not None else None
        if watched is None:
            return None
        leaf = {k: v for k, v in node.items() if k != 'field_raw'}
        leaf['field'] = watched
        if node.get('value_field') is not None:
            other = id_map.get(node['value_field'])
            if other is None:
                return None
            leaf['value_field'] = other
        return leaf

    result = walk(normalized)
    if not result:
        return {}
    if result.get('action') in ROUTING_ACTIONS and result['action'] == 'skip_to_section':
        target = id_map.get(_as_int(result.get('action_value')))
        if target is None:
            return {}
        result['action_value'] = target
    elif result.get('action') == 'display_message' and isinstance(result.get('action_value'), str):
        result['action_value'] = _remap_pipes(result['action_value'], id_map)
    return result


def _remap_pipes(text: str, id_map: dict[int, int]) -> str:
    return _PIPE_REF.sub(lambda m: '{{%d}}' % id_map.get(int(m.group(1)), int(m.group(1))), text or '')


def remap_references(field: dict[str, Any], id_map: dict[int, int]) -> dict[str, Any]:
    """``field`` with every reference to another question rewritten through ``id_map`` (old id -> new id)."""
    out = dict(field)
    out['conditional_logic'] = _remap_condition(field.get('conditional_logic'), id_map)
    rules = dict(field.get('validation_rules') or {})
    cross = rules.get('crossField')
    if isinstance(cross, list):
        mapped = []
        for rule in cross:
            if not isinstance(rule, dict):
                continue
            target = id_map.get(_as_int(rule.get('field', rule.get('otherField'))))
            if target is not None:
                mapped.append({**{k: v for k, v in rule.items() if k != 'otherField'}, 'field': target})
        if mapped:
            rules['crossField'] = mapped
        else:
            rules.pop('crossField')
    if isinstance(rules.get('formula'), str):
        rules['formula'] = _FORMULA_REF.sub(lambda m: '{%d}' % id_map.get(int(m.group(1)), int(m.group(1))), rules['formula'])
    out['validation_rules'] = rules
    out['label'] = _remap_pipes(field.get('label') or '', id_map)
    out['description'] = _remap_pipes(field.get('description') or '', id_map)
    return out


def _materialize(*, settings: dict[str, Any], fields: list[dict[str, Any]], club_mapping: dict[str, Any], user,
                 title: str, is_template: bool = False, note: str = '') -> Form:
    """Create a DRAFT form from ``settings`` and ``fields`` (each possibly carrying the id it had elsewhere)."""
    from .serializers import FormSerializer  # imported here: serializers imports this package's siblings

    payload = {k: v for k, v in settings.items() if k in SETTING_KEYS and k not in ('club_id_field_mapping',)}
    payload.update({
        'title': title[:200], 'slug': unique_slug(title), 'status': 'DRAFT',
        'fields': [{**{k: v for k, v in f.items() if k in FIELD_KEYS and k != 'id'}, 'order': i + 1} for i, f in enumerate(fields)],
    })
    serializer = FormSerializer(data=payload, context={'request': None, 'version_note': note})
    serializer.is_valid(raise_exception=True)

    with transaction.atomic():
        form = serializer.save(created_by=user if getattr(user, 'is_authenticated', False) else None, is_template=is_template)
        created = list(form.fields.order_by('order', 'id'))
        id_map = {src['id']: new.id for src, new in zip(fields, created) if _as_int(src.get('id')) is not None}
        id_map = {int(k): v for k, v in id_map.items()}
        for new in created:
            remapped = remap_references(
                {'label': new.label, 'description': new.description,
                 'conditional_logic': new.conditional_logic, 'validation_rules': new.validation_rules},
                id_map,
            )
            for attr in ('label', 'description', 'conditional_logic', 'validation_rules'):
                setattr(new, attr, remapped[attr])
            new.save(update_fields=['label', 'description', 'conditional_logic', 'validation_rules', 'updated_at'])
        form.club_id_field_mapping = {
            key: id_map[_as_int(value)] for key, value in (club_mapping or {}).items() if _as_int(value) in id_map
        }
        form.save(update_fields=['club_id_field_mapping', 'updated_at'])
        record_version(form, user, note)
    return form


def duplicate_form(source: Form, *, user, title: str | None = None, as_template: bool = False) -> Form:
    snap = snapshot_form(source)
    settings = {**snap['form'], **{key: None for key in _NOT_COPIED}}
    return _materialize(
        settings=settings, fields=snap['fields'], club_mapping=snap['form'].get('club_id_field_mapping') or {},
        user=user, title=title or (f'{source.title} (template)' if as_template else f'Copy of {source.title}'),
        is_template=as_template, note=f"{'Template' if as_template else 'Copy'} of '{source.title}'",
    )


def export_definition(form: Form) -> dict[str, Any]:
    snap = snapshot_form(form)
    return {
        'format': EXPORT_FORMAT,
        'version': EXPORT_VERSION,
        'exported_at': timezone.now().isoformat(),
        'form': snap['form'],
        'fields': snap['fields'],
    }


def import_definition(doc: Any, *, user) -> Form:
    """Create a DRAFT form from an exported file. Raises ``serializers.ValidationError`` for anything unusable."""
    def bad(message: str):
        raise serializers.ValidationError({'detail': message, 'code': 'INVALID_FORM_FILE'})

    if not isinstance(doc, dict) or doc.get('format') != EXPORT_FORMAT:
        bad('This is not a form file exported from this platform.')
    if doc.get('version') != EXPORT_VERSION:
        bad(f'Unsupported form file version {doc.get("version")!r}.')
    form_part, fields = doc.get('form'), doc.get('fields')
    if not isinstance(form_part, dict) or not isinstance(fields, list) or not all(isinstance(f, dict) for f in fields):
        bad('The form file is missing its form settings or questions.')
    if len(fields) > limits.MAX_FIELDS_PER_FORM:
        bad(f'A form can have at most {limits.MAX_FIELDS_PER_FORM} questions.')
    title = str(form_part.get('title') or '').strip()
    if not title:
        bad('The form file has no title.')

    # The email template and the dates belong to the platform / event they were written for.
    settings = {k: v for k, v in form_part.items() if k in SETTING_KEYS and k != 'confirmation_email_template' and k not in _NOT_COPIED}
    settings['confirmation_email_enabled'] = False if form_part.get('confirmation_email_template') else settings.get('confirmation_email_enabled', False)
    return _materialize(
        settings=settings, fields=fields, club_mapping=form_part.get('club_id_field_mapping') or {},
        user=user, title=title, note='Imported from a form file',
    )
