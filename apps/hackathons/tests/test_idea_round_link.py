"""Idea review linked to a round: reviews set the round's results, and final outcomes wait for the round to be published."""
from django.core.cache import cache
from rest_framework.test import APIClient, APITestCase

from apps.hackathons.models import EntryStatus, Hackathon, IdeaStatus, IdeaSubmission, Round, RoundEntry
from apps.notifications.models import Notification
from .helpers import make_hackathon, make_team, make_user


def api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


class IdeaRoundCase(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.hack = make_hackathon(min_team_size=1, features={'ideas': True, 'submissions': True})
        Hackathon.objects.filter(pk=self.hack.pk).update(project_requires_approved_idea=True)
        self.hack.refresh_from_db()
        self.admin = make_user(role='ADMIN')
        self.teams = [make_team(self.hack, make_user(), name=name) for name in ('Alpha', 'Beta', 'Gamma')]
        self.ideas = [
            IdeaSubmission.objects.create(team=team, title=f'{team.name} idea', problem='p' * 30, solution='s' * 30, plan='w')
            for team in self.teams
        ]
        self.round = Round.objects.create(hackathon=self.hack, order=1, name='Idea round', kind='IDEA_REVIEW')
        self.base = f'/api/hackathons/{self.hack.slug}'

    def link(self, round_id='self', **extra):
        reply = api(self.admin).post(f'{self.base}/ideas/link-round/', {'round': self.round.id if round_id == 'self' else round_id, **extra}, format='json')
        return reply

    def review(self, idea, status, feedback='Needs more detail.', **extra):
        return api(self.admin).post(f'{self.base}/ideas/{idea.id}/review/', {'status': status, 'feedback': feedback, **extra}, format='json')

    def entry(self, team):
        return RoundEntry.objects.filter(round=self.round, team=team).values_list('status', flat=True).first()

    def team_idea(self, team):
        return api(team.leader).get(f'{self.base}/my-idea/').data['idea']

    def publish(self):
        with self.captureOnCommitCallbacks(execute=True):
            reply = api(self.admin).post(f'{self.base}/rounds/{self.round.id}/publish/', {}, format='json')
        self.assertEqual(reply.status_code, 200, reply.data)


class Reviews(IdeaRoundCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.link().status_code, 200)

    def test_each_outcome_sets_the_team_result_in_the_round(self):
        alpha, beta, gamma = self.ideas
        self.review(alpha, 'APPROVED', '')
        self.review(beta, 'REJECTED')
        self.review(gamma, 'CHANGES_REQUESTED')
        self.assertEqual([self.entry(t) for t in self.teams], [EntryStatus.SHORTLISTED, EntryStatus.REJECTED, EntryStatus.PENDING])
        self.assertTrue(api(self.admin).get(f'{self.base}/rounds/').data[0]['linked_to_ideas'])

    def test_a_final_outcome_is_held_from_the_team_until_the_round_is_published(self):
        alpha = self.ideas[0]
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.review(alpha, 'APPROVED', '')
        self.assertTrue(reply.data['result_held'])
        seen = self.team_idea(self.teams[0])
        self.assertEqual((seen['status'], seen['result_held']), ('SUBMITTED', True))
        self.assertFalse(Notification.objects.filter(recipient=self.teams[0].leader, title__contains='idea reviewed').exists())
        gate = lambda: api(self.teams[0].leader).get(f'{self.base}/my-submission/').data['gate']['satisfied']  # noqa: E731
        self.assertFalse(gate())  # the project phase stays shut while the approval is held
        self.publish()
        seen = self.team_idea(self.teams[0])
        self.assertEqual((seen['status'], seen['result_held']), ('APPROVED', False))
        self.assertTrue(gate())

    def test_a_held_result_does_not_show_through_the_edit_message(self):
        self.review(self.ideas[1], 'REJECTED')
        data = api(self.teams[1].leader).get(f'{self.base}/my-idea/').data
        self.assertNotIn('decided', data['edit_blocked_reason'])
        self.assertIn('round results are published', data['edit_blocked_reason'])

    def test_the_organizers_always_see_the_real_status(self):
        self.review(self.ideas[1], 'REJECTED')
        row = next(r for r in api(self.admin).get(f'{self.base}/ideas/').data['rows'] if r['id'] == self.ideas[1].id)
        self.assertEqual(row['status'], 'REJECTED')

    def test_changes_requested_reaches_the_team_at_once(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.review(self.ideas[2], 'CHANGES_REQUESTED', 'Add a plan.')
        seen = self.team_idea(self.teams[2])
        self.assertEqual((seen['status'], seen['feedback']), ('CHANGES_REQUESTED', 'Add a plan.'))
        self.assertTrue(Notification.objects.filter(recipient=self.teams[2].leader, title__contains='idea reviewed').exists())

    def test_a_resubmitted_idea_goes_back_to_pending(self):
        self.review(self.ideas[2], 'CHANGES_REQUESTED', 'Add a plan.')
        reply = api(self.teams[2].leader).put(f'{self.base}/my-idea/', {
            'title': 'Gamma idea v2', 'problem': 'p' * 30, 'solution': 's' * 30, 'plan': 'week one',
        }, format='json')
        self.assertEqual(reply.status_code, 200, reply.data)
        self.assertEqual(self.entry(self.teams[2]), EntryStatus.PENDING)

    def test_reviewing_after_the_round_is_published_asks_for_confirmation(self):
        self.publish()
        reply = self.review(self.ideas[0], 'APPROVED', '')
        self.assertEqual((reply.status_code, reply.data['code']), (409, 'RESULTS_PUBLISHED'))
        self.assertEqual(IdeaSubmission.objects.get(pk=self.ideas[0].pk).status, IdeaStatus.SUBMITTED)  # nothing changed
        reply = self.review(self.ideas[0], 'APPROVED', '', confirm_published=True)
        self.assertEqual((reply.status_code, reply.data['result_held']), (200, False))
        self.assertEqual(self.entry(self.teams[0]), EntryStatus.SHORTLISTED)

    def test_the_round_target_is_respected(self):
        Round.objects.filter(pk=self.round.pk).update(shortlist_limit=1)
        self.review(self.ideas[0], 'APPROVED', '')
        reply = self.review(self.ideas[1], 'APPROVED', '')
        self.assertEqual((reply.status_code, reply.data['code']), (409, 'SHORTLIST_LIMIT'))
        self.assertEqual(self.review(self.ideas[1], 'APPROVED', '', confirm_over_limit=True).status_code, 200)

    def test_without_review_a_submitted_idea_is_shortlisted(self):
        Hackathon.objects.filter(pk=self.hack.pk).update(idea_review_required=False)
        newcomer = make_team(self.hack, make_user(), name='Delta')
        reply = api(newcomer.leader).put(f'{self.base}/my-idea/', {
            'title': 'Delta idea', 'problem': 'p' * 30, 'solution': 's' * 30, 'plan': 'week one',
        }, format='json')
        self.assertEqual(reply.status_code, 200, reply.data)
        self.assertEqual(self.entry(newcomer), EntryStatus.SHORTLISTED)  # added to the round, then decided


class Linking(IdeaRoundCase):
    def setUp(self):
        super().setUp()
        IdeaSubmission.objects.filter(pk=self.ideas[0].pk).update(status=IdeaStatus.APPROVED)
        IdeaSubmission.objects.filter(pk=self.ideas[1].pk).update(status=IdeaStatus.REJECTED)

    def test_the_preview_changes_nothing(self):
        reply = self.link(preview=True)
        self.assertEqual((reply.data['shortlisted'], reply.data['rejected']), (1, 1))
        self.hack.refresh_from_db()
        self.assertIsNone(self.hack.idea_round_id)

    def test_linking_copies_the_ideas_already_reviewed(self):
        self.assertEqual(self.link().status_code, 200)
        self.assertEqual([self.entry(t) for t in self.teams], [EntryStatus.SHORTLISTED, EntryStatus.REJECTED, None])

    def test_copying_into_a_published_round_needs_confirmation(self):
        Round.objects.filter(pk=self.round.pk).update(results_published=True)
        reply = self.link()
        self.assertEqual((reply.status_code, reply.data['code']), (409, 'RESULTS_PUBLISHED'))
        self.hack.refresh_from_db()
        self.assertIsNone(self.hack.idea_round_id)  # the whole link was undone
        self.assertEqual(self.link(confirm_published=True).status_code, 200)

    def test_unlinking_stops_reviews_from_touching_the_round(self):
        self.link()
        self.assertEqual(self.link(round_id=None).status_code, 200)
        self.review(self.ideas[2], 'APPROVED', '')
        self.assertIsNone(self.entry(self.teams[2]))
        self.assertEqual(self.team_idea(self.teams[2])['status'], 'APPROVED')  # no link: shown at once, as before

    def test_only_a_round_of_this_hackathon(self):
        other = Round.objects.create(hackathon=make_hackathon(), order=1, name='Elsewhere')
        self.assertEqual(self.link(round_id=other.id).data['code'], 'ROUND_INVALID')

    def test_people_without_rounds_and_ideas_permissions_cannot_link(self):
        from apps.hackathons.models import HackathonStaff
        content_only = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=content_only, role='ORGANIZER', permissions=['ideas'])
        reply = api(content_only).post(f'{self.base}/ideas/link-round/', {'round': self.round.id}, format='json')
        self.assertEqual(reply.status_code, 403)


class NoLink(IdeaRoundCase):
    def test_without_a_link_reviews_behave_as_before(self):
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.review(self.ideas[0], 'APPROVED', '')
        self.assertFalse(reply.data['result_held'])
        self.assertIsNone(self.entry(self.teams[0]))
        self.assertEqual(self.team_idea(self.teams[0])['status'], 'APPROVED')
        self.assertTrue(Notification.objects.filter(recipient=self.teams[0].leader, title__contains='idea reviewed').exists())


class HoldMemory(IdeaRoundCase):
    """Only outcomes decided while held wait; what a team was already told is never taken back."""

    def test_linking_later_does_not_hide_results_teams_were_already_told(self):
        self.review(self.ideas[0], 'APPROVED', '')          # no link yet: the team is told at once
        self.assertEqual(self.link().status_code, 200)
        seen = self.team_idea(self.teams[0])
        self.assertEqual((seen['status'], seen['result_held']), ('APPROVED', False))
        self.assertTrue(api(self.teams[0].leader).get(f'{self.base}/my-submission/').data['gate']['satisfied'])

    def test_unpublishing_after_the_reveal_does_not_hide_them_again(self):
        self.link()
        self.review(self.ideas[0], 'APPROVED', '')
        self.publish()
        api(self.admin).post(f'{self.base}/rounds/{self.round.id}/unpublish/', {}, format='json')
        self.assertEqual(self.team_idea(self.teams[0])['status'], 'APPROVED')

    def test_publishing_tells_held_teams_their_result_with_the_feedback(self):
        self.link()
        self.review(self.ideas[1], 'REJECTED', 'Too broad for the time we have.')
        self.assertFalse(Notification.objects.filter(recipient=self.teams[1].leader, title__contains='idea reviewed').exists())
        self.publish()
        note = Notification.objects.get(recipient=self.teams[1].leader, title__contains='idea reviewed')
        self.assertIn('Too broad', note.message)

    def test_unlinking_releases_held_results_with_their_notification(self):
        self.link()
        with self.captureOnCommitCallbacks(execute=True):
            self.review(self.ideas[0], 'APPROVED', '')
            self.link(round_id=None)
        self.assertEqual(self.team_idea(self.teams[0])['status'], 'APPROVED')
        self.assertTrue(Notification.objects.filter(recipient=self.teams[0].leader, title__contains='idea reviewed').exists())

    def test_editing_a_waiting_idea_keeps_a_decision_made_by_hand_in_the_round(self):
        self.link()
        RoundEntry.objects.create(round=self.round, team=self.teams[2])
        api(self.admin).post(f'{self.base}/rounds/{self.round.id}/decide/', {'team_ids': [self.teams[2].id], 'status': 'SHORTLISTED'}, format='json')
        reply = api(self.teams[2].leader).put(f'{self.base}/my-idea/', {
            'title': 'Gamma idea, clearer', 'problem': 'p' * 30, 'solution': 's' * 30, 'plan': 'week one',
        }, format='json')
        self.assertEqual(reply.status_code, 200, reply.data)
        self.assertEqual(self.entry(self.teams[2]), EntryStatus.SHORTLISTED)

    def test_linking_copies_only_teams_still_competing(self):
        from apps.hackathons.models import Team, TeamStatus
        IdeaSubmission.objects.filter(pk=self.ideas[0].pk).update(status=IdeaStatus.APPROVED)
        IdeaSubmission.objects.filter(pk=self.ideas[1].pk).update(status=IdeaStatus.APPROVED)
        Team.objects.filter(pk=self.teams[1].pk).update(status=TeamStatus.WITHDRAWN)
        self.assertEqual(self.link(preview=True).data['shortlisted'], 1)
