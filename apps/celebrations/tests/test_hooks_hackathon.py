"""Where a hackathon raises celebrations: the right people, once, never for rejections, and taken back when the moment is undone."""
from django.core.cache import cache
from rest_framework.test import APIClient, APITestCase

from apps.celebrations import services
from apps.celebrations.models import Celebration
from apps.hackathons import payments
from apps.hackathons.models import (
    HackathonStaff, HackathonWinner, IdeaSubmission, PaymentStatus, Team, TeamMember, TeamPayment, TeamStatus,
)
from apps.hackathons.services import TeamService
from apps.hackathons.tests.helpers import make_hackathon, make_team, make_user


def api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


def kinds_of(user):
    return sorted(Celebration.objects.filter(user=user).values_list('kind', flat=True))


class HackathonCase(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = make_user(role='ADMIN')

    def run_commit(self):
        return self.captureOnCommitCallbacks(execute=True)

    def create_team(self, hack, leader, name='Alpha'):
        with self.run_commit():
            reply = api(leader).post(f'/api/hackathons/{hack.slug}/teams/', {'name': name}, format='json')
        self.assertEqual(reply.status_code, 201, reply.data)
        return Team.objects.get(pk=reply.data['id'])


class RegistrationTests(HackathonCase):
    def test_a_team_that_meets_its_minimum_at_once_celebrates_when_created(self):
        hack = make_hackathon(min_team_size=1)
        leader = make_user()
        team = self.create_team(hack, leader)
        card = Celebration.objects.get(user=leader)
        self.assertEqual((card.kind, card.size, card.key, card.link_url), ('hackathon.team_registered', 'BIG', f'team-registered:{team.pk}', f'/hackathons/{hack.slug}/dashboard'))
        self.assertIn(f'{team.name} is registered for {hack.title}', card.message)

    def test_a_forming_team_waits_and_everyone_celebrates_when_it_reaches_its_minimum(self):
        hack = make_hackathon(min_team_size=2)
        leader, mate = make_user(), make_user()
        team = self.create_team(hack, leader)
        self.assertEqual(Celebration.objects.count(), 0)                                 # still forming
        with self.run_commit():
            TeamService.invite(team, leader, mate.email)
        from apps.hackathons.models import TeamInvite

        invite = TeamInvite.objects.get(team=team, invited_user=mate)
        with self.run_commit():
            reply = api(mate).post(f'/api/hackathons/invites/{invite.pk}/accept/', {}, format='json')
        self.assertEqual(reply.status_code, 200, reply.data)
        self.assertEqual(kinds_of(mate), ['hackathon.joined_team', 'hackathon.team_registered'])
        self.assertEqual(kinds_of(leader), ['hackathon.member_joined', 'hackathon.team_registered'])
        said = Celebration.objects.get(user=leader, kind='hackathon.member_joined')
        self.assertEqual((said.size, said.key), ('SMALL', f'member-joined:{team.pk}:{mate.pk}'))
        self.assertIn(mate.first_name, said.title)
        self.assertIn(team.name, said.title)

    def test_the_team_registered_card_is_given_once_even_if_the_team_dips_and_recovers(self):
        hack = make_hackathon(min_team_size=2)
        leader, mate, later = make_user(), make_user(), make_user()
        team = make_team(hack, leader, mate, name='Pair', status=TeamStatus.FORMING)
        with self.run_commit():
            TeamService.recompute_statuses(hack)
        self.assertEqual(Celebration.objects.filter(kind='hackathon.team_registered').count(), 2)
        with self.run_commit():
            TeamMember.objects.filter(team=team, user=mate).delete()
            TeamService.recompute_statuses(hack)                                         # back to forming
            TeamMember.objects.create(team=team, hackathon=hack, user=later)
            TeamService.recompute_statuses(hack)                                         # registered again
        self.assertEqual(Celebration.objects.filter(kind='hackathon.team_registered', user=leader).count(), 1)

    def test_an_organizer_adding_a_member_welcomes_them_and_tells_the_team(self):
        hack = make_hackathon(min_team_size=1)
        leader, added = make_user(), make_user()
        team = self.create_team(hack, leader)
        with self.run_commit():
            reply = api(self.admin).post(f'/api/hackathons/teams/{team.pk}/admin-add-member/', {'email': added.email}, format='json')
        self.assertEqual(reply.status_code, 200, reply.data)
        self.assertEqual(kinds_of(added), ['hackathon.joined_team'])
        self.assertEqual(sorted(Celebration.objects.filter(user=leader).values_list('kind', flat=True)), ['hackathon.member_joined', 'hackathon.team_registered'])

    def test_a_team_on_the_waitlist_is_not_celebrated_until_a_place_opens(self):
        hack = make_hackathon(min_team_size=1, max_teams=1, waitlist_enabled=True)
        first, second = make_user(), make_user()
        placed = self.create_team(hack, first, 'First')
        queued = self.create_team(hack, second, 'Second')
        self.assertTrue(queued.is_waitlisted)
        self.assertEqual(kinds_of(second), [])                                           # queued: nothing to celebrate
        with self.run_commit():
            TeamService.admin_set_status(placed, self.admin, TeamStatus.WITHDRAWN, 'left')
        card = Celebration.objects.get(user=second)
        self.assertEqual((card.kind, card.size, card.key), ('hackathon.waitlist_promoted', 'BIG', f'waitlist-promoted:{queued.pk}'))

    def test_a_team_withdrawn_before_its_members_saw_the_card_loses_it(self):
        hack = make_hackathon(min_team_size=1)
        leader, other = make_user(), make_user()
        team = self.create_team(hack, leader)
        seen = Celebration.objects.get(user=leader)
        services.mark_seen(leader, [seen.pk])
        with self.run_commit():
            api(self.admin).post(f'/api/hackathons/teams/{team.pk}/admin-add-member/', {'email': other.email}, format='json')
        self.assertIn('hackathon.team_registered', kinds_of(leader))
        with self.run_commit():
            TeamService.admin_set_status(team, self.admin, TeamStatus.DISQUALIFIED, 'rules')
        self.assertTrue(Celebration.objects.filter(user=leader, kind='hackathon.team_registered').exists())      # already seen: stays
        self.assertFalse(Celebration.objects.filter(kind='hackathon.team_registered', seen_at__isnull=True).exists())

    def test_an_organizer_setting_a_registered_team_to_registered_again_does_not_celebrate_twice(self):
        hack = make_hackathon(min_team_size=1)
        leader = make_user()
        team = self.create_team(hack, leader)
        self.assertEqual(Celebration.objects.filter(user=leader).count(), 1)
        with self.run_commit():
            TeamService.admin_set_status(team, self.admin, TeamStatus.REGISTERED)
        self.assertEqual(Celebration.objects.filter(user=leader).count(), 1)


class PaymentTests(HackathonCase):
    def pending(self, hack, leader, name='Pay Team'):
        team = Team.objects.create(hackathon=hack, name=name, leader=leader, status=TeamStatus.PAYMENT_PENDING)
        TeamMember.objects.create(team=team, hackathon=hack, user=leader, role='LEADER')
        payment = TeamPayment.objects.create(team=team, hackathon=hack, amount=200, transaction_id=f'T-{name}', team_name=name)
        return team, payment

    def test_verifying_a_payment_celebrates_the_team_that_is_now_registered(self):
        hack = make_hackathon(min_team_size=1, payment_stage='REGISTRATION', idea_fee_amount=200)
        leader = make_user()
        team, payment = self.pending(hack, leader)
        self.assertEqual(Celebration.objects.count(), 0)                                 # paying alone celebrates nothing yet
        with self.run_commit():
            payments.verify(hack, self.admin, payment.pk)
        self.assertEqual(kinds_of(leader), ['hackathon.team_registered'])

    def test_a_verified_payment_on_a_full_hackathon_is_not_celebrated_but_a_rejected_one_never_is(self):
        hack = make_hackathon(min_team_size=1, max_teams=1, waitlist_enabled=True, payment_stage='REGISTRATION', idea_fee_amount=200)
        make_team(hack, make_user(), name='Placed')
        waiting_leader, rejected_leader = make_user(), make_user()
        team, payment = self.pending(hack, waiting_leader, 'Waiting')
        _, other = self.pending(hack, rejected_leader, 'Rejected')
        with self.run_commit():
            payments.verify(hack, self.admin, payment.pk)                                # full: waitlisted
            payments.reject(hack, self.admin, other.pk, 'blurry')
        self.assertEqual(Celebration.objects.count(), 0)
        self.assertEqual(TeamPayment.objects.get(pk=other.pk).status, PaymentStatus.REJECTED)


class ProgressTests(HackathonCase):
    def setUp(self):
        super().setUp()
        self.hack = make_hackathon(min_team_size=1, features={'ideas': True, 'submissions': True, 'certificates': True, 'check_in': True})
        self.leader, self.mate = make_user(), make_user()
        self.team = make_team(self.hack, self.leader, self.mate, name='Builders')

    def test_an_approved_idea_celebrates_the_team_and_a_rejected_one_does_not(self):
        from apps.hackathons.ideas import IdeaService

        idea = IdeaSubmission.objects.create(team=self.team, title='Bins', problem='p' * 30, solution='s' * 30)
        with self.run_commit():
            IdeaService.review(idea, self.admin, 'REJECTED', 'not this time')
        self.assertEqual(Celebration.objects.count(), 0)
        with self.run_commit():
            IdeaService.review(idea, self.admin, 'APPROVED')
        for person in (self.leader, self.mate):
            self.assertEqual(kinds_of(person), ['hackathon.idea_approved'])

    def test_submitting_the_project_celebrates_the_whole_team_once(self):
        url = f'/api/hackathons/{self.hack.slug}/my-submission/'
        body = {'project_title': 'Robo', 'description': 'A small robot that moves along a line and carries parts around the lab.', 'repo_url': 'https://github.com/x/robo'}
        with self.run_commit():
            self.assertEqual(api(self.leader).put(url, body, format='json').status_code, 200)
        with self.run_commit():
            self.assertEqual(api(self.leader).put(url, {**body, 'project_title': 'Robo 2'}, format='json').status_code, 200)   # an edit
        for person in (self.leader, self.mate):
            self.assertEqual(kinds_of(person), ['hackathon.project_submitted'])

    def test_shortlisting_celebrates_only_after_results_are_published_and_only_the_shortlisted(self):
        rejected = make_team(self.hack, make_user(), make_user(), name='Others')
        rounds = f'/api/hackathons/{self.hack.slug}/rounds/'
        admin = api(self.admin)
        round_id = admin.post(rounds, {'name': 'Round 1'}, format='json').data['id']
        with self.run_commit():
            admin.post(f'{rounds}{round_id}/decide/', {'team_ids': [self.team.pk], 'status': 'SHORTLISTED'}, format='json')
            admin.post(f'{rounds}{round_id}/decide/', {'team_ids': [rejected.pk], 'status': 'REJECTED'}, format='json')
        self.assertEqual(Celebration.objects.count(), 0)                                 # not published: the teams cannot see it
        with self.run_commit():
            self.assertEqual(admin.post(f'{rounds}{round_id}/publish/', {}, format='json').status_code, 200)
        for person in (self.leader, self.mate):
            card = Celebration.objects.get(user=person)
            self.assertEqual((card.kind, card.size), ('hackathon.shortlisted', 'BIG'))
            self.assertIn('Round 1', card.message)
        self.assertEqual(Celebration.objects.filter(user=rejected.leader).count(), 0)    # a rejection is never celebrated

    def test_changing_a_published_result_celebrates_or_takes_the_card_back(self):
        rounds = f'/api/hackathons/{self.hack.slug}/rounds/'
        admin = api(self.admin)
        round_id = admin.post(rounds, {'name': 'Round 1'}, format='json').data['id']
        admin.post(f'{rounds}{round_id}/publish/', {}, format='json')
        with self.run_commit():
            admin.post(f'{rounds}{round_id}/decide/', {'team_ids': [self.team.pk], 'status': 'SHORTLISTED', 'confirm_published': True}, format='json')
        self.assertEqual(kinds_of(self.leader), ['hackathon.shortlisted'])
        with self.run_commit():
            admin.post(f'{rounds}{round_id}/decide/', {'team_ids': [self.team.pk], 'status': 'REJECTED', 'confirm_published': True}, format='json')
        self.assertEqual(kinds_of(self.leader), [])                                      # changed their mind before it was seen

    def test_unpublishing_takes_back_shortlist_cards_nobody_has_seen(self):
        rounds = f'/api/hackathons/{self.hack.slug}/rounds/'
        admin = api(self.admin)
        round_id = admin.post(rounds, {'name': 'Round 1'}, format='json').data['id']
        admin.post(f'{rounds}{round_id}/decide/', {'team_ids': [self.team.pk], 'status': 'SHORTLISTED'}, format='json')
        with self.run_commit():
            admin.post(f'{rounds}{round_id}/publish/', {}, format='json')
        seen = Celebration.objects.get(user=self.mate)
        services.mark_seen(self.mate, [seen.pk])
        with self.run_commit():
            admin.post(f'{rounds}{round_id}/unpublish/', {}, format='json')
        self.assertEqual(kinds_of(self.leader), [])
        self.assertEqual(kinds_of(self.mate), ['hackathon.shortlisted'])                 # this one was already seen

    def test_publishing_results_celebrates_each_winning_team_with_its_award(self):
        other = make_team(self.hack, make_user(), name='Runners')
        admin = api(self.admin)
        base = f'/api/hackathons/{self.hack.slug}'
        admin.put(f'{base}/winners/', {'winners': [{'team_id': self.team.pk, 'title': 'Winner', 'rank': 1}, {'team_id': other.pk, 'title': 'Runner-up', 'rank': 2}]}, format='json')
        self.assertEqual(Celebration.objects.count(), 0)                                 # chosen but not announced
        with self.run_commit():
            self.assertEqual(admin.post(f'{base}/results/publish/').status_code, 200)
        card = Celebration.objects.get(user=self.leader)
        self.assertEqual((card.kind, card.size), ('hackathon.winner', 'BIG'))
        self.assertIn('Winner', card.message)
        self.assertIn('Runner-up', Celebration.objects.get(user=other.leader).message)
        with self.run_commit():
            admin.post(f'{base}/results/unpublish/')
        self.assertEqual(Celebration.objects.filter(kind='hackathon.winner').count(), 0)

    def test_releasing_certificates_celebrates_those_who_earned_one_and_only_once(self):
        forming = make_team(self.hack, make_user(), name='Late', status=TeamStatus.FORMING)
        admin = api(self.admin)
        url = f'/api/hackathons/{self.hack.slug}/'
        with self.run_commit():
            reply = admin.patch(url, {'certificate_config': {'released': True}}, format='json')
        self.assertEqual(reply.status_code, 200, reply.data)
        for person in (self.leader, self.mate):
            self.assertEqual(kinds_of(person), ['hackathon.certificate_ready'])
        self.assertEqual(kinds_of(forming.leader), [])                                   # not registered: no certificate
        with self.run_commit():
            admin.patch(url, {'certificate_config': {'released': False}}, format='json')
            admin.patch(url, {'certificate_config': {'released': True}}, format='json')   # released again
        self.assertEqual(Celebration.objects.filter(user=self.leader, kind='hackathon.certificate_ready').count(), 1)

    def test_arriving_at_the_venue_celebrates_once_for_the_main_check_in(self):
        volunteer = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=volunteer, role='VOLUNTEER')
        base = f'/api/hackathons/{self.hack.slug}/check-in/'
        with self.run_commit():
            self.assertEqual(api(volunteer).post(base, {'user_id': self.leader.pk}, format='json').status_code, 200)
            api(volunteer).post(base, {'user_id': self.leader.pk}, format='json')        # scanned twice
        self.assertEqual(kinds_of(self.leader), ['hackathon.checked_in'])
        self.assertEqual(kinds_of(self.mate), [])

    def test_a_later_session_check_in_is_not_celebrated(self):
        from apps.hackathons import check_in_sessions

        self.hack.check_in_days = 2
        self.hack.save()
        check_in_sessions.sync(self.hack)
        day2 = self.hack.check_in_sessions.get(day_index=1)
        volunteer = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=volunteer, role='VOLUNTEER')
        base = f'/api/hackathons/{self.hack.slug}/check-in/'
        api(volunteer).post(base, {'user_id': self.leader.pk}, format='json')            # arrival (not committed here)
        Celebration.objects.all().delete()
        with self.run_commit():
            self.assertEqual(api(volunteer).post(base, {'user_id': self.leader.pk, 'session': day2.pk}, format='json').status_code, 200)
        self.assertEqual(kinds_of(self.leader), [])
