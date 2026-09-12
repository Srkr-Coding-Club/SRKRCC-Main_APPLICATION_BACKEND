import logging

from django.conf import settings

from apps.accounts.services.user_account_service import UserAccountService
from apps.core.services.email_service import EmailNotificationService, MemberEmailContext

logger = logging.getLogger(__name__)


def _coerce_field_id(raw) -> int | None:
    """club_id_field_mapping values should always be real FormField pks (ints), but
    tolerate a non-numeric value defensively rather than raising — e.g. a stray
    client-side placeholder id that was never actually a saved field."""
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class FormAutomationService:
    """
    Wires the two per-Form automations configured in the Form Builder — Club Member
    ID generation and submission confirmation email — into the Response submission
    flow (apps/forms/views.py ResponseViewSet.create).
    """

    @classmethod
    def resolve_club_member(cls, form, answers_by_field_id: dict):
        """
        Given a form with club_id_enabled and the just-saved answers for one Response
        (keyed by FormField id), find-or-create the club member by their mapped email
        and return the resolved User. Returns None if the form has no email mapping
        configured or the submitter left the mapped email field blank.

        Reuses UserAccountService.upsert_member — the same atomic, race-safe,
        find-or-create-by-email operation the CSV member-import pipeline already
        uses — so a person keeps exactly one Club ID no matter how many forms they
        fill out. May raise ClubIdImmutableError / ClubIdConflictError, which the
        caller should let propagate so the whole response is rolled back together.
        """
        mapping = form.club_id_field_mapping or {}
        email_field_id = _coerce_field_id(mapping.get('email'))
        if email_field_id is None:
            # Missing, or a non-numeric leftover from a field that was never
            # actually persisted (e.g. the Form Builder UI captured a field before
            # the form's first save) — treat as "not configured" rather than crash
            # the whole submission over a stale admin-side configuration mistake.
            return None

        email_value = answers_by_field_id.get(email_field_id)
        if not email_value or not str(email_value).strip():
            return None

        payload = {'email': str(email_value).strip()}
        for key in ('full_name', 'phone_number', 'branch', 'roll_number'):
            field_id = _coerce_field_id(mapping.get(key))
            if field_id is not None and answers_by_field_id.get(field_id):
                payload[key] = answers_by_field_id[field_id]

        user, _created, _changed = UserAccountService.upsert_member(
            payload,
            is_backup_import=False,
            source_origin='FORM_REGISTRATION',
        )
        return user

    @classmethod
    def send_confirmation_email(cls, form, resolved_user, answers_by_field_id: dict, response=None):
        """
        Core confirmation-email send. Builds the recipient context, creates an
        EmailJob/EmailDelivery pair (linked to `response` when given, so a
        response's send status can be queried and the email re-triggered later
        from the admin responses viewer), and dispatches it.

        Raises on failure/misconfiguration — callers that must not fail the
        surrounding request (the auto-fire path right after a public submission)
        should go through `dispatch_confirmation_email` instead, which wraps
        this in a swallow-and-log. A manual admin "resend" action should call
        this directly so a failure is actually reported back.
        """
        if not form.confirmation_email_enabled or not form.confirmation_email_template:
            raise ValueError("This form does not have confirmation email automation configured.")

        if resolved_user:
            recipient_email = resolved_user.email
            context = MemberEmailContext.build_for_user(resolved_user)
        else:
            mapping = form.club_id_field_mapping or {}
            email_field_id = _coerce_field_id(mapping.get('email'))
            recipient_email = answers_by_field_id.get(email_field_id) if email_field_id is not None else None
            if not recipient_email or not str(recipient_email).strip():
                # No club-id mapping (or club-id disabled) and no other configured
                # email source on this form — nothing to send a confirmation to.
                raise ValueError("No recipient email could be resolved for this response.")

            full_name_field_id = _coerce_field_id(mapping.get('full_name'))
            full_name = (answers_by_field_id.get(full_name_field_id) if full_name_field_id is not None else '') or ''
            base_portal = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000')
            context = {
                'full_name': full_name or str(recipient_email).split('@')[0],
                'first_name': (full_name.split(' ')[0] if full_name else '') or str(recipient_email).split('@')[0],
                'last_name': '',
                'email': recipient_email,
                'club_id': 'Pending',
                'branch': 'General',
                'membership_status': 'ACTIVE',
                'roll_number': 'N/A',
                'portal_url': base_portal,
                'login_url': f'{base_portal}/login',
                'setup_password_url': f'{base_portal}/login',
                'discount_code': '',
                'custom_message': '',
            }

        job = EmailNotificationService.create_email_job(
            template=form.confirmation_email_template,
            recipient_tuples=[(recipient_email, resolved_user, context)],
            campaign_name=f"Form Confirmation - {form.title}",
            created_by=None,
        )
        if response is not None:
            job.deliveries.update(response=response)

        # Synchronous — a single-recipient send is fast, and the admin "resend"
        # action (the other caller of this method) needs the real outcome back
        # immediately rather than a job that's still PENDING when the response
        # is returned.
        EmailNotificationService.process_email_job(job)
        return job

    @classmethod
    def dispatch_confirmation_email(cls, form, resolved_user, answers_by_field_id: dict, response=None) -> None:
        """
        Best-effort wrapper around `send_confirmation_email` for the auto-fire path
        right after a public submission. Must be called AFTER the response's
        transaction has committed — never from inside an open transaction (mirrors
        MemberImportService.commit_import's existing convention) so a failed send
        can never roll back an otherwise-successful submission. Runs on a
        background thread (see apps.core.tasks.run_in_background) so the
        submitter's response isn't held up by the SMTP round-trip, and swallows
        its own errors since a failed confirmation email must never surface as a
        problem with what was already a successful submission.
        """
        if not form.confirmation_email_enabled or not form.confirmation_email_template:
            return

        def _send():
            try:
                cls.send_confirmation_email(form, resolved_user, answers_by_field_id, response=response)
            except Exception as ex:
                logger.error("FORM_CONFIRMATION_EMAIL_FAILED: form_id=%s error=%s", form.id, str(ex))

        from apps.core.tasks import run_in_background
        run_in_background(_send)
