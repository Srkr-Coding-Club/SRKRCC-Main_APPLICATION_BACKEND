"""One-click event registration.

An admin ticks which member details an event needs (name, email, phone, ...). Behind the scenes
that is an ordinary published ``Form`` whose fields are all bound to profile attributes
(``FormField.profile_field``), so the server fills every answer from the signed-in member's own
profile. Responses, exports, confirmation emails, attendance badges and duplicate prevention all
keep working because it is the same form engine, not a second registration system.

A form counts as "one-click" when every one of its active fields is profile-bound. Adding a normal
question to it in the Form Builder therefore turns it into a custom form with no extra bookkeeping.
"""
from django.db import transaction
from django.utils.text import slugify

from apps.forms.models import FieldType, Form, FormField, FormStatus, ProfileField

# Display order of the details an admin can pick: profile key -> (field type, label).
QUICK_FIELDS: dict[str, tuple[str, str]] = {
    ProfileField.FULL_NAME.value: (FieldType.TEXT, 'Full Name'),
    ProfileField.EMAIL.value: (FieldType.EMAIL, 'Email'),
    ProfileField.PHONE_NUMBER.value: (FieldType.PHONE, 'Phone Number'),
    ProfileField.BRANCH.value: (FieldType.TEXT, 'Branch'),
    ProfileField.ROLL_NUMBER.value: (FieldType.TEXT, 'Roll Number'),
    ProfileField.COLLEGE.value: (FieldType.TEXT, 'College'),
    ProfileField.YEAR.value: (FieldType.NUMBER, 'Year'),
    ProfileField.CLUB_ID.value: (FieldType.TEXT, 'Club ID'),
}

# A registration is meaningless without knowing who registered.
ALWAYS_COLLECTED = (ProfileField.FULL_NAME.value, ProfileField.EMAIL.value)

FORM_CATEGORY = 'Event Registration'
SLUG_MAX_LENGTH = 190


def normalize_quick_fields(keys) -> list[str]:
    """Drop duplicates, add the always-collected details and put everything in display order."""
    wanted = set(keys) | set(ALWAYS_COLLECTED)
    return [key for key in QUICK_FIELDS if key in wanted]


def is_quick_form(form: Form | None) -> bool:
    if form is None:
        return False
    fields = list(form.fields.all())
    return bool(fields) and all(field.profile_field for field in fields)


def quick_field_keys(form: Form | None) -> list[str]:
    if not is_quick_form(form):
        return []
    return [field.profile_field for field in sorted(form.fields.all(), key=lambda f: f.order)]


def _unique_slug(event) -> str:
    base = slugify(f'{event.slug}-registration')[:SLUG_MAX_LENGTH] or 'event-registration'
    candidate, counter = base, 2
    while Form.objects.filter(slug=candidate).exists():
        candidate = f'{base}-{counter}'
        counter += 1
    return candidate


def _create_form(event, actor) -> Form:
    return Form.objects.create(
        title=event.title[:200],
        slug=_unique_slug(event),
        description=f'One-click registration for {event.title}. Your details come from your profile.',
        category=FORM_CATEGORY,
        status=FormStatus.PUBLISHED,
        created_by=actor if getattr(actor, 'is_authenticated', False) else None,
        enable_prefill=True,
        allow_multiple_responses=False,
        allow_response_editing=False,
        max_responses_per_user=1,
        max_total_responses=event.capacity or None,
    )


def _sync_fields(form: Form, keys: list[str]) -> bool:
    """Make the form's active fields exactly ``keys``, in order. Returns True if anything changed."""
    changed = False
    existing = {field.profile_field: field for field in form.fields.all() if field.profile_field}

    for key, field in existing.items():
        if key not in keys:
            field.is_deleted = True  # soft delete: old responses still export their answers
            field.save(update_fields=['is_deleted', 'updated_at'])
            changed = True

    for order, key in enumerate(keys, start=1):
        field_type, label = QUICK_FIELDS[key]
        field = existing.get(key)
        if field is None:
            FormField.objects.create(
                form=form, label=label, type=field_type, is_required=True, profile_field=key, order=order,
            )
            changed = True
        elif field.order != order:
            field.order = order
            field.save(update_fields=['order', 'updated_at'])
            changed = True
    return changed


def apply_quick_registration(event, keys, actor=None) -> Form:
    """Create or update the one-click form behind ``event`` and return it.

    A custom form already linked to the event is never modified: a new one-click form is created
    instead, and the caller links it. The old form and its responses stay untouched.
    """
    selected = normalize_quick_fields(keys)
    with transaction.atomic():
        form = event.registration_form
        if not is_quick_form(form):
            form = _create_form(event, actor)

        fields_changed = _sync_fields(form, selected)
        form.title = event.title[:200]
        form.max_total_responses = event.capacity or None
        if fields_changed:
            form.version += 1
        form.save()
    # A fresh instance: the one passed in may carry prefetched fields from before the sync.
    return Form.objects.get(pk=form.pk)


def sync_quick_form_details(event) -> None:
    """Keep a one-click form's title and response cap in step with the event it belongs to."""
    form = event.registration_form
    if not is_quick_form(form):
        return
    title, cap = event.title[:200], event.capacity or None
    if form.title != title or form.max_total_responses != cap:
        form.title = title
        form.max_total_responses = cap
        form.save(update_fields=['title', 'max_total_responses', 'updated_at'])
