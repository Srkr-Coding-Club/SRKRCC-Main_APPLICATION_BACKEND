"""Where the account, events, attendance and CodeQuest raise celebrations: the right person, once, and never for what is not a happy moment."""
from datetime import timedelta

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.accounts.models import User
from apps.accounts.services import email_verification
from apps.attendance.models import AttendanceBadge, AttendanceRecord
from apps.attendance.services import generate_sessions, issue_badge
from apps.celebrations import services
from apps.celebrations.models import Celebration
from apps.codequest.models import Problem, Submission
from apps.events.models import Event
from apps.forms.models import FormStatus, Response
from apps.forms.tests.factories import make_form

_n = {'i': 0}


def make_user(**kw):
    _n['i'] += 1
    defaults = dict(username=f'hook{_n["i"]}', email=f'hook{_n["i"]}@srkr.ac.in', password='pw12345!', role='NON_AFFILIATE')
    defaults.update(kw)
    return User.objects.create_user(**defaults)


def api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


def kinds_of(user):
    return sorted(Celebration.objects.filter(user=user).values_list('kind', flat=True))


class AccountTests(APITestCase):
    body = {'email': 'newbie@example.org', 'password': 'Str0ng!Pass123', 'first_name': 'New', 'branch': 'CSE', 'year': 2}

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_signing_up_celebrates_the_new_member_once(self):
        with self.captureOnCommitCallbacks(execute=True):
            reply = APIClient().post('/api/auth/register/', self.body, format='json')
        self.assertEqual(reply.status_code, 201, reply.data)
        member = User.objects.get(email='newbie@example.org')
        self.assertEqual(kinds_of(member), ['account.welcome'])
        card = Celebration.objects.get(user=member)
        self.assertEqual((card.size, card.title), ('MEDIUM', 'Welcome to SRKR Coding Club!'))

    def test_a_refused_signup_celebrates_nobody(self):
        with self.captureOnCommitCallbacks(execute=True):
            reply = APIClient().post('/api/auth/register/', {**self.body, 'password': 'x'}, format='json')
        self.assertEqual(reply.status_code, 400)
        self.assertEqual(Celebration.objects.count(), 0)

    def test_the_welcome_is_there_before_the_email_is_verified(self):
        with self.captureOnCommitCallbacks(execute=True):
            APIClient().post('/api/auth/register/', self.body, format='json')
        member = User.objects.get(email='newbie@example.org')
        self.assertEqual(len(api(member).get('/api/celebrations/pending/').data['results']), 1)

    def test_verifying_the_email_celebrates_once(self):
        member = make_user()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(email_verification.mark_verified(member))
            self.assertFalse(email_verification.mark_verified(member))        # already verified: nothing again
        self.assertEqual(kinds_of(member), ['account.email_verified'])

    @override_settings(EMAIL_VERIFICATION_MODE='features')
    def test_confirming_the_link_celebrates_once_even_when_pressed_twice(self):
        from django.core import mail

        member = make_user()
        email_verification.request_link(member)
        import re

        token = re.search(r'verify-email\?token=([\w-]+)', mail.outbox[-1].body).group(1)
        with self.captureOnCommitCallbacks(execute=True):
            first = email_verification.confirm(token)
            second = email_verification.confirm(token)
        self.assertEqual((first['already'], second['already']), (False, True))
        self.assertEqual(kinds_of(member), ['account.email_verified'])


class EventTests(APITestCase):
    def setUp(self):
        self.member = make_user()
        self.form = make_form(status=FormStatus.PUBLISHED)
        self.event = Event.objects.create(title='Django Workshop', slug='django-workshop', description='d', registration_form=self.form)

    def register(self, **kw):
        with self.captureOnCommitCallbacks(execute=True):
            return Response.objects.create(form=self.form, user=self.member, **kw)

    def test_registering_for_an_event_celebrates_with_the_event_name_and_a_link(self):
        self.register()
        card = Celebration.objects.get(user=self.member)
        self.assertEqual((card.kind, card.size, card.link_url), ('event.registered', 'MEDIUM', '/events/django-workshop'))
        self.assertIn('Django Workshop', card.message)

    def test_a_second_registration_of_the_same_event_is_not_celebrated_again(self):
        self.register()
        self.register()
        self.assertEqual(Celebration.objects.count(), 1)

    def test_previews_admin_entries_anonymous_answers_and_other_forms_are_not_celebrated(self):
        admin = make_user(role='ADMIN')
        self.register(is_test_submission=True)
        with self.captureOnCommitCallbacks(execute=True):
            Response.objects.create(form=self.form, user=self.member, is_manual_entry=True, created_by_admin=admin)
            Response.objects.create(form=self.form, user=None)
            Response.objects.create(form=make_form(status=FormStatus.PUBLISHED), user=self.member)
        self.assertEqual(Celebration.objects.count(), 0)

    def test_scanning_a_registrant_in_for_the_first_time_celebrates_them_once_per_event(self):
        form = make_form(status=FormStatus.PUBLISHED, attendance_enabled=True, attendance_start_date=timezone.now().date(),
                         attendance_days=1, attendance_sessions_per_day=2)
        generate_sessions(form)
        event = Event.objects.create(title='Hack Night', slug='hack-night', description='d', registration_form=form)
        response = Response.objects.create(form=form, user=self.member)
        badge = issue_badge(response)
        volunteer = make_user(role='ADMIN')
        first, second = list(form.attendance_sessions.order_by('session_label'))
        with self.captureOnCommitCallbacks(execute=True):
            for session in (first, second, first):
                reply = api(volunteer).post('/api/attendance/scan/', {'token': badge.token, 'session_id': session.id}, format='json')
                self.assertEqual(reply.status_code, 200, reply.data)
        self.assertEqual(AttendanceRecord.objects.filter(badge=badge).count(), 2)
        checked_in = Celebration.objects.filter(user=self.member, kind='event.checked_in')
        self.assertEqual(checked_in.count(), 1)
        self.assertEqual(checked_in.get().link_url, '/events/hack-night')

    def test_a_scan_for_a_form_that_is_not_an_event_celebrates_nothing(self):
        form = make_form(status=FormStatus.PUBLISHED, attendance_enabled=True, attendance_start_date=timezone.now().date(),
                         attendance_days=1, attendance_sessions_per_day=1)
        generate_sessions(form)
        badge = issue_badge(Response.objects.create(form=form, user=self.member))
        with self.captureOnCommitCallbacks(execute=True):
            api(make_user(role='ADMIN')).post('/api/attendance/scan/', {'token': badge.token, 'session_id': form.attendance_sessions.first().id}, format='json')
        self.assertFalse(Celebration.objects.filter(kind='event.checked_in').exists())


class CodeQuestTests(APITestCase):
    def setUp(self):
        self.member = make_user()
        self.admin = make_user(role='ADMIN')
        self.today = timezone.localdate()

    def solved(self, days_ago):
        problem = Problem.objects.create(
            title=f'P{days_ago}', slug=f'p{days_ago}-{self.member.pk}', statement='s', scheduled_date=self.today - timedelta(days=days_ago),
        )
        return Submission.objects.create(problem=problem, user=self.member, code='x')

    def review(self, submission, correct=True):
        with self.captureOnCommitCallbacks(execute=True):
            reply = api(self.admin).post(f'/api/codequest/submissions/{submission.pk}/review/', {'is_correct': correct}, format='json')
        self.assertEqual(reply.status_code, 200, reply.data)

    def test_the_first_correct_solution_is_celebrated_once(self):
        self.review(self.solved(0))
        self.assertEqual(kinds_of(self.member), ['codequest.first_solve'])
        self.review(self.solved(5))                                              # a second one is not the first
        self.assertEqual(kinds_of(self.member), ['codequest.first_solve'])

    def test_a_wrong_answer_celebrates_nothing(self):
        self.review(self.solved(0), correct=False)
        self.assertEqual(Celebration.objects.count(), 0)

    def test_a_streak_is_celebrated_on_the_day_it_reaches_a_milestone_and_not_before_or_after(self):
        submissions = [self.solved(days) for days in range(7, -1, -1)]          # 8 days in a row, ending today
        for submission in submissions[:6]:
            self.review(submission)
        self.assertFalse(Celebration.objects.filter(kind='codequest.streak').exists())    # 6 days
        self.review(submissions[6])                                                       # the 7th day
        streak = Celebration.objects.get(kind='codequest.streak')
        self.assertEqual((streak.title, streak.key, streak.size), ('7-day streak!', 'streak:7', 'MEDIUM'))
        self.review(submissions[7])                                                       # the 8th day: no new card
        self.assertEqual(Celebration.objects.filter(kind='codequest.streak').count(), 1)

    def test_re_reviewing_the_same_verdict_does_not_celebrate_again(self):
        submission = self.solved(0)
        self.review(submission)
        before = Celebration.objects.count()
        self.review(submission)
        self.assertEqual(Celebration.objects.count(), before)

    def test_someone_with_celebrations_off_gets_none(self):
        services.set_enabled(self.member, False)
        self.review(self.solved(0))
        self.assertEqual(Celebration.objects.count(), 0)
