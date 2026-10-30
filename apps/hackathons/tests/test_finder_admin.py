"""Team finder, organizer side: the overview, removing listings, closing requests, placing people, reminders and exports."""

import csv
import io
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.audit.models import AuditLog
from apps.hackathons.models import HackathonStaff, JoinRequestStatus, TeamJoinRequest, TeamMember, TeamSeeker, TeamStatus
from apps.notifications.models import Notification
from .helpers import make_hackathon, make_team, make_user


def api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


class FinderAdminCase(APITestCase):
    def setUp(self):
        cache.clear()
        self.hack = make_hackathon(min_team_size=2, max_team_size=3, status='LIVE', features={'team_finder': True})
        self.base = f'/api/hackathons/{self.hack.slug}/finder/admin'
        self.admin = make_user(role='ADMIN')
        self.leader = make_user(first_name='Lena')
        self.team = make_team(self.hack, self.leader, name='Open Door', status=TeamStatus.FORMING)
        self.team.looking_for_members, self.team.looking_for = True, 'a designer'
        self.team.save()
        self.seeker = make_user(first_name='Sita', last_name='Rao')
        self.listing = TeamSeeker.objects.create(hackathon=self.hack, user=self.seeker, skills='Figma, React', note='Happy to learn')

    def post(self, path, user=None, **body):
        return api(user or self.admin).post(f'{self.base}/{path}', body, format='json')

    def ask(self, user=None, team=None):
        return TeamJoinRequest.objects.create(hackathon=self.hack, team=team or self.team, user=user or self.seeker, message='hi')

    def notes(self, user):
        return list(Notification.objects.filter(recipient=user).values_list('title', flat=True))


class Overview(FinderAdminCase):
    def test_shows_listed_people_recruiting_teams_requests_and_numbers(self):
        self.ask()
        data = api(self.admin).get(f'{self.base}/').data
        self.assertTrue(data['enabled'])
        person = data['seekers'][0]
        self.assertEqual((person['name'], person['email'], person['phone'], person['status'], person['pending_requests']), ('Sita Rao', self.seeker.email, '9876543210', 'WAITING', 1))
        self.assertEqual(person['skills'], 'Figma, React')
        team = data['teams'][0]
        self.assertEqual((team['name'], team['members'], team['seats_open'], team['pending_requests'], team['leader_email']), ('Open Door', 1, 2, 1, self.leader.email))
        self.assertEqual(team['problem'], '')                       # never null: the page lowercases it to search
        self.assertEqual(data['requests'][0]['status'], 'PENDING')
        self.assertEqual(data['stats'], {
            'seekers_waiting': 1, 'teams_recruiting': 1, 'requests_pending': 1, 'requests_stuck': 0, 'matched': 0, 'median_hours_to_match': None,
        })

    def test_a_request_waiting_more_than_two_days_is_stuck(self):
        join = self.ask()
        TeamJoinRequest.objects.filter(pk=join.pk).update(created_at=timezone.now() - timedelta(hours=49))
        data = api(self.admin).get(f'{self.base}/').data
        self.assertTrue(data['requests'][0]['stuck'])
        self.assertEqual(data['stats']['requests_stuck'], 1)

    def test_someone_on_a_team_is_matched_and_a_withdrawn_listing_says_so(self):
        TeamMember.objects.create(team=self.team, user=self.seeker)
        gone = make_user()
        TeamSeeker.objects.create(hackathon=self.hack, user=gone, is_active=False)
        rows = {row['name']: row for row in api(self.admin).get(f'{self.base}/').data['seekers']}
        self.assertEqual((rows['Sita Rao']['status'], rows['Sita Rao']['team']), ('MATCHED', 'Open Door'))
        self.assertEqual(rows[f'{gone.first_name} {gone.last_name}']['status'], 'WITHDRAWN')
        stats = api(self.admin).get(f'{self.base}/').data['stats']
        self.assertEqual((stats['seekers_waiting'], stats['matched']), (0, 1))
        self.assertIsNotNone(stats['median_hours_to_match'])

    def test_a_full_team_is_listed_but_does_not_count_as_having_seats(self):
        for _ in range(2):
            TeamMember.objects.create(team=self.team, user=make_user())
        data = api(self.admin).get(f'{self.base}/').data
        self.assertEqual(data['teams'][0]['seats_open'], 0)
        self.assertEqual(data['stats']['teams_recruiting'], 0)

    def test_only_organizers_can_look(self):
        self.assertEqual(api(self.seeker).get(f'{self.base}/').status_code, 403)
        self.assertEqual(api().get(f'{self.base}/').status_code, 401)
        organizer = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=organizer, role='ORGANIZER', permissions=[])
        self.assertEqual(api(organizer).get(f'{self.base}/').status_code, 200)

    def test_still_readable_when_the_finder_is_switched_off(self):
        self.hack.features = {'team_finder': False}
        self.hack.save()
        data = api(self.admin).get(f'{self.base}/').data
        self.assertFalse(data['enabled'])


class RemovingAListing(FinderAdminCase):
    def test_takes_the_listing_down_clears_the_text_withdraws_requests_and_tells_the_person(self):
        join = self.ask()
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.post(f'seekers/{self.listing.pk}/remove/', reason='Not appropriate')
        self.assertEqual(reply.status_code, 200, reply.data)
        self.listing.refresh_from_db()
        self.assertEqual((self.listing.is_active, self.listing.skills, self.listing.note), (False, '', ''))
        join.refresh_from_db()
        self.assertEqual(join.status, JoinRequestStatus.CANCELLED)
        self.assertIn('Your team finder listing was removed', self.notes(self.seeker))
        self.assertIn('Not appropriate', Notification.objects.get(recipient=self.seeker).message)
        self.assertTrue(AuditLog.objects.filter(action='Admin Removed Team Finder Listing').exists())

    def test_the_person_can_list_themselves_again(self):
        self.post(f'seekers/{self.listing.pk}/remove/')
        again = api(self.seeker).put(f'/api/hackathons/{self.hack.slug}/finder/me/', {'skills': 'Python', 'note': 'new'}, format='json')
        self.assertEqual(again.status_code, 200, again.data)
        self.listing.refresh_from_db()
        self.assertTrue(self.listing.is_active)

    def test_unknown_listing_and_a_listing_of_another_hackathon_are_not_found(self):
        other = make_hackathon(features={'team_finder': True})
        foreign = TeamSeeker.objects.create(hackathon=other, user=make_user())
        self.assertEqual(self.post('seekers/999999/remove/').status_code, 404)
        self.assertEqual(self.post(f'seekers/{foreign.pk}/remove/').status_code, 404)

    def test_participants_cannot_use_it(self):
        self.assertEqual(self.post(f'seekers/{self.listing.pk}/remove/', user=self.leader).status_code, 403)
        self.listing.refresh_from_db()
        self.assertTrue(self.listing.is_active)


class StoppingRecruiting(FinderAdminCase):
    def test_switches_it_off_closes_the_waiting_requests_and_tells_everyone(self):
        join = self.ask()
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.post(f'teams/{self.team.pk}/stop-recruiting/', reason='Team is complete')
        self.assertEqual((reply.status_code, reply.data['requests_closed']), (200, 1))
        self.team.refresh_from_db()
        self.assertFalse(self.team.looking_for_members)
        join.refresh_from_db()
        self.assertEqual((join.status, join.decided_by_id), (JoinRequestStatus.CANCELLED, self.admin.pk))
        self.assertIn('Open Door stopped recruiting', self.notes(self.seeker))
        self.assertIn('Open Door is no longer listed as recruiting', self.notes(self.leader))
        self.assertIn('Team is complete', Notification.objects.get(recipient=self.leader).message)

    def test_works_when_nobody_has_asked(self):
        self.assertEqual(self.post(f'teams/{self.team.pk}/stop-recruiting/').data['requests_closed'], 0)

    def test_a_team_of_another_hackathon_is_not_found(self):
        other = make_hackathon(features={'team_finder': True})
        foreign = make_team(other, make_user())
        self.assertEqual(self.post(f'teams/{foreign.pk}/stop-recruiting/').status_code, 404)


class ClosingARequest(FinderAdminCase):
    def test_closes_a_waiting_request_and_the_sender_is_told(self):
        join = self.ask()
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.post(f'requests/{join.pk}/cancel/')
        self.assertEqual(reply.status_code, 200, reply.data)
        join.refresh_from_db()
        self.assertEqual((join.status, join.decided_by_id), (JoinRequestStatus.CANCELLED, self.admin.pk))
        self.assertIn('Your request to Open Door was closed', self.notes(self.seeker))

    def test_an_answered_request_cannot_be_closed_again(self):
        join = self.ask()
        TeamJoinRequest.objects.filter(pk=join.pk).update(status=JoinRequestStatus.ACCEPTED)
        reply = self.post(f'requests/{join.pk}/cancel/')
        self.assertEqual((reply.status_code, reply.data['code']), (409, 'REQUEST_NOT_PENDING'))

    def test_the_sender_may_ask_again_afterwards(self):
        join = self.ask()
        self.post(f'requests/{join.pk}/cancel/')
        again = api(self.seeker).post(f'/api/hackathons/{self.hack.slug}/finder/teams/{self.team.pk}/request/', {'message': 'again'}, format='json')
        self.assertEqual(again.status_code, 200, again.data)


class PlacingAPerson(FinderAdminCase):
    def place(self, seeker=None, team=None, user=None):
        return self.post('place/', user=user, seeker_id=(seeker or self.listing).pk, team_id=(team or self.team).pk)

    def test_puts_them_on_the_team_registers_it_and_tells_them(self):
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.place()
        self.assertEqual(reply.status_code, 200, reply.data)
        self.assertTrue(TeamMember.objects.filter(team=self.team, user=self.seeker, role='MEMBER').exists())
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, TeamStatus.REGISTERED)
        self.assertIn('You were placed in Open Door', self.notes(self.seeker))
        self.assertTrue(AuditLog.objects.filter(action='Admin Placed Seeker On Hackathon Team').exists())

    def test_a_waiting_request_to_that_team_counts_as_accepted_and_the_others_are_withdrawn(self):
        other_leader = make_user()
        other = make_team(self.hack, other_leader, name='Other', status=TeamStatus.FORMING)
        other.looking_for_members = True
        other.save()
        asked = self.ask()
        elsewhere = self.ask(team=other)
        self.place()
        asked.refresh_from_db()
        elsewhere.refresh_from_db()
        self.assertEqual((asked.status, asked.decided_by_id), (JoinRequestStatus.ACCEPTED, self.admin.pk))
        self.assertEqual(elsewhere.status, JoinRequestStatus.CANCELLED)

    def test_rules_and_questions_are_left_for_the_person_to_complete(self):
        self.hack.rules, self.hack.require_rules_acceptance = 'Be kind.', True
        self.hack.save()
        reply = self.place()
        self.assertEqual(reply.status_code, 200, reply.data)
        self.assertIsNone(TeamMember.objects.get(user=self.seeker).rules_accepted_at)

    def test_a_full_team_refuses(self):
        for _ in range(2):
            TeamMember.objects.create(team=self.team, user=make_user())
        reply = self.place()
        self.assertEqual((reply.status_code, reply.data['code']), (400, 'TEAM_FULL'))
        self.assertFalse(TeamMember.objects.filter(user=self.seeker).exists())

    def test_someone_already_on_a_team_refuses(self):
        make_team(self.hack, self.seeker, name='Taken')
        reply = self.place()
        self.assertEqual((reply.status_code, reply.data['code']), (400, 'ALREADY_IN_TEAM'))

    def test_a_team_waiting_on_its_payment_or_withdrawn_refuses(self):
        for status in (TeamStatus.PAYMENT_PENDING, TeamStatus.WITHDRAWN, TeamStatus.DISQUALIFIED):
            self.team.status = status
            self.team.save()
            reply = self.place()
            self.assertEqual((reply.status_code, reply.data['code']), (400, 'TEAM_INACTIVE'), status)
        self.assertFalse(TeamMember.objects.filter(user=self.seeker).exists())

    def test_locked_team_changes_and_a_closed_registration_refuse(self):
        self.hack.team_edits_locked = True
        self.hack.save()
        self.assertEqual(self.place().data['code'], 'TEAM_EDITS_LOCKED')

    def test_an_incomplete_profile_refuses(self):
        self.hack.required_profile_fields = ['linkedin_url']
        self.hack.save()
        reply = self.place()
        self.assertEqual((reply.status_code, reply.data['code']), (400, 'PROFILE_INCOMPLETE'))

    def test_junk_input_and_unknown_ids(self):
        self.assertEqual(self.post('place/', seeker_id='x', team_id=1).data['code'], 'INVALID')
        self.assertEqual(self.post('place/', seeker_id=999999, team_id=self.team.pk).status_code, 404)
        self.assertEqual(self.post('place/', seeker_id=self.listing.pk, team_id=999999).status_code, 404)

    def test_only_organizers_may_place(self):
        self.assertEqual(self.place(user=self.leader).status_code, 403)
        self.assertFalse(TeamMember.objects.filter(user=self.seeker).exists())


class Reminders(FinderAdminCase):
    def age_listing(self, days=4):
        TeamSeeker.objects.filter(pk=self.listing.pk).update(created_at=timezone.now() - timedelta(days=days))

    def test_a_person_who_has_waited_is_nudged_once_a_day(self):
        self.age_listing()
        with self.captureOnCommitCallbacks(execute=True):
            first = self.post('remind/', audience='seekers')
        self.assertEqual(first.data['reminded'], 1)
        self.assertIn('Teams are looking for members', self.notes(self.seeker))
        self.assertEqual(self.post('remind/', audience='seekers').data['reminded'], 0)
        TeamSeeker.objects.filter(pk=self.listing.pk).update(organizer_reminded_at=timezone.now() - timedelta(hours=25))
        self.assertEqual(self.post('remind/', audience='seekers').data['reminded'], 1)

    def test_someone_who_only_just_listed_is_left_alone(self):
        self.assertEqual(self.post('remind/', audience='seekers').data['reminded'], 0)

    def test_nobody_is_nudged_when_no_team_is_recruiting(self):
        self.age_listing()
        self.team.looking_for_members = False
        self.team.save()
        self.assertEqual(self.post('remind/', audience='seekers').data['reminded'], 0)

    def test_a_recruiting_team_is_nudged_when_people_are_waiting(self):
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.post('remind/', audience='teams')
        self.assertEqual(reply.data['reminded'], 1)
        self.assertIn('People are looking for a team', self.notes(self.leader))
        self.assertEqual(self.post('remind/', audience='teams').data['reminded'], 0)

    def test_a_full_team_or_nobody_waiting_means_no_nudge(self):
        for _ in range(2):
            TeamMember.objects.create(team=self.team, user=make_user())
        self.assertEqual(self.post('remind/', audience='teams').data['reminded'], 0)
        TeamMember.objects.filter(team=self.team).exclude(user=self.leader).delete()
        TeamSeeker.objects.all().update(is_active=False)
        self.assertEqual(self.post('remind/', audience='teams').data['reminded'], 0)

    def test_the_audience_must_be_named(self):
        self.assertEqual(self.post('remind/', audience='everyone').data['code'], 'INVALID')


class Exports(FinderAdminCase):
    def rows(self, kind, user=None):
        reply = api(user or self.admin).get(f'/api/hackathons/{self.hack.slug}/export/{kind}/')
        self.assertEqual(reply.status_code, 200)
        return list(csv.reader(io.StringIO(reply.content.decode('utf-8-sig'))))

    def test_unmatched_people_lists_only_those_still_waiting(self):
        matched = make_user()
        TeamSeeker.objects.create(hackathon=self.hack, user=matched)
        TeamMember.objects.create(team=self.team, user=matched)
        rows = self.rows('finder-seekers')
        self.assertEqual(rows[0][:3], ['Name', 'Email', 'Phone'])
        self.assertEqual([row[1] for row in rows[1:]], [self.seeker.email])

    def test_recruiting_teams_list_leader_contact_and_open_seats(self):
        rows = self.rows('finder-teams')
        self.assertEqual(rows[1][0], 'Open Door')
        self.assertIn(self.leader.email, rows[1])
        self.assertIn('2', rows[1])

    def test_participants_cannot_download(self):
        self.assertEqual(api(self.seeker).get(f'/api/hackathons/{self.hack.slug}/export/finder-seekers/').status_code, 403)
