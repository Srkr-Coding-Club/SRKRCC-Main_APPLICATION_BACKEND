"""
Pins the platform-wide delete policy (see docs/architecture/database-schema-and-frontend-mapping.md,
"Delete / Cascade Policy"):

- Owned child data CASCADEs with its parent.
- Cross-module links (event -> form, email delivery -> response, import -> form) SET_NULL,
  so deleting one entity never silently deletes a different kind of entity.
- Authorship / actor links SET_NULL, so history and shared content outlive an account.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.attendance.models import AttendanceBadge, AttendanceRecord, AttendanceSession
from apps.audit.models import AuditLog
from apps.blogs.models import BlogPost
from apps.codequest.models import Problem, Submission as CodeSubmission, UserStreak
from apps.core.models import (
    BackupJob, EmailDelivery, EmailJob, EmailTemplate, ImportAttempt,
)
from apps.events.models import Event
from apps.forms.models import (
    Answer, BulkIngestSession, FieldType, Form, FormField, FormStatus, MemberNote, Response,
)
from apps.hackathons.models import Hackathon, Submission as HackSubmission, Team
from apps.accounts.models import PasswordSetupToken

User = get_user_model()


def _later(days=7):
    return timezone.now() + timezone.timedelta(days=days)


class _Fixture(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username='admin1', email='admin1@srkr.ac.in', password='pw12345!', role='ADMIN',
        )
        self.member = User.objects.create_user(
            username='member1', email='member1@srkr.ac.in', password='pw12345!', role='NON_AFFILIATE',
        )
        self.teammate = User.objects.create_user(
            username='mate1', email='mate1@srkr.ac.in', password='pw12345!', role='NON_AFFILIATE',
        )
        self.form = Form.objects.create(title='Reg', slug='reg', status=FormStatus.PUBLISHED)
        self.field = FormField.objects.create(form=self.form, label='Name', type=FieldType.TEXT)
        self.response = Response.objects.create(form=self.form, user=self.member)
        self.answer = Answer.objects.create(response=self.response, field=self.field, value='x')

        self.session = AttendanceSession.objects.create(
            form=self.form, day_index=0, session_label='MORNING', date=timezone.now().date(),
        )
        self.badge = AttendanceBadge.objects.create(response=self.response)
        self.record = AttendanceRecord.objects.create(badge=self.badge, session=self.session, scanned_by=self.admin)
        self.ingest = BulkIngestSession.objects.create(form=self.form, idempotency_key='k1', created_by=self.admin)

        template = EmailTemplate.objects.create(name='t1', subject_template='s', html_template='h')
        job = EmailJob.objects.create(template=template)
        self.delivery = EmailDelivery.objects.create(
            job=job, recipient_email=self.member.email, recipient_user=self.member, response=self.response,
        )
        backup = BackupJob.objects.create(
            original_filename='b.csv', file_sha256='0' * 64, file_size_bytes=1, file_format='CSV',
        )
        self.import_attempt = ImportAttempt.objects.create(
            backup_job=backup, target_domain='FORMS', target_form=self.form,
        )

        self.event = Event.objects.create(
            title='E', slug='e', description='d', registration_form=self.form,
            start_time=_later(), end_time=_later(8),
        )
        self.hackathon = Hackathon.objects.create(
            title='H', slug='h', theme='t', description='d', registration_form=self.form,
            start_date=_later(), end_date=_later(8),
        )
        self.team = Team.objects.create(hackathon=self.hackathon, name='T', leader=self.member)
        self.team.members.add(self.teammate)
        self.hack_submission = HackSubmission.objects.create(
            team=self.team, project_title='P', description='d', repo_url='https://github.com/x/y',
        )


class FormDeleteCascadeTests(_Fixture):
    def test_admin_form_delete_succeeds_even_when_form_was_an_import_target(self):
        """Regression: ImportAttempt.target_form used to be PROTECT -> 500 on delete."""
        self.client.force_authenticate(self.admin)
        resp = self.client.delete(f'/api/forms/{self.form.slug}/')
        self.assertEqual(resp.status_code, 204)

    def test_form_delete_cascades_owned_data_and_unlinks_everything_else(self):
        self.form.delete()

        self.assertFalse(FormField.all_objects.filter(pk=self.field.pk).exists())
        for model, pk in [
            (Response, self.response.pk), (Answer, self.answer.pk),
            (AttendanceSession, self.session.pk), (AttendanceBadge, self.badge.pk),
            (AttendanceRecord, self.record.pk), (BulkIngestSession, self.ingest.pk),
        ]:
            self.assertFalse(model.objects.filter(pk=pk).exists(), model.__name__)

        self.event.refresh_from_db()
        self.hackathon.refresh_from_db()
        self.delivery.refresh_from_db()
        self.import_attempt.refresh_from_db()
        self.assertIsNone(self.event.registration_form)
        self.assertIsNone(self.hackathon.registration_form)
        self.assertIsNone(self.delivery.response)
        self.assertIsNone(self.import_attempt.target_form)

    def test_form_delete_is_audit_logged(self):
        self.client.force_authenticate(self.admin)
        self.client.delete(f'/api/forms/{self.form.slug}/')
        log = AuditLog.objects.get(action='Deleted Form', target_id='reg')
        self.assertEqual(log.details['responses_deleted'], 1)
        self.assertEqual(log.details['unlinked_events'], ['e'])


class EventAndHackathonDeleteCascadeTests(_Fixture):
    def test_event_delete_keeps_the_linked_form_and_its_responses(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.delete(f'/api/events/{self.event.slug}/')
        self.assertEqual(resp.status_code, 204)
        self.assertTrue(Form.objects.filter(pk=self.form.pk).exists())
        self.assertTrue(Response.objects.filter(pk=self.response.pk).exists())
        self.assertTrue(AuditLog.objects.filter(action='Deleted Event', target_id='e').exists())

    def test_hackathon_delete_cascades_teams_and_submissions_but_keeps_form(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.delete(f'/api/hackathons/{self.hackathon.slug}/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Team.objects.filter(pk=self.team.pk).exists())
        self.assertFalse(HackSubmission.objects.filter(pk=self.hack_submission.pk).exists())
        self.assertTrue(Form.objects.filter(pk=self.form.pk).exists())
        self.assertTrue(User.objects.filter(pk=self.teammate.pk).exists())
        log = AuditLog.objects.get(action='Deleted Hackathon', target_id='h')
        self.assertEqual(log.details['teams_deleted'], 1)

    def test_non_admin_cannot_delete_event_or_hackathon(self):
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.delete(f'/api/events/{self.event.slug}/').status_code, 403)
        self.assertEqual(self.client.delete(f'/api/hackathons/{self.hackathon.slug}/').status_code, 403)
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())
        self.assertTrue(Hackathon.objects.filter(pk=self.hackathon.pk).exists())


class UserDeleteCascadeTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.token = PasswordSetupToken.objects.create(
            user=self.member, token_hash='a' * 64, expires_at=_later(1),
        )
        problem = Problem.objects.create(title='P', slug='p', statement='s', scheduled_date=timezone.now().date())
        self.code_sub = CodeSubmission.objects.create(problem=problem, user=self.member, code='print(1)')
        self.streak = UserStreak.objects.create(user=self.member)
        self.note = MemberNote.objects.create(user=self.member, note='n', created_by=self.admin)
        self.blog = BlogPost.objects.create(title='B', slug='b', author=self.member, content='c')

    def test_user_delete_removes_personal_data(self):
        self.member.delete()
        self.assertFalse(PasswordSetupToken.objects.filter(pk=self.token.pk).exists())
        self.assertFalse(CodeSubmission.objects.filter(pk=self.code_sub.pk).exists())
        self.assertFalse(UserStreak.objects.filter(pk=self.streak.pk).exists())
        self.assertFalse(MemberNote.objects.filter(pk=self.note.pk).exists())

    def test_user_delete_keeps_shared_records_with_link_cleared(self):
        self.member.delete()

        self.team.refresh_from_db()
        self.assertIsNone(self.team.leader)  # regression: used to CASCADE the whole team
        self.assertIn(self.teammate, self.team.members.all())
        self.assertTrue(HackSubmission.objects.filter(pk=self.hack_submission.pk).exists())

        self.blog.refresh_from_db()
        self.assertIsNone(self.blog.author)  # regression: used to CASCADE the post

        self.response.refresh_from_db()
        self.assertIsNone(self.response.user)
        self.delivery.refresh_from_db()
        self.assertIsNone(self.delivery.recipient_user)

    def test_actor_delete_keeps_history(self):
        self.admin.delete()
        self.record.refresh_from_db()
        self.assertIsNone(self.record.scanned_by)
        self.ingest.refresh_from_db()
        self.assertIsNone(self.ingest.created_by)
