"""Publishing a round's results: who the "results are out" notes reach, and that nobody gets one twice."""
from unittest import mock

from rest_framework.test import APITestCase

from apps.hackathons.models import AnnouncementAudience, EntryStatus, HackathonAnnouncement, Round, RoundEntry, TeamStatus
from .helpers import make_hackathon, make_team, make_user


class RoundResultEmails(APITestCase):
    def setUp(self):
        self.hack = make_hackathon()
        self.admin = make_user(role='ADMIN')
        self.alpha = make_team(self.hack, make_user(), make_user(), name='Alpha')
        self.beta = make_team(self.hack, make_user(), name='Beta')
        self.gamma = make_team(self.hack, make_user(), name='Gamma')
        self.left = make_team(self.hack, make_user(), name='Left')
        self.round = Round.objects.create(hackathon=self.hack, order=1, name='Idea round')
        for team, status in ((self.alpha, 'SHORTLISTED'), (self.left, 'SHORTLISTED'), (self.beta, 'REJECTED'), (self.gamma, 'PENDING')):
            RoundEntry.objects.create(round=self.round, team=team, status=status)
        type(self.left).objects.filter(pk=self.left.pk).update(status=TeamStatus.WITHDRAWN)
        self.client.force_authenticate(self.admin)
        self.sent = []
        self._patch('apps.core.tasks.run_in_background', lambda fn: None)
        real = __import__('apps.core.services.email_service', fromlist=['EmailNotificationService']).EmailNotificationService.create_email_job

        def record(template, recipients, **kw):
            self.sent.extend(email for email, _user, _ctx in recipients)
            return real(template, recipients, **kw)
        self._patch('apps.core.services.email_service.EmailNotificationService.create_email_job', record)

    def _patch(self, target, new):
        patcher = mock.patch(target, new)
        patcher.start()
        self.addCleanup(patcher.stop)

    def url(self, action):
        return f'/api/hackathons/{self.hack.slug}/rounds/{self.round.id}/{action}/'

    def publish(self, **body):
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.client.post(self.url('publish'), body, format='json')
        self.assertEqual(reply.status_code, 200, reply.data)

    def emails(self, *teams):
        return sorted(email for team in teams for email in team.memberships.values_list('user__email', flat=True))

    def test_shortlisted_and_other_teams_can_be_emailed_separately(self):
        self.publish(announce=True, announce_rejected=True, email_shortlisted=True, email_rejected=False)
        self.assertEqual(sorted(self.sent), self.emails(self.alpha))
        rejected = HackathonAnnouncement.objects.get(round=self.round, audience=AnnouncementAudience.ROUND_REJECTED)
        self.assertFalse(rejected.send_email)

    def test_the_old_single_email_switch_still_emails_both(self):
        self.publish(announce=True, announce_rejected=True, email=True)
        self.assertEqual(sorted(self.sent), self.emails(self.alpha, self.beta))

    def test_a_withdrawn_team_is_never_emailed_or_shown_the_note(self):
        self.publish(announce=True, email_shortlisted=True)
        self.assertNotIn(self.left.leader.email, self.sent)
        self.client.force_authenticate(self.left.leader)
        titles = [a['title'] for a in self.client.get(f'/api/hackathons/{self.hack.slug}/announcements/').data]
        self.assertNotIn('Idea round: results are out', titles)

    def test_publishing_again_updates_the_note_and_emails_nobody_twice(self):
        self.publish(announce=True, email_shortlisted=True)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url('unpublish'), {}, format='json')
        RoundEntry.objects.filter(round=self.round, team=self.gamma).update(status=EntryStatus.SHORTLISTED)
        self.publish(announce=True, email_shortlisted=True, message='Well done, round two brief inside.')
        notes = HackathonAnnouncement.objects.filter(round=self.round, audience=AnnouncementAudience.ROUND_SHORTLISTED)
        self.assertEqual(notes.count(), 1)
        self.assertEqual(notes.get().message, 'Well done, round two brief inside.')
        self.assertEqual(sorted(self.sent), self.emails(self.alpha, self.gamma))  # Alpha once, Gamma (new) once

    def test_publishing_again_without_the_note_hides_it(self):
        self.publish(announce=True)
        self.client.post(self.url('unpublish'), {}, format='json')
        self.publish(announce=False)
        self.assertFalse(HackathonAnnouncement.objects.get(round=self.round, audience=AnnouncementAudience.ROUND_SHORTLISTED).is_active)

    def test_a_team_shortlisted_after_publishing_gets_the_same_email(self):
        self.publish(announce=True, email_shortlisted=True)
        self.sent.clear()
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.client.post(self.url('decide'), {'team_ids': [self.gamma.id], 'status': 'SHORTLISTED', 'confirm_published': True}, format='json')
        self.assertEqual(reply.status_code, 200, reply.data)
        self.assertEqual(sorted(self.sent), self.emails(self.gamma))

    def test_no_email_follows_a_late_decision_when_the_note_was_not_emailed(self):
        self.publish(announce=True, email_shortlisted=False)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url('decide'), {'team_ids': [self.gamma.id], 'status': 'SHORTLISTED', 'confirm_published': True}, format='json')
        self.assertEqual(self.sent, [])


class TargetedAnnouncements(APITestCase):
    def test_a_team_waiting_for_its_registration_payment_still_gets_a_targeted_email(self):
        from apps.hackathons.services import AnnouncementService
        hack = make_hackathon()
        team = make_team(hack, make_user(), name='Unpaid', status=TeamStatus.PAYMENT_PENDING)
        note = HackathonAnnouncement.objects.create(hackathon=hack, title='Screenshot unclear', message='m', audience=AnnouncementAudience.TEAMS)
        note.target_teams.set([team])
        self.assertEqual(list(AnnouncementService.recipients(note)), [team.leader])
