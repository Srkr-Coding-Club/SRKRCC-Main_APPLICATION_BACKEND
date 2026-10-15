from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.hackathons.models import (
    AnnouncementAudience, EntryStatus, Hackathon, HackathonAnnouncement, HackathonStatus, TeamStatus,
)
from apps.hackathons.services import AnnouncementService, RoundService, TeamService
from apps.notifications.models import Notification

from .helpers import make_hackathon, make_ps, make_team, make_user


class PublishedResultsTests(TestCase):
    def setUp(self):
        self.admin = make_user(role='ADMIN')
        self.hack = make_hackathon(min_team_size=1)
        self.leader_a, self.leader_b = make_user(), make_user()
        self.team_a = make_team(self.hack, self.leader_a)
        self.team_b = make_team(self.hack, self.leader_b)
        self.round = RoundService.create_round(self.hack, self.admin, name='R1')
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.base = f'/api/hackathons/{self.hack.slug}/rounds/{self.round.id}'

    def _decide(self, team, status, **extra):
        return self.client.post(
            f'{self.base}/decide/', {'team_ids': [team.id], 'status': status, **extra}, format='json',
        )

    def test_deciding_before_publishing_needs_no_confirmation(self):
        self.assertEqual(self._decide(self.team_a, 'SHORTLISTED').status_code, 200)

    def test_changing_a_decision_after_publishing_requires_confirmation(self):
        self._decide(self.team_a, 'SHORTLISTED')
        RoundService.publish_results(self.round, self.admin)
        response = self._decide(self.team_a, 'REJECTED')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'RESULTS_PUBLISHED')
        self.assertEqual(self.round.entries.get(team=self.team_a).status, EntryStatus.SHORTLISTED)

    def test_confirmed_change_updates_the_entry_and_notifies_the_team(self):
        self._decide(self.team_a, 'SHORTLISTED')
        RoundService.publish_results(self.round, self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            response = self._decide(self.team_a, 'REJECTED', confirm_published=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.round.entries.get(team=self.team_a).status, EntryStatus.REJECTED)
        self.assertEqual(Notification.objects.filter(recipient=self.leader_a).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.leader_b).count(), 0)

    def test_confirming_an_unchanged_decision_notifies_nobody(self):
        self._decide(self.team_a, 'SHORTLISTED')
        RoundService.publish_results(self.round, self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            self._decide(self.team_a, 'SHORTLISTED', confirm_published=True)
        self.assertEqual(Notification.objects.count(), 0)

    def test_publishing_twice_is_rejected_and_posts_one_announcement(self):
        first = self.client.post(f'{self.base}/publish/', {'announce': True}, format='json')
        second = self.client.post(f'{self.base}/publish/', {'announce': True}, format='json')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.data['code'], 'ALREADY_PUBLISHED')
        self.assertEqual(HackathonAnnouncement.objects.count(), 1)

    def test_unpublish_then_publish_again_works(self):
        self.client.post(f'{self.base}/publish/', {}, format='json')
        self.client.post(f'{self.base}/unpublish/', {}, format='json')
        self.assertEqual(self.client.post(f'{self.base}/publish/', {}, format='json').status_code, 200)


class RejectedTeamsAnnouncementTests(TestCase):
    def setUp(self):
        self.admin = make_user(role='ADMIN')
        self.hack = make_hackathon(min_team_size=1)
        self.winner_user, self.loser_user = make_user(), make_user()
        winner, loser = make_team(self.hack, self.winner_user), make_team(self.hack, self.loser_user)
        self.round = RoundService.create_round(self.hack, self.admin, name='R1')
        RoundService.decide(self.round, self.admin, [winner.id], EntryStatus.SHORTLISTED)
        RoundService.decide(self.round, self.admin, [loser.id], EntryStatus.REJECTED)

    def _visible_titles(self, user):
        return [a.audience for a in AnnouncementService.visible_for(self.hack, user)]

    def test_publish_can_announce_to_rejected_teams_only(self):
        RoundService.publish_results(self.round, self.admin, announce_rejected=True)
        self.assertEqual(self._visible_titles(self.loser_user), [AnnouncementAudience.ROUND_REJECTED])
        self.assertEqual(self._visible_titles(self.winner_user), [])

    def test_both_announcements_reach_their_own_audience(self):
        RoundService.publish_results(self.round, self.admin, announce=True, announce_rejected=True)
        self.assertEqual(self._visible_titles(self.winner_user), [AnnouncementAudience.ROUND_SHORTLISTED])
        self.assertEqual(self._visible_titles(self.loser_user), [AnnouncementAudience.ROUND_REJECTED])

    def test_rejected_announcement_emails_only_rejected_members(self):
        RoundService.publish_results(self.round, self.admin, announce_rejected=True)
        announcement = HackathonAnnouncement.objects.get()
        self.assertEqual(list(AnnouncementService.recipients(announcement)), [self.loser_user])

    def test_rejected_audience_requires_a_round(self):
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.post(f'/api/hackathons/{self.hack.slug}/announcements/', {
            'title': 'T', 'message': 'm', 'audience': 'ROUND_REJECTED',
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_rejected_result_stays_hidden_until_published(self):
        announcement = HackathonAnnouncement.objects.create(
            hackathon=self.hack, title='T', message='m', audience=AnnouncementAudience.ROUND_REJECTED, round=self.round,
        )
        self.assertNotIn(announcement.pk, [a.pk for a in AnnouncementService.visible_for(self.hack, self.loser_user)])


class TeamSizeChangeTests(TestCase):
    def setUp(self):
        self.admin = make_user(role='ADMIN')
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_raising_min_size_demotes_teams_that_no_longer_qualify(self):
        hack = make_hackathon(min_team_size=1, max_team_size=4)
        solo = TeamService.create_team(hack, make_user(), 'Solo')
        full = make_team(hack, make_user(), make_user(), make_user(), name='Trio')
        self.assertEqual(solo.status, TeamStatus.REGISTERED)

        response = self.client.patch(f'/api/hackathons/{hack.slug}/', {'min_team_size': 3}, format='json')

        self.assertEqual(response.status_code, 200)
        solo.refresh_from_db()
        full.refresh_from_db()
        self.assertEqual(solo.status, TeamStatus.FORMING)
        self.assertEqual(full.status, TeamStatus.REGISTERED)

    def test_lowering_min_size_promotes_forming_teams(self):
        hack = make_hackathon(min_team_size=3, max_team_size=4)
        team = make_team(hack, make_user(), make_user(), name='Pair', status=TeamStatus.FORMING)
        self.client.patch(f'/api/hackathons/{hack.slug}/', {'min_team_size': 2}, format='json')
        team.refresh_from_db()
        self.assertEqual(team.status, TeamStatus.REGISTERED)

    def test_max_size_cannot_drop_below_an_existing_team(self):
        hack = make_hackathon(min_team_size=1, max_team_size=4)
        make_team(hack, make_user(), make_user(), make_user(), name='Trio')
        response = self.client.patch(f'/api/hackathons/{hack.slug}/', {'max_team_size': 2}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('max_team_size', response.data)


class LockedHackathonTests(TestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, team_edits_locked=True)

    def test_participants_cannot_create_teams_while_locked(self):
        client = APIClient()
        client.force_authenticate(make_user())
        response = client.post(f'/api/hackathons/{self.hack.slug}/teams/', {'name': 'Late'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'TEAM_EDITS_LOCKED')

    def test_admins_can_still_create_teams_while_locked(self):
        admin = make_user(role='ADMIN')
        client = APIClient()
        client.force_authenticate(admin)
        response = client.post(
            f'/api/hackathons/{self.hack.slug}/teams/', {'name': 'Admin team', 'leader_email': make_user().email}, format='json',
        )
        self.assertEqual(response.status_code, 201)


class HackathonValidationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(make_user(role='ADMIN'))
        now = timezone.now()
        self.start, self.end = now + timezone.timedelta(days=5), now + timezone.timedelta(days=6)

    def _create(self, **overrides):
        body = {
            'title': 'X', 'slug': 'x-hack', 'theme': 't', 'description': 'd',
            'start_date': self.start.isoformat(), 'end_date': self.end.isoformat(),
        }
        body.update(overrides)
        return self.client.post('/api/hackathons/', body, format='json')

    def test_end_must_be_after_start(self):
        response = self._create(end_date=(self.start - timezone.timedelta(days=1)).isoformat())
        self.assertEqual(response.status_code, 400)
        self.assertIn('end_date', response.data)

    def test_visibility_window_must_be_ordered(self):
        response = self._create(
            visible_from=self.end.isoformat(), visible_until=self.start.isoformat(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('visible_until', response.data)

    def test_registration_must_close_before_the_event_ends(self):
        response = self._create(registration_closes_at=(self.end + timezone.timedelta(days=1)).isoformat())
        self.assertEqual(response.status_code, 400)
        self.assertIn('registration_closes_at', response.data)

    def test_reserved_slugs_are_rejected(self):
        for slug in ('my-teams', 'my-invites', 'submissions'):
            self.assertEqual(self._create(slug=slug).status_code, 400, slug)

    def test_slug_cannot_change_after_creation(self):
        self._create()
        response = self.client.patch('/api/hackathons/x-hack/', {'slug': 'renamed'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('slug', response.data)

    def test_resending_the_same_slug_is_fine(self):
        self._create()
        self.assertEqual(self.client.patch('/api/hackathons/x-hack/', {'slug': 'x-hack', 'theme': 'new'}, format='json').status_code, 200)

    def test_unrelated_patch_is_not_blocked_by_legacy_bad_dates(self):
        hack = make_hackathon(slug='legacy', registration_closes_at=self.end + timezone.timedelta(days=30))
        response = self.client.patch(f'/api/hackathons/{hack.slug}/', {'theme': 'tweak'}, format='json')
        self.assertEqual(response.status_code, 200)


class MyTeamRoundsVisibilityTests(TestCase):
    def test_someone_without_a_team_does_not_see_rounds(self):
        hack = make_hackathon(min_team_size=1)
        RoundService.create_round(hack, make_user(role='ADMIN'), name='Secret', description='hidden plan')
        client = APIClient()
        client.force_authenticate(make_user())
        self.assertEqual(client.get(f'/api/hackathons/{hack.slug}/my-team/').data['rounds'], [])

    def test_team_members_still_see_rounds(self):
        hack = make_hackathon(min_team_size=1)
        RoundService.create_round(hack, make_user(role='ADMIN'), name='R1')
        leader = make_user()
        make_team(hack, leader)
        client = APIClient()
        client.force_authenticate(leader)
        # the team was created after the round, so it has no entry but still sees the round list
        self.assertEqual(len(client.get(f'/api/hackathons/{hack.slug}/my-team/').data['rounds']), 1)


class DraftHackathonTests(TestCase):
    def setUp(self):
        self.admin = make_user(role='ADMIN')
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        now = timezone.now()
        self.payload = {
            'title': 'Draft Hack', 'slug': 'draft-hack', 'theme': 't', 'description': 'Rules and details.',
            'start_date': (now + timezone.timedelta(days=5)).isoformat(),
            'end_date': (now + timezone.timedelta(days=6)).isoformat(),
        }

    def _create(self, **overrides):
        return self.client.post('/api/hackathons/', {**self.payload, **overrides}, format='json')

    def test_new_hackathons_start_as_drafts_that_are_not_open(self):
        response = self._create()
        self.assertEqual(response.data['status'], HackathonStatus.DRAFT)
        self.assertFalse(response.data['is_registration_open'])

    def test_drafts_are_invisible_to_the_public_but_not_to_admins(self):
        self._create()
        public = APIClient()
        self.assertEqual(public.get('/api/hackathons/draft-hack/').status_code, 404)
        self.assertNotIn('draft-hack', [h['slug'] for h in public.get('/api/hackathons/').data])
        self.assertEqual(self.client.get('/api/hackathons/draft-hack/').status_code, 200)

    def test_publishing_needs_a_description_and_a_problem_option(self):
        self._create(slug='empty-desc')
        Hackathon.objects.filter(slug='empty-desc').update(description='   ')
        response = self.client.post('/api/hackathons/empty-desc/publish/')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'PUBLISH_CHECKLIST')
        self.assertEqual(len(response.data['items']), 1)

    def test_publishing_without_statements_needs_open_innovation(self):
        hack = Hackathon.objects.get(slug=self._create().data['slug'])
        hack.allow_open_innovation = False
        hack.save()
        response = self.client.post('/api/hackathons/draft-hack/publish/')
        self.assertEqual(response.status_code, 400)
        make_ps(hack)
        self.assertEqual(self.client.post('/api/hackathons/draft-hack/publish/').status_code, 200)

    def test_publishing_makes_it_live_and_public(self):
        self._create()
        response = self.client.post('/api/hackathons/draft-hack/publish/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], HackathonStatus.LIVE)
        self.assertEqual(APIClient().get('/api/hackathons/draft-hack/').status_code, 200)

    def test_cannot_publish_twice_close_a_draft_or_reopen_a_draft(self):
        self._create()
        self.assertEqual(self.client.post('/api/hackathons/draft-hack/close/').status_code, 409)
        self.assertEqual(self.client.post('/api/hackathons/draft-hack/reopen/').status_code, 409)
        self.client.post('/api/hackathons/draft-hack/publish/')
        self.assertEqual(self.client.post('/api/hackathons/draft-hack/publish/').status_code, 409)

    def test_drafts_reject_team_registration(self):
        self._create()
        player = APIClient()
        player.force_authenticate(make_user())
        self.assertEqual(player.post('/api/hackathons/draft-hack/teams/', {'name': 'Early'}, format='json').status_code, 404)

    def test_close_and_reopen_still_work_for_live_hackathons(self):
        self._create()
        self.client.post('/api/hackathons/draft-hack/publish/')
        self.assertEqual(self.client.post('/api/hackathons/draft-hack/close/').data['status'], 'CLOSED')
        self.assertEqual(self.client.post('/api/hackathons/draft-hack/reopen/').data['status'], 'LIVE')
