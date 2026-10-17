"""The numbers on the member's profile page (GET /api/auth/me/)."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.attendance.models import AttendanceBadge, AttendanceRecord, AttendanceSession, SessionLabel
from apps.codequest.models import Problem, Submission
from apps.events.models import Event
from apps.forms.models import Response
from apps.forms.tests.factories import make_form
from apps.hackathons.models import Hackathon, Team, TeamMember

User = get_user_model()


class ProfileStatsTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(username='stats1', email='stats1@srkr.ac.in', password='pw12345!')
        self.other = User.objects.create_user(username='stats2', email='stats2@srkr.ac.in', password='pw12345!')
        self.client = APIClient()
        self.client.force_authenticate(self.member)

    def _stats(self):
        resp = self.client.get('/api/auth/me/')
        self.assertEqual(resp.status_code, 200)
        return resp.data

    def _event(self, slug):
        form = make_form(slug=f'{slug}-form', title=f'{slug} form')
        event = Event.objects.create(title=slug, slug=slug, description='d', registration_form=form)
        return event, form

    def test_a_new_member_starts_at_zero(self):
        data = self._stats()
        for key in ('events_registered_count', 'events_attended_count', 'hackathon_teams_count', 'problems_solved'):
            self.assertEqual(data[key], 0, key)

    def test_the_replaced_stats_are_gone(self):
        data = self._stats()
        self.assertNotIn('points', data)
        self.assertNotIn('projects_count', data)

    def test_events_registered_counts_events_not_every_form(self):
        _, event_form = self._event('workshop')
        _, second_event_form = self._event('seminar')
        plain_form = make_form(slug='survey', title='Survey')
        for form in (event_form, second_event_form, plain_form):
            Response.objects.create(form=form, user=self.member)

        data = self._stats()
        self.assertEqual(data['events_registered_count'], 2)
        self.assertEqual(data['events_count'], 3)  # every registration and submission, as before

    def test_registering_twice_for_one_event_counts_it_once(self):
        _, form = self._event('workshop')
        Response.objects.create(form=form, user=self.member)
        Response.objects.create(form=form, user=self.member)
        self.assertEqual(self._stats()['events_registered_count'], 1)

    def test_test_submissions_and_other_members_do_not_count(self):
        _, form = self._event('workshop')
        Response.objects.create(form=form, user=self.member, is_test_submission=True)
        Response.objects.create(form=form, user=self.other)
        self.assertEqual(self._stats()['events_registered_count'], 0)

    def test_events_attended_counts_each_event_once_however_many_sessions(self):
        _, form = self._event('workshop')
        response = Response.objects.create(form=form, user=self.member)
        badge = AttendanceBadge.objects.create(response=response)
        for day in range(2):
            session = AttendanceSession.objects.create(
                form=form, day_index=day, session_label=SessionLabel.MORNING, date=timezone.localdate(),
            )
            AttendanceRecord.objects.create(badge=badge, session=session)

        self.assertEqual(self._stats()['events_attended_count'], 1)

    def test_a_registration_without_a_scan_is_not_attendance(self):
        _, form = self._event('workshop')
        response = Response.objects.create(form=form, user=self.member)
        AttendanceBadge.objects.create(response=response)
        self.assertEqual(self._stats()['events_attended_count'], 0)

    def test_someone_elses_attendance_does_not_count(self):
        _, form = self._event('workshop')
        response = Response.objects.create(form=form, user=self.other)
        badge = AttendanceBadge.objects.create(response=response)
        session = AttendanceSession.objects.create(
            form=form, day_index=0, session_label=SessionLabel.MORNING, date=timezone.localdate(),
        )
        AttendanceRecord.objects.create(badge=badge, session=session)
        self.assertEqual(self._stats()['events_attended_count'], 0)

    def test_hackathon_teams_counts_the_teams_the_member_belongs_to(self):
        start = timezone.now() + timezone.timedelta(days=5)

        def hackathon(slug):
            return Hackathon.objects.create(
                title=slug, slug=slug, theme='t', description='d',
                start_date=start, end_date=start + timezone.timedelta(days=1),
            )

        first, second, third = hackathon('first'), hackathon('second'), hackathon('third')
        led = Team.objects.create(hackathon=first, name='Led', leader=self.member)
        joined = Team.objects.create(hackathon=second, name='Joined', leader=self.other)
        not_mine = Team.objects.create(hackathon=third, name='Not mine', leader=self.other)
        TeamMember.objects.create(team=led, user=self.member, role='LEADER')
        TeamMember.objects.create(team=joined, user=self.other, role='LEADER')
        TeamMember.objects.create(team=joined, user=self.member, role='MEMBER')
        TeamMember.objects.create(team=not_mine, user=self.other, role='LEADER')

        self.assertEqual(self._stats()['hackathon_teams_count'], 2)

    def test_problems_solved_counts_each_problem_once_and_only_correct_answers(self):
        today = timezone.localdate()
        first = Problem.objects.create(title='A', slug='a', statement='s', scheduled_date=today)
        second = Problem.objects.create(
            title='B', slug='b', statement='s', scheduled_date=today + timezone.timedelta(days=1),
        )
        third = Problem.objects.create(
            title='C', slug='c', statement='s', scheduled_date=today + timezone.timedelta(days=2),
        )
        Submission.objects.create(problem=first, user=self.member, code='x', is_correct=True)
        Submission.objects.create(problem=first, user=self.member, code='y', is_correct=True)
        Submission.objects.create(problem=second, user=self.member, code='x', is_correct=True)
        Submission.objects.create(problem=third, user=self.member, code='x', is_correct=False)
        Submission.objects.create(problem=third, user=self.other, code='x', is_correct=True)

        self.assertEqual(self._stats()['problems_solved'], 2)
