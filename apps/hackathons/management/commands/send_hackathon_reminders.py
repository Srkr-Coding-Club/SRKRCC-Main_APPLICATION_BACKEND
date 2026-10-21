from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.hackathons.models import Hackathon, HackathonStatus
from apps.hackathons.submissions import SubmissionService

REMINDER_LEAD = timedelta(hours=24)


class Command(BaseCommand):
    help = (
        "Remind teams that have not submitted a project when a deadline is less than 24 hours away "
        "(safe to run from cron every hour; each hackathon is reminded once)."
    )

    def handle(self, *args, **options):
        now = timezone.now()
        due = Hackathon.objects.filter(
            status=HackathonStatus.LIVE, submission_deadline__gt=now, submission_deadline__lte=now + REMINDER_LEAD,
            submission_reminder_sent_at__isnull=True,
        )
        reminded = 0
        for hackathon in due:
            if not hackathon.feature('submissions'):
                continue
            reminded += SubmissionService.remind_missing(hackathon, None)
            hackathon.submission_reminder_sent_at = now
            hackathon.save(update_fields=['submission_reminder_sent_at'])
        self.stdout.write(self.style.SUCCESS(f"Reminded {reminded} team(s)."))
