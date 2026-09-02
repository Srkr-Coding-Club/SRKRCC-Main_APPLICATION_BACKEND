import re
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone
from django.conf import settings
from apps.core.models import EmailTemplate, EmailJob, EmailDelivery, DeliveryStatus, EmailJobStatus

PARAM_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")

GLOBAL_ALLOWED_PARAMETERS = {
    "full_name",
    "first_name",
    "last_name",
    "email",
    "club_id",
    "branch",
    "membership_status",
    "roll_number",
    "portal_url",
    "login_url",
    "setup_password_url",
    "discount_code",
    "custom_message",
}


class TemplateSecurityError(Exception):
    """Raised when an email template or caller attempts to access unwhitelisted / sensitive parameters."""
    pass


class MemberEmailContext:
    """
    Constructs a strictly whitelisted dictionary of parameters for a user/member.
    Prevents leaking internal fields (like password_hash, id, is_superuser).
    """

    @classmethod
    def build_for_user(
        cls,
        user,
        portal_url: str | None = None,
        setup_password_url: str | None = None,
        custom_message: str = "",
        discount_code: str | None = None,
    ) -> dict[str, str]:
        base_portal = portal_url or getattr(settings, "FRONTEND_URL", "http://localhost:3000")
        
        full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.username or user.email.split("@")[0]
        first_name = user.first_name or full_name.split()[0]
        
        return {
            "full_name": full_name,
            "first_name": first_name,
            "last_name": user.last_name or "",
            "email": user.email,
            "club_id": user.club_id or "Pending",
            "branch": user.branch or "General",
            "membership_status": getattr(user, "membership_status", "ACTIVE"),
            "roll_number": getattr(user, "roll_number", "") or "N/A",
            "portal_url": base_portal,
            "login_url": f"{base_portal}/login",
            "setup_password_url": setup_password_url or f"{base_portal}/login",
            "discount_code": discount_code or "",
            "custom_message": custom_message,
        }


class EmailNotificationService:
    """
    Universal Email Notification Engine for templated, parameter-whitelisted dispatch.
    """

    @classmethod
    def render_template(cls, template: EmailTemplate, context: dict) -> tuple[str, str, str]:
        """
        Renders subject, html body, and plaintext body against the whitelisted context.
        Raises TemplateSecurityError if forbidden variables are found.
        """
        allowed = set(template.allowed_parameters or []).union(GLOBAL_ALLOWED_PARAMETERS)

        def replace_match(match):
            key = match.group(1)
            if key not in allowed:
                raise TemplateSecurityError(
                    f"Template parameter '{{{{{key}}}}}' is not allowed in template '{template.name}'."
                )
            return str(context.get(key, ""))

        rendered_subject = PARAM_PATTERN.sub(replace_match, template.subject_template)
        rendered_html = PARAM_PATTERN.sub(replace_match, template.html_template)
        
        text_source = template.text_template if template.text_template else template.html_template
        rendered_text = PARAM_PATTERN.sub(replace_match, text_source)

        return rendered_subject, rendered_html, rendered_text

    @classmethod
    def send_single_email(
        cls,
        recipient_email: str,
        template: EmailTemplate,
        context: dict,
        from_email: str | None = None,
    ) -> bool:
        """Sends a single rendered email synchronously."""
        sender = from_email or getattr(settings, "DEFAULT_FROM_EMAIL", "SRKR Coding Club <noreply@srkrcc.in>")
        subject, html_content, text_content = cls.render_template(template, context)

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=sender,
            to=[recipient_email],
        )
        msg.attach_alternative(html_content, "text/html")
        try:
            msg.send(fail_silently=False)
            return True
        except Exception as e:
            print(f"[Email Notification Error] Failed sending to {recipient_email}: {e}")
            return False

    @classmethod
    def send_email(
        cls,
        recipient_email: str,
        subject: str,
        plain_content: str,
        html_content: str | None = None,
        from_email: str | None = None,
    ) -> bool:
        """Sends a plaintext or multipart email synchronously."""
        sender = from_email or getattr(settings, "DEFAULT_FROM_EMAIL", "SRKR Coding Club <noreply@srkrcc.in>")
        msg = EmailMultiAlternatives(
            subject=subject,
            body=plain_content,
            from_email=sender,
            to=[recipient_email],
        )
        if html_content:
            msg.attach_alternative(html_content, "text/html")
        try:
            msg.send(fail_silently=False)
            return True
        except Exception as e:
            print(f"[Email Notification Error] Failed sending direct email to {recipient_email}: {e}")
            return False

    @classmethod
    def create_email_job(
        cls,
        template: EmailTemplate,
        recipient_tuples: list[tuple[str, object | None, dict]], # list of (email, user_or_none, context_dict)
        campaign_name: str = "",
        created_by=None,
    ) -> EmailJob:
        """
        Creates an EmailJob with recipient-level EmailDelivery rows for tracking.
        """
        job = EmailJob.objects.create(
            template=template,
            campaign_name=campaign_name or f"Campaign - {template.name}",
            total_recipients=len(recipient_tuples),
            created_by=created_by,
            status=EmailJobStatus.PENDING,
        )

        deliveries = []
        for email, user, ctx in recipient_tuples:
            try:
                subj, _, _ = cls.render_template(template, ctx)
            except Exception:
                subj = template.subject_template

            deliveries.append(EmailDelivery(
                job=job,
                recipient_email=email,
                recipient_user=user,
                status=DeliveryStatus.PENDING,
                rendered_subject=subj,
            ))

        EmailDelivery.objects.bulk_create(deliveries, batch_size=200)
        return job

    @classmethod
    def process_email_job(cls, job: EmailJob, recipient_contexts: dict[str, dict] | None = None) -> EmailJob:
        """
        Executes delivery for all pending deliveries in an EmailJob.
        """
        job.status = EmailJobStatus.PROCESSING
        job.save(update_fields=["status", "updated_at"])

        deliveries = job.deliveries.filter(status=DeliveryStatus.PENDING).select_related('recipient_user')
        sender = getattr(settings, "DEFAULT_FROM_EMAIL", "SRKR Coding Club <noreply@srkrcc.in>")

        sent_count = 0
        failed_count = 0

        for delivery in deliveries:
            ctx = (recipient_contexts or {}).get(delivery.recipient_email)
            if not ctx and delivery.recipient_user:
                ctx = MemberEmailContext.build_for_user(delivery.recipient_user)
            elif not ctx:
                ctx = {"email": delivery.recipient_email, "full_name": delivery.recipient_email.split("@")[0]}

            try:
                subj, html_body, text_body = cls.render_template(job.template, ctx)
                msg = EmailMultiAlternatives(
                    subject=subj,
                    body=text_body,
                    from_email=sender,
                    to=[delivery.recipient_email],
                )
                msg.attach_alternative(html_body, "text/html")
                msg.send(fail_silently=False)

                delivery.status = DeliveryStatus.SENT
                delivery.sent_at = timezone.now()
                delivery.rendered_subject = subj
                delivery.save(update_fields=["status", "sent_at", "rendered_subject", "updated_at"])
                sent_count += 1

            except Exception as ex:
                delivery.status = DeliveryStatus.FAILED
                delivery.error_message = str(ex)
                delivery.save(update_fields=["status", "error_message", "updated_at"])
                failed_count += 1

        job.sent_count += sent_count
        job.failed_count += failed_count
        job.status = EmailJobStatus.COMPLETED if failed_count == 0 else EmailJobStatus.PARTIALLY_FAILED
        job.save(update_fields=["sent_count", "failed_count", "status", "updated_at"])
        return job
