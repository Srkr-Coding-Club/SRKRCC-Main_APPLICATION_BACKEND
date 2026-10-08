import logging
from django.contrib.auth import get_user_model
from django.utils.html import escape, linebreaks
from django.conf import settings
from apps.audit.utils import log_audit_event
from apps.core.models import EmailTemplate
from apps.core.services.email_service import EmailNotificationService, MemberEmailContext
from apps.core.tasks import run_in_background, process_email_job
from .models import Notification, NotificationType, NotificationCategory

logger = logging.getLogger(__name__)


class NotificationService:
    @staticmethod
    def create_notification(
        recipient,
        title: str,
        message: str,
        type: str = NotificationType.INFO,
        category: str = NotificationCategory.GENERAL,
        link_url: str = '',
        created_by=None,
    ) -> Notification:
        """Create a single in-app notification for a user."""
        return Notification.objects.create(
            recipient=recipient,
            title=title,
            message=message,
            type=type,
            category=category,
            link_url=link_url or '',
            created_by=created_by,
        )

    @staticmethod
    def create_bulk_notifications(
        recipients,
        title: str,
        message: str,
        type: str = NotificationType.INFO,
        category: str = NotificationCategory.GENERAL,
        link_url: str = '',
        created_by=None,
    ) -> int:
        """Bulk-create in-app notifications for multiple recipients."""
        records = [
            Notification(
                recipient=user,
                title=title,
                message=message,
                type=type,
                category=category,
                link_url=link_url or '',
                created_by=created_by,
            )
            for user in recipients
        ]
        Notification.objects.bulk_create(records, batch_size=500)
        return len(records)

    @staticmethod
    def resolve_audience(
        audience: str,
        target_role: str = '',
        target_hackathon_slug: str = '',
        target_user_ids: list[int] | None = None,
    ):
        """Resolves target queryset of active users based on audience selection."""
        User = get_user_model()
        qs = User.objects.filter(is_active=True)

        if audience == 'ROLE' and target_role:
            return qs.filter(role=target_role)

        if audience == 'HACKATHON' and target_hackathon_slug:
            return qs.filter(team_memberships__hackathon__slug=target_hackathon_slug).distinct()

        if audience == 'USERS' and target_user_ids:
            return qs.filter(id__in=target_user_ids)

        # 'ALL'
        return qs

    @classmethod
    def broadcast(
        cls,
        title: str,
        message: str,
        channels: list[str],
        audience: str = 'ALL',
        target_role: str = '',
        target_hackathon_slug: str = '',
        target_user_ids: list[int] | None = None,
        type: str = NotificationType.INFO,
        category: str = NotificationCategory.GENERAL,
        link_url: str = '',
        actor=None,
    ) -> dict:
        """
        Dispatches in-app notifications and/or emails across target recipients.
        Dispatches emails safely in background threads via apps/core/tasks.py.
        """
        users = list(cls.resolve_audience(audience, target_role, target_hackathon_slug, target_user_ids))
        total_recipients = len(users)

        in_app_count = 0
        email_count = 0
        email_job_id = None

        if not users:
            return {
                'total_recipients': 0,
                'in_app_count': 0,
                'email_count': 0,
                'email_job_id': None,
            }

        # 1. In-App Notifications
        if 'IN_APP' in channels:
            in_app_count = cls.create_bulk_notifications(
                recipients=users,
                title=title,
                message=message,
                type=type,
                category=category,
                link_url=link_url,
                created_by=actor,
            )

        # 2. Email Notifications
        if 'EMAIL' in channels:
            users_with_email = [u for u in users if u.email and '@' in u.email]
            if users_with_email:
                def defuse(text):
                    return text.replace('{{', '{ {').replace('}}', '} }')

                clean_title = defuse(title)[:200]
                clean_body = defuse(message)
                portal_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000')

                action_html = f'<p style="margin-top: 24px;"><a href="{portal_url}{link_url}" style="background-color: #FF7A00; color: #ffffff; padding: 10px 20px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">Open In Club Portal</a></p>' if link_url else ''
                action_text = f'\n\nLink: {portal_url}{link_url}' if link_url else ''

                template = EmailTemplate.objects.create(
                    name=f"broadcast_{type.lower()}_{actor.id if actor else 'sys'}",
                    display_title=f"Broadcast: {clean_title}"[:200],
                    subject_template=f"[SRKR Coding Club] {clean_title}",
                    html_template=(
                        '<p>Hi {{first_name}},</p>'
                        f'<h3 style="color: #1A1A2E;">{escape(clean_title)}</h3>'
                        f'{linebreaks(escape(clean_body))}'
                        f'{action_html}'
                        '<hr style="border: 0; border-top: 1px solid #e2e8f0; margin: 30px 0;">'
                        '<p style="font-size: 11px; color: #94a3b8;">This is an official announcement from SRKR Coding Club.</p>'
                    ),
                    text_template=(
                        'Hi {{first_name}},\n\n'
                        f'{clean_title}\n\n'
                        f'{clean_body}'
                        f'{action_text}\n\n'
                        '---\nSRKR Coding Club'
                    ),
                    allowed_parameters=['first_name', 'portal_url'],
                    created_by=actor,
                )

                recipients_data = [(u.email, u, MemberEmailContext.build_for_user(u)) for u in users_with_email]
                job = EmailNotificationService.create_email_job(
                    template=template,
                    recipient_tuples=recipients_data,
                    campaign_name=f"Broadcast: {clean_title}"[:100],
                    created_by=actor,
                )
                email_job_id = job.id
                email_count = len(recipients_data)

                # Dispatch on background thread without blocking the response
                run_in_background(lambda: process_email_job(job.id))

        if actor:
            log_audit_event(
                actor=actor,
                action="Broadcast Sent",
                target_model="Notification",
                details={
                    "title": title,
                    "channels": channels,
                    "audience": audience,
                    "recipients": total_recipients,
                    "in_app_count": in_app_count,
                    "email_count": email_count,
                },
            )

        return {
            'total_recipients': total_recipients,
            'in_app_count': in_app_count,
            'email_count': email_count,
            'email_job_id': email_job_id,
        }
